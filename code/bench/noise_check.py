"""Phase 0 noise check: how repeatable are measurements on this laptop?

Everything runs against the backends directly (no load balancer yet):
  1. calibrate  work units per ms on a P-core and an E-core; choose -work so
                one request costs COST_MS of CPU on a P-core
  2. rough      quick ramp to find roughly where each backend type misses the SLO
  3. repeat     p99 at 50% and 80% of that rate, `repeats` times each, alternating
  4. capacity   the full capacity-at-SLO search, `capacity_reps` times on beP1 and beE1
  5. coupling   beP1's capacity again while the other three backends' cores are
                70% busy (antagonists: CPU load without network traffic, so we
                see shared-cache/power effects, not the router's CPU being busy)

System tweaks (scripts/tune.sh) are applied at the start and always undone at
the end, also on errors or Ctrl-C.

Usage: sudo python3 bench/noise_check.py [--quick] [--no-tune]
Output: results/noise-<timestamp>/ with runs.jsonl, calibration.json, sysmon.csv, summary.md
"""

import argparse
import json
import os
import subprocess
import sys
import time

import analyze_noise
import testbed as tb

SLO_P99_MS = 10.0       # a rate meets the SLO if p99 latency <= 10 ms,
SLO_ERROR_RATE = 0.001  # at most 0.1% of requests fail,
MIN_ACHIEVED = 0.98     # and at least 98% of the offered rate is actually served
COST_MS = 1.0           # CPU time per request on a P-core

FULL = dict(rough_measure=15, repeats=10, repeat_warmup=20, repeat_measure=60, repeat_rest=10,
            probes=6, probe_warmup=10, probe_measure=40, probe_rest=5, capacity_reps=5, coupling_reps=3)
QUICK = dict(rough_measure=8, repeats=2, repeat_warmup=5, repeat_measure=15, repeat_rest=2,
             probes=4, probe_warmup=5, probe_measure=10, probe_rest=2, capacity_reps=1, coupling_reps=1)


def meets_slo(s):
    return (s["ok"] > 0
            and s["latency_ms"]["p99"] <= SLO_P99_MS
            and s["error_rate"] <= SLO_ERROR_RATE
            and s["achieved_rate"] >= MIN_ACHIEVED * s["target_rate"])


class Recorder:
    """Appends one JSON object per line to runs.jsonl."""

    def __init__(self, path):
        self.path = path

    def write(self, obj):
        with open(self.path, "a") as f:
            f.write(json.dumps(obj) + "\n")


def step(rec, phase, be, rate, warmup, measure, rest, **tags):
    """One load-generator run at a fixed rate; records it and returns whether it met the SLO."""
    t_start = time.time()
    s = tb.loadgen(be.ip, rate, warmup, measure)
    passed = meets_slo(s)
    rec.write(dict(phase=phase, backend=be.name, rate=round(rate, 1), passed=passed,
                   t_start=t_start, t_end=time.time(), **tags, summary=s))
    print(f"  {phase:15s} {be.name} rate {rate:7.1f}  p99 {s['latency_ms']['p99']:7.2f} ms  "
          f"errors {s['error_rate']:.4f}  served {s['achieved_rate']:7.1f}/s  -> {'PASS' if passed else 'fail'}",
          flush=True)
    time.sleep(rest)
    return passed


def rough_capacity(rec, be, theoretical, cfg):
    """Ramp in steps of 10% of the theoretical capacity; return the last rate that met the SLO."""
    last_pass = None
    for tenth in range(5, 13):
        rate = theoretical * tenth / 10
        if not step(rec, "rough", be, rate, 5, cfg["rough_measure"], 2):
            break
        last_pass = rate
    if last_pass is None:
        raise RuntimeError(f"{be.name} misses the SLO even at 50% of its theoretical capacity")
    return last_pass


