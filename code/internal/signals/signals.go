// Package signals measures each backend's load relative to its capacity
// (0 = idle, 1 = fully loaded) over a control period, from different sources
// (D10). Every signal reads cumulative counters and differences them between
// reads, so nothing that happens between two reads is missed.
package signals

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"cs331lb/internal/testbed"
)

// A Signal returns one value per backend (backends.conf order) for the time
// since its previous Read. The first Read after New covers the time since New.
type Signal interface {
	Read() ([]float64, error)
}

// New returns the named signal for the testbed's backends:
// "cpu" (whole-core CPU), "rif" (requests in flight per core), "rif-total"
// (requests in flight, not divided by cores: to tell whether RIF's result in
// S2 comes from the normalisation), "cgroup" (the backend's own CPU time) or
// "psi" (CPU pressure of the backend's cgroup).
func New(name string, cfg *testbed.Config) (Signal, error) {
	if cfg.Cluster { // the backends are on other machines: everything comes from their /metrics
		s, err := fromMetrics(name, cfg)
		if err != nil {
			return nil, err
		}
		_, err = s.Read()
		return s, err
	}
	var s Signal
	switch name {
	case "cpu":
		s = &WholeCoreCPU{ProcStat: "/proc/stat", CPUs: cpusOf(cfg)}
	case "rif", "rif-total":
		port, err := cfg.Get("METRICS_PORT")
		if err != nil {
			return nil, err
		}
		cores := coresOf(cfg)
		if name == "rif-total" {
			for i := range cores {
				cores[i] = 1
			}
		}
		s = &RIF{Cores: cores, Fetch: func(i int) (string, error) {
			return Metrics(cfg, i, port)
		}}
	case "cgroup":
		s = &CgroupCPU{Dirs: cgroupDirs(cfg), Cores: coresOf(cfg)}
	case "psi":
		s = &PSI{Dirs: cgroupDirs(cfg)}
	default:
		return nil, fmt.Errorf("unknown signal %q (cpu, rif, rif-total, cgroup, psi)", name)
	}
	_, err := s.Read() // prime the counters
	return s, err
}

func cpusOf(cfg *testbed.Config) [][]int {
	cpus := make([][]int, len(cfg.Backends))
	for i, be := range cfg.Backends {
		cpus[i] = testbed.ExpandCPUs(be.CPUs)
	}
	return cpus
}

func coresOf(cfg *testbed.Config) []float64 {
	cores := make([]float64, len(cfg.Backends))
	for i, be := range cfg.Backends {
		cores[i] = float64(len(testbed.ExpandCPUs(be.CPUs)))
	}
	return cores
}

// cgroupDirs: where systemd puts each backend (bench/testbed.py starts them
// as units cs331-<name> in cs331.slice).
func cgroupDirs(cfg *testbed.Config) []string {
	dirs := make([]string, len(cfg.Backends))
	for i, be := range cfg.Backends {
		dirs[i] = filepath.Join("/sys/fs/cgroup/cs331.slice", "cs331-"+be.Name+".service")
	}
	return dirs
}

// --- Whole-core CPU (main signal) ---

// WholeCoreCPU: the fraction of time the backend's cores were busy with
// anything (our backend, an antagonist, interrupts), from /proc/stat.
type WholeCoreCPU struct {
	ProcStat            string
	CPUs                [][]int // per backend
	prevBusy, prevTotal []uint64
}

func (s *WholeCoreCPU) Read() ([]float64, error) {
	busy, total, err := readProcStat(s.ProcStat)
	if err != nil {
		return nil, err
	}
	u := make([]float64, len(s.CPUs))
	curBusy, curTotal := make([]uint64, len(s.CPUs)), make([]uint64, len(s.CPUs))
	for i, cpus := range s.CPUs {
		for _, c := range cpus {
			curBusy[i] += busy[c]
			curTotal[i] += total[c]
		}
		if s.prevTotal != nil && curTotal[i] > s.prevTotal[i] {
			u[i] = float64(curBusy[i]-s.prevBusy[i]) / float64(curTotal[i]-s.prevTotal[i])
		}
	}
	s.prevBusy, s.prevTotal = curBusy, curTotal
	return u, nil
}

