package lb

// Golden-packet tests: run the real BPF programs in the kernel on hand-made
// packets with BPF_PROG_TEST_RUN and compare the output byte by byte.
// They need root (make test-bpf); the plain-Go tests at the bottom don't.
// Every director test runs twice, once for the TC program and once for the
// XDP one (D35), with the same expected bytes; only the return codes differ.
//
// The expected bytes were computed with an independent Python implementation
// of the same hash and checksums, so these tests check the C code, the Go
// mirror in lb.go, and our understanding of the format against each other.

import (
	"bytes"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math/rand/v2"
	"net/netip"
	"os"
	"strings"
	"testing"

	"github.com/cilium/ebpf"
)

const objDir = "../../bin/bpf" // built by make

// Return codes (linux/bpf.h, linux/pkt_cls.h).
const (
	xdpDrop       = 1
	xdpPass       = 2
	xdpTX         = 3
	tcActOK       = 0
	tcActShot     = 2
	tcActRedirect = 7
)

var (
	vip     = netip.MustParseAddr("10.2.0.1")
	lbIP    = netip.MustParseAddr("10.1.0.10")
	client  = netip.MustParseAddr("10.0.0.2")
	vipPort = uint16(8080)
	// Test backends: id -> address; MAC is 02:00:00:00:00:<last byte of IP> (10.1.0.11 -> ...:0b).
	testBackends = map[uint32]netip.Addr{
		1: netip.MustParseAddr("10.1.0.11"),
		2: netip.MustParseAddr("10.1.0.12"),
		3: netip.MustParseAddr("10.1.0.13"),
		4: netip.MustParseAddr("10.1.0.14"),
	}
)

func unhex(s string) []byte {
	b, err := hex.DecodeString(strings.ReplaceAll(s, " ", ""))
	if err != nil {
		panic(err)
	}
	return b
}

// The golden packet: a SYN from 10.0.0.2:40000 to the VIP 10.2.0.1:8080.
var (
	goldenIn = unhex("" +
		"02 00 00 00 00 10  02 00 00 00 00 01  08 00" + // eth: to the LB, from the router, IPv4
		goldenInner)
	goldenInnerIP = "45 00 00 28  00 01 40 00  40 06 26 cb  0a 00 00 02  0a 02 00 01" // IPv4 10.0.0.2 -> 10.2.0.1, TCP
	goldenInner   = goldenInnerIP +
		"9c 40 1f 90  00 00 00 01  00 00 00 00  50 02 ff ff  e0 0c 00 00" // TCP 40000 -> 8080, SYN

	// With every bucket = {first: 1 (10.1.0.11), second: 3 (10.1.0.13)}, table generation 1.
	// The 4-tuple hashes to 0xd993aef1: bucket 3825 (0x0ef1), UDP source port 0xc000|(hash>>18) = 63076.
	goldenOut = unhex("" +
		"02 00 00 00 00 0b  02 00 00 00 00 10  08 00" + // eth: to backend 1, from the LB
		"45 00 00 50  00 00 40 00  40 11 26 87  0a 01 00 0a  0a 01 00 0b" + // outer IPv4 10.1.0.10 -> 10.1.0.11, UDP, length 80
		"f6 64 1e 14  00 3c 00 00" + // UDP 63076 -> 7700, length 60, no checksum
		"01 00 0e f1  0a 01 00 0d  00 00 00 01" + // encap: version 1, flags 0, bucket 3825, second hop 10.1.0.13, generation 1
		goldenInner) // the original IPv4 packet, unchanged
)

// Field names by byte offset in an encapsulated frame, to explain mismatches.
var encapFields = []struct {
	name     string
	from, to int
}{
	{"eth.dst", 0, 6}, {"eth.src", 6, 12}, {"eth.type", 12, 14},
	{"ip.version_ihl", 14, 15}, {"ip.tos", 15, 16}, {"ip.tot_len", 16, 18}, {"ip.id", 18, 20},
	{"ip.frag_off", 20, 22}, {"ip.ttl", 22, 23}, {"ip.protocol", 23, 24}, {"ip.check", 24, 26},
	{"ip.saddr", 26, 30}, {"ip.daddr", 30, 34},
	{"udp.sport", 34, 36}, {"udp.dport", 36, 38}, {"udp.len", 38, 40}, {"udp.check", 40, 42},
	{"hdr.version", 42, 43}, {"hdr.flags", 43, 44}, {"hdr.bucket", 44, 46},
	{"hdr.second_hop", 46, 50}, {"hdr.table_gen", 50, 54},
	{"inner packet", 54, 1 << 30},
}

