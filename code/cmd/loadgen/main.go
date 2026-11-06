// Command loadgen is an open-loop HTTP load generator.
//
// Open loop means requests are *scheduled* at a given average rate no matter
// how fast the server answers. Latency is measured from the scheduled time,
// not from when the request was actually sent. So if the server (or our
// connection pool) falls behind, the waiting time is counted instead of hidden.
// Closed-loop tools that wait for a reply before sending the next request hide
// it; that mistake is called "coordinated omission".
//
// The run has a warm-up phase followed by a measured phase; only requests
// scheduled in the measured phase are counted. One JSON summary goes to stdout.
// With -out, one CSV line per request is also written (for later phases).
//
// With -targets it loads several servers at once, without any balancer: one
// generator (several would compete for its CPUs), each request goes to a server
// chosen at random in proportion to the rates (so each sees Poisson arrivals),
// each server has its own connections, and the summary also has one entry per
// server.
//
// Usage:
//
//	loadgen -target 10.1.0.11:8080 -rate 1500 -warmup 20s -duration 60s
//	loadgen -targets 10.1.0.11:8080=950,10.1.0.12:8080=1650 -warmup 5s -duration 20s
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"os"
	"runtime"
	"slices"
	"strconv"
	"strings"
	"sync/atomic"
	"time"
)

// connections counts connections opened. With persistent connections, every
// one beyond -conns replaced a connection that broke.
var connections atomic.Int64

type job struct {
	scheduled time.Time
}

// server is one target: its address, its share of the requests and its jobs.
type server struct {
	addr   string
	rate   float64
	conns  int
	jobs   chan job
	done   chan []record // one result per connection worker
	opened atomic.Int64  // connections opened
}

type record struct {
	scheduled time.Time
	sent      time.Time // when the request was written to a connection
	done      time.Time
	ok        bool
	expired   bool // never sent: still queued long after the run ended
}

type config struct {
	target      string
	targets     string
	path        string
	rate        float64
	warmup      time.Duration
	duration    time.Duration
	conns       int
	reqsPerConn int
	arrival     string
	seed        uint64
	timeout     time.Duration
	window      time.Duration
	out         string
}

func main() {
	var c config
	flag.StringVar(&c.target, "target", "", "host:port of the server (required)")
	flag.StringVar(&c.targets, "targets", "", "several servers, host:port=rate,... (instead of -target and -rate; -conns are shared in proportion)")
	flag.StringVar(&c.path, "path", "/work", "request path")
	flag.Float64Var(&c.rate, "rate", 1000, "average requests per second")
	flag.DurationVar(&c.warmup, "warmup", 20*time.Second, "warm-up phase (not measured)")
	flag.DurationVar(&c.duration, "duration", 60*time.Second, "measured phase")
	flag.IntVar(&c.conns, "conns", 64, "connections (= max requests in flight)")
	flag.IntVar(&c.reqsPerConn, "reqs-per-conn", 0, "close a connection after this many requests (0 = keep it)")
	flag.StringVar(&c.arrival, "arrival", "poisson", "poisson (random gaps) or fixed (equal gaps)")
	flag.Uint64Var(&c.seed, "seed", 1, "random seed for poisson arrivals")
	flag.DurationVar(&c.timeout, "timeout", 2*time.Second, "per-request timeout")
	flag.DurationVar(&c.window, "window", 10*time.Second, "window size for per-window p99")
	flag.StringVar(&c.out, "out", "", "optional CSV file with one line per request")
	flag.Parse()
	servers, err := parseServers(&c)
	if err != nil {
		log.Fatal(err)
	}
	// The spinning dispatcher occupies one Go processor all the time; give the
	// connection workers one extra so they never wait behind it.
	runtime.GOMAXPROCS(runtime.NumCPU() + 1)

	start := time.Now().Add(200 * time.Millisecond)
	end := start.Add(c.warmup + c.duration)

	for _, sv := range servers {
		for i := 0; i < sv.conns; i++ {
			go worker(c, sv, end)
		}
	}
	dispatchLag, dropped := dispatch(c, servers, start, end)

	perServer := map[string]summary{}
	var recs []record
	for _, sv := range servers {
		var own []record
		for i := 0; i < sv.conns; i++ {
			own = append(own, <-sv.done...)
		}
		if len(servers) > 1 {
			one := c
			one.target, one.rate, one.conns = sv.addr, sv.rate, sv.conns
			s := summarize(one, start.Add(c.warmup), end, own, nil, 0)
			s.Connections = sv.opened.Load()
			perServer[sv.addr] = s
		}
		recs = append(recs, own...)
	}

	if c.out != "" {
		if err := writeCSV(c.out, start, recs); err != nil {
			log.Fatal(err)
		}
	}
	summary := summarize(c, start.Add(c.warmup), end, recs, dispatchLag, dropped)
	summary.PerServer = perServer
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	enc.Encode(summary)
}