// readProcStat returns busy and total time (in ticks) per CPU number.
// Columns: user nice system idle iowait irq softirq steal guest guest_nice;
// guest time is already included in user/nice.
func readProcStat(path string) (busy, total map[int]uint64, err error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()
	busy, total = map[int]uint64{}, map[int]uint64{}
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) < 9 || !strings.HasPrefix(fields[0], "cpu") || fields[0] == "cpu" {
			continue
		}
		cpu, err := strconv.Atoi(strings.TrimPrefix(fields[0], "cpu"))
		if err != nil {
			continue
		}
		var v [8]uint64
		for k := range v {
			v[k], _ = strconv.ParseUint(fields[k+1], 10, 64)
		}
		idle := v[3] + v[4] // idle + iowait
		b := v[0] + v[1] + v[2] + v[5] + v[6] + v[7]
		busy[cpu], total[cpu] = b, b+idle
	}
	return busy, total, sc.Err()
}

// --- Requests in flight (main signal) ---

// RIF: the average number of requests in flight per core over the period,
// from the backend's own time integral (rif_seconds_total, cmd/backend):
// avg RIF = Δ rif_seconds_total / Δ time.
type RIF struct {
	Cores []float64
	Fetch func(backend int) (string, error) // returns the backend's /metrics text
	prev  []float64
	when  time.Time
}

func (s *RIF) Read() ([]float64, error) {
	now := time.Now()
	cur := make([]float64, len(s.Cores))
	for i := range s.Cores {
		text, err := s.Fetch(i)
		if err != nil {
			return nil, err
		}
		if cur[i], err = metric(text, "rif_seconds_total"); err != nil {
			return nil, err
		}
	}
	u := make([]float64, len(s.Cores))
	if s.prev != nil {
		dt := now.Sub(s.when).Seconds()
		for i := range u {
			u[i] = (cur[i] - s.prev[i]) / dt / s.Cores[i]
		}
	}
	s.prev, s.when = cur, now
	return u, nil
}

func metric(text, name string) (float64, error) {
	for _, line := range strings.Split(text, "\n") {
		if f := strings.Fields(line); len(f) == 2 && f[0] == name {
			return strconv.ParseFloat(f[1], 64)
		}
	}
	return 0, fmt.Errorf("no metric %s", name)
}

// --- Cgroup CPU and PSI (extra signals for the key scenarios) ---

// CgroupCPU: CPU time used by the backend's own cgroup, per allocated core.
// Blind to anything else on the same cores (an antagonist in another cgroup).
type CgroupCPU struct {
	Dirs  []string
	Cores []float64
	prev  []float64
	when  time.Time
}

func (s *CgroupCPU) Read() ([]float64, error) {
	return readDelta(s.Dirs, "cpu.stat", "usage_usec", &s.prev, &s.when, func(i int, perSecond float64) float64 {
		return perSecond / 1e6 / s.Cores[i]
	})
}

// PSI: the fraction of time at least one of the backend's tasks was waiting
// for a CPU ("some" CPU pressure of its cgroup). Not a utilisation: it only
// rises when tasks queue in the kernel (see D25).
type PSI struct {
	Dirs []string
	prev []float64
	when time.Time
}

func (s *PSI) Read() ([]float64, error) {
	return readDelta(s.Dirs, "cpu.pressure", "total", &s.prev, &s.when, func(_ int, perSecond float64) float64 {
		return perSecond / 1e6
	})
}

