// Package sim simulates the controller against a model of backends and
// connections, to check that it converges, doesn't oscillate, never changes
// a bucket twice within a connection lifetime, and how each signal behaves.
//
// The model works at the level of connections, because that is what the LB
// steers: a new connection hashes to a random bucket and goes to that bucket's
// first hop; it stays on that backend until it ends (lifetime drawn from a
// distribution); while open it sends ReqRate requests per second. So a
// backend's load is (its open connections) × ReqRate, and moving buckets only
// moves load as connections turn over, which is the lag the controller must live with.
//
// Signals a backend reports, per control period (averaged over the period):
//   - "cpu":  whole-core CPU = min(ρ, 1), ρ = load / capacity. Saturates at 1.
//   - "rif":  requests in flight per core = (c·min(ρ̄,1) + queue) / c, where ρ̄
//     is the period's average utilisation and the queue is the M/M/c
//     steady-state queue at ρ̄ plus the period's average backlog, which grows
//     by (load - capacity) per second above capacity. Using ρ̄ rather than each
//     instant's ρ matters: the M/M/c queue is steeply convex near capacity, and
//     applying it to every 50-ms load wiggle made model RIF ~3× noisier than
//     RIF measured on the testbed (a real queue can't follow such wiggles).
//   - "true": ρ itself, unclipped. No real system has this; it shows the ideal.
//
// Each reading gets multiplicative noise (Noise, a coefficient of variation).
//
// Latency: every second, the p99 response time over all requests, treating
// each backend as an M/M/c queue (c = cores, each core serving capacity/c
// requests per second) at its load in that second, plus the wait for any
// backlog. This is what the SLO is about, so it is the fair yardstick across
// signals: equal utilisation is what the CPU signal aims for, not the goal.
package sim

import (
	"container/heap"
	"math"
	"math/rand/v2"

	"cs331lb/internal/controller"
	"cs331lb/internal/lb"
)

type Backend struct {
	Name     string
	Cores    int
	Capacity float64 // requests per second at 100% CPU
}

// Event changes the scenario at time At: a backend's capacity is multiplied
// by CapacityFactor (an antagonist arrives or leaves), or, with Backend < 0,
// the offered load is multiplied by LoadFactor.
type Event struct {
	At             float64
	Backend        int
	CapacityFactor float64
	LoadFactor     float64
}

type Scenario struct {
	Name     string
	Backends []Backend
	Load     float64 // offered load at the start, as a fraction of total capacity at the start
	Events   []Event
	Duration float64 // seconds
	// Connections: lifetime distribution and requests per second while open.
	MeanLifetime float64
	Lifetime     string  // "exp" (exponential) or "pareto" (heavy tail, same mean, shape 1.5)
	ReqRate      float64 // requests per second per connection
	// Controller
	Signal string // "cpu", "rif" (per core), "rif-total" or "true"
	// LifetimeBound: the longest a connection is assumed to live. The safety
	// check counts first-hop changes of a bucket within this window of its
	// previous change. It is fixed by the workload, not by the controller's Dwell.
	LifetimeBound float64
	Period        float64
	Noise         float64
	Config        controller.Config
	// Starting table: bucket shares proportional to these weights (default:
	// cores, i.e. "static by cores"). With NoController it stays that way (baselines).
	Initial      []float64
	NoController bool
	Seed         uint64
}

// Sample is the state once per second.
type Sample struct {
	T         float64
	Rho       []float64 // utilisation per backend (1-s average)
	U         []float64 // last signal reading per backend
	Buckets   []int     // first-hop buckets per backend
	Imbalance float64   // max ρ_i / ρ_ideal, ρ_ideal = total load / total capacity
	P99       float64   // p99 response time over all requests, ms (+Inf if overloaded without bound)
	Moves     int       // bucket changes in this second
}

type Result struct {
	Samples []Sample
	Steps   int
	Moves   int
	// Safety, from the simulator's own bookkeeping (not the controller's):
	Rechanges int // first-hop changes of a bucket less than LifetimeBound after its previous change
	Broken    int // connections whose backend was dropped from their bucket while they were open
	Conns     int // connections opened
	// Readings: every control period's signal values, also without a
	// controller (ctltune uses them to measure the model's own noise).
	Readings [][]float64
}

// conn is one open connection, kept in a heap ordered by end time.
type conn struct {
	end     float64
	bucket  int
	backend int
	epoch   int // see broken(): connections from before a break are already gone
}

type connHeap []conn

func (h connHeap) Len() int           { return len(h) }
func (h connHeap) Less(i, j int) bool { return h[i].end < h[j].end }
func (h connHeap) Swap(i, j int)      { h[i], h[j] = h[j], h[i] }
func (h *connHeap) Push(x any)        { *h = append(*h, x.(conn)) }
func (h *connHeap) Pop() any {
	old := *h
	x := old[len(old)-1]
	*h = old[:len(old)-1]
	return x
}

const dt = 0.05 // simulation time step, seconds

