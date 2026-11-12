# Measurement protocol

Rules every measurement run follows, so results are comparable and the noise
is known rather than assumed. For the final campaign, [campaign.md](campaign.md)
shortens steps and repeats (D37); everything else here still applies.

## Before and after a run

- `sudo scripts/tune.sh apply` before, `sudo scripts/tune.sh undo` after.
  Apply = turbo off, fixed base clock and no deep idle states on the
  experiment CPUs, everything else (desktop, editor) confined to CPUs 0–1,
  device interrupts on CPUs 0–1 (decisions D15, D20).
  The bench scripts do this themselves and always undo, also on errors or Ctrl-C.
  If something crashes hard, run `sudo scripts/tune.sh undo` by hand (it is a no-op if nothing is applied).
- On AC power, power profile `performance`, laptop raised for airflow, browser closed.
- Each experiment component runs in its own cgroup under `cs331.slice` on its CPUs (see `config.env`, `backends.conf`).

## Load generation

- Open loop (`cmd/loadgen`): requests are scheduled at a given average rate
  (Poisson by default, fixed seed); latency is measured from the scheduled time.
- Each step = warm-up (not measured) + measured phase.
- A step is invalid if the generator was the bottleneck: requests dropped by the
  generator > 0, or dispatch lag p99 large compared with request latency.

## SLO and capacity

- A rate **meets the SLO** if p99 ≤ L, error rate ≤ E, and at least 98% of the
  offered rate was served. Noise check: L = 10 ms, E = 0.1%, 1 ms requests.
- **Capacity at SLO**: binary search between 0.85× and 1.25× a rough estimate
  (from a ramp in 10% steps), 6 probes (~0.6% resolution).
  A result is flagged if no probe passed or it sits at the edge of the range.

## Repeats and statistics

- Fixed-rate comparisons: ≥ 10 runs, alternating between the configurations
  being compared, so slow drift doesn't look like a difference.
- Capacity: ≥ 5 searches per configuration.
- Report mean ± standard deviation, coefficient of variation (CV) and range,
  and say which. Never report a single run.
- Don't claim differences smaller than ~2× the capacity CV measured in the
  noise check.
- Capacities are only comparable if measured with the same probe settings
  (warm-up, measured length, number of probes) in the same session, with
  the configurations interleaved. Quick (10 s) probes read 3–5% higher
  than full (40 s) ones (journal 2026-11-02).

## Where the campaign fell short of this

The rules above were written for a longer plan; D37 cut it to fit the time. The repeat
sessions of 2026-11-12 (S8–S15, [campaign.md](campaign.md)) closed part of the gap.
Plainly, as it stands now:
- **"Capacity: ≥ 5 searches per configuration":** met in S8 for the oracle, the CPU
  tables and the total-RIF tables (5 each, one session). Equal and by cores have 3,
  RIF per core 2, each hindsight capacity and each alone denominator of S8 have 1.
- **"Fixed-rate comparisons: ≥ 10 runs":** met only for the antagonist with the CPU
  controller (10, over five sessions). Total RIF there has 9, the step 7–8, S14's
  fixed-load tables 5 per load, everything else 1–4.
- **"Never report a single run":** still broken, labelled wherever it happens: the
  sweep's 5-, 20- and 60-s points, S13's other load levels, PSI, the sibling
  neighbour. The redirector contrast, the hook cost, the cgroup controller and the
  no-protection run now have 3–5.
- **"Mean ± standard deviation, CV":** S8, S14 and the hook and overhead repeats
  give means with 95% intervals; elsewhere we still give ranges.
- **Interleaving:** only partly ([campaign.md](campaign.md#run-order-what-actually-happened)).
- **Same session for capacities:** not across S2, S2-extra and S2-rif-total (S6
  redoes CPU vs total RIF in one session).
- **The neighbours-busy check:** not applied to the denominators until S6.
- **Claim limit:** "2× the capacity CV" is ~5%, from only 4 standalone pairs of
  the short protocol (S1), not from the noise check (0.3–0.8% with longer probes
  and n = 5). With 4 pairs its 95% interval is 1.5–7.3%, so the limit could be
  3–15%. Near 20 ms p99 isn't monotonic with load either, so a bisection can
  be led astray by one probe (the oracle's probes at 87–98% of the summed
  capacities range over 13–28 ms). Read ~5% as a rule of thumb, not as a bound.

## Pass criteria for the noise check

| Check | Limit |
|---|---|
| p99 CV at 80% load (run to run) | ≤ 10% |
| Capacity-at-SLO CV | ≤ 3% |
| Thermal throttle events during the run | 0 |
| beP1 capacity with neighbours busy vs alone | within 2× CV (otherwise measure denominators with all backends loaded) |

If a check fails, change one thing at a time and keep what helps: siblings
offline, desktop vs TTY, longer measured phase, higher cost per request.

## Machine-state checks (sysmon)

- Clock speed of the backends' cores is steady (turbo off → base clock).
  Read the *median* of `scaling_cur_freq`: single samples can read ~800 MHz
  on a core whose clock is steady (checked against APERF/MPERF).
- No thermal-throttle events.
- Packet processing (softirq) appears on the intended CPUs (RPS).
