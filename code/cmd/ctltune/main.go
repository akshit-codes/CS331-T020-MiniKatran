// Command ctltune applies the tuning rule of D36 (docs/campaign.md): for each
// signal, it picks the control period P and the dead band from the signal's
// measured noise, using the simulator with the same scenarios for all.
//
// For each signal, each P with a measured noise σ(P), and each band
// multiplier k ∈ {1.5, 2, 3}:
//  1. dead band: δ_on = k·σ(P), δ_off = δ_on/2;
//  2. calibrate the simulator: measure the noise its readings already have at
//     this P (load changes as connections come and go) and add the rest, so
//     simulated readings are exactly as noisy as the measured ones. The model
//     uses 20× more, 20× lighter connections than the testbed (same load, same
//     lifetimes): with the real count its own load noise exceeded the measured
//     noise (the testbed's generator sends each request to a free connection,
//     which smooths load), and noise can only be added, not removed;
//  3. simulate, 3 seeds each: "static" (start from shares by cores at the
//     campaign load; churn = moves per minute over the last 120 s) and
//     "step" (start from the oracle table; at 150 s one backend loses half
//     its capacity; recovery = seconds until no backend's 10-s average
//     utilisation is above 1, i.e. no queue keeps growing, for 30 s in a row;
//     not "for good", which would measure the last random load spike).
//
// Then, among the (P, k) whose churn is at most max(1% of the table per
// minute, 2 × the least churn this signal achieves), it picks the fastest
// recovery (ties: the larger P, then the larger k). The relative part exists
// because a plain cap with a "least churn" fallback was a cliff: on the
// campaign's data it gave CPU P=8 s (61 s recovery) over P=1 s (14 s) for
// 10 fewer moves per minute (D36, revised before any live comparison). Everything else is
// the controller's default configuration, the same for every signal.
//
// Usage: go run ./cmd/ctltune -in noise.json -out tuning.json
// (bench/tune_signals.py writes noise.json from the campaign's noise runs).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"slices"
	"sort"
	"strconv"

	"cs331lb/internal/controller"
	"cs331lb/internal/lb"
	"cs331lb/internal/sim"
)

type input struct {
	Backends              []sim.Backend                 `json:"backends"`
	Load                  float64                       `json:"load"`                    // fraction of total capacity
	Connections           int                           `json:"connections"`             // open at once (loadgen -conns)
	RequestsPerConnection float64                       `json:"requests_per_connection"` // loadgen -reqs-per-conn
	StepBackend           string                        `json:"step_backend"`            // loses half its capacity in "step"
	Noise                 map[string]map[string]float64 `json:"noise"`                   // signal -> period (s) -> σ
}

type candidate struct {
	Period        float64  `json:"period_s"`
	K             float64  `json:"k"`               // band multiplier
	Sigma         float64  `json:"sigma"`           // measured
	Intrinsic     float64  `json:"sim_intrinsic"`   // the simulator's own reading noise at this P
	AddedNoise    float64  `json:"sim_added_noise"` // what we add to match σ
	DeltaOn       float64  `json:"delta_on"`
	DeltaOff      float64  `json:"delta_off"`
	ChurnPerMin   float64  `json:"churn_moves_per_min"` // median of seeds
	RecoveryS     *float64 `json:"recovery_s"`          // median of seeds; null = never
	MeetsChurnCap bool     `json:"meets_churn_cap"`     // ≤ 1% of the table per minute
	Admitted      bool     `json:"admitted"`            // ≤ max(cap, 2 × this signal's least churn)
}

type choice struct {
	Signal     string      `json:"signal"`
	Chosen     *candidate  `json:"chosen"`
	Note       string      `json:"note,omitempty"`
	Candidates []candidate `json:"candidates"`
}

const (
	seeds    = 3
	churnCap = lb.NumBuckets / 100 // moves per minute: 1% of the table
)

