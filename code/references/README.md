# References

What we read, where to get it, and which part of the project it feeds.
Local copies live in `papers/` and `blogs/` (git-ignored: download them from
the links below). Blog posts were saved as HTML plus a rough `.txt` extraction
for searching.

## Papers (`papers/`)

| File | Paper | Used for |
|---|---|---|
| keshav-how-to-read-a-paper.pdf | S. Keshav, How to Read a Paper (CCR 2007) — [pdf](http://ccr.sigcomm.org/online/files/p83-keshavA.pdf) | How we split and do the reading |
| maglev-nsdi16.pdf | Eisenbud et al., Maglev (NSDI 2016) — [pdf](https://www.usenix.org/system/files/conference/nsdi16/nsdi16-paper-eisenbud.pdf) | Phase 1 lookup table design; phase 2 evaluation style (balance vs resilience) |
| ananta-sigcomm13.pdf | Patel et al., Ananta (SIGCOMM 2013) — [pdf](https://conferences.sigcomm.org/sigcomm/2013/papers/sigcomm/p207.pdf) | Phase 1: encapsulation + direct server return; splitting work between LB and hosts |
| silkroad-sigcomm17.pdf | Miao et al., SilkRoad (SIGCOMM 2017) — [pdf](https://minlanyu.seas.harvard.edu/writeup/sigcomm17.pdf) | RQ4: definition of per-connection consistency (PCC); how often pools change |
| beamer-nsdi18.pdf | Olteanu et al., Beamer (NSDI 2018) — [pdf](https://www.usenix.org/system/files/conference/nsdi18/nsdi18-olteanu.pdf) | Phase 1 second hop (daisy chaining); RQ4/H7 evaluation we model ours on |
| faild-nsdi18.pdf | Araújo et al., Faild (NSDI 2018) — [pdf](https://www.usenix.org/system/files/conference/nsdi18/nsdi18-araujo.pdf) | Phase 1 TC redirector: host-side redirection; drain (phase 2) |
| cheetah-nsdi20.pdf | Barbette et al., Cheetah (NSDI 2020) — [pdf](https://www.usenix.org/system/files/nsdi20-paper-barbette.pdf) | Related work: PCC with arbitrary balancing policies |
| xdp-conext18.pdf | Høiland-Jørgensen et al., The eXpress Data Path (CoNEXT 2018) — [pdf](https://github.com/xdp-project/xdp-paper/raw/master/xdp-the-express-data-path.pdf) | Phase 1 data path; measuring the LB itself fairly |
| prequal-nsdi24.pdf | Wydrowski et al., Prequal (NSDI 2024) — [pdf](https://www.usenix.org/system/files/nsdi24-wydrowski.pdf) | RQ3 and H4: why balancing CPU can backfire; RIF as a signal |
| tail-at-scale-cacm13.pdf | Dean & Barroso, The Tail at Scale (CACM 2013) — [pdf](https://www.barroso.org/publications/TheTailAtScale.pdf) | Phase 0/2: why the SLO is on p99 |
| consistent-hashing-bounded-loads.pdf | Mirrokni et al., Consistent Hashing with Bounded Loads (SODA 2018) — [arXiv](https://arxiv.org/abs/1608.01350) | RQ1 related work: affinity vs balance |

## Blog posts (`blogs/`)

| File | Post | Used for |
|---|---|---|
| cloudflare-unimog.* | Cloudflare, Unimog (2020) — [link](https://blog.cloudflare.com/unimog-cloudflares-edge-load-balancer/) | The design we build on: buckets, two hops (phase 1), swap trick and least-recently-modified, CPU control loop and its oscillation (phase 3, RQ2) |
| (not saved) | Facebook Engineering, N. Shirokov and R. Dasineni, Open-sourcing Katran, a scalable network load balancer (2018) — [link](https://engineering.fb.com/2018/05/22/open-source/open-sourcing-katran-a-scalable-network-load-balancer/); code: [facebookincubator/katran](https://github.com/facebookincubator/katran) | Background: the fixed-share L4 balancer the project is named after; cited in the report next to Maglev |
| github-glb-director-2018.* | GitHub, GLB Director (2018) — [link](https://github.blog/engineering/infrastructure/glb-director-open-source-load-balancer/) | Phase 1: primary/secondary table, second chance, encapsulation header; drain states (phase 2, RQ4) |
| github-glb-introducing-2016.* | GitHub, Introducing GLB (2016) — [link](https://github.blog/engineering/infrastructure/introducing-glb/) | Background: why GLB was built |
| cloudflare-nginx-worker-imbalance.* | Cloudflare, Why does one NGINX worker take all the load? (2017) — [link](https://blog.cloudflare.com/the-sad-state-of-linux-socket-balancing/) | Background: balancing inside one host; where a server queues (D25) |
| heiser-benchmarking-crimes.* | G. Heiser, Systems Benchmarking Crimes — [link](https://gernot-heiser.org/benchmarking-crimes.html) | Phase 0 measurement protocol: mistakes to avoid |
| stripe-rate-limiters.* | Stripe, Scaling your API with rate limiters (2017) — [link](https://stripe.com/blog/rate-limiters) | Problem framing: overload in a latency-sensitive API |
| aws-shuffle-sharding.* | AWS Builders' Library, Workload isolation using shuffle-sharding — [link](https://aws.amazon.com/builders-library/workload-isolation-using-shuffle-sharding/) | Background: blast-radius isolation (out of scope) |

## Not saved (sites block scripted downloads; save from a browser if needed)

- Vimeo, Improving load balancing with a new consistent-hashing algorithm — https://medium.com/vimeo-engineering-blog/improving-load-balancing-with-a-new-consistent-hashing-algorithm-9f1bd75709ed
- Twitter, Deterministic Aperture (2019) — https://blog.twitter.com/engineering/en_us/topics/infrastructure/2019/daperture-load-balancer
- Netflix, Performance Under Load (2018) — https://netflixtechblog.medium.com/performance-under-load-3e6fa9a60581
- Matt Klein, Introduction to modern network load balancing and proxying (2017) — https://blog.envoyproxy.io/introduction-to-modern-network-load-balancing-and-proxying-a57f6ff80236
- Gil Tene, How NOT to Measure Latency (talk) — search for the recording
