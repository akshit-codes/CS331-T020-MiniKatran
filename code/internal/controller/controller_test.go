package controller

import (
	"math"
	"testing"

	"cs331lb/internal/lb"
)

var ids = []uint32{1, 2, 3, 4}

func TestInitialTable(t *testing.T) {
	weights := []float64{1, 1, 2, 0}
	buckets, err := InitialTable(ids, weights)
	if err != nil {
		t.Fatal(err)
	}
	first, second := map[uint32]int{}, map[uint32]int{}
	for b, bk := range buckets {
		first[bk.First]++
		second[bk.Second]++
		if bk.First == bk.Second {
			t.Fatalf("bucket %d: first hop = second hop", b)
		}
	}
	if first[1] != 1024 || first[2] != 1024 || first[3] != 2048 || first[4] != 0 {
		t.Errorf("first-hop counts %v, want exactly 1024/1024/2048/0", first)
	}
	if second[4] != 0 || second[0] != 0 {
		t.Errorf("second hops %v: a zero-weight backend or none must never be a second hop", second)
	}
	again, _ := InitialTable(ids, weights)
	for b := range buckets {
		if buckets[b] != again[b] {
			t.Fatal("not deterministic")
		}
	}
}

func newTestController(t *testing.T) *Controller {
	buckets, err := InitialTable(ids, []float64{1, 1, 1, 1})
	if err != nil {
		t.Fatal(err)
	}
	return New(DefaultConfig(), ids, []float64{1, 1, 1, 1}, buckets)
}

func TestGates(t *testing.T) {
	c := newTestController(t)
	if r := c.Step(100, []float64{0.15, 0.05, 0.05, 0.05}); len(r.Moves) != 0 || r.Reason != "low load" {
		t.Errorf("low load: %d moves, reason %q", len(r.Moves), r.Reason)
	}
	if r := c.Step(102, []float64{0.53, 0.5, 0.5, 0.5}); len(r.Moves) != 0 || r.Reason != "within band" {
		t.Errorf("4.4%% deviation, not active yet: %d moves, reason %q", len(r.Moves), r.Reason)
	}
	if r := c.Step(104, []float64{0.7, 0.5, 0.5, 0.5}); len(r.Moves) == 0 {
		t.Errorf("33%% deviation: no moves (%q)", r.Reason)
	}
	// Hysteresis: once active, keep correcting down to DeltaOff.
	// (Deviation is from the mean, which includes the high backend: 0.56 vs
	// mean 0.515 is 8.7%: above DeltaOff, below DeltaOn.)
	if r := c.Step(106, []float64{0.56, 0.5, 0.5, 0.5}); len(r.Moves) == 0 {
		t.Errorf("active, 8.7%% deviation (> DeltaOff): no moves (%q)", r.Reason)
	}
	if r := c.Step(108, []float64{0.51, 0.5, 0.5, 0.5}); len(r.Moves) != 0 {
		t.Errorf("1.5%% deviation (< DeltaOff): %d moves", len(r.Moves))
	}
}

// Near capacity a saturated backend is only a few percent above the mean:
// inside the band, unless the override is on.
func TestSaturationOverride(t *testing.T) {
	c := newTestController(t)
	u := []float64{1.0, 0.95, 0.95, 0.95} // 3.9% above the mean
	if r := c.Step(100, u); len(r.Moves) != 0 {
		t.Errorf("override off: %d moves inside the band", len(r.Moves))
	}
	c.SaturatedAt = 0.97
	if r := c.Step(102, u); len(r.Moves) == 0 || r.After[0] >= r.Before[0] {
		t.Errorf("override on: backend 1 at 100%% should lose buckets (%q)", r.Reason)
	}
}

func TestOverloadedBackendLosesBucketsWithinCaps(t *testing.T) {
	c := newTestController(t)
	r := c.Step(100, []float64{1.0, 0.5, 0.5, 0.5})
	if r.After[0] >= r.Before[0] {
		t.Fatalf("backend 1 (overloaded) went from %d to %d buckets", r.Before[0], r.After[0])
	}
	if len(r.Moves) > c.GlobalCap {
		t.Errorf("%d moves > global cap %d", len(r.Moves), c.GlobalCap)
	}
	for i := range r.Before {
		if d := r.After[i] - r.Before[i]; d > c.PerBackendCap || -d > c.PerBackendCap {
			t.Errorf("backend %d changed by %d > per-backend cap %d", i+1, d, c.PerBackendCap)
		}
	}
	swaps := 0
	for _, m := range r.Moves {
		if m.Swap {
			swaps++
		}
	}
	if swaps == 0 {
		t.Error("no swaps: the initial table has second hops, so free swaps should come first")
	}
}

// checkMove fails if a move changes a bucket within the dwell or drops its old first hop.
func checkMove(t *testing.T, c *Controller, now float64, m Move, last []float64) {
	t.Helper()
	if now-last[m.Bucket] < c.Dwell {
		t.Fatalf("t=%g: bucket %d changed again %.0f s after its last change (dwell %g)",
			now, m.Bucket, now-last[m.Bucket], c.Dwell)
	}
	if m.New.First != m.Old.First && m.New.Second != m.Old.First {
		t.Fatalf("t=%g: bucket %d: old first hop %d dropped from the bucket", now, m.Bucket, m.Old.First)
	}
	last[m.Bucket] = now
}

func neverChanged() []float64 {
	last := make([]float64, lb.NumBuckets)
	for i := range last {
		last[i] = math.Inf(-1)
	}
	return last
}