func main() {
	in := flag.String("in", "", "noise and testbed description (JSON)")
	out := flag.String("out", "", "result (JSON)")
	flag.Parse()
	data, err := os.ReadFile(*in)
	check(err)
	var cfg input
	check(json.Unmarshal(data, &cfg))

	var results []choice
	signalNames := make([]string, 0, len(cfg.Noise))
	for s := range cfg.Noise {
		signalNames = append(signalNames, s)
	}
	sort.Strings(signalNames)
	for _, signal := range signalNames {
		c := choice{Signal: signal}
		for _, pText := range sortedKeys(cfg.Noise[signal]) {
			p, _ := strconv.ParseFloat(pText, 64)
			for _, k := range []float64{1.5, 2, 3} {
				c.Candidates = append(c.Candidates, evaluate(cfg, signal, p, k, cfg.Noise[signal][pText]))
			}
		}
		c.Chosen, c.Note = pick(c.Candidates)
		results = append(results, c)
		printChoice(c)
	}
	data, err = json.MarshalIndent(results, "", "  ")
	check(err)
	check(os.WriteFile(*out, data, 0o644))
}

// scenario returns the simulator setup shared by every run.
func scenario(cfg input, signal string, period float64) sim.Scenario {
	total := 0.0
	cores := make([]float64, len(cfg.Backends))
	for i, b := range cfg.Backends {
		total += b.Capacity
		cores[i] = float64(b.Cores)
	}
	const connectionScale = 20                                             // see step 2 above
	reqRate := cfg.Load * total / float64(cfg.Connections*connectionScale) // requests/s per open connection
	simSignal := signal
	if signal == "cgroup" { // without an antagonist, the backend's own CPU time is its whole-core CPU
		simSignal = "cpu"
	}
	return sim.Scenario{
		Backends: cfg.Backends, Load: cfg.Load,
		MeanLifetime: cfg.RequestsPerConnection / (reqRate * connectionScale), Lifetime: "exp", ReqRate: reqRate, // testbed lifetime
		LifetimeBound: 20, Signal: simSignal, Period: period, Config: controller.DefaultConfig(),
		Initial: cores,
	}
}

func evaluate(cfg input, signal string, period, k, sigma float64) candidate {
	c := candidate{Period: period, K: k, Sigma: sigma, DeltaOn: k * sigma, DeltaOff: k * sigma / 2}
	base := scenario(cfg, signal, period)
	capacities := make([]float64, len(cfg.Backends))
	step := -1
	for i, b := range cfg.Backends {
		capacities[i] = b.Capacity
		if b.Name == cfg.StepBackend {
			step = i
		}
	}

	// The simulator's own noise: oracle table, no controller, no added noise.
	quiet := base
	quiet.Initial, quiet.NoController, quiet.Duration, quiet.Seed = capacities, true, 200, 1
	c.Intrinsic = readingCV(sim.Run(quiet).Readings[int(20/period):])
	c.AddedNoise = math.Sqrt(max(0, sigma*sigma-c.Intrinsic*c.Intrinsic))

	var churns, recoveries []float64
	for seed := uint64(1); seed <= seeds; seed++ {
		s := base
		s.Noise, s.Seed, s.Duration = c.AddedNoise, seed, 300
		s.Config.DeltaOn, s.Config.DeltaOff = c.DeltaOn, c.DeltaOff
		static := sim.Run(s)
		moves := 0
		for _, smp := range static.Samples {
			if smp.T >= s.Duration-120 {
				moves += smp.Moves
			}
		}
		churns = append(churns, float64(moves)/2)

		s.Initial, s.Duration = capacities, 400
		s.Events = []sim.Event{{At: 150, Backend: step, CapacityFactor: 0.5}}
		recoveries = append(recoveries, recovery(sim.Run(s), 150))
	}
	c.ChurnPerMin = median(churns)
	if r := median(recoveries); !math.IsInf(r, 1) {
		c.RecoveryS = &r
	}
	c.MeetsChurnCap = c.ChurnPerMin <= churnCap
	return c
}

