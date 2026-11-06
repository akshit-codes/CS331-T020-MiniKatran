// Package controller decides how many buckets each backend gets and which
// buckets move (D11). It is pure logic: the caller supplies time and load
// signals and publishes the resulting table (cmd/controller), or a model does
// (internal/sim). Nothing here touches the kernel or the clock.
package controller

import (
	"encoding/binary"
	"fmt"
	"math"
	"slices"

	"cs331lb/internal/lb"
)

// Config holds the controller's knobs. See DefaultConfig for values and reasons.
type Config struct {
	Gain        float64 // exponent in target = n·(mean/u)^Gain: how hard to correct per period
	MaxStep     float64 // a backend's target changes by at most this fraction of its buckets per period
	MinBuckets  int     // floor: every backend keeps at least this many buckets (so we keep seeing it)
	UMin        float64 // loads below this count as UMin (avoids dividing by ~0)
	DeltaOn     float64 // start correcting when some backend's load is this far from the mean (relative)
	DeltaOff    float64 // stop once every backend is closer than this
	LowLoadGate float64 // do nothing while the mean load is below this: idle signals are mostly noise
	// SaturatedAt, if > 0: a backend at or above this load, while the mean is
	// below it, is always out of band. For utilisation signals (CPU) only: near
	// capacity, "within DeltaOff of the mean" can still mean over 100%, and a
	// saturated CPU reading can't deviate much (ctlsim "saturated"). Off by
	// default: near capacity, noise alone triggers it (more churn).
	SaturatedAt   float64
	Dwell         float64 // seconds a bucket stays frozen after its first hop changed; must be ≥ the longest connection
	GlobalCap     int     // at most this many buckets change per period
	PerBackendCap int     // at most this many buckets per backend are gained or lost per period
}

// DefaultConfig: starting values from D11, to be tuned with the simulator and
// later live (phase 3).
func DefaultConfig() Config {
	return Config{
		Gain:          0.3,
		MaxStep:       0.25,
		MinBuckets:    16,
		UMin:          0.05,
		DeltaOn:       0.10, // ~3× the per-reading noise we measured (CV ~2-3%)
		DeltaOff:      0.05,
		LowLoadGate:   0.20,
		Dwell:         20,
		GlobalCap:     lb.NumBuckets / 50, // 2% of the table per period
		PerBackendCap: lb.NumBuckets / 100,
	}
}

// Controller holds the table and, for every bucket, when its first hop last changed.
type Controller struct {
	Config
	IDs         []uint32    // backend ids, in backends.conf order; index i everywhere below
	Weights     []float64   // capacity weights c_i for the mean (we use cores)
	Buckets     []lb.Bucket // the current table
	LastChanged []float64   // per bucket: time of the last first-hop change (-Inf = never)
	active      bool        // hysteresis state: are we correcting right now?
}

// Move is one bucket change.
type Move struct {
	Bucket   int
	Old, New lb.Bucket
	Swap     bool // first and second hop exchanged (else: reassigned to a new first hop)
}

// StepResult explains one control period.
type StepResult struct {
	Mean          float64 // capacity-weighted mean load ū
	MaxDeviation  float64 // max_i |u_i/ū - 1|
	Reason        string  // why we did (or didn't) change anything
	Before, After []int   // buckets (first hop) per backend
	Target        []int   // what the update rule wanted (before caps)
	Moves         []Move
}

// New starts a controller from a table, e.g. InitialTable's.
func New(cfg Config, ids []uint32, weights []float64, buckets []lb.Bucket) *Controller {
	last := make([]float64, len(buckets))
	for i := range last {
		last[i] = math.Inf(-1)
	}
	return &Controller{Config: cfg, IDs: ids, Weights: weights, Buckets: slices.Clone(buckets), LastChanged: last}
}

// NewFrom starts a controller whose first table replaces the active one while
// traffic may already be flowing: buckets whose first hop changes keep the old
// first hop as second hop (lb.WithSecondHops), and count as changed at time 0, so
// the dwell also protects the connections opened just before the first publish.
// Publish c.Buckets, not initial. It can't know when the active table's own
// changes were made: connections that only live on its second hops are safe
// only if the first hop stays, so start at least Dwell after any earlier change.
func NewFrom(cfg Config, ids []uint32, weights []float64, active, initial []lb.Bucket) *Controller {
	c := New(cfg, ids, weights, lb.WithSecondHops(active, initial))
	for b := range c.Buckets {
		if c.Buckets[b].First != active[b].First {
			c.LastChanged[b] = 0
		}
	}
	return c
}

