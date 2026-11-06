package lb

// Golden-packet tests for the redirector (bpf/redirector.c), run in the
// kernel like the director's. The redirector's socket lookup happens in the
// namespace of the test process, so the tests open real TCP connections on
// 127.0.0.1 and use their 4-tuples as the "client's" packet inside the
// encapsulation. (The SYN-cookie case needs a real handshake; it is covered
// by bench/move_test.py.)

import (
	"bytes"
	"encoding/binary"
	"net"
	"net/netip"
	"testing"

	"github.com/cilium/ebpf"
)

var self = testBackends[1] // the backend running the redirector: 10.1.0.11

func loadTestRedirector(t *testing.T) *ebpf.Collection {
	t.Helper()
	needRoot(t)
	coll, err := LoadRedirector(objDir, self, true, true)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(coll.Close)
	return coll
}

// Offsets in an encapsulated frame (see encapFields in director_test.go).
const (
	offHdrFlags  = 43
	offSecondHop = 46
	offInner     = 54
	offTCPFlags  = offInner + 20 + 13
)

// encapsulated returns the golden encapsulated SYN with its inner 4-tuple
// replaced, and the given TCP flags, encap flags and second hop.
func encapsulated(src, dst netip.AddrPort, tcpFlags, encapFlags byte, second netip.Addr) []byte {
	p := append([]byte(nil), goldenOut...)
	s, d := src.Addr().As4(), dst.Addr().As4()
	var h [4]byte // zero = no second hop
	if second.IsValid() {
		h = second.As4()
	}
	copy(p[offInner+12:], s[:])
	copy(p[offInner+16:], d[:])
	binary.BigEndian.PutUint16(p[offInner+20:], src.Port())
	binary.BigEndian.PutUint16(p[offInner+22:], dst.Port())
	p[offTCPFlags] = tcpFlags
	p[offHdrFlags] = encapFlags
	copy(p[offSecondHop:], h[:])
	return p
}

const (
	tcpSYN = 0x02
	tcpACK = 0x10
)

// decapsulated is what a kept packet must look like: Ethernet header + inner packet.
func decapsulated(in []byte) []byte {
	return append(append([]byte(nil), in[:14]...), in[offInner:]...)
}

func redirectorStat(t *testing.T, coll *ebpf.Collection, name string) uint64 {
	t.Helper()
	for i, n := range RedirectorStatNames {
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

// Real sockets on loopback: an established connection and the listener it came from.
func loopbackConnection(t *testing.T) (listener net.Listener, clientEnd netip.AddrPort) {
	t.Helper()
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.Close() })
	c, err := net.Dial("tcp4", l.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	s, err := l.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.Close() })
	return l, netip.MustParseAddrPort(c.LocalAddr().String())
}

// No socket for the flow (the golden one: 10.0.0.2:40000 -> VIP) and a second
// hop: forward. Only the outer IPv4 header and the HOPPED flag change.
func TestRedirectorGoldenForward(t *testing.T) {
	coll := loadTestRedirector(t)
	in := append([]byte(nil), goldenOut...)
	in[offTCPFlags] = tcpACK // not a SYN

	ret, out := run(t, coll.Programs["redirector"], in)
	if ret != tcActRedirect {
		t.Fatalf("return %d, want TC_ACT_REDIRECT (%d)", ret, tcActRedirect)
	}
	want := append([]byte(nil), in...)
	copy(want[14:34], unhex("45 00 00 50  00 00 40 00  40 11 26 84  0a 01 00 0b  0a 01 00 0d")) // 10.1.0.11 -> 10.1.0.13
	want[offHdrFlags] = FlagHopped
	compareEncap(t, out, want)
	if n := redirectorStat(t, coll, "forward"); n != 1 {
		t.Errorf("forward = %d, want 1", n)
	}
}

