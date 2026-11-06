// Command backend is the test server that sits behind the load balancer.
//
// Every request to /work does an amount of CPU *work* (not a fixed time), so a
// slower core takes longer for the same request, the way an older server
// generation would. With -cost exp the work is drawn per request from an
// exponential distribution with mean -work (the campaign's workload, D37: it
// matches the M/M/c model of internal/sim); with -cost fixed every request
// does exactly -work. -slowness multiplies the work, to emulate an even slower
// server on the same cores; it can be changed while running (GET
// /slowness?set=2 on the metrics port), so a backend can slow down without
// restarting and losing its connections (the step-change scenario).
//
// The work is done by a fixed pool of worker threads, each pinned to one of
// the CPUs we may use, taking requests from one FIFO queue (like a
// thread-pool server with one worker per core). Network handling gets one
// extra Go processor, so reading and queueing requests never has to wait for
// a worker to finish. See docs/decisions.md D24 for why the pool is pinned.
// So waiting requests queue inside this process, where the kernel (and PSI)
// can't see them. -mode kernel instead runs -threads unpinned worker threads
// (default 64), all allowed to run at once (GOMAXPROCS), so excess requests
// wait as runnable threads in the kernel's run queue, like a
// thread-per-request server (D25). Only for showing what PSI measures.
//
// Metrics (separate port, so reading them never counts as a request):
//   - requests in flight (RIF = queued + being worked on) and its time
//     integral, so a reader can compute the exact average RIF over any period:
//     avg RIF = Δ rif_seconds_total / Δ time
//   - how long the work itself took (no queueing): total and a cumulative
//     histogram service_le_<bound>ms
//
// Usage:
//
//	backend -work 200000 [-cost fixed|exp] [-slowness 1.0] [-mode pool|kernel] [-listen :8080] [-metrics :9100]
//	backend -calibrate      (prints work units per millisecond on this CPU)
package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"math/rand/v2"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"cs331lb/internal/work"
)

// rifTracker counts requests in flight and integrates that count over time.
type rifTracker struct {
	mu       sync.Mutex
	inflight int64
	integral float64 // sum of inflight * dt, in request-seconds
	last     time.Time
	done     uint64 // completed requests
}

// advance adds the time since the last change to the integral. Caller holds mu.
func (r *rifTracker) advance(now time.Time) {
	r.integral += float64(r.inflight) * now.Sub(r.last).Seconds()
	r.last = now
}

func (r *rifTracker) begin() {
	r.mu.Lock()
	r.advance(time.Now())
	r.inflight++
	r.mu.Unlock()
}

func (r *rifTracker) end() {
	r.mu.Lock()
	r.advance(time.Now())
	r.inflight--
	r.done++
	r.mu.Unlock()
}

func (r *rifTracker) snapshot() (inflight int64, integral float64, done uint64) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.advance(time.Now())
	return r.inflight, r.integral, r.done
}

// job is one request's work, handed from the HTTP handler to a worker.
type job struct {
	units  int
	ctx    context.Context // cancelled when the client disconnects
	result chan uint64
}

// cancelled counts queued requests skipped because their client had gone.
// Like real servers, we don't spend CPU on answers nobody will read: after an
// overload, a queue full of abandoned requests would otherwise keep the
// backend busy long after the load is gone (journal: campaign S2, first try).
var cancelled atomic.Uint64

// unitsDone counts work units actually done (slowness included), after the
// work: skipped requests don't count.
var unitsDone atomic.Uint64

// serviceStats records how long the work itself took (no queueing), as a
// cumulative histogram, so slow *work* can be told apart from waiting.
type serviceStats struct {
	mu      sync.Mutex
	seconds float64  // total time spent working
	counts  []uint64 // counts[i] = jobs that took <= bounds[i]
}

var serviceBoundsMs = []float64{0.5, 1, 1.25, 1.5, 2, 2.5, 3, 5, 10, 20, 50}

func (s *serviceStats) record(d time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.seconds += d.Seconds()
	ms := float64(d) / float64(time.Millisecond)
	for i, bound := range serviceBoundsMs {
		if ms <= bound {
			s.counts[i]++
		}
	}
}

// cpuMask is the bitmask format of sched_getaffinity/sched_setaffinity (up to 1024 CPUs).
type cpuMask [16]uint64

func affinity(op uintptr, mask *cpuMask) {
	// pid 0 = the calling thread
	_, _, errno := syscall.RawSyscall(op, 0, unsafe.Sizeof(*mask), uintptr(unsafe.Pointer(mask)))
	if errno != 0 {
		log.Fatalf("affinity syscall: %v", errno)
	}
}

// allowedCPUs lists the CPUs this process may run on (set by systemd-run AllowedCPUs).
func allowedCPUs() []int {
	var mask cpuMask
	affinity(syscall.SYS_SCHED_GETAFFINITY, &mask)
	var cpus []int
	for cpu := 0; cpu < len(mask)*64; cpu++ {
		if mask[cpu/64]&(1<<(cpu%64)) != 0 {
			cpus = append(cpus, cpu)
		}
	}
	return cpus
}