func compareEncap(t *testing.T, got, want []byte) {
	t.Helper()
	if bytes.Equal(got, want) {
		return
	}
	if len(got) != len(want) {
		t.Errorf("length: got %d, want %d", len(got), len(want))
	}
	for _, f := range encapFields {
		g, w := got[min(f.from, len(got)):min(f.to, len(got))], want[min(f.from, len(want)):min(f.to, len(want))]
		if !bytes.Equal(g, w) {
			t.Errorf("%-15s got % x, want % x", f.name, g, w)
		}
	}
}

func needRoot(t *testing.T) {
	if os.Geteuid() != 0 {
		t.Skip("needs root to run BPF programs (make test-bpf)")
	}
}

func mac(ip netip.Addr) [6]byte { return [6]byte{2, 0, 0, 0, 0, ip.As4()[3]} }

// hook: the director built for one hook, and what its verdicts are called there.
type hook struct {
	name             string
	pass, drop, sent uint32
}

var hooks = []hook{
	{"tc", tcActOK, tcActShot, tcActRedirect},
	{"xdp", xdpPass, xdpDrop, xdpTX},
}

func forEachHook(t *testing.T, test func(t *testing.T, h hook)) {
	for _, h := range hooks {
		t.Run(h.name, func(t *testing.T) { test(t, h) })
	}
}

// loadTestDirector loads the director for hook h with the test backends and,
// if buckets != nil, publishes them.
func loadTestDirector(t *testing.T, h hook, buckets []Bucket) (*ebpf.Collection, *ebpf.Program) {
	t.Helper()
	needRoot(t)
	coll, err := LoadDirector(objDir, h.name, vip, vipPort, lbIP)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(coll.Close)
	for id, ip := range testBackends {
		if err := coll.Maps["backends"].Put(id, Backend{IP: ip.As4(), MAC: mac(ip)}); err != nil {
			t.Fatal(err)
		}
	}
	if buckets != nil {
		if _, err := Publish(coll.Maps["tables"], buckets); err != nil {
			t.Fatal(err)
		}
	}
	return coll, coll.Programs[DirectorProgram(h.name)]
}

func uniform(b Bucket) []Bucket {
	buckets := make([]Bucket, NumBuckets)
	for i := range buckets {
		buckets[i] = b
	}
	return buckets
}

func run(t *testing.T, prog *ebpf.Program, in []byte) (uint32, []byte) {
	t.Helper()
	ret, out, err := prog.Test(in)
	if err != nil {
		t.Fatal(err)
	}
	return ret, out
}

func stat(t *testing.T, coll *ebpf.Collection, name string) uint64 {
	t.Helper()
	for i, n := range StatNames {
		if n == name {
			v, err := SumPerCPU(coll.Maps["stats"], uint32(i))
			if err != nil {
				t.Fatal(err)
			}
			return v
		}
	}
	t.Fatalf("no stat %q", name)
	return 0
}

// tcpPacket builds an Ethernet + IPv4 + TCP frame (checksums left zero: the
// director doesn't check them).
func tcpPacket(src, dst netip.Addr, sport, dport uint16) []byte {
	p := unhex("02 00 00 00 00 10  02 00 00 00 00 01  08 00" +
		"45 00 00 28  00 01 40 00  40 06 00 00  00 00 00 00  00 00 00 00" +
		"00 00 00 00  00 00 00 01  00 00 00 00  50 02 ff ff  00 00 00 00")
	s, d := src.As4(), dst.As4()
	copy(p[26:30], s[:])
	copy(p[30:34], d[:])
	binary.BigEndian.PutUint16(p[34:36], sport)
	binary.BigEndian.PutUint16(p[36:38], dport)
	return p
}

func TestGoldenSYN(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		coll, prog := loadTestDirector(t, h, uniform(Bucket{First: 1, Second: 3}))
		ret, out := run(t, prog, goldenIn)
		if ret != h.sent {
			t.Fatalf("return code %d, want %d (sent)", ret, h.sent)
		}
		compareEncap(t, out, goldenOut)
		if n := stat(t, coll, "tx"); n != 1 {
			t.Errorf("tx counter = %d, want 1", n)
		}
	})
}