func TestRedirectorKeeps(t *testing.T) {
	coll := loadTestRedirector(t)
	listener, clientEnd := loopbackConnection(t)
	server := netip.MustParseAddrPort(listener.Addr().String())
	clientAddr := netip.MustParseAddrPort("10.0.0.2:40000")
	vipAddr := netip.AddrPortFrom(vip, vipPort)
	second := testBackends[3]

	cases := []struct {
		name string
		in   []byte
		stat string
	}{
		{"SYN", encapsulated(clientAddr, vipAddr, tcpSYN, 0, second), "keep_syn"},
		{"already hopped", encapsulated(clientAddr, vipAddr, tcpACK, FlagHopped, second), "keep_hopped"},
		{"no second hop", encapsulated(clientAddr, vipAddr, tcpACK, 0, netip.Addr{}), "keep_no_second"},
		{"established socket", encapsulated(clientEnd, server, tcpACK, 0, second), "keep_socket"},
	}
	for _, c := range cases {
		ret, out := run(t, coll.Programs["redirector"], c.in)
		if ret != tcActOK || !bytes.Equal(out, decapsulated(c.in)) {
			t.Errorf("%s: return %d (want TC_ACT_OK %d), decapsulated correctly: %v",
				c.name, ret, tcActOK, bytes.Equal(out, decapsulated(c.in)))
		}
		if n := redirectorStat(t, coll, c.stat); n != 1 {
			t.Errorf("%s: %s = %d, want 1", c.name, c.stat, n)
		}
	}
}

func TestRedirectorForwardsWithoutConnection(t *testing.T) {
	coll := loadTestRedirector(t)
	listener, _ := loopbackConnection(t)
	server := netip.MustParseAddrPort(listener.Addr().String())
	closed, err := net.Listen("tcp4", "127.0.0.1:0") // a port nobody listens on
	if err != nil {
		t.Fatal(err)
	}
	closedPort := netip.MustParseAddrPort(closed.Addr().String())
	closed.Close()
	stranger := netip.MustParseAddrPort("127.0.0.1:1") // no connection from this port

	cases := map[string][]byte{
		// Only the listener matches, and the ACK is not a valid SYN cookie.
		"listener only": encapsulated(stranger, server, tcpACK, 0, testBackends[3]),
		"no socket":     encapsulated(stranger, closedPort, tcpACK, 0, testBackends[3]),
	}
	for name, in := range cases {
		if ret, out := run(t, coll.Programs["redirector"], in); ret != tcActRedirect || out[offHdrFlags] != FlagHopped {
			t.Errorf("%s: return %d (want TC_ACT_REDIRECT %d), flags %#x", name, ret, tcActRedirect, out[offHdrFlags])
		}
	}
}

// With forwarding off (the RQ4 contrast) a packet without a connection is
// decapsulated and kept, as if there were no second hop.
func TestRedirectorWithoutForwardingKeepsEverything(t *testing.T) {
	needRoot(t)
	coll, err := LoadRedirector(objDir, self, true, false)
	if err != nil {
		t.Fatal(err)
	}
	defer coll.Close()
	in := append([]byte(nil), goldenOut...)
	in[offTCPFlags] = tcpACK // no socket for it, second hop set: would be forwarded
	if ret, out := run(t, coll.Programs["redirector"], in); ret != tcActOK || !bytes.Equal(out, decapsulated(in)) {
		t.Errorf("return %d (want TC_ACT_OK), decapsulated: %v", ret, bytes.Equal(out, decapsulated(in)))
	}
}

func TestRedirectorLeavesOtherTrafficAlone(t *testing.T) {
	coll := loadTestRedirector(t)
	plain := tcpPacket(client, vip, 40000, vipPort)
	arp := unhex("ff ff ff ff ff ff  02 00 00 00 00 01  08 06" +
		"00 01 08 00 06 04 00 01  02 00 00 00 00 01 0a 01 00 01  00 00 00 00 00 00 0a 01 00 0b")
	for name, in := range map[string][]byte{"plain TCP": plain, "ARP": arp} {
		if ret, out := run(t, coll.Programs["redirector"], in); ret != tcActOK || !bytes.Equal(out, in) {
			t.Errorf("%s: return %d, changed=%v; want TC_ACT_OK and unchanged", name, ret, !bytes.Equal(out, in))
		}
	}
	bad := append([]byte(nil), goldenOut...)
	bad[42] = 9 // unknown encapsulation version
	if ret, _ := run(t, coll.Programs["redirector"], bad); ret != tcActShot {
		t.Errorf("unknown version: return %d, want TC_ACT_SHOT (%d)", ret, tcActShot)
	}
}
