# Noise check noise-20261101-225150-quick

## Calibration
- work per request: 101333 units (= 1.0 ms on a P-core)
- P-core 101333 units/ms, E-core 54960 units/ms -> a request takes **1.84x** as long on an E-core
- P backend: theoretical 2000 req/s, rough SLO capacity 1600 req/s
- E backend: theoretical 2169 req/s, rough SLO capacity 1736 req/s
- SLO: p99 <= 10.0 ms, errors <= 0.1%, served >= 98% of offered

## Repeatability of p99 (beP1, fixed rates, alternating)
- 50% load (800 req/s): p50 1.26 ms ± 0.00 (CV 0.1%, range 1.25–1.26, n=2)
  - p99 3.34 ms ± 0.01 (CV 0.3%, range 3.33–3.35, n=2)
  - within one run, p99 per 10 s window varies by CV 1.8% (median over runs)
- 80% load (1280 req/s): p50 1.61 ms ± 0.00 (CV 0.3%, range 1.61–1.61, n=2)
  - p99 4.56 ms ± 0.09 (CV 2.1%, range 4.50–4.63, n=2)
  - within one run, p99 per 10 s window varies by CV 1.9% (median over runs)

## Repeatability of capacity at SLO
- beP1 alone: 1680.0 req/s ± 0.0 (CV 0.0%, range 1680.0–1680.0, n=1)
- beE1 alone: 1865.7 req/s ± 0.0 (CV 0.0%, range 1865.7–1865.7, n=1)
- beP1, others at 70%: 1680.0 req/s ± 0.0 (CV 0.0%, range 1680.0–1680.0, n=1)
- coupling: beP1 capacity changes by +0.0% when the other backends are busy (within 2x the run-to-run CV)
- smallest difference between policies we could claim (~2x capacity CV): 0.0%

## Generator health (all runs)
- dispatch lag p99, worst run: 0.001 ms
- requests dropped by the generator: 0

## Machine state (sysmon)
- repeat: beP1 cores 2082 MHz, beE1 cores 1577 MHz, package 71 °C (max 72), 15.6 W
- capacity-probe: beP1 cores 2145 MHz, beE1 cores 1596 MHz, package 72 °C (max 74), 16.1 W
- coupled-probe: beP1 cores 2169 MHz, beE1 cores 1599 MHz, package 73 °C (max 74), 19.5 W
- thermal throttle events during the run: package 0, cores 0
- where packet processing ran (softirq %, 80% runs): cpu6 0.8%, cpu2 0.6%, cpu8 0.5%, cpu1 0.5%, cpu10 0.5%, cpu4 0.4%
  expected: loadgen 2,4, router 6, beP1 8,10

## Verdict
- PASS: p99 CV at 80% load = 2.1% (limit 10%)
- PASS: thermal throttle events = 0
