// Package work is the CPU work shared by the backend (one request = a fixed
// number of work units) and the antagonist (burns work to steal CPU).
package work

// table is 1 MiB of pseudo-random numbers. Burn reads it at random offsets,
// so cache size and memory latency matter, not only clock speed.
var table [1 << 17]uint64

func init() {
	x := uint64(88172645463325252)
	for i := range table {
		x ^= x << 13
		x ^= x >> 7
		x ^= x << 17
		table[i] = x
	}
}

// Burn does n units of work. Each unit depends on the previous one, so the
// CPU can't skip ahead. It returns the result so the compiler can't drop it;
// callers must use the result (or add it to a package-level variable).
//
// noinline: otherwise the compiler inlines Burn where n is a constant (as in
// calibration) and generates a ~20% faster loop than the one requests run,
// so calibration would overstate the speed.
//
//go:noinline
func Burn(n int) uint64 {
	x := uint64(n) | 1
	for i := 0; i < n; i++ {
		x = (x ^ table[(x>>20)&(uint64(len(table))-1)]) * 0x9E3779B97F4A7C15
	}
	return x
}
