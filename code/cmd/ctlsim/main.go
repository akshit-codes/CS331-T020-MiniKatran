// Command ctlsim runs the controller (internal/controller) against the model
// in internal/sim for a set of scenarios and signals, and writes a summary.
//
// Per run and phase (between disturbances) it reports:
//   - reached: seconds from the start of the phase until the imbalance (max ρ_i
//     / ideal ρ, 10-s average) first gets within the noise floor + 1%. The
//     noise floor is the p99 of what a perfect static table shows with the
//     same random connections (oracle run, no controller);
//   - over the last 60 s of the phase: imbalance, the highest backend
//     utilisation, the p99 response time over all requests (internal/sim's
//     queueing model; the fair yardstick across signals), and moves per minute;
//   - reversals: how often a backend's bucket count changed direction
//     (10-s blocks), i.e. oscillation;
//   - safety: first-hop changes of a bucket within Dwell of its previous
//     change ("re-changes"), and connections broken because their backend was
//     dropped from their bucket. Both from the simulator's own bookkeeping.
//
// Usage: go run ./cmd/ctlsim [-noise 0.02] [-out results/sim-<time>]
package main

import (
	"flag"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"time"

	"cs331lb/internal/controller"
	"cs331lb/internal/sim"
)

// More unequal than our testbed, to make the controller's job visible:
// beP1 is a P backend slowed down (like -slowness 1.6).
var backends = []sim.Backend{
	{Name: "beP1", Cores: 2, Capacity: 1000},
	{Name: "beP2", Cores: 2, Capacity: 1600},
	{Name: "beE1", Cores: 4, Capacity: 1760},
	{Name: "beE2", Cores: 4, Capacity: 1760},
}

const seeds = 5

type scenario struct {
	name, about string
	base        sim.Scenario
	phases      []float64 // phase start times (first is 0)
}

func capacities() []float64 {
	c := make([]float64, len(backends))
	for i, b := range backends {
		c[i] = b.Capacity
	}
	return c
}

func scenarios(noise float64) []scenario {
	base := sim.Scenario{
		Backends: backends, MeanLifetime: 2, Lifetime: "exp", ReqRate: 1, LifetimeBound: 20,
		Period: 2, Noise: noise, Config: controller.DefaultConfig(), Seed: 1,
	}
	with := func(f func(*sim.Scenario)) sim.Scenario { s := base; f(&s); return s }
	return []scenario{
		{"static", "60% load, start from shares by cores", with(func(s *sim.Scenario) {
			s.Load, s.Duration = 0.6, 600
		}), []float64{0}},
		{"antagonist", "60% load; at 300 s beE1 loses half its capacity (overloaded), at 600 s gets it back", with(func(s *sim.Scenario) {
			s.Load, s.Duration = 0.6, 900
			s.Events = []sim.Event{{At: 300, Backend: 2, CapacityFactor: 0.5}, {At: 600, Backend: 2, CapacityFactor: 2}}
		}), []float64{0, 300, 600}},
		{"overload-start", "80% load, start from equal shares: beP1 at ρ=1.22", with(func(s *sim.Scenario) {
			s.Load, s.Duration, s.Initial = 0.8, 600, []float64{1, 1, 1, 1}
		}), []float64{0}},
		{"near-capacity", "90% load, start from the oracle table; at 200 s beE1 loses 12% (ρ≈1.02)", with(func(s *sim.Scenario) {
			s.Load, s.Duration, s.Initial = 0.9, 600, capacities()
			s.Events = []sim.Event{{At: 200, Backend: 2, CapacityFactor: 0.88}}
		}), []float64{0, 200}},
		{"saturated", "95% load, start from the oracle table; at 200 s beE1 loses 10% (ρ≈1.06: over capacity for good unless the controller acts)", with(func(s *sim.Scenario) {
			s.Load, s.Duration, s.Initial = 0.95, 600, capacities()
			s.Events = []sim.Event{{At: 200, Backend: 2, CapacityFactor: 0.9}}
		}), []float64{0, 200}},
		{"heavy-tails", "like antagonist, but connection lifetimes are Pareto (same 2 s mean; 0.6% live longer than the 20 s dwell)", with(func(s *sim.Scenario) {
			s.Load, s.Duration, s.Lifetime = 0.6, 900, "pareto"
			s.Events = []sim.Event{{At: 300, Backend: 2, CapacityFactor: 0.5}, {At: 600, Backend: 2, CapacityFactor: 2}}
		}), []float64{0, 300, 600}},
		{"no-protection", "control for the safety check: heavy tails, beE1 flapping (half capacity on/off every 10 s), and a controller with no dwell, no caps, gain 1, band 2%/1%", with(func(s *sim.Scenario) {
			s.Load, s.Duration, s.Lifetime = 0.6, 600, "pareto"
			s.Config.Dwell, s.Config.GlobalCap, s.Config.PerBackendCap = 0, 4096, 4096
			s.Config.MaxStep, s.Config.Gain, s.Config.DeltaOn, s.Config.DeltaOff = 1, 1, 0.02, 0.01
			for at := 100.0; at < 500; at += 20 {
				s.Events = append(s.Events, sim.Event{At: at, Backend: 2, CapacityFactor: 0.5}, sim.Event{At: at + 10, Backend: 2, CapacityFactor: 2})
			}
		}), []float64{0}},
	}
}