// readDelta reads counter `key` from file `name` in each dir and turns the
// change since the previous read into a per-second rate, scaled by norm.
func readDelta(dirs []string, name, key string, prev *[]float64, when *time.Time, norm func(int, float64) float64) ([]float64, error) {
	now := time.Now()
	cur := make([]float64, len(dirs))
	for i, dir := range dirs {
		data, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return nil, err
		}
		if cur[i], err = counter(string(data), key); err != nil {
			return nil, fmt.Errorf("%s/%s: %w", dir, name, err)
		}
	}
	u := make([]float64, len(dirs))
	if *prev != nil {
		dt := now.Sub(*when).Seconds()
		for i := range u {
			u[i] = norm(i, (cur[i]-(*prev)[i])/dt)
		}
	}
	*prev, *when = cur, now
	return u, nil
}

// counter finds "key value" (cpu.stat) or "key=value" on the first line that
// has it ("some avg10=... total=N" in cpu.pressure).
func counter(text, key string) (float64, error) {
	for _, line := range strings.Split(text, "\n") {
		for i, f := range strings.Fields(line) {
			if f == key && i+1 < len(strings.Fields(line)) {
				return strconv.ParseFloat(strings.Fields(line)[i+1], 64)
			}
			if v, ok := strings.CutPrefix(f, key+"="); ok {
				return strconv.ParseFloat(v, 64)
			}
		}
	}
	return 0, fmt.Errorf("no %s", key)
}

// --- From the backends' /metrics (cluster mode) ---

// FromMetrics computes a signal from counters in each backend's /metrics: the
// change of Num divided by the change of Den (Den "" = per second), divided by
// Per[i] (cores, or 1). The backend exports the same counters the local
// signals read from /proc and /sys (cmd/backend), so the values match.
type FromMetrics struct {
	Fetch    func(backend int) (string, error)
	Num, Den string
	Per      []float64
	Scale    float64 // e.g. 1e-6 for microsecond counters
	prevNum  []float64
	prevDen  []float64
	prevWhen time.Time
}

func fromMetrics(name string, cfg *testbed.Config) (*FromMetrics, error) {
	port, err := cfg.Get("METRICS_PORT")
	if err != nil {
		return nil, err
	}
	ones := make([]float64, len(cfg.Backends))
	for i := range ones {
		ones[i] = 1
	}
	s := &FromMetrics{Fetch: func(i int) (string, error) { return Metrics(cfg, i, port) }, Per: ones, Scale: 1}
	switch name {
	case "cpu":
		s.Num, s.Den = "cpu_busy_ticks", "cpu_total_ticks"
	case "rif":
		s.Num, s.Per = "rif_seconds_total", coresOf(cfg)
	case "rif-total":
		s.Num = "rif_seconds_total"
	case "cgroup":
		s.Num, s.Per, s.Scale = "cgroup_usage_usec", coresOf(cfg), 1e-6
	case "psi":
		s.Num, s.Scale = "cgroup_pressure_some_usec", 1e-6
	default:
		return nil, fmt.Errorf("unknown signal %q (cpu, rif, rif-total, cgroup, psi)", name)
	}
	return s, nil
}

func (s *FromMetrics) Read() ([]float64, error) {
	now := time.Now()
	num, den := make([]float64, len(s.Per)), make([]float64, len(s.Per))
	for i := range s.Per {
		text, err := s.Fetch(i)
		if err != nil {
			return nil, err
		}
		if num[i], err = metric(text, s.Num); err != nil {
			return nil, err
		}
		if s.Den != "" {
			if den[i], err = metric(text, s.Den); err != nil {
				return nil, err
			}
		}
	}
	u := make([]float64, len(s.Per))
	if s.prevNum != nil {
		for i := range u {
			d := now.Sub(s.prevWhen).Seconds()
			if s.Den != "" {
				d = den[i] - s.prevDen[i]
			}
			if d > 0 {
				u[i] = (num[i] - s.prevNum[i]) * s.Scale / d / s.Per[i]
			}
		}
	}
	s.prevNum, s.prevDen, s.prevWhen = num, den, now
	return u, nil
}

// --- Fake (tests and dry runs) ---

// Fake returns whatever its function says.
type Fake struct {
	F func() []float64
}

func (s *Fake) Read() ([]float64, error) { return s.F(), nil }
