# mini-katran

Toy version of Facebook's [Katran](https://engineering.fb.com/2018/05/22/open-source/open-sourcing-katran-a-scalable-network-load-balancer/): an L4 load balancer that runs as an XDP program, for my Computer Networks course. Two user space load balancers serve as baselines so the kernel version can be benchmarked against "traditional" designs.

```
                     +--------------------------------------------+
   client            |  lb namespace, eth0                        |          be1 (10.0.2.11)
 10.0.1.2  ------->  |  XDP: parse eth/ip/tcp -> flow table hit?  |  ------>  be2 (10.0.2.12)
 to VIP 10.0.0.10    |    miss: hash(client) -> ring -> backend   |
                     |  rewrite dst ip+mac, fix csums, XDP_TX     |
           <-------  |  reply: flow table -> src = VIP, XDP_TX    |  <------  (backends route via lb)
                     +--------------------------------------------+
                                     ^  maps (pinned in /sys/fs/bpf/mklb)
                                     |
                            bin/lb add-vip / add-backend / list / flows / stats
```

What is in here:
- `bpf/lb_kern.c` - the XDP program: header parsing, 5-tuple flow table (LRU hash), consistent hash ring lookup, DNAT with RFC 1624 incremental IP/TCP checksum updates, `XDP_TX`. `bpf/xdp_pass.c` is a no-op program needed on the veth peer.
- `userspace/lb.c` - control plane with libbpf: load/attach, pin maps, manage VIPs and backends, rebuild rings, dump the flow table and counters.
- `common/` - hash function and ring builder shared by the BPF program and everything in user space.
- `baseline/tcp_proxy.c` - epoll TCP proxy (HAProxy-style) with the same consistent hashing.
- `baseline/raw_fwd.c` - the XDP logic done in user space on an AF_PACKET socket.
- `bench/` - small http server + connection rate generator, `scripts/bench.sh` runs the comparison.
- `scripts/topo.sh` - the namespace testbed, `scripts/run_*.sh` start a load balancer mode.
- `tests/smoke.sh` - end to end check of every mode, `tests/ring_test.c` - hash ring sanity check.

Build: `make`. Needs clang, libbpf, bpftool (Arch: `pacman -S clang libbpf bpf tcpdump iperf3 ethtool`). Everything network/BPF related needs root.

Quick run:
```
sudo scripts/topo.sh up          # namespaces: client, lb, be1, be2 behind a bridge in "sw"
sudo scripts/backends.sh start   # http :8080/:8081 and iperf3 :5201 in be1/be2
sudo scripts/run_xdp.sh start    # load the xdp lb into lb, vip 10.0.0.10
sudo ip netns exec client curl http://10.0.0.10:8080/
sudo bin/lb list                 # vips, backends and their share of the ring
sudo bin/lb flows                # live connection table
sudo bin/lb stats                # counters, per backend too
sudo bin/lb del-backend 10.0.0.10:8080 10.0.2.11     # existing flows keep working, new ones go to be2
sudo scripts/run_xdp.sh stop
sudo scripts/run_proxy.sh start  # same thing with the user space proxy (or run_raw.sh)
sudo tests/smoke.sh              # all modes end to end
sudo scripts/bench.sh            # results/summary.md
```
Results (`results/summary.md`, median of 3, namespaces + 8-queue veths on one laptop):

| mode | 4 streams Gbit/s | conn/s | request p50 us | machine cpu s/GB | LB cpu |
|---|---|---|---|---|---|
| no LB (reference) | 39.3 | 86k | 30 | 1.84 | - |
| tcp proxy | 12.0 | 42k | 52 | 2.95 | 75% of a core |
| af_packet forwarder | 5.7 | 52k | 45 | 5.19 | 69% of a core |
| **xdp** | **18.5** | **74k** | **33** | **2.43** | 58 ns/packet, ~9% of a core |

Look inside with `sudo bpftool prog show`, `sudo bpftool map dump pinned /sys/fs/bpf/mklb/flows`, `sudo nsenter --net=/run/netns/lb ethtool -S eth0 | grep xdp`.
