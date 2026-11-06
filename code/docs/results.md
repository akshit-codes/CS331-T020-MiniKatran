# Results by research question

From the campaign of 2026-11-02 and two follow-up sessions, S6 and S7, on 2026-11-06
([campaign.md](campaign.md); all tables and plots:
[results/campaign-20261102/summary.md](../results/campaign-20261102/summary.md)),
plus the earlier move test (RQ4) and simulator (D33). Numbers were checked against
those data files on 2026-11-06 (journal). Where S6 changed a conclusion, the old
numbers stay next to the new ones. The one run on real laptops has its own section
([Real laptops](#real-laptops)), and isn't part of any claim before it.

**Setup in one paragraph.** One laptop (i7-14650HX, turbo off, fixed clocks).
Four backends of unequal capacity: beP1 (2 P-cores, slowed 1.6×) 957 req/s,
beP2 (2 P-cores) 1656, beE1/beE2 (4 E-cores each) 1810 each, at p99 ≤ 20 ms.
Requests cost 1 ms of P-core CPU on average (exponential); an E-core takes
1.85× as long. Clients: 512 connections of 20 requests each, plus 100
long-lived connections in the dynamic runs. LB at TC ingress (D35). The
controller is tuned per signal by one rule (D36).

**The tables have names that promise too much, so:**
- **"Oracle"** is only our name for the table proportional to each backend's
  capacity measured alone, before any disturbance. Nobody searched for the best
  static split, and it isn't one: 2 of the 5 CPU tables measured above the oracle's
  mean in their session (96.0% vs 95.1%; 100.1% vs 96.7%). After a disturbance it
  is out of date.
- **"Hindsight"** (S6) is proportional to the capacities that hold *during* one
  disturbance, measured beforehand. It knows what will happen, which no real table
  does.

**Applies to every claim below.**
- Short runs: 20-s probes, 5-s windows, **1–3 runs per configuration**; the
  number of runs is given with every result. This falls short of our own protocol
  (≥ 5 searches, ≥ 10 fixed-rate runs, never a single run), and we say where:
  [measurement-protocol.md](measurement-protocol.md#where-the-campaign-fell-short-of-this).
- **The ~5% limit is a rule of thumb.** The smallest difference we claim between
  capacities is 2× the 2.5% run-to-run spread, from only 4 standalone pairs of 20-s
  campaign probes: its 95% interval makes the limit anywhere from 3% to 15%. (The
  ~1.5% in [plan.md](plan.md) is a different measurement: the full noise check, with
  longer probes and 5 repeats. These short probes are noisier, so ~5% applies here.) And p99 near 20 ms isn't
  monotonic with load: the oracle's S2 probes at 87.5–100.6% of the sum range over
  12.6–27.7 ms, and the same rate (95.1%) gave 15.7 and 27.7 ms in different repeats.
- **Order:** most sessions were not interleaved A B C A B C. In S2 all static
  policies ran before the controller tables; in S3 the oracle ran once, first in
  each block; total-RIF runs (S2-extra, S2-rif-total, S5) came hours after what
  they're compared with, and hindsight (S6) days after, with a reboot in between.
  Only S6's tables and S7's four runs are properly interleaved
  ([what actually happened](campaign.md#run-order-what-actually-happened)). The
  machine's calibration stayed within 0.4% across all nine sessions, but that
  checks one CPU loop, not the whole testbed.
- **The startup fix (D40) came after S1–S5.** Those runs let the controller publish
  its first table without second hops from the active one. All had 0 backend resets
  and 0 broken long-lived connections, so they weren't rerun. S6's tables were found
  with the fix, and S7 repeats the antagonist scenario on the final code (RQ4).
- **The inner map's definition changed after all the runs** (key and value sizes instead of
  BTF types, so clang 21 can build it; journal). This doesn't affect the numbers: built with
  one compiler, old and new source give identical instructions, relocations and map sizes.
  Only the debug type information differs.
- Window p99 counts successful requests only, so runs with errors get no
  recovery time.
- One machine (namespaces, not a network), one synthetic workload, one load
  level for the dynamic runs (75% of the summed capacities). The one run on real
  laptops is [separate](#real-laptops).
- Backend failures weren't tested. From reading the code, not from a run: with whole-core
  CPU on one machine a dead backend's cores look idle, so the controller would give it
  more buckets.

## Hypotheses: outcome

The hypotheses ([plan.md](plan.md)) are unchanged since they were written
(git history). Several *tests* were added after we saw data. Those results
are exploratory: they explain a surprise but weren't predicted.

| | hypothesis (short) | outcome | runs | changed after seeing data |
|---|---|---|---|---|
| H1 | by cores < oracle | **supported** (86.7% vs 95.1%) | 1 vs 3 | — |
| H2 | controller table ≈ oracle, no breaks | CPU: **supported, with a wide spread** (4.0 points below in S6; runs 87.2–100.1%). Total RIF: **supported in S2, on the edge in S6** (5.7 points below, all 3 runs below both oracle runs). RIF per core: **not supported** | 2–3 (S2), 3 (S6) | total RIF added after per-core RIF lost; S6 interleaved |
| H3 | controller helps only for slow disturbances | first half **consistent**, second half **untested**: every disturbance we ran lasts longer than the threshold, so nothing tested "too fast to help" (see RQ2) | 1–3 per point | the sweep (5, 20, 60 s) was added after S3–S4 |
| H4 | CPU loses to RIF on unequal cores | **not supported**: RIF per core lost to CPU; total RIF and CPU can't be told apart | 2–3 (S2), 3 (S6) | the normalisation explanation and its test (total RIF) came after |
| H5 | cgroup CPU blind to a neighbour; PSI, CPU see it | **supported** | 1 (+1 controller run) | — |
| H6 | only RIF sees a sibling-hyperthread neighbour | **not supported**: time-based signals see it too and RIF the most, PSI not. But the rise was small enough that no controller would have acted on it | 1 | the contending ("stream") neighbour was designed after S4's neighbour didn't contend |
| H7 | one change breaks nothing; breaks grow with change rate | first half **supported**; second half **not tested live** (simulator only) | 2 + 34 + 4 | the live contrast was changed to "redirectors not forwarding" after the planned one broke nothing |

Also decided after seeing data, before any campaign measurement: the SLO
bound went from 10 to 20 ms (dry run: one E-core request alone has a p99 of
~8.5 ms), and the tuning rule got its multiplier k and the simulator's
connection scaling (D36 corrections).

## RQ1: usable capacity fraction

Capacity at SLO through the LB ÷ sum of the backends' own capacities (S1: 6234
req/s). S2 and S6 side by side; only S6 is interleaved:

| policy | S2: runs, fraction | S6: runs, fraction |
|---|---|---|
| equal shares | 1, 62.5% | – |
| shares by cores | 1, 86.7% | – |
| oracle | 3, 95.1% (94.0–96.2) | 2, 96.7% (96.2–97.1) |
| table found by the CPU controller | 2, ≥ 92.8% (89.5*–96.0) | 3, 92.7% (87.2–100.1) |
| table found by the total-RIF controller | 3, ≥ 92.8% (90.7–95.1*) | 3, 90.9% (88.7–93.4) |
| table found by the RIF-per-core controller | 2, 86.6% (84.9–88.3) | – |
| same shares as the oracle, **no LB** | – | 2, 100.0% (99.1–100.9) |

\* every probe up to this rate passed: a lower bound, so the mean is one too.

- **H1 supported.** By cores loses 8.4 points to the oracle (> 5%), equal
  33 points: an E-core is not worth a P-core.
- **H2:** the CPU tables are within the ~5% of the oracle in both sessions, but a
  single CPU table can land anywhere from 87% to 100%. The total-RIF tables were
  within it in S2 and just past it in S6 (5.7 points, 3 runs vs 2). Since 5% is
  itself soft, we say *close, possibly a few points below*, rather than *matches*.
- **CPU vs total RIF (S6, interleaved, 3 runs each): no difference we can
  resolve.** 92.7% vs 90.9%, 1.8 points, with overlapping runs. Before S6 we had
  ≥ 92.8% for both across sessions, so the conclusion stands and is now better
  founded. Pooling sessions: CPU ≥ 92.7% (5 runs), total RIF ≥ 91.9% (6).
- **A table's capacity varies more than its shares suggest.** The CPU tables differ
  by only a few points of share (beP1 16.5–17.4%, beP2 24.7–26.5%), yet range over
  13 points of capacity, and the prediction from shares misses by up to 7 points. Part of
  that is the noise near the SLO described above; S6 can't separate it from real
  differences between tables.
- **RIF per core loses because of the normalisation** (an explanation found
  after the data, then tested): total RIF's three S2 runs (90.7–95.1%) don't
  overlap with RIF per core's (84.9–88.3%). The first total-RIF run alone
  (≥ 95.1%) suggested it beat CPU. With an open search range the next two
  landed at 90.7% and 92.7%, so it matches CPU, not more.

**Why does the oracle stop at ~95%?** We had guessed uneven arrival (not tested).
S6 tested two parts of it, in one session:

| | capacity (req/s) | ÷ S1 alone (6234) | ÷ S6 alone (6296) | ÷ S6 busy (6173) |
|---|---|---|---|---|
| oracle through the LB (2 runs) | 5997, 6056 | 96.7% | 95.7% | 97.6% |
| same shares, no LB (2 runs) | 6175, 6292 | 100.0% | 99.0% | 101.0% |

- **Busy denominators explain a part, at most about half.** With the other three
  backends at 90% load each backend's capacity falls 1.2–2.5%, and the sum 2.0%.
  That moves the oracle from 95.7% to 97.6% of the same session's sum. It's one
  search per backend, and the fall is no larger than the difference between two
  alone searches (S1 vs S6: 1.3–3.8% per backend), though all four fell.
- **Most of the rest appears only through the LB.** The same shares without the LB
  reach 99–101%; the oracle through the LB is 3.3% below that, and the two runs of
  each don't overlap. Without the LB requests also arrive more evenly: the CV of each
  backend's requests per second is 1.6–4.3%, against 4.9–8.6% through it. That fits
  uneven arrival but doesn't prove it. The no-LB case differs in several ways at
  once (a choice per request instead of per connection, no bucket hashing, no
  encapsulation, no director or redirector work), and we didn't separate them.
- These gaps are all under the ~5% limit, so read them as direction, not size.
- Our earlier figures for that variation were wrong: per backend, the CV is
  4.4–12.5% across S3 and S4 (from 5 s to the disturbance; not 5–8%) and 11.4–12.8%
  in S1's noise run at 80% load (not 12–13%).
- **100% isn't a ceiling.** The SLO judges the mix, so one backend can run over its
  own 20 ms: at 6292 req/s without the LB, beP1's p99 was 24.6 ms and the mix's 19.9.
  That may be how one CPU table reached 100.1%; we can't see per-backend latency
  through the LB. The generator wasn't the limit (over S6's 100 probes: worst dispatch
  lag p99 0.002 ms, send delay p99 0.14 ms, none dropped).
- *Can't claim:* differences under ~5% (e.g. CPU vs oracle or vs total RIF); that the
  oracle's split is the best one; capacities under *changing* capacity (we measured p99 at a
  fixed load there, RQ2).

## RQ4: do connections survive table changes?

- **Move test** (before the campaign; 1 run with the XDP director, 1 with
  TC): a change of 50% of the buckets broke **0 of 300** long-lived
  connections with second hops and **130–146 of 300** without; with the
  SYN-cookie check off, 632–671 of ~5,950 new connections broke. Counting
  broken connections as distinct client sockets that received a reset
  matched the load generator's errors exactly.
- **Live runs (S3–S5), redirector forwarding:** 0 long-lived connections
  broken and 0 backend resets in **34 runs** (26 with the controller moving
  buckets, 8 static), 100 long-lived connections each, 160–240 s per run.
  **These predate the startup fix** (D40): the controller could publish its first
  table at the moment the load started, and a connection opened just before could
  have been stranded. None was, but the 0 doesn't show that the race can't happen.
- **On the final code (S7, after the startup fix): 0 long-lived connections broken and
  0 backend resets in 4 runs** (antagonist scenario, 2 CPU and 2 total RIF in ABBA
  order, 100 long-lived connections each, 240 s). That's 0 of 400 connections, through
  6,233 bucket moves, all swaps, with 4.5 M short-lived requests and 0 errors. It's one
  scenario, not a repeat of the 34, and with 0 of 400 the 95% upper bound is about 0.75%
  per run. It also doesn't force the startup race: the first table is probably published
  before the load generators connect, so the unit test is what covers that logic. S6's
  six table runs (0 errors, 420,344 short-lived requests each) and 4 hindsight runs (long-lived
  connections, 0 moves, nothing broken) point the same way.
- **Same scenario with the redirectors not forwarding** (they still
  decapsulate; **1 run**, CPU controller, antagonist): **36 long-lived
  connections broke** (36 errors, 36 reconnects), plus 160 short ones. That's
  196 failed requests, in a different run from the 34 above; the reset monitor saw 197 client sockets reset, and the
  backends' own counters show 395 resets sent (the monitor's tracepoint
  counted 391). All of its 1359 moves were swaps. So a swap is safe only
  because the redirector forwards: after a swap the new first hop has no
  socket for the bucket's older connections and passes their packets to the
  old first hop, now the second hop. Without that, every swap strands them.
- **A property of the design:** of the 74,928 moves the controller made in the
  34 runs with forwarding, **74,899 were swaps** and 29 were reassignments
  (two RIF step runs, under the dwell rule). Every bucket starts with a
  rendezvous second hop (D32), so the controller can nearly always move load
  by exchanging a bucket's two hops, a move that keeps both backends in the
  bucket. Swaps can't strand a connection, at any rate and without any dwell.
  The dwell only has to protect the rare reassignment. The "no protection"
  run (no dwell, no caps, gain 1; 1 run) made 16,311 moves, all swaps, and
  broke nothing for that reason.
- **Simulator** (`results/sim-20261102-033415-*`): 0 re-changes within the
  lifetime bound and 0 broken connections in ~173 M simulated connections per
  noise level (22 scenario × signal cases, 5 seeds, at 2% and 10% signal
  noise), with the dwell. Without any protection: 99k–467k re-changes and
  27–6,711 broken connections, so the checker does fire.
- *Can claim:* second hops keep connections alive through big changes and
  through every swap; without forwarding, swaps break connections live; the
  SYN-cookie check is necessary even without overload; with rendezvous second
  hops the controller nearly only needs swaps.
- *Can't claim:* live breakage rates for reassignment-heavy changes (e.g.
  draining a backend while connections outlive the dwell), i.e. H7's second
  half; only the simulator and the move test cover those. The redirector
  contrast is one run.

