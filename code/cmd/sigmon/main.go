// Command sigmon logs, once per second, every load signal for every backend
// (whole-core CPU, RIF, cgroup CPU, PSI: internal/signals) plus each backend's
// request and work rates, as one CSV line. It only observes; the controller
// is separate. Uses (docs/campaign.md):
//   - signal noise for the tuning rule (D36): a run with a fixed table gives
//     every signal's noise at every period, by averaging the 1-s readings
//     into longer windows afterwards;
//   - which signals see an antagonist (RQ3 extras), logged passively during
//     every scenario run;
//   - the timeline of where the load went (requests per second per backend).
//
// Usage (from the repo root, backends started by bench/testbed.py):
//
//	sudo bin/sigmon [-duration 0] [-out sigmon.csv]
package main

import (
	"bufio"
	"flag"
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"cs331lb/internal/signals"
	"cs331lb/internal/testbed"
)

var signalNames = []string{"cpu", "rif", "cgroup", "psi"}

func main() {
	duration := flag.Duration("duration", 0, "stop after this long (0 = until Ctrl-C)")
	out := flag.String("out", "", "CSV file (default stdout)")
	flag.Parse()

	cfg, err := testbed.Load(".")
	check(err)
	port, err := cfg.Get("METRICS_PORT")
	check(err)
	sigs := make([]signals.Signal, len(signalNames))
	for i, name := range signalNames {
		sigs[i], err = signals.New(name, cfg)
		check(err)
	}
	counters := func() (reqs, units []float64) { // cumulative, per backend
		for i := range cfg.Backends {
			text, err := signals.Metrics(cfg, i, port)
			check(err)
			reqs = append(reqs, metric(text, "requests_total"))
			units = append(units, metric(text, "work_units_total"))
		}
		return
	}

	w := bufio.NewWriter(os.Stdout)
	if *out != "" {
		f, err := os.Create(*out)
		check(err)
		defer f.Close()
		w = bufio.NewWriter(f)
	}
	defer w.Flush()
	header := []string{"t"}
	for _, be := range cfg.Backends {
		for _, s := range signalNames {
			header = append(header, s+"_"+be.Name)
		}
		header = append(header, "req_per_s_"+be.Name, "units_per_s_"+be.Name)
	}
	fmt.Fprintln(w, strings.Join(header, ","))

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	start := time.Now()
	prevReqs, prevUnits := counters()
	prevTime := time.Now()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-stop:
			return
		case <-ticker.C:
		}
		values := make([][]float64, len(sigs))
		for i, s := range sigs {
			values[i], err = s.Read()
			check(err)
		}
		reqs, units := counters()
		dt := time.Since(prevTime).Seconds()
		prevTime = time.Now()

		line := []string{fmt.Sprintf("%.1f", time.Since(start).Seconds())}
		for b := range cfg.Backends {
			for i := range sigs {
				line = append(line, fmt.Sprintf("%.4f", values[i][b]))
			}
			line = append(line, fmt.Sprintf("%.1f", (reqs[b]-prevReqs[b])/dt), fmt.Sprintf("%.0f", (units[b]-prevUnits[b])/dt))
		}
		prevReqs, prevUnits = reqs, units
		fmt.Fprintln(w, strings.Join(line, ","))
		w.Flush()
		if *duration > 0 && time.Since(start) >= *duration {
			return
		}
	}
}

func metric(text, name string) float64 {
	for _, line := range strings.Split(text, "\n") {
		if f := strings.Fields(line); len(f) == 2 && f[0] == name {
			v, _ := strconv.ParseFloat(f[1], 64)
			return v
		}
	}
	return 0
}

func check(err error) {
	if err != nil {
		log.Fatal("sigmon: ", err)
	}
}
