# Thought process

None of us had used eBPF before and the week was short, so we decided to learn with a working example instead of only from docs. We had Claude help build a reference version of the project stage by stage, and at each stage we tried to predict what would happen, ran it, looked at the packets and the maps, and asked about whatever did not match. Every design choice in the reference came with its reasoning (why rewrite addresses instead of tunnelling, why consistent hashing, why keep connection state in a map), which we went through as a group with the study guide and the walkthrough.

Then we built our own version. The reference was there to compare against when something behaved differently, and to look up how a piece we were stuck on had been done, the way you would use a textbook example. Going through the same problems ourselves (the verifier, the veth quirks, the benchmark not making sense at first) is where most of the learning happened.

We think this was a good way to use the tool: we would not have got as far in a week on our own, but we also would not have understood it as well if we had just been handed a finished project.
