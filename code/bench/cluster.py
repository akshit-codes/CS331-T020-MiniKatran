#!/usr/bin/env python3
"""The short campaign on real machines, driven from one laptop over ssh
(docs/cluster-runbook.md). Reuses bench/campaign.py; testbed.use_cluster()
makes its helpers run each command on the right machine.

    python3 bench/cluster.py cluster/cluster.conf [--quick] [--dry] [--no-tune]
    python3 bench/cluster.py cluster/emulated.conf --no-tune [--quick] [--dry]   (emulation, emulate.sh up first)

Run it from the client machine, as the user that can ssh to every machine
(not with sudo: each command uses sudo where it runs, also in emulation, so the
emulated run finds what a root driver would hide). Steps, ~50 min:
  1. preflight: every machine reachable, same commit and cluster.conf, built, kernel new enough
  2. setup.sh up on every machine (undone at the end, whatever happens)
  3. S1: smoke test through the VIP, each backend's capacity (1 search each), signal noise at 80%
  4. tuning: bench/tune_signals.py on S1's noise (here)
  5. S2: static policies (oracle ×2, equal, by cores) and controller tables (CPU, total RIF)
  6. S3: antagonist on beE1 with the static oracle, CPU, total RIF, and the redirector contrast
  7. collect the files the machines wrote, setup.sh down everywhere, analysis
--quick, ~33 min: 4 probes per search, no by-cores table and one oracle run in S2, and
a shorter antagonist in S3 (on at 30 s, off at 120 s, end at 150 s).
Output: results/cluster-<time>/, in the campaign's format (bench/analyze_campaign.py).
"""
import argparse
import hashlib
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import campaign as c
import testbed as tb


def on_all(cmd_for_host, check=True):
    """Run a command on every machine; returns {machine: stdout}."""
    out = {}
    for host in tb.CLUSTER["hosts"]:
        r = subprocess.run(tb.command(f"host:{host}", cmd_for_host(host)), capture_output=True, text=True, cwd=tb.REPO)
        if check and r.returncode != 0:
            sys.exit(f"{host}: {' '.join(cmd_for_host(host))} failed: {r.stderr.strip()}")
        out[host] = r.stdout.strip()
    return out


def preflight(conf):
    """Same code and config everywhere, built, and a kernel with TCX (6.6+)."""
    print("=== preflight", flush=True)
    on_all(lambda h: ["true"])  # reachable, with passwordless sudo
    for host in tb.CLUSTER["hosts"]:
        r = subprocess.run(tb.command(f"host:{host}", ["test", "-x", str(tb.BIN / "lbctl")]), cwd=tb.REPO)
        if r.returncode != 0:
            sys.exit(f"{host}: bin/lbctl missing: run make in {tb.CLUSTER['repo']}")
    kernels = on_all(lambda h: ["uname", "-r"])
    for host, k in kernels.items():
        if tuple(int(x) for x in k.split(".")[:2]) < (6, 6):
            sys.exit(f"{host}: kernel {k} is too old (TCX needs 6.6+)")
    if not tb.CLUSTER["emulated"]:
        here = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=tb.REPO).stdout.strip()
        commits = on_all(lambda h: ["git", "-C", tb.CLUSTER["repo"], "rev-parse", "HEAD"])
        digest = hashlib.sha256(Path(conf).read_bytes()).hexdigest()
        confs = on_all(lambda h: ["sha256sum", str(tb.REPO / tb.CLUSTER["path"])])
        for host in tb.CLUSTER["hosts"]:
            if commits[host] != here:
                sys.exit(f"{host}: commit {commits[host][:10]}, here {here[:10]}: check out the same commit and run make")
            if not confs[host].startswith(digest):
                sys.exit(f"{host}: its {conf} differs from this one: copy it over")
    for host in tb.CLUSTER["hosts"]:
        print(f"  {host:8} ok, kernel {kernels[host]}", flush=True)


def setup(action):
    print(f"=== setup.sh {action} on every machine", flush=True)
    script = str(tb.REPO / "scripts/cluster/setup.sh")
    on_all(lambda h: [script, action, h], check=(action == "up"))


def results_owned(root, session="", check=True):
    """Make root/session on the LB and client machines, where the tools write as
    root, and give the whole of root to the user running sudo there. The client's
    is also where this driver writes as a normal user, and what collect() reads
    on the LB's as one."""
    script = 'mkdir -p "$1" && chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$2"'
    for where in ("lb", "cli"):
        subprocess.run(tb.command(where, ["sh", "-c", script, "sh", str(root / session), str(root)]), check=check, cwd=tb.REPO)


