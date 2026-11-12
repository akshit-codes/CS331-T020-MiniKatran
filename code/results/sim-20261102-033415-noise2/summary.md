# Controller simulation

Backends: beP1 (2 cores, 1000 req/s) beP2 (2 cores, 1600 req/s) beE1 (4 cores, 1760 req/s) beE2 (4 cores, 1760 req/s) 

Controller: {Gain:0.3 MaxStep:0.25 MinBuckets:16 UMin:0.05 DeltaOn:0.1 DeltaOff:0.05 LowLoadGate:0.2 SaturatedAt:0 Dwell:20 GlobalCap:81 PerBackendCap:40}, period 2 s, signal noise 2%.
Connections: mean lifetime 2 s, 1 request/s each. Every case runs with 5 seeds; the table shows medians.
Imbalance, max ρ, p99 and moves/min are over the last 60 s of each phase;
re-changes and broken connections are totals over the whole run and all seeds.

## static

60% load, start from shares by cores. Noise floor (oracle table, p99 of 10-s imbalance) 1.047, so reached = imbalance ≤ 1.057.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 22 | 1.036 | 0.62 | 11.2 | 63 | 19 | 0 | 0 of 5.5M |
| cpu | 0–600 s | 22 | 1.036 | 0.62 | 11.2 | 63 | 19 | 0 | 0 of 5.5M |
| rif | 0–600 s | 154 | 1.085 | 0.65 | 11.2 | 614 | 138 | 0 | 0 of 5.5M |

## antagonist

60% load; at 300 s beE1 loses half its capacity (overloaded), at 600 s gets it back. Noise floor (oracle table, p99 of 10-s imbalance) 1.047, so reached = imbalance ≤ 1.057.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–300 s | 22 | 1.038 | 0.62 | 11.3 | 0 | 6 | 0 | 0 of 8.3M |
| true | 300–600 s | 34 | 1.031 | 0.72 | 17.6 | 0 | 7 |  |  |
| true | 600–900 s | 29 | 1.037 | 0.62 | 11.3 | 60 | 6 |  |  |
| cpu | 0–300 s | 22 | 1.038 | 0.62 | 11.3 | 0 | 6 | 0 | 0 of 8.3M |
| cpu | 300–600 s | 34 | 1.036 | 0.72 | 17.9 | 39 | 8 |  |  |
| cpu | 600–900 s | 30 | 1.031 | 0.62 | 11.3 | 48 | 6 |  |  |
| rif | 0–300 s | 154 | 1.085 | 0.65 | 11.3 | 593 | 66 | 0 | 0 of 8.3M |
| rif | 300–600 s | never | 1.091 | 0.76 | 19.1 | 1018 | 68 |  |  |
| rif | 600–900 s | never | 1.079 | 0.65 | 11.3 | 729 | 68 |  |  |

## overload-start

80% load, start from equal shares: beP1 at ρ=1.22. Noise floor (oracle table, p99 of 10-s imbalance) 1.057, so reached = imbalance ≤ 1.067.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 26 | 1.031 | 0.81 | 18.5 | 0 | 10 | 0 | 0 of 7.3M |
| cpu | 0–600 s | 26 | 1.027 | 0.82 | 18.5 | 0 | 8 | 0 | 0 of 7.3M |
| rif | 0–600 s | 26 | 1.067 | 0.85 | 18.9 | 1253 | 153 | 0 | 0 of 7.3M |

## near-capacity

90% load, start from the oracle table; at 200 s beE1 loses 12% (ρ≈1.02). Noise floor (oracle table, p99 of 10-s imbalance) 1.053, so reached = imbalance ≤ 1.063.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–200 s | 10 | 1.028 | 0.92 | 41.5 | 0 | 0 | 0 | 0 of 8.3M |
| true | 200–600 s | 14 | 1.030 | 0.96 | 92.3 | 0 | 3 |  |  |
| cpu | 0–200 s | 10 | 1.028 | 0.92 | 42.3 | 0 | 0 | 0 | 0 of 8.3M |
| cpu | 200–600 s | 35 | 1.027 | 0.96 | 79.0 | 0 | 3 |  |  |
| rif | 0–200 s | 10 | 1.043 | 0.93 | 49.2 | 1552 | 47 | 0 | 0 of 8.3M |
| rif | 200–600 s | 15 | 1.040 | 0.96 | 133.5 | 1661 | 99 |  |  |
| true+override | 0–200 s | 10 | 1.031 | 0.92 | 43.4 | 151 | 19 | 0 | 0 of 8.3M |
| true+override | 200–600 s | 11 | 1.028 | 0.95 | 87.0 | 261 | 89 |  |  |
| cpu+override | 0–200 s | 10 | 1.031 | 0.92 | 43.4 | 151 | 19 | 0 | 0 of 8.3M |
| cpu+override | 200–600 s | 13 | 1.028 | 0.95 | 82.3 | 242 | 92 |  |  |

