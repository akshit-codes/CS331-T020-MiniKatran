package signals

import (
	"math"
	"os"
	"path/filepath"
	"testing"

	"cs331lb/internal/testbed"
)

func near(a, b float64) bool { return math.Abs(a-b) < 1e-6 }

func write(t *testing.T, path, text string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(text), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestWholeCoreCPU(t *testing.T) {
	path := filepath.Join(t.TempDir(), "stat")
	// columns: user nice system idle iowait irq softirq steal guest guest_nice
	write(t, path, "cpu  0 0 0 0 0 0 0 0 0 0\ncpu8 100 0 0 900 0 0 0 0 0 0\ncpu10 0 0 0 1000 0 0 0 0 0 0\n")
	s := &WholeCoreCPU{ProcStat: path, CPUs: [][]int{{8, 10}}}
	if _, err := s.Read(); err != nil {
		t.Fatal(err)
	}
	// Next 1000 ticks per CPU: cpu8 busy 600 (user 500 + softirq 100), cpu10 busy 200 (system).
	write(t, path, "cpu  0 0 0 0 0 0 0 0 0 0\ncpu8 600 0 0 1300 0 0 100 0 0 0\ncpu10 0 0 200 1800 0 0 0 0 0 0\n")
	u, err := s.Read()
	if err != nil || !near(u[0], 0.4) {
		t.Errorf("u = %v, %v; want (600+200)/2000 = 0.4", u, err)
	}
}

func TestRIF(t *testing.T) {
	values := []string{"rif_seconds_total 10\n", "rif_seconds_total 10\n"}
	s := &RIF{Cores: []float64{2}, Fetch: func(int) (string, error) { return values[0], nil }}
	s.Read()
	s.when = s.when.Add(-2e9) // pretend the first read was 2 s ago
	values[0] = "rif_inflight 3\nrif_seconds_total 18\n"
	u, err := s.Read()
	// 8 request-seconds in 2 s = 4 requests in flight on average, over 2 cores = 2.
	if err != nil || math.Abs(u[0]-2) > 0.01 {
		t.Errorf("u = %v, %v; want ~2", u, err)
	}
}

// FromMetrics must give the same numbers as the local signals: here CPU from
// tick counters and RIF per core from the integral.
func TestFromMetrics(t *testing.T) {
	text := []string{"cpu_busy_ticks 100\ncpu_total_ticks 1000\nrif_seconds_total 10\n",
		"cpu_busy_ticks 500\ncpu_total_ticks 2000\nrif_seconds_total 18\n"}
	call := 0
	fetch := func(int) (string, error) { return text[min(call, 1)], nil }
	cpu := &FromMetrics{Fetch: fetch, Num: "cpu_busy_ticks", Den: "cpu_total_ticks", Per: []float64{1}, Scale: 1}
	cpu.Read()
	call++
	if u, _ := cpu.Read(); !near(u[0], 0.4) {
		t.Errorf("cpu = %v, want (500-100)/(2000-1000) = 0.4", u)
	}
	call = 0
	rif := &FromMetrics{Fetch: fetch, Num: "rif_seconds_total", Per: []float64{2}, Scale: 1}
	rif.Read()
	rif.prevWhen = rif.prevWhen.Add(-2e9) // 2 s ago
	call++
	if u, _ := rif.Read(); math.Abs(u[0]-2) > 0.01 {
		t.Errorf("rif = %v, want 8 / 2 s / 2 cores = 2", u)
	}
}

func TestCgroupCounters(t *testing.T) {
	if v, err := counter("usage_usec 123456\nuser_usec 1\n", "usage_usec"); err != nil || v != 123456 {
		t.Errorf("cpu.stat: %v %v", v, err)
	}
	pressure := "some avg10=1.50 avg60=0.20 avg300=0.00 total=987654\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=5\n"
	if v, err := counter(pressure, "total"); err != nil || v != 987654 {
		t.Errorf("cpu.pressure: %v %v (want the 'some' line)", v, err)
	}
}

func TestExpandCPUs(t *testing.T) {
	got := testbed.ExpandCPUs("8,10,16-19")
	want := []int{8, 10, 16, 17, 18, 19}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}