// worker runs on its own OS thread, pinned to one CPU, and serves jobs from
// the queue one at a time. Pinning matters: unpinned, the kernel sometimes put
// two workers on the same CPU, and ~10% of jobs took 2-2.5x as long.
func worker(cpu int, queue <-chan job, stats *serviceStats) {
	runtime.LockOSThread()
	if cpu >= 0 { // pool mode; in kernel mode (cpu < 0) the kernel places the thread
		var mask cpuMask
		mask[cpu/64] = 1 << (cpu % 64)
		affinity(syscall.SYS_SCHED_SETAFFINITY, &mask)
	}
	for j := range queue {
		if j.ctx.Err() != nil {
			cancelled.Add(1)
			j.result <- 0
			continue
		}
		start := time.Now()
		result := work.Burn(j.units)
		stats.record(time.Since(start))
		unitsDone.Add(uint64(j.units))
		j.result <- result
	}
}

// slowness is read by every request and may be changed at any time, so it
// lives in an atomic (as the bits of a float64).
type atomicFloat struct{ bits atomic.Uint64 }

func (f *atomicFloat) Load() float64   { return math.Float64frombits(f.bits.Load()) }
func (f *atomicFloat) Store(v float64) { f.bits.Store(math.Float64bits(v)) }

// requestUnits is the work for one request: the mean, or with exponential
// cost a draw with that mean (capped at 20× so one request can't stall a
// worker for long; that cap is hit by 2 in a billion requests).
func requestUnits(mean int, exponential bool) int {
	if !exponential {
		return mean
	}
	return int(float64(mean) * min(rand.ExpFloat64(), 20))
}

// cpuTicks sums busy and total time (USER_HZ ticks) of the given CPUs from
// /proc/stat: the whole-core CPU signal (internal/signals reads the same).
func cpuTicks(cpus []int) (busy, total uint64, err error) {
	data, err := os.ReadFile("/proc/stat")
	if err != nil {
		return 0, 0, err
	}
	want := map[string]bool{}
	for _, c := range cpus {
		want["cpu"+strconv.Itoa(c)] = true
	}
	for _, line := range strings.Split(string(data), "\n") {
		f := strings.Fields(line)
		if len(f) < 9 || !want[f[0]] {
			continue
		}
		var v [8]uint64
		for i := range v {
			v[i], _ = strconv.ParseUint(f[i+1], 10, 64)
		}
		b := v[0] + v[1] + v[2] + v[5] + v[6] + v[7] // user nice system irq softirq steal
		busy += b
		total += b + v[3] + v[4] // + idle iowait
	}
	return busy, total, nil
}

// ownCgroup is this process's cgroup directory (cgroup v2), from /proc/self/cgroup.
func ownCgroup() string {
	data, err := os.ReadFile("/proc/self/cgroup")
	if err != nil {
		return ""
	}
	_, path, _ := strings.Cut(strings.TrimSpace(string(data)), "::")
	return filepath.Join("/sys/fs/cgroup", path)
}

// cgroupCounter reads "key value" (cpu.stat) or "key=value" on the first line
// that has it (cpu.pressure "some ... total=N"). Fails if /sys/fs/cgroup isn't
// visible, e.g. under `ip netns exec`, which remounts /sys.
func cgroupCounter(dir, file, key string) (uint64, error) {
	data, err := os.ReadFile(filepath.Join(dir, file))
	if err != nil {
		return 0, err
	}
	for _, line := range strings.Split(string(data), "\n") {
		f := strings.Fields(line)
		for i, field := range f {
			if field == key && i+1 < len(f) {
				return strconv.ParseUint(f[i+1], 10, 64)
			}
			if v, ok := strings.CutPrefix(field, key+"="); ok {
				return strconv.ParseUint(v, 10, 64)
			}
		}
	}
	return 0, fmt.Errorf("no %s in %s", key, file)
}

// sink keeps calibrate's results alive; otherwise the compiler deletes the
// unused work.Burn() calls and the measurement is meaningless.
var sink uint64

func calibrate() {
	sink += work.Burn(1_000_000) // warm up caches
	const units = 5_000_000
	best := time.Duration(1 << 62)
	for i := 0; i < 5; i++ { // take the fastest of 5 runs: least disturbed
		start := time.Now()
		sink += work.Burn(units)
		if d := time.Since(start); d < best {
			best = d
		}
	}
	fmt.Printf("{\"units_per_ms\": %.1f}\n", units/(float64(best)/float64(time.Millisecond)))
}

