#!/usr/bin/env python3
# results/data.csv -> results/*.png
import csv, statistics, collections, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
ACCENT, GRAY, ORANGE = "#2a78d6", "#b1b0aa", "#eb6834"
MODES = [("direct", "no LB"), ("proxy", "tcp proxy"), ("raw", "af_packet"), ("xdp", "xdp")]

def load(path):
    rows = collections.defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                rows[(r["mode"], r["test"], r["metric"])].append(float(r["value"]))
            except ValueError:
                pass
    return lambda m, t, k: statistics.median(rows[(m, t, k)]) if rows.get((m, t, k)) else float("nan")

med = load("results/data.csv")
med1 = None

plt.rcParams.update({
    "font.family": "sans-serif", "font.size": 10, "text.color": INK, "axes.labelcolor": INK2,
    "axes.edgecolor": AXIS, "axes.facecolor": SURFACE, "figure.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "xtick.color": INK2, "ytick.color": MUTED, "axes.titlecolor": INK, "axes.titlesize": 11, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False, "axes.spines.left": False,
    "axes.grid": True, "axes.grid.axis": "y", "grid.color": GRID, "grid.linewidth": 1, "ytick.left": False,
    "axes.axisbelow": True,
})

def bars(ax, labels, values, colors, title, unit="", fmt="{:.1f}", note=None):
    x = range(len(values))
    ax.bar(x, values, width=0.42, color=colors, zorder=3)
    ax.set_xticks(list(x), labels)
    ax.set_title(title, loc="left", pad=18 if note else 8)
    if note:
        ax.text(0, 1.015, note, transform=ax.transAxes, color=MUTED, fontsize=8.5, va="bottom")
    top = max(values)
    for i, v in enumerate(values):
        ax.text(i, v + top * 0.02, fmt.format(v) + unit, ha="center", va="bottom", color=INK2, fontsize=9)
    ax.set_ylim(0, top * 1.18)
    ax.tick_params(axis="x", length=0)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 10 else f"{v:g}"))

def mode_panel(ax, test, metric, title, unit="", fmt="{:.1f}", note=None, scale=1.0):
    vals = [med(m, test, metric) * scale for m, _ in MODES]
    bars(ax, [n for _, n in MODES], vals, [ACCENT if m == "xdp" else GRAY for m, _ in MODES], title, unit, fmt, note)

def cpu_per_gb(m):
    return med(m, "tput4", "sys_cpu_s") / (med(m, "tput4", "gbps") * med(m, "tput4", "elapsed_s") / 8)

def cpu_per_100k(m):
    return med(m, "cps64", "sys_cpu_s") / (med(m, "cps64", "conn_per_s") * med(m, "cps64", "elapsed_s")) * 1e5

PANELS = [
    ("throughput", lambda ax: mode_panel(ax, "tput4", "gbps", "Throughput, 4 streams", " Gbit/s", "{:.1f}", "iperf3 through the VIP, higher is better")),
    ("connections", lambda ax: mode_panel(ax, "cps64", "conn_per_s", "Connections per second", "", "{:,.0f}", "64 connections in flight, higher is better", 1)),
    ("latency", lambda ax: mode_panel(ax, "cps1", "p50_us", "Request latency, p50", " µs", "{:.0f}", "connect + request + response, 1 in flight, lower is better")),
    ("cpu", lambda ax: bars(ax, [n for _, n in MODES], [cpu_per_gb(m) for m, _ in MODES],
                             [ACCENT if m == "xdp" else GRAY for m, _ in MODES], "Machine CPU per GB moved", " s", "{:.2f}",
                             "all cores, whole testbed, 4 streams, lower is better")),
]

os.makedirs("results", exist_ok=True)
fig, axes = plt.subplots(2, 2, figsize=(10, 7))
for (name, draw), ax in zip(PANELS, axes.flat):
    draw(ax)
fig.suptitle("Mini-Katran: XDP load balancer vs user space baselines", x=0.02, ha="left", fontsize=13, fontweight="bold", color=INK)
fig.tight_layout(rect=(0, 0, 1, 0.96))
fig.savefig("results/overview.png", dpi=150)
for name, draw in PANELS:
    f, ax = plt.subplots(figsize=(5.2, 3.6))
    draw(ax)
    f.tight_layout()
    f.savefig(f"results/{name}.png", dpi=150)

# lb cost = everything above the no-lb run
f, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.6))
lbs = MODES[1:]
base_gb, base_c = cpu_per_gb("direct"), cpu_per_100k("direct")
bars(a1, [n for _, n in lbs], [cpu_per_gb(m) - base_gb for m, _ in lbs], [ACCENT if m == "xdp" else GRAY for m, _ in lbs],
     "CPU seconds per GB, load balancing only", " s", "{:.2f}", "machine CPU minus the no-LB run, lower is better")
bars(a2, [n for _, n in lbs], [cpu_per_100k(m) - base_c for m, _ in lbs], [ACCENT if m == "xdp" else GRAY for m, _ in lbs],
     "CPU seconds per 100k connections, LB only", " s", "{:.1f}", "machine CPU minus the no-LB run, lower is better")
f.tight_layout()
f.savefig("results/cpu_overhead.png", dpi=150)

# bpf program time per packet
f, ax = plt.subplots(figsize=(5.6, 3.6))
tests = [("tput1", "bulk, 1 stream"), ("tput4", "bulk, 4 streams"), ("cps1", "1 conn in flight"), ("cps64", "64 conns in flight")]
bars(ax, [n for _, n in tests], [med("xdp", t, "xdp_ns_per_pkt") for t, _ in tests], [ACCENT] * 4,
     "XDP program run time per packet", " ns", "{:.0f}", "run_time_ns / run_cnt from bpftool, connection heavy traffic inserts flow entries")
f.tight_layout()
f.savefig("results/xdp_cost.png", dpi=150)

# single queue vs 8 queue veths (needs the old round-1 csv)
if med1:
    f, ax = plt.subplots(figsize=(5.6, 3.6))
    cats = ["1 stream", "4 streams"]
    old = [med1("xdp", "tput1", "gbps"), med1("xdp", "tput4", "gbps")]
    new = [med("xdp", "tput1", "gbps"), med("xdp", "tput4", "gbps")]
    x = [0, 1]
    w = 0.3
    ax.bar([i - w / 2 - 0.02 for i in x], old, width=w, color=ORANGE, label="single queue veth (round 1)", zorder=3)
    ax.bar([i + w / 2 + 0.02 for i in x], new, width=w, color=ACCENT, label="8 queue veth (final)", zorder=3)
    for i in x:
        ax.text(i - w / 2 - 0.02, old[i] + 0.4, f"{old[i]:.1f}", ha="center", color=INK2, fontsize=9)
        ax.text(i + w / 2 + 0.02, new[i] + 0.4, f"{new[i]:.1f}", ha="center", color=INK2, fontsize=9)
    ax.set_xticks(x, cats)
    ax.tick_params(axis="x", length=0)
    ax.set_ylim(0, max(new + old) * 1.3)
    ax.set_title("XDP throughput and veth queues (Gbit/s)", loc="left", pad=18)
    ax.text(0, 1.015, "with one queue every packet's XDP work runs on one cpu", transform=ax.transAxes, color=MUTED, fontsize=8.5, va="bottom")
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    f.tight_layout()
    f.savefig("results/scaling.png", dpi=150)
print("wrote", sorted(p for p in os.listdir("results") if p.endswith(".png")))
