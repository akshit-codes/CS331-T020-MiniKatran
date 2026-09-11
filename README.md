# CS331-T020-MiniKatran

**Team ID:** T020 

**Project ID:** 16 

**Project Title:** Mini-Katran: High-Performance L4 Load Balancing with eBPF and XDP

| Name | Roll number |
|---|---|
| Akshit Chhabra | 24110026 |
| Bhavay Goyal | 24110070 |
| Devarshi Savalgi | 24110103 |
| Kunal Chandola | 24110176 |

CS 331 Computer Networks, 2026-27 Semester 1.

## What this is

A toy version of Facebook's Katran: a layer-4 load balancer that runs inside the Linux kernel as an XDP program. It parses Ethernet/IPv4/TCP headers, keeps connection state in BPF maps, picks a backend with a consistent hash ring, rewrites the destination address, patches the IP and TCP checksums and sends the frame back out without it ever entering the kernel's network stack. A libbpf control plane configures it through pinned maps. Two conventional user-space load balancers (an epoll TCP proxy and an AF_PACKET forwarder) serve as baselines, and everything is benchmarked on a network-namespace testbed.

Headline numbers (median of 3, same testbed): XDP forwards 18.5 Gbit/s with four streams against 12.0 for the TCP proxy, handles 74k connections/s against 42k, adds 3 µs to a request instead of 22, and the XDP program itself costs 58 ns per packet.

## Layout

```
README.md
code/       all source code, scripts, tests and benchmark results (see code/README.md)
report/     final report (PDF) and its source
ppt/        presentation slides
AI_Used/    how we used AI tools: Tools, Prompts, Thought process, Step-by-step details
```

## Running it

Linux with root, clang, libbpf, bpftool, tcpdump, iperf3, ethtool. From `code/`:

```
make
sudo scripts/topo.sh up && sudo scripts/backends.sh start
sudo scripts/run_xdp.sh start
sudo ip netns exec client curl http://10.0.0.10:8080/
sudo bin/lb list; sudo bin/lb flows; sudo bin/lb stats
sudo tests/smoke.sh          # every mode end to end
sudo scripts/bench.sh        # the comparison, results/summary.md
```

