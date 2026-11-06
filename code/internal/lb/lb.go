// Package lb is the user-space side of the load balancer: it loads the BPF
// programs built from bpf/ (see the Makefile), publishes bucket tables and
// reads the counters.
//
// The constants and struct layouts here mirror bpf/lb.h. If you change one,
// change the other; the golden-packet tests (director_test.go) catch a mismatch.
package lb

import (
	"encoding/binary"
	"fmt"
	"net/netip"
	"path/filepath"

	"github.com/cilium/ebpf"
)

const (
	NumBuckets   = 4096       // power of two
	GenSlot      = NumBuckets // extra table slot: the table generation
	MaxBackends  = 64         // ids 1..63; 0 = none
	HashSeed     = 0x5eed
	EncapPort    = 7700
	EncapVersion = 1
	FlagHopped   = 0x01
	EncapLen     = 20 + 8 + 12 // outer IPv4 + UDP + encap header
)

// Bucket is one table entry: backend ids of the first and second hop.
type Bucket struct {
	First  uint32 // gets new connections
	Second uint32 // gets packets the first hop has no connection for; 0 = none
}

// Backend is one entry of the backends map.
type Backend struct {
	IP  [4]byte // network byte order
	MAC [6]byte
	_   [2]byte // padding, as in the C struct
}

// --- Hash: Bob Jenkins' lookup3, same as jhash_3words in bpf/lb.h ---

func rol32(x uint32, k uint) uint32 { return x<<k | x>>(32-k) }

func jhash3Words(a, b, c, initval uint32) uint32 {
	initval += 0xdeadbeef + 3<<2
	a += initval
	b += initval
	c += initval
	c ^= b
	c -= rol32(b, 14)
	a ^= c
	a -= rol32(c, 11)
	b ^= a
	b -= rol32(a, 25)
	c ^= b
	c -= rol32(b, 16)
	a ^= c
	a -= rol32(c, 4)
	b ^= a
	b -= rol32(a, 14)
	c ^= b
	c -= rol32(b, 24)
	return c
}

// FlowHash hashes a TCP 4-tuple exactly like the director does.
func FlowHash(src, dst netip.Addr, sport, dport uint16) uint32 {
	return jhash3Words(addrToUint32(src), addrToUint32(dst), uint32(sport)<<16|uint32(dport), HashSeed)
}

// BucketOf returns the bucket the director uses for a TCP 4-tuple.
func BucketOf(src, dst netip.Addr, sport, dport uint16) uint32 {
	return FlowHash(src, dst, sport, dport) & (NumBuckets - 1)
}

func addrToUint32(a netip.Addr) uint32 {
	b := a.As4()
	return binary.BigEndian.Uint32(b[:])
}

// --- Loading ---

// LoadDirector loads the director for hook "tc" (bin/bpf/director_tc.o, the
// one we use) or "xdp" (director_xdp.o, side measurement only, D35), with the
// VIP, port and the LB's own address filled in. The returned collection holds
// the program (named DirectorProgram(hook)) and its maps.
func LoadDirector(objDir, hook string, vip netip.Addr, port uint16, lbIP netip.Addr) (*ebpf.Collection, error) {
	spec, err := ebpf.LoadCollectionSpec(filepath.Join(objDir, "director_"+hook+".o"))
	if err != nil {
		return nil, err
	}
	var portBE [2]byte
	binary.BigEndian.PutUint16(portBE[:], port)
	consts := map[string]any{"VIP": vip.As4(), "VIP_PORT": portBE, "LB_IP": lbIP.As4()}
	for name, value := range consts {
		if err := spec.Variables[name].Set(value); err != nil {
			return nil, fmt.Errorf("setting %s: %w", name, err)
		}
	}
	return ebpf.NewCollection(spec)
}

// LoadRedirector loads bin/bpf/redirector.o for one backend. checkSyncookie
// and forward are true except to show what goes wrong without them (forward
// false: decapsulate everything, never use the second hop).
func LoadRedirector(objDir string, self netip.Addr, checkSyncookie, forward bool) (*ebpf.Collection, error) {
	spec, err := ebpf.LoadCollectionSpec(filepath.Join(objDir, "redirector.o"))
	if err != nil {
		return nil, err
	}
	check := uint8(0)
	if checkSyncookie {
		check = 1
	}
	if err := spec.Variables["SELF_IP"].Set(self.As4()); err != nil {
		return nil, err
	}
	if err := spec.Variables["CHECK_SYNCOOKIE"].Set(check); err != nil {
		return nil, err
	}
	fwd := uint8(0)
	if forward {
		fwd = 1
	}
	if err := spec.Variables["FORWARD"].Set(fwd); err != nil {
		return nil, err
	}
	return ebpf.NewCollection(spec)
}

// DirectorProgram is the program's name in the collection LoadDirector returns.
func DirectorProgram(hook string) string { return "director_" + hook }

// --- Tables ---

// innerTableSpec must match `struct table` in bpf/director.h, or the kernel
// refuses to put the new table into the outer map.
var innerTableSpec = ebpf.MapSpec{
	Name:       "table",
	Type:       ebpf.Array,
	KeySize:    4,
	ValueSize:  8,
	MaxEntries: NumBuckets + 1,
}