// The safety property: never change a bucket's first hop again within Dwell.
// The dwell only matters when nothing older is left to move: plan takes the
// oldest buckets first, so with a thousand old buckets it never reaches a recent
// one, dwell or not. So here backend 1 starts with only 32 buckets and the load
// keeps reversing: it gains buckets, then has to give buckets back when all it
// has left were changed moments ago. The controller wants to move them and must not.
func TestNeverChangesABucketWithinDwell(t *testing.T) {
	two := ids[:2]
	buckets := make([]lb.Bucket, lb.NumBuckets)
	for b := range buckets {
		buckets[b] = lb.Bucket{First: two[0], Second: two[1]}
		if b < 32 {
			buckets[b] = lb.Bucket{First: two[1], Second: two[0]}
		}
	}
	c := New(DefaultConfig(), two, []float64{1, 1}, buckets)
	last := neverChanged()
	moves, heldBack := 0, 0
	for step := range 200 {
		now := 2 * float64(step)
		u := []float64{1.0, 0.2}
		if step%4 >= 2 {
			u = []float64{0.2, 1.0}
		}
		r := c.Step(now, u)
		for _, m := range r.Moves {
			moves++
			checkMove(t, c, now, m, last)
		}
		if step%4 >= 2 && r.Target[1] < r.Before[1] && len(r.Moves) == 0 {
			heldBack++ // it wanted to take buckets from backend 1, and the dwell said no
		}
	}
	if moves == 0 || heldBack == 0 {
		t.Errorf("%d moves, %d periods held back: the test didn't exercise the dwell", moves, heldBack)
	}
}

// With plenty of buckets and persistent imbalance: many moves, and none of them
// loses a bucket's old first hop.
func TestMovesKeepTheOldFirstHop(t *testing.T) {
	c := newTestController(t)
	last := neverChanged()
	moves := 0
	for step := range 200 {
		now := 2 * float64(step)
		// Backend 1 always looks overloaded, backend 2 alternates.
		u := []float64{1.0, 0.3 + 0.4*float64(step%2), 0.5, 0.5}
		for _, m := range c.Step(now, u).Moves {
			moves++
			checkMove(t, c, now, m, last)
		}
	}
	if moves < 1000 {
		t.Errorf("only %d moves in 200 periods: the test didn't exercise the controller", moves)
	}
}

// A connection opened on the active table just before the controller starts
// must stay reachable: through the first table and through every move within
// the dwell.
func TestNewFromKeepsConnectionsOfTheActiveTable(t *testing.T) {
	active, err := lb.StaticTable(ids, []float64{1, 1, 1, 1}) // contiguous, like lbctl's
	if err != nil {
		t.Fatal(err)
	}
	active[0].Second = ids[3] // one bucket that already has a second hop
	initial, err := InitialTable(ids, []float64{1, 1, 1, 1})
	if err != nil {
		t.Fatal(err)
	}
	c := NewFrom(DefaultConfig(), ids, []float64{1, 1, 1, 1}, active, initial)

	changed := 0
	for b, bk := range c.Buckets {
		if bk.First != initial[b].First {
			t.Fatalf("bucket %d: first hop %d, want the initial table's %d", b, bk.First, initial[b].First)
		}
		if bk.Second == 0 || bk.Second == bk.First {
			t.Fatalf("bucket %d: second hop %d with first hop %d: no free swap", b, bk.Second, bk.First)
		}
		if bk.First == active[b].First {
			if c.LastChanged[b] != math.Inf(-1) {
				t.Errorf("bucket %d: first hop unchanged, yet marked as changed at %g", b, c.LastChanged[b])
			}
			if active[b].Second != 0 && bk.Second != active[b].Second {
				t.Errorf("bucket %d: old second hop %d replaced by %d", b, active[b].Second, bk.Second)
			}
			continue
		}
		changed++
		if bk.Second != active[b].First {
			t.Errorf("bucket %d: second hop %d, but the connections are on %d", b, bk.Second, active[b].First)
		}
		if c.LastChanged[b] != 0 {
			t.Errorf("bucket %d: first hop changed, LastChanged = %g, want 0", b, c.LastChanged[b])
		}
	}
	if changed < 1000 {
		t.Fatalf("only %d buckets changed: the test didn't exercise this", changed)
	}

	// Within the dwell only buckets the first table left alone may move ...
	u := []float64{1.0, 0.3, 0.5, 0.5}
	moves := 0
	for now := 2.0; now < c.Dwell; now += 2 {
		for _, m := range c.Step(now, u).Moves {
			moves++
			if m.Old.First != active[m.Bucket].First {
				t.Fatalf("t=%g: bucket %d moved although the first table changed it", now, m.Bucket)
			}
		}
	}
	if moves == 0 {
		t.Fatal("no moves within the dwell: the test didn't exercise this")
	}
	// ... and afterwards the others may too.
	sawChanged := false
	for now := c.Dwell; now < 3*c.Dwell && !sawChanged; now += 2 {
		for _, m := range c.Step(now, u).Moves {
			sawChanged = sawChanged || initial[m.Bucket].First != active[m.Bucket].First
		}
	}
	if !sawChanged {
		t.Error("buckets changed by the first table never moved after the dwell")
	}
}

func TestFloorKeepsEveryBackendVisible(t *testing.T) {
	c := newTestController(t)
	for step := range 300 {
		c.Step(float64(step)*30, []float64{5, 0.5, 0.5, 0.5}) // backend 1 hopelessly overloaded
	}
	if n := c.Counts()[0]; n < c.MinBuckets {
		t.Errorf("backend 1 has %d buckets, floor is %d", n, c.MinBuckets)
	}
}
