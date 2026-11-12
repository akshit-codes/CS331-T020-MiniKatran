# Decisions

One entry per decision: what we decided, why, and what we rejected.
Newest decisions go at the bottom. If a decision changes, add a new entry
that supersedes the old one instead of editing history.

---

## D1 — Problem statement (2026-11-01)

> On a cluster where servers have unequal and changing capacity, an L4 load
> balancer that can only steer *new* connections should carry as much load as
> possible within a latency and error SLO (p99 ≤ L, error rate including
> resets ≤ E), without breaking live connections and without per-connection
> state on the LB.

**Why:** the old project (mini-katran) copied Katran without a problem. This
one starts from a need that real systems have (Cloudflare Unimog: mixed
hardware generations, uneven connection cost, background work) and is narrow
enough to measure. "Can only steer new connections" is the constraint that
makes it interesting and separates it from per-request balancers like Prequal.

## D2 — Headline metric: usable capacity fraction (2026-11-01)

*Capacity at SLO* = highest offered load (req/s) with p99 ≤ L, errors ≤ E and
≥ 98% of the offered load served. *Usable capacity fraction* = capacity at SLO
through the LB ÷ sum of each backend's own capacity at SLO (measured directly).

**Why:** it is what operators provision for, it folds imbalance, tail latency
and broken connections (they count as errors) into one number, and it has a
known ceiling of ~1.0. Max/mean load (Maglev's overprovision factor) stays as a
*diagnostic*, not the outcome. Report results for 2–3 values of L.

## D3 — Baselines (2026-11-01)

Equal weights → static by core count → **static oracle** (weights ∝ each
backend's measured capacity at SLO) → dynamic controller (one run per signal).
The dynamic controller must beat the static oracle; beating only the weaker
baselines would be a straw man. In a static scenario it must *match* the
oracle within noise.

(2026-11-06: "oracle" is only our name for this table. It knows the capacities
*before* any disturbance and nobody searched for a better split, so it is neither
the best static table nor one that knows the future. After a disturbance it is
simply out of date, so beating it there says less than this entry assumed; S6
adds a static table re-weighted for each disturbance, D40.)

## D4 — Research questions, ranked (2026-11-01)

1. **RQ1** Usable capacity fraction of each policy, with static and with changing capacity.
2. **RQ4** Does the second hop keep connections alive through table changes; how often does a second bucket change break them (trade-off with controller aggressiveness).
3. **RQ2** Under which disturbance timescales does the dynamic controller beat the static oracle, and is it stable (settling time, oscillation amplitude, bucket changes/min)?
4. **RQ3** Which signal works best: whole-core CPU vs requests in flight (main); cgroup CPU, PSI on the key scenarios (a connection-count signal was planned and dropped, D10).

RQ1 (static part) and RQ4 (first part) don't need the controller, so they are
the guaranteed results (cut line = end of phase 2/3).

## D5 — One dedicated, stateless LB (2026-11-01)

Not Unimog's "LB on every server". **Why:** running forwarding on the backends
would mix LB work into the CPU numbers we use as both signal and capacity
measure. The research question doesn't depend on where the LB runs. The LB is
stateless (reads the controller's table only), so a second LB behind a
multipath route is a cheap optional extension (LB-failure demo).

## D6 — Bucket table (2026-11-01)

- Power-of-two array, 4096 buckets (> 100× backends, Unimog's rule); bucket = jhash(4-tuple, fixed seed) & (N−1). Never use `skb->hash` (random per boot).
- Each bucket: **first hop** (gets new connections) and **second hop** (gets packets the first hop has no socket for). Named by role, not "current/previous owner", because swaps make "previous" untrue.
- Second hops initialised from the rendezvous-hash second choice (GLB-style), so the controller has free swaps from the start.
- Whole table swapped atomically via map-in-map (`ARRAY_OF_MAPS`); table generation stored in an extra slot of the inner map.

## D7 — Encapsulation format (2026-11-01)

Outer Ethernet | IPv4 | UDP (dst `ENCAP_PORT`, src = per-flow hash, checksum 0)
| 12-byte header `{u8 version, u8 flags, be16 bucket, be32 second_hop, be32 table_gen}`
| original IPv4 packet. 40 bytes overhead → fabric MTU 1600, client MTU 1500.
`F_HOPPED` flag prevents forwarding loops; `second_hop = 0` means none.
`bucket` and `table_gen` are for debugging and RQ4 attribution only.
No ICMP/fragment handling (fixed MTUs avoid the need).

## D8 — Redirector in TC on each backend (2026-11-01)

Keep locally if: hopped, SYN, `bpf_skc_lookup_tcp` finds a non-listener
(established, SYN_RECV, TIME_WAIT), or listener + valid SYN cookie
(`bpf_tcp_check_syncookie`). Otherwise forward to the second hop (rewrite
outer, set `F_HOPPED`, `bpf_redirect_neigh`); if there is none, keep locally.
Decap with `bpf_skb_adjust_room(-40, BPF_ADJ_ROOM_MAC)`.
**Why TC, not XDP:** tcpdump works, `bpf_redirect_neigh` exists, and most
packets pass through untouched (Cloudflare's reasons for `cls-redirect`).
**Why the syncookie check:** under load the listen queue overflows, the final
handshake ACK then matches no socket and would be wrongly forwarded, which
would look like broken connections exactly during capacity tests.

## D9 — LB in XDP, native mode on veth (2026-11-01)

Native XDP works on veth; generic mode is only a fallback. Attach an
`XDP_PASS` program to the bridge-side peer, or `XDP_TX`'d frames are dropped.
Header building/parsing lives in shared inline functions on (data, data_end),
so a TC version of the LB is a one-day fallback.

## D10 — Load signals (2026-11-01)

All signals are reported by the backends (a stateless DSR LB can't see
requests or connection ends) and read as **cumulative counters**, differenced
per control period, then normalised to "load relative to capacity" uᵢ:

| Signal | Counter | uᵢ |
|---|---|---|
| Whole-core CPU (main) | `/proc/stat` busy of the backend's cores | Δbusy / (cores × P) |
| Requests in flight (main) | app's ∫RIF dt (`rif_seconds_total`) | (Δ∫RIF / P) / cores |
| Cgroup CPU | `cpu.stat usage_usec` | Δusage / (allocation × P) |
| PSI | `cpu.pressure some total=` | Δtotal / P |
| ~~Connections~~ | ~~established sockets~~ | **Dropped:** never built, there is none in `internal/signals` |

Main sweeps: whole-core CPU vs RIF. The others only on the key scenarios.
Note: "Prequal-style" here means Prequal's *signal* in a slow controller, not
Prequal's per-request probing.

## D11 — Controller design (2026-11-01)

Equalise uᵢ around ū = Σcᵢuᵢ/Σcᵢ. target nᵢ = nᵢ·(ū/max(uᵢ,U_MIN))^GAIN,
clamped per step, floor per backend, rescaled to N, integerised with largest
remainder. Moves: free swaps `[d|r]→[r|d]` first, then reassign `[d|x]→[r|d]`
preferring empty second slot, then oldest `second_since`. Anti-flip-flop:
hysteresis (δ_on/δ_off), dwell time per bucket, global and per-backend change
caps, low-load gate, floor. Period P ≥ typical connection lifetime (load only
moves as connections turn over). Starting values in the controller section of
the design discussion (GAIN 0.3, P 2–5 s, cap 2%/period, δ from measured noise).
Develop against an offline simulator first.

## D12 — How we make backends unequal (2026-11-01)

- **Slower cores:** real P-cores vs E-cores, plus the backend app's `-slowness` factor.
  *Not* `cpu.max`: a quota makes a saturated backend look idle to whole-core CPU, an emulation artefact that real slow cores don't have.
- **Fewer cores:** smaller cpuset.
- **VM-style allocations (Prequal's scenario):** `cpu.max`, only in that scenario.
- **Changing capacity:** antagonists in their own cgroup on the backend's cores.

## D13 — Key scenarios for RQ3 (2026-11-01)

- *Shared-core antagonist* (separate cgroup, same cores): predicted to fool cgroup CPU, not PSI or whole-core CPU.
- *SMT-sibling antagonist* (on the idle hyperthread of a P backend's core): invisible to whole-core CPU, cgroup CPU and PSI; only RIF should see it.

Hypothesis to state up front: equalising *utilisation* maximises throughput,
but our SLO is latency; when core speeds differ, a slow backend hits the
deadline at lower utilisation, so CPU-equalising may lose capacity where RIF
doesn't.

## D14 — Single-laptop testbed and CPU plan (2026-11-01)

Network namespaces `cli`, `rtr` (router + bridge `fab0`), `lb`, `beP1 beP2 beE1 beE2`.

| Role | CPUs |
|---|---|
| Housekeeping (OS, desktop, monitoring) | 0,1 |
| Load generator | 2,4 |
| Router + LB | 6 |
| beP1 / beP2 (2 P-cores each) | 8,10 / 12,14 |
| beE1 / beE2 (one E-cluster each) | 16–19 / 20–23 |

Never two roles on the two threads of one P-core; siblings stay idle.
**RPS** places receive processing (veth traffic is otherwise processed on the
sending CPU): client eth0 → loadgen CPUs, router cli0 → LB CPU, backend eth0 →
backend CPUs. Verified with `topology.sh check`.

## D15 — System tweaks are runtime-only and undoable (2026-11-01)

`scripts/tune.sh apply|undo`: turbo off, governor `performance`, user/system
slices confined to housekeeping CPUs, device interrupts to housekeeping.
Original values recorded line by line in `/run/cs331-lb/tune.state` before each
change; undo restores in reverse order (tested: restores exactly). Nothing on
disk, so a reboot also undoes it. Applied only during measurement runs,
because VS Code etc. stay open between runs. The power profile is checked but
not changed (power-profiles-daemon persists it to disk).

## D16 — Our own load generator and monitor (2026-11-01)

`cmd/loadgen` (Go, open loop, latency from the *scheduled* time) instead of
wrk2; `bench/sysmon.py` instead of turbostat/mpstat. **Why:** wrk2 can't log
per request or control connection lifetime (both needed later); neither tool
was installed; and our own versions are short enough to explain line by line.

## D17 — Backend app v0 (2026-11-01)

Go HTTP server. Each request does a fixed amount of *work* (units of dependent
multiply + random reads in a 1 MiB table), not a fixed time, so slower cores
really take longer. Exports `rif_seconds_total` (∫RIF dt) on a separate
metrics port so reading metrics is never counted as a request. Work per
request is chosen by calibration: COST_MS of CPU on a P-core with turbo off.

## D18 — Noise-check workload and SLO (provisional, 2026-11-01)

1 ms of CPU per request on a P-core, Poisson arrivals, 64 keep-alive
connections. SLO: p99 ≤ 10 ms, errors ≤ 0.1%, served ≥ 98% of offered.
The full workload model (new-connection rate, requests per connection, cost
distribution, connection lifetime) still has to be fixed before phase 2.

## D19 — Scope cuts (2026-11-01)

TCP over IPv4 only; one VIP; two hops (no Beamer generation numbers or longer
chains); no automatic health checks (drain on command); no ICMP/fragments; no IP
options (D40).

**No UDP, because the second hop depends on TCP state.** The redirector decides
"keep or forward" from what the backend knows about the connection: a SYN is
new, an existing socket means it's ours, a valid SYN cookie means it's ours even
without a socket. UDP has no handshake and no cookies, and an unconnected UDP
socket doesn't tell a flow from any other, so nothing says whether a packet
belongs here or at the old first hop. Doing it would take connected UDP sockets
(one per flow, so a socket lookup answers the question) or, for QUIC, connection
IDs that name the server. Either is a different keep rule, not a small addition.

## D20 — Fixed clock and no deep idle states on experiment CPUs (2026-11-01)

Extends D15. On every CPU except housekeeping, `tune.sh apply` also sets
`scaling_min_freq = scaling_max_freq` (base clock, since turbo is off) and
disables idle states with exit latency > 10 µs (this laptop's C2 = 127 µs,
C3 = 1048 µs; C1 = 1 µs stays). **Why:** without it, requests were up to ~2×
slower at *low* load than at high load (waking from C3 plus cold caches), which
would make service time depend on load for power-management reasons rather
than real capacity. Measured with `bench/service_time.py` (journal 2026-11-01).
Still runtime-only and exactly undone.

## D21 — Coupling check uses CPU antagonists, not extra load generators (2026-11-01)

To ask "does one backend's capacity depend on how busy the others are"
(shared L3, power), the other backends' cores are loaded with
`cmd/antagonist` (70% duty, same work as requests) instead of extra network
traffic. **Why:** extra load generators shared the router's CPU and measured
"router busy" instead (first smoke test: −14%). Router/LB-CPU load is a real
but separate effect, checked once the LB exists (LB CPU must stay far from saturation).

## D22 — Load generator spins for the last 2 ms before each send (2026-11-01)

Go timers wake up to ~1 ms late; because latency is measured from the
scheduled time, that added ~0.5 ms of fake latency to every request (send
delay p50 0.5 ms). The dispatcher now sleeps until 2 ms before each send and
spins the rest (send delay p50 ≈ 8 µs). Costs one loadgen core while running.

## D23 — Keep the P-core low-load slowdown; it is real (2026-11-01)

Even tuned, a P backend's requests take ~1.6 ms at 1% load vs ~1.05 ms at
30%+ load; E backends are flat. Cause (tested): requests bounce between the
backend's two P-cores, which have *private* L2 caches, so the 1 MiB working set
is cold at low load; on one P-core the effect nearly vanishes. E-core clusters
share one L2. This is a genuine hardware difference, not a tooling artefact,
and it fades near capacity where SLO decisions happen, so we keep it and note
it when interpreting low-load numbers. (Re-checked after D24: still there.)

## D24 — Backend = pinned worker pool with a FIFO queue (2026-11-01)

Supersedes the goroutine-per-request design of D17. One worker thread per
allowed CPU, each pinned to its CPU (`sched_setaffinity`), serving one FIFO
queue; `GOMAXPROCS = workers + 1` so network handling has its own Go processor.
**Why:** with unpinned threads the kernel sometimes ran two workers on the same
CPU; the service-time histogram showed ~10% of P-backend jobs taking 1.5–3 ms
instead of ~1.05 ms, and p99 at 50% load was 8–15 ms (2-CPU P backend). Pinned:
0.3% slow jobs, p99 3.7 ms, and all backends now behave like textbook queues
(p99 grows smoothly with load, scales with service time). It is also the
classic one-worker-per-core server design, so it's easy to explain.
The backend now also exports the work-time histogram (useful later to show
what antagonists do to service time).

## D25 — PSI only sees overload if the server queues in the kernel (2026-11-02)

The backend (D24) has exactly one pinned worker per CPU and keeps waiting
requests in its own FIFO queue. When it is overloaded by *requests*, the
queue grows inside our process and the kernel run queue stays short: each CPU
has one runnable worker, so `cpu.pressure` stays near zero (apart from the
network goroutine's thread and Go runtime threads). PSI rises only when
something *else* competes for the CPUs, like an antagonist. Whole-core CPU and
cgroup CPU have a related limit: both stop at 100%, so they can't tell
"slightly over" from "far over" capacity. Only RIF measures the queue.

This is a property of server architecture, not just of our code. Event-loop
and fixed-pool servers (nginx, Envoy, most thread-pool servers) queue in user
space; thread-per-request servers queue in the kernel. So a PSI result
measured on one backend design says little about PSI in general.

**Decision:** keep the pool backend as the main backend (it is what all
capacity numbers are measured with). In phase 4, for the RQ3 runs that
include PSI only, add a second backend mode where waiting happens in the
kernel: many more unpinned workers than CPUs (e.g. 64) on the same FIFO
queue, so excess requests wait as runnable threads. Report PSI for both modes.
Expected: PSI tracks request overload only in kernel-queue mode, RIF in
both. Compare signals *within* a mode, never capacities *across* modes (the
kernel time-slices the threads, which changes the latency distribution and
cache behaviour; D24 showed unpinned threads also cost throughput).
**Rejected:** documenting it as a limitation only. Then RQ3 could only say
"PSI fails here", without showing why.

## D26 — Client computes its own TCP checksums (2026-11-02)

`topology.sh up` runs `ethtool -K eth0 tx off` in namespace `cli`.
**Why:** a veth has no wire, so by default the sender leaves the TCP checksum
unfilled and marks the packet "checksum still to do" (checksum offload). The
mark travels with the packet inside the kernel, but not through XDP_TX: the
director sends a new frame, and the backend saw an unfilled checksum and
dropped every SYN (`TcpInCsumErrors`, found in the first end-to-end try). A real client's NIC
fills in the checksum before the wire, so turning the offload off makes the
veth behave like real hardware. The cost is a little CPU on the loadgen cores.
Rejected: fixing checksums in the director. That would cost every packet work
a real LB never has to do.

## D27 — Director details, refining D6/D7 (2026-11-02)

- Hash: `jhash_3words(saddr, daddr, sport<<16 | dport, 0x5eed)`, all in host
  byte order; bucket = low 12 bits. Mirrored in Go (`internal/lb`), so
  user space can tell which bucket a flow is in.
- Outer UDP source port = `0xc000 | hash>>18` (49152–65535): different flows
  get different outer ports, so the backend's RPS spreads them over its CPUs.
- Outer IPv4: DF set, TTL 64, no UDP checksum (the inner TCP checksum covers
  the data). The inner packet is not changed at all (no TTL decrement).
- The table generation is stored in `.first` of the extra slot 4096.
- VIP, port and the LB's address are load-time constants (`volatile const`,
  set by the loader). The outer source MAC is the original destination MAC,
  since the frame was addressed to the LB.
- Not for the VIP → `XDP_PASS`. For the VIP but not TCP to the port, or a
  fragment → drop. No table, or an empty bucket → drop. Every case has a counter.
- Static tables (until the controller's table generator exists): bucket
  counts by largest remainder, one contiguous run per backend (the hash
  already scatters flows), no second hops.

## D28 — Loader: Go with cilium/ebpf, pinned links (2026-11-02)

`bin/lbctl` loads the `.o` files built by clang (no code generation),
attaches the director (native XDP, never a silent fallback to generic),
the peer `XDP_PASS` and the backends' decap (TCX ingress), and pins links
and maps under `/sys/fs/bpf/cs331`. Programs therefore stay attached after
lbctl exits, and `lbctl unload` (or `topology.sh down`) deletes the pins,
which detaches everything. To attach inside a namespace, lbctl switches one
locked OS thread into it (`setns`). **Why Go/cilium-ebpf:** same language
as the rest of our tools, and the same library runs the golden-packet tests
(`BPF_PROG_TEST_RUN`). Rejected: iproute2 loading (`ip link set xdp obj`),
which can't fill a map-in-map, and libbpf in C, which is more code to explain.

## D29 — Redirector details, refining D8 (2026-11-02)

- Decision order: `F_HOPPED` → no second hop → SYN (no ACK) → socket lookup
  (`bpf_skc_lookup_tcp` with the inner 4-tuple; any non-listener state, i.e.
  established, half-open request or TIME_WAIT, counts as ours) → listener only:
  `bpf_tcp_check_syncookie` → otherwise forward. Each outcome has a counter.
- Forwarding: outer source = the backend's own address, outer destination =
  second hop, `F_HOPPED` set, then `bpf_redirect_neigh` out of the same
  interface (the kernel fills in the MACs). One program load per backend,
  because each needs its own address and its own counters.
- Malformed encapsulated packets (unknown version, inner not TCP, inner IP
  options) are dropped. The director never produces them.
- `CHECK_SYNCOOKIE` is a load-time switch (`lbctl load -no-syncookie-check`)
  that exists only to show what breaks without the check (move test, run C).
- Director: inner length from `tot_len`, Ethernet padding trimmed before
  encapsulating (`bpf_skb_change_tail` at TC, the hook in use since D35;
  `bpf_xdp_adjust_tail` in the XDP build), and packets whose `tot_len`
  is longer than the frame are dropped. This matters on real NICs: a bare ACK is
  54 bytes and gets padded to 60.

## D30 — Second hops for testing: "old first hop" (2026-11-02)

Until the controller's table generator exists (D6/D11), `lbctl table`
derives second hops from the table being replaced: a bucket whose first hop
changes gets the old first hop as second hop, and an unchanged bucket keeps its
second hop (`lb.WithSecondHops`). `-second none` publishes without second
hops (the RQ4 baseline). This is the minimum the move test needs. The
rendezvous-based initial second hops come with the controller.

## D31 — Reset monitor: packet tuple from tp_btf, attribution from namespace counters (2026-11-02)

`bpf/resets.c` uses the *typed* tracepoint `tcp_send_reset(sk, skb, reason)`
and reads the 4-tuple from the packet being answered (`skb`), with CO-RE for
the three kernel fields it needs. **Why not the classic tracepoint:** when a
packet hits only the listener, the classic tracepoint reports the listener's
own addresses (`[::]:8080`, IPv6 for Go's dual-stack listener), not the
connection. Our first version therefore saw none of the resets that matter (journal).
Received resets use the classic `tcp_receive_reset`, whose IPv4 fields are
filled for dual-stack sockets too and which gives a socket cookie. Which backend
sent a reset is not in the tracepoint data. We take it from each namespace's
`TcpOutRsts`.
**Counting broken connections:** distinct client sockets that received a
reset (equal to the loadgen's errors in every run so far), not the total of
resets: a broken connection also produces follow-up resets (journal 2026-11-02).

## D32 — Controller as built, refining D11 (2026-11-02)

`internal/controller` is pure logic (no kernel, no clock), driven by
`cmd/controller` live and by `internal/sim` offline.
- **Initial table:** rendezvous ranking per bucket. First hop = highest-ranked
  backend that still has room (counts exact, largest remainder), second hop
  = highest-ranked other backend. Zero-weight backends get neither role.
- **Per period:** ū = Σcᵢuᵢ/Σcᵢ (cᵢ = cores). Gates: low load (ū < 0.2),
  then hysteresis (start above 10% max deviation, stop below 5%). Target
  nᵢ·(ū/max(uᵢ,0.05))^0.3, clamped to ±25%, floor 16 buckets, largest remainder.
  Per-backend cap 40 buckets per period (1%), global cap 81 (2%).
- **Moves:** only *settled* buckets (first hop unchanged for ≥ `Dwell`), oldest
  first. Swaps `[d|r]→[r|d]` first, then reassignments `[d|x]→[r|d]` (no
  second hop first, then oldest).
- **Safety by construction:** while no connection outlives `Dwell`, the
  backends that can hold a bucket's connections are its first hop and its
  previous first hop. Every move keeps the current first hop in the bucket, and
  a bucket is only touched after its previous first hop's connections have
  ended. So no move can strand a connection. Unit-tested
  (`TestNeverChangesABucketWithinDwell`: the load keeps reversing, with so few old
  buckets that only recent ones are left to move; it fails if the check is removed)
  and checked by the simulator's own bookkeeping.
  `Dwell` = 20 s for now; it must be ≥ the longest connection in the
  workload model (open item).
- **Overload override** (`SaturatedAt`, off by default): a backend at ≥ 0.97
  while the mean is below counts as out of band. It fixes the stuck case in the
  simulator but adds churn near capacity (journal). Decide in phase 3.

## D33 — Simulator: connection-level model, p99 as the yardstick (2026-11-02)

`internal/sim` models connections, not requests: each lands on its bucket's
first hop and stays there for its lifetime, so load moves only as connections
turn over. Signals: CPU = min(ρ,1); RIF = in service + M/M/c queue below
capacity + a backlog growing above it; "true" ρ as the unreachable
reference. Results are judged by **p99 response time** (M/M/c per backend,
mixed by load share), not only by utilisation imbalance, because equal
utilisation is the CPU signal's own goal and would bias the comparison
(Prequal's point). Safety is counted independently of the controller: first-hop
changes within the 20 s lifetime bound, and connections whose backend was
dropped from their bucket. A no-protection control run proves the checker fires.
Limits: RIF is the queue's *mean* plus noise, not a real queue; signal noise
is a parameter and has to be calibrated from measurements (journal).

## D34 — Signal plugins and live controller (2026-11-02)

`internal/signals`: one interface (`Read() []float64`: load relative to
capacity since the last read), counters differenced between reads. Whole-core CPU
from `/proc/stat` (busy/total ticks of the backend's CPUs); RIF from the
backend's `rif_seconds_total` (Δ/Δt/cores), fetched over a socket opened on a
thread switched into the backend's namespace (`testbed.InNetns`); cgroup CPU
from `cpu.stat usage_usec` and PSI from `cpu.pressure some total` of
`cs331.slice/cs331-<name>.service`; `Fake` for tests. `cmd/controller`
publishes through the same map-in-map swap as `lbctl` (`lb.Publish`) and logs
one CSV line per period.

## D35 — The LB runs at TC ingress, on purpose (2026-11-02; replaces D9, retires D26)

**Decision:** the director is a TC ingress program (`bpf/director_tc.c`,
attached with TCX). All experiments use it. The XDP version
(`bpf/director_xdp.c`) stays only for one side measurement of per-packet
cost between the two hooks on veth. It costs little to keep: both versions share
all logic (`bpf/director.h`) and pass the same golden tests with the same
expected bytes.

**Why:** D9 chose XDP partly for a physical demo on a laptop with native XDP.
None of our laptops has one (no wired NIC with an XDP driver), and Raspberry
Pis are out. So on real hardware the LB would always run in *generic* XDP,
which gives us XDP's limits without its speed:
- generic XDP runs after the kernel has built its packet buffer (sk_buff),
  the same point as TC, so it saves nothing (the XDP paper describes it only
  as running "at reduced performance");
- XDP sees a flat frame, not the buffer, so what the kernel knew about the
  packet is lost on XDP_TX: a checksum left to offload (our D26 bug) and large
  GSO/GRO packets, which generic XDP has to linearise and which then don't fit
  the MTU logic;
- frames sent with XDP_TX never pass the stack's capture points, so tcpdump on
  the LB doesn't see what it sends.
TC keeps the sk_buff: `bpf_skb_adjust_room` with the ENCAP flags
(`ENCAP_L3_IPV4 | ENCAP_L4_UDP | ENCAP_L2(12)`) marks the packet as a UDP
tunnel, so the kernel can still segment a 64 KB GSO packet and finish
offloaded checksums correctly after we encapsulate it. tcpdump sees both
directions, it works on any NIC, and the whole data path (director and
redirector) now uses one kind of hook.

**What TC costs us compared with native XDP:** on a NIC with an XDP driver,
XDP runs before the sk_buff exists. The XDP paper measures 24 Mpps on one core
for native XDP against 4.8 Mpps at the earliest point of the regular stack
(iptables raw), a 5× gap; TC sits at about that point. Our LB handles
tens of thousands of packets per second, far below either limit. What we give
up is a line-rate claim, which is not our research question. On veth the gap is
smaller anyway: veth's "native" XDP also starts from an sk_buff that the
peer handed over, which the side measurement will quantify.

**How:** both hooks write the MACs from the backends map and send the packet
back out of the same interface (`bpf_redirect` to egress / `XDP_TX`), so
they do the same work and produce identical bytes; only the way room is made
differs (`bpf_skb_adjust_room` vs `bpf_xdp_adjust_head`, `bpf_skb_change_tail`
vs `bpf_xdp_adjust_tail` for padding). Small headers-not-linear packets are
pulled in first (`bpf_skb_pull_data`). `lbctl load` uses TC; `-hook xdp`
attaches the XDP version plus the `XDP_PASS` peer program (D9's detail,
still needed there).

**D26 retired:** with TC, the client no longer needs `ethtool -K eth0 tx off`.
Checked: with checksum and segmentation offload back on, the client sends
1 MiB uploads as 64 KB GSO packets; they leave the LB encapsulated, the
redirector decapsulates them, and all 20 uploads arrive complete
(`bench/e2e_lb.py`); the move test gives the same results as with XDP. The
setting is needed again only for the XDP side measurement.

## D36 — One tuning rule for every signal (2026-11-02)

Each signal's period P and dead band come from the same procedure
([campaign.md](campaign.md)): measure its noise σ(P) at a fixed table, set
δ_on = k·σ(P) and δ_off = δ_on/2, simulate every P ∈ {1,2,4,8} s and
k ∈ {1.5,2,3} with the simulator's noise calibrated to σ(P), and pick, among
the settings with steady-state churn ≤ 1% of the table per minute, the
fastest recovery (no backend over capacity). k is searched rather than fixed
at 3: in a dry run a fixed 3σ band was wider than a saturated CPU reading can
ever deviate, so the CPU controller ignored a backend losing half its capacity. Gain, caps, floor, dwell, gate and override are identical for all.
**Why:** the simulator showed that noise decides how a signal behaves (RIF
chased noise at a band that suited CPU). Tuning each signal by hand would
make RQ3 partly a comparison of our tuning effort. With one rule, a noisier
signal gets a wider band or a longer period, which is a property of the
signal. Smoothing is the averaging window P; no separate EWMA, which would
be a second knob doing the same job. PSI is logged but not used as a
controller input: with our pool backend its values (~0.02–0.1) never pass the
low-load gate (D25).

## D37 — A short campaign instead of the full plan (2026-11-02)

About 10 hours remain for the whole project, so measurement gets ~2.5 h of
testbed time ([campaign.md](campaign.md)): 20-s measured steps, 5-probe
capacity searches (1.25% resolution), 2–3 repeats only where small
differences matter (oracle, controllers), 1 run where differences are
expected to be large (equal, by cores, extras). The stateful baseline and the
timescale sweep are dropped (two timescale points remain). Each experiment
states what it can and can't claim at this length. The key experiments are
rerun later on four laptops.
**Workload model** (the open item since D18): exponential request cost
(mean 1 ms of P-core CPU), matching the simulator's M/M/c model; Poisson
arrivals; 20 requests per connection for the churn class (lifetime well
under the 20 s dwell); plus 100 long-lived connections in dynamic runs,
which outlive the dwell and so measure RQ4's real trade-off. Heterogeneity:
beP1 at `-slowness 1.6`, so that neither *equal* nor *by cores* is right.
SLO bound L = 20 ms (10 and 50 ms read off the same probes): with
exponential cost an E-core request alone has a p99 of ~8.5 ms, so at 10 ms
capacity would mostly reflect single-request times rather than load. "Served"
counts completed requests against those actually scheduled, not the target
rate (Poisson counts in a 20-s window vary by ~1–3%).
Backend additions for this: `-cost exp`, runtime `/slowness?set=` (for the
step scenario, without restarting the backend and losing connections),
`work_units_total`. Harness logging: `cmd/sigmon` records all four signals
and per-backend rates every second, for the noise measurement and as
passive RQ3 extras.

**Correction to D33 (2026-11-02):** model RIF is now computed from the
period's *average* utilisation (plus the backlog), not from each 50-ms
instant: the M/M/c queue is so convex near capacity that the per-instant
version made model RIF ~3× noisier than RIF measured on the testbed (a real
queue can't follow 50-ms load wiggles). The earlier scenario results barely
change (RIF churn 665 → 614 moves/min static): RIF's churn there comes from
the queue's convexity amplifying real load differences.

**D36 revised (2026-11-02, after the simulator tuning, before any live
controller run):** the selection now admits every setting whose churn is at
most max(1% of the table per minute, 2 × the least churn that signal
achieves), then takes the fastest recovery. **Why:** with the plain cap and a
"least churn" fallback, no CPU setting met the cap (least 54 moves/min), so
the fallback picked P = 8 s (61 s recovery in the simulator) over P = 1 s
(14 s) for 10 fewer moves per minute, a cliff that would have made RQ3 compare
a slow CPU controller with a fast RIF one. Outcome under the original rule:
CPU and cgroup P = 8 s, k = 3; RIF P = 1 s, k = 3 (unchanged by the
revision). Under the revised rule: CPU P = 1 s, k = 3; cgroup P = 2 s, k = 3;
RIF P = 1 s, k = 3. Also fixed before choosing: the simulator gets each
backend's *theoretical* capacity (utilisation 1), with the campaign load
expressed relative to it. With the capacities at SLO it ran ~20% hotter than
the testbed and its RIF was ~35% noisier than measured.
**D36, second correction (before S2's valid run):** the tuning simulator
now uses 20× more, 20× lighter connections than the testbed (same load, same
lifetimes). With the testbed's 512, the model's own load noise was above the
measured noise for every signal (the testbed's generator sends each request to
a free connection, which smooths load; the model has no such feedback), and
noise can only be added, never removed. That made the tuning pessimistic,
CPU most of all. Now the model's own noise is 0.014–0.035 and each signal's
measured noise is added in full, the same way for all. Final choices: CPU and
cgroup CPU P = 2 s, k = 3; RIF P = 1 s, k = 3; all within the churn cap.

## D38 — Testbed fixes found by the campaign (2026-11-02)

Each made S2's capacities wrong before it was fixed (journal):
- **Idle backends before every probe**, and the backend skips queued requests
  whose client has gone (`requests_cancelled_total`). Otherwise an overloaded
  probe leaves a queue the next probe runs into.
- **Load generator on both threads of its two cores** (`LOADGEN_CPUS=2,3,4,5`,
  extends D14: one role on both siblings is allowed). On 2,4 its spinning
  dispatcher took one CPU and it saturated above ~4500 req/s.
- **Client namespace reuses TIME_WAIT ports and the full port range.** At
  ~250 new connections/s, once ~40% of the default ports were in TIME_WAIT,
  picking a port stalled now and then for tens of ms. Direct test: the same
  4700 req/s failed the SLO in 2 of 5 probes before, passed 5 of 5 after.
- Probe flag `send_delayed` (send delay p99 > 5 ms): the generator's CPUs or
  its connection pool were the limit; check sysmon to tell which.

## D39 — Four laptops over wifi: routed backends, one harness (2026-11-02)

For the rerun on our laptops ([cluster-runbook.md](cluster-runbook.md)):
1 client, 1 LB, 2 machines with 2 backends each, all on wifi.
- **Backends behind their machine, routed:** a wifi client can only send
  frames with its own MAC, so backends can't have addresses on the wifi
  network (no bridging, no macvlan). Each backend machine keeps its
  backends in namespaces behind a bridge (as on the single laptop) and routes
  to them; the director sends a backend's packets to its machine's MAC (from
  the LB's neighbour table), with the backend's address as the outer
  destination. No change to the director or redirector code.
- **Client route to the VIP with MTU 1460:** the encapsulation adds 40 bytes
  and wifi drivers often can't raise their MTU.
- **Signals over the network:** the controller on the LB machine can't read
  other machines' `/proc` and `/sys`, so each backend also exports the raw
  counters (CPU ticks of its CPUs, its cgroup's CPU time and pressure) on its
  metrics port, and the signals read them there (same arithmetic).
- **One harness:** `bench/testbed.py`'s `command()` decides where a command
  runs (locally in a namespace, or `ssh … sudo` on the role's machine);
  `bench/campaign.py`'s experiments are reused as they are by
  `bench/cluster.py`, which only adds preflight, setup, a smoke test and
  collecting files.
- **Workload scaled for wifi:** 20 ms requests, SLO 400 ms, 64 connections (40 ms, 800 ms and 32 on a phone hotspot, after the first dry run on 2026-11-06),
  20 long-lived (guesses to check in the dry run).
- **Testable here only by emulation:** `scripts/cluster/emulate.sh` makes
  each machine a namespace on one bridge; the driver then reaches them with
  `ip netns exec` instead of ssh. Everything but the transport and wifi
  itself runs the real code path.

## D40 — Safety at startup, dead controllers, and fairer baselines (2026-11-06)

From a review of the whole repo.
- **Controller startup broke the safety claim (not seen, but possible).** It
  published its first table over the live one without second hops and treated
  every bucket as never changed, while the harness started the load generators at
  the same moment. A connection opened just before that publish could land on a
  bucket whose new second hop wasn't its first hop. Now the first table is built
  from the active one (`controller.NewFrom`: `lb.WithSecondHops`, and buckets whose
  first hop changed count as changed at time 0). Cost: on a contiguous static table
  about 3/4 of the buckets change, and those stay frozen for the first 20 s of a
  run. Old runs: 0 backend resets in all 34 with forwarding, so they aren't rerun.
- **`lb.WithSecondHops` now does what its comment says:** a bucket whose first hop
  stays keeps its old second hop, also when the new table proposes another.
- **First signal reading discarded:** it covers the ramp-up, and moved up to 103
  buckets at t = 1–2 s in the old runs.
- **Director drops IP options (ihl > 5)**, as the redirector does. Before, such a
  connection's packets were encapsulated and then dropped at the backend.
- **A dead controller makes a run invalid.** One failed signal read ends it with
  `log.Fatal`, leaving the table frozen, which looks like a controller run that
  simply did little. The harness now checks at the end of each run, writes
  `invalid` into the record, and the analysis lists such runs and leaves them out.
  It matters more on wifi, where a read can fail. (sigmon has the same exit, without
  a check: its log just stops, which is visible.)
- **S6, a static baseline that knows the disturbance:** table proportional to the
  capacities that hold during the step or the antagonist (measured). It beat every
  controller where it ran, so we no longer describe the controller as beating static
  tables, only as not needing to know the disturbance (results, RQ2).
- **S6, denominators with neighbours busy, and a load generator that needs no LB:**
  `loadgen -targets` loads several backends from one process. Several generators
  won't do: at these rates each one's dispatcher spins a whole hyperthread, and the
  generator has four.
