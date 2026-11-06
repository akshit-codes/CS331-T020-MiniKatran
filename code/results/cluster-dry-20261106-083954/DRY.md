# Dry run: not a result

First `bench/cluster.py --dry` on the four real laptops (2026-11-06), with COST_MS=20,
SLO_P99_MS=400, CONNS=64. Probes are 4 s, `tune.sh` isn't applied (`--dry` skips it, so turbo
was on), and it crashed in S2, so there's no `summary.md`.

The crash: at most 4 signal samples fell inside the dry run's short noise window (gaps of
1.5-2.8 s in `s1/noise-0.8.csv`), too few for any period, so the noise table is all `nan`, no
setting is chosen, and `controller_table` fails on the empty choice (`run.log`).

Only used for: this hotspot carried 278 req/s with no errors, at p99 1089 ms. See
[docs/results.md](../../docs/results.md#real-laptops).