## RQ2: when does the dynamic controller do better than a static table?

At 75% load (S3–S6). Recovery = back within the SLO for 15 s. Cells: recovery;
share of 5-s windows over the SLO during the disturbance; late p99. Ranges over
the runs, number of runs in brackets:

| disturbance | oracle: table for before the disturbance (1) | hindsight: table for during it (2, S6) | CPU controller (3) | RIF per core (3) | total RIF (2, S5) |
|---|---|---|---|---|---|
| step: beP2 needs 2× CPU per request, for good | collapses: 12% errors, late p99 ~17 s | 0 s; 0–4%; 14.5–15.4 ms | 30–35 s; 33–50%; 17.7–19.1 ms | 15–25 s; 17–29%; 17.2–18.0 ms | 40–45 s; 50%; 18.3–18.8 ms |
| antagonist 60% on beE1's cores for 120 s | never; 100%; ~206 ms | 0 s; 0–4%; 13.0–13.3 ms | 10 s; 17–21%; 15.0–16.1 ms | 5–10 s; 8–25%; 15.0–17.7 ms | 10 s; 8–12%; 13.5–15.6 ms |

**This changes what we can say.** Before S6 this section's headline was that the
controller "beat the static oracle". True, but that table is simply out of date
once the disturbance starts. **A static table that knows the disturbance does better
than every controller in both scenarios:** no transient, no moves, one window over
the SLO at most. The S3 and S5 runs are from days earlier, but S7's repeat of the
antagonist, 20 minutes after S6, gives the same picture: CPU 10 s and 12% of windows,
total RIF 5–10 s and 8%, against hindsight's 0 s and 0–4%. So the controller
isn't finding something a static table can't reach. What it gives is **not having to
know** which backend, when or how much: these tables were built from a capacity
measured beforehand, under the very disturbance they then face.