// readingCV: the median over backends of each backend's coefficient of variation.
func readingCV(readings [][]float64) float64 {
	var cvs []float64
	for b := range readings[0] {
		var sum, sq float64
		for _, r := range readings {
			sum += r[b]
		}
		mean := sum / float64(len(readings))
		for _, r := range readings {
			sq += (r[b] - mean) * (r[b] - mean)
		}
		cvs = append(cvs, math.Sqrt(sq/float64(len(readings)-1))/mean)
	}
	return median(cvs)
}

// recovery: seconds after `at` until no backend's 10-s average utilisation is
// above 1 (a queue that keeps growing) for 30 s in a row; +Inf if never.
func recovery(res sim.Result, at float64) float64 {
	ok := make([]bool, len(res.Samples))
	for i := range res.Samples {
		lo := max(0, i-9)
		ok[i] = true
		for b := range res.Samples[i].Rho {
			s := 0.0
			for _, x := range res.Samples[lo : i+1] {
				s += x.Rho[b]
			}
			if s/float64(i+1-lo) > 1 {
				ok[i] = false
			}
		}
	}
	run := 0 // consecutive good seconds
	for i, s := range res.Samples {
		if s.T < at || !ok[i] {
			run = 0
			continue
		}
		if run++; run == 30 {
			return s.T - 29 - at
		}
	}
	return math.Inf(1)
}

// pick applies the rule: admit the candidates whose churn is at most
// max(churnCap, 2 × the least churn of any candidate); among those, the
// fastest recovery; ties (same second) go to the larger P, then the larger k.
func pick(cs []candidate) (*candidate, string) {
	least := math.Inf(1)
	for _, c := range cs {
		least = min(least, c.ChurnPerMin)
	}
	limit := max(float64(churnCap), 2*least)
	var best *candidate
	for i := range cs {
		c := &cs[i]
		c.Admitted = c.ChurnPerMin <= limit
		if !c.Admitted {
			continue
		}
		if best == nil || rec(c) < rec(best) || (rec(c) == rec(best) &&
			(c.Period > best.Period || (c.Period == best.Period && c.K > best.K))) {
			best = c
		}
	}
	note := ""
	if limit > float64(churnCap) {
		note = fmt.Sprintf("no setting meets the cap of %d moves/min; admitted up to 2 × the least churn (%.0f)", churnCap, limit)
	}
	return best, note
}

func rec(c *candidate) float64 {
	if c.RecoveryS == nil {
		return math.Inf(1)
	}
	return math.Round(*c.RecoveryS)
}

func printChoice(c choice) {
	fmt.Printf("%s:\n  %4s %4s %7s %9s %8s %8s %10s %10s\n", c.Signal, "P", "k", "σ", "sim own", "δ_on", "δ_off", "churn/min", "recovery")
	for _, k := range c.Candidates {
		r := "never"
		if k.RecoveryS != nil {
			r = fmt.Sprintf("%.0f s", *k.RecoveryS)
		}
		mark := ""
		if c.Chosen != nil && k.Period == c.Chosen.Period && k.K == c.Chosen.K {
			mark = "  <- chosen"
		}
		fmt.Printf("  %3.0fs %4.1f %7.3f %9.3f %8.3f %8.3f %10.0f %10s%s\n", k.Period, k.K, k.Sigma, k.Intrinsic, k.DeltaOn, k.DeltaOff, k.ChurnPerMin, r, mark)
	}
	if c.Note != "" {
		fmt.Println("  note:", c.Note)
	}
	for _, k := range c.Candidates {
		if k.Intrinsic > k.Sigma {
			fmt.Printf("  note: at P=%gs the simulator alone is noisier (%.3f) than measured (%.3f): tuning is pessimistic for %s\n",
				k.Period, k.Intrinsic, k.Sigma, c.Signal)
			break
		}
	}
}

func sortedKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.SortFunc(keys, func(a, b string) int {
		fa, _ := strconv.ParseFloat(a, 64)
		fb, _ := strconv.ParseFloat(b, 64)
		return int(fa - fb)
	})
	return keys
}

func median(xs []float64) float64 {
	s := slices.Clone(xs)
	slices.Sort(s)
	return s[len(s)/2]
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "ctltune:", err)
		os.Exit(1)
	}
}