// parseServers turns -target/-rate or -targets into the servers to load.
func parseServers(c *config) ([]*server, error) {
	if c.targets == "" {
		if c.target == "" || c.rate <= 0 {
			return nil, fmt.Errorf("need -target and a positive -rate, or -targets")
		}
		return []*server{{addr: c.target, rate: c.rate, conns: c.conns, jobs: make(chan job, 1<<20), done: make(chan []record)}}, nil
	}
	var servers []*server
	c.rate = 0
	for _, part := range strings.Split(c.targets, ",") {
		addr, value, ok := strings.Cut(part, "=")
		rate, err := strconv.ParseFloat(value, 64)
		if !ok || err != nil || rate <= 0 {
			return nil, fmt.Errorf("bad target %q (want host:port=rate)", part)
		}
		servers = append(servers, &server{addr: addr, rate: rate, jobs: make(chan job, 1<<20), done: make(chan []record)})
		c.rate += rate
	}
	for _, sv := range servers {
		sv.conns = max(1, int(float64(c.conns)*sv.rate/c.rate+0.5))
	}
	c.target = c.targets
	return servers, nil
}

// spinWindow: Go's timers can wake up to ~1 ms late, and since latency is
// measured from the scheduled time, that lateness would count as latency.
// So the dispatcher sleeps until spinWindow before each send, then spins.
const spinWindow = 2 * time.Millisecond

// dispatch schedules jobs from start to end and returns how late each one was
// handed to the workers (a sign the generator itself is too slow) and how many
// were dropped because the queue was full.
func dispatch(c config, servers []*server, start, end time.Time) (lag []time.Duration, dropped int) {
	runtime.LockOSThread() // the spinning dispatcher gets its own thread
	rng := rand.New(rand.NewPCG(c.seed, 0))
	mean := float64(time.Second) / c.rate
	next := start
	for next.Before(end) {
		if d := time.Until(next); d > spinWindow {
			time.Sleep(d - spinWindow)
		}
		for time.Now().Before(next) {
			// spin until the scheduled time
		}
		lag = append(lag, time.Since(next))
		sv := servers[0]
		if len(servers) > 1 {
			x := rng.Float64() * c.rate
			for _, sv = range servers {
				if x -= sv.rate; x < 0 {
					break
				}
			}
		}
		select {
		case sv.jobs <- job{scheduled: next}:
		default:
			dropped++
		}
		gap := mean
		if c.arrival == "poisson" {
			gap = rng.ExpFloat64() * mean
		}
		next = next.Add(time.Duration(gap))
	}
	for _, sv := range servers {
		close(sv.jobs)
	}
	return lag, dropped
}

