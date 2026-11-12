"""Summarize a noise-check run: prints a report and writes summary.md next to the data.

Usage: python3 bench/analyze_noise.py results/noise-<timestamp>
"""

import csv
import json
import statistics
import sys
from pathlib import Path

import testbed as tb

# Pass criteria from docs/measurement-protocol.md
MAX_P99_CV_AT_80 = 0.10
MAX_CAPACITY_CV = 0.03


def stats(xs):
    xs = list(xs)
    mean = statistics.fmean(xs) if xs else float("nan")
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return dict(n=len(xs), mean=mean, sd=sd, cv=sd / mean if mean else float("nan"),
                min=min(xs, default=float("nan")), max=max(xs, default=float("nan")))


def fmt(st, unit="", digits=2):
    return (f"{st['mean']:.{digits}f}{unit} ± {st['sd']:.{digits}f} (CV {st['cv']:.1%}, "
            f"range {st['min']:.{digits}f}–{st['max']:.{digits}f}, n={st['n']})")


def load_sysmon(path):
    if not path.exists():
        return []
    with open(path) as f:
        return [{k: float(v) if v else float("nan") for k, v in row.items()} for row in csv.DictReader(f)]


def during(samples, runs):
    """Sysmon samples that fall inside any of the given runs."""
    spans = [(r["t_start"], r["t_end"]) for r in runs]
    return [s for s in samples if any(a <= s["time"] <= b for a, b in spans)]


def mean_of(samples, key):
    vals = [s[key] for s in samples]
    return statistics.fmean(vals) if vals else float("nan")