// --- Initial table: rendezvous hashing ---

// score ranks backend id for bucket b (rendezvous / highest-random-weight
// hashing): every bucket has its own random-looking order of backends.
func score(bucket int, id uint32) uint64 {
	var buf [8]byte
	binary.LittleEndian.PutUint32(buf[:4], uint32(bucket))
	binary.LittleEndian.PutUint32(buf[4:], id)
	x := binary.LittleEndian.Uint64(buf[:])
	x ^= x >> 33 // splitmix64 finaliser: any good 64-bit mixer works
	x *= 0xff51afd7ed558ccd
	x ^= x >> 33
	x *= 0xc4ceb9fe1a85ec53
	x ^= x >> 33
	return x
}

// ranking returns the backends' indices in bucket b's preference order.
func ranking(bucket int, ids []uint32) []int {
	order := make([]int, len(ids))
	for i := range order {
		order[i] = i
	}
	slices.SortFunc(order, func(a, b int) int {
		sa, sb := score(bucket, ids[a]), score(bucket, ids[b])
		if sa > sb {
			return -1
		}
		if sa < sb {
			return 1
		}
		return 0
	})
	return order
}

// InitialTable gives backends bucket counts proportional to weights (exactly,
// largest remainder). Each bucket's first hop is its highest-ranked backend
// that still has room, and its second hop is its highest-ranked other backend
// (GLB-style, D6). So the controller has free swaps from the start, and if a
// backend is removed, its buckets' second hops are spread over all the others.
// Backends with weight 0 get no buckets and are never second hops.
func InitialTable(ids []uint32, weights []float64) ([]lb.Bucket, error) {
	room, err := lb.Apportion(lb.NumBuckets, weights)
	if err != nil {
		return nil, err
	}
	buckets := make([]lb.Bucket, lb.NumBuckets)
	for b := range buckets {
		order := ranking(b, ids)
		first := -1
		for _, i := range order {
			if room[i] > 0 {
				first = i
				break
			}
		}
		room[first]--
		buckets[b].First = ids[first]
		for _, i := range order {
			if i != first && weights[i] > 0 {
				buckets[b].Second = ids[i]
				break
			}
		}
	}
	return buckets, nil
}

// --- The update rule ---

func (c *Controller) index(id uint32) int {
	return slices.Index(c.IDs, id)
}

// Counts returns how many buckets each backend is first hop of.
func (c *Controller) Counts() []int {
	n := make([]int, len(c.IDs))
	for _, b := range c.Buckets {
		if i := c.index(b.First); i >= 0 {
			n[i]++
		}
	}
	return n
}

// Settled reports whether bucket b may be changed at time now: its first hop
// last changed at least Dwell seconds ago. Moving only settled buckets is what
// makes a broken connection impossible as long as no connection lives longer
// than Dwell: when a bucket changes, the backends that can still hold its
// connections are its first hop and (until Dwell has passed) its previous
// first hop, which every move keeps in the bucket (as first or second hop).
func (c *Controller) Settled(b int, now float64) bool {
	return now-c.LastChanged[b] >= c.Dwell
}

