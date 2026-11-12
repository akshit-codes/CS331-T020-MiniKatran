#!/usr/bin/env python3
"""Tables and plots for the campaign (docs/campaign.md).

Usage: python3 bench/analyze_campaign.py results/campaign-<date>
Reads whatever sessions exist (s1, tuning, s2), writes <root>/summary.md and
PNG figures next to it.
"""
import csv
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Reference palette, light mode.
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"]  # reference palette order
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]  # second channel: identity never by colour alone
BACKENDS = ["beP1", "beP2", "beE1", "beE2"]
POLICIES = ["equal", "cores", "oracle", "ctl-cpu", "ctl-rif", "ctl-rif-total"]
SIGNALS = ["cpu", "rif", "cgroup", "psi"]
# Colour follows the signal in every chart (a controller's table, its run, its readings); the cpu
# variants are tints of cpu. Tables with no signal are grey, darker = knows more. Total RIF is the
# darker violet, validated against cpu, rif and cgroup, all pairs. Backends have their own colours.
STYLE = {  # name: (colour, marker)
    "cpu": (SLOTS[0], "o"), "rif": (SLOTS[1], "s"), "cgroup": (SLOTS[2], "^"), "psi": (SLOTS[3], "D"),
    "rif-total": ("#4a3aa7", "P"), "cpu-noprotect": ("#86b6ef", "v"), "cpu-noredirect": ("#184f95", "X"),
    "cpu-gain0.1": ("#5b9be3", "<"), "cpu-gain1": ("#0f3a70", ">"),
    "equal": ("#a3a29a", "v"), "cores": ("#6e6d67", "^"), "oracle": ("#3a3936", "o"), "hindsight": ("#0b0b0b", "D"),
    "direct": ("#a3a29a", "*"),
}


def color_of(name):
    return STYLE[name.removeprefix("ctl-")][0]


def marker_of(name):
    return STYLE[name.removeprefix("ctl-")][1]


def mark(name):
    """Marker style of a signal or table. Plus, cross and star are mostly edge, so a ring around
    them would eat them: they get no ring and a larger size. Triangles shrink under the ring too."""
    m = marker_of(name)
    size = 10 if m in "P*X" else 9 if m in "^v" else 7
    return dict(marker=m, color=color_of(name), markeredgecolor=SURFACE,
                markeredgewidth=0 if m in "P*X" else 1.5, markersize=size)


TUNED_ORDER = ["cpu", "rif", "rif-total", "cgroup"]
LATENCY_TICKS = [2, 5, 10, 20, 50, 100, 200, 500, 1000]
SLO_MS = 20.0  # p99 bound; read from the session's calibration record (cluster runs use their own)

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "text.color": INK, "xtick.color": INK2, "ytick.color": INK2,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.spines.top": False, "axes.spines.right": False,
    "lines.linewidth": 2, "lines.markersize": 7, "font.size": 10, "axes.titlesize": 11, "axes.titleweight": "bold",
    "legend.frameon": False,
})


def end_labels(ax, items, x, gap=0.07):
    """Direct labels at the right end of lines, spread apart so they don't
    overlap. items: (y, text) pairs in data coordinates; gap: fraction of the
    axis height (linear or log)."""
    log = ax.get_yscale() == "log"
    lo, hi = ax.get_ylim()
    to_frac = (lambda y: (math.log(y) - math.log(lo)) / (math.log(hi) - math.log(lo))) if log else (lambda y: (y - lo) / (hi - lo))
    placed = []
    for frac, text in sorted((to_frac(y), text) for y, text in items):
        if placed and frac - placed[-1][0] < gap:
            frac = placed[-1][0] + gap
        placed.append((frac, text))
    for frac, text in placed:
        ax.annotate(text, (1.0, frac), xycoords=ax.transAxes, xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=8, color=INK, annotation_clip=False)


def records(path):
    return [json.loads(line) for line in open(path)] if path.exists() else []


def capacity_at(probes, bound_ms):
    """Highest rate meeting p99 <= bound, interpolated (log p99) between the
    bracketing probes of one search. None if no probe meets it; '>' if all do."""
    pts = sorted((p["rate"], p["summary"]["latency_ms"]["p99"]) for p in probes)
    ok = [(r, l) for r, l in pts if l <= bound_ms]
    bad = [(r, l) for r, l in pts if l > bound_ms]
    if not ok:
        return None
    lo = max(ok)
    above = [b for b in bad if b[0] > lo[0]]
    if not above:
        return lo[0]
    hi = min(above)
    f = (math.log(bound_ms) - math.log(lo[1])) / (math.log(hi[1]) - math.log(lo[1]))
    return lo[0] + f * (hi[0] - lo[0])


def resolution(probes):
    """How finely one search located its capacity: from the highest passing
    probe to the next failing one, relative to the capacity. (It depends on the
    search's range, which differs between experiments.) None if nothing brackets it."""
    passed = [p["rate"] for p in probes if p["meets_slo"]]
    above = [p["rate"] for p in probes if not p["meets_slo"] and passed and p["rate"] > max(passed)]
    return (min(above) - max(passed)) / max(passed) if above else None


def resolutions(probes, *keys):
    """The resolution of each search in probes (grouped by keys)."""
    groups = {}
    for p in probes:
        groups.setdefault(tuple(p[k] for k in keys), []).append(p)
    return [r for g in groups.values() if (r := resolution(g)) is not None]


def worst_resolution(probes, *keys):
    found = resolutions(probes, *keys)
    return max(found) if found else float("nan")


def typical_resolution(probes, *keys):
    found = resolutions(probes, *keys)
    return statistics.median(found) if found else float("nan")


def latency_axis(ax):
    """Log scale with plain millisecond ticks, and the SLO line labelled at the right."""
    ax.set_yscale("log")
    lo, hi = ax.get_ylim()
    ax.set_yticks([v for v in LATENCY_TICKS if lo <= v <= hi] or [SLO_MS], minor=False)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%g"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.axhline(SLO_MS, color=INK2, linewidth=1, linestyle=(0, (4, 3)))
    ax.text(ax.get_xlim()[1], SLO_MS * 1.06, f"SLO {SLO_MS:g} ms ", color=INK2, fontsize=8, va="bottom", ha="right")


# --- S1 ---

