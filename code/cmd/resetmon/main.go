// Command resetmon records every TCP reset on this machine, sent or received,
// in any namespace (bpf/resets.c), until it gets SIGINT or SIGTERM.
//
// Only resets of connections to the VIP are recorded, one CSV line each: when,
// whether it was sent or received, by which side ("server" = a backend, which
// uses the VIP as its address; "client" = the load generator), the client's
// address and port, and the director's bucket for that connection. A broken
// connection usually shows up as a reset sent by a backend that got a packet
// for a connection it doesn't have, and the same reset received by the client.
// Which backend sent a reset is not recorded: the namespaces' own counters
// (TcpOutRsts) tell that. A summary goes to stderr at the end.
//
// Usage (from the repo root): sudo bin/resetmon [-out resets.csv]
package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/netip"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"
	"github.com/cilium/ebpf/ringbuf"

	"cs331lb/internal/lb"
	"cs331lb/internal/testbed"
)

// event mirrors struct event in bpf/resets.c.
type event struct {
	TimeNs     uint64 // CLOCK_MONOTONIC
	SockCookie uint64
	Saddr      [4]byte // the side sending (sent) or receiving (received) the reset
	Daddr      [4]byte
	Sport      uint16
	Dport      uint16
	Kind       uint8 // 0 sent, 1 received
	_          [3]byte
}

func main() {
	out := flag.String("out", "", "CSV file (default stdout)")
	flag.Parse()
	cfg, err := testbed.Load(".")
	check(err)
	vipStr, err := cfg.Get("VIP")
	check(err)
	portStr, err := cfg.Get("BACKEND_PORT")
	check(err)
	vip := netip.MustParseAddr(vipStr)
	port, err := strconv.Atoi(portStr)
	check(err)

	coll, err := ebpf.LoadCollection("bin/bpf/resets.o")
	check(err)
	defer coll.Close()
	sent, err := link.AttachTracing(link.TracingOptions{Program: coll.Programs["on_send_reset"]}) // typed (tp_btf)
	check(err)
	defer sent.Close()
	received, err := link.Tracepoint("tcp", "tcp_receive_reset", coll.Programs["on_receive_reset"], nil)
	check(err)
	defer received.Close()
	reader, err := ringbuf.NewReader(coll.Maps["events"])
	check(err)

	w := bufio.NewWriter(os.Stdout)
	if *out != "" {
		f, err := os.Create(*out)
		check(err)
		defer f.Close()
		w = bufio.NewWriter(f)
	}
	defer w.Flush()
	fmt.Fprintln(w, "time_s,kind,side,client_ip,client_port,bucket,sock_cookie")

	// Stop cleanly on SIGINT/SIGTERM: closing the reader ends the loop below.
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-stop; reader.Close() }()

	counts := map[string]int{}
	brokenClients := map[uint64]bool{} // client sockets that received a reset
	for {
		record, err := reader.Read()
		if errors.Is(err, ringbuf.ErrClosed) {
			break
		}
		check(err)
		var e event
		check(binary.Read(bytes.NewReader(record.RawSample), binary.NativeEndian, &e))

		kind := [...]string{"sent", "received"}[e.Kind]
		// Which side is this, and who is the client?
		self, other := netip.AddrFrom4(e.Saddr), netip.AddrFrom4(e.Daddr)
		side, clientIP, clientPort, serverPort := "client", self, e.Sport, e.Dport
		if self == vip {
			side, clientIP, clientPort, serverPort = "server", other, e.Dport, e.Sport
		}
		if (self != vip && other != vip) || int(serverPort) != port {
			continue // not a connection to the VIP
		}
		bucket := lb.BucketOf(clientIP, vip, clientPort, serverPort)
		fmt.Fprintf(w, "%.6f,%s,%s,%s,%d,%d,%d\n", float64(e.TimeNs)/1e9, kind, side,
			clientIP, clientPort, bucket, e.SockCookie)
		counts[side+" "+kind]++
		if side == "client" && kind == "received" {
			brokenClients[e.SockCookie] = true
		}
	}
	fmt.Fprintf(os.Stderr, "resets: server sent %d, client received %d (%d distinct client sockets), client sent %d, server received %d\n",
		counts["server sent"], counts["client received"], len(brokenClients), counts["client sent"], counts["server received"])
}

func check(err error) {
	if err != nil {
		log.Fatal("resetmon: ", err)
	}
}
