# Prompts

We used Claude Code as a VS Code extension, so the whole conversation was a local session on one laptop and not on the claude.ai website. There is no web history for it, and the local history of that session got lost, so we cannot paste the exact prompts and responses here. We have informed the TAs of this. What we can do is write down every prompt we can remember, and that is what this file is: the prompts as we remember them, roughly in order, with the wording from memory rather than verbatim. We hope it is an acceptable substitute for the transcript.

## Starting out

The first thing we asked for was a reading list: we are third year CS students taking the networks course, we have to build a small Katran with eBPF and XDP, and we do not know all the networking terms yet, so what should we read and in what order before any of it makes sense.

Then we gave it the Facebook post (https://engineering.fb.com/2018/05/22/open-source/open-sourcing-katran-a-scalable-network-load-balancer/) and asked what we should know before reading it and which terms to look up first. After reading it we came back with questions on the article itself: what IP-in-IP encapsulation and direct server return mean and how the backend can reply to the client directly, what Maglev hashing is and why they need it, and why the load balancer keeps connection state at all if the hash already picks the backend.

After that it was mostly clarification questions while reading:

- what eBPF is, how a program gets loaded, what the verifier checks and why it rejects programs
- what XDP is, what XDP_PASS / XDP_DROP / XDP_TX / XDP_REDIRECT mean, native vs generic mode
- BPF maps: hash vs LRU hash vs array, how user space reads and writes them, what pinning under /sys/fs/bpf means
- veth pairs, bridges and network namespaces, and how to build a testbed with them on one machine
- what a packet goes through in the kernel on receive and where XDP sits compared to the rest of the stack
- the difference between DNAT and DSR, and which one the brief is asking for (destination rewriting plus checksum recomputation, so DNAT)

## Reference code

We had no idea what a working version of any of this looks like, so for each piece we asked for a small reference version with the reasoning, to see how something like that is put together and what it does when you run it. We ran each one in the testbed, looked at the packets with tcpdump and dumped the maps, and then wrote our own. Roughly:

- a minimal namespace testbed, client + lb + two backends behind a bridge, with the routing set up so the backends' replies come back through the lb. Explain each ip command.
- a minimal TCP proxy with epoll that listens on the VIP and connects to a backend picked by consistent hashing
- a minimal XDP program: parse eth/ip/tcp, look up a flow table, pick a backend from a hash ring, rewrite dst ip and mac, fix checksums, XDP_TX. We asked it to explain every bounds check the verifier wants.
- the checksum update in the reference only touches the bytes that changed instead of summing the whole packet again. How does that trick work, why is it correct, and what is the RFC 1624 thing it mentions
- why rewrite addresses instead of tunnelling like Katran, and what that costs on the reply path
- a minimal libbpf control plane: load, attach, pin maps, add/del vip and backend, rebuild the ring, dump flows and counters. We asked for it to be easy to use from the command line, so that adding a backend or checking the flow table is one short command.
- the same forwarding logic on an AF_PACKET socket in user space, so we would have a second baseline where the only difference to XDP is where the code runs
- what a small benchmark should look like (iperf3, a tiny http server, a connection rate generator, latency, cpu) and how to do the "no load balancer" run so the lb's own cost shows up
- a study guide with self check questions and a walkthrough of the reference, so everyone in the team could go through it before we wrote ours

While writing our own version we kept coming back with "why does the reference do it this way": why an LRU map for the flow table and what happens when an entry gets evicted, why the ring has the number of slots it has, why the reply direction needs its own entry, why a SYN should never consult the flow table.

## Scripts

Most of the shell scripts were written with help from the LLM, since none of us were very familiar with shell scripting or with tools like ip netns, bpftool and ethtool: topo.sh, backends.sh, the run_xdp.sh / run_proxy.sh / run_raw.sh start and stop scripts, and tests/smoke.sh. We would describe what the script had to do, get a first version, and then edit it to fit our layout and fix whatever did not work on our machine. The benchmark runner and the plotting and summarising scripts, however, are ours.

## Debugging

- XDP_TX on the lb's veth and nothing comes out, tcpdump on the peer sees nothing. Turned out the other end of the veth pair needs an XDP program attached too, even a no-op one. That is where bpf/xdp_pass.c comes from.
- verifier errors: "invalid access to packet" when we read a header without a bounds check, and rejected loops in the ring lookup
- iperf3 through the VIP hangs even though curl works. iperf3 opens a control connection and a data connection and both have to land on the same backend, so hash without the client port for that test.
- throughput does not grow with more iperf3 streams. A veth has one queue by default, ethtool -L fixes it.
- multi-stream iperf3 shows tens of thousands of retransmissions but no drops anywhere. Is our program reordering packets? Suggestion was to run the same test with no lb at all, and the veth path reorders on its own.
- the raw socket forwarder stops answering after a busy run, capture shows the backend's SYN-ACK arriving and being dropped. One half of the connection's pair had been evicted from the table while the other still matched the reused port. Hence the rule that a SYN never consults the table.
- CPU at 97 °C while benchmarking and the CPU numbers way too high, what could be doing this. It was our own test http server spinning at 100% after a client closed without sending (a stale errno check).
- two runs of the same mode half an hour apart differ by 45 µs latency. How do you make benchmarks on a laptop reproducible: pin the governor, interleave the modes, repeat and take the median.
- connections between the no-lb run and the VIP run failing now and then: reused client ports colliding with TIME_WAIT sockets on the backends, use a separate client address for the no-lb runs.
- more generally, how to tell a problem in the program from a problem in the testbed

## Report and slides

- other than adding UDP and DSR, what would be sensible future work (IPv6, Maglev hashing, health checks, a multi-process proxy so the throughput comparison is fairer, re-measuring on a real NIC)
- help with the structure of the report and the wording of some sections
- help formatting the report. We wrote it as an HTML page with CSS and printed it to PDF from Chrome, so the questions were about print CSS: page breaks, keeping the results table and the figures on one page, margins, headers and the reference list.
- help with the slides: which six to have, what goes on each, and how to lay out the results table so it reads clearly