def s1(root, md):
    d = json.loads((root / "s1" / "denominators.json").read_text())
    runs = records(root / "s1" / "runs.jsonl")
    calib = next(r for r in runs if r["kind"] == "calibration")
    md.append("## S1: denominators and signal noise\n")
    md.append(f"Calibration: {calib['work']} work units per request = {calib.get('cost_ms', 1.0):g} ms on a P-core; an E-core takes "
              f"{calib['p_units_per_ms'] / calib['e_units_per_ms']:.2f}× as long. beP1 slowness {calib['slowness']['beP1']}.\n")
    md.append("| backend | cores | theoretical (req/s) | capacity at SLO, per repeat | mean | % of theoretical | repeats differ by |")
    md.append("|---|---|---|---|---|---|---|")
    rel_var = []
    for b in BACKENDS:
        caps = [c for c in d["runs"][b] if c]
        diff = (max(caps) - min(caps)) / statistics.mean(caps) if len(caps) > 1 else float("nan")
        if len(caps) == 2:
            rel_var.append((caps[0] - caps[1]) ** 2 / 2 / statistics.mean(caps) ** 2)
        md.append(f"| {b} | {d['cores'][b]} | {d['theoretical'][b]:.0f} | {', '.join(f'{c:.0f}' for c in caps)} | "
                  f"{d['capacity'][b]:.0f} | {d['capacity'][b] / d['theoretical'][b]:.0%} | {'–' if len(caps) < 2 else f'{diff:.1%}'} |")
    cv = math.sqrt(statistics.mean(rel_var)) if rel_var else float("nan")
    total = sum(d["capacity"].values())
    probes = [r for r in runs if r["kind"] == "probe" and r.get("experiment") == "denominator"]
    res = worst_resolution(probes, "backend", "rep")
    if rel_var:
        md.append(f"\nSum of denominators: **{total:.0f} req/s**. Short-protocol capacity CV (pooled over "
                  f"{len(rel_var)} pairs): **{cv:.1%}**. Smallest difference we claim between policies: "
                  f"**{max(2 * res, 2 * cv):.1%}** (2× the larger of the CV and the coarsest search resolution here, {res:.1%}). "
                  f"How firm: with {len(rel_var)} pairs the CV's 95% interval is {0.6 * cv:.1%}–{2.87 * cv:.1%}, so that "
                  f"limit could be anywhere from {2 * 0.6 * cv:.0%} to {2 * 2.87 * cv:.0%}.\n")
    else:
        md.append(f"\nSum of denominators: **{total:.0f} req/s**. One search per backend, so this run has no capacity CV. "
                  f"Differences under {2 * res:.1%} (2× the coarsest search resolution here) can't be resolved at all, "
                  "and the real limit is higher by an amount this run can't tell.\n")

    fig, axes = plt.subplots(1, 4, figsize=(13, 3.4), sharey=True)
    for ax, b, color in zip(axes, BACKENDS, SLOTS):
        for rep, marker in ((1, "o"), (2, "s")):
            pts = sorted((p["rate"], p["summary"]["latency_ms"]["p99"]) for p in probes if p["backend"] == b and p["rep"] == rep)
            if pts:
                ax.plot(*zip(*pts), marker=marker, linestyle="none", color=color, markeredgecolor=SURFACE,
                        markeredgewidth=1.5, label=f"repeat {rep}")
        ax.axvline(d["capacity"][b], color=color, linewidth=1)
        ax.set_title(b, loc="left")
        ax.set_xlabel("offered load (req/s)")
        ax.set_ylim(min(ax.get_ylim()[0], 5), max(ax.get_ylim()[1], 60))
        latency_axis(ax)
        ax.text(d["capacity"][b], ax.get_ylim()[0] * 1.1, f" {d['capacity'][b]:.0f} req/s", color=INK, fontsize=8)
    axes[0].set_ylabel("p99 latency (ms)")
    axes[0].legend(loc="lower right", fontsize=8)
    fig.suptitle("Each backend alone: p99 per probe; vertical line = capacity at SLO (mean of repeats)", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(root / "s1-denominators.png", dpi=130)
    md.append("![denominators](s1-denominators.png)\n")
    return d, total, cv


# --- Tuning ---

def tuning(root, md):
    path = root / "tuning" / "tuning.json"
    if not path.exists():
        return
    noise = json.loads((root / "tuning" / "noise.json").read_text())["measured_noise"]
    chosen = json.loads(path.read_text())
    md.append("## Tuning (D36)\n")
    md.append("Signal noise σ(P) (median over backends of the CV of P-second averages):\n")
    md.append("| signal | load | P=1 s | P=2 s | P=4 s | P=8 s |")
    md.append("|---|---|---|---|---|---|")
    for sig in SIGNALS:
        for load in sorted(noise):
            md.append(f"| {sig} | {float(load):.0%} | " + " | ".join(f"{noise[load][sig].get(p, float('nan')):.3f}" for p in ("1", "2", "4", "8")) + " |")
    md.append("\nChosen settings (same rule for every signal):\n")
    md.append("| signal | period | k | δ_on | δ_off | churn (moves/min, sim) | recovery after a backend loses half (sim) | note |")
    md.append("|---|---|---|---|---|---|---|---|")
    for c in chosen:
        k = c["chosen"]
        rec = f"{k['recovery_s']:.0f} s" if k["recovery_s"] is not None else "never"
        md.append(f"| {c['signal']} | {k['period_s']:g} s | {k['k']:g} | {k['delta_on']:.3f} | {k['delta_off']:.3f} | "
                  f"{k['churn_moves_per_min']:.0f} | {rec} | {c.get('note', '')} |")

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.4), sharey=True)
    for ax, load in zip(axes, sorted(noise)):
        for sig in SIGNALS:
            pts = sorted((int(p), v) for p, v in noise[load][sig].items())
            ax.plot(*zip(*pts), **mark(sig))
        ax.set_xscale("log", base=2)
        ax.set_xticks([1, 2, 4, 8], ["1", "2", "4", "8"])
        ax.set_xlim(0.8, 9)
        end_labels(ax, [(noise[load][sig]["8"], sig) for sig in SIGNALS if "8" in noise[load][sig]], 8)
        ax.set_xlabel("averaging window P (s)")
        ax.set_title(f"at {float(load):.0%} of oracle capacity", loc="left")
    axes[0].set_ylabel("noise σ (coefficient of variation)")
    fig.suptitle("Signal noise with a fixed table: longer windows average it out", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(root / "tuning-noise.png", dpi=130)

    chosen.sort(key=lambda c: TUNED_ORDER.index(c["signal"]))
    never = 1.12 * max([k["recovery_s"] or 0 for c in chosen for k in c["candidates"]] + [60])
    fig, axes = plt.subplots(1, len(chosen), figsize=(4.6 * len(chosen), 3.8), sharey=True)
    for ax, c in zip(axes, chosen):
        color = color_of(c["signal"])
        for k in c["candidates"]:
            y = k["recovery_s"] if k["recovery_s"] is not None else never
            marker = {1.5: "o", 2.0: "s", 3.0: "^"}[k["k"]]
            ax.plot(max(k["churn_moves_per_min"], 1), y, marker=marker, color=color, markeredgecolor=SURFACE,
                    markeredgewidth=1.5, alpha=1 if k["meets_churn_cap"] else 0.45)
            ax.annotate(f"{k['period_s']:g}s", (max(k["churn_moves_per_min"], 1), y), xytext=(5, 2),
                        textcoords="offset points", fontsize=7, color=INK2)
        k = c["chosen"]
        y = k["recovery_s"] if k["recovery_s"] is not None else never
        ax.plot(max(k["churn_moves_per_min"], 1), y, marker="o", markersize=16, markerfacecolor="none",
                markeredgecolor=INK, markeredgewidth=1.5)
        ax.axvline(41, color=INK2, linewidth=1, linestyle=(0, (4, 3)))
        ax.text(41, never * 0.97, " churn cap", color=INK2, fontsize=8, va="top")
        ax.axhline(never, color=GRID, linewidth=6, zorder=0)
        ax.set_xscale("log")
        ax.set_xlim(10, 6000)
        ax.text(12, never, "never recovers", color=INK2, fontsize=8, va="center")
        ax.set_title(f"{c['signal']}: P={k['period_s']:g} s, k={k['k']:g}", loc="left")
        ax.set_xlabel("steady-state churn (moves/min, sim)")
    axes[0].set_ylabel("recovery after the step (s, sim)")
    for kval, marker in ((1.5, "o"), (2.0, "s"), (3.0, "^")):
        axes[-1].plot([], [], marker=marker, linestyle="none", color=INK2, label=f"k = {kval:g}")
    axes[-1].legend(loc="lower right", fontsize=8)
    fig.suptitle("Tuning in the calibrated simulator: each point a (period, band multiplier); ring = chosen",
                 x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(root / "tuning-choice.png", dpi=130)
    md.append("\n![noise](tuning-noise.png)\n\n![choice](tuning-choice.png)\n")


# --- S2 ---

def s2(root, md, d, total, cv):
    runs = records(root / "s2" / "runs.jsonl") + records(root / "s2-extra" / "runs.jsonl") + records(root / "s2-rif-total" / "runs.jsonl")
    if not runs:
        return
    caps = [r for r in runs if r["kind"] == "capacity" and not r.get("invalid")]
    probes = [r for r in runs if r["kind"] == "probe" and not r.get("invalid")]
    md.append("## S2: static policies and controller-found tables (RQ1)\n")
    md.append(f"Usable capacity fraction = capacity at SLO through the LB ÷ sum of denominators ({total:.0f} req/s).\n")
    low, high = SLO_MS / 2, SLO_MS * 2.5  # other bounds, read off the same probes
    md.append(f"| policy | repeats | capacity at {SLO_MS:g} ms (req/s; ≥ = every probe passed, a lower bound) | usable fraction | range | "
              f"predicted from shares | at {low:g} ms | at {high:g} ms |")
    md.append("|---|---|---|---|---|---|---|---|")
    fractions, lower_bound = {}, {}
    for policy in POLICIES:
        rs = [c for c in caps if c["policy"] == policy and c["capacity"]]
        if not rs:
            continue
        values = [c["capacity"] for c in rs]
        fractions[policy] = [v / total for v in values]
        lower_bound[policy] = [c["flag"] == "above range" for c in rs]
        other = {}
        for bound in (low, high):
            vals = [capacity_at([p for p in probes if p["policy"] == policy and p["rep"] == c["rep"]], bound) for c in rs]
            vals = [v for v in vals if v]
            other[bound] = f"{statistics.mean(vals) / total:.1%}" if vals else "–"
        cells = ", ".join(("≥" if c["flag"] == "above range" else "") + f"{c['capacity']:.0f}" for c in rs)
        md.append(f"| {policy} | {len(rs)} | {cells} | **{statistics.mean(values) / total:.1%}** | "
                  f"{min(values) / total:.1%}–{max(values) / total:.1%} | {statistics.mean(c['predicted'] for c in rs) / total:.1%} | "
                  f"{other[low]} | {other[high]} |")
    md.append(f"\nResolution of these searches (highest pass to next failure): typically {typical_resolution(probes, 'policy', 'rep'):.1%}, "
              f"at worst {worst_resolution(probes, 'policy', 'rep'):.1%}.")
    oracle = [p for p in probes if p["policy"] == "oracle"]
    if any(p["meets_slo"] and any(not q["meets_slo"] and q["rate"] < p["rate"] for q in oracle) for p in oracle):
        md.append("Here p99 isn't monotonic with load near the SLO: an oracle probe passed above a rate where another "
                  "failed (plotted below), so a capacity depends on which probes the bisection happened to run.")

    fig, ax = plt.subplots(figsize=(8, 3.2))
    shown = [p for p in POLICIES if p in fractions]
    for i, policy in enumerate(shown):
        fs, color = fractions[policy], color_of(policy)
        bound = any(lower_bound[policy])  # a mean with a lower bound in it is a lower bound too
        if len(fs) > 1:  # with one run the dot is the mean: a bar would only hide it
            ax.plot([statistics.mean(fs)] * 2, [i - 0.28, i + 0.28], color=color, linewidth=3, solid_capstyle="round")
        for f, lb in zip(fs, lower_bound[policy]):
            if lb:  # hollow, pointing right: the real value is somewhere to the right of it
                ax.plot(f, i, marker=">", markerfacecolor=SURFACE, markeredgecolor=color, markeredgewidth=1.5, markersize=8)
                ax.annotate("", (f + 0.025, i), xytext=(f + 0.006, i), arrowprops=dict(arrowstyle="->", color=color, linewidth=1.2))
            else:
                ax.plot(f, i, **mark(policy))
        ax.text(max(fs) + (0.032 if bound else 0.012), i, ("≥ " if bound else "") + f"{statistics.mean(fs):.1%} (n={len(fs)})", va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(shown)), shown)
    ax.invert_yaxis()
    every = [f for fs in fractions.values() for f in fs]
    ax.set_xlim(min(0.5, min(every) - 0.03), max(1.1, max(every) + 0.1))
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_xlabel("usable capacity fraction (capacity at SLO ÷ sum of backends' own capacities)")
    ax.grid(axis="y", visible=False)
    ax.set_title("Dots = runs, bar = mean, hollow arrow = a lower bound (every probe passed)", loc="left")
    fig.tight_layout()
    fig.savefig(root / "s2-capacity.png", dpi=130)

    fig, ax = plt.subplots(figsize=(8, 3.8))
    for policy in POLICIES:
        pts = sorted((p["rate"] / total, p["summary"]["latency_ms"]["p99"]) for p in probes if p["policy"] == policy)
        if pts:
            ax.plot(*zip(*pts), linestyle="none", label=policy, **mark(policy))
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_xlabel("offered load (÷ sum of denominators)")
    ax.set_ylabel("p99 latency (ms)")
    latency_axis(ax)
    ax.legend(loc="upper left", fontsize=8, ncols=5)
    ax.set_title("Every probe of every search through the LB", loc="left")
    fig.tight_layout()
    fig.savefig(root / "s2-p99.png", dpi=130)

    logs = [log for d in ("s2", "s2-extra", "s2-rif-total") for log in sorted((root / d).glob("controller-*.csv"))]
    if logs:
        oracle = {b: d["capacity"][b] / total * 4096 for b in BACKENDS}
        fig, axes = plt.subplots(1, len(logs), figsize=(3.9 * len(logs), 3.2), sharey=True, squeeze=False)
        for ax, log in zip(axes[0], logs):
            rows = list(csv.DictReader(open(log)))
            t = [float(r["t"]) for r in rows]
            for b, color, marker in zip(BACKENDS, SLOTS, MARKERS):
                ax.plot(t, [int(r[f"buckets_{b}"]) for r in rows], color=color)
                ax.axhline(oracle[b], color=color, linewidth=1, linestyle=(0, (2, 3)))
            ax.set_title(log.stem.replace("controller-", ""), loc="left")
            ax.set_xlabel("time (s)")
            ax.set_xlim(0, t[-1])
            ax.set_ylim(550, 1450)
            end_labels(ax, [(int(rows[-1][f"buckets_{b}"]), b) for b in BACKENDS], t[-1])
        axes[0][0].set_ylabel("buckets (first hop)")
        fig.suptitle("Controller converging at 75% load from shares by cores; dotted = oracle shares", x=0.01, ha="left", fontsize=11)
        fig.tight_layout()
        fig.savefig(root / "s2-convergence.png", dpi=130)
        md.append("\n![capacity](s2-capacity.png)\n\n![p99](s2-p99.png)\n\n![convergence](s2-convergence.png)\n")

    health = [p["summary"] for p in probes]
    md.append(f"\nGenerator health over all S2 probes: worst dispatch lag p99 {max(h['dispatch_lag_ms']['p99'] for h in health):.3f} ms, "
              f"dropped {sum(h['gen_dropped'] for h in health)}.")
    delayed = [p for p in probes if p["summary"]["send_delay_ms"]["p99"] > 5]
    for p in delayed:
        md.append(f"Send delay p99 {p['summary']['send_delay_ms']['p99']:.0f} ms in one probe ({p['policy']} at {p['rate']:.0f} req/s): "
                  + ("it missed the SLO anyway (above capacity), so no result depends on it." if not p["meets_slo"]
                     else "**it met the SLO: check that result.**"))
    md.append("")


# --- S3 and S4: scenarios ---

CONFIGS = ["oracle", "hindsight", "cpu", "rif", "rif-total", "cgroup", "cpu-gain0.1", "cpu-gain1", "cpu-noprotect", "cpu-noredirect"]
WINDOW_S = 5
TOP_MS = 2000  # timelines are drawn up to this p99 (100 × the SLO, set in main)


def disturbance(run):
    """(onset, end) of the disturbance in seconds."""
    times = [at for at, _ in run["events"]]
    return times[0], (times[-1] if len(times) > 1 else run["length"])


def recovery(windows, onset, end):
    """Seconds from onset until the 5-s p99 stays within the SLO for 3 windows
    (15 s) in a row; None if that doesn't happen before the disturbance ends."""
    good = 0
    for i, p99 in enumerate(windows):
        start = i * WINDOW_S
        if start < onset or start >= end:
            continue
        good = good + 1 if p99 <= SLO_MS else 0
        if good == 3:
            return start - 2 * WINDOW_S - onset
    return None


def scenario_row(run):
    w = run["churn"]["window_p99_ms"]
    onset, end = disturbance(run)
    before = [x for i, x in enumerate(w) if onset / 2 <= i * WINDOW_S < onset]  # the first half is start-up
    during = [x for i, x in enumerate(w) if onset <= i * WINDOW_S < end]
    late = [x for i, x in enumerate(w) if onset + 60 <= i * WINDOW_S < end] or during
    # The window p99 only covers successful requests: when most requests time
    # out, the few that succeed look fast. So no recovery with > 0.1% errors.
    error_rate = run["churn"]["errors"] / max(run["churn"]["requests"], 1)
    return dict(
        before=statistics.median(before) if before else float("nan"),
        recovery=recovery(w, onset, end) if error_rate <= 0.001 else f"n/a ({100 * error_rate:.2g}% errors)",
        late=statistics.median(late) if late else float("nan"),
        over=sum(x > SLO_MS for x in during) / len(during) if during else float("nan"),
        errors=run["churn"]["errors"],
        long_broken=run["long_lived"]["errors"],
        long_reconnects=run["long_lived"]["connections"] - 100,
        resets=sum(run["rsts_sent"].values()),
        client_resets=run["client_resets"],
    )


def scenario_runs(root, sessions):
    """Scenario records from several sessions, each tagged with its session."""
    runs = []
    for session in sessions:
        for r in records(root / session / "runs.jsonl"):
            if r["kind"] == "scenario":
                runs.append({**r, "_session": session})
    return [r for r in runs if not r.get("invalid")]


def moves(root, session, name):
    """(moves, of which swaps) from a controller log."""
    path = root / session / f"{name}-controller.csv"
    if not path.exists():
        return 0, 0
    rows = list(csv.DictReader(open(path)))
    return sum(int(r["moves"]) for r in rows), sum(int(r["swaps"]) for r in rows)


def invalid_runs(root, md, sessions):
    """Runs whose controller died: listed, in no table or plot."""
    bad = [r for session in sessions for r in records(root / session / "runs.jsonl") if r.get("invalid")
           and r["kind"] in ("scenario", "converge")]
    if bad:
        md.append("**Invalid runs, left out of everything below:** " + "; ".join(
            f"{r.get('scenario', 'table')} {r.get('config', r.get('signal'))} rep {r['rep']} ({r['invalid'][:60]})" for r in bad) + "\n")


def scenarios(root, md, sessions, title, only=None):
    runs = [r for r in scenario_runs(root, sessions) if only is None or only(r["scenario"])]
    if not runs:
        return
    session = sessions[0]  # names the figures
    md.append(f"## {title}\n")
    md.append("Per run: p99 per 5-s window of the churn traffic (75% of the summed capacities unless the scenario's name says @60 or @85) plus "
              "long-lived connections. *Recovery*: seconds from the disturbance until p99 ≤ the SLO for 3 windows in "
              "a row. *Late p99*: median window p99 from 60 s after onset to the end of the disturbance.\n")
    for scenario in dict.fromkeys(r["scenario"] for r in runs):
        rs = [r for r in runs if r["scenario"] == scenario]
        onset, end = disturbance(rs[0])
        md.append(f"### {scenario}: {', '.join(f'{what} at {at} s' for at, what in rs[0]['events'][:3])}"
                  f"{' …' if len(rs[0]['events']) > 3 else ''}\n")
        md.append("| config | session | rep | p99 before | recovery | late p99 | windows over SLO | churn errors | long-lived broken | backend RSTs | bucket moves (swaps) |")
        md.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in sorted(rs, key=lambda r: (CONFIGS.index(r["config"]), r["rep"])):
            row = scenario_row(r)
            rec = row["recovery"]
            rec = "never" if rec is None else (rec if isinstance(rec, str) else f"{rec} s")
            mv, sw = moves(root, r["_session"], f"{scenario}-{r['config']}-{r['rep']}")
            md.append(f"| {r['config']} | {r['_session'].upper()} | {r['rep']} | {row['before']:.1f} | {rec} | {row['late']:.1f} | {row['over']:.0%} | "
                      f"{row['errors']} | {row['long_broken']} | {row['resets']} | {mv} ({sw}) |")
        md.append("")
        scenario_summary(md, rs)
        fig_timeline(root, session, scenario, rs, sessions[0])
        md.append(f"![{scenario}]({session}-{scenario.replace('@', '-at-')}.png)\n")


def scenario_summary(md, rs):
    """Per config, over all its runs of one scenario: ranges and the median."""
    md.append("Per config, over its runs (range; median in brackets):\n")
    md.append("| config | runs | recovery (s) | windows over SLO | late p99 (ms) | long-lived broken | backend RSTs |")
    md.append("|---|---|---|---|---|---|---|")
    for config in [c for c in CONFIGS if any(r["config"] == c for r in rs)]:
        rows = [scenario_row(r) for r in rs if r["config"] == config]
        rec = [r["recovery"] for r in rows]
        if all(isinstance(x, (int, float)) for x in rec):
            rec_text = f"{min(rec):g}–{max(rec):g} ({statistics.median(rec):g})"
        else:
            rec_text = ", ".join("never" if x is None else str(x) for x in dict.fromkeys(rec))
        over, late = [r["over"] for r in rows], [r["late"] for r in rows]
        md.append(f"| {config} | {len(rows)} | {rec_text} | {min(over):.0%}–{max(over):.0%} ({statistics.median(over):.0%}) | "
                  f"{min(late):.1f}–{max(late):.1f} ({statistics.median(late):.1f}) | {sum(r['long_broken'] for r in rows)} | "
                  f"{sum(r['resets'] for r in rows)} |")
    md.append("")


def fig_timeline(root, session, scenario, rs, main_session):
    """One panel per config. Runs from another session than main_session are
    dashed, and their panel says which: they weren't interleaved with the rest."""
    configs = [c for c in CONFIGS if any(r["config"] == c for r in rs)]
    fig, axes = plt.subplots(1, len(configs), figsize=(3.4 * len(configs), 3.3), sharey=True, squeeze=False)
    for ax, config in zip(axes[0], configs):
        clipped = False
        for r in [r for r in rs if r["config"] == config]:
            w = r["churn"]["window_p99_ms"]
            # Above 2 s, or no successful request in the window (p99 = 0): drawn at the top.
            y = [TOP_MS if x == 0 or x > TOP_MS else x for x in w]
            clipped |= any(x == 0 or x > TOP_MS for x in w)
            ax.plot([i * WINDOW_S + WINDOW_S / 2 for i in range(len(w))], y, color=color_of(config), linewidth=1.5, alpha=0.9,
                    linestyle="-" if r["_session"] == main_session else (0, (3, 1.5)))
        if clipped:
            ax.text(0.02, 0.97, "top edge: p99 > 2 s or\nevery request timed out", transform=ax.transAxes,
                    fontsize=7, color=INK2, va="top")
        for at, what in r["events"]:
            if "off" not in what:
                ax.axvspan(at, next((a for a, x in r["events"] if a > at and "off" in x), r["length"]),
                           color=GRID, alpha=0.6, linewidth=0, zorder=0)
        here = [x for x in rs if x["config"] == config]
        others = sorted({x["_session"].upper() for x in here if x["_session"] != main_session})
        ax.set_title(f"{config} (×{len(here)}" + (f", {', '.join(others)}" if others else "") + ")", loc="left")
        ax.set_xlabel("time (s)")
        ax.set_ylim(SLO_MS / 4, TOP_MS * 1.05)
        latency_axis(ax)
    axes[0][0].set_ylabel("p99 per 5 s (ms)")
    fig.suptitle(f"{scenario}: p99 over time, one line per repeat; grey = disturbance; dashed = run from a session other "
                 f"than {main_session.upper()}", x=0.01, ha="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(root / f"{session}-{scenario.replace('@', '-at-')}.png", dpi=130)
    plt.close(fig)


def signals_seen(root, md, session, scenario, backend, title):
    """Which signals see a disturbance: each signal of `backend` relative to
    its own average before the disturbance (static oracle run, no controller)."""
    run = next((r for r in records(root / session / "runs.jsonl")
                if r.get("kind") == "scenario" and r["scenario"] == scenario and r["config"] == "oracle"), None)
    if not run:
        return
    rows = list(csv.DictReader(open(root / session / f"{scenario}-oracle-1-sigmon.csv")))
    onset, end = disturbance(run)
    fig, ax = plt.subplots(figsize=(8, 3.4))
    md.append(f"### {title}\n")
    md.append(f"{backend}'s signals in the static-oracle run, relative to their own mean in the {onset / 2:g} s before the disturbance:\n")
    md.append("| signal | before | during (mean) | ratio |")
    md.append("|---|---|---|---|")
    for sig in SIGNALS:
        t = [float(r["t"]) for r in rows]
        v = [float(r[f"{sig}_{backend}"]) for r in rows]
        base = statistics.mean(x for ti, x in zip(t, v) if onset / 2 <= ti < onset) or float("nan")
        during = statistics.mean(x for ti, x in zip(t, v) if onset + 5 <= ti < end)
        md.append(f"| {sig} | {base:.3f} | {during:.3f} | **{during / base:.2f}×** |")
        smooth = [statistics.mean(v[max(0, i - 2):i + 3]) / base for i in range(len(v))]  # 5-s centred mean
        # Markers too: psi's yellow and rif's orange are close in colour.
        ax.plot(t, smooth, label=sig, markevery=15, **mark(sig))
    ax.axvspan(onset, end, color=GRID, alpha=0.6, linewidth=0, zorder=0)
    ax.axhline(1, color=INK2, linewidth=1)
    ax.set_yscale("log")
    ticks = [0.5, 1, 2, 5, 10, 20, 50]
    ax.set_ylim(0.4, max(60, ax.get_ylim()[1]))
    ax.set_yticks(ticks, [f"{v:g}×" for v in ticks])
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_xlabel("time (s)")
    ax.set_ylabel("relative to before (log)")
    ax.legend(loc="upper left", fontsize=8, ncols=4)
    ax.set_title(f"{backend}'s signals (5-s mean); grey = disturbance", loc="left")
    fig.tight_layout()
    name = f"{session}-signals-{scenario}.png"
    fig.savefig(root / name, dpi=130)
    plt.close(fig)
    md.append(f"\n![signals]({name})\n")


def sweep(root, md):
    """RQ2/H3: share of 5-s windows over the SLO during the disturbance, by
    flapping period. The slowest point is S3's antagonist (120 s on, 60 s off).
    S4's "flapping" (10 s on, 10 s off, 60-180 s) is a second 20-s point."""
    runs = scenario_runs(root, ["s3", "s4", "s5", "s10"])
    period = {"fastflap-2": 2, "flapping-5": 5, "flapping": 20, "flapping-20": 20, "flapping-60": 60, "antagonist": 240}
    pts = {}
    for r in runs:
        if r["scenario"] in period and r["config"] in ("oracle", "cpu", "rif", "rif-total"):
            pts.setdefault(r["config"], {}).setdefault(period[r["scenario"]], []).append(scenario_row(r)["over"])
    if not pts or not {p for c in pts.values() for p in c} - {240}:  # one antagonist run alone isn't a sweep
        return
    md.append("### Timescale sweep (RQ2, H3)\n")
    md.append("Share of 5-s windows with p99 over the SLO during the disturbance (antagonist on beE1 for half of "
              "each period). 2 s (S10): the neighbour switches itself every second, faster than the control period and the "
              "connection lifetime. 20 s includes S4's flapping run (10 s on/off from 60 s). 240 s = S3's antagonist run "
              "(120 s on, once): it covers only the on-phase while the others cover on and off, so it isn't "
              "comparable with them. *oracle* here is the table set for the capacities before the disturbance; no table that "
              "knows the antagonist was run on flapping. Median over runs where there are several (number of runs in brackets):\n")
    periods = sorted({p for c in pts.values() for p in c})
    md.append("| config | " + " | ".join(f"{p} s" + (" (on-phase only)" if p == 240 else "") for p in periods) + " |")
    md.append("|---|" + "---|" * len(periods))
    fig, ax = plt.subplots(figsize=(8, 3.8))
    count_above = {"oracle": 7, "cpu": -13, "rif": 7, "rif-total": 7}  # where each config's run counts go
    count_beside = {"oracle": -3, "cpu": -3, "rif": 2, "rif-total": -3}
    for config in ("oracle", "cpu", "rif", "rif-total"):
        if config not in pts:
            continue
        cells = [f"{statistics.median(pts[config][p]):.0%} ({len(pts[config][p])})" if p in pts[config] else "–" for p in periods]
        md.append(f"| {config} | " + " | ".join(cells) + " |")
        # The flapping points are joined only where neighbouring periods both have data. S3's single
        # 120-s on-phase counts no off-phase, unlike them: a separate hollow point, never joined.
        flap = [p for p in periods if p != 240]
        segment = []
        for p in flap + [None]:
            if p in pts[config]:
                segment.append(p)
                continue
            if segment:
                ax.plot(segment, [statistics.median(pts[config][x]) for x in segment], **mark(config),
                        label=config if not ax.get_legend_handles_labels()[1].count(config) else None)
            segment = []
        if 240 in pts[config]:
            ax.plot(240, statistics.median(pts[config][240]), linestyle="none", marker=marker_of(config),
                    markerfacecolor=SURFACE, markeredgecolor=color_of(config), markeredgewidth=1.5,
                    label=config if not ax.get_legend_handles_labels()[1].count(config) else None)
        for p, values in pts[config].items():
            # At the separate point the markers sit close together, so the counts go beside them.
            offset, ha = ((9, count_beside[config]), "left") if p == 240 else ((0, count_above[config]), "center")
            ax.annotate(f"n={len(values)}", (p, statistics.median(values)), xytext=offset, ha=ha,
                        textcoords="offset points", fontsize=7, color=INK2)
    ax.set_xscale("log")
    ax.set_xticks(periods, [f"{p} s" if p != 240 else "120 s on, once\n(on-phase only:\nnot comparable)" for p in periods])
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_xlim(4 if min(periods) >= 5 else 1.6, 420)
    if 240 in periods:
        ax.axvline(120, color=INK2, linewidth=1, linestyle=(0, (2, 3)))
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.set_ylim(-0.06, 1.1)
    ax.set_xlabel("disturbance period (on for half of it)")
    ax.set_ylabel("windows over SLO")
    ax.legend(loc="upper center", fontsize=8, ncols=4)
    ax.set_title("Share of the disturbance spent over the SLO (n = runs)", loc="left")
    fig.tight_layout()
    fig.savefig(root / "sweep.png", dpi=130)
    plt.close(fig)
    md.append("\n![sweep](sweep.png)\n")


def sibling_stream(root, md):
    """H6 with an antagonist that contends: beP1's signals and p99 per phase."""
    run = next((r for r in scenario_runs(root, ["s5"]) if r["scenario"] == "sibling-stream"), None)
    if not run:
        return
    rows = list(csv.DictReader(open(root / "s5" / "sibling-stream-oracle-1-sigmon.csv")))
    w = run["churn"]["window_p99_ms"]
    ev = [at for at, _ in run["events"]]
    phases = [("before", 0, ev[0]), ("burn on the siblings", ev[0], ev[1]), ("stream on the siblings", ev[2], ev[3])]
    md.append("### Sibling antagonist that contends (H6)\n")
    md.append("Static oracle; antagonist on the idle hyperthreads of beP1's two cores. Mean per phase (first 5 s skipped):\n")
    md.append("| phase | p99 (median of windows) | " + " | ".join(f"beP1 {s}" for s in SIGNALS) + " |")
    md.append("|---|---|" + "---|" * len(SIGNALS))
    for label, a, b in phases:
        sel = [r for r in rows if a + 5 <= float(r["t"]) < b]
        win = [x for i, x in enumerate(w) if a + 5 <= i * WINDOW_S < b]
        md.append(f"| {label} | {statistics.median(win) if win else float('nan'):.1f} | " +
                  " | ".join(f"{statistics.mean(float(r[f'{s}_beP1']) for r in sel):.3f}" for s in SIGNALS) + " |")
    md.append("")


def kernel_psi(root, md):
    runs = [r for r in records(root / "s4" / "runs.jsonl") if r["kind"] == "kernelpsi"]
    if not runs:
        return
    md.append("### PSI with a kernel-queue backend (D25)\n")
    md.append("All traffic to beP2. Mean of each signal over each 40-s phase (first 5 s skipped):\n")
    md.append("| mode | phase | p99 (ms) | PSI | whole-core CPU | RIF per core | cgroup CPU |")
    md.append("|---|---|---|---|---|---|---|")
    fig, ax = plt.subplots(figsize=(7, 3.2))
    for run, marker in zip(runs, ("o", "s")):
        rows = list(csv.DictReader(open(root / "s4" / f"kernelpsi-{run['mode']}-sigmon.csv")))
        psi = []
        for ph in run["phases"]:
            sel = [r for r in rows if ph["start"] + 5 <= float(r["t"]) < ph["end"]]
            m = {sig: statistics.mean(float(r[f"{sig}_beP2"]) for r in sel) for sig in SIGNALS}
            psi.append(m["psi"])
            md.append(f"| {run['mode']} | {ph['phase']} | {ph['p99']:.0f} | **{m['psi']:.2f}** | {m['cpu']:.2f} | {m['rif']:.2f} | {m['cgroup']:.2f} |")
        ax.plot(range(len(psi)), psi, marker=marker, color=color_of("psi"), markeredgecolor=SURFACE, markeredgewidth=1.5,
                linestyle="-" if run["mode"] == "kernel" else (0, (3, 1.5)), label=f"{run['mode']} mode")
        ax.annotate(f"{run['mode']}", (len(psi) - 1, psi[-1]), xytext=(6, 0), textcoords="offset points", va="center",
                    fontsize=8, color=INK)
    ax.set_xticks(range(len(runs[0]["phases"])), [ph["phase"] for ph in runs[0]["phases"]])
    ax.set_xlim(-0.3, len(runs[0]["phases"]) - 0.4)
    ax.set_ylabel("PSI (some), beP2")
    ax.set_ylim(0, 1.05)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("PSI sees request overload only when requests wait in the kernel", loc="left")
    fig.tight_layout()
    fig.savefig(root / "s4-kernelpsi.png", dpi=130)
    plt.close(fig)
    md.append("\n![kernel psi](s4-kernelpsi.png)\n")


def hook_cost(root, md):
    runs = [dict(r, _session=s) for s in ("s4", "s10") for r in records(root / s / "runs.jsonl") if r["kind"] == "hookcost"]
    if not runs:
        return
    md.append("### Director at TC vs XDP (D35)\n")
    md.append("CPU time per packet on the router/LB CPU (CPU 6: router forwarding + bridge + director, so not the "
              "director alone); same program logic and output bytes for both hooks. Busy time is counted in ticks of "
              "10 ms. S4: 1 run of 20 s per rate. S10: 4 repeats of 40 s per rate, the hooks' order alternating:\n")
    md.append("| session | repeat | hook | rate (req/s) | packets | busy ticks (10 ms each) | ns per packet | p99 (ms) |")
    md.append("|---|---|---|---|---|---|---|---|")
    for r in runs:
        md.append(f"| {r['_session'].upper()} | {r.get('rep', 1)} | {r['hook']} | {r['rate']} | {r['packets']} | {r['busy_ticks']} | "
                  f"{r['ns_per_packet']:.0f} | {r['p99']:.1f} |")
    later = [r for r in runs if r["_session"] == "s10"]
    if later:
        md.append("\nS10, per hook and rate (ns per packet; XDP ÷ TC pairs the two hooks of one repeat):\n")
        md.append("| rate (req/s) | TC: mean (range) | XDP: mean (range) | XDP ÷ TC per repeat | mean ratio, 95% interval |")
        md.append("|---|---|---|---|---|")
        for rate in sorted({r["rate"] for r in later}):
            by = {h: {r["rep"]: r["ns_per_packet"] for r in later if r["hook"] == h and r["rate"] == rate} for h in ("tc", "xdp")}
            ratios = [by["xdp"][k] / by["tc"][k] for k in sorted(by["tc"]) if k in by["xdp"]]
            cell = lambda v: f"{statistics.mean(v):.0f} ({min(v):.0f}–{max(v):.0f})" if v else "–"
            m, half = mean_ci(ratios)
            md.append(f"| {rate} | {cell(list(by['tc'].values()))} | {cell(list(by['xdp'].values()))} | "
                      f"{', '.join(f'{x:.2f}' for x in ratios)} | {m:.2f} ± {half:.2f} |")
    md.append("")


# --- small-sample statistics ---

T975 = {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


def t975(df):
    """97.5% quantile of Student's t (the largest tabulated df not above df: slightly wide)."""
    below = [k for k in T975 if k <= df]
    return T975[max(below)] if below else float("nan")


def mean_ci(values):
    """(mean, half-width of its 95% interval); nan half-width for fewer than 2 values."""
    n = len(values)
    if n < 2:
        return (values[0] if values else float("nan")), float("nan")
    return statistics.mean(values), t975(n - 1) * statistics.stdev(values) / math.sqrt(n)


def diff_ci(a, b):
    """(mean(a) - mean(b), half-width of its 95% interval), Welch: unequal variances."""
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    if va + vb == 0:
        return statistics.mean(a) - statistics.mean(b), 0.0
    df = (va + vb) ** 2 / (va ** 2 / (len(a) - 1) + vb ** 2 / (len(b) - 1))
    return statistics.mean(a) - statistics.mean(b), t975(int(df)) * math.sqrt(va + vb)


# --- S8: RQ1 with 5 runs each ---

def s8(root, md, d, total):
    runs = records(root / "s8" / "runs.jsonl")
    caps = [r for r in runs if r["kind"] == "capacity" and not r.get("invalid")]
    probes = [r for r in runs if r["kind"] == "probe" and not r.get("invalid")]
    tables = [c for c in caps if c.get("policy")]
    if not tables:
        return
    md.append("## S8: RQ1 again, 5 runs each, interleaved (one session, checkpointed)\n")
    starts = [r for r in runs if r["kind"] == "calibration"]
    md.append(f"Session started {len(starts)} time(s) (each start calibrates anew: "
              f"{', '.join(str(c['work']) for c in starts)} work units per request). "
              f"Generator health over all {len(probes)} probes: worst dispatch lag p99 "
              f"{max(p['summary']['dispatch_lag_ms']['p99'] for p in probes):.3f} ms, worst send delay p99 of a passing probe "
              f"{max([p['summary']['send_delay_ms']['p99'] for p in probes if p['meets_slo']] or [0]):.2f} ms, "
              f"dropped {sum(p['summary']['gen_dropped'] for p in probes)}.\n")
    md.append("Run order: " + " → ".join(f"{c['policy']} {c['rep']}" for c in tables) + "\n")

    alone = {c["backend"]: c["capacity"] for c in caps if c.get("experiment") == "denominator-alone" and c["capacity"]}
    own = sum(alone.values()) if len(alone) == len(BACKENDS) else None
    if own:
        md.append("Each backend alone in this session (1 search each): " +
                  ", ".join(f"{b} {alone[b]:.0f} (S1: {d['capacity'][b]:.0f})" for b in BACKENDS) +
                  f"; sum **{own:.0f}** req/s (S1: {total:.0f}).\n")

    md.append("| table | runs | capacities (req/s; ≥ = every probe passed) | mean ÷ S1 sum | 95% interval of the mean | range | "
              "std dev of a run | mean ÷ S8's own sum |")
    md.append("|---|---|---|---|---|---|---|---|")
    fr = {}
    for policy in POLICIES:
        rs = [c for c in tables if c["policy"] == policy and c["capacity"]]
        if not rs:
            continue
        fs = fr[policy] = [c["capacity"] / total for c in rs]
        m, half = mean_ci(fs)
        cells = ", ".join(("≥" if c["flag"] == "above range" else "") + f"{c['capacity']:.0f}" for c in rs)
        sd = f"{statistics.stdev(fs):.1%}" if len(fs) > 1 else "–"
        md.append(f"| {policy} | {len(fs)} | {cells} | **{m:.1%}** | ± {half:.1%} | {min(fs):.1%}–{max(fs):.1%} | {sd} | "
                  f"{m * total / own:.1%} |" if own else
                  f"| {policy} | {len(fs)} | {cells} | **{m:.1%}** | ± {half:.1%} | {min(fs):.1%}–{max(fs):.1%} | {sd} | – |")
    if not fr:
        return
    edge = [c for c in tables if c["flag"]]
    if edge:
        md.append("\nSearches that ended at an edge of their range: " +
                  ", ".join(f"{c['policy']} {c['rep']} ({c['flag']})" for c in edge) + ".")
    md.append(f"\nResolution of these searches (highest pass to next failure): typically "
              f"{typical_resolution([p for p in probes if p.get('policy')], 'policy', 'rep'):.1%}, at worst "
              f"{worst_resolution([p for p in probes if p.get('policy')], 'policy', 'rep'):.1%}.\n")

    pairs = [("oracle", "ctl-cpu"), ("oracle", "ctl-rif-total"), ("ctl-cpu", "ctl-rif-total"), ("oracle", "cores"), ("cores", "equal")]
    pairs = [(a, b) for a, b in pairs if len(fr.get(a, [])) > 1 and len(fr.get(b, [])) > 1]
    if pairs:
        md.append("Differences between tables, in points of usable fraction, with 95% intervals (Welch). *Resolved* = the "
                  "interval excludes 0:\n")
        md.append("| a − b | difference | 95% interval | resolved? |")
        md.append("|---|---|---|---|")
        for a, b in pairs:
            diff, half = diff_ci(fr[a], fr[b])
            md.append(f"| {a} − {b} | {100 * diff:+.1f} | {100 * (diff - half):+.1f} to {100 * (diff + half):+.1f} | "
                      f"{'**yes**' if abs(diff) > half else 'no'} |")
        md.append("")

    # S6's interleaved runs of the same tables, same search range: pooled
    old = [c for c in records(root / "s6" / "runs.jsonl") if c["kind"] == "capacity" and not c.get("invalid") and c["capacity"]]
    pooled = {p: fr[p] + [c["capacity"] / total for c in old if c.get("policy") == p] for p in ("oracle", "ctl-cpu", "ctl-rif-total") if p in fr}
    if any(len(pooled[p]) > len(fr[p]) for p in pooled):
        md.append("Pooled with S6's runs of the same tables (same protocol and range, five days earlier, both ÷ S1's sum):\n")
        md.append("| table | runs | mean | 95% interval of the mean | range |")
        md.append("|---|---|---|---|---|")
        for p, fs in pooled.items():
            m, half = mean_ci(fs)
            md.append(f"| {p} | {len(fs)} | {m:.1%} | ± {half:.1%} | {min(fs):.1%}–{max(fs):.1%} |")
        if len(pooled.get("ctl-cpu", [])) > 1 and len(pooled.get("ctl-rif-total", [])) > 1:
            for a, b in (("ctl-cpu", "ctl-rif-total"), ("oracle", "ctl-cpu"), ("oracle", "ctl-rif-total")):
                diff, half = diff_ci(pooled[a], pooled[b])
                md.append(f"\nPooled {a} − {b}: {100 * diff:+.1f} points, 95% interval {100 * (diff - half):+.1f} to "
                          f"{100 * (diff + half):+.1f}: {'resolved' if abs(diff) > half else 'not resolved'}.")
        md.append("")

    fig, ax = plt.subplots(figsize=(8, 3.2))
    shown = [p for p in POLICIES if p in fr]
    for i, policy in enumerate(shown):
        fs = fr[policy]
        m, half = mean_ci(fs)
        if len(fs) > 1:
            ax.plot([m - half, m + half], [i + 0.3, i + 0.3], color=color_of(policy), linewidth=1.5)
            ax.plot([m] * 2, [i - 0.28, i + 0.28], color=color_of(policy), linewidth=3, solid_capstyle="round")
        for c, f in zip([c for c in tables if c["policy"] == policy and c["capacity"]], fs):
            if c["flag"] == "above range":
                ax.plot(f, i, marker=">", markerfacecolor=SURFACE, markeredgecolor=color_of(policy), markeredgewidth=1.5)
            else:
                ax.plot(f, i, **mark(policy))
        ax.text(max(max(fs), m + (half if len(fs) > 1 else 0)) + 0.008, i, f"{m:.1%} (n={len(fs)})", va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(shown)), shown)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    every = [f for fs in fr.values() for f in fs]
    ax.set_xlim(min(every) - 0.03, max(every) + 0.09)
    ax.set_xlabel("capacity at SLO ÷ sum of S1 denominators")
    ax.grid(axis="y", visible=False)
    ax.set_title("S8, interleaved in one session; dots = runs, bar = mean, thin line = its 95% interval", loc="left")
    fig.tight_layout()
    fig.savefig(root / "s8-tables.png", dpi=130)
    plt.close(fig)
    md.append("![tables](s8-tables.png)\n")


# --- S6: denominators with neighbours busy, the gap, hindsight tables, interleaved tables ---

def rate_spread(rows, sigmon_start, probe):
    """CV of the per-second request rate, per backend, over one probe's measured
    phase (from its sigmon log): how evenly the requests arrive at the backends."""
    summary = probe["summary"]
    end = probe["finished"] - sigmon_start - 2.5  # roughly: the generator's exit, before its result is in
    sel = [r for r in rows if end - summary["duration_s"] <= float(r["t"]) < end]
    return {b: statistics.pstdev(v) / statistics.mean(v) for b in BACKENDS
            if (v := [float(r[f"req_per_s_{b}"]) for r in sel])}


def s6(root, md, d, total):
    runs = records(root / "s6" / "runs.jsonl")
    if not runs:
        return
    probes = [r for r in runs if r["kind"] == "probe" and not r.get("invalid")]
    caps = [r for r in runs if r["kind"] == "capacity" and not r.get("invalid")]
    def found(experiment, **match):
        return [c for c in caps if c.get("experiment") == experiment and all(c.get(k) == v for k, v in match.items())]
    md.append("## S6: interleaved follow-ups (one session)\n")
    md.append(f"Generator health over all {len(probes)} probes: worst dispatch lag p99 {max(p['summary']['dispatch_lag_ms']['p99'] for p in probes):.3f} ms, "
              f"worst send delay p99 {max(p['summary']['send_delay_ms']['p99'] for p in probes):.2f} ms, dropped {sum(p['summary']['gen_dropped'] for p in probes)}.\n")

    # --- denominators
    alone, busy = {}, {}
    for b in BACKENDS:
        alone[b] = next((c["capacity"] for c in found("denominator-alone", backend=b)), None)
        busy[b] = next((c["capacity"] for c in found("denominator-busy", backend=b)), None)
    if all(alone.values()) and all(busy.values()):
        s_alone, s_busy = sum(alone.values()), sum(busy.values())
        md.append("### Denominators: alone vs neighbours busy (no LB)\n")
        md.append("Same protocol, one round, order alternating per backend. *Busy*: the other three backends held at 90% of "
                  "their S1 capacity, generated by one load generator, this backend's requests judged.\n")
        md.append("| backend | alone, S1 (mean of 2) | alone, S6 | busy, S6 | busy ÷ alone (S6) |")
        md.append("|---|---|---|---|---|")
        for b in BACKENDS:
            md.append(f"| {b} | {d['capacity'][b]:.0f} | {alone[b]:.0f} | {busy[b]:.0f} | {busy[b] / alone[b]:.3f} |")
        md.append(f"| sum | {total:.0f} | {s_alone:.0f} | {s_busy:.0f} | {s_busy / s_alone:.3f} |")
        res = worst_resolution([p for p in probes if str(p.get("experiment")).startswith("denominator")], "experiment", "backend")
        md.append(f"\nCoarsest search resolution here: {res:.1%}.\n")

        # --- the gap
        oracle = [c for c in found("static", policy="oracle")]
        direct = [c for c in found("direct-mix")]
        if oracle and direct:
            md.append("### The oracle's gap: which denominator, and with or without the LB\n")
            md.append("Oracle through the LB vs the same shares sent directly to the backends (no LB, no hashing: each "
                      "request goes to a backend at random in proportion, one load generator), same range, interleaved.\n")
            md.append("| | capacities (req/s) | mean | ÷ S1 alone | ÷ S6 alone | ÷ S6 busy |")
            md.append("|---|---|---|---|---|---|")
            for label, rs in (("oracle through the LB", oracle), ("same shares, no LB", direct)):
                values = [c["capacity"] for c in rs]
                mean = statistics.mean(values)
                cells = ", ".join(("≥" if c["flag"] == "above range" else "") + f"{c['capacity']:.0f}" for c in rs)
                md.append(f"| {label} | {cells} | {mean:.0f} | {mean / total:.1%} | {mean / s_alone:.1%} | {mean / s_busy:.1%} |")
            starts = {r["name"]: r["started"] for r in runs if r["kind"] == "sigmon_start"}
            spread = {}
            for label, policy in (("oracle through the LB", "oracle"), ("same shares, no LB", "direct")):
                out = []
                for c in (oracle if policy == "oracle" else direct):
                    ps = [p for p in probes if p.get("policy") == policy and p["rep"] == c["rep"] and p["meets_slo"]]
                    if not ps:
                        continue
                    rows = list(csv.DictReader(open(root / "s6" / f"{policy}-{c['rep']}-sigmon.csv")))
                    out += list(rate_spread(rows, starts[f"{policy}-{c['rep']}"], max(ps, key=lambda p: p["rate"])).values())
                spread[label] = out
            md.append("\nHow evenly requests arrive: CV of each backend's requests per second, over the highest passing probe "
                      "of each search: " + "; ".join(f"{k} {min(v):.1%}–{max(v):.1%}" for k, v in spread.items() if v) + ".\n")

    # --- capacities during the disturbances
    dist = found("disturbed")
    if dist:
        md.append("### Capacities during the disturbances, and the hindsight tables\n")
        md.append("Each disturbed backend alone (no LB), 1 search. The hindsight table is proportional to the S1 capacities "
                  "with this one replaced.\n")
        md.append("| disturbance | backend | capacity alone, S1 | during it, S6 | ratio | sum of capacities during it | 75% load is |")
        md.append("|---|---|---|---|---|---|---|")
        for c in dist:
            b = c["backend"]
            left = total - d["capacity"][b] + c["capacity"]
            md.append(f"| {c['disturbance']} | {b} | {d['capacity'][b]:.0f} | {c['capacity']:.0f} | {c['capacity'] / d['capacity'][b]:.2f} | "
                      f"{left:.0f} | {0.75 * total / left:.0%} of it |")
        md.append("")

    # --- tables
    tables = [c for c in caps if c.get("experiment") == "controller"]
    if tables:
        md.append("### CPU vs total RIF tables, interleaved\n")
        md.append("Run order, all in one session, same search range (0.85–1.15 × the prediction from the table's shares), each "
                  "table after 90 s at 75% from shares by cores:\n")
        order = [c for c in caps if c.get("policy") in ("ctl-cpu", "ctl-rif-total", "oracle", "direct")]
        md.append(" → ".join(f"{c['policy']} {c['rep']}" for c in order) + "\n")
        md.append("| table | run | capacity (req/s) | ÷ S1 alone | predicted from its shares | moves in the first reading |")
        md.append("|---|---|---|---|---|---|")
        for policy in ("ctl-cpu", "ctl-rif-total"):
            for c in [c for c in tables if c["policy"] == policy]:
                log = list(csv.DictReader(open(root / "s6" / f"controller-{policy[4:]}-{c['rep']}.csv")))
                md.append(f"| {policy} | {c['rep']} | {'≥' if c['flag'] == 'above range' else ''}{c['capacity']:.0f} | {c['capacity'] / total:.1%} | "
                          f"{c['predicted'] / total:.1%} | {log[0]['moves']} |")
            values = [c["capacity"] for c in tables if c["policy"] == policy]
            md.append(f"| {policy} | mean | {statistics.mean(values):.0f} | {statistics.mean(values) / total:.1%} | | |")
        fig, ax = plt.subplots(figsize=(8, 2.6))
        names = [p for p in ("oracle", "direct", "ctl-cpu", "ctl-rif-total") if any(c.get("policy") == p for c in caps)]
        for i, policy in enumerate(names):
            rs = [c for c in caps if c.get("policy") == policy]
            fs = [c["capacity"] / total for c in rs]
            ax.plot([statistics.mean(fs)] * 2, [i - 0.28, i + 0.28], color=color_of(policy), linewidth=3, solid_capstyle="round")
            for c, f in zip(rs, fs):
                if c["flag"] == "above range":
                    ax.plot(f, i, marker=">", markerfacecolor=SURFACE, markeredgecolor=color_of(policy), markeredgewidth=1.5)
                else:
                    ax.plot(f, i, **mark(policy))
            ax.text(max(fs) + 0.006, i, f"{statistics.mean(fs):.1%} (n={len(fs)})", va="center", color=INK, fontsize=9)
        ax.set_yticks(range(len(names)), [{"direct": "same shares, no LB"}.get(n, n) for n in names])
        ax.invert_yaxis()
        ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
        ax.set_xlim(0.82, 1.08)
        ax.set_xlabel("capacity at SLO ÷ sum of S1 denominators")
        ax.grid(axis="y", visible=False)
        ax.set_title("S6, interleaved in one session; dots = runs, bar = mean", loc="left")
        fig.tight_layout()
        fig.savefig(root / "s6-tables.png", dpi=130)
        plt.close(fig)
        md.append("\n![tables](s6-tables.png)\n")


# --- S14: p99 at fixed loads; S15: overhead ---

def s14(root, md, d, total):
    runs = [r for r in records(root / "s14" / "runs.jsonl") if not r.get("invalid")]
    fixed = [r for r in runs if r["kind"] == "fixed"]
    if fixed:
        md.append("## S14: p99 at fixed loads (RQ1 without a threshold search)\n")
        md.append("Each table at fixed loads through the LB, 60 s measured per run, interleaved rounds; controller tables are "
                  "found anew each round (90 s at 75% from shares by cores, then frozen). Mean with the 95% interval of the "
                  "mean; range; runs within the SLO.\n")
        order = list(dict.fromkeys(f"{r['policy']} {r['rep']}" for r in fixed))
        md.append("Run order: " + " → ".join(order) + "\n")
        loads = sorted({r["load"] for r in fixed})
        policies = [p for p in ("cores", "oracle", "ctl-cpu", "ctl-rif-total") if any(r["policy"] == p for r in fixed)]
        md.append("| table | load | runs | p99 (ms): mean ± 95% | range | p50 (ms) | error rate (max) | runs within the SLO |")
        md.append("|---|---|---|---|---|---|---|---|")
        cell = {}
        for policy in policies:
            for load in loads:
                rs = [r for r in fixed if r["policy"] == policy and r["load"] == load]
                if not rs:
                    continue
                p99 = cell[policy, load] = [r["summary"]["latency_ms"]["p99"] for r in rs]
                m, half = mean_ci(p99)
                md.append(f"| {policy} | {load:.0%} | {len(rs)} | {m:.1f} ± {half:.1f} | {min(p99):.1f}–{max(p99):.1f} | "
                          f"{statistics.mean(r['summary']['latency_ms']['p50'] for r in rs):.2f} | "
                          f"{max(r['summary']['error_rate'] for r in rs):.4f} | {sum(r['meets_slo'] for r in rs)} of {len(rs)} |")
        md.append("\nDifferences in p99 (ms) with 95% intervals (Welch); *resolved* = the interval excludes 0:\n")
        md.append("| a − b | load | difference | 95% interval | resolved? |")
        md.append("|---|---|---|---|---|")
        for a, b in (("ctl-cpu", "oracle"), ("ctl-rif-total", "oracle"), ("ctl-cpu", "ctl-rif-total"), ("cores", "oracle")):
            for load in loads:
                if len(cell.get((a, load), [])) > 1 and len(cell.get((b, load), [])) > 1:
                    diff, half = diff_ci(cell[a, load], cell[b, load])
                    md.append(f"| {a} − {b} | {load:.0%} | {diff:+.1f} | {diff - half:+.1f} to {diff + half:+.1f} | "
                              f"{'**yes**' if abs(diff) > half else 'no'} |")
        fig, axes = plt.subplots(1, len(loads), figsize=(5 * len(loads), 3.2), squeeze=False)
        for ax, load in zip(axes[0], loads):
            for i, policy in enumerate(policies):
                p99 = cell.get((policy, load), [])
                if not p99:
                    continue
                if len(p99) > 1:
                    ax.plot([statistics.mean(p99)] * 2, [i - 0.28, i + 0.28], color=color_of(policy), linewidth=3, solid_capstyle="round")
                for v in p99:
                    ax.plot(v, i, **mark(policy))
            ax.axvline(SLO_MS, color=INK2, linewidth=1, linestyle=(0, (4, 3)))
            ax.set_xscale("log")
            ax.xaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%g"))
            ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
            ax.set_yticks(range(len(policies)), policies)
            ax.invert_yaxis()
            ax.grid(axis="y", visible=False)
            ax.set_xlabel("p99 (ms, log); dashed = SLO")
            ax.set_title(f"at {load:.0%} of the summed capacities", loc="left")
        fig.suptitle("S14: p99 at a fixed load; dots = runs, bar = mean", x=0.01, ha="left", fontsize=11)
        fig.tight_layout()
        fig.savefig(root / "s14-fixed.png", dpi=130)
        plt.close(fig)
        md.append("\n![fixed](s14-fixed.png)\n")

    caps = [r for r in runs if r["kind"] == "capacity" and r["capacity"]]
    gap = {p: [c["capacity"] / total for c in caps if c.get("policy") == p] for p in ("oracle", "direct")}
    if all(len(v) > 1 for v in gap.values()):
        old = [c for c in records(root / "s6" / "runs.jsonl") if c["kind"] == "capacity" and c["capacity"]]
        md.append("### The oracle through the LB vs the same shares without it, again\n")
        md.append("| | runs (S14) | S14: fractions of S1's sum | mean | with S6's 2 runs: mean ± 95% |")
        md.append("|---|---|---|---|---|")
        both = {}
        for p, label in (("oracle", "oracle through the LB"), ("direct", "same shares, no LB")):
            both[p] = gap[p] + [c["capacity"] / total for c in old if c.get("policy") == p]
            m, half = mean_ci(both[p])
            md.append(f"| {label} | {len(gap[p])} | {', '.join(f'{f:.1%}' for f in gap[p])} | {statistics.mean(gap[p]):.1%} | {m:.1%} ± {half:.1%} (n={len(both[p])}) |")
        for label, g in (("S14", gap), ("S6 + S14", both)):
            diff, half = diff_ci(g["direct"], g["oracle"])
            md.append(f"\n{label}: no LB − through the LB = {100 * diff:+.1f} points, 95% interval {100 * (diff - half):+.1f} to "
                      f"{100 * (diff + half):+.1f}: {'resolved' if abs(diff) > half else 'not resolved'}.")
        md.append("")


def s15_overhead(root, md):
    runs = [r for r in records(root / "s15" / "runs.jsonl") if r["kind"] == "overhead"]
    if not runs:
        return
    md.append("### Latency through the LB vs the same shares sent directly (S15)\n")
    md.append("Oracle shares, 30 s per run, pairs in alternating order. Through the LB a connection goes to one backend "
              "(bucket hashing, encapsulation, the redirector); directly, each request goes to a backend at random in "
              "proportion. So the difference is the whole LB path and the per-connection choice, not the director alone.\n")
    md.append("| load | percentile | through the LB (ms): mean (range) | direct (ms): mean (range) | LB − direct per pair: mean ± 95% |")
    md.append("|---|---|---|---|---|")
    for load in sorted({r["load"] for r in runs}):
        for q in ("p50", "p90", "p99"):
            by = {path: {r["rep"]: r["summary"]["latency_ms"][q] for r in runs if r["path"] == path and r["load"] == load}
                  for path in ("lb", "direct")}
            diffs = [by["lb"][k] - by["direct"][k] for k in sorted(by["lb"]) if k in by["direct"]]
            m, half = mean_ci(diffs)
            cell = lambda v: f"{statistics.mean(v):.2f} ({min(v):.2f}–{max(v):.2f})"
            md.append(f"| {load:.0%} | {q} | {cell(list(by['lb'].values()))} | {cell(list(by['direct'].values()))} | {m:+.2f} ± {half:.2f} |")
    md.append("")


def main():
    global SLO_MS, TOP_MS
    root = Path(sys.argv[1])
    calib = next((r for r in records(root / "s1" / "runs.jsonl") if r["kind"] == "calibration"), {})
    SLO_MS = calib.get("slo", {}).get("p99_ms", SLO_MS)
    TOP_MS = 100 * SLO_MS
    md = [f"# Campaign results: {root.name}\n"]
    invalid_runs(root, md, ["s2", "s2-extra", "s2-rif-total", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s12", "s13", "s14", "s15"])
    d, total, cv = s1(root, md)
    tuning(root, md)
    s2(root, md, d, total, cv)
    s6(root, md, d, total)
    s8(root, md, d, total)
    s14(root, md, d, total)
    scenarios(root, md, ["s3", "s5", "s6", "s7", "s9", "s10", "s12"], "S3 + S5 + S6 + S7 + S9 + S10 + S12: step and antagonist (RQ2, RQ3, RQ4)",
              only=lambda s: s in ("step", "antagonist"))
    scenarios(root, md, ["s9", "s12"], "S9 + S12 alone: step and antagonist, statics and controllers from the same sessions",
              only=lambda s: s in ("step", "antagonist"))
    scenarios(root, md, ["s13"], "S13: the same disturbances at 60% and 85% load", only=lambda s: "@" in s)
    scenarios(root, md, ["s15"], "S15: controller gain 0.1, 0.3 (ours), 1, and no protection (antagonist)",
              only=lambda s: s == "antagonist")
    signals_seen(root, md, "s3", "antagonist", "beE1", "Which signals see a neighbour in another cgroup (H5)")
    scenarios(root, md, ["s4", "s5", "s10"], "S4 + S5 + S10: flapping antagonist, sibling antagonist",
              only=lambda s: s.startswith("flapping") or s.startswith("sibling") or s.startswith("fastflap"))
    sweep(root, md)
    signals_seen(root, md, "s4", "sibling", "beP1", "Which signals see a neighbour on the sibling hyperthreads (H6)")
    sibling_stream(root, md)
    if (root / "s4").exists():
        md.append("## S4 + S10: PSI and the hook\n")
    kernel_psi(root, md)
    hook_cost(root, md)
    s15_overhead(root, md)
    (root / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))


if __name__ == "__main__":
    main()