type phaseStats struct {
	reached        float64 // seconds after phase start; NaN = never
	finalImbalance float64
	finalMaxRho    float64
	finalP99       float64 // ms, median of the per-second values
	movesPerMin    float64
	reversals      int
}

// movingImbalance: 10-s trailing average of the per-second imbalance.
func movingImbalance(samples []sim.Sample) []float64 {
	ma := make([]float64, len(samples))
	for i := range samples {
		lo := max(0, i-9)
		s := 0.0
		for _, x := range samples[lo : i+1] {
			s += x.Imbalance
		}
		ma[i] = s / float64(i+1-lo)
	}
	return ma
}

func analyse(res sim.Result, from, to, threshold float64) phaseStats {
	ma := movingImbalance(res.Samples)
	var idx []int
	for i, s := range res.Samples {
		if s.T >= from && s.T < to {
			idx = append(idx, i)
		}
	}
	st := phaseStats{reached: math.NaN()}
	for _, i := range idx {
		if res.Samples[i].T >= from+10 && ma[i] <= threshold { // skip the first 10 s: the average still holds the old phase
			st.reached = res.Samples[i].T - from
			break
		}
	}
	// Oscillation: direction changes of each backend's bucket count, in 10-s blocks.
	for b := range res.Samples[0].Buckets {
		lastSign := 0
		for k := 10; k < len(idx); k += 10 {
			d := res.Samples[idx[k]].Buckets[b] - res.Samples[idx[k-10]].Buckets[b]
			sign := 0
			if d > 0 {
				sign = 1
			} else if d < 0 {
				sign = -1
			}
			if sign != 0 && lastSign != 0 && sign != lastSign {
				st.reversals++
			}
			if sign != 0 {
				lastSign = sign
			}
		}
	}
	// Last 60 s of the phase.
	var p99s []float64
	rhoSum := make([]float64, len(backends))
	moves := 0
	for _, i := range idx {
		s := res.Samples[i]
		if s.T < to-60 {
			continue
		}
		st.finalImbalance += s.Imbalance
		for b, r := range s.Rho {
			rhoSum[b] += r
		}
		p99s = append(p99s, s.P99)
		moves += s.Moves
	}
	n := float64(len(p99s))
	st.finalImbalance /= n
	for _, r := range rhoSum {
		st.finalMaxRho = max(st.finalMaxRho, r/n)
	}
	slices.Sort(p99s)
	st.finalP99 = p99s[len(p99s)/2]
	st.movesPerMin = float64(moves) / (n / 60)
	return st
}

// noiseFloor: the 99th percentile of the 10-s imbalance with a perfect static
// table (shares ∝ capacity) and no controller: what randomness alone gives.
func noiseFloor(base sim.Scenario) float64 {
	s := base
	s.Events, s.Initial, s.NoController, s.Duration = nil, capacities(), true, 300
	ma := movingImbalance(sim.Run(s).Samples)[10:]
	slices.Sort(ma)
	return ma[len(ma)*99/100]
}

func writeCSV(path string, res sim.Result) error {
	var b strings.Builder
	b.WriteString("t,imbalance,p99_ms,moves")
	for _, be := range backends {
		fmt.Fprintf(&b, ",rho_%s,u_%s,buckets_%s", be.Name, be.Name, be.Name)
	}
	b.WriteString("\n")
	for _, s := range res.Samples {
		fmt.Fprintf(&b, "%.0f,%.4f,%.2f,%d", s.T, s.Imbalance, s.P99, s.Moves)
		for i := range backends {
			fmt.Fprintf(&b, ",%.4f,%.4f,%d", s.Rho[i], s.U[i], s.Buckets[i])
		}
		b.WriteString("\n")
	}
	return os.WriteFile(path, []byte(b.String()), 0o644)
}