func TestNotForVIPPassesUnchanged(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		_, prog := loadTestDirector(t, h, uniform(Bucket{First: 1}))
		cases := map[string][]byte{
			"TCP to the LB's own address": tcpPacket(client, lbIP, 40000, 22),
			"ARP request": unhex("ff ff ff ff ff ff  02 00 00 00 00 01  08 06" +
				"00 01 08 00 06 04 00 01  02 00 00 00 00 01 0a 01 00 01  00 00 00 00 00 00 0a 01 00 0a"),
		}
		// A truncated IPv4 header: the kernel's test runner refuses to feed one
		// to a TC program (it wants a full IPv4 header for an IPv4 frame), so
		// only the XDP run tests it. The bounds check is shared (choose()).
		if h.name == "xdp" {
			cases["runt frame"] = unhex("02 00 00 00 00 10  02 00 00 00 00 01  08 00  45 00")
		}
		for name, in := range cases {
			ret, out := run(t, prog, in)
			if ret != h.pass || !bytes.Equal(out, in) {
				t.Errorf("%s: return %d (want pass %d), changed=%v", name, ret, h.pass, !bytes.Equal(out, in))
			}
		}
	})
}

func TestDropsWhatItCannotBalance(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		coll, prog := loadTestDirector(t, h, uniform(Bucket{First: 1}))
		udp := tcpPacket(client, vip, 40000, vipPort)
		udp[23] = 17 // protocol UDP
		fragment := tcpPacket(client, vip, 40000, vipPort)
		fragment[20] = 0x20 // more fragments
		options := tcpPacket(client, vip, 40000, vipPort)
		options[14] = 0x46 // ihl 6: IP options, which the redirector would drop
		cases := map[string][]byte{
			"UDP to the VIP":      udp,
			"TCP to another port": tcpPacket(client, vip, 40000, 22),
			"fragment":            fragment,
			"IP options":          options,
		}
		for name, in := range cases {
			if ret, _ := run(t, prog, in); ret != h.drop {
				t.Errorf("%s: return %d, want drop (%d)", name, ret, h.drop)
			}
		}
		if n := stat(t, coll, "drop_proto"); n != uint64(len(cases)) {
			t.Errorf("drop_proto = %d, want %d", n, len(cases))
		}
	})
}

func TestDropsWithoutTableOrHop(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		coll, prog := loadTestDirector(t, h, nil) // no table published yet
		in := tcpPacket(client, vip, 40000, vipPort)
		if ret, _ := run(t, prog, in); ret != h.drop {
			t.Errorf("no table: return %d, want drop", ret)
		}
		if _, err := Publish(coll.Maps["tables"], uniform(Bucket{First: 0})); err != nil {
			t.Fatal(err)
		}
		if ret, _ := run(t, prog, in); ret != h.drop {
			t.Errorf("bucket without first hop: return %d, want drop", ret)
		}
		if n := stat(t, coll, "drop_no_hop"); n != 2 {
			t.Errorf("drop_no_hop = %d, want 2", n)
		}
	})
}

// For many random flows: the bucket in the header is the one Go computes, and
// the packet goes to that bucket's first hop with its second hop in the header.
func TestBucketsAndHopsMatchGo(t *testing.T) {
	buckets := make([]Bucket, NumBuckets)
	for i := range buckets {
		buckets[i] = Bucket{First: uint32(i%4) + 1, Second: uint32((i+1)%4) + 1}
	}
	forEachHook(t, func(t *testing.T, h hook) {
		_, prog := loadTestDirector(t, h, buckets)
		rng := rand.New(rand.NewPCG(1, 2))
		for range 2000 {
			src := netip.AddrFrom4([4]byte{10, byte(rng.IntN(256)), byte(rng.IntN(256)), byte(rng.IntN(256))})
			sport := uint16(1024 + rng.IntN(64000))
			ret, out := run(t, prog, tcpPacket(src, vip, sport, vipPort))
			if ret != h.sent {
				t.Fatalf("return %d, want %d (sent)", ret, h.sent)
			}
			want := BucketOf(src, vip, sport, vipPort)
			got := uint32(binary.BigEndian.Uint16(out[44:46]))
			if got != want {
				t.Fatalf("%v:%d: bucket in header %d, Go says %d", src, sport, got, want)
			}
			first, second := testBackends[buckets[want].First].As4(), testBackends[buckets[want].Second].As4()
			if !bytes.Equal(out[30:34], first[:]) || !bytes.Equal(out[46:50], second[:]) {
				t.Fatalf("bucket %d: outer dst % x / second hop % x, want % x / % x",
					want, out[30:34], out[46:50], first, second)
			}
		}
	})
}

