# Dry run: not a result

Clean `bench/cluster.py --dry` on the four real laptops (2026-11-06), with the settings of the
real run (COST_MS=40, SLO_P99_MS=800, CONNS=32). Probes are 4 s and there are 2 per search,
and `tune.sh` isn't applied (`--dry` skips it, so turbo was on and the work per request is
larger than in the real run). Its capacities and fractions are not results.

Only used for: minutes before the real run the same hotspot carried 147 req/s at p99 332 ms, and
h2's backends kept up with h1's with turbo on. See
[docs/results.md](../../docs/results.md#real-laptops).