func main() {
	out := flag.String("out", filepath.Join("results", "sim-"+time.Now().Format("20060102-150405")), "output directory")
	noise := flag.Float64("noise", 0.02, "signal noise per reading (coefficient of variation)")
	flag.Parse()
	if err := os.MkdirAll(*out, 0o755); err != nil {
		panic(err)
	}
	var md strings.Builder
	cfg := controller.DefaultConfig()
	fmt.Fprintf(&md, "# Controller simulation\n\nBackends: ")
	for _, b := range backends {
		fmt.Fprintf(&md, "%s (%d cores, %.0f req/s) ", b.Name, b.Cores, b.Capacity)
	}
	fmt.Fprintf(&md, "\n\nController: %+v, period 2 s, signal noise %.0f%%.\n"+
		"Connections: mean lifetime 2 s, 1 request/s each. Every case runs with %d seeds; the table shows medians.\n"+
		"Imbalance, max ρ, p99 and moves/min are over the last 60 s of each phase;\n"+
		"re-changes and broken connections are totals over the whole run and all seeds.\n\n", cfg, *noise*100, seeds)

	for _, sc := range scenarios(*noise) {
		floor := noiseFloor(sc.base)
		threshold := floor + 0.01
		fmt.Fprintf(&md, "## %s\n\n%s. Noise floor (oracle table, p99 of 10-s imbalance) %.3f, so reached = imbalance ≤ %.3f.\n\n",
			sc.name, sc.about, floor, threshold)
		fmt.Fprintf(&md, "| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |\n")
		fmt.Fprintf(&md, "|---|---|---|---|---|---|---|---|---|---|\n")
		signals := []string{"true", "cpu", "rif"}
		if sc.name == "near-capacity" || sc.name == "saturated" {
			signals = append(signals, "true+override", "cpu+override")
		}
		for _, signal := range signals {
			// Each case runs with several seeds (different random connections
			// and noise); the table shows medians, and safety totals.
			perPhase := make([][]phaseStats, len(sc.phases))
			rechanges, broken, conns := 0, 0, 0
			for seed := uint64(1); seed <= seeds; seed++ {
				s := sc.base
				s.Signal, s.Seed = signal, seed
				if name, ok := strings.CutSuffix(signal, "+override"); ok {
					s.Signal, s.Config.SaturatedAt = name, 0.97
				}
				res := sim.Run(s)
				if seed == 1 {
					if err := writeCSV(filepath.Join(*out, fmt.Sprintf("%s-%s.csv", sc.name, signal)), res); err != nil {
						panic(err)
					}
				}
				rechanges += res.Rechanges
				broken += res.Broken
				conns += res.Conns
				for p, from := range sc.phases {
					to := s.Duration
					if p+1 < len(sc.phases) {
						to = sc.phases[p+1]
					}
					perPhase[p] = append(perPhase[p], analyse(res, from, to, threshold))
				}
			}
			for p, from := range sc.phases {
				to := sc.base.Duration
				if p+1 < len(sc.phases) {
					to = sc.phases[p+1]
				}
				st := perPhase[p]
				med := func(f func(phaseStats) float64) float64 {
					xs := make([]float64, len(st))
					for i := range st {
						xs[i] = f(st[i])
						if math.IsNaN(xs[i]) {
							xs[i] = math.Inf(1) // "never" sorts last
						}
					}
					slices.Sort(xs)
					return xs[len(xs)/2]
				}
				reached := med(func(s phaseStats) float64 { return s.reached })
				reachedText := "never"
				if !math.IsInf(reached, 1) {
					reachedText = fmt.Sprintf("%.0f", reached)
				}
				safety := " | "
				if p == 0 { // whole run, all seeds
					safety = fmt.Sprintf("%d | %d of %.1fM", rechanges, broken, float64(conns)/1e6)
				}
				fmt.Fprintf(&md, "| %s | %.0f–%.0f s | %s | %.3f | %.2f | %.1f | %.0f | %.0f | %s |\n",
					signal, from, to, reachedText,
					med(func(s phaseStats) float64 { return s.finalImbalance }),
					med(func(s phaseStats) float64 { return s.finalMaxRho }),
					med(func(s phaseStats) float64 { return s.finalP99 }),
					med(func(s phaseStats) float64 { return s.movesPerMin }),
					med(func(s phaseStats) float64 { return float64(s.reversals) }),
					safety)
			}
			fmt.Printf("%-15s %-5s re-changes %d  broken %d of %d\n", sc.name, signal, rechanges, broken, conns)
		}
		md.WriteString("\n")
	}
	if err := os.WriteFile(filepath.Join(*out, "summary.md"), []byte(md.String()), 0o644); err != nil {
		panic(err)
	}
	fmt.Println("wrote", *out)
}
