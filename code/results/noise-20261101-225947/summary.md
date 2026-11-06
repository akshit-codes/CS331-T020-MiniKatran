# Noise check noise-20261101-225947

## Calibration
- work per request: 101289 units (= 1.0 ms on a P-core)
- P-core 101289 units/ms, E-core 54942 units/ms -> a request takes **1.84x** as long on an E-core
- P backend: theoretical 2000 req/s, rough SLO capacity 1600 req/s
- E backend: theoretical 2170 req/s, rough SLO capacity 1736 req/s
- SLO: p99 <= 10.0 ms, errors <= 0.1%, served >= 98% of offered

## Repeatability of p99 (beP1, fixed rates, alternating)
- 50% load (800 req/s): p50 1.25 ms ± 0.00 (CV 0.2%, range 1.25–1.26, n=10)
  - p99 3.31 ms ± 0.02 (CV 0.6%, range 3.27–3.34, n=10)
  - within one run, p99 per 10 s window varies by CV 2.5% (median over runs)
- 80% load (1280 req/s): p50 1.60 ms ± 0.01 (CV 0.3%, range 1.59–1.61, n=10)
  - p99 4.83 ms ± 0.05 (CV 1.1%, range 4.71–4.89, n=10)
  - within one run, p99 per 10 s window varies by CV 8.9% (median over runs)

## Repeatability of capacity at SLO
- beP1 alone: 1630.0 req/s ± 12.2 (CV 0.8%, range 1610.0–1640.0, n=5)
- beE1 alone: 1770.5 req/s ± 4.9 (CV 0.3%, range 1768.3–1779.2, n=5)
- beP1, others at 70%: 1626.7 req/s ± 5.8 (CV 0.4%, range 1620.0–1630.0, n=3)
- coupling: beP1 capacity changes by -0.2% when the other backends are busy (within 2x the run-to-run CV)
- smallest difference between policies we could claim (~2x capacity CV): 1.5%

## Generator health (all runs)
- dispatch lag p99, worst run: 0.002 ms
- requests dropped by the generator: 0

## Machine state (sysmon)
- repeat: beP1 cores 2199 MHz, beE1 cores 1600 MHz (median), package 72 °C (max 74), 15.7 W
- capacity-probe: beP1 cores 2200 MHz, beE1 cores 1600 MHz (median), package 72 °C (max 74), 16.1 W
- coupled-probe: beP1 cores 2200 MHz, beE1 cores 1600 MHz (median), package 73 °C (max 75), 19.5 W
- thermal throttle events during the run: package 0, cores 0
- where packet processing ran (softirq %, 80% runs): cpu6 0.8%, cpu2 0.6%, cpu1 0.5%, cpu10 0.5%, cpu8 0.5%, cpu4 0.4%
  expected: loadgen 2,4, router 6, beP1 8,10

## Verdict
- PASS: p99 CV at 80% load = 1.1% (limit 10%)
- PASS: capacity CV, beP1 alone = 0.8% (limit 3%)
- PASS: capacity CV, beE1 alone = 0.3% (limit 3%)
- PASS: thermal throttle events = 0
