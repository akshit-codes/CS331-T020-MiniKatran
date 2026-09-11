| mode | 1 stream Gbit/s | 4 streams Gbit/s | conn/s (64 conc) | req latency p50 us (1 conc) | p99 us (64 conc) | sys cpu s/GB (4 streams) | lb process cpu % (4 streams) | lb process cpu % (cps64) | xdp ns/packet |
|---|---|---|---|---|---|---|---|---|---|
| direct | 13.1 | 39.3 | 86136 | 30 | 1343 | 1.84 | 0 | 0 | - |
| proxy | 11.5 | 12.0 | 41706 | 52 | 2288 | 2.95 | 75 | 77 | - |
| raw | 5.7 | 5.7 | 51606 | 45 | 1618 | 5.19 | 69 | 65 | - |
| xdp | 12.7 | 18.5 | 73728 | 33 | 1519 | 2.43 | 0 | 0 | 58 |