func Run(s Scenario) Result {
	rng := rand.New(rand.NewPCG(s.Seed, 1))
	nb := len(s.Backends)
	ids := make([]uint32, nb)
	cores := make([]float64, nb)
	capacity := make([]float64, nb)
	totalCap := 0.0
	for i, b := range s.Backends {
		ids[i] = uint32(i + 1)
		cores[i] = float64(b.Cores)
		capacity[i] = b.Capacity
		totalCap += b.Capacity
	}
	// Offered load L = (connection rate) × (mean lifetime) × ReqRate.
	connRate := s.Load * totalCap / (s.MeanLifetime * s.ReqRate)

	weights := cores
	if s.Initial != nil {
		weights = s.Initial
	}
	table, err := controller.InitialTable(ids, weights)
	if err != nil {
		panic(err)
	}
	ctl := controller.New(s.Config, ids, cores, table)

	var res Result
	open := &connHeap{}
	live := make([][]int, lb.NumBuckets) // live[bucket][backend]: open connections
	epoch := make([][]int, lb.NumBuckets)
	for b := range live {
		live[b] = make([]int, nb)
		epoch[b] = make([]int, nb)
	}
	liveTotal := make([]int, nb)
	lastChange := make([]float64, lb.NumBuckets) // our own record, to check the controller
	for b := range lastChange {
		lastChange[b] = math.Inf(-1)
	}

	backlog := make([]float64, nb)
	cpuSum, backlogSum, rhoSum := make([]float64, nb), make([]float64, nb), make([]float64, nb)
	periodSteps := 0
	secRho := make([]float64, nb)
	secSteps, secMoves := 0, 0
	u := make([]float64, nb)
	nextControl, nextSample := s.Period, 1.0
	events := s.Events

	for step := 0; float64(step)*dt < s.Duration; step++ {
		t := float64(step) * dt

		for len(events) > 0 && events[0].At <= t {
			e := events[0]
			events = events[1:]
			if e.Backend < 0 {
				connRate *= e.LoadFactor
			} else {
				capacity[e.Backend] *= e.CapacityFactor
			}
		}

		// Connections end ...
		for open.Len() > 0 && (*open)[0].end <= t {
			c := heap.Pop(open).(conn)
			if c.epoch == epoch[c.bucket][c.backend] {
				live[c.bucket][c.backend]--
				liveTotal[c.backend]--
			}
		}
		// ... and start: Poisson arrivals, random bucket, that bucket's first hop.
		for n := poisson(rng, connRate*dt); n > 0; n-- {
			b := rng.IntN(lb.NumBuckets)
			i := int(ctl.Buckets[b].First) - 1
			heap.Push(open, conn{end: t + lifetime(rng, s), bucket: b, backend: i, epoch: epoch[b][i]})
			live[b][i]++
			liveTotal[i]++
			res.Conns++
		}

		// Load and signals.
		totalLoad := 0.0
		for i := range s.Backends {
			load := float64(liveTotal[i]) * s.ReqRate
			totalLoad += load
			rho := load / capacity[i]
			backlog[i] = max(0, backlog[i]+(load-capacity[i])*dt)
			cpuSum[i] += min(rho, 1)
			backlogSum[i] += backlog[i]
			rhoSum[i] += rho
			secRho[i] += rho
		}
		periodSteps++
		secSteps++

		// Control.
		if t >= nextControl {
			nextControl += s.Period
			for i := range u {
				n := float64(periodSteps)
				switch rho := rhoSum[i] / n; s.Signal {
				case "cpu":
					u[i] = cpuSum[i] / n
				case "rif", "rif-total":
					u[i] = cores[i]*min(rho, 1) + mmcQueue(min(rho, 0.99), s.Backends[i].Cores) + backlogSum[i]/n
					if s.Signal == "rif" {
						u[i] /= cores[i] // per core
					}
				case "true":
					u[i] = rho
				}
				if !s.NoController { // (runs without a controller draw no random numbers here)
					u[i] *= 1 + s.Noise*rng.NormFloat64()
				}
				cpuSum[i], backlogSum[i], rhoSum[i] = 0, 0, 0
			}
			periodSteps = 0
			res.Readings = append(res.Readings, append([]float64(nil), u...))
		}
		if len(res.Readings) > res.Steps && !s.NoController {
			r := ctl.Step(t, u)
			res.Steps++
			for _, m := range r.Moves {
				if m.Old.First != m.New.First {
					if t-lastChange[m.Bucket] < s.LifetimeBound {
						res.Rechanges++
					}
					lastChange[m.Bucket] = t
				}
				res.Broken += breakDropped(m, live, epoch, liveTotal)
			}
			res.Moves += len(r.Moves)
			secMoves += len(r.Moves)
		}

		// Once per second: a sample.
		if t+dt/2 >= nextSample {
			nextSample++
			smp := Sample{T: t, Rho: make([]float64, nb), U: append([]float64(nil), u...), Buckets: ctl.Counts(), Moves: secMoves}
			ideal := totalLoad / sum(capacity)
			loads := make([]float64, nb)
			for i := range smp.Rho {
				smp.Rho[i] = secRho[i] / float64(secSteps)
				smp.Imbalance = max(smp.Imbalance, smp.Rho[i]/ideal)
				loads[i] = smp.Rho[i] * capacity[i]
				secRho[i] = 0
			}
			smp.P99 = clusterP99(loads, capacity, cores, backlog)
			secSteps, secMoves = 0, 0
			res.Samples = append(res.Samples, smp)
		}
	}
	return res
}