def main(out_dir):
    out = Path(out_dir)
    runs = [json.loads(line) for line in open(out / "runs.jsonl")]
    calib = json.loads((out / "calibration.json").read_text())
    samples = load_sysmon(out / "sysmon.csv")
    lines = []

    def say(s=""):
        lines.append(s)
        print(s)

    say(f"# Noise check {out.name}\n")
    say("## Calibration")
    say(f"- work per request: {calib['work']} units (= {calib['cost_ms']} ms on a P-core)")
    say(f"- P-core {calib['p_units_per_ms']:.0f} units/ms, E-core {calib['e_units_per_ms']:.0f} units/ms "
        f"-> a request takes **{calib['e_slowdown']:.2f}x** as long on an E-core")
    for kind in ("P", "E"):
        say(f"- {kind} backend: theoretical {calib['theoretical'][kind]:.0f} req/s, "
            f"rough SLO capacity {calib['rough'][kind]:.0f} req/s")
    slo = calib["slo"]
    say(f"- SLO: p99 <= {slo['p99_ms']} ms, errors <= {slo['error_rate']:.1%}, "
        f"served >= {slo['min_achieved']:.0%} of offered\n")

    say("## Repeatability of p99 (beP1, fixed rates, alternating)")
    verdict = {}
    for level in ("50%", "80%"):
        rs = [r for r in runs if r["phase"] == "repeat" and r.get("level") == level]
        if not rs:
            continue
        p99 = stats(r["summary"]["latency_ms"]["p99"] for r in rs)
        p50 = stats(r["summary"]["latency_ms"]["p50"] for r in rs)
        window_cv = statistics.median(stats(r["summary"]["window_p99_ms"])["cv"] for r in rs)
        say(f"- {level} load ({rs[0]['rate']:.0f} req/s): p50 {fmt(p50, ' ms')}")
        say(f"  - p99 {fmt(p99, ' ms')}")
        say(f"  - within one run, p99 per 10 s window varies by CV {window_cv:.1%} (median over runs)")
        if level == "80%":
            verdict["p99 CV at 80% load"] = (p99["cv"], MAX_P99_CV_AT_80)

    say("\n## Repeatability of capacity at SLO")
    series = {}
    for phase, name, label in (("capacity", "beP1", "beP1 alone"), ("capacity", "beE1", "beE1 alone"),
                               ("coupled", "beP1", "beP1, others at 70%")):
        rs = [r for r in runs if r["phase"] == phase and r["backend"] == name and "capacity" in r]
        if rs:
            series[label] = st = stats(r["capacity"] for r in rs)
            flags = [f"rep {r['rep']}: " + ("no probe passed" if not r["verified"] else "at search edge")
                     for r in rs if not r["verified"] or r["at_edge"]]
            say(f"- {label}: {fmt(st, ' req/s', 1)}" + (f"  ⚠ {', '.join(flags)}" if flags else ""))
    for label in ("beP1 alone", "beE1 alone"):
        if label in series and series[label]["n"] > 1:
            verdict[f"capacity CV, {label}"] = (series[label]["cv"], MAX_CAPACITY_CV)
    if "beP1 alone" in series and "beP1, others at 70%" in series:
        alone, coupled = series["beP1 alone"], series["beP1, others at 70%"]
        diff = coupled["mean"] / alone["mean"] - 1
        say(f"- coupling: beP1 capacity changes by {diff:+.1%} when the other backends are busy "
            f"({'within' if abs(diff) <= 2 * alone['cv'] else 'OUTSIDE'} 2x the run-to-run CV)")
    if "beP1 alone" in series:
        say(f"- smallest difference between policies we could claim (~2x capacity CV): "
            f"{2 * series['beP1 alone']['cv']:.1%}")

    say("\n## Generator health (all runs)")
    measured = [r for r in runs if "summary" in r]
    say(f"- dispatch lag p99, worst run: {max(r['summary']['dispatch_lag_ms']['p99'] for r in measured):.3f} ms")
    say(f"- requests dropped by the generator: {sum(r['summary']['gen_dropped'] for r in measured)}")

    if samples:
        say("\n## Machine state (sysmon)")
        p_cpus = tb.expand_cpus(tb.BACKENDS["beP1"].cpus)
        e_cpus = tb.expand_cpus(tb.BACKENDS["beE1"].cpus)
        for phase in ("repeat", "capacity-probe", "coupled-probe"):
            s = during(samples, [r for r in runs if r["phase"] == phase])
            if s:
                # Median, not mean: scaling_cur_freq is an estimate from the last
                # timer tick and sometimes reads ~800 MHz on a core whose clock is
                # actually steady (checked against APERF/MPERF; journal 2026-11-02).
                p_mhz = statistics.median(x[f"mhz{c}"] for x in s for c in p_cpus)
                e_mhz = statistics.median(x[f"mhz{c}"] for x in s for c in e_cpus)
                say(f"- {phase}: beP1 cores {p_mhz:.0f} MHz, beE1 cores {e_mhz:.0f} MHz (median), "
                    f"package {mean_of(s, 'temp_c'):.0f} °C (max {max(x['temp_c'] for x in s):.0f}), "
                    f"{mean_of(s, 'watts'):.1f} W")
        throttles = {k: samples[-1][k] - samples[0][k] for k in ("pkg_throttle", "core_throttle")}
        say(f"- thermal throttle events during the run: package {throttles['pkg_throttle']:.0f}, "
            f"cores {throttles['core_throttle']:.0f}")
        verdict["thermal throttle events"] = (throttles["pkg_throttle"] + throttles["core_throttle"], 0)
        rs = during(samples, [r for r in runs if r["phase"] == "repeat" and r.get("level") == "80%"])
        if rs:
            ncpu = sum(1 for k in rs[0] if k.startswith("soft"))
            soft = sorted(((mean_of(rs, f"soft{c}"), c) for c in range(ncpu)), reverse=True)[:6]
            say("- where packet processing ran (softirq %, 80% runs): "
                + ", ".join(f"cpu{c} {v:.1f}%" for v, c in soft))
            say(f"  expected: loadgen {tb.CFG['LOADGEN_CPUS']}, router {tb.CFG['LB_CPUS']}, "
                f"beP1 {tb.BACKENDS['beP1'].cpus}")

    say("\n## Verdict")
    for name, (value, limit) in verdict.items():
        ok = value <= limit
        shown = f"{value:.1%} (limit {limit:.0%})" if limit else f"{value:.0f}"
        say(f"- {'PASS' if ok else 'FAIL'}: {name} = {shown}")

    (out / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