// worker owns one connection at a time and sends the jobs it takes, one by one.
func worker(c config, sv *server, end time.Time) {
	request := []byte(fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\n\r\n", c.path, sv.addr))
	var conn net.Conn
	var reader *bufio.Reader
	used := 0
	var recs []record

	for j := range sv.jobs {
		r := record{scheduled: j.scheduled}
		if time.Now().After(end.Add(c.timeout)) {
			// The run is over and this request never got a connection: count it as failed.
			r.expired, r.done = true, time.Now()
			recs = append(recs, r)
			continue
		}
		if conn == nil {
			var err error
			conn, err = net.DialTimeout("tcp", sv.addr, c.timeout)
			if err != nil {
				r.done = time.Now()
				recs = append(recs, r)
				conn = nil
				continue
			}
			reader = bufio.NewReader(conn)
			used = 0
			connections.Add(1)
			sv.opened.Add(1)
		}
		r.sent = time.Now()
		conn.SetDeadline(r.sent.Add(c.timeout))
		_, err := conn.Write(request)
		if err == nil {
			var resp *http.Response
			resp, err = http.ReadResponse(reader, nil)
			if err == nil {
				_, err = io.Copy(io.Discard, resp.Body)
				resp.Body.Close()
				r.ok = err == nil && resp.StatusCode == http.StatusOK
			}
		}
		r.done = time.Now()
		recs = append(recs, r)

		used++
		if err != nil || (c.reqsPerConn > 0 && used >= c.reqsPerConn) {
			conn.Close()
			conn = nil
		}
	}
	if conn != nil {
		conn.Close()
	}
	sv.done <- recs
}

type percentiles struct {
	P50  float64 `json:"p50"`
	P90  float64 `json:"p90"`
	P99  float64 `json:"p99"`
	P999 float64 `json:"p999"`
	Max  float64 `json:"max"`
}

type summary struct {
	Target        string             `json:"target"`
	TargetRate    float64            `json:"target_rate"`
	Arrival       string             `json:"arrival"`
	Conns         int                `json:"conns"`
	WarmupS       float64            `json:"warmup_s"`
	DurationS     float64            `json:"duration_s"`
	Requests      int                `json:"requests"`
	OK            int                `json:"ok"`
	Errors        int                `json:"errors"`
	Expired       int                `json:"expired"`
	GenDropped    int                `json:"gen_dropped"`
	Connections   int64              `json:"connections"` // opened during the whole run, warm-up included
	ErrorRate     float64            `json:"error_rate"`
	AchievedRate  float64            `json:"achieved_rate"`
	LatencyMs     percentiles        `json:"latency_ms"`
	SendDelayMs   percentiles        `json:"send_delay_ms"`
	DispatchLagMs percentiles        `json:"dispatch_lag_ms"`
	WindowP99Ms   []float64          `json:"window_p99_ms"`
	PerServer     map[string]summary `json:"per_server,omitempty"`
}

// summarize looks only at requests scheduled in the measured phase [from, to).
func summarize(c config, from, to time.Time, recs []record, dispatchLag []time.Duration, dropped int) summary {
	s := summary{
		Target: c.target, TargetRate: c.rate, Arrival: c.arrival, Conns: c.conns,
		WarmupS: c.warmup.Seconds(), DurationS: c.duration.Seconds(), GenDropped: dropped,
		Connections: connections.Load(),
	}
	var latency, sendDelay []time.Duration
	windows := make([][]time.Duration, int((to.Sub(from)+c.window-1)/c.window))
	for _, r := range recs {
		if r.scheduled.Before(from) || !r.scheduled.Before(to) {
			continue
		}
		s.Requests++
		switch {
		case r.expired:
			s.Expired++
			s.Errors++
		case !r.ok:
			s.Errors++
		default:
			s.OK++
			l := r.done.Sub(r.scheduled) // from the *scheduled* time: includes any queueing
			latency = append(latency, l)
			sendDelay = append(sendDelay, r.sent.Sub(r.scheduled))
			w := int(r.scheduled.Sub(from) / c.window)
			windows[w] = append(windows[w], l)
		}
	}
	if s.Requests > 0 {
		s.ErrorRate = float64(s.Errors) / float64(s.Requests)
	}
	s.AchievedRate = float64(s.OK) / c.duration.Seconds()
	s.LatencyMs = summarizeDurations(latency)
	s.SendDelayMs = summarizeDurations(sendDelay)
	s.DispatchLagMs = summarizeDurations(dispatchLag)
	for _, w := range windows {
		s.WindowP99Ms = append(s.WindowP99Ms, summarizeDurations(w).P99)
	}
	return s
}

func summarizeDurations(d []time.Duration) percentiles {
	if len(d) == 0 {
		return percentiles{}
	}
	slices.Sort(d)
	at := func(q float64) float64 { // nearest-rank percentile, in milliseconds
		i := int(q*float64(len(d))+0.5) - 1
		i = max(0, min(i, len(d)-1))
		return float64(d[i]) / float64(time.Millisecond)
	}
	return percentiles{P50: at(0.50), P90: at(0.90), P99: at(0.99), P999: at(0.999), Max: at(1)}
}

func writeCSV(path string, start time.Time, recs []record) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	w := bufio.NewWriter(f)
	fmt.Fprintln(w, "scheduled_us,sent_us,done_us,ok")
	for _, r := range recs {
		us := func(t time.Time) int64 {
			if t.IsZero() {
				return -1
			}
			return t.Sub(start).Microseconds()
		}
		fmt.Fprintf(w, "%d,%d,%d,%t\n", us(r.scheduled), us(r.sent), us(r.done), r.ok)
	}
	return w.Flush()
}
