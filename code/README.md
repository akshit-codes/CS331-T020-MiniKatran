# cs331-lb

A load-aware, stateless L4 load balancer (eBPF at TC ingress) for servers with
unequal and changing capacity, evaluated by how much of the cluster's capacity
it can use within a latency SLO without breaking connections.
CS331 (Computer Networks) course project.

## The problem

A layer-4 load balancer sees packets, not requests. It can choose a server
only when a connection starts, and must then keep sending that connection's
packets to the same server. Most L4 balancers (Maglev, Katran) give every
server an equal or fixed share. That wastes capacity when servers differ (an
older machine, slower cores) and breaks the latency target when a server
slows down (a noisy neighbour, a slow request type). Moving load away means
changing which server new connections go to *without* breaking the ones
already open, and without keeping a table of every connection on the LB.

We ask four questions:
- **RQ1:** what fraction of the servers' summed capacity can each policy use
  within the SLO (p99 ≤ 20 ms, errors ≤ 0.1%)?
- **RQ2:** for which disturbance timescales does a dynamic controller do better
  than a static table?
- **RQ3:** which load signal should it use: CPU utilisation, requests in
  flight (RIF), cgroup CPU or pressure (PSI)?
- **RQ4:** do connections survive the table changes?

## The design

```
 client ──► director (LB, eBPF at TC) ──UDP encap──► backend A ──► reply straight to the client
                 │ 4096 buckets, each:                  │ (direct server return)
                 │ first hop + second hop               │ no socket for this packet?
                 ▼                                      ▼ forward once to the second hop
            controller: reads load signals, moves buckets (swaps first)
```

- **Director** (`bpf/director.h`, `director_tc.c`): hashes each packet's
  4-tuple into one of 4096 buckets. Each bucket names a first-hop and a
  second-hop backend. The director encapsulates the packet (UDP + a 12-byte
  header that carries the second hop) to the first hop. It keeps no
  per-connection state. Replies go from the backend straight to the client.
- **Redirector** (`bpf/redirector.c`, TC on every backend): keeps the packet
  if this backend has a socket for it, if it's a SYN, or if it carries a
  valid SYN cookie. Otherwise it forwards the packet once to the second hop.
  So when a bucket moves, connections opened before the move still reach
  their old server.
- **Controller** (`internal/controller`, `cmd/controller`): every 1–2 s it
  reads one load signal per backend (`internal/signals`) and moves buckets
  from loaded to lighter backends. Most moves just exchange a bucket's two
  hops (a *swap*), which can't strand a connection. A bucket that was
  reassigned isn't changed again for 20 s.
- **Tuning:** one rule for every signal: the averaging period and dead band
  come from each signal's measured noise and a calibrated simulator
  (`cmd/ctlsim`, `cmd/ctltune`; D36).

Every choice and its reasons: [docs/decisions.md](docs/decisions.md).

## What we borrowed and what's ours

| Idea | From | What we changed |
|---|---|---|
| Hash into a power-of-two table of buckets, over 100× the backends | Unimog | One dedicated LB, not one on every server (D5), so forwarding doesn't mix into the CPU we measure |
| Two hops per bucket, second hop carried in the packet | Unimog | Our own 12-byte header instead of GUE (D7) |
| Second-hop forwarding ("daisy chaining") | Beamer, through Unimog | Two hops only, no generation numbers (D19); a SYN-cookie check in the keep rule (D8) |
| Swapping a bucket's two hops moves new connections and leaves old ones alone | Unimog | Same trick; here nearly every move is one |
| Load feedback: a control loop adjusting bucket counts | Unimog (it uses processor utilisation) | Signals are plugins, plus a 20-s dwell and caps (D32) |
| Rendezvous hashing for first and second hops | GLB | Only for the initial table, so swaps are available from the start (D6) |
| A large lookup table instead of per-connection decisions | Maglev | The general idea only: not its permutation filling or connection tracking, and our table's details follow Unimog |
| Requests in flight as a load signal | Prequal | RIF alone, read every 1–2 s to move buckets, not per-request probing of RIF and latency. Dividing by cores was our change, and it lost |

Ours, meaning our work in this project and not a claim that nobody did it first:
- **The LB at TC, for laptops with no native XDP** (D35). Unimog's forwarder is XDP. All the
  numbers below are on veth on one laptop; the wifi setup (D39) has only run on emulated machines.
- **One noise-based tuning rule across signals** (D36), so that comparing signals doesn't
  compare our tuning effort. Live churn turned out higher than its simulator predicted.
- **CPU vs RIF on unequal cores in one bucket controller** (RQ3), with the two RIF definitions.
  It's a comparison on our setup, not a test of Prequal.

## Main results

All on one laptop, four backends of unequal speed, 1–3 runs per
configuration, and differences under ~5% are beyond what we can resolve. Details,
run counts and caveats: [docs/results.md](docs/results.md).

