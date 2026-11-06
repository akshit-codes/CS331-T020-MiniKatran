# Benchmark campaign (D36, D37)

The whole project has about 10 hours left, so the measurement plan is cut to
**about 2.5 hours of testbed time** (plus ~45 minutes of reserve for one
rerun), spent where it buys the most confidence. The important experiments
will be rerun later on our four laptops. This document replaces the
measurement parts of phases 2–4 in [plan.md](plan.md).
[measurement-protocol.md](measurement-protocol.md) still applies, except where
this document says otherwise (shorter steps, fewer repeats).

## Common setup

| | |
|---|---|
| Machine state | `tune.sh apply` for each session (turbo off, fixed clocks, desktop on CPUs 0–1) |
| Backends | beP1 (2 P-cores, `-slowness 1.6`: an older server), beP2 (2 P-cores), beE1, beE2 (4 E-cores each). Capacities roughly 1000 / 1630 / 1770 / 1770 req/s, so *equal* and *by cores* are both wrong, in different directions |
| Request cost | exponential, mean 1 ms of P-core CPU (`backend -cost exp`), recalibrated at the start of each session |
| Traffic | Poisson arrivals. **Churn class:** 128 connections planned; the harness used **512** from its first version, before any measurement (reason not recorded), 20 requests each, then a new connection (lifetime about 2.2 s, measured at 75% load: 512 connections, 235 opened per second; far below the 20 s dwell). **Long-lived class** (dynamic runs only): 100 persistent connections at 2 req/s each, which outlive the dwell (RQ4) |
| SLO | p99 ≤ **20 ms**, errors ≤ 0.1%, ≥ 98% of the scheduled requests served. With exponential cost one E-core request alone has a p99 of ~8.5 ms, so 10 ms would mostly measure single-request times, not load (dry run). Capacity at 10 and 50 ms is read off the same probes, approximately (interpolated) |
| Load step ("probe") | 5 s warm-up + **20 s measured** + 2 s rest = 27 s |
| Capacity search | bisection, **5 probes**, range by session: S1 0.5–1.0 × theoretical; S2 0.7–1.05 × predicted from the table's shares; S2-rif-total and S6's tables 0.85–1.15 × predicted; S6's denominators 0.8–1.2 × S1's capacity. **Resolution about 1–2%**, ~2.3 min. If the result sits at an edge, one extra probe widens the range |
| Always logged | loadgen summary + 5-s window p99; `sigmon` (all four signals and requests/s per backend, every second); `resetmon`; controller log; sysmon |
| Order | **Planned:** interleaved (A B C A B C …). **What happened:** see [below](#run-order-what-actually-happened) |

## Tuning the controller per signal: one rule for all (D36)

So that RQ3 compares signals, not how well we tuned each one:

1. **Measure noise** (session S1): oracle table, no controller, `sigmon` for
   150 s at 50% and at 80% of oracle capacity. For each signal and each
   averaging window P ∈ {1, 2, 4, 8} s, the noise σ(P) is the median over
   backends of the coefficient of variation of the P-second averages (80%
   run; 50% is reported).
2. **Dead band from noise**: δ_on = k·σ(P), δ_off = δ_on/2, with the
   multiplier k ∈ {1.5, 2, 3} chosen in step 4 like the period (a fixed k = 3
   made the CPU controller ignore a backend losing half its capacity in a
   dry run: saturated CPU can't deviate more than ~20%).
3. **Simulate** each P with the same scenarios (static from by-cores at 75%
   load; one backend losing half its capacity), 3 seeds, with the simulator's
   reading noise calibrated to the measured σ(P). Recovery = time until no
   backend is over capacity (10-s average utilisation ≤ 1) for 30 s in a row.
4. **Pick**, among the (P, k) whose steady-state churn is at most max(40 moves/min
   (1% of the table per minute), 2 × the least churn that signal achieves; revised
   in D36), the one with the fastest recovery after the step (ties: the larger P,
   then the larger k).
5. **Everything else is identical** for all signals: gain 0.3, caps, floor,
   dwell 20 s, low-load gate 0.2, overload override off.

Smoothing *is* the averaging window P: an EWMA on top would be a second knob
trading noise against lag the same way. Machine time: ~6 min (in S1); the
simulations take a few minutes of CPU time.

PSI isn't tuned or used as a controller input. Its values (~0.02–0.1 in a quick
check) never pass the low-load gate, because with our pool backend requests
queue in the process, not in the kernel (D25). It is logged passively in every
run instead.

## Experiments

| Session | Experiment | Answers | Configs × repeats | Per run | Time |
|---|---|---|---|---|---|
| S1 | End-to-end check (`e2e_lb.py`) | gate: nothing measured if this fails | 1 | 2 min | 2 min |
| S1 | Denominators: each backend's own capacity, direct (no LB) | RQ1 denominators, oracle weights, **CV of the short protocol** | 4 backends × 2 | 2.3 min | 18 min |
| S1 | Signal noise (fixed oracle table) | tuning rule (D36) | 2 loads × 1 | 2.8 min | 6 min |
| — | Tuning in the simulator | per-signal period and band | 4 P × 2 signals × 3 seeds | CPU only | ~5 min |
| S2 | Static policies through the LB: equal, by cores, oracle | **RQ1** static, H1 | equal ×1, cores ×1, oracle ×3 | 2.3 min | 12 min |
| S2 | Controller-found tables: converge 90 s at 75% load from by-cores, freeze, search | **RQ1** (H2: matches oracle?), H4 | cpu ×2, rif ×2 | 3.8 min | 15 min |
| S3 | **Step:** beP2's slowness 1 → 2 at 60 s, run to 180 s, 75% load | **RQ2**, RQ3 main, RQ4 | oracle (static) ×1, cpu ×3, rif ×3 | 3.5 min | 25 min |
| S3 | **Antagonist:** 60% duty on beE1's cores (own cgroup) from 60 to 180 s, off until 240 s | **RQ2**, **RQ3** main + extras (H5), RQ4 | oracle ×1, cpu ×3, rif ×3, cgroup ×1 | 4.5 min | 36 min |
| S3 | No-protection contrast: antagonist run, cpu controller with dwell 0, no caps, gain 1 | **RQ4**: what the dwell prevents, live | 1 | 4.5 min | 5 min |
| S4 | Fast disturbance: antagonist 10 s on / 10 s off from 60 to 180 s (a 20-s period) | RQ2 / H3, second timescale point | cpu ×1, rif ×1 | 4 min | 8 min |
| S4 | SMT sibling: antagonist on beP1's idle hyperthreads, passive (sigmon) | H6 | 1 | 3 min | 3 min |
| S4 | Hook cost: director at TC vs XDP, fixed packet rates (D35) | side measurement | 2 hooks × 2 rates (2000, 4000 req/s) | 20 s | ~2 min |
| S4 | **PSI with a kernel-queue backend** (D25): all traffic to beP2; 40-s phases at 50%, 90%, 130% of its capacity, then 50% + antagonist; sigmon only | H5 / RQ3: does PSI see *request* overload when the server queues in the kernel? | backend `-mode pool` vs `-mode kernel` | 3 min | 7 min |
| | Setup/teardown between runs (~20 s each) | | ~45 runs | | ~10 min |
| | **Total** | | | | **~2.6 h** (+ ~45 min reserve) |

Session order matters: S1 → tuning → S2 → S3 → S4. S4 is dropped first if
time runs out. Every session is one `tune.sh apply` … `undo`.

**Dropped:**
- The stateful (conntrack) baseline. It would only show that per-connection
  state keeps connections alive, which nobody doubts; RQ4's contrast is no
  second hop (move test, done).
- The timescale sweep, shrunk to two points (120 s disturbance in S3, 10 s on / 10 s off
  in S4). Partly restored in S5 (5, 20, 60 s), after seeing S3–S4.
- The full noise check's 10/5 repeats.
- A sweep over the SLO bound L (one L, two more read off the same probes).

**Already done, reused:** move test A/B/C (RQ4: second hops vs none vs no
cookie check, `results/move-20261102-030314`); simulator results (D33).

## Follow-up session S5 (after S1–S4)

Total RIF: 2 more S2 tables with a wider search range, 2 runs each of step and
antagonist; the RQ4 contrast with redirectors not forwarding (1 run); a
sibling antagonist that contends (1 run); a flapping sweep at 5, 20 and 60 s
for oracle, CPU and total RIF (1 run each). ~63 min.

## Follow-up session S6 (2026-11-06, after a review)

One session, about 70 min, for three questions the earlier runs couldn't answer:
- **Denominators with all backends busy:** each backend alone vs with the other
  three held at 90% of their capacity, no LB, 1 round, order alternating per
  backend. Our protocol has this check; it was never applied to the denominators.
- **The gap:** the oracle's table through the LB vs the same shares sent straight
  to the backends (`loadgen -targets`), 2 runs each, interleaved with the tables.
- **Hindsight static table:** the capacities during the step and during the
  antagonist (1 search each), then a static table proportional to them, step and
  antagonist, 2 runs each, alternating.
- **CPU vs total RIF tables:** 3 pairs in ABBA order with the oracle and the
  no-LB runs in between, same search range for all (D40).

Runs after the startup fix; everything before it predates the fix.

It ran as planned on 2026-11-06 (69 min, the third pair included, no invalid runs).
Answers: [results.md](results.md#rq1-usable-capacity-fraction) and
[RQ2](results.md#rq2-when-does-the-dynamic-controller-do-better-than-a-static-table).
A browser stayed open on the desktop during it, confined to CPUs 0–1 by `tune.sh`.

## Follow-up session S7 (2026-11-06)

The final code's own "0 broken": S1–S5 predate the startup fix and S6 has no
controller run with long-lived connections. The antagonist scenario, CPU, total RIF,
total RIF, CPU, 16 min in all. Valid, and 0 broken:
[results.md](results.md#rq4-do-connections-survive-table-changes).

## Run order: what actually happened

| Session | Order | So |
|---|---|---|
| S1 | beP1 beP2 beE1 beE2, twice | interleaved |
| S2 | oracle, equal, oracle, cores, oracle, *then* ctl-cpu, ctl-rif, ctl-cpu, ctl-rif | static tables interleaved among themselves, and controller tables among themselves, but all static ones ran first |
| S2-extra, S2-rif-total | total-RIF tables only, later sessions | not interleaved with anything else; S6 fixes this for CPU vs total RIF |
| S3 | step: oracle once, then cpu, rif ×3; antagonist: oracle once, then cpu, rif, cgroup, cpu, rif, cpu, rif, no-protection | oracle always first in its block, once; the step block all before the antagonist block |
| S4 | flapping: oracle, cpu, rif; then sibling, PSI, hook | one run each |
| S5 | total-RIF step, antagonist, stream sibling, total-RIF step, antagonist; flapping 5, 20, 60 s (oracle, cpu, total RIF each); redirector contrast last | total RIF compared with S3's CPU runs across sessions |
| S6 | alone/busy alternating per backend; disturbed capacities; hindsight step, antagonist ×2; cpu, total RIF, oracle, no LB, total RIF, cpu, no LB, oracle, cpu, total RIF | interleaved, in one session |
| S7 | antagonist: cpu, total RIF, total RIF, cpu | interleaved (ABBA) |

## What the shorter runs can and can't claim

| Experiment | Can claim | Can't claim |
|---|---|---|
| Denominators | each backend's capacity at SLO; a rough CV for the 20-s protocol (4 pairs) | exact absolute capacities: 20-s probes read a few % high (quick vs full noise check: +3–5%). Usable-capacity *fractions* are ratios of capacities measured the same way, so most of that bias cancels |
| Static policies | equal and by-cores vs oracle: expected 10–35% apart, far beyond resolution (about 1%) and noise, so 1 run each is enough (H1) | differences smaller than ~3% (2× resolution, or 2× the measured CV if larger) |
| Controller-found tables | "matches oracle within ~3%" (H2); whether CPU and RIF settle on different tables and what that does to p99 (H4) | anything about the live controller under *changing* load: the table is frozen for the search |
| Step and antagonist | the time course: p99 per 5-s window (~2000 requests each, so ±~15%), recovery time to 5 s resolution, moves, broken connections. With 3 repeats: differences whose ranges don't overlap (e.g. "recovers in 30–40 s" vs "never") | settle-time differences under ~2 windows (10 s), p99 differences under ~30%, anything about loads other than 75% |
| Extras (cgroup, PSI) | which signals *see* the antagonist (8 runs of passive logs); that a cgroup-CPU controller doesn't move load away (1 run) | a ranking of PSI or cgroup CPU as controller signals |
| RQ4 | exact counts: churned connections broken (expected 0: "0 of N", 95% upper bound 3/N); long-lived connections broken per controller run (the real trade-off number, small counts, raw); live contrast without the dwell | a rate model of breakage vs change rate (would need the sweep) |
| Timescales | whether the controller does better than the oracle's table at a 120 s disturbance and at 10 s on / 10 s off | where the crossover lies (H3 only partly) |
| Everything | one laptop, one session per experiment | repeatability across machines: that's what the later 4-laptop reruns are for (S1 denominators, S2 static, S3 antagonist cpu/rif) |