// Ethernet pads short frames to 60 bytes. A bare ACK (54 bytes) arrives with 6
// bytes of padding, which must not end up inside the encapsulated packet.
func TestPaddedFrameLosesPadding(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		_, prog := loadTestDirector(t, h, uniform(Bucket{First: 1, Second: 3}))
		ackInner := unhex(goldenInnerIP + // same IPv4 header as the SYN (same length)
			"9c 40 1f 90  00 00 00 02  00 00 00 01  50 10 ff ff  df fc 00 00") // TCP ACK
		in := append(append(unhex("02 00 00 00 00 10  02 00 00 00 00 01  08 00"), ackInner...), make([]byte, 6)...)
		ret, out := run(t, prog, in)
		if ret != h.sent {
			t.Fatalf("return %d, want %d (sent)", ret, h.sent)
		}
		// Same flow and same length as the golden SYN, so the same outer headers.
		compareEncap(t, out, append(append([]byte(nil), goldenOut[:54]...), ackInner...))
	})
}

func TestDropsIfIPLengthExceedsFrame(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		coll, prog := loadTestDirector(t, h, uniform(Bucket{First: 1}))
		in := tcpPacket(client, vip, 40000, vipPort)
		binary.BigEndian.PutUint16(in[16:18], 1000) // tot_len says 1000 bytes, frame has 40
		if ret, _ := run(t, prog, in); ret != h.drop {
			t.Errorf("return %d, want drop", ret)
		}
		if n := stat(t, coll, "drop_error"); n != 1 {
			t.Errorf("drop_error = %d, want 1", n)
		}
	})
}

func TestNewTableTakesEffectWithNextGeneration(t *testing.T) {
	forEachHook(t, func(t *testing.T, h hook) {
		coll, prog := loadTestDirector(t, h, uniform(Bucket{First: 1}))
		gen, err := Publish(coll.Maps["tables"], uniform(Bucket{First: 2, Second: 4}))
		if err != nil {
			t.Fatal(err)
		}
		if gen != 2 {
			t.Errorf("generation %d, want 2", gen)
		}
		_, out := run(t, prog, goldenIn)
		want := append([]byte(nil), goldenOut...)
		copy(want[0:6], []byte{2, 0, 0, 0, 0, 12}) // eth.dst: backend 2
		copy(want[30:34], []byte{10, 1, 0, 12})    // ip.daddr: backend 2
		copy(want[24:26], []byte{0x26, 0x86})      // ip.check changes with daddr
		copy(want[46:50], []byte{10, 1, 0, 14})    // second hop: backend 4
		copy(want[50:54], []byte{0, 0, 0, 2})      // generation 2
		compareEncap(t, out, want)
	})
}

// --- Plain Go tests (no root needed) ---

// The Go hash must agree with the independent Python computation.
func TestFlowHashReference(t *testing.T) {
	if h := FlowHash(client, vip, 40000, 8080); h != 0xd993aef1 {
		t.Errorf("FlowHash = %#08x, want 0xd993aef1", h)
	}
}

func TestWithSecondHops(t *testing.T) {
	prev := []Bucket{{1, 0}, {1, 0}, {2, 3}, {2, 3}, {2, 3}, {1, 0}, {1, 0}}
	next := []Bucket{{1, 0}, {4, 0}, {2, 0}, {1, 0}, {2, 4}, {1, 4}, {3, 4}}
	want := []Bucket{
		{1, 0}, // unchanged, no second hop
		{4, 1}, // moved 1 -> 4: old first hop becomes second hop
		{2, 3}, // unchanged: keeps its second hop (3 may still have connections)
		{1, 2}, // moved 2 -> 1: second hop 2; connections still on 3 are lost (moved twice)
		{2, 3}, // unchanged: next's own second hop doesn't push out the old one
		{1, 4}, // unchanged, had no second hop: takes next's
		{3, 1}, // moved 1 -> 3: the old first hop wins over next's own second hop
	}
	if got := WithSecondHops(prev, next); fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("got %v, want %v", got, want)
	}
}

func TestApportion(t *testing.T) {
	cases := []struct {
		weights []float64
		want    []int
	}{
		{[]float64{1, 1, 1, 1}, []int{1024, 1024, 1024, 1024}},
		{[]float64{1, 1, 1}, []int{1366, 1365, 1365}},
		{[]float64{2, 0, 1}, []int{2731, 0, 1365}},
		{[]float64{1000, 1092}, []int{1958, 2138}},
	}
	for _, c := range cases {
		got, err := Apportion(NumBuckets, c.weights)
		if err != nil || fmt.Sprint(got) != fmt.Sprint(c.want) {
			t.Errorf("Apportion(%v) = %v, %v; want %v", c.weights, got, err, c.want)
		}
	}
	if _, err := Apportion(NumBuckets, []float64{0, 0}); err == nil {
		t.Error("all-zero weights: want an error")
	}
}
