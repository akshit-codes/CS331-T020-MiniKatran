# Prompts

Our prompts were mostly of these kinds:

- Explain the concepts: what eBPF and XDP are, what the verifier checks, what BPF maps are and how user space reads them, how veth pairs and network namespaces work, what a packet goes through in the kernel.
- Show us a working reference of each piece (testbed, a user space proxy, the XDP program, the control plane, a benchmark) with the reasoning, so we could see it run before trying it ourselves.
- Explain things we saw and did not understand: why XDP_TX did nothing on a veth at first, why iperf3 needs both of its connections on the same backend, how a checksum can be updated without reading the whole packet, why throughput did not grow with more streams.
- Write a study guide and a walkthrough so the whole team could learn from the reference.
- Questions while building our own version, mostly "why does the reference do it this way" and help reading verifier errors.
- Help with the structure and wording of the report and the slides.