Two table names used below: the **oracle** is proportional to each backend's
capacity measured alone, before any disturbance (nobody searched for the best
split, and some controller tables measured above it). **Hindsight** is proportional
to the capacities during one known disturbance.

- **RQ1:** the oracle uses **95–97%** of the summed capacity, shares by core count
  **87%**, equal shares **63%** (1 run each). Tables found by the CPU and total-RIF
  controllers reach **91–93%** on average, single CPU tables 87–100%. So they come
  close to the oracle, possibly a few points below. The same shares without the
  LB reach ~100%, so most of the missing 3–5% appears only through the LB; we
  didn't separate why.
- **RQ2:** when a server loses capacity, the controller is back within the
  SLO in **5–45 s**, while the oracle stays over it (a step: 12% errors) until the
  disturbance ends. Against that out-of-date table the controller does better at every
  flapping period we tried, 5 to 60 s, and for a single 120-s neighbour. **But a static
  table that knows the disturbance beats every controller** where we measured it
  (0–4% of windows over the SLO, against 8–50% for CPU and RIF). What the controller gives is not
  having to know which server, when or how much. H3: its first half is consistent
  with this; its second half is untested, since no disturbance we ran was faster
  than the control period and connection lifetime.
- **RQ3:** CPU utilisation and total requests in flight perform alike (S6, interleaved:
  92.7% vs 90.9%, 3 runs each, no difference we can resolve). Dividing RIF by core
  count (our first choice) loses 6 points of capacity. cgroup CPU is blind to a
  neighbour in another cgroup. PSI only sees overload when the server queues in the
  kernel. The RIF controllers keep moving buckets back and forth after recovering; CPU
  settles.
- **RQ4:** **0 broken connections** in 34 live runs and ~75,000 bucket moves, which
  predate a startup fix for a race they didn't hit (D40), and in 4 more on the final
  code (antagonist scenario, ~6,200 moves). In a separate run with
  the redirectors not forwarding, 36 long-lived connections broke, 196 failed requests
  in all (1 run). Almost every move is a swap, which is safe only because the second
  hop forwards.
- Running the LB at TC instead of XDP shows no extra CPU per packet on our hardware
  (no NIC with native XDP; 1 run, coarse: 39 vs 45 and 64 vs 78 ticks of 10 ms).

