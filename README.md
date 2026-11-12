# CS331-T020-MiniKatran

**Team ID:** T020

**Project ID:** 16

**Project Title:** Mini-Katran: Load-Aware, Stateless L4 Load Balancing with eBPF at TC

| Name | Roll number |
|---|---|
| Akshit Chhabra | 24110026 |
| Bhavay Goyal | 24110070 |
| Devarshi Savalgi | 24110103 |
| Kunal Chandola | 24110176 |

CS 331 Computer Networks, 2026-27 Semester 1.

## What this is

A layer-4 load balancer in the style of Cloudflare's Unimog, for servers of unequal and changing capacity. A stateless director at the TC hook hashes packets into 4096 buckets, a feedback controller moves buckets according to measured load, and a redirector on each server forwards packets of older connections to their previous server. No per-connection state is kept on the balancer.

Main results (one laptop, four unequal backends; 5 interleaved runs for the main comparison, 1 to 4 elsewhere): the table found by the CPU controller reaches 94.4% of summed capacity at p99 at most 20 ms, against 94.7% for shares proportional to capacity, 87.3% for the table found from requests in flight, 86.3% for shares by core count and 64.9% for equal shares. Moving half of the buckets broke 0 of 300 long-lived connections with second hops and 130 to 146 without. Across 91 live runs with forwarding, no long-lived connection broke. A static table built for a known disturbance beats every controller; the controller's value is that it doesn't need that knowledge, and it still helped against a neighbour switching every second. One quick run on four real laptops over a phone hotspot confirmed connection survival (0 of 20 broken with forwarding, 12 of 20 without), but nothing recovered within the latency target during the disturbance.

## Layout

```
README.md
code/       all source code, scripts, tests and results (see code/README.md)
report/     final report (PDF)
ppt/        presentation slides
ai/         all AI prompts and conversations
```

## Running it

Linux with kernel 6.6 or newer, root, clang, libbpf headers, Go and Python 3 with matplotlib. From `code/`:

```
make
make test && make test-bpf
sudo python3 scripts/demo.py     # connections through a table change, with and without second hops
```

`code/README.md` covers the full campaign on one laptop, and `code/docs/cluster-runbook.md` covers the 4-laptop run. `code/docs/results.md` has all results with run counts and caveats.
