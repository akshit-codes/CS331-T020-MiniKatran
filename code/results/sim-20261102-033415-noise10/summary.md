# Controller simulation

Backends: beP1 (2 cores, 1000 req/s) beP2 (2 cores, 1600 req/s) beE1 (4 cores, 1760 req/s) beE2 (4 cores, 1760 req/s) 

Controller: {Gain:0.3 MaxStep:0.25 MinBuckets:16 UMin:0.05 DeltaOn:0.1 DeltaOff:0.05 LowLoadGate:0.2 SaturatedAt:0 Dwell:20 GlobalCap:81 PerBackendCap:40}, period 2 s, signal noise 10%.
Connections: mean lifetime 2 s, 1 request/s each. Every case runs with 5 seeds; the table shows medians.
Imbalance, max ρ, p99 and moves/min are over the last 60 s of each phase;
re-changes and broken connections are totals over the whole run and all seeds.

## static

60% load, start from shares by cores. Noise floor (oracle table, p99 of 10-s imbalance) 1.047, so reached = imbalance ≤ 1.057.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 38 | 1.058 | 0.62 | 11.4 | 1129 | 145 | 0 | 0 of 5.5M |
| cpu | 0–600 s | 38 | 1.058 | 0.62 | 11.4 | 1129 | 145 | 0 | 0 of 5.5M |
| rif | 0–600 s | 38 | 1.089 | 0.65 | 11.3 | 1303 | 163 | 0 | 0 of 5.5M |

## antagonist

60% load; at 300 s beE1 loses half its capacity (overloaded), at 600 s gets it back. Noise floor (oracle table, p99 of 10-s imbalance) 1.047, so reached = imbalance ≤ 1.057.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–300 s | 38 | 1.054 | 0.62 | 11.4 | 1130 | 64 | 0 | 0 of 8.3M |
| true | 300–600 s | 45 | 1.059 | 0.73 | 18.0 | 1080 | 67 |  |  |
| true | 600–900 s | 34 | 1.057 | 0.62 | 11.4 | 1083 | 62 |  |  |
| cpu | 0–300 s | 38 | 1.054 | 0.62 | 11.4 | 1130 | 64 | 0 | 0 of 8.3M |
| cpu | 300–600 s | 40 | 1.057 | 0.72 | 17.9 | 1088 | 65 |  |  |
| cpu | 600–900 s | 32 | 1.056 | 0.62 | 11.4 | 1081 | 62 |  |  |
| rif | 0–300 s | 38 | 1.087 | 0.65 | 11.3 | 1218 | 71 | 0 | 0 of 8.3M |
| rif | 300–600 s | never | 1.096 | 0.76 | 19.2 | 1303 | 72 |  |  |
| rif | 600–900 s | 185 | 1.083 | 0.65 | 11.3 | 1231 | 71 |  |  |

## overload-start

80% load, start from equal shares: beP1 at ρ=1.22. Noise floor (oracle table, p99 of 10-s imbalance) 1.057, so reached = imbalance ≤ 1.067.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 31 | 1.059 | 0.83 | 19.7 | 1187 | 143 | 0 | 0 of 7.3M |
| cpu | 0–600 s | 31 | 1.057 | 0.83 | 20.1 | 1168 | 139 | 0 | 0 of 7.3M |
| rif | 0–600 s | 26 | 1.063 | 0.85 | 18.8 | 1390 | 157 | 0 | 0 of 7.3M |

## near-capacity

90% load, start from the oracle table; at 200 s beE1 loses 12% (ρ≈1.02). Noise floor (oracle table, p99 of 10-s imbalance) 1.053, so reached = imbalance ≤ 1.063.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–200 s | 10 | 1.050 | 0.94 | 57.8 | 1218 | 45 | 0 | 0 of 8.3M |
| true | 200–600 s | 17 | 1.047 | 0.96 | 180.8 | 1164 | 97 |  |  |
| cpu | 0–200 s | 10 | 1.049 | 0.94 | 57.3 | 1211 | 45 | 0 | 0 of 8.3M |
| cpu | 200–600 s | 21 | 1.048 | 0.95 | 193.5 | 1179 | 91 |  |  |
| rif | 0–200 s | 10 | 1.045 | 0.93 | 52.2 | 1559 | 48 | 0 | 0 of 8.3M |
| rif | 200–600 s | 13 | 1.040 | 0.96 | 128.1 | 1689 | 100 |  |  |
| true+override | 0–200 s | 10 | 1.049 | 0.94 | 61.2 | 1218 | 44 | 0 | 0 of 8.3M |
| true+override | 200–600 s | 17 | 1.048 | 0.95 | 209.0 | 1198 | 94 |  |  |
| cpu+override | 0–200 s | 10 | 1.049 | 0.94 | 58.2 | 1211 | 45 | 0 | 0 of 8.3M |
| cpu+override | 200–600 s | 22 | 1.049 | 0.96 | 212.5 | 1182 | 94 |  |  |