**On four real laptops** (phone hotspot, close to identical hardware, 40 ms requests, 1 run per
configuration; [not comparable](docs/results.md#real-laptops) with the numbers above): 0 of 20
long-lived connections broke with forwarding on, 12 of 20 with it off. The CPU controller found
nearly the oracle's table by itself, and held p99 lower than the static table under an antagonist
(2.9 s vs 12.9 s, most of it waiting for a free client connection), but nothing got back under the
800 ms SLO during the antagonist. Capacities can't be ranked: one table measured twice differed
by about 3× in p99 at the highest rate.

## Running it on one laptop

Needs: Linux with BTF and kernel ≥ 6.6 (TCX), cgroup v2 + systemd, clang, libbpf
headers (`libbpf-dev` or `libbpf`), Go, Python 3 with matplotlib, iproute2, ethtool,
curl, sudo. The Go tools build on Linux only (they use namespaces and CPU affinity
system calls). Everything runs in network namespaces on one machine. CPU placement is in `config.env` and
`backends.conf`; edit them for a different laptop.

```bash
make                               # BPF programs (clang) + Go tools into bin/
make test && make test-bpf         # unit tests; golden-packet tests in the kernel (sudo)

# Try it by hand
sudo scripts/topology.sh up        # namespaces cli, rtr, lb, beP1 beP2 beE1 beE2
sudo scripts/topology.sh check     # reachability + where packet processing runs
sudo bin/lbctl load && sudo bin/lbctl table equal
sudo ip netns exec cli curl http://10.2.0.1:8080/work   # a request through the LB
sudo bin/lbctl stats
sudo scripts/topology.sh down      # also unloads the LB

# Functional checks (~1–3 min each)
sudo python3 bench/e2e_lb.py
sudo python3 bench/move_test.py    # RQ4: connections through a 50% table change
sudo python3 scripts/demo.py       # live demo of that, ~40 s, big numbers, cleans up after itself
go run ./cmd/ctlsim                # controller simulator, ~1.5 min

# The campaign (docs/campaign.md): add --dry first, a few minutes per session
R=results/campaign-$(date +%Y%m%d)
sudo python3 bench/campaign.py s1 --root $R      # capacities, signal noise
python3 bench/tune_signals.py $R                 # tuning rule (no sudo)
sudo python3 bench/campaign.py s2 --root $R      # RQ1
sudo python3 bench/campaign.py s2-extra --root $R       # RQ1: total-RIF tables (run 1)
sudo python3 bench/campaign.py s2-rif-total --root $R   # RQ1: total-RIF tables (runs 2, 3)
sudo python3 bench/campaign.py s3 --root $R      # RQ2-4: step, antagonist
sudo python3 bench/campaign.py s4 --root $R      # flapping, sibling, PSI, hook cost
sudo python3 bench/campaign.py s5 --root $R      # total RIF, redirector contrast, contending sibling, flapping sweep
sudo python3 bench/campaign.py s6 --root $R      # denominators, hindsight tables, CPU vs total RIF
sudo python3 bench/campaign.py s7 --root $R      # antagonist on the final code: its own "0 broken"
python3 bench/analyze_campaign.py $R             # $R/summary.md and plots
```

The full campaign took about 7 hours of machine time here. Each session
applies `scripts/tune.sh` (turbo off, fixed clocks, desktop confined to two
CPUs) and always undoes it at the end, so the laptop feels slow while a run
is going. If a run is killed hard: `sudo scripts/tune.sh undo`.

## Running it on four laptops

One client, one LB, two laptops with two backends each, over wifi, driven
from the client by one command (~50 min; `--quick` took 32). It writes the same files, so the
same analysis and plots work.

```bash
cp cluster/cluster.conf.example cluster/cluster.conf   # addresses, interfaces, CPUs; commit it
python3 bench/cluster.py cluster/cluster.conf --dry    # ~10 min
python3 bench/cluster.py cluster/cluster.conf          # add --quick for ~33 min, at a cost (runbook)
```

Read [docs/cluster-runbook.md](docs/cluster-runbook.md) first: ssh and
sudo setup, checks before starting (client isolation, MTU, wifi power
save), and what will differ from the single-laptop numbers. It has run once on real
laptops, with `--quick` ([results](docs/results.md#real-laptops)).

## Documents

- Problem, research questions, hypotheses: [docs/plan.md](docs/plan.md)
- Every design decision with its reasons: [docs/decisions.md](docs/decisions.md)
- How we measure: [docs/measurement-protocol.md](docs/measurement-protocol.md), [docs/campaign.md](docs/campaign.md)
- **Results by research question: [docs/results.md](docs/results.md)**
- Four laptops: [docs/cluster-runbook.md](docs/cluster-runbook.md)
- What happened when: [docs/journal.md](docs/journal.md)
- Papers and blog posts: [references/](references/README.md)
- Packet captures for each hop: [results/captures/](results/captures/README.md)

## Layout

```
config.env, backends.conf   testbed layout (CPUs, addresses): the single source of truth
scripts/topology.sh         build/tear down the namespace testbed (+ RPS placement)
scripts/tune.sh             runtime-only system tweaks for measurement runs, with exact undo
scripts/lib.sh              helpers shared by the shell scripts
scripts/demo.py             live demo for a talk: connections through a table change, with and without second hops
scripts/cluster/            per-machine setup for the four-laptop runs (+ emulation)
cluster/                    example and emulated cluster configuration
bpf/director.h              the LB: bucket table, encapsulation; director_tc.c (used), director_xdp.c (hook comparison)
bpf/redirector.c            backends, TC ingress: keep (decapsulate) or forward to the second hop
bpf/lb.h, xdp_pass.c        definitions shared by LB and backends; the pass program veth needs for XDP_TX (D9)
bpf/resets.c, cmd/resetmon  every TCP reset to/from the VIP, with its bucket
cmd/lbctl                   load/unload the LB, publish tables, show counters
internal/controller         the bucket controller (update rule, safety rule); cmd/controller runs it live
internal/signals            load signals: whole-core CPU, RIF, cgroup CPU, PSI
internal/lb, testbed, work  table + maps + golden tests; entering namespaces; the CPU work of a request
internal/sim, cmd/ctlsim    controller simulator; cmd/ctltune picks each signal's settings
cmd/backend                 test server: CPU work per request, exports its counters
cmd/loadgen                 open-loop load generator (latency from the scheduled time)
cmd/antagonist              noisy neighbour: burns CPU or walks memory at a set duty cycle
cmd/sigmon                  logs every signal for every backend once per second
bench/campaign.py           the campaign, one function per session (+ analyze_campaign.py, tune_signals.py)
bench/cluster.py            the short campaign on four machines
bench/testbed.py            shared helpers: where and how each command runs
bench/e2e_lb.py, move_test.py, noise_check.py   functional tests, RQ4 move test, phase-0 noise check
bench/controller_smoke.py   smoke test of the live controller, per signal
bench/service_time.py       does a request cost the same at low and high load? (tuning check)
bench/sysmon.py             logs temperature, power, throttling and per-CPU load during a session
bench/analyze_noise.py      report of a noise-check run
results/                    one folder per run (summaries in git, raw per-request data ignored)
```
