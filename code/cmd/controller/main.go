// Command controller runs the bucket controller live against the testbed:
// it publishes an initial table (rendezvous second hops, shares by cores) in a
// way that keeps connections on the table it replaces (controller.NewFrom),
// then every period reads a load signal, runs one controller step
// (internal/controller) and publishes the new table through the same
// map-in-map swap as lbctl. One CSV line per period goes to -out. The first
// reading only moves the counters on: traffic may have just started, so it
// covers the ramp-up, not the load.
//
// Usage (from the repo root, LB loaded with lbctl):
//
//	sudo bin/controller -signal rif|rif-total|cpu|cgroup|psi [-period 2s] [-duration 60s] [-out log.csv]
//	                    [-weights beP1=957,beP2=1656,...]  (initial shares; default: by cores)
package main

import (
	"bufio"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/cilium/ebpf"

	"cs331lb/internal/controller"
	"cs331lb/internal/lb"
	"cs331lb/internal/signals"
	"cs331lb/internal/testbed"
)

func main() {
	cfg := controller.DefaultConfig()
	signalName := flag.String("signal", "rif", "load signal: cpu, rif (per core), rif-total, cgroup, psi")
	period := flag.Duration("period", 2*time.Second, "control period")
	duration := flag.Duration("duration", 0, "stop after this long (0 = until Ctrl-C)")
	out := flag.String("out", "", "CSV log (default stdout)")
	flag.Float64Var(&cfg.Gain, "gain", cfg.Gain, "controller gain")
	flag.Float64Var(&cfg.Dwell, "dwell", cfg.Dwell, "seconds a changed bucket stays frozen (≥ longest connection)")
	flag.Float64Var(&cfg.SaturatedAt, "saturated-at", cfg.SaturatedAt, "overload override threshold (0 = off)")
	flag.Float64Var(&cfg.DeltaOn, "delta-on", cfg.DeltaOn, "start correcting above this relative deviation (D36: 3σ of the signal)")
	flag.Float64Var(&cfg.DeltaOff, "delta-off", cfg.DeltaOff, "stop correcting below this relative deviation (D36: 1.5σ)")
	flag.Float64Var(&cfg.MaxStep, "max-step", cfg.MaxStep, "largest change of a backend's buckets per period (fraction)")
	flag.IntVar(&cfg.GlobalCap, "global-cap", cfg.GlobalCap, "most bucket changes per period")
	flag.IntVar(&cfg.PerBackendCap, "per-backend-cap", cfg.PerBackendCap, "most buckets a backend gains or loses per period")
	initial := flag.String("weights", "", "initial shares, NAME=WEIGHT,... (default: by cores)")
	flag.Parse()

	tb, err := testbed.Load(".")
	check(err)
	sig, err := signals.New(*signalName, tb)
	check(err)
	tables, err := ebpf.LoadPinnedMap("/sys/fs/bpf/cs331/tables", nil)
	check(err)
	defer tables.Close()

	ids := make([]uint32, len(tb.Backends))
	cores := make([]float64, len(tb.Backends))
	for i, be := range tb.Backends {
		ids[i] = uint32(i + 1)
		cores[i] = float64(len(testbed.ExpandCPUs(be.CPUs)))
	}
	weights := cores
	if *initial != "" {
		weights, err = parseWeights(*initial, tb)
		check(err)
	}
	table, err := controller.InitialTable(ids, weights)
	check(err)
	var ctl *controller.Controller
	if active, _, err := lb.Active(tables); err == nil {
		ctl = controller.NewFrom(cfg, ids, cores, active, table)
	} else { // no table yet, so no connections to keep
		ctl = controller.New(cfg, ids, cores, table)
	}
	gen, err := lb.Publish(tables, ctl.Buckets)
	check(err)

	w := bufio.NewWriter(os.Stdout)
	if *out != "" {
		check(os.MkdirAll(filepath.Dir(*out), 0o755))
		f, err := os.Create(*out)
		check(err)
		defer f.Close()
		w = bufio.NewWriter(f)
	}
	defer w.Flush()
	header := []string{"t", "generation", "mean", "max_dev", "moves", "swaps", "reason"}
	for _, be := range tb.Backends {
		header = append(header, "u_"+be.Name, "buckets_"+be.Name)
	}
	fmt.Fprintln(w, strings.Join(header, ","))
	log.Printf("controller: signal %s, period %s, initial table generation %d, config %+v", *signalName, *period, gen, cfg)

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	start := time.Now()
	ticker := time.NewTicker(*period)
	defer ticker.Stop()
	first := true
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
		}
		now := time.Since(start).Seconds()
		u, err := sig.Read()
		check(err)
		var res controller.StepResult
		if first {
			first = false
			res = controller.StepResult{Reason: "first reading discarded", After: ctl.Counts()}
		} else {
			res = ctl.Step(now, u)
		}
		swaps := 0
		for _, m := range res.Moves {
			if m.Swap {
				swaps++
			}
		}
		if len(res.Moves) > 0 {
			gen, err = lb.Publish(tables, ctl.Buckets)
			check(err)
		}
		fmt.Fprintf(w, "%.1f,%d,%.4f,%.4f,%d,%d,%s", now, gen, res.Mean, res.MaxDeviation, len(res.Moves), swaps, res.Reason)
		for i := range u {
			fmt.Fprintf(w, ",%.4f,%d", u[i], res.After[i])
		}
		fmt.Fprintln(w)
		w.Flush()
		if *duration > 0 && time.Since(start) >= *duration {
			return
		}
	}
}

// parseWeights turns "beP1=957,beE1=1810" into weights in backends.conf order (unlisted = 0).
func parseWeights(spec string, tb *testbed.Config) ([]float64, error) {
	weights := make([]float64, len(tb.Backends))
	for _, part := range strings.Split(spec, ",") {
		name, value, ok := strings.Cut(part, "=")
		w, err := strconv.ParseFloat(value, 64)
		i := slices.IndexFunc(tb.Backends, func(b testbed.Backend) bool { return b.Name == name })
		if !ok || err != nil || i < 0 {
			return nil, fmt.Errorf("bad weight %q", part)
		}
		weights[i] = w
	}
	return weights, nil
}

func check(err error) {
	if err != nil {
		log.Fatal("controller: ", err)
	}
}
