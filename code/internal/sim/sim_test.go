package sim

import (
	"math"
	"testing"
)

// Known values: M/M/1 at ρ = 0.5 with mu = 1000/s has an exponential response
// time with rate mu - lambda = 500/s, so P(T > t) = exp(-500 t) and the p99 is
// ln(100)/500 s = 9.21 ms.
func TestMM1(t *testing.T) {
	if q := mmcQueue(0.5, 1); math.Abs(q-0.5) > 1e-9 {
		t.Errorf("M/M/1 Lq at ρ=0.5 = %g, want ρ²/(1-ρ) = 0.5", q)
	}
	if p := mmcTail(0.004, 500, 1000, 1); math.Abs(p-math.Exp(-2)) > 1e-9 {
		t.Errorf("P(T > 4 ms) = %g, want e^-2", p)
	}
	p99 := clusterP99([]float64{500}, []float64{1000}, []float64{1}, []float64{0})
	if math.Abs(p99-9.2103) > 0.01 {
		t.Errorf("p99 = %.4f ms, want 9.2103", p99)
	}
}

// More cores at the same utilisation means less waiting (Erlang C).
func TestMoreCoresLessQueue(t *testing.T) {
	if mmcQueue(0.7, 4) >= mmcQueue(0.7, 2) {
		t.Error("4 cores should queue less than 2 at the same utilisation")
	}
}