// Publish makes buckets the active table: it fills a brand-new table, then
// swaps it in with a single update of the outer map, so packets see either the
// old table or the new one, never a mix. Returns the new generation number.
func Publish(tables *ebpf.Map, buckets []Bucket) (uint32, error) {
	if len(buckets) != NumBuckets {
		return 0, fmt.Errorf("need %d buckets, got %d", NumBuckets, len(buckets))
	}
	gen := uint32(1)
	if _, oldGen, err := Active(tables); err == nil {
		gen = oldGen + 1
	}

	table, err := ebpf.NewMap(&innerTableSpec)
	if err != nil {
		return 0, err
	}
	defer table.Close() // the outer map keeps its own reference
	keys := make([]uint32, NumBuckets+1)
	values := make([]Bucket, NumBuckets+1)
	for i := range keys {
		keys[i] = uint32(i)
	}
	copy(values, buckets)
	values[GenSlot] = Bucket{First: gen}
	if _, err := table.BatchUpdate(keys, values, nil); err != nil {
		return 0, err
	}
	if err := tables.Put(uint32(0), table); err != nil {
		return 0, err
	}
	return gen, nil
}

// Active reads the active table and its generation.
func Active(tables *ebpf.Map) ([]Bucket, uint32, error) {
	var id uint32 // looking up a map-of-maps from user space gives the inner map's id
	if err := tables.Lookup(uint32(0), &id); err != nil {
		return nil, 0, fmt.Errorf("no table published: %w", err)
	}
	table, err := ebpf.NewMapFromID(ebpf.MapID(id))
	if err != nil {
		return nil, 0, err
	}
	defer table.Close()
	buckets := make([]Bucket, NumBuckets+1)
	for i := range buckets {
		if err := table.Lookup(uint32(i), &buckets[i]); err != nil {
			return nil, 0, err
		}
	}
	return buckets[:NumBuckets], buckets[GenSlot].First, nil
}

// StaticTable splits the buckets between backends in proportion to their
// weights, rounding with the largest-remainder method so the counts add up
// to exactly NumBuckets. Each backend gets one contiguous run of buckets;
// that is fine because the hash already scatters flows randomly over buckets.
// It has no second hops: the controller's tables get them from rendezvous
// hashing (D32), and lbctl table derives them with WithSecondHops.
func StaticTable(ids []uint32, weights []float64) ([]Bucket, error) {
	counts, err := Apportion(NumBuckets, weights)
	if err != nil {
		return nil, err
	}
	buckets := make([]Bucket, 0, NumBuckets)
	for i, n := range counts {
		for range n {
			buckets = append(buckets, Bucket{First: ids[i]})
		}
	}
	return buckets, nil
}

// WithSecondHops prepares next to replace prev without breaking connections:
// a bucket whose first hop changes gets the old first hop as its second hop,
// so packets of connections opened before the change still reach the backend
// that has them (via the redirector). A bucket whose first hop stays keeps
// its old second hop. A bucket that changes again before its old connections
// have ended loses them: that is what RQ4 measures.
func WithSecondHops(prev, next []Bucket) []Bucket {
	out := make([]Bucket, len(next))
	for i := range next {
		out[i] = next[i]
		switch {
		case next[i].First != prev[i].First:
			out[i].Second = prev[i].First
		case prev[i].Second != 0:
			out[i].Second = prev[i].Second
		}
	}
	return out
}

// Apportion splits total into integer parts proportional to weights
// (largest-remainder method).
func Apportion(total int, weights []float64) ([]int, error) {
	sum := 0.0
	for _, w := range weights {
		if w < 0 {
			return nil, fmt.Errorf("negative weight %g", w)
		}
		sum += w
	}
	if sum == 0 {
		return nil, fmt.Errorf("all weights are zero")
	}
	counts := make([]int, len(weights))
	remainders := make([]float64, len(weights))
	given := 0
	for i, w := range weights {
		exact := float64(total) * w / sum
		counts[i] = int(exact)
		remainders[i] = exact - float64(counts[i])
		given += counts[i]
	}
	for ; given < total; given++ { // hand out what's left, largest remainder first
		best := 0
		for i := range remainders {
			if remainders[i] > remainders[best] {
				best = i
			}
		}
		counts[best]++
		remainders[best] = -1
	}
	return counts, nil
}

// --- Counters ---

// Stat names, in the order of the enums in bpf/director.h and bpf/redirector.c.
var (
	StatNames           = []string{"pass", "tx", "drop_proto", "drop_no_hop", "drop_error"}
	RedirectorStatNames = []string{"keep_hopped", "keep_no_second", "keep_syn", "keep_socket",
		"keep_cookie", "forward", "bad"}
)

// SumPerCPU reads entry key of a per-CPU array of uint64 counters and adds up the CPUs.
func SumPerCPU(m *ebpf.Map, key uint32) (uint64, error) {
	var perCPU []uint64
	if err := m.Lookup(key, &perCPU); err != nil {
		return 0, err
	}
	var total uint64
	for _, n := range perCPU {
		total += n
	}
	return total, nil
}
