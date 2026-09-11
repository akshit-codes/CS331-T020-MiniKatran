#!/usr/bin/env python3
# results/data.csv -> results/summary.md (median over reps)
import csv, statistics, collections, sys

rows = collections.defaultdict(list)
with open("results/data.csv") as f:
    for r in csv.DictReader(f):
        try:
            rows[(r["mode"], r["test"], r["metric"])].append(float(r["value"]))
        except ValueError:
            pass

def med(mode, test, metric, default=float("nan")):
    v = rows.get((mode, test, metric))
    return statistics.median(v) if v else default

modes = []
for m in ["direct", "proxy", "raw", "xdp"]:
    if any(k[0] == m for k in rows):
        modes.append(m)

cols = [
    ("1 stream Gbit/s", lambda m: "%.1f" % med(m, "tput1", "gbps")),
    ("4 streams Gbit/s", lambda m: "%.1f" % med(m, "tput4", "gbps")),
    ("conn/s (64 conc)", lambda m: "%.0f" % med(m, "cps64", "conn_per_s")),
    ("req latency p50 us (1 conc)", lambda m: "%.0f" % med(m, "cps1", "p50_us")),
    ("p99 us (64 conc)", lambda m: "%.0f" % med(m, "cps64", "p99_us")),
    ("sys cpu s/GB (4 streams)", lambda m: "%.2f" % (med(m, "tput4", "sys_cpu_s") / (med(m, "tput4", "gbps") * med(m, "tput4", "elapsed_s") / 8))),
    ("lb process cpu % (4 streams)", lambda m: "%.0f" % (100 * med(m, "tput4", "lb_cpu_s") / med(m, "tput4", "elapsed_s"))),
    ("lb process cpu % (cps64)", lambda m: "%.0f" % (100 * med(m, "cps64", "lb_cpu_s") / med(m, "cps64", "elapsed_s"))),
    ("xdp ns/packet", lambda m: "%.0f" % med(m, "tput4", "xdp_ns_per_pkt") if m == "xdp" else "-"),
]

out = ["| mode | " + " | ".join(c[0] for c in cols) + " |", "|" + "---|" * (len(cols) + 1)]
for m in modes:
    out.append("| %s | " % m + " | ".join(c[1](m) for c in cols) + " |")
text = "\n".join(out) + "\n"
open("results/summary.md", "w").write(text)
print(text)