What hindsight costs, and what we didn't test:
- A table for one disturbance doesn't help with another: the step's table does
  nothing for beE1, and the other way round. Not run, it follows from the shares.
- It gives capacity away when nothing is wrong: from its shares, the step's table
  should stop near 85% of the sum and the antagonist's near 90%, against 95–97% for
  the oracle. That's computed, not measured. Before the disturbance its p99 was
  14.0–14.5 ms on the step's table against 11.4–13.4 ms for the others.
- It holds here because 75% load is under what's left: the step takes beP2 from 1656
  to 704 req/s (0.42×), so the load is 89% of the remaining sum; the antagonist takes
  beE1 from 1810 to 1188 (0.66×), 83%. A bigger disturbance would leave no table
  able to hold. Each capacity is one search, alone.
- We ran hindsight only on these two scenarios, not on flapping.

For comparison, where the controllers end up (120–180 s, exploratory): on the step,
beP2 has 555–660 buckets against the hindsight table's 546; under the antagonist
beE1 has 877–882 with total RIF, 911–929 with RIF per core, 648–707 with CPU, against
867. CPU sheds more from the hit backend, likely because the neighbour's own CPU time
counts as load (one explanation, not tested).

**Timescale sweep** (added after S3–S4): the same antagonist switching on and
off with different periods (on for half of each); share of 5-s windows over
the SLO during the disturbance, 1 run per point unless noted. The 20-s point
includes S4's flapping run. The last column is S3's single 120-s on-phase: it
counts only the on-phase, so it isn't comparable with the other columns:

| period | 5 s | 20 s | 60 s | 120 s on, once (S3) |
|---|---|---|---|---|
| oracle | 96% | 50% (2 runs: 50, 50) | 50% | 100% |
| CPU controller | 12% | 15% (2 runs: 12, 17) | 8% | 17% (3 runs) |
| total-RIF controller | 12% | 12% | 12% | 10% (2 runs) |
| RIF per core | – | 38% | – | 21% (3 runs) |

- **The controllers did better than the table set before the disturbance at every
  period we tried, down to 5 s.** No table that knows the antagonist was run here.
- **H3: first half consistent, second half untested.** H3's threshold is the control
  period (1–2 s) and the connection lifetime. The mean lifetime is about 2.2 s here
  (512 connections, 235 opened per second at 75% load; the campaign's estimate was
  0.5–2 s). The fastest on-phase is 2.5 s, so every point lies above the threshold,
  the fastest only just. So we never tested a disturbance that H3 says nobody can
  follow. It would take a shorter on-phase or longer connections.
- At 5 s the controller doesn't follow each flap (load only moves as connections turn
  over): with CPU, beE1's buckets drop from 1190 to a band of 862–1011 and stay there
  (total RIF: 1150 to 864–986), which keeps the on-phases within the SLO. At 60 s it
  follows the flaps (beE1 between 714 and 1023 buckets). The oracle is worst at 5 s
  because every 5-s window contains an on-phase.