## saturated

95% load, start from the oracle table; at 200 s beE1 loses 10% (ρ≈1.06: over capacity for good unless the controller acts). Noise floor (oracle table, p99 of 10-s imbalance) 1.037, so reached = imbalance ≤ 1.047.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–200 s | 10 | 1.045 | 0.98 | 293.0 | 1053 | 43 | 0 | 0 of 8.7M |
| true | 200–600 s | 15 | 1.055 | 1.02 | 2369.8 | 1171 | 99 |  |  |
| cpu | 0–200 s | 10 | 1.049 | 0.98 | 341.7 | 1049 | 44 | 0 | 0 of 8.7M |
| cpu | 200–600 s | 21 | 1.071 | 1.02 | 7183.9 | 1110 | 91 |  |  |
| rif | 0–200 s | 10 | 1.050 | 0.98 | 240.9 | 1827 | 55 | 0 | 0 of 8.7M |
| rif | 200–600 s | never | 1.296 | 1.15 | 9974.8 | 2100 | 53 |  |  |
| true+override | 0–200 s | 10 | 1.045 | 0.98 | 300.4 | 1070 | 43 | 0 | 0 of 8.7M |
| true+override | 200–600 s | 15 | 1.055 | 1.02 | 2439.1 | 1150 | 104 |  |  |
| cpu+override | 0–200 s | 10 | 1.049 | 0.98 | 385.5 | 1065 | 44 | 0 | 0 of 8.7M |
| cpu+override | 200–600 s | 33 | 1.073 | 1.02 | 7277.1 | 1129 | 94 |  |  |

## heavy-tails

like antagonist, but connection lifetimes are Pareto (same 2 s mean; 0.6% live longer than the 20 s dwell). Noise floor (oracle table, p99 of 10-s imbalance) 1.056, so reached = imbalance ≤ 1.066.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–300 s | 30 | 1.046 | 0.60 | 11.1 | 1111 | 66 | 0 | 0 of 8.3M |
| true | 300–600 s | 41 | 1.046 | 0.70 | 17.2 | 1144 | 65 |  |  |
| true | 600–900 s | 34 | 1.050 | 0.61 | 11.3 | 1164 | 57 |  |  |
| cpu | 0–300 s | 30 | 1.046 | 0.60 | 11.1 | 1111 | 66 | 0 | 0 of 8.3M |
| cpu | 300–600 s | 41 | 1.043 | 0.70 | 17.3 | 1152 | 65 |  |  |
| cpu | 600–900 s | 33 | 1.052 | 0.61 | 11.2 | 1149 | 59 |  |  |
| rif | 0–300 s | 78 | 1.082 | 0.63 | 11.1 | 1180 | 70 | 0 | 0 of 8.3M |
| rif | 300–600 s | 138 | 1.090 | 0.75 | 18.5 | 1281 | 69 |  |  |
| rif | 600–900 s | 107 | 1.084 | 0.63 | 11.1 | 1170 | 67 |  |  |

## no-protection

control for the safety check: heavy tails, beE1 flapping (half capacity on/off every 10 s), and a controller with no dwell, no caps, gain 1, band 2%/1%. Noise floor (oracle table, p99 of 10-s imbalance) 1.056, so reached = imbalance ≤ 1.066.

| signal | phase | reached (s) | imbalance | max ρ | p99 (ms) | moves/min | reversals | re-changes within 20 s | broken conns |
|---|---|---|---|---|---|---|---|---|---|
| true | 0–600 s | 10 | 1.083 | 0.60 | 11.1 | 5200 | 190 | 125543 | 533 of 5.5M |
| cpu | 0–600 s | 10 | 1.081 | 0.60 | 11.1 | 5171 | 191 | 117743 | 501 of 5.5M |
| rif | 0–600 s | 10 | 1.104 | 0.62 | 11.0 | 6306 | 168 | 467326 | 6711 of 5.5M |