## saturated

95% load, start from the oracle table; at 200 s beE1 loses 10% (ρ≈1.06: over capacity for good unless the controller acts). Noise floor (oracle table, p99 of 10-s imbalance) 1.037, so reached = imbalance ≤ 1.047.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–200 s | 10 | 1.028 | 0.97 | 208.2 | 0 | 0 | 0 | 0 of 8.7M |
| true | 200–600 s | 17 | 1.033 | 1.02 | 4998.5 | 0 | 0 |  |  |
| cpu | 0–200 s | 10 | 1.024 | 0.97 | 200.6 | 0 | 0 | 0 | 0 of 8.7M |
| cpu | 200–600 s | 108 | 1.078 | 1.07 | 25941.4 | 0 | 0 |  |  |
| rif | 0–200 s | 10 | 1.039 | 0.97 | 244.4 | 1887 | 48 | 0 | 0 of 8.7M |
| rif | 200–600 s | never | 1.310 | 1.15 | 9937.4 | 2141 | 50 |  |  |
| true+override | 0–200 s | 10 | 1.029 | 0.97 | 208.8 | 240 | 43 | 0 | 0 of 8.7M |
| true+override | 200–600 s | 14 | 1.031 | 1.01 | 2229.5 | 49 | 25 |  |  |
| cpu+override | 0–200 s | 10 | 1.029 | 0.97 | 209.1 | 288 | 43 | 0 | 0 of 8.7M |
| cpu+override | 200–600 s | 16 | 1.032 | 1.00 | 1557.0 | 53 | 32 |  |  |

## heavy-tails

like antagonist, but connection lifetimes are Pareto (same 2 s mean; 0.6% live longer than the 20 s dwell). Noise floor (oracle table, p99 of 10-s imbalance) 1.056, so reached = imbalance ≤ 1.066.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–300 s | 23 | 1.035 | 0.60 | 11.0 | 0 | 5 | 0 | 0 of 8.3M |
| true | 300–600 s | 34 | 1.037 | 0.71 | 17.1 | 0 | 9 |  |  |
| true | 600–900 s | 32 | 1.033 | 0.60 | 11.1 | 0 | 3 |  |  |
| cpu | 0–300 s | 23 | 1.035 | 0.60 | 11.0 | 0 | 5 | 0 | 0 of 8.3M |
| cpu | 300–600 s | 34 | 1.038 | 0.70 | 17.1 | 45 | 14 |  |  |
| cpu | 600–900 s | 32 | 1.036 | 0.61 | 11.1 | 0 | 7 |  |  |
| rif | 0–300 s | 33 | 1.083 | 0.63 | 11.0 | 234 | 48 | 0 | 0 of 8.3M |
| rif | 300–600 s | 93 | 1.091 | 0.74 | 18.4 | 679 | 65 |  |  |
| rif | 600–900 s | 54 | 1.081 | 0.64 | 11.1 | 440 | 54 |  |  |

## no-protection

control for the safety check: heavy tails, beE1 flapping (half capacity on/off every 10 s), and a controller with no dwell, no caps, gain 1, band 2%/1%. Noise floor (oracle table, p99 of 10-s imbalance) 1.056, so reached = imbalance ≤ 1.066.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 10 | 1.036 | 0.59 | 11.1 | 1525 | 205 | 103884 | 50 of 5.5M |
| cpu | 0–600 s | 10 | 1.036 | 0.60 | 11.1 | 1486 | 204 | 99090 | 27 of 5.5M |
| rif | 0–600 s | 10 | 1.083 | 0.63 | 11.1 | 2998 | 179 | 328593 | 967 of 5.5M |