// breakDropped counts (and closes) the open connections of every backend that
// a move takes out of the bucket entirely: their packets now reach neither the
// first nor the second hop, so the redirector can't save them.
func breakDropped(m controller.Move, live, epoch [][]int, liveTotal []int) int {
	broken := 0
	for i, n := range live[m.Bucket] {
		id := uint32(i + 1)
		if n > 0 && id != m.New.First && id != m.New.Second {
			broken += n
			liveTotal[i] -= n
			live[m.Bucket][i] = 0
			epoch[m.Bucket][i]++ // their heap entries no longer count
		}
	}
	return broken
}

func lifetime(rng *rand.Rand, s Scenario) float64 {
	if s.Lifetime == "pareto" {
		// Pareto with shape a = 1.5 and the same mean: x_m = mean·(a-1)/a.
		const a = 1.5
		xm := s.MeanLifetime * (a - 1) / a
		return xm / math.Pow(1-rng.Float64(), 1/a)
	}
	return rng.ExpFloat64() * s.MeanLifetime
}

func poisson(rng *rand.Rand, mean float64) int {
	if mean > 30 { // normal approximation, fine for our rates
		return max(0, int(math.Round(mean+math.Sqrt(mean)*rng.NormFloat64())))
	}
	n, p, limit := 0, 1.0, math.Exp(-mean)
	for {
		p *= rng.Float64()
		if p <= limit {
			return n
		}
		n++
	}
}

// mmcQueue is the mean number of requests waiting (not in service) in an M/M/c
// queue at utilisation rho < 1 (Erlang C).
func mmcQueue(rho float64, c int) float64 {
	a := rho * float64(c) // offered load in Erlangs
	term, sum := 1.0, 0.0 // term = a^k / k!
	for k := 0; k < c; k++ {
		if k > 0 {
			term *= a / float64(k)
		}
		sum += term
	}
	last := term * a / float64(c) / (1 - rho) // a^c / c! / (1-ρ)
	pWait := last / (sum + last)
	return pWait * rho / (1 - rho)
}

// mmcTail is P(response time > t) in an M/M/c queue: c cores each serving mu
// requests/s, arrival rate lambda < c·mu. A request waits with probability
// pWait (Erlang C), for an exponential time with rate c·mu - lambda, then is
// served for an exponential time with rate mu.
func mmcTail(t, lambda, mu float64, c int) float64 {
	rho := lambda / (float64(c) * mu)
	pWait := mmcQueue(rho, c) * (1 - rho) / rho // from Lq = pWait·ρ/(1-ρ)
	theta := float64(c)*mu - lambda
	var waitThenServe float64 // P(Exp(theta) + Exp(mu) > t)
	if math.Abs(theta-mu) < 1e-9 {
		waitThenServe = (1 + mu*t) * math.Exp(-mu*t)
	} else {
		waitThenServe = (mu*math.Exp(-theta*t) - theta*math.Exp(-mu*t)) / (mu - theta)
	}
	return (1-pWait)*math.Exp(-mu*t) + pWait*waitThenServe
}

// clusterP99 is the p99 response time (ms) over all requests: each backend
// contributes in proportion to its load. A backlog adds its drain time
// (backlog / capacity) to every request of that backend; a backend at or
// above capacity is treated as being at 99%.
func clusterP99(loads, capacity, cores, backlog []float64) float64 {
	total := sum(loads)
	if total == 0 {
		return 0
	}
	tail := func(t float64) float64 {
		p := 0.0
		for i, l := range loads {
			if l == 0 {
				continue
			}
			c := int(cores[i])
			mu := capacity[i] / cores[i]
			lambda := min(l, 0.99*capacity[i])
			delay := backlog[i] / capacity[i]
			pi := 1.0
			if t > delay {
				pi = mmcTail(t-delay, lambda, mu, c)
			}
			p += l / total * pi
		}
		return p
	}
	lo, hi := 0.0, 60.0 // seconds; bisection for tail(t) = 1%
	if tail(hi) > 0.01 {
		return math.Inf(1)
	}
	for range 60 {
		mid := (lo + hi) / 2
		if tail(mid) > 0.01 {
			lo = mid
		} else {
			hi = mid
		}
	}
	return hi * 1000
}

func sum(xs []float64) float64 {
	s := 0.0
	for _, x := range xs {
		s += x
	}
	return s
}
