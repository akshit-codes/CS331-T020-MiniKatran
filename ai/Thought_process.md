# Thought process

Our first version of Mini-Katran was rejected in review. The professor said we had copied Katran instead of picking a load-balancing problem and solving it, that our benchmarks were meaningless, and that we couldn't explain parts of our own work. The professor also said AI for implementation is fine, as long as the team understands the implementation.

So this time we started from a problem: servers of unequal and changing capacity. We used AI heavily to read widely and then to build, on the basis that the implementation could come from AI as long as the problem, the decisions and the understanding were ours.

**Decisions made by the team, not the AI:**
- Build a Unimog-style design (buckets, two hops, load feedback), from the options the research turned up.
- Run the load balancer at TC and not XDP. The AI had recommended XDP because we first told it that one of our laptops had a network card with native XDP. When we checked, none of them did, and Akshit changed the decision to TC (prompts 5 and 21).
- Measure balancing quality through a latency target, not only overhead, after reading how papers define their benchmarks.
- Which experiments to keep or cut for time, and which suggestions from reviews to take.
- Laptop roles for the real run, which we ran ourselves.
