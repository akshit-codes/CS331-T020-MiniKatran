// Command antagonist steals CPU from whatever shares its cores: on every CPU
// it may use, one thread is busy for `duty` of each 10 ms period and sleeps
// for the rest. It does the same work as a backend request (internal/work),
// so it competes for the same caches as well as for CPU time.
//
// Run it on a backend's cores, in its own cgroup, to emulate background work
// or a noisy neighbour.
//
// -kind chooses what it competes for, which matters on a hyperthread sibling
// (the two threads of a core share its execution units and L1/L2 caches):
//   - burn:   the request work itself (default). On a sibling it barely
//     contends: both threads wait on their own dependency chains and read the
//     same table (campaign S4).
//   - mul:    independent multiplications, keeping the multiplier busy.
//   - stream: walks a 64 MiB buffer, evicting the request work's table from
//     the caches the siblings share.
//
// Usage: antagonist -duty 0.7 [-kind burn|mul|stream]
package main

import (
	"flag"
	"log"
	"runtime"
	"sync/atomic"
	"time"

	"cs331lb/internal/work"
)

// sink collects the work results so the compiler can't delete the work.
var sink atomic.Uint64

func main() {
	duty := flag.Float64("duty", 0.5, "fraction of each period spent busy (0..1)")
	period := flag.Duration("period", 10*time.Millisecond, "length of one busy+sleep cycle")
	kind := flag.String("kind", "burn", "burn, mul or stream (see above)")
	flap := flag.Duration("flap", 0, "if > 0: alternate between on and off phases of this length (a disturbance faster than starting and stopping the process allows)")
	flag.Parse()
	work1 := map[string]func() uint64{"burn": func() uint64 { return work.Burn(1000) }, "mul": multiply, "stream": stream}[*kind]
	if work1 == nil {
		log.Fatalf("-kind must be burn, mul or stream, not %q", *kind)
	}
	if *duty < 0 || *duty > 1 {
		log.Fatal("-duty must be between 0 and 1")
	}

	threads := runtime.GOMAXPROCS(0) // = number of CPUs we're allowed to use
	busy := time.Duration(float64(*period) * *duty)
	log.Printf("antagonist: %d threads, %s, duty %.2f, period %v", threads, *kind, *duty, *period)
	begin := time.Now() // all threads share the phases
	for i := 0; i < threads; i++ {
		go func() {
			runtime.LockOSThread()
			for {
				if *flap > 0 {
					if since := time.Since(begin); (since / *flap)%2 == 1 { // an off phase: sleep to its end
						time.Sleep(*flap - since%*flap)
						continue
					}
				}
				start := time.Now()
				var result uint64
				for time.Since(start) < busy {
					result += work1()
				}
				sink.Add(result)
				time.Sleep(*period - busy)
			}
		}()
	}
	select {} // run until killed
}

// multiply runs 8 independent multiplication chains, so the multiplier
// always has work ready (unlike the request work's single dependent chain).
func multiply() uint64 {
	var a [8]uint64
	for i := range a {
		a[i] = uint64(i) | 1
	}
	for n := 0; n < 1000; n++ {
		for i := range a {
			a[i] *= 0x9E3779B97F4A7C15
		}
	}
	return a[0] ^ a[1] ^ a[2] ^ a[3] ^ a[4] ^ a[5] ^ a[6] ^ a[7]
}

// buffer is far larger than any cache, so walking it evicts everything else.
var buffer = make([]uint64, 64<<20/8)
var cursor int

// stream reads and writes one word per 64-byte cache line, 4096 lines per call.
// (Each thread has its own position only roughly: they share cursor, which is
// fine for evicting caches.)
func stream() uint64 {
	var sum uint64
	for n := 0; n < 4096; n++ {
		cursor = (cursor + 8) % len(buffer)
		buffer[cursor]++
		sum += buffer[cursor]
	}
	return sum
}
