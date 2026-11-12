# Journal

What we did, what we found, what surprised us. Newest entries at the bottom.
Decisions that came out of an entry are recorded in [decisions.md](decisions.md).

---

## 2026-11-01 — Phase 0: repo, testbed, backend, and making measurements trustworthy

**Built:** repo layout and docs; `scripts/topology.sh` (namespaces, bridge,
DSR settings, RPS placement); `scripts/tune.sh` (runtime-only tweaks + exact
undo); `cmd/backend`, `cmd/loadgen`, `cmd/antagonist`; `bench/` (sysmon,
noise check, service-time check). Papers and blog posts copied to `references/`.

**Checks that passed first time**
- `topology.sh check`: all backends reachable; ping traffic processed on the
  loadgen CPU, router CPU 6 and backend CPU 8, as planned with RPS.
- `tune.sh apply` then `undo` restores every value exactly (diffed: governor,
  EPP, no_turbo, AllowedCPUs of user/system slices, 47 IRQ affinities, idle
  states, min frequencies). A second `apply` is refused. The noise check's
  cleanup also ran correctly when a run failed half-way.

**Problems found, in the order we found them.** Each one would have
silently distorted results; together they are the reason phase 0 exists.

1. *Calibration was 40× off.* `calibrate()` ignored `burn()`'s result and the
   compiler deleted the loop. Fixed with a package-level sink.
2. *Coupling check measured the wrong thing.* Background load generators ran
   on CPU 6, which is also where the router processes every request, so
   "beP1 loses 14% when others are busy" partly meant "router CPU busy".
   Replaced by CPU antagonists on the other backends' cores (D21).