func main() {
	listen := flag.String("listen", ":8080", "address for /work")
	metrics := flag.String("metrics", ":9100", "address for /metrics")
	units := flag.Int("work", 200000, "work units per request, the mean with -cost exp (override per request with ?work=N)")
	cost := flag.String("cost", "fixed", "per-request work: fixed, or exp (exponential with mean -work)")
	initialSlowness := flag.Float64("slowness", 1.0, "multiplier on all work (emulates a slower server)")
	mode := flag.String("mode", "pool", "pool (one pinned worker per CPU) or kernel (-threads unpinned workers, D25)")
	threads := flag.Int("threads", 64, "worker threads in -mode kernel")
	doCalibrate := flag.Bool("calibrate", false, "print work units per millisecond on this CPU and exit")
	flag.Parse()

	if *doCalibrate {
		calibrate()
		return
	}
	if *cost != "fixed" && *cost != "exp" {
		log.Fatalf("-cost must be fixed or exp, not %q", *cost)
	}
	var slowness atomicFloat
	slowness.Store(*initialSlowness)

	rif := &rifTracker{last: time.Now()}

	// Pool: one pinned worker per CPU we may use. Kernel: many unpinned ones.
	// Plus one Go processor for networking.
	cpus := allowedCPUs()
	workerCPUs := cpus
	switch *mode {
	case "pool":
	case "kernel":
		workerCPUs = make([]int, *threads)
		for i := range workerCPUs {
			workerCPUs[i] = -1 // unpinned
		}
	default:
		log.Fatalf("-mode must be pool or kernel, not %q", *mode)
	}
	workers := len(workerCPUs)
	runtime.GOMAXPROCS(workers + 1) // every worker can run at once: waiting happens in the kernel
	queue := make(chan job, 1<<16)  // FIFO: requests are served in arrival order
	stats := &serviceStats{counts: make([]uint64, len(serviceBoundsMs))}
	for _, cpu := range workerCPUs {
		go worker(cpu, queue, stats)
	}

	app := http.NewServeMux()
	// /upload reads the whole request body and answers with its length: a test
	// that large requests (which the client sends as large GSO packets) survive
	// the LB and the redirector intact (bench/e2e_lb.py).
	app.HandleFunc("/upload", func(w http.ResponseWriter, req *http.Request) {
		n, err := io.Copy(io.Discard, req.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		fmt.Fprintf(w, "%d\n", n)
	})

	app.HandleFunc("/work", func(w http.ResponseWriter, req *http.Request) {
		rif.begin()
		defer rif.end()
		n := *units
		if s := req.URL.Query().Get("work"); s != "" {
			if v, err := strconv.Atoi(s); err == nil && v >= 0 {
				n = v
			}
		}
		j := job{units: int(float64(requestUnits(n, *cost == "exp")) * slowness.Load()), ctx: req.Context(), result: make(chan uint64, 1)}
		queue <- j
		fmt.Fprintf(w, "%x\n", <-j.result)
	})

	// Metrics live on their own port so that reading them never counts as a request.
	mon := http.NewServeMux()
	cgroupDir := ownCgroup()
	mon.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		inflight, integral, done := rif.snapshot()
		// Raw counters for every load signal, so a controller on another
		// machine can compute them all from here (cluster mode).
		if busy, total, err := cpuTicks(cpus); err == nil {
			fmt.Fprintf(w, "cpu_busy_ticks %d\ncpu_total_ticks %d\n", busy, total)
		}
		if v, err := cgroupCounter(cgroupDir, "cpu.stat", "usage_usec"); err == nil {
			fmt.Fprintf(w, "cgroup_usage_usec %d\n", v)
		}
		if v, err := cgroupCounter(cgroupDir, "cpu.pressure", "total"); err == nil {
			fmt.Fprintf(w, "cgroup_pressure_some_usec %d\n", v)
		}
		fmt.Fprintf(w, "rif_inflight %d\nrif_seconds_total %.6f\nrequests_total %d\nwork_units %d\nwork_units_total %d\nslowness %g\nrequests_cancelled_total %d\n",
			inflight, integral, done, *units, unitsDone.Load(), slowness.Load(), cancelled.Load())
		stats.mu.Lock()
		defer stats.mu.Unlock()
		fmt.Fprintf(w, "service_seconds_total %.6f\n", stats.seconds)
		for i, bound := range serviceBoundsMs {
			fmt.Fprintf(w, "service_le_%gms %d\n", bound, stats.counts[i])
		}
	})
	mon.HandleFunc("/slowness", func(w http.ResponseWriter, req *http.Request) {
		v, err := strconv.ParseFloat(req.URL.Query().Get("set"), 64)
		if err != nil || v <= 0 {
			http.Error(w, "want /slowness?set=<positive number>", http.StatusBadRequest)
			return
		}
		slowness.Store(v)
		log.Printf("slowness now %g", v)
		fmt.Fprintf(w, "slowness %g\n", v)
	})
	go func() { log.Fatal(http.ListenAndServe(*metrics, mon)) }()

	log.Printf("backend: listen %s, metrics %s, work %d (%s), slowness %g, %d workers (%s mode)",
		*listen, *metrics, *units, *cost, slowness.Load(), workers, *mode)
	srv := &http.Server{Addr: *listen, Handler: app, ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
