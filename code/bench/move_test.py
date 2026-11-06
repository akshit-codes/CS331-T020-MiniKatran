#!/usr/bin/env python3
"""Do connections survive when the table moves buckets? (RQ4, first part)

Traffic, all through the LB:
  - long-lived: 300 persistent connections, each sending a request about every
    100 ms (3000 req/s in total),
  - churn: 200 new connections per second, one request each.
beE1 runs with tcp_syncookies=2, so it answers every SYN with a SYN cookie and
keeps no half-open connections.

After SWITCH_AT seconds the table changes from "equal" to one without beP1
(beP1=0), which gives about half of all buckets a new first hop.

Three runs, each on a fresh testbed:
  A. second hops = old first hops (lbctl table -second previous)
  B. no second hops                (lbctl table -second none)
  C. like A, but redirectors without the SYN-cookie check

Expected: A breaks (almost) nothing. B breaks the long-lived connections in
moved buckets. C keeps the long-lived ones, but new connections to beE1 in
buckets that moved to it break: their last handshake ACK finds only beE1's
listener, and without the cookie check it is forwarded to the old first hop,
which answers with a reset.

This is a functional test, not a measurement: no tuning.
Usage: sudo python3 bench/move_test.py     Results: results/move-<time>/
"""
import csv
import json
import signal
import subprocess
import sys
import time
from datetime import datetime

import testbed as tb

WORK = 20000            # ~0.2 ms per request on a P-core: backends stay far from busy
WARMUP, DURATION = 2, 28
SWITCH_AT = 12          # seconds after the load generators start
COOKIE_BACKEND = "beE1"
NEW_TABLE = "beP1=0,beP2=1,beE1=1,beE2=1"

RUNS = [
    ("A: second hops", [], ["-second", "previous"]),
    ("B: no second hops", [], ["-second", "none"]),
    ("C: second hops, no syncookie check", ["-no-syncookie-check"], ["-second", "previous"]),
]


def lbctl(*args):
    return tb.run([str(tb.BIN / "lbctl"), *args], cwd=tb.REPO, capture_output=True, text=True).stdout


def read_table():
    """bucket -> (first, second) from 'lbctl dump'."""
    table = {}
    for line in lbctl("dump").splitlines():
        if not line.startswith("#"):
            bucket, first, second = line.split()
            table[int(bucket)] = (first, second)
    return table


def redirector_stats():
    """backend -> {stat: count} from 'lbctl stats'."""
    lines = lbctl("stats").splitlines()
    header = lines.index("redirectors:") + 1
    names = lines[header].split()
    return {parts[0]: dict(zip(names, map(int, parts[1:])))
            for parts in (line.split() for line in lines[header + 1:header + 1 + len(tb.BACKENDS)])}


def start_loadgen(cpus, rate, extra):
    cmd = tb.on_cpus(cpus, tb.loadgen_cmd(tb.CFG["VIP"], rate, WARMUP, DURATION, extra))
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)


def one_run(name, load_flags, table_flags, out_dir):
    print(f"\n=== {name}", flush=True)
    tag = name[0]
    try:
        tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
        tb.run(tb.in_ns(COOKIE_BACKEND, ["sysctl", "-qw", "net.ipv4.tcp_syncookies=2"]))
        for be in tb.BACKENDS.values():
            tb.start_backend(be, WORK)
        lbctl("load", *load_flags)
        lbctl("table", "equal")
        before = read_table()

        resets_csv = out_dir / f"resets-{tag}.csv"
        resetmon = subprocess.Popen([str(tb.BIN / "resetmon"), "-out", str(resets_csv)],
                                    cwd=tb.REPO, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)  # let it attach
        if resetmon.poll() is not None:
            raise RuntimeError(f"resetmon failed: {resetmon.communicate()[1]}")

        # Long-lived connections on the load generator's CPUs; the churn on the
        # housekeeping CPUs, so the two don't compete (functional test only).
        long_lived = start_loadgen(tb.CFG["LOADGEN_CPUS"], 3000, ["-conns", "300"])
        churn = start_loadgen(tb.CFG["HOUSEKEEPING_CPUS"], 200, ["-conns", "32", "-reqs-per-conn", "1"])
        time.sleep(SWITCH_AT)
        switch_time = time.monotonic()
        lbctl("table", *table_flags, NEW_TABLE)
        after = read_table()

        long_lived_summary = json.loads(long_lived.communicate()[0])
        churn_summary = json.loads(churn.communicate()[0])
        resetmon.send_signal(signal.SIGINT)
        resetmon_summary = resetmon.communicate()[1].strip()
        counters = {ns: tb.tcp_counters(ns) for ns in [*tb.BACKENDS, "cli"]}
        redirectors = redirector_stats()
    finally:
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
        subprocess.run([str(tb.BIN / "lbctl"), "unload"], cwd=tb.REPO, capture_output=True)
        subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"], capture_output=True)

    moved = {b for b in before if before[b][0] != after[b][0]}
    with open(resets_csv) as f:
        resets = list(csv.DictReader(f))
    server_sent = [r for r in resets if r["kind"] == "sent" and r["side"] == "server"]
    after_switch = [r for r in server_sent if float(r["time_s"]) >= switch_time]
    in_moved = [r for r in after_switch if int(r["bucket"]) in moved]

    result = {
        "run": name,
        "moved_buckets": len(moved),
        "long_lived": {k: long_lived_summary[k] for k in ("requests", "errors", "connections")},
        "long_lived_reconnects": long_lived_summary["connections"] - 300,
        "churn": {k: churn_summary[k] for k in ("requests", "errors", "connections")},
        "server_resets": len(server_sent),
        "server_resets_after_switch": len(after_switch),
        "server_resets_in_moved_buckets": len(in_moved),
        "client_resets_received": int(counters["cli"]["TcpEstabResets"]),
        "resetmon": resetmon_summary,
        "rsts_sent_per_backend": {be: counters[be]["TcpOutRsts"] for be in tb.BACKENDS},
        "rsts_sent_by_client": counters["cli"]["TcpOutRsts"],
        "retransmits_per_backend": {be: counters[be]["TcpRetransSegs"] for be in tb.BACKENDS},
        # Orphaned sockets (old backend, client gone) show up as keepalive
        # probes 15 s after their last activity (Go's default), which the
        # client answers with resets that go to the new first hop.
        "keepalive_probes_per_backend": {be: counters[be]["TcpExtTCPKeepAlive"] for be in tb.BACKENDS},
        "switch_time_s": switch_time,
        "cookies": {k: counters[COOKIE_BACKEND][f"TcpExt{k}"] for k in ("SyncookiesSent", "SyncookiesRecv", "SyncookiesFailed")},
        "redirectors": redirectors,
    }
    print(json.dumps(result, indent=2), flush=True)
    return result


def main():
    sys.stdout.reconfigure(line_buffering=True)
    out_dir = tb.REPO / "results" / f"move-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True)
    results = [one_run(name, load_flags, table_flags, out_dir) for name, load_flags, table_flags in RUNS]
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    print("\n=== summary")
    print(f"{'run':38} {'long-lived errors':>18} {'churn errors':>13} {'clients reset':>14} "
          f"{'server RSTs':>12} {'in moved buckets':>17}")
    for r in results:
        print(f"{r['run']:38} {r['long_lived']['errors']:>18} {r['churn']['errors']:>13} "
              f"{r['client_resets_received']:>14} {r['server_resets_after_switch']:>12} {r['server_resets_in_moved_buckets']:>17}")
    tb.give_back(out_dir)


if __name__ == "__main__":
    main()
