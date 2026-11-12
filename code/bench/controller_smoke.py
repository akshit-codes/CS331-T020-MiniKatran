#!/usr/bin/env python3
"""Smoke test of the live controller (not a measurement: no tuning, short).

beP1 runs with -slowness 2 (half capacity). Traffic through the LB with
connection churn (20 requests per connection). The controller starts from
shares by cores and should take buckets away from beP1. Checks, per signal:
the controller publishes tables, beP1 ends with fewer buckets than it started
with, and no request fails.

Usage: sudo python3 bench/controller_smoke.py    Results: results/controller-smoke-<time>/
"""
import csv
import json
import subprocess
import sys
import time
from datetime import datetime

import testbed as tb

WORK = 100000        # ~1 ms on a P-core
RATE = 3000          # req/s: about half the cluster's capacity
SECONDS = 90
failures = []


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}: {what}")
    if not ok:
        failures.append(what)


def one_signal(signal, out_dir):
    print(f"\n=== signal {signal}")
    log = out_dir / f"{signal}.csv"
    try:
        tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
        for be in tb.BACKENDS.values():
            tb.start_backend(be, WORK, slowness=2.0 if be.name == "beP1" else 1.0)
        tb.run([str(tb.BIN / "lbctl"), "load"], cwd=tb.REPO, capture_output=True)
        controller = subprocess.Popen([str(tb.BIN / "controller"), "-signal", signal, "-out", str(log),
                                       "-duration", f"{SECONDS + 5}s"], cwd=tb.REPO)
        time.sleep(1)
        summary = tb.loadgen(tb.CFG["VIP"], RATE, warmup=2, duration=SECONDS,
                             extra=["-conns", "64", "-reqs-per-conn", "20"])
        controller.wait(timeout=30)
    finally:
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
        subprocess.run([str(tb.BIN / "lbctl"), "unload"], cwd=tb.REPO, capture_output=True)
        subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"], capture_output=True)

    with open(log) as f:
        rows = list(csv.DictReader(f))
    first, last = rows[0], rows[-1]
    moves = sum(int(r["moves"]) for r in rows)
    print(f"  buckets beP1 {first['buckets_beP1']} -> {last['buckets_beP1']}, beP2 {first['buckets_beP2']} -> {last['buckets_beP2']}, "
          f"{moves} moves, generation {last['generation']}")
    print(f"  last u: " + " ".join(f"{be}={float(last['u_' + be]):.2f}" for be in tb.BACKENDS))
    check(int(last["generation"]) > 1, "controller published new tables")
    check(int(last["buckets_beP1"]) < int(first["buckets_beP1"]), "beP1 (half capacity) lost buckets")
    check(summary["errors"] == 0, f"{summary['requests']} requests, {summary['errors']} errors, p99 {summary['latency_ms']['p99']:.1f} ms")
    return {"signal": signal, "loadgen": summary, "first": first, "last": last, "moves": moves}


def main():
    sys.stdout.reconfigure(line_buffering=True)
    out_dir = tb.REPO / "results" / f"controller-smoke-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True)
    results = [one_signal(s, out_dir) for s in ("rif", "cpu")]
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    tb.give_back(out_dir)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