def capacity_search(rec, be, rough, cfg, phase, rep):
    """Binary search for the highest rate that meets the SLO, between 0.85x and 1.25x the rough estimate."""
    lo, hi = 0.85 * rough, 1.25 * rough
    verified = False
    for _ in range(cfg["probes"]):
        mid = (lo + hi) / 2
        if step(rec, phase + "-probe", be, mid, cfg["probe_warmup"], cfg["probe_measure"], cfg["probe_rest"], rep=rep):
            lo, verified = mid, True
        else:
            hi = mid
    at_edge = lo > 0.97 * 1.25 * rough  # true capacity may be above our search range
    rec.write(dict(phase=phase, backend=be.name, rep=rep, capacity=round(lo, 1), verified=verified, at_edge=at_edge))
    print(f"  => {phase} {be.name} rep {rep}: {lo:.1f} req/s{'' if verified else ' (no probe passed!)'}"
          f"{' (at edge of search range)' if at_edge else ''}", flush=True)
    return lo


def experiment(out, rec, cfg):
    beP, beE = tb.BACKENDS["beP1"], tb.BACKENDS["beE1"]

    print("== 1. calibrate", flush=True)
    p_units = tb.calibrate(tb.expand_cpus(beP.cpus)[0])
    e_units = tb.calibrate(tb.expand_cpus(beE.cpus)[0])
    work = round(p_units * COST_MS)
    e_slowdown = p_units / e_units  # how many times longer a request takes on an E-core
    for be in tb.BACKENDS.values():
        tb.start_backend(be, work)

    print("== 2. rough capacity", flush=True)
    theoretical = {"P": beP.ncpus * 1000 / COST_MS, "E": beE.ncpus * 1000 / (COST_MS * e_slowdown)}
    rough = {"P": rough_capacity(rec, beP, theoretical["P"], cfg),
             "E": rough_capacity(rec, beE, theoretical["E"], cfg)}
    calibration = dict(cost_ms=COST_MS, work=work, p_units_per_ms=p_units, e_units_per_ms=e_units,
                       e_slowdown=e_slowdown, theoretical=theoretical, rough=rough,
                       slo=dict(p99_ms=SLO_P99_MS, error_rate=SLO_ERROR_RATE, min_achieved=MIN_ACHIEVED),
                       settings=cfg)
    (out / "calibration.json").write_text(json.dumps(calibration, indent=2) + "\n")

    print("== 3. repeatability at 50% and 80% load", flush=True)
    for rep in range(cfg["repeats"]):
        for level in (0.5, 0.8):
            step(rec, "repeat", beP, level * rough["P"], cfg["repeat_warmup"], cfg["repeat_measure"],
                 cfg["repeat_rest"], level=f"{level:.0%}", rep=rep)

    print("== 4. capacity search, repeated", flush=True)
    for be in (beP, beE):
        for rep in range(cfg["capacity_reps"]):
            capacity_search(rec, be, rough[be.kind], cfg, "capacity", rep)

    print("== 5. coupling: beP1 capacity while the other backends' cores are 70% busy", flush=True)
    others = [be for be in tb.BACKENDS.values() if be is not beP]
    for be in others:
        tb.start_antagonist(be.name, be.cpus, 0.7)
    try:
        time.sleep(10)  # let temperatures settle
        for rep in range(cfg["coupling_reps"]):
            capacity_search(rec, beP, rough["P"], cfg, "coupled", rep)
    finally:
        for be in others:
            tb.stop_antagonist(be.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true", help="short smoke test (~15 min) instead of the full check")
    ap.add_argument("--no-tune", action="store_true", help="skip scripts/tune.sh (debugging only)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("run with sudo")

    cfg = QUICK if args.quick else FULL
    out = tb.REPO / "results" / f"noise-{time.strftime('%Y%m%d-%H%M%S')}{'-quick' if args.quick else ''}"
    out.mkdir(parents=True)
    print(f"results: {out}", flush=True)
    sysmon = None
    try:
        if not args.no_tune:
            tb.run([str(tb.REPO / "scripts/tune.sh"), "apply"])
        tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
        sysmon = subprocess.Popen([sys.executable, str(tb.REPO / "bench/sysmon.py"), str(out / "sysmon.csv")])
        experiment(out, Recorder(out / "runs.jsonl"), cfg)
    finally:
        print("== cleanup", flush=True)
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
            tb.stop_antagonist(be.name)
        if sysmon:
            sysmon.terminate()
            sysmon.wait()
        subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"])
        if not args.no_tune:
            subprocess.run([str(tb.REPO / "scripts/tune.sh"), "undo"])
        tb.give_back(out)

    analyze_noise.main(out)
    tb.give_back(out)  # again, for summary.md


if __name__ == "__main__":
    main()