def collect(root):
    """Copy what the LB and client machines wrote (sigmon, controller, resetmon) here."""
    results_owned(root)
    if tb.CLUSTER["emulated"]:
        return  # same file system
    for host in {tb.CFG["LB"], tb.CFG["CLIENT"]}:
        address = tb.CLUSTER["hosts"][host]["address"]
        if tb.CLUSTER["user"]:
            address = f"{tb.CLUSTER['user']}@{address}"
        subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(tb.SSH_OPTS),
                        f"{address}:{tb.CLUSTER['repo']}/{root}/", f"{root}/"], check=True, cwd=tb.REPO)


def smoke(s):
    """Ten requests through the VIP, each on a new connection. If this fails,
    nothing else will work: print what to check."""
    c.lbctl("table", "equal")
    url = f"http://{tb.CFG['VIP']}:{tb.CFG['BACKEND_PORT']}/work"
    ok = 0
    for _ in range(10):
        r = subprocess.run(tb.command("cli", ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "5", url]),
                           capture_output=True, text=True, cwd=tb.REPO)
        ok += r.stdout == "200"
    s.rec.write(dict(kind="smoke", ok=ok, of=10))
    if ok < 10:
        sys.exit(f"smoke test: {ok}/10 requests through the VIP succeeded. Check (docs/cluster-runbook.md): "
                 "client isolation on the access point, its filtering of replies from the VIP, "
                 "the MTU, a firewall on the machines, and `sudo bin/lbctl stats` on the LB machine.")
    print("  smoke test: 10/10 through the VIP", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("conf")
    ap.add_argument("--dry", action="store_true", help="tiny durations, to test the plumbing")
    ap.add_argument("--quick", action="store_true", help="~33 min instead of ~50; docs/cluster-runbook.md says what it gives up")
    ap.add_argument("--no-tune", action="store_true", help="skip tune.sh (emulation: one kernel for all machines)")
    ap.add_argument("--root", default=f"results/cluster-{datetime.now():%Y%m%d-%H%M%S}")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    if os.geteuid() == 0:
        sys.exit("run it as your user, not with sudo: each command uses sudo where it runs")

    tb.use_cluster(args.conf)
    c.configure_cluster()
    c.NO_TUNE = args.no_tune
    c.P = c.DRY if args.dry else dict(c.FULL, denominator_reps=1, noise_measure=90, probes=4 if args.quick else 5)
    if args.quick:
        c.ANTAGONIST = (30, 120, 150)
    c.NOISE_LOADS = (0.8,)
    root = Path(args.root)

    preflight(args.conf)
    try:
        setup("up")  # inside: if it fails on one machine, the ones before it get undone too
        results_owned(root, "s1")
        with c.Session(root, "s1", args.dry) as s:
            smoke(s)
            denominators = c.measure_denominators(s, args.dry)
            c.noise_runs(s, denominators)
        collect(root)
        print("=== tuning", flush=True)
        subprocess.run([sys.executable, str(tb.REPO / "bench/tune_signals.py"), str(root)], check=True, cwd=tb.REPO)

        d, tuning = c.campaign_inputs(root)
        capacities, cores = d["capacity"], d["cores"]
        policies = {"equal": dict.fromkeys(capacities, 1), "cores": cores, "oracle": capacities}
        results_owned(root, "s2")
        with c.Session(root, "s2", args.dry) as s:
            for policy, rep in [("oracle", 1), ("equal", 1), ("oracle", 2), ("cores", 1)][:2 if args.dry or args.quick else 4]:
                c.lbctl("table", c.weights(policies[policy]))
                shares = c.shares_of_table()
                pred = c.predicted(capacities, shares)
                c.search(s, tb.CFG["VIP"], 0.7 * pred, 1.05 * pred, experiment="static", policy=policy, rep=rep,
                         shares=shares, predicted=pred)
            for signal_name in ("cpu", "rif-total")[:1 if args.dry else 2]:
                c.controller_table(s, signal_name, 1, tuning, capacities)
        collect(root)

        results_owned(root, "s3")
        with c.Session(root, "s3", args.dry) as s:
            for config in ("oracle", "cpu", "rif-total", "cpu-noredirect"):  # the contrast last (orphaned sockets)
                c.scenario_run(s, "antagonist", config, 1, capacities, tuning, args.dry)
        collect(root)
    finally:
        setup("down")
        results_owned(root, check=False)  # also after a failure: the tools' files are root's until here
    subprocess.run([sys.executable, str(tb.REPO / "bench/analyze_campaign.py"), str(root)], cwd=tb.REPO,
                   stdout=subprocess.DEVNULL)
    print(f"done: {root}/summary.md", flush=True)


if __name__ == "__main__":
    main()