3. *Requests were slower at low load* (P: 1.92 ms at 20 req/s vs 1.21 ms at
   200; E: 3.82 vs 1.86 ms; expected 1.0 / 1.85). Idle cores dropped into deep
   sleep (this laptop's C3 takes 1048 µs to wake from) and lost their caches.
   Fix: deep idle states off and fixed clock on experiment CPUs (D20).
   E became flat at 1.85 ms.
4. *The load generator added ~0.5 ms to every request.* Go timers wake up to
   ~1 ms late, and latency is measured from the scheduled time. Fix: sleep
   until 2 ms before, then spin (D22). Send delay p50: 0.5 ms → 0.01 ms.
5. *P backend had a huge tail at only 50% load* (p99 8–15 ms, while E with
   2 cores had 6 ms). Not the clock (steady 2200 MHz under load, no throttling),
   not C1/C1E (disabling C1 barely helped), not calibration (it matches the
   backend's real per-request time under load). A service-time histogram in
   the backend showed ~10% of P jobs taking 1.5–3 ms instead of ~1.05 ms: the
   kernel sometimes ran both worker threads on one CPU. Fix: one worker
   thread pinned per CPU (D24). Slow jobs 10% → 0.3%; p99 at 50% → 3.7 ms.
6. *Remaining, and kept:* P backends are still slower at very low load
   (1.75 ms at 1% load, 1.06 ms at 40%). Requests alternate between two
   P-cores with *private* L2 caches, so the 1 MiB working set goes cold; on a
   single P-core the effect almost vanishes, and E clusters (shared L2)
   don't have it. Real hardware behaviour, fades near capacity (D23).

**Numbers worth remembering**
- Turbo off: P-core 2200 MHz, E-core 1600 MHz, steady under load.
- One request (fixed work) takes **1.84×** as long on an E-core as on a P-core,
  so a 4-E-core backend (~2170 req/s theoretical) is worth *more* than a
  2-P-core backend (2000 req/s), not 2× more as core counts suggest. Supports H1.
- Package power 13–17 W, ~72 °C during runs; no thermal-throttle events.

**Next:** quick noise check with all fixes, then the full noise check (~2 h).

---

## 2026-11-02 — Full noise check: PASS, phase 0 done

Run `results/noise-20261101-225947/` (1 h 50 min, settings `FULL` in
`bench/noise_check.py`). Tweaks applied and undone by the script; afterwards
`tune.sh status` showed everything restored and no `cs331-*` units left.

| Check | Result | Limit |
|---|---|---|
| p99 CV at 80% load, run to run (beP1, n=10) | 1.1% (4.83 ± 0.05 ms) | ≤ 10% |
| p99 CV at 50% load (n=10) | 0.6% (3.31 ± 0.02 ms) | — |
| Capacity CV, beP1 alone (n=5) | 0.8% (1630 ± 12 req/s) | ≤ 3% |
| Capacity CV, beE1 alone (n=5) | 0.3% (1771 ± 5 req/s) | ≤ 3% |
| beP1 capacity with the other backends at 70% (n=3) | −0.2% | within 2× CV |
| Thermal throttle events | 0 | 0 |
| Generator: dispatch lag p99 (worst run) / dropped | 0.002 ms / 0 | — |

- **Smallest difference between policies we can claim: ~1.5%** (2× the larger
  capacity CV). The usable-capacity differences H1 predicts are much larger
  (static-by-cores vs oracle should differ by tens of percent), so the testbed
  is precise enough for RQ1.
- **Usable capacity per backend:** P 1630 req/s = 82% of theoretical
  (2000), E 1771 = 82% of theoretical (2170). Same fraction, so the
  SLO costs both kinds of backend the same headroom; the E backend is worth
  1.09× a P backend, as calibration predicted (H1).
- **Neighbours don't matter** (−0.2%), so single-backend capacities can serve
  as the oracle's denominators without loading all backends at once.
- Within one run, p99 per 10 s window varies more at 80% load (CV 8.9%) than
  at 50% (2.5%). Expected near capacity (queueing amplifies bursts); this
  is why every point is a 40–60 s measurement and why we repeat runs.

**Quick vs full run disagree by more than the noise.** The quick run (10 s
probes, n=1) gave P 1680 / E 1866 req/s; the full run (40 s probes) gave
1630 / 1771, i.e. 3–5% lower, while the full run's own CV is < 1%. Likely
cause: a 10 s probe has fewer requests at the tail and fewer chances to hit a
backlog, so its p99 is optimistic and the search ends higher. We didn't test
this separately. Rule added to the protocol: capacities are only compared
with identical probe settings, in the same session, interleaved.

**False alarm: "P cores at 2010 MHz".** The summary first reported the mean of
`scaling_cur_freq` (2010 MHz on P cores, which are pinned at 2200), and
~16% of samples read below 2100, some as low as 800. Cross-check (60 s,
tweaks applied, antagonist at 60% on CPUs 8, 10, 16; script in the session
scratchpad, not kept): the hardware cycle counters (APERF/MPERF) showed the P-core clock
constant to ±0.2% for the whole minute, while `scaling_cur_freq` jumped to
800–825 MHz. The kernel's value is a per-tick estimate and is unreliable
on cores that go in and out of idle. The analysis now reports the median
(exactly 2200 / 1600 MHz). The tiny p99 and capacity CVs agree:
a core that really dropped to 800 MHz for seconds would show up there.

Mishap during that cross-check: the first attempt used `pkill -f bin/antagonist`
in a trap, which also matched the `sudo bash -c` running the trap, so the
undo never ran. Caught immediately via `tune.sh status`, undone by hand.
Lessons: stop experiment processes by systemd unit name (as the bench scripts
already do), never `pkill -f`; and under `tune.sh apply`, `taskset` can't
place anything on experiment CPUs (the user slice is confined to 0–1), so use
`systemd-run --slice cs331.slice`.

**Small fixes:** `summary.md` was left root-owned (the `chown` ran before the
analysis wrote it). Fixed in `noise_check.py`.

**Phase 0 exit check met.** Next: phase 1 (XDP LB with a static table,
encapsulation, TC redirector, golden-packet tests). Before phase 2: fix the
workload model and choose L values (open items in plan.md).

---

## 2026-11-02 — Phase 1, part one: director, encapsulation, loader, tests

**Built:** `bpf/director.c` (XDP LB), `bpf/decap.c` (minimal backend-side
decap; the redirector replaces it in part two), `bpf/xdp_pass.c` (peer of
the LB's veth), shared `bpf/lb.h`; `internal/lb` (Go mirror of `lb.h`,
loading, table publishing, counters); `cmd/lbctl`; `bench/e2e_lb.py`;
`make`, `make test`, `make test-bpf`. Details in D26–D28.

**Golden-packet tests** (`make test-bpf`, 7 tests in the kernel via
`BPF_PROG_TEST_RUN`) all pass. The main one checks a SYN's
encapsulation byte by byte against bytes computed by a separate Python
implementation of the hash and checksums. A mismatch is reported per field
(`udp.sport`, `hdr.bucket`, ...); the only failure while writing them was in
the test itself (a MAC written as 0x11 instead of 0x0b), and the per-field
report pointed straight at `eth.dst`. 2000 random flows confirm the C hash and
the Go mirror pick the same bucket and hops.

**End to end** (`sudo python3 bench/e2e_lb.py`, all checks pass):
- native XDP mode; without a table VIP traffic is dropped and counted;
- equal table: 24.7 / 25.3 / 25.9 / 24.1% of ~4000 fresh connections; 
  `beP1=1`: 100% to beP1; `beP1=1,beE1=3`: 24.4 / 75.6%. Zero errors;
- the LB encapsulated 79k packets and received only 4 others, so replies
  bypass it (direct server return).

**First try failed:** every SYN was encapsulated and reached the backend, and
the backend counted it as a checksum error (`TcpInCsumErrors`). The
client's veth leaves TCP checksums to "offload" and the mark saying so doesn't
survive XDP_TX. Fixed on the client (D26). Also seen: right after `up`,
the bridge doesn't know the backends' MACs yet and floods the first
encapsulated frames to all backends. They drop frames that aren't addressed to
them, so this is harmless.

**Next (part two):** TC redirector (socket lookup, second hop, `F_HOPPED`,
syncookie check) replacing `decap.c`; reset counter (tracepoint); table
generator with second hops; controller offline + simulator.

---

## 2026-11-02 — Phase 1, part two: redirector, SYN cookies, reset monitor, move test

**Built:** `bpf/redirector.c` (replaces `decap.c`), `bpf/resets.c` +
`cmd/resetmon`, second hops in `lbctl table` (`-second previous|none`),
`lbctl dump`, `bench/move_test.py`. Director fix: inner length from
`tot_len`, Ethernet padding trimmed (review comment: a bare ACK is padded
from 54 to 60 bytes on real NICs). Details in D29–D31.

**Golden tests** (`make test-bpf`): 15 tests pass. New: a padded bare ACK
loses its padding; `tot_len` beyond the frame is dropped; the redirector
forwards byte-exactly (outer addresses, checksum, `F_HOPPED`). The
redirector's keep/forward cases use real loopback sockets, because the socket
lookup runs in the test's own namespace: the same ACK is kept with an
established socket and forwarded when only the listener matches.
The SYN-cookie path needs a real handshake, so the move test covers it.

**Move test** (`results/move-20261102-014359`). 300 long-lived connections (a request every
~100 ms) + 200 new connections/s; beE1 with `tcp_syncookies=2`; after 12 s
the table switches from equal to "no beP1", so 2047 of 4096 buckets (50%) get a
new first hop.

| Run | long-lived broken (of 300) | churn broken (of ~6000) | backend RSTs | in moved buckets |
|---|---|---|---|---|
| A: second hops | 0 | 0 | 0 | — |
| B: no second hops | 146 | 0 | 192 | 192 |
| C: second hops, no SYN-cookie check | 0 | 671 | 1290 | 1290 |

- A: the redirectors forwarded ~55k packets of old connections to their old
  backends. beE1 kept 598 handshake-completing ACKs because of a valid SYN
  cookie; without the check those would have broken.
- B: about half of the long-lived connections broke (50% of buckets moved). Every
  backend reset is in a moved bucket.
- C: long-lived connections survive, but new connections to beE1 in buckets
  that moved *to* it break: their final handshake ACK finds only beE1's
  listener, gets forwarded to the old first hop, which resets it. Cross-check:
  beE1 sent 1971 cookies, got 1300 back. The missing 671 are exactly the
  671 broken connections. This is the failure D8 predicted, and it happens with no
  overload at all, just cookies plus a second hop.

**Bug found: the first reset monitor missed the important resets.** It
used the classic `tcp_send_reset` tracepoint. When a packet hits only the
listener, that tracepoint reports the *listener's* addresses ([::]:8080,
IPv6, because Go listens dual-stack), and we filtered IPv6 out. In the first
run it reported "0 server resets" while the namespaces' `TcpOutRsts` said
205 and 1214. Fixed by reading the 4-tuple from the packet that triggered the
reset (typed tracepoint, D31). Now it agrees exactly with `TcpOutRsts`.

**Surprise: broken connections cause resets *from the client* later** (run
B: 84–123 per run). We investigated with packet captures and counters: each broken connection
leaves an orphaned socket on its old backend, which still thinks the
connection is open. When it next sends something, the client (no socket any
more) answers with a reset, which goes through the LB to the *new* first hop,
so the old backend never learns and keeps trying. It sends again either
- after 15 s idle: a TCP keepalive probe (Go turns keepalive on for accepted
  connections, 15 s). Run B: 77 + 51 + 18 = 146 probes = one per broken
  connection, from exactly the backends that lost buckets; or
- after 200 ms, doubling: a retransmission, when the client's ACK of the
  last response was already redirected (captured: `seq 5093:5227` resent at
  +0.2, 0.4, 0.8, 1.6, 3.3 s, each answered with a reset).
Consequences: count broken connections as *distinct client sockets that
received a reset*, not as total resets. And breaking a connection also costs
the old backend a socket for minutes. My first guess (all retransmissions)
was wrong: the counters showed only 14 retransmissions for 123 client resets.

**Next:** controller offline (table generator with rendezvous second hops,
signal plugins, simulator), then phase 2.

---

## 2026-11-02 — Controller offline: table generator, update rule, signals, simulator

**Built:** `internal/controller`, `internal/signals`, `internal/sim`,
`cmd/ctlsim`, `cmd/controller`, `bench/controller_smoke.py` (D32–D34).
Simulator results: `results/sim-20261102-021822-noise2` (2% signal noise) and
`-noise10`; live smoke test: `results/controller-smoke-20261102-021221`.

**Simulator setup:** beP1 2 cores 1000 req/s (slowed P), beP2 2 cores 1600,
beE1/beE2 4 cores 1760; connections live 2 s on average (exponential), 1
request/s each; period 2 s. Every case has 5 seeds, medians shown.

**Safety (the property we wanted by design):** 0 first-hop changes within the
20 s lifetime bound and 0 broken connections in every scenario, ~50 M
connections in total, including heavy-tailed lifetimes (Pareto, 0.6% outlive
the dwell). Control run with all protections off (no dwell, no caps, gain 1,
flapping antagonist): 99k–288k re-changes and 27–824 broken connections, so
the checker does fire. The guarantee comes from the "settled buckets only" rule.
In practice the least-recently-changed ordering and the 2%-per-period cap
already make re-changes rare; the dwell turns "rare" into "impossible".

**Convergence (2% noise):** the true and CPU signals bring the imbalance
within the noise floor in 22 s (static), 34 s after an antagonist halves
beE1, and 26 s from an equal table that overloads beP1 (ρ 1.22). No
sustained oscillation: 0–60 moves/min once settled.

**Does CPU saturation hurt?** With a big overload, not noticeably: CPU is
exactly as fast as the true signal (34 vs 34 s, 26 vs 26 s); the error is
compressed but still large. Near capacity it does:
| scenario | true | cpu | cpu + override |
|---|---|---|---|
| 90% load, beE1 −12% (reach) | 14 s | 35 s | 13 s |
| 95% load, beE1 −10%: max ρ at the end | 1.02 | **1.07** (p99 26 s) | 1.00 |
But the true signal also ends over capacity at 95%: the main cause is the
*relative* dead band (within 5% of a mean of 0.98 still allows > 100%), and
CPU saturation makes it worse (its deviation can't exceed 1/ū − 1 ≈ 2–3%).
The overload override (D32) unsticks it, but at 90% load noise alone triggers
it (92 vs 3 direction reversals). Off by default; a phase-3 decision.

**RIF:** it equalises queueing per core, not utilisation, so the 2-core
backends end at lower utilisation (ρ imbalance 1.08), but p99 is the same as
CPU's (11.3 vs 11.3 ms static, 19.1 vs 17.9 ms antagonist). Its readings are much
noisier in the model (the queue term amplifies load noise), so it never stops
moving buckets (500–1800 moves/min). At 95% load the backlog acts like an
integrator and it oscillates (max ρ 1.18). How much of this carries over
depends on real RIF noise, which we haven't measured yet.

**Live smoke test** (untuned, beP1 at half speed, 3000 req/s with churn):
both RIF and CPU controllers publish ~45 tables in 90 s, take beP1 from ~680
to ~540 buckets, 0 errors in 270k requests. But real per-period noise is
~7–15%, so the controller keeps moving ~40 buckets per period after
converging. The simulator at 10% noise reproduces this (~1100 moves/min, same
convergence time, same p99, still 0 re-changes). **Next for the controller
(phase 3):** measure per-period signal noise on a tuned run with a fixed
table, then choose period, smoothing and band with the simulator.

---

## 2026-11-02 — The LB moves to TC (D35)

The laptop ran out of battery during the controller work. The reboot also
removed the testbed and the tuning (both live in RAM only, as intended); the
uncommitted controller work was intact and is now committed.

**Why move:** none of our laptops has a NIC with native XDP, so on real
hardware the LB would always run in generic XDP: no faster than TC, but with
XDP's limits. Now a deliberate decision (D35), replacing D9.

**Port:** all director logic moved to `bpf/director.h` (`choose()`,
`write_headers()`); `director_tc.c` and `director_xdp.c` only make room and
send. Every golden test now runs against both programs with the same expected
bytes: 31 test runs pass. One limit: the kernel's test runner refuses a
truncated IPv4 frame for TC programs (it wants a full IPv4 header), so that
case is tested on XDP only; the bounds check is shared code.

**D26 isn't needed any more.** With checksum and segmentation offload back on
in the client, a capture shows 65 KB GSO packets arriving at the LB, leaving
encapsulated, reaching the backend as large encapsulated packets and being
decapsulated; 20 uploads of 1 MiB arrive complete (new `/upload` endpoint on
the backend). With XDP this would have been the D26 checksum failure, plus
GSO packets that XDP can't handle as such.

**End to end and RQ4 with TC:** e2e all passed (shares within 1 point,
0 errors, replies bypass the LB). Move test (`results/move-20261102-030314`):
A 0 broken, B 130 broken long-lived connections (= 130 keepalive probes from
orphaned sockets), C 632 broken new connections (= 1911 cookies sent − 1279
returned). Same picture as with XDP (146, 671); the differences are
which buckets the random connections land in.

---

## 2026-11-02 — Campaign redesign for the time left (D36, D37)

About 10 hours remain, so the measurement plan became a ~2.5 h campaign
([campaign.md](campaign.md)) with one tuning rule for every signal (D36) and
a fixed workload model (D37). Built for it: `backend -cost exp` (checked
locally: 50155 units/request on average for a mean of 50000; times spread
from < 0.5 ms to 5 ms), runtime slowness (`/slowness?set=2` doubled the work
per request without a restart), `work_units_total`, and `cmd/sigmon`.

**sigmon check** (untuned, 2000 req/s through the LB, 60% antagonist on
beE1's cores halfway): beE1's whole-core CPU 0.13 → 0.64, PSI 0.02 → 0.10,
RIF 0.10 → 0.15, cgroup CPU 0.10 → 0.12. As predicted (H5): cgroup CPU is
blind to a neighbour in another cgroup. PSI reacts but stays far below the
controller's low-load gate, so it is logged, not used as a controller input.

---

## 2026-11-02 — Campaign S1, tuning and S2 (RQ1 static)

Results: `results/campaign-20261102/` (`summary.md`, plots). Earlier,
invalid attempts are kept next to it (`s2-first-try`, `s2-generator-limited`,
`before-tw-fix`).

**Kernel-queue backend** (D25, for S4): `backend -mode kernel` runs 64
unpinned workers with GOMAXPROCS to match. Checked: under overload, 2
runnable threads in pool mode vs ~32 in kernel mode, on 2 CPUs.

**Dry runs found three bugs before any real run:** "served" was compared with
the target rate (Poisson counts in a short window vary, so a fully served run
failed at 96.6%); SLO 10 ms is too tight for exponential cost (an E-core
request alone has a p99 of ~8.5 ms), so L = 20 ms; and a crash when a
search found nothing.

**S1:** denominators beP1 957, beP2 1656, beE1 1810, beE2 1810 req/s (77–84%
of theoretical); repeats differ by up to 5.6%, pooled CV 2.5%. So the
smallest difference we claim is **~5%**. The noise runs were later redone
(see TIME_WAIT below).

**S2 took four runs.** Each invalid one had a different testbed cause (D38):
1. capacities all over the place (oracle 3739–5452): a probe after an
   overloaded one ran into its queue. Fixed: wait for idle, cancel abandoned
   requests. Turned out not to be the main cause (idle waits were 0.1 s);
2. still noisy: the load generator's two CPUs were saturated above ~4500
   req/s (spinning dispatcher on one). Fixed: give it its cores' siblings;
3. still single 10-s windows at ~75–130 ms with the other at ~11 ms. My
   first explanation (lumpy per-connection load) was wrong: a direct test
   showed client port selection stalling once ~40% of ports were in
   TIME_WAIT. Fixed in the `cli` namespace. This had also inflated the
   noise runs (RIF σ at 80%: 0.31 → 0.19 after the fix);
4. clean: 1 of 46 probes with uneven windows (an overloaded one), oracle
   repeats within 1.2%.

**Tuning** also needed two corrections to the simulator's setup, both made
before the live comparison and recorded in D36: theoretical capacities
(not capacities at SLO) and 20× finer connections, so the model's own noise
stays below every signal's measured noise. Chosen: CPU and cgroup CPU P = 2 s,
band 0.17/0.08; RIF P = 1 s, band 0.57/0.29 (RIF is ~3× noisier than CPU at
80% load, so it gets a ~3× wider band).

**RQ1, static (S2 fourth run):** usable capacity fraction at p99 ≤ 20 ms.
| policy | runs | usable | range |
|---|---|---|---|
| equal | 1 | 62.5% | |
| by cores | 1 | 86.7% | |
| oracle | 3 | 95.1% | 94.0–96.2% |
| CPU controller's table | 2 | 92.8% | 89.5 (lower bound)–96.0% |
| RIF controller's table | 2 | 86.6% | 84.9–88.3% |
- **H1 supported:** by cores is 8.4 points below the oracle (> 5%), equal 33 points.
- **H2 supported for CPU:** its tables are within the 5% threshold of the oracle.
- **RIF's tables lose 8.5 points** (ranges don't overlap). RIF gives the E
  backends more and beP2 less than the oracle (E 0.30–0.33 vs 0.29, beP2
  0.21–0.23 vs 0.27). At the same utilisation a 4-core backend has fewer
  requests queued per core (M/M/c), so RIF per core makes it look less
  loaded, but its cores are 1.84× slower, so its p99 reaches the SLO first.
  This is the opposite of what H4 expected for RIF. It may be a normalisation
  issue (RIF ÷ cores, D10) rather than RIF itself; not changed now.
- Even the oracle reaches 95%, not 100%: with 512 connections each backend's
  load moves 7–11% from second to second, and the p99 of the mix follows the
  busiest backend.

**Machine time so far:** about 3 h for S1, S2 (four runs) and the checks,
against 1 h planned for S1 + S2. S3 + S4 were planned at ~1.5 h.

---

## 2026-11-02 — Campaign S2-extra, S3, S4: results by research question

All in [results.md](results.md); tables and plots in
`results/campaign-20261102/summary.md`. All sessions cleaned up after
themselves; no testbed problems this time (generator fine, no uneven windows
outside overload).

**Small fixes first:** the backend counted work units when queueing (now after
the work, so skipped requests don't count); the S2 table had a stray column;
the one send-delayed S2 probe (ctl-cpu at 7048 req/s) had missed the SLO
anyway, which the summary now says.

**RIF normalisation (S2-extra):** a controller using RIF *not* divided by
cores (tuned by the same rule: P = 2 s, k = 3) found a table close to the
oracle's that reached ≥ 95.1% (lower bound) vs 86.6% for RIF per core. So the
normalisation causes RIF's S2 loss, not RIF itself. One run.

**Analysis traps found on the way:**
- window p99 counts only successful requests, so the static oracle in the
  step run "recovered" while 12% of requests timed out. Runs with > 0.1%
  errors now get no recovery time;
- the no-protection contrast broke nothing because all its 16,311 moves were
  swaps; live, only 29 of 55,172 moves were reassignments. The contrast
  didn't test what it was meant to;
- the sibling antagonist ran (CPUs 9, 11 at 100%) but didn't slow beP1 at all,
  so H6 can't be tested with our latency-bound work;
- RIF's 33× jump flattened the H5 chart; it now uses a log scale.

**Machine time:** S2-extra 6 min, S3 ~70 min, S4 ~30 min. Campaign total
~4 h 50 min including the three invalid S2 runs and the checks, against
~2.5 h planned.

---

## 2026-11-02 — Follow-ups: total RIF, the redirector contrast, H6, the sweep

~63 min of machine time (estimate 66; the 180-s sweep point was cut and
S3's antagonist run serves as the slow end). Results in [results.md](results.md).

- **Total RIF, open search range (2 more S2 runs):** 5653 and 5778 req/s
  (90.7%, 92.7%). With the first run (≥ 95.1%), 92.8% on average: the same as
  CPU, clearly above RIF per core. The first run's lower bound had suggested
  more. In the step and antagonist scenarios (2 runs each) it recovers like
  CPU from the antagonist and more slowly from the step (40–45 s).
- **RQ4 contrast that tests the right thing:** redirectors loaded with the
  new `-no-forward` switch (still decapsulating), CPU controller, antagonist:
  36 of 100 long-lived connections broke, all 1359 moves were swaps. With
  forwarding, 0 broke in 34 runs and 74,899 of 74,928 moves were swaps. The
  earlier "no protection" run didn't test this: it only removed the dwell,
  and swaps don't need it. The run is last in its session because its
  orphaned sockets keep sending keepalive probes for minutes (seen in the
  dry run's later runs).
- **H6:** checked locally first which neighbour contends on a hyperthread
  sibling: our request loop −2%, independent multiplies −1%, a 64 MiB walk
  −39%. In situ the walk raised beP1's CPU time per request ~10% and its RIF
  25%; whole-core and cgroup CPU saw it, PSI didn't. H6's "only RIF notices"
  is wrong for time-based CPU signals.
- **Sweep:** the static oracle spends 50–100% of the disturbance over the SLO
  at every period from 5 to 240 s; CPU and total RIF 8–17%. No crossover
  found; H3's fast end not supported down to 5 s.

---

## 2026-11-02 — Four-laptop scripts (D39)

`bench/cluster.py` (driver), `scripts/cluster/setup.sh` (per machine, with
exact undo), `scripts/cluster/emulate.sh`, `cluster/cluster.conf.example`,
`cluster/emulated.conf`, [cluster-runbook.md](cluster-runbook.md). Supporting
changes: cluster mode in `bench/testbed.py` behind one `command()` function;
lbctl roles (director on a machine's interface, redirectors per machine);
backend metrics with raw counters for every signal; signals from metrics;
the analysis reads the SLO from the data.

**Tested:** the whole driver in dry mode on four emulated machines: preflight,
setup + undo, smoke test 10/10 through the VIP, S1, tuning, S2, S3 with the
redirector contrast (15 long-lived connections broken without forwarding,
0 with), same file layout, analysis. Nothing left behind. The single-laptop
harness still runs (e2e, golden tests, a dry S3).

**Not tested:** ssh/rsync themselves, wifi (drivers, MTU, power save, access
points), tune.sh on non-Intel CPUs, the full ~50-min run, and whether the
scaled workload (20 ms requests, 400 ms SLO) suits our wifi.

---

## 2026-11-02 — Review of the results against the data and the methodology

Every number in [results.md](results.md) rechecked against the data files
(`runs.jsonl`, controller logs, sigmon, move test, simulator), and the setup
against our protocol, Heiser's benchmarking crimes, Prequal and Unimog. No
rerun was needed: every result below still holds once stated correctly.
Fixed:
- **Sweep mislabelled:** S4's flapping run (10 s on, 10 s off) is a 20-s
  period, but the analysis plotted it at 10 s. It's now a second 20-s point;
  there is no 10-s point. The conclusion (the controller beat the oracle at
  every period tried) doesn't change.
- **"No sustained oscillation" was wrong:** CPU settles (0–143 moves/min
  60–120 s after the step), but per-core RIF keeps hunting (1279–1929
  moves/min, 7–11 direction reversals per minute) and total RIF does too, less
  (756–1046, 2–7). p99 stays within the SLO and every move is a swap. The tuning
  simulator predicted ≤ 40 moves/min; live churn is far higher.
- **Simulator numbers were stale** (from the run before the RIF model fix):
  without protection 27–6,711 broken (not 27–824), ~173 M connections per
  noise level with protection (not ~50 M).
- **Redirector contrast:** 395 resets by the backends' counters (391 by the
  tracepoint); "36 of the 100 long-lived connections" → 36 breaks (36
  reconnects).
- **Load fluctuation** behind the oracle's 95%: 5–8% (scenario runs) or
  12–13% (S1 noise run), not 7–11%; and marked as an untested explanation.
- **Means that include a lower bound** (CPU and total-RIF tables) are now "≥".
- Smaller: total RIF moves 2–3× as many buckets as CPU after the step (not
  "about as many"); its lower late p99 is within noise; RIF per core moves
  2–3.5× (not 2–3×); run length 160–240 s; E-core ratio 1.85×.
- **Labelled:** run counts on every result; which tests were added after
  seeing data (total RIF, the stream sibling neighbour, the sweep, the
  redirector contrast); that cross-session comparisons weren't interleaved
  (calibration within 0.4% across sessions); that total RIF is Prequal's
  RIF definition and our test is not a test of Prequal.
- `campaign.md` said 128 churn connections; the harness used 512 from its
  first version. `plan.md`'s SLO line still said 10 ms.

Also: `tune.sh` finds the turbo switch for amd-pstate and acpi-cpufreq
(checked against fake sysfs trees for both, with undo; not on real AMD).

## 2026-11-06 — Review of everything, startup fix, S6, and what it changed

Went through the repo and results again before calling it finished, and found
things that were wrong or claimed too much. Fixed and measured in one go.

**Code (D40):** the controller's first table is now built from the active one, with
second hops, and changed buckets count as changed at time 0 (`controller.NewFrom`, with
a test); first signal reading discarded; `WithSecondHops` keeps the old second hop as
its comment says; the director drops IP options like the redirector; a dead controller
marks a run invalid; `loadgen -targets`.

**S6 (69 min; tune.sh applied and undone; no invalid runs; laptop rebooted since S5):**
- **Hindsight table beats every controller** on the step and the antagonist (0–4% of
  windows over the SLO, no transient). This is the result that changes a headline:
  "the controller beats the oracle" was only ever "beats a table that's out of date".
  What the controller gives is not needing to know the disturbance. Across sessions,
  but the gap is large.
- **Oracle's missing 5%:** busy denominators explain at most about half (sum −2.0%,
  from one search each, within the alone-vs-alone repeat difference); the same shares
  without the LB reach 99–101%. So most of it appears only through the LB, and
  requests arrive less evenly there (CV 4.9–8.6% vs 1.6–4.3%). Not separated further.
- **CPU vs total RIF, interleaved:** 92.7% vs 90.9%, 3 runs each, overlapping. Still
  no difference we can resolve. But total RIF is now 5.7 points below the oracle (2.3
  in S2), so H2 for total RIF moved from "supported" to "on the edge", and one CPU
  table spans 87–100%.
- 100% isn't a ceiling: the SLO judges the mix (6292 req/s without the LB: beP1 24.6
  ms, mix 19.9 ms).

**Corrections to what we had written:**
- **H3** was marked "not supported". Its threshold is the control period and the
  connection lifetime (about 2.2 s, measured; we'd said 0.5–2 s), and the fastest
  on-phase is 2.5 s, so every point is above it: first half consistent, second half
  untested.
- **"Best static split"** was never searched for: it's shares proportional to
  standalone capacity, and CPU tables have measured above it. Reworded with "beat the
  oracle" (results, README, plan's RQ2). Older journal entries keep their old wording.
- **D19:** the real reason for no UDP is that the keep rule depends on TCP state.
- **H6:** the rises (CPU 1.10×, RIF 1.25×) are all inside the dead bands, so no
  controller would have acted. Dropped "−39% speed", which was never saved.
- **H5:** RIF's 33× is a queue growing under overload and CPU is capped at 1, so the
  ratios aren't comparable.
- **Hook cost:** only 39 vs 45 and 64 vs 78 ticks of 10 ms in one run, router and
  bridge included.
- **Numbers:** "0–440 moves/min" holds only for S3 and S4 (flapping-20 made 726 and
  520, no protection 4000); load CV is 4.4–12.5%, S1's noise run 11.4–12.8%; flapping-5
  beE1 1190 to 862–1011 for CPU; "the same run broke 196 connections" was a different
  run and 196 is failed requests (36 long-lived + 160 short); beP2 lost 952 req/s, not
  the assumed 828.
- **Method:** the docs said A B C A B C; S2 ran all static policies first and S3 the
  oracle once per block (`campaign.md` now lists what ran). The fixed `RESOLUTION`
  assumed ±20% ranges; it's computed per search now. ~5% rests on 4 pairs (limit
  anywhere from 3% to 15%) and p99 near 20 ms isn't monotonic. `measurement-protocol.md`
  says plainly where we fell short.
- **Still open:** no hindsight table on flapping. No disturbance below H3's threshold.
  (No controller run with long-lived connections after the startup fix: closed by S7,
  below.)

**Plots:** colour follows the signal everywhere and grey means a static table; run
counts shown; lower bounds drawn as bounds; the sweep's 120-s on-phase is a separate
point; the dotted RIF line is gone; dashed lines mark runs from another session.

Emulated four-machine dry run repeated (5 min 40 s, clean undo, no invalid runs, 14
long-lived connections broken in the redirector contrast).

## 2026-11-06 — S7, a demo script, and a quick pass from a friend's read-through

**S7 (16 min, tune.sh applied and undone, no invalid runs):** antagonist scenario on the
final code, CPU, total RIF, total RIF, CPU. 0 of 400 long-lived connections broken, 0
backend resets, 6,233 moves (all swaps), 4.5 M short-lived requests with 0 errors.
Recovery 5–10 s, 8–12% of windows over the SLO, in line with S3 and S5 and still behind
hindsight's 0–4%. It doesn't force the startup race; the unit test covers that.

**Demo (`scripts/demo.py`):** the connection-survival sequence, ~40 s, no tune.sh. Tried 7
full runs: 0 of 300 broken with second hops every time, and 133, 151, 149, 156, 154, 142,
146 without (50% of the buckets move, so about 150 is what you'd expect; the move test had
130–146). One of those with all 24 CPUs kept busy by other work: same outcome. Also tried
Ctrl-C partway (cleans up, and now stops the load generator too; at first it ran on) and
starting with a testbed already up (refuses).

**Quick pass:** `-signal` help lists rif-total; stale comments in `internal/lb/lb.go`;
campaign.md's hook cost is 2 rates, and its "still to build" section is gone; plan.md's
phase table and open items are current and phase 1 says TC (D35); the ~1.5% (full noise
check) and ~5% (20-s probes) now say why they differ; D29 names `bpf_skb_change_tail`;
README: libbpf headers, Linux-only Go tools (cross-compiling to Windows and macOS fails on
netns and affinity system calls), the missing session commands and helper scripts.
`.gitattributes` forces LF for scripts and source only, no `* text=auto`: the 14 committed
`sysmon.csv` files keep their CRLF.

## 2026-11-06 — The dwell test didn't test the dwell

`TestNeverChangesABucketWithinDwell` passed with the dwell check removed from `plan`:
oldest-first with 4096 buckets never reaches a recent one, so only the NewFrom test caught
it. Now the load keeps reversing with so few old buckets that only recent ones are left to
move. Without the check it fails ("bucket 32 changed again 6 s after its last change"); the
old scenario stays as `TestMovesKeepTheOldFirstHop`. Also: stale search ranges, churn rule and
connection lifetime in campaign.md; the connections signal marked as dropped (D10); a note
in results.md, from reading the code only, that a dead backend's cores look idle to CPU.

## 2026-11-06 — Two cluster bugs that emulation hid

Emulation ran the driver as root, so it couldn't see either. (1) `remote_dirs` ran `sudo mkdir`
on the client too, leaving the results directory root-owned, and the driver then couldn't
write `runs.jsonl`. Now the emulated transport uses `sudo` per command like the real one, the
driver refuses to run as root, and this reproduced at once (`PermissionError`). Fixed with
`results_owned`: it makes the directory and gives it to the user running sudo there. (2)
`setup("up")` was outside the `try`: with a failure on the third machine the first two stayed
set up and the next attempt stopped with "already set up" (reproduced). It's inside now, and
the same failure leaves no state files. Full emulated dry run as a normal user: 6 min 15 s, no
root-owned files left, no invalid runs, 19 long-lived connections broken in the contrast.
Still only checkable on real machines: ssh, rsync, ownership on a separate LB machine.

## 2026-11-06 — `bench/cluster.py --quick`

For ~33 min instead of ~50 (estimate from step lengths): 4 probes per search, oracle and
equal only among the static tables, antagonist on at 30 s, off at 120 s, end at 150 s. The
default and the single-laptop scenario don't change. Checking the analysis found text that
was wrong for any run with one search per backend, or for a short lead-in: "CV pooled over
0 pairs: nan%"; "p99 before" empty when the disturbance starts at 30 s (S5's flapping rows
had this all along and now have values); the 30-s signal baseline would have included
start-up; a one-point "sweep" that described the 120-s on-phase; hard-coded claims about
non-monotonic p99 and the worst resolution, now stated only if the data shows them; empty
rows for absent policies in the capacity plot; "n/a (0% errors)". The 30/120/150 windows were
checked on synthetic records, since `--dry` shortens the timing.

## 2026-11-06 — clang 21: inner map value type

On a teammate's Ubuntu laptop (clang 21.1.8) every director test failed: `can't get size of
BTF value: type *btf.Fwd`. Clang 21 emits a nested map's value type (`struct bucket` in
`struct table`) only as a forward declaration; clang 23 emits it in full. `struct table` now
gives `key_size` and `value_size`. `backends`, the only other struct-valued map, is top-level
and stayed a full struct on both compilers. Same compiler, old vs new source: identical
instructions, relocations and map sizes, so no rerun. Reproduced and parse-checked with an
Arch clang 21.1.8 build, not loaded into a kernel with it; the laptop is the real test.

## 2026-11-06 — First run on real laptops

Client i7, LB and h1/h2 i5s, phone hotspot, `--quick`, 1 run per configuration, ~32 min. The first
dry run crashed in tuning (`nan`: too few signal samples in a dry run's short window) and capacity
came out well under the sum, so COST_MS 20 → 40, SLO 400 → 800, CONNS 64 → 32. Details:
[results.md](results.md#real-laptops).

Checking the numbers changed a few readings: the errors are mostly requests never sent, equal and
total RIF are one table measured twice (RIF made 0 moves in S2), and at 40 ms the limit looks like
the backends, not wifi. h2's 69% is its CPU speed with clocks fixed (~72% of h1's, calibration only
ran on h1); why the clocks differ is unknown. Also fixed the summary's calibration label, which
printed the work per request as "= 1 ms".

## 2026-11-12 — Repeat sessions S8–S10, S12–S15

After a second review asked for more runs of what had 1–3. `bench/campaign.py` got sessions made
of units with a checkpoint after each (`runs.jsonl`: `unit_start`, `unit_done`; an unfinished unit
is cut out and redone), `bench/repeats.sh` drives them and waits when the machine is busy, and
`bench/analyze_campaign.py` got means with 95% intervals (Student's t; Welch for differences). A
first attempt on the 11th ran while another job used every CPU and was thrown away; the busy
check comes from that. The seven sessions then ran 00:28–07:17 without a break, 115 units, none
dropped, none invalid ([campaign.md](campaign.md)).

What changed in the conclusions ([results.md](results.md)):
- Total RIF's tables are 7.1 points below CPU's (S8, +2.7 to +11.5; S14 agrees at fixed load).
  With 3 runs each (S6) we had written that they couldn't be told apart. CPU ≈ oracle (0.3 points).
- A neighbour switching every second, under the control period and connection lifetime, didn't
  defeat the controller (H3's second half): it settles on a table for the average.
- The no-protection controller broke 10 and 1 long-lived connections in two runs; S3's single
  run had broken none, because it happened to make no reassignments.
- New: at 85% load nothing holds the SLO under the antagonist; a tenfold range of gain makes
  little difference; XDP on veth costs 1.24× TC (4 repeats); the LB adds < 0.1 ms to p99 below
  the knee; through the LB the oracle is 3.4 points under the same shares without it (resolved).
- The backends alone were 3.6% slower that night than on 2 November (sum 6011 vs 6234), cause
  unknown; fractions stay against S1's sum.

Not fixed, on purpose: total RIF's weaker table is a property of the signal (equal in-flight
counts over-load 2-core backends), not a fault in the controller.
