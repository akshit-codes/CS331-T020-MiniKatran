#!/usr/bin/env python3
"""End-to-end check of the load balancer with static tables (phase 1, part one).

Brings up the testbed, starts all backends, loads the LB with bin/lbctl and
sends real HTTP traffic to the VIP. Every request uses a new connection, so
each one is a new flow that the director hashes on its own. Checks:
  - the director is attached at TC ingress (D35),
  - without a table, VIP traffic is dropped (and counted),
  - with each static table, requests are split between backends as the table
    says, with no errors,
  - large requests (sent as GSO packets, with checksums left to "offload")
    arrive complete through the encapsulation and back out of it,
  - replies bypass the LB (direct server return): the LB receives only VIP packets.

This is a functional test, not a measurement: no tuning, short runs.
Usage: sudo python3 bench/e2e_lb.py
"""
import subprocess
import sys

import testbed as tb

WORK = 20000          # work units per request: ~0.2 ms on a P-core, keeps the backends far from busy
RATE = 400            # requests per second, each on a new connection
SECONDS = 10
TOLERANCE = 0.03      # allowed difference between measured and table share (4000 flows: sd ~0.7%)

failures = []


def lbctl(*args):
    return tb.run([str(tb.BIN / "lbctl"), *args], cwd=tb.REPO, capture_output=True, text=True).stdout


def lb_stats():
    """Director counters from 'lbctl stats' as a dict (backend names map to packets sent)."""
    stats = {}
    for line in lbctl("stats").splitlines():
        parts = line.split()
        if len(parts) == 2:
            stats[parts[0]] = int(parts[1])
    return stats


def check(ok, what):
    print(f"  {'PASS' if ok else 'FAIL'}: {what}")
    if not ok:
        failures.append(what)


def send_traffic():
    """Run the load generator against the VIP; return its summary and the share of
    requests each backend served (from the backends' own request counters)."""
    before = {name: tb.backend_metrics(be)["requests_total"] for name, be in tb.BACKENDS.items()}
    summary = tb.loadgen(tb.CFG["VIP"], RATE, warmup=1, duration=SECONDS,
                         extra=["-reqs-per-conn", "1", "-conns", "16"])
    served = {name: tb.backend_metrics(be)["requests_total"] - before[name] for name, be in tb.BACKENDS.items()}
    total = sum(served.values())
    return summary, {name: n / total if total else 0 for name, n in served.items()}


def table_case(spec, expected):
    print(f"table {spec}")
    lbctl("table", spec)
    summary, shares = send_traffic()
    check(summary["errors"] == 0 and summary["requests"] > 0.9 * RATE * SECONDS,
          f"{summary['requests']} requests, {summary['errors']} errors")
    for name, want in expected.items():
        got = shares[name]
        check(abs(got - want) <= TOLERANCE, f"{name} served {got:.1%} of requests (table: {want:.1%})")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with the tools' output
    try:
        tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
        for be in tb.BACKENDS.values():
            tb.start_backend(be, WORK)
        print(lbctl("load"), end="")

        print("attach point")
        hooks = subprocess.run(tb.in_ns("lb", ["bpftool", "net", "show", "dev", "eth0"]),
                               capture_output=True, text=True).stdout
        link = subprocess.run(tb.in_ns("lb", ["ip", "link", "show", "dev", "eth0"]),
                              capture_output=True, text=True).stdout
        check("tcx/ingress director_tc" in hooks and "xdp" not in link,
              "director attached at TC ingress, no XDP program on the LB")

        print("no table")
        curl = subprocess.run(tb.in_ns("cli", ["curl", "-s", "-m", "1", f"http://{tb.CFG['VIP']}:{tb.CFG['BACKEND_PORT']}/work"]))
        check(curl.returncode != 0 and lb_stats()["drop_no_hop"] > 0, "VIP traffic dropped and counted as drop_no_hop")

        before = lb_stats()
        table_case("equal", {name: 0.25 for name in tb.BACKENDS})
        table_case("beP1=1", {"beP1": 1.0, "beP2": 0, "beE1": 0, "beE2": 0})
        table_case("beP1=1,beE1=3", {"beP1": 0.25, "beP2": 0, "beE1": 0.75, "beE2": 0})

        print("large requests (GSO packets through the LB)")
        body = bytes(range(256)) * 4096  # 1 MiB
        url = f"http://{tb.CFG['VIP']}:{tb.CFG['BACKEND_PORT']}/upload"
        answers = [subprocess.run(tb.in_ns("cli", ["curl", "-s", "-m", "5", "-H", "Expect:", "--data-binary", "@-", url]),
                                  input=body, capture_output=True).stdout.strip() for _ in range(20)]
        check(all(a == str(len(body)).encode() for a in answers),
              f"20 uploads of {len(body)} bytes arrive complete (answers: {sorted(set(answers))})")

        print("direct server return")
        after = lb_stats()
        tx, other = after["tx"] - before["tx"], after["pass"] - before["pass"]
        check(tx > 0 and other <= 0.01 * tx,
              f"LB encapsulated {tx} packets and saw only {other} others (replies don't come through the LB)")
        drops = sum(after[k] - before[k] for k in ("drop_proto", "drop_no_hop", "drop_error"))
        check(drops == 0, f"no director drops with a table ({drops})")
    finally:
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
        subprocess.run([str(tb.BIN / "lbctl"), "unload"], cwd=tb.REPO)
        subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"])

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
