# Plan

## Problem

On a cluster where servers have unequal and changing capacity, an L4 load
balancer that can only steer *new* connections should carry as much load as
possible within a latency and error SLO (p99 ≤ L, errors including resets ≤ E),
without breaking live connections and without per-connection state on the LB.
Details and reasons for every choice: [decisions.md](decisions.md).

## Design in one paragraph

A dedicated, stateless load balancer (eBPF at TC ingress, D35) hashes each packet's 4-tuple into a
4096-bucket table. Each bucket names a first-hop and a second-hop backend. The
LB encapsulates the packet (UDP + 12-byte header carrying the second hop) to the
first hop; backends reply to the client directly. A TC program on each backend
keeps the packet if it has a socket for it (or it's a SYN, or a valid SYN
cookie) and otherwise forwards it once to the second hop. A controller reads
load signals from the backends and moves buckets between backends (free swaps
first, then the safest reassignments) so that load relative to capacity evens out.

## Research questions (ranked)

1. **RQ1** What usable capacity fraction does each policy reach, with static and with changing capacity?
2. **RQ4** Does the second hop keep connections alive through table changes, and how often does a second change to a bucket break them?
3. **RQ2** For which disturbance timescales does the dynamic controller do better than a static table, and does it stay stable? Two static tables: proportional to the capacities before the disturbance ("the oracle"), and, added later, proportional to those during it ("hindsight").
4. **RQ3** Which load signal works best: whole-core CPU vs requests in flight (main); cgroup CPU, PSI on key scenarios (a connections signal was planned and dropped, D10).

## Hypotheses (written before measuring)

Unchanged since (git history). Outcomes, and which tests were added after
seeing data: [results.md](results.md#hypotheses-outcome). "Static oracle" is only
our name for the table proportional to each backend's standalone capacity,
measured before any disturbance: nobody searched for the best static split.


- **H1** Static-by-cores reaches clearly less than the static oracle, because an E-core is not worth the same as a P-core (calibration with turbo off: a request takes 1.84× as long on an E-core, so a 4-E-core backend is worth ~1.1× a 2-P-core backend, not 2×).
- **H2** With static capacity, the dynamic controller matches the static oracle within noise and breaks no connections.
- **H3** With changing capacity, the controller beats the static oracle only when disturbances last longer than both the control period and the typical connection lifetime; for fast disturbances nobody helps, and an aggressive controller may hurt.
- **H4** Equalising CPU utilisation maximises throughput, not capacity under a latency SLO. When core speeds differ, a slower backend reaches the deadline at lower utilisation, so whole-core CPU may lose usable capacity where RIF (which grows with service time) holds up.
- **H5** With an antagonist on a backend's cores in another cgroup, cgroup CPU is fooled; PSI and whole-core CPU are not.
- **H6** With an antagonist on the idle hyperthread sibling of a P backend's core, only RIF notices.
- **H7** One change to a bucket breaks no connections; broken connections grow with the controller's change rate and with connection lifetime (a second change within one connection lifetime).

## Phases

| Phase | Work | Exit check | Status |
|---|---|---|---|
| 0 | Repo, docs, topology + pinning + RPS, runtime tuning, backend v0, loadgen, sysmon, **noise check** | Noise within limits (see protocol) | done 2026-11-02: PASS, smallest claimable difference ~1.5% (from the full noise check, with long probes and 5 repeats; the campaign's 20-s probes are noisier, which gives the ~5% in [results.md](results.md)) |
| 1 | LB with a static table + encapsulation (first at XDP, at TC since D35); TC redirector (decap, second hop, loop flag, syncookie check); controller offline (table generator, publishing, signal plugins, simulator); tracepoint reset counter; golden-packet tests | End to end through the LB with a static table, everything logged | done 2026-11-02 (director, redirector, reset monitor, move test, controller offline + simulator, live smoke test) |
| 2–4 | ~~Drain test, harness v1, sweeps~~ replaced by the short campaign (D37) | | |
| 2 | Harness for the campaign: capacity search through the LB, scenario runner, tuning tool ([campaign.md](campaign.md)) | dry run: one probe of each experiment type | done 2026-11-02 |
| 3 | Campaign S1–S4, ~2.5 h of testbed time; follow-ups S5, S6, S7 | all tables in campaign.md filled | done: S1–S5 2026-11-02 (S1–S4 ~4 h 50 min), S6 and S7 2026-11-06; [results.md](results.md) |
| 4 | Write-up; later: key experiments rerun on 4 laptops | | write-up done 2026-11-06. 4 laptops: one `--quick` run on real machines 2026-11-06, 1 run per configuration ([results.md](results.md#real-laptops)) |

Cut line (not needed in the end): S4 is dropped first; S1–S3 answer every research question.

## Open items

- **Four laptops:** one `--quick` run only, so no repeats; why h2 ran slower with clocks fixed is unknown ([results.md](results.md#real-laptops)).
- **H3's second half:** no disturbance faster than the control period and connection
  lifetime was tried. **H7's second half:** only the simulator covers it.
- **Hindsight tables** weren't run on flapping.
- **Startup fix:** S7 gives the final code its own "0 broken" in one scenario; S1–S5's
  34 runs predate the fix.

Settled (they were open when this plan was written):
- Workload model: D37.
- Per-packet cost of the director at TC vs XDP (D35): measured in S4, 1 run
  ([results.md](results.md#the-hook-d35)).
- Controller tuning: one rule for every signal (D36), run in S1. Overload override stays off.
- L: 20 ms (10 ms was the plan; changed after the dry run, before the campaign: an
  E-core request alone has a p99 of ~8.5 ms); 10 and 50 ms read off the same probes.
