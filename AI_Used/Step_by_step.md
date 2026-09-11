# Step by step

1. Learning the concepts with Claude: eBPF, the verifier, XDP verdicts and modes, maps, namespaces and veth, the Linux receive path.
2. Reference version, built with Claude one stage at a time (testbed, TCP proxy, XDP program, control plane, raw forwarder, benchmark) and explained as it went. We ran each stage, captured packets and dumped maps to see it work, and broke things on purpose to see what happened.
3. Studying it as a team: the journal of what went wrong and why, the decision log, the code walkthrough and the study guide with its self check questions.
4. Our own build of the project, using the reference to compare and to look things up when stuck.
5. Testing and benchmarking our version with the same kind of tests: smoke test, tcpdump checksum checks, md5 of a download through the VIP, iperf3 and connection rate runs.
6. Report and slides written by us from our notes, with help on structure and wording.