// Step runs one control period. u[i] is backend i's load relative to its
// capacity over the period (0 = idle, 1 = full), from a signal plugin.
func (c *Controller) Step(now float64, u []float64) StepResult {
	res := StepResult{Before: c.Counts()}
	res.After = res.Before

	// 1. The capacity-weighted mean load, and how far each backend is from it.
	var sum, wsum float64
	for i := range u {
		sum += c.Weights[i] * u[i]
		wsum += c.Weights[i]
	}
	res.Mean = sum / wsum
	saturated := false
	for i := range u {
		res.MaxDeviation = max(res.MaxDeviation, math.Abs(u[i]/res.Mean-1))
		if c.SaturatedAt > 0 && u[i] >= c.SaturatedAt && res.Mean < c.SaturatedAt {
			saturated = true
		}
	}

	// 2. Gates: low load, and hysteresis (start above DeltaOn, stop below DeltaOff).
	if res.Mean < c.LowLoadGate {
		c.active = false
		res.Reason = "low load"
		return res
	}
	if saturated || (!c.active && res.MaxDeviation > c.DeltaOn) {
		c.active = true
	} else if c.active && res.MaxDeviation < c.DeltaOff {
		c.active = false
	}
	if !c.active {
		res.Reason = "within band"
		return res
	}

	// 3. Targets: target_i = n_i·(ū/u_i)^Gain, clamped to ±MaxStep, at least
	// MinBuckets, scaled to NumBuckets with largest-remainder rounding.
	want := make([]float64, len(u))
	for i, n := range res.Before {
		t := float64(n) * math.Pow(res.Mean/max(u[i], c.UMin), c.Gain)
		t = min(max(t, float64(n)*(1-c.MaxStep)), float64(n)*(1+c.MaxStep))
		want[i] = max(t, float64(c.MinBuckets))
	}
	target, err := lb.Apportion(lb.NumBuckets, want)
	if err != nil {
		res.Reason = err.Error()
		return res
	}
	res.Target = target

	// 4. How many buckets each backend should gain (+) or lose (-) this
	// period, within the per-backend cap and never below the floor.
	need := make([]int, len(u))
	for i := range need {
		need[i] = max(min(target[i]-res.Before[i], c.PerBackendCap), -c.PerBackendCap)
		need[i] = max(need[i], c.MinBuckets-res.Before[i])
	}

	// 5. Moves, only on settled buckets, at most GlobalCap of them.
	res.Moves = c.plan(now, need)
	for _, m := range res.Moves {
		c.Buckets[m.Bucket] = m.New
		c.LastChanged[m.Bucket] = now
	}
	res.After = c.Counts()
	res.Reason = fmt.Sprintf("%d moves", len(res.Moves))
	return res
}

// plan picks the buckets to change. First free swaps: a settled bucket
// [d|r] whose first hop d should lose buckets and whose second hop r should
// gain becomes [r|d]; every connection stays reachable. Then reassignments:
// [d|x] becomes [r|d] for the receiver r that needs the most. x drops out of
// the bucket, so we prefer buckets without a second hop, then those changed
// longest ago (their old connections are the most surely gone).
func (c *Controller) plan(now float64, need []int) []Move {
	var settled []int
	for b := range c.Buckets {
		if c.Settled(b, now) {
			settled = append(settled, b)
		}
	}
	slices.SortStableFunc(settled, func(a, b int) int { // oldest change first
		return cmpFloat(c.LastChanged[a], c.LastChanged[b])
	})

	var moves []Move
	used := map[int]bool{}
	full := func() bool { return len(moves) >= c.GlobalCap }

	for _, b := range settled { // swaps
		if full() {
			return moves
		}
		old := c.Buckets[b]
		d, r := c.index(old.First), c.index(old.Second)
		if d >= 0 && r >= 0 && need[d] < 0 && need[r] > 0 {
			moves = append(moves, Move{Bucket: b, Old: old, New: lb.Bucket{First: old.Second, Second: old.First}, Swap: true})
			need[d]++
			need[r]--
			used[b] = true
		}
	}

	slices.SortStableFunc(settled, func(a, b int) int { // no second hop first, then oldest
		ea, eb := c.Buckets[a].Second == 0, c.Buckets[b].Second == 0
		if ea != eb {
			if ea {
				return -1
			}
			return 1
		}
		return cmpFloat(c.LastChanged[a], c.LastChanged[b])
	})
	for _, b := range settled { // reassignments
		if full() {
			break
		}
		old := c.Buckets[b]
		d := c.index(old.First)
		if used[b] || d < 0 || need[d] >= 0 {
			continue
		}
		r := -1
		for i := range need {
			if i != d && need[i] > 0 && (r < 0 || need[i] > need[r]) {
				r = i
			}
		}
		if r < 0 {
			break // nobody needs more
		}
		moves = append(moves, Move{Bucket: b, Old: old, New: lb.Bucket{First: c.IDs[r], Second: old.First}})
		need[d]++
		need[r]--
	}
	return moves
}

func cmpFloat(a, b float64) int {
	switch {
	case a < b:
		return -1
	case a > b:
		return 1
	}
	return 0
}
