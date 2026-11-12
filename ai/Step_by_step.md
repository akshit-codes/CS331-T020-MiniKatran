# Step by step

Prompt numbers refer to `Prompts.pdf`. The prompts for step 8 are in `Prompts-report.pdf`.

1. **Research.** Discussed the professor's feedback, surveyed load-balancer papers and blog posts, and compared possible problems. We settled on Unimog-style balancing and wrote research questions and hypotheses.
2. **Measurement first.** The AI built the testbed, pinning and tuning, a test backend and a load generator, with a noise check that had to pass before anything else counted.
3. **Data path.** The director, redirector and packet format, with golden-packet tests, then a move test. The director was then ported from XDP to TC after our decision.
4. **Controller and simulator.** The control loop, signals, simulator and a single tuning rule.
5. **Campaign.** Capacity, step, antagonist, flapping, sibling and hook measurements. S2 took four attempts, because of testbed bugs each caught by the harness. An audit led to the forwarding-off contrast, total RIF, a contending sibling neighbour, and a timescale sweep.
6. **Review.** The AI reviewed its results against the papers. Further AI reviews audited the code and every main number, which led to session S6 (a table built for the disturbance, busy denominators, interleaved tables), session S7 (rerun on the final code), and corrected wording.
7. **Real laptops.** The AI wrote the 4-laptop driver and setup scripts. We set up the laptops, which exposed a clang 21 incompatibility, then ran dry runs on a phone hotspot. The first dry run failed on a saturated hotspot, so we doubled the request cost and latency target, and then ran the real quick run.
8. **Report.** After repeat runs of the main experiments (five interleaved runs each, sessions S8 to S15), the AI wrote the final report from the results, one section at a time, and the numbers in each section were checked against `code/docs/results.md`.
