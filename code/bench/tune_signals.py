#!/usr/bin/env python3
"""Tuning step of the campaign (D36): signal noise from S1's sigmon logs, then
the period and dead band per signal from cmd/ctltune.

For each signal and window P ∈ {1, 2, 4, 8} s: average the 1-s readings over
consecutive P-second windows, take each backend's coefficient of variation
(std / mean) over the measured phase, and the median over backends. The 80%
run feeds the tuning (that is where control matters); the 50% run is reported.
PSI is reported but not tuned (D36).

Usage: python3 bench/tune_signals.py results/campaign-<date>
Writes <root>/tuning/noise.json, tuning.json and tuning.txt.
"""
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SIGNALS = ["cpu", "rif", "cgroup", "psi"]
TUNED = ["cpu", "rif", "cgroup"]
PERIODS = [1, 2, 4, 8]


def noise(csv_path, warmup, measure, backends):
    """{signal: {P: σ}} from one sigmon log."""
    rows = [r for r in csv.DictReader(open(csv_path)) if warmup + 2 <= float(r["t"]) <= warmup + measure]
    result = {}
    for sig in SIGNALS:
        result[sig] = {}
        for p in PERIODS:
            cvs = []
            for be in backends:
                values = [float(r[f"{sig}_{be}"]) for r in rows]
                windows = [statistics.mean(values[i:i + p]) for i in range(0, len(values) - p + 1, p)]
                if len(windows) < 5:  # too short a run for this window (dry runs)
                    break
                mean = statistics.mean(windows)
                cvs.append(statistics.stdev(windows) / mean if mean > 0 else float("nan"))
            if len(cvs) == len(backends):
                result[sig][str(p)] = statistics.median(cvs)
    return result


def main():
    root = Path(sys.argv[1])
    d = json.loads((root / "s1" / "denominators.json").read_text())
    noise_runs = {r["load"]: r for r in map(json.loads, open(root / "s1" / "runs.jsonl")) if r.get("kind") == "noise"}
    backends = list(d["capacity"])
    sigma = {load: noise(root / "s1" / f"noise-{load}.csv", run["warmup"], run["summary"]["duration_s"], backends)
             for load, run in noise_runs.items()}

    out = root / "tuning"
    out.mkdir(exist_ok=True)
    # The simulator's capacity is where utilisation reaches 1 (maximum
    # throughput), so give it the theoretical capacities, and express the
    # campaign's load (75% of the summed capacities at SLO) relative to those.
    # (First try used the SLO capacities: the model then ran ~20% hotter than
    # the testbed and its RIF was ~35% noisier than measured.)
    load = 0.75 * sum(d["capacity"].values()) / sum(d["theoretical"].values())
    spec = dict(
        backends=[dict(name=b, cores=d["cores"][b], capacity=d["theoretical"][b]) for b in backends],
        load=load, connections=512, requests_per_connection=20, step_backend="beE1",
        # rif-total is RIF times a constant (1/cores), so its coefficient of
        # variation is RIF's: same measured noise, its own dynamics in the model.
        noise={**{sig: sigma[0.8][sig] for sig in TUNED}, "rif-total": sigma[0.8]["rif"]},
        measured_noise=sigma,  # all signals, both loads (ctltune ignores this)
    )
    (out / "noise.json").write_text(json.dumps(spec, indent=2))

    lines = ["Signal noise σ(P): median over backends of the CV of P-second averages", ""]
    for load in sorted(sigma):
        lines.append(f"at {load:.0%} load:")
        lines.append("  signal " + "".join(f"{f'P={p}s':>9}" for p in PERIODS))
        for sig in SIGNALS:
            lines.append(f"  {sig:6} " + "".join(f"{sigma[load][sig].get(str(p), float('nan')):9.3f}" for p in PERIODS))
        lines.append("")
    tuned = subprocess.run([str(REPO / "bin/ctltune"), "-in", str(out / "noise.json"), "-out", str(out / "tuning.json")],
                           capture_output=True, text=True, check=True).stdout
    text = "\n".join(lines) + "\n" + tuned
    (out / "tuning.txt").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