- After recovering, late p99 is 13.5–19.1 ms, close to the 20-ms SLO. After the
  step beP2 has lost 952 req/s (measured in S6; we had assumed half, 828), so the
  same load is 89% of what's left. The hindsight table sits lower (14.5–15.4 ms), so
  part of that is the controller's table and not only the load.
- **Stability: CPU settles, the RIF controllers keep hunting.** From 60 to
  120 s after the step, the CPU controller made 0–143 moves/min and beP2's
  bucket count changed direction at most twice. RIF per core made 1279–1929
  moves/min with 7–11 direction reversals (beP2 swinging over 110–180 of 4096
  buckets); total RIF 756–1046 moves/min with 2–7 reversals. p99 stayed
  within the SLO and every move was a swap, so nothing broke, but it is the
  feedback-loop behaviour Unimog warns about. The tuning simulator predicted
  ≤ 40 moves/min in steady state for every signal. Live, in the 30 s before any
  disturbance, the controllers made 0–440 moves/min in S3 and S4, and more in S5's
  flapping runs: 282–340 at 5 s, 520–726 at 20 s, 336–400 at 60 s (and 4000 in the
  no-protection run). So the simulator underestimates live noise.
- An aggressive controller (no dwell, no caps, gain 1; 1 run) recovered as
  fast (10 s) but spent more windows over the SLO (38% vs 17–21%) with 8–11×
  the moves: H3's "may hurt", weakly (1 run).
- *Can claim:* a controller does much better than a table set for the capacities
  before the disturbance, at every timescale tried. *Can't claim:* that it does better
  than a static table in general (hindsight beats it where measured); a crossover
  timescale (nothing below 5 s or under H3's threshold was tried); behaviour at other
  loads; anything from the single-run sweep points beyond large differences (oracle
  50–96% vs controllers 8–17%).

## RQ3: which signal works best?

| | whole-core CPU | RIF per core | RIF total | cgroup CPU | PSI |
|---|---|---|---|---|---|
| sees request load | yes (saturates at 1) | yes (unbounded) | yes | yes | **only if the server queues in the kernel** (1 run per mode) |
| sees a neighbour in another cgroup (H5; 1 run) | 1.32× | 33× | (same counter) | **0.91×: blind** | 3.2× |
| sees a cache-thrashing neighbour on the sibling hyperthreads (H6; 1 run) | 1.10× | 1.25× | (same counter) | 1.09× | 0.98× |
| static capacity of its table (S2) | ≥ 92.8% (2) | 86.6% (2) | ≥ 92.8% (3) | — | — |
| same, interleaved (S6) | 92.7% (3) | — | 90.9% (3) | — | — |
| step recovery | 30–35 s (3) | 15–25 s (3) | 40–45 s (2) | — | — |
| antagonist recovery (late p99) | 10 s (15.0–16.1 ms) (3) | 5–10 s (15.0–17.7) (3) | 10 s (13.5–15.6) (2) | never (85 ms) (1) | — |
| bucket moves per antagonist run | 1425–2158 | 2675–3751 | 1241–1364 | 787 | — |
| bucket moves per step run | 1010–1170 | 3812–4228 | 2287–2861 | — | — |

- **CPU vs RIF:** with RIF counted per server, the way Prequal counts it
  (total RIF here), the two are close: no static-capacity difference we can resolve
  (S2 across sessions, S6 interleaved), same antagonist recovery, similar sweep. Total RIF recovers more slowly from the step
  (40–45 s in 2 runs vs 30–35 s in 3) and moves 2–3× as many buckets there,
  slightly fewer under the antagonist, and keeps hunting after the step
  (RQ2). Its late p99 under the antagonist (13.5–15.6 vs 15.0–16.1 ms) is not
  a difference 2–3 runs can resolve.
- **RIF per core** was fastest on the step (15–25 s, 3 runs), but lost 6
  points of static capacity, moves about 2× (antagonist) to 3.5× (step) as
  many buckets as CPU, and hunts the most.
- **H4 not supported; the explanation came after the data.** At the same
  utilisation a 4-core backend has a shorter queue per core, so per-core RIF
  makes the 4 slow E-cores look lighter than they are, but their 1.85× slower
  service makes them miss the latency target first. The per-core RIF table
  lost 6 points to CPU and total RIF (ranges don't overlap). Dividing by cores
  was our choice (D10); Prequal uses the plain count per replica. H4 expected
  CPU to lose to RIF on heterogeneous cores; with Prequal's definition they're
  alike here, with ours RIF loses.
- **Not a test of Prequal.** Prequal picks a replica per request from fresh
  probes of RIF *and* latency, and at YouTube it halved tail latency compared
  with CPU-balancing weighted round robin (which its authors call "a
  formidable competitor"). Our controller moves buckets of *new connections*
  every 1–2 s using RIF alone, on 4 backends of one machine. That it only
  ties with CPU here says nothing against Prequal's result.
- **H5 supported** (1 run): cgroup CPU does not see a CPU-heavy neighbour in
  another cgroup (it even drops, 0.91×), and a controller using it never moved
  load away (1 run, p99 ~85 ms for the whole 120 s). Whole-core CPU, PSI and
  RIF all see it. Don't compare the sizes of the ratios: they come from the static
  run, where the backend is overloaded and its queue keeps growing, so RIF's 33× is
  a queue running away, while CPU can't exceed 1 (0.76 to 1.0).
- **PSI (D25), 1 run per backend mode:** with our pool backend PSI stays
  ≤ 0.14 and even *falls* under request overload (0.05 at 130%), because
  requests wait in our process. With the kernel-queue backend it rises to
  0.68 at 90% and 0.99 at 130%. Both modes see the antagonist (0.33 / 0.45).
  So PSI's usefulness depends on where the server queues.
- **H6 not supported** (1 run each). The neighbour planned for S4 (our
  request loop on the idle hyperthread siblings) didn't contend at all
  (signals 0.96–0.97×): both threads wait on their own dependency chains and
  read the same table. After seeing that, we built one that does: a neighbour
  walking a 64 MiB buffer, evicting the shared L1/L2 caches. With it on beP1's
  siblings, whole-core CPU (1.10×) and cgroup CPU (1.09×) rose by about 10%, because
  they measure time, not work. RIF rose most (1.25×), PSI not at all (0.98×). p99
  barely moved (10.7 → 11.0 ms) at beP1's ~63% utilisation. So H6's "only RIF
  notices" is wrong.
- **But no controller would have acted on it.** Every rise is inside the signal's
  dead band: 17% for CPU, 16% for cgroup, 57% for RIF per core, 48% for total RIF.
  "Notices" here means the number moved in a log, in one run, not that the system
  would respond.
- *Can't claim:* rankings of cgroup CPU and PSI as *controller* signals (one
  run, or none); differences between CPU and total RIF beyond the step
  recovery and moves (2–3 runs); H6 at higher load, where a 10% slowdown would
  matter.

## The hook (D35)

CPU time per packet on the router/LB CPU, director at TC vs XDP on veth, **1 run of
20 s per hook and rate**: TC 4439 / 3621 ns, XDP 5121 / 4413 ns at 2000 / 4000 req/s.
That looks like XDP costing 15–22% more, but the measurement is coarse: it's **39 vs
45 and 64 vs 78 busy ticks of 10 ms**, and those ticks include the router's forwarding
and the bridge, not only the director. So: no sign that TC costs more here, and
probably a little less. It fits how veth works (even "native" XDP starts from the
kernel's packet buffer and builds a new one after XDP_TX, while TC keeps the buffer),
and it's enough to back running the LB in TC on our hardware. It says nothing about
NICs with real native XDP, where the XDP paper measures a 5× advantage for XDP.

## Real laptops

**One run of `bench/cluster.py --quick` on 2026-11-06:** [results/cluster-20261106-085033/](../results/cluster-20261106-085033/summary.md),
~32 min, with its `cluster.conf` and `run.log`. Client i7, LB a thin i5, h1 and h2 i5
(Arch, Ubuntu), over a phone hotspot. A request costs 40 ms, SLO p99 ≤ 800 ms, 32 connections.
**1 run per configuration and 1 search per backend, so nothing here measures its own
repeatability**, and the configurations ran in sequence, not interleaved.

- **Not comparable in one table with the single-laptop numbers.** Request cost, SLO,
  connections, network, tuned settings and the antagonist's length (90 s) all differ.
- **The hardware was close to identical:** 2 cores per backend, all of one kind. By-cores
  is the same as equal here, so H1 isn't tested. The capacity differences come from
  beP1's artificial 1.6× slowdown and from h2 running slower with `tune.sh` applied (below).
  This run says little about really different machines.
- Power profiles weren't set: `tune.sh` warned of power-saver on the LB, unknown on h1,
  balanced on h2, and they were left. The cluster has no record of the LB's CPU or of airtime.

| backend | machine | capacity (req/s) | theoretical | % |
|---|---|---|---|---|
| beP1 (slowed 1.6×) | h1 | 28 | 31 | 91% |
| beP2 | h1 | 48 | 50 | 97% |
| beE1 | h2 | 34 | 50 | 69% |
| beE2 | h2 | 34 | 50 | 69% |

| table | highest pass (req/s) | next failure | usable fraction of 146 | spacing of those probes |
|---|---|---|---|---|
| oracle | 134 | 137 | 91.8% | 2.4% |
| CPU controller | 128 | 131 | 87.8% | 2.3% |
| equal | 116 | 149 | between 80% and 102% | 27.7% |
| total RIF controller (the same table as equal) | 116 | 149 | between 80% and 102% | 27.7% |

| antagonist on beE1, 30–120 s, 109 req/s | moves | beE1's buckets at 120 s | late p99 | last window's p99 (145–150 s) | requests never sent / other errors | long-lived broken (of 20) | backend RSTs | long-lived p99 |
|---|---|---|---|---|---|---|---|---|
| oracle | 0 | 968 | 12.9 s | 5.6 s | 281 / 0 | 0 | 0 | 1.4 s |
| CPU | 1223 | 613 | 2.9 s | 0.7 s | 0 / 1 | 0 | 0 | 1.4 s |
| total RIF | 511 | 568 | 10.0 s | 5.3 s | 248 / 1 | 0 | 1 | 1.2 s |
| CPU, no forwarding | 1409 | 640 | 5.4 s | 4.9 s | 200 / 19 | **12** | 34 | 1.3 s |

### What it shows

- **The second hop works over a real network, across machines (RQ4).** With forwarding on, 0
  of 20 long-lived connections broke, through 1223 and 511 bucket moves; with it off, 12 of 20
  through 1409, with 34 backend RSTs against 0 and 1. Same direction as on the single laptop.
- **The CPU controller found a table close to the oracle's without being told the capacities.**
  From equal, in 733 moves, it reached shares within 1.7 points of the oracle's (19/32/24/25%
  vs 19/33/24/24%).
- **In this one antagonist run the CPU controller did better than the static table.** It took
  beE1 from 968 to 613 buckets, and p99 was falling when the antagonist ended (6.7 s at its peak,
  1.5 s in the antagonist's last window), and back under 800 ms by 130–135 s. The static table's p99 climbed
  to 14 s and was still 5.6 s at the end of the run. Total RIF moved about as many buckets off
  beE1 but its p99 stayed near 10 s until the antagonist ended.
- **Signals:** beE1's in the static run, during vs before: cgroup CPU 1.23×, whole-core CPU
  1.98× (it saturates at 1), RIF 15×, PSI 30×. Same direction as H5, 1 run.

### What it doesn't show

- **No ranking of the capacities.** Oracle vs CPU is 4 points, and the noise is larger: equal
  and total RIF are one table measured twice (RIF's band is so wide that it made 0 moves in S2,
  so it stayed at 1024 buckets each). Same table, same rates: p99 558/551/548/527 ms vs
  406/435/444/681 ms, and at 149 req/s 2398 ms with no errors vs 7611 ms with 26% errors.
  Equal's "≥ 80%" is only a lower bound, since the next probe was at 102%, so equal can't be
  placed below the oracle either. It also says nothing about RIF as a signal in S2.
- **The search resolution in `summary.md` needs reading with care.** "Typically 15%" is the median of
  two clusters, ~2.4% and 27.7%. The 58.1% is 2× the coarsest standalone search (beP2's 29%),
  the limit on differences between backends, not a resolution of these tables. And 2.4% is
  only how close the probes were, far finer than the noise above.
- **No recovery.** No configuration got back under 800 ms for 3 windows while the antagonist was
  on. CPU and total RIF had no window under 800 ms in those 90 s; the oracle had 2 and no
  forwarding 1, all right after the onset, before the backlog built. The antagonist lasted only
  90 s and CPU was still improving, and no table was measured for the disturbed capacities, so we
  don't know whether recovery was possible.
- **The multi-second p99s aren't only time in the cluster.** Latency counts from the scheduled
  time, and most of it was waiting for one of the 32 connections (oracle: p99 13.3 s, of
  which send delay 12.8 s). The long-lived connections, on their own pool, had a p99 of 1.2–1.4
  s over the whole run in all four configurations. The ranking might change with a bigger pool; we
  didn't test that.
- **The errors are mostly a backlog, not failures.** 281 of the oracle's 281, 248 of RIF's 249 and
  200 of the 219 without forwarding were never sent: still queued in the generator when the run
  ended. Failures once sent: 0, 1 (RIF), 1 (CPU), 19 (no forwarding).
- Client-side reset counts of 4–7 appear in the three runs with forwarding, including the oracle
  with no moves (31 without forwarding). We don't know where those few come from.

### Checks

- **Send delay warnings.** 13 passing S2 probes are flagged. They don't make a probe pass wrongly:
  latency counts from the scheduled time, so the delay is inside the p99. Rates were met (within
  2.5%, the same in both runs of a rate, so the Poisson draw) and the generator was healthy (no
  drops, no dispatch lag). 9 of them are ≤ 78 ms. The other 4, next to capacity (oracle at 127
  and 134 req/s, CPU at 125 and 128), are 187–407 ms: the pool was filling up. So the oracle's and
  CPU's capacities may be somewhat understated, and don't depend on a lucky send.
- **Is the limit wifi or the backends? At 40 ms, it looks like the backends.** A rough M/M/2 per
  backend, with the oracle's shares, each machine's measured speed and no network at all, gives a
  p99 of ~860 ms at 134 req/s and ~1000 ms at 137 (measured 695 and 1515), and ~26–31 requests in
  flight, which is also why the 32-connection pool fills at the same point. The backends were at
  65–82% CPU at 116 req/s. The same hotspot had carried 147 req/s at p99 332 ms in the clean dry run
  (4-s probes) and 278 req/s with no errors in the first. **Wifi shows up as noise** (above; all
  signals are noisier than on the laptop) and in latency: a hand ping LB to backends of ~140 ms,
  up to 362, at idle, not in the data. Under load p50 through the LB at 99 req/s was only 95 ms. At
  20 ms, the first dry run failed well under the sum of 317 (equal passed 190 and failed 208; oracle
  passed 167 and failed 245). The hotspot may be that limit, but the LB's CPU and the pool of 64
  weren't ruled out.
- **Why h2's backends reached 69% and h1's 91–97%: h2's CPUs did less work per busy second.**
  Calibration runs only on the first backend (on h1), so the theoretical 50 req/s assumes h1's speed
  everywhere. From each backend's own counters, h2 does 83–89 thousand work units per busy core-ms and
  h1 113–123, a ratio of ~0.72, which matches 34 vs 48 (0.71). At h2's own speed its theoretical
  is ~37–38 req/s and it reached 90–94%, like h1. It isn't the network or the LB. The gap appeared only
  with `tune.sh` applied: in the dry runs (turbo on) h2 kept up with h1, within their coarse searches.
  **Why the fixed clock differs, we don't know:** different base clocks or the different power
  profiles, say. Nothing recorded separates them; `lscpu` and `tune.sh status` on h1 and h2 would.
- **Tuning.** Whole-core CPU: P 2 s, band 0.515/0.258. RIF: P 8 s, band 1.819/0.910, and no setting met
  the churn cap. RIF's σ is 0.61–0.73 (single laptop 0.09–0.19). CPU's also rose, to 0.20–0.27 (from
  0.03–0.07), so it isn't only RIF. We haven't shown wifi is the cause: RIF averages just 1–3 per
  backend here, and small counts are noisy anyway.
