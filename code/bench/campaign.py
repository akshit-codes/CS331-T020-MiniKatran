#!/usr/bin/env python3
"""The benchmark campaign (docs/campaign.md): one function per session.

    sudo python3 bench/campaign.py s1 --root results/campaign-<date> [--dry]
    python3 bench/tune_signals.py results/campaign-<date>        # tuning (no sudo)
    sudo python3 bench/campaign.py s2 --root results/campaign-<date> [--dry]
    sudo python3 bench/campaign.py s2-extra|s3|s4|s5|s6|s7 --root results/campaign-<date> [--dry]
    sudo python3 bench/campaign.py s8|...|s15 --root results/campaign-<date> [--dry]   # checkpointed: rerun to resume
    python3 bench/analyze_campaign.py results/campaign-<date>    # tables and plots

Each session applies tune.sh, builds the testbed, calibrates the request cost,
starts the backends (exponential cost, beP1 slowed), loads the LB, runs its
experiments and always undoes everything. Every probe is appended to
<root>/<session>/runs.jsonl as it happens, so a crash loses nothing.
--dry runs every experiment type with tiny durations and no tuning, to find
bugs before spending real machine time.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import testbed as tb

# --- Campaign settings (D37) ---
# L = 20 ms: with exponential cost an E-core request alone has a p99 of ~8.5 ms
# (4.6 × its 1.84 ms mean); at 10 ms capacity would mostly measure single
# request times, not load. 10 and 50 ms are read off the same probes.
SLO = dict(p99_ms=20.0, error_rate=0.001, min_served=0.98)
SLOWNESS = {"beP1": 1.6}                       # an older, slower server
COST_MS = 1.0                                  # mean request cost on a P-core
CHURN = ["-conns", "512", "-reqs-per-conn", "20"]
FULL = dict(warmup=5, measure=20, rest=2, probes=5, noise_warmup=15, noise_measure=150,
            converge=90, denominator_reps=2)
DRY = dict(warmup=2, measure=4, rest=1, probes=2, noise_warmup=3, noise_measure=12,
           converge=10, denominator_reps=1)
P = FULL
NOISE_LOADS = (0.5, 0.8)  # fractions of the summed capacities for the noise runs
NO_TUNE = False  # bench/cluster.py --no-tune (emulation: all "machines" share one kernel)
ANTAGONIST = (60, 180, 240)  # antagonist scenario: on, off, end (s); bench/cluster.py --quick shortens it


def slowness(be):
    """A backend's slowness: per backend in cluster.conf, else SLOWNESS."""
    return be.slowness if tb.CLUSTER else SLOWNESS.get(be.name, 1.0)


def configure_cluster():
    """Workload settings from cluster.conf (real machines: a wifi network can't
    carry thousands of requests per second, so requests cost more, D39)."""
    global COST_MS, CHURN, SLO, LONG_LIVED
    cfg = tb.CFG
    COST_MS = float(cfg.get("COST_MS", COST_MS))
    SLO = dict(SLO, p99_ms=float(cfg.get("SLO_P99_MS", SLO["p99_ms"])))
    CHURN = ["-conns", cfg.get("CONNS", "512"), "-reqs-per-conn", "20"]
    LONG_LIVED = dict(rate=float(cfg.get("LONG_LIVED_RATE", 200)),
                      extra=["-conns", cfg.get("LONG_LIVED_CONNS", "100"), "-reqs-per-conn", "0"])


class Recorder:
    """Appends one JSON object per line."""

    def __init__(self, path):
        self.path = path

    def write(self, obj):
        with open(self.path, "a") as f:
            f.write(json.dumps(obj) + "\n")


def meets_slo(s):
    # "Served" compares completed requests with the requests actually
    # scheduled, not with the target rate: with Poisson arrivals the count in a
    # short window varies by a few percent (dry run: 96.6% at 469 req/s, all served).
    return (s["ok"] > 0 and s["latency_ms"]["p99"] <= SLO["p99_ms"]
            and s["error_rate"] <= SLO["error_rate"]
            and s["ok"] >= SLO["min_served"] * s["requests"])


def lbctl(*args):
    return tb.run(tb.command("lb", [str(tb.BIN / "lbctl"), *args]), cwd=tb.REPO, capture_output=True, text=True).stdout


# --- Session setup ---

class Session:
    """tune.sh + testbed + calibrated backends + LB, always undone on exit.
    In cluster mode the machines were set up by bench/cluster.py, and this
    tunes them, starts the backends and loads the LB on them."""

    def __init__(self, root, name, dry):
        self.dir = Path(root) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rec = Recorder(self.dir / "runs.jsonl")
        self.dry = dry
        self.tune = not (dry or NO_TUNE)

    def __enter__(self):
        if self.tune:
            tb.tune("apply")
        try:
            if not tb.CLUSTER:
                tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
                self.sysmon = subprocess.Popen([sys.executable, str(tb.REPO / "bench/sysmon.py"), str(self.dir / "sysmon.csv")])
            backends = list(tb.BACKENDS.values())
            p_ref = next(be for be in backends if be.kind == "P")
            e_ref = next((be for be in backends if be.kind == "E"), p_ref)
            p_units, e_units = tb.calibrate_backend(p_ref), tb.calibrate_backend(e_ref)
            self.work = round(p_units * COST_MS)
            # Mean service time per request on each kind of core, slowness included.
            self.service_ms = {be.name: COST_MS * (1 if be.kind == "P" else p_units / e_units) * slowness(be)
                               for be in backends}
            self.rec.write(dict(kind="calibration", p_units_per_ms=p_units, e_units_per_ms=e_units,
                                work=self.work, service_ms=self.service_ms, slo=SLO, cost_ms=COST_MS,
                                slowness={be.name: slowness(be) for be in backends}))
            for be in backends:
                tb.start_backend(be, self.work, slowness=slowness(be), cost="exp")
            tb.lb_load()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        print("== cleanup", flush=True)
        # After an interrupted run: the tools and load generators it left behind.
        for name in list(tb._tools):
            tb._tools.pop(name).kill()
        if not tb.CLUSTER:
            for tool in ("loadgen", "resetmon", "sigmon", "controller"):
                subprocess.run(["pkill", "-x", tool], capture_output=True)
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
            tb.stop_antagonist(be.name)
        tb.lb_unload()
        if getattr(self, "sysmon", None):
            self.sysmon.terminate()
            self.sysmon.wait()
        if not tb.CLUSTER:
            subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"], capture_output=True)
        if self.tune:
            tb.tune("undo")
        tb.give_back(self.dir.parent)  # the campaign root too: sudo created it


# --- Measurement building blocks ---

def wait_idle(timeout=60):
    """Wait until no backend has requests in flight; returns the seconds waited.
    Without this, a probe after an overloaded one starts while the backends
    still work off the old queue (journal: campaign S2, first try)."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if all(tb.backend_metrics(be)["rif_inflight"] == 0 for be in tb.BACKENDS.values()):
            break
        time.sleep(0.25)
    return time.monotonic() - start


def direct_targets(rates):
    """loadgen arguments that load backends directly (no LB) at these rates."""
    port = tb.CFG["BACKEND_PORT"]
    return ["-targets", ",".join(f"{tb.BACKENDS[b].ip}:{port}={r:.1f}" for b, r in rates.items())]


def probe(s, target_ip, rate, targets=None, judged=None, **tags):
    """One load step, started on idle backends: warm-up + measured phase;
    True if it meets the SLO. targets(rate) -> {backend: rate}: load several
    backends directly instead of target_ip; judged: only this one's requests
    count for the SLO."""
    waited = wait_idle()
    rates = targets(rate) if targets else None
    summary = tb.loadgen(target_ip, sum(rates.values()) if rates else rate, P["warmup"], P["measure"],
                         extra=CHURN + direct_targets(rates) if rates else CHURN)
    if judged:
        tags["whole_summary"] = {k: summary[k] for k in ("target_rate", "gen_dropped", "dispatch_lag_ms")}
        summary = summary["per_server"][f"{tb.BACKENDS[judged].ip}:{tb.CFG['BACKEND_PORT']}"]
    ok = meets_slo(summary)
    # Requests waited long to be sent: either the generator's CPUs can't keep
    # up (S2 second try, on 2 CPUs) or all connections are busy waiting for slow
    # responses (S2 third try: only ever in probes that missed the SLO anyway).
    # Check the generator's CPUs in sysmon.csv to tell which.
    limited = summary["send_delay_ms"]["p99"] > 5
    s.rec.write(dict(kind="probe", rate=rate, meets_slo=ok, send_delayed=limited, summary=summary,
                     idle_wait_s=round(waited, 2), finished=time.time(), **tags))
    print(f"    {rate:7.0f} req/s: p99 {summary['latency_ms']['p99']:6.2f} ms, errors {summary['error_rate']:.4f}"
          f" -> {'ok' if ok else 'over'}{'  (requests waited to be sent)' if limited else ''}", flush=True)
    time.sleep(P["rest"])
    return ok


def search(s, target_ip, lo, hi, targets=None, judged=None, **tags):
    """Capacity at SLO: bisection between lo and hi. If no probe passes, one
    more at 0.75·lo; if all pass, one more at 1.25·hi (flagged either way)."""
    print(f"  search {tags} in [{lo:.0f}, {hi:.0f}]", flush=True)
    tags = dict(tags, search_range=[lo, hi])
    passed, failed = None, None
    for _ in range(P["probes"]):
        mid = (lo + hi) / 2
        if probe(s, target_ip, mid, targets=targets, judged=judged, **tags):
            lo, passed = mid, mid
        else:
            hi, failed = mid, mid
    flag = ""
    if passed is None:
        flag = "below range"
        extra = 0.75 * lo
        passed = extra if probe(s, target_ip, extra, targets=targets, judged=judged, **tags) else None
    elif failed is None:
        flag = "above range"
        extra = 1.25 * hi
        if probe(s, target_ip, extra, targets=targets, judged=judged, **tags):
            passed = extra
    s.rec.write(dict(kind="capacity", capacity=passed, flag=flag, **tags))
    print(f"  => capacity {passed} {flag}", flush=True)
    return passed


def shares_of_table():
    """Each backend's share of the active table's buckets (first hops)."""
    counts = dict.fromkeys(tb.BACKENDS, 0)
    for line in lbctl("dump").splitlines():
        if not line.startswith("#"):
            counts[line.split()[1]] += 1
    total = sum(counts.values())
    return {name: n / total for name, n in counts.items()}


def predicted(capacities, shares):
    """Cluster capacity if the busiest backend (relative to its capacity) is the limit."""
    return min(capacities[b] / shares[b] for b in shares if shares[b] > 0)


def weights(values):
    return ",".join(f"{name}={v:.3f}" for name, v in values.items())


def load_json(path):
    return json.loads(Path(path).read_text())


# --- Session S1: denominators and signal noise ---

def s1(root, dry, noise_only=False):
    if not dry and not noise_only:
        print("=== end-to-end check (gate)", flush=True)
        if subprocess.run([sys.executable, str(tb.REPO / "bench/e2e_lb.py")], capture_output=True).returncode != 0:
            sys.exit("e2e_lb.py failed: fix that first")
    with Session(root, "s1", dry) as s:
        if noise_only:  # keep the denominators, redo the noise runs
            denominators = load_json(s.dir / "denominators.json")["capacity"]
        else:
            denominators = measure_denominators(s, dry)
        noise_runs(s, denominators)


def measure_denominators(s, dry):
    """Each backend alone, directly (no LB), interleaved repeats."""
    capacities = {name: [] for name in tb.BACKENDS}
    theoretical = {be.name: be.ncpus * 1000 / s.service_ms[be.name] for be in tb.BACKENDS.values()}
    for rep in range(1, P["denominator_reps"] + 1):
        for be in tb.BACKENDS.values():
            t = theoretical[be.name]
            capacities[be.name].append(search(s, be.ip, 0.5 * t, 1.0 * t, experiment="denominator", backend=be.name, rep=rep))
    denominators = {}
    for name, caps in capacities.items():
        found = [c for c in caps if c]
        if not found and not dry:
            sys.exit(f"no capacity found for {name}: check runs.jsonl before going on")
        denominators[name] = sum(found) / len(found) if found else 0.5 * theoretical[name]
    (s.dir / "denominators.json").write_text(json.dumps(dict(
        capacity=denominators, runs=capacities, theoretical=theoretical, service_ms=s.service_ms,
        cores={be.name: be.ncpus for be in tb.BACKENDS.values()}), indent=2))
    return denominators


def noise_runs(s, denominators):
    """Signal noise: oracle table, no controller, all signals every second."""
    lbctl("table", weights(denominators))
    for load in NOISE_LOADS:
        rate = load * sum(denominators.values())
        print(f"=== noise at {load:.0%} ({rate:.0f} req/s)", flush=True)
        tb.start_tool("lb", "sigmon", [str(tb.BIN / "sigmon"), "-out", str(s.dir / f"noise-{load}.csv")])
        summary = tb.loadgen(tb.CFG["VIP"], rate, P["noise_warmup"], P["noise_measure"], extra=CHURN)
        tb.stop_tool("lb", "sigmon")
        s.rec.write(dict(kind="noise", load=load, rate=rate, warmup=P["noise_warmup"], summary=summary,
                         connections=int(CHURN[1])))


# --- Session S2: static policies and controller-found tables ---

def start_controller(signal_name, tuning, log, initial=None, extra=()):
    """The live controller with the tuned period and band for this signal (on
    the LB's machine); stop it with tb.stop_tool("lb", "controller")."""
    t = tuning[signal_name]
    cmd = [str(tb.BIN / "controller"), "-signal", signal_name, "-period", f"{t['period_s']}s",
           "-delta-on", str(t["delta_on"]), "-delta-off", str(t["delta_off"]), "-out", str(log), *extra]
    if initial:
        cmd += ["-weights", weights(initial)]
    tb.start_tool("lb", "controller", cmd)


def stop_controller():
    """Stop the controller; returns why the run is invalid if it had died
    during the run (one failed signal read ends it, and the table stays frozen:
    the run would look like a controller run but isn't), else None."""
    alive = tb.tool_alive("lb", "controller")
    last_words = tb.stop_tool("lb", "controller")
    if alive:
        return None
    print(f"  !!! the controller died during this run: {last_words}", flush=True)
    return f"controller died: {last_words}"


def campaign_inputs(root):
    d = load_json(Path(root) / "s1" / "denominators.json")
    tuning = {t["signal"]: t["chosen"] for t in load_json(Path(root) / "tuning" / "tuning.json")}
    return d, tuning


def converge(s, signal_name, rep, tuning, capacities):
    """Let the controller converge at 75% load from shares by cores and freeze
    its table. Returns (shares, capacity predicted from them, {"invalid": why} or {})."""
    total = sum(capacities.values())
    t = tuning[signal_name]
    print(f"=== controller {signal_name} (P {t['period_s']} s, band {t['delta_on']:.3f}/{t['delta_off']:.3f}),"
          f" converging {P['converge']} s at 75%", flush=True)
    start_controller(signal_name, tuning, s.dir / f"controller-{signal_name}-{rep}.csv")
    summary = tb.loadgen(tb.CFG["VIP"], 0.75 * total, 0, P["converge"], extra=CHURN)
    died = stop_controller()  # the table it published stays
    invalid = dict(invalid=died) if died else {}
    shares = shares_of_table()
    (s.dir / f"table-{signal_name}-{rep}.txt").write_text(lbctl("dump"))
    pred = predicted(capacities, shares)
    s.rec.write(dict(kind="converge", signal=signal_name, rep=rep, tuning=t, summary=summary, shares=shares, **invalid))
    return shares, pred, invalid


def controller_table(s, signal_name, rep, tuning, capacities, lo=0.7, hi=1.05):
    """Let the controller converge at 75% load from shares by cores, freeze its
    table, and measure the capacity at SLO with it (S2), searching
    [lo, hi] × the capacity predicted from its shares."""
    shares, pred, invalid = converge(s, signal_name, rep, tuning, capacities)
    search(s, tb.CFG["VIP"], lo * pred, hi * pred, experiment="controller", policy=f"ctl-{signal_name}",
           rep=rep, shares=shares, predicted=pred, **invalid)


def s2(root, dry):
    d, tuning = campaign_inputs(root)
    capacities, cores = d["capacity"], d["cores"]
    policies = {"equal": dict.fromkeys(capacities, 1), "cores": cores, "oracle": capacities}
    with Session(root, "s2", dry) as s:
        static_order = [("oracle", 1), ("equal", 1), ("oracle", 2), ("cores", 1), ("oracle", 3)]
        controller_order = [("cpu", 1), ("rif", 1), ("cpu", 2), ("rif", 2)]
        if dry:
            static_order, controller_order = static_order[:2], controller_order[:1]
        for policy, rep in static_order:
            lbctl("table", weights(policies[policy]))
            shares = shares_of_table()
            pred = predicted(capacities, shares)
            search(s, tb.CFG["VIP"], 0.7 * pred, 1.05 * pred, experiment="static", policy=policy, rep=rep,
                   shares=shares, predicted=pred)
        for signal_name, rep in controller_order:
            controller_table(s, signal_name, rep, tuning, capacities)


def s2_extra(root, dry):
    """One more controller-found table, with RIF not divided by cores: does
    RIF's S2 result come from the normalisation or from RIF itself?"""
    d, tuning = campaign_inputs(root)
    with Session(root, "s2-extra", dry) as s:
        controller_table(s, "rif-total", 1, tuning, d["capacity"])


def s2_rif_total(root, dry):
    """Two more total-RIF tables, with the search range opened up: the first
    run passed every probe up to 1.05 × its prediction (a lower bound only)."""
    d, tuning = campaign_inputs(root)
    with Session(root, "s2-rif-total", dry) as s:
        for rep in (2, 3)[:1 if dry else 2]:
            controller_table(s, "rif-total", rep, tuning, d["capacity"], lo=0.85, hi=1.15)


# --- Sessions S3, S4, S5: scenarios ---

NEIGHBOURS_AT = 0.9                  # S6: the other backends' load while one's capacity is searched (near the oracle's operating point)
S6_THIRD_PAIR_BEFORE_MIN = 66        # S6: run the third pair of tables only if the session is not later than this
SCENARIO_LOAD = 0.75                 # offered load, fraction of the sum of denominators
LONG_LIVED = dict(rate=200, extra=["-conns", "100", "-reqs-per-conn", "0"])  # outlive the dwell (RQ4)
WINDOW = ["-window", "5s"]
NO_PROTECTION = ["-dwell", "0", "-global-cap", "4096", "-per-backend-cap", "4096", "-max-step", "1",
                 "-gain", "1", "-delta-on", "0.02", "-delta-off", "0.01"]


def set_slowness(name, value):
    port = tb.CFG["METRICS_PORT"]
    tb.run(tb.command(name, ["curl", "-sf", f"localhost:{port}/slowness?set={value}"]), capture_output=True)


def siblings(cpus):
    """The idle hyperthread siblings of P-cores: 8,10 -> 9,11 (pairs (8,9), (10,11))."""
    return ",".join(str(c + 1) for c in tb.expand_cpus(cpus))


def scenario_events(name):
    """(seconds, description, action) for each scenario, and its length."""
    name = name.partition("@")[0]  # "antagonist@60": the same events at another load (scenario_run)
    on = lambda: tb.start_antagonist("beE1", tb.BACKENDS["beE1"].cpus, 0.6)
    off = lambda: tb.stop_antagonist("beE1")
    if name == "step":  # beP2 suddenly needs twice the CPU per request, for good
        return 180, [(60, "beP2 slowness 2", lambda: set_slowness("beP2", 2.0))]
    if name == "antagonist":  # a CPU-heavy neighbour on beE1's cores, in its own cgroup
        at_on, at_off, length = ANTAGONIST
        return length, [(at_on, "antagonist on beE1", on), (at_off, "antagonist off", off)]
    if name == "flapping":  # the same neighbour, 10 s on / 10 s off
        return 200, [(at, "antagonist " + ("on" if i % 2 == 0 else "off"), on if i % 2 == 0 else off)
                     for i, at in enumerate(range(60, 180, 10))] + [(180, "antagonist off", off)]
    if name.startswith("flapping-"):  # flapping-<period>: on for half the period, off for half, 30-150 s
        half = int(name.split("-")[1]) / 2
        toggles = [i * half for i in range(int(120 / half))]
        return 160, [(30 + at, "antagonist " + ("on" if i % 2 == 0 else "off"), on if i % 2 == 0 else off)
                     for i, at in enumerate(toggles)] + [(150, "antagonist off", off)]
    if name.startswith("fastflap-"):  # fastflap-<period>: the neighbour switches itself on and off, 30-150 s
        half = float(name.split("-")[1]) / 2
        return 160, [(30, f"antagonist flapping, {half:g}-s phases",
                      lambda: tb.start_antagonist("beE1", tb.BACKENDS["beE1"].cpus, 0.6, flap=half)),
                     (150, "antagonist off", off)]
    if name == "sibling":  # a busy neighbour on the idle hyperthreads of beP1's cores
        cpus = siblings(tb.BACKENDS["beP1"].cpus)
        return 180, [(60, f"antagonist on CPUs {cpus}", lambda: tb.start_antagonist("sibling", cpus, 1.0, on="beP1")),
                     (150, "antagonist off", lambda: tb.stop_antagonist("sibling", on="beP1"))]
    if name == "sibling-stream":  # the same, first running the request loop, then walking 64 MiB (evicts caches)
        cpus = siblings(tb.BACKENDS["beP1"].cpus)
        return 170, [(30, f"burn antagonist on CPUs {cpus}", lambda: tb.start_antagonist("sibling", cpus, 1.0, "burn", on="beP1")),
                     (90, "antagonist off", lambda: tb.stop_antagonist("sibling", on="beP1")),
                     (100, f"stream antagonist on CPUs {cpus}", lambda: tb.start_antagonist("sibling", cpus, 1.0, "stream", on="beP1")),
                     (160, "antagonist off", lambda: tb.stop_antagonist("sibling", on="beP1"))]
    raise ValueError(name)


def reset_backends(capacities):
    for be in tb.BACKENDS.values():
        set_slowness(be.name, slowness(be))
        tb.stop_antagonist(be.name)
    if "beP1" in tb.BACKENDS:
        tb.stop_antagonist("sibling", on="beP1")
    lbctl("table", weights(capacities))
    wait_idle()


def scenario_run(s, scenario, config, rep, capacities, tuning, dry, table=None):
    """One scenario repeat. config: "oracle" (static oracle table, no
    controller), "hindsight" (static, table proportional to `table`: the
    capacities during the disturbance), a signal name (controller with that
    signal, starting from the oracle table), "cpu-noprotect" (the CPU controller
    without dwell and caps) or "cpu-noredirect" (the CPU controller, redirectors
    not forwarding to the second hop: the RQ4 contrast)."""
    length, events = scenario_events(scenario)
    if dry:
        length, events = length // 6, [(at // 6, what, act) for at, what, act in events]
    name = f"{scenario}-{config}-{rep}"
    print(f"=== {name} ({length} s)", flush=True)
    if config == "cpu-noredirect":
        tb.lb_unload()
        tb.lb_load("-no-forward")
    reset_backends(table or capacities)
    namespaces = [*tb.BACKENDS, "cli"]
    before = {ns: tb.tcp_counters(ns) for ns in namespaces}

    # resetmon watches tracepoints, which `ip netns exec` can't reach on the
    # namespace testbed: there it runs with the LB tools; on a cluster it runs
    # on the client, where broken connections show up as received resets.
    resets_at = "cli" if tb.CLUSTER else "lb"
    tb.start_tool("lb", "sigmon", [str(tb.BIN / "sigmon"), "-out", str(s.dir / f"{name}-sigmon.csv")])
    tb.start_tool(resets_at, "resetmon", [str(tb.BIN / "resetmon"), "-out", str(s.dir / f"{name}-resets.csv")])
    ctl = config not in ("oracle", "hindsight")
    if ctl:
        signal_name, extra = {"cpu-noprotect": ("cpu", NO_PROTECTION), "cpu-noredirect": ("cpu", ()),
                              "cpu-gain0.1": ("cpu", ("-gain", "0.1")), "cpu-gain1": ("cpu", ("-gain", "1")),
                              }.get(config, (config, ()))
        start_controller(signal_name, tuning, s.dir / f"{name}-controller.csv", initial=capacities, extra=extra)
    total = sum(capacities.values())
    percent = scenario.partition("@")[2]  # "<scenario>@<percent>": offered load other than SCENARIO_LOAD
    load = int(percent) / 100 if percent else SCENARIO_LOAD
    churn = subprocess.Popen(tb.loadgen_command(tb.CFG["VIP"], load * total, 0, length, CHURN + WINDOW),
                             stdout=subprocess.PIPE, text=True, cwd=tb.REPO)
    long_lived = subprocess.Popen(tb.loadgen_command(tb.CFG["VIP"], LONG_LIVED["rate"], 0, length,
                                                     LONG_LIVED["extra"] + WINDOW),
                                  stdout=subprocess.PIPE, text=True, cwd=tb.REPO)
    start = time.monotonic()
    for at, what, act in events:
        time.sleep(max(0, start + at - time.monotonic()))
        act()
        print(f"  t={time.monotonic() - start:5.1f} s: {what}", flush=True)
    churn_summary = json.loads(churn.communicate()[0])
    long_summary = json.loads(long_lived.communicate()[0])
    died = stop_controller() if ctl else None
    resets = tb.stop_tool(resets_at, "resetmon")
    tb.stop_tool("lb", "sigmon")
    after = {ns: tb.tcp_counters(ns) for ns in namespaces}
    delta = lambda ns, key: after[ns][key] - before[ns][key]
    s.rec.write(dict(kind="scenario", scenario=scenario, config=config, rep=rep, length=length,
                     events=[(at, what) for at, what, _ in events], rate=load * total, load=load,
                     churn=churn_summary, long_lived=long_summary, resetmon=resets,
                     rsts_sent={b: delta(b, "TcpOutRsts") for b in tb.BACKENDS},
                     client_resets=delta("cli", "TcpEstabResets"),
                     cancelled={b: tb.backend_metrics(be)["requests_cancelled_total"] for b, be in tb.BACKENDS.items()},
                     **(dict(table=table) if table else {}), **(dict(invalid=died) if died else {})))
    print(f"  p99 per 5 s: {[round(w) for w in churn_summary['window_p99_ms']]}", flush=True)
    if config == "cpu-noredirect":  # back to the normal redirectors
        tb.lb_unload()
        tb.lb_load()
    print(f"  long-lived: {long_summary['errors']} errors, {long_summary['connections']} connections; {resets}", flush=True)


def s3(root, dry):
    d, tuning = campaign_inputs(root)
    runs = [("step", c, r) for c, r in [("oracle", 1), ("cpu", 1), ("rif", 1), ("cpu", 2), ("rif", 2), ("cpu", 3), ("rif", 3)]]
    runs += [("antagonist", c, r) for c, r in [("oracle", 1), ("cpu", 1), ("rif", 1), ("cgroup", 1), ("cpu", 2),
                                                ("rif", 2), ("cpu", 3), ("rif", 3), ("cpu-noprotect", 1)]]
    if dry:
        runs = [runs[0], runs[1], runs[7], runs[-1]]
    with Session(root, "s3", dry) as s:
        for scenario, config, rep in runs:
            scenario_run(s, scenario, config, rep, d["capacity"], tuning, dry)


def s5(root, dry):
    """Follow-ups (journal): total RIF in the dynamic scenarios, the redirector
    contrast for RQ4, a sibling antagonist that contends, and the flapping sweep."""
    d, tuning = campaign_inputs(root)
    runs = [("step", "rif-total", 1), ("antagonist", "rif-total", 1), ("sibling-stream", "oracle", 1),
            ("step", "rif-total", 2), ("antagonist", "rif-total", 2)]
    for period in (5, 20, 60):
        runs += [(f"flapping-{period}", c, 1) for c in ("oracle", "cpu", "rif-total")]
    # Last: its broken connections leave orphaned sockets on the backends that
    # send keepalive probes for minutes, which would show up in later runs' resets.
    runs.append(("antagonist", "cpu-noredirect", 1))
    if dry:
        runs = [runs[2], runs[5], runs[-1]]
    with Session(root, "s5", dry) as s:
        for scenario, config, rep in runs:
            scenario_run(s, scenario, config, rep, d["capacity"], tuning, dry)


def s6(root, dry):
    """Follow-ups after the review (docs/campaign.md), one session, interleaved:
    the denominators with all backends busy (does contention explain the
    oracle's missing 5%?), a static table re-weighted for each disturbance (the
    fair static baseline), and CPU vs total-RIF tables in one session. The last
    pair of tables is what's skipped if the session runs long."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]
    total = sum(capacities.values())
    names = list(capacities)
    began = time.monotonic()
    with Session(root, "s6", dry) as s:
        # Each backend alone vs with its neighbours held at NEIGHBOURS_AT of
        # theirs. Same protocol, this session; the order alternates so that
        # "busy" isn't always second.
        print("=== denominators: alone and with neighbours busy", flush=True)
        for i, name in enumerate(names[:1 if dry else 4]):
            c = capacities[name]
            ip = tb.BACKENDS[name].ip
            def busy(rate, name=name):
                return {**{o: NEIGHBOURS_AT * capacities[o] for o in names if o != name}, name: rate}
            for how in ("alone", "busy") if i % 2 == 0 else ("busy", "alone"):
                search(s, ip, 0.8 * c, 1.2 * c, targets=busy if how == "busy" else None, judged=name if how == "busy" else None,
                       experiment=f"denominator-{how}", backend=name, rep=1)

        # The capacities that hold during each disturbance.
        print("=== capacities during the disturbances", flush=True)
        set_slowness("beP2", 2.0)
        step_cap = search(s, tb.BACKENDS["beP2"].ip, 0.35 * capacities["beP2"], 0.65 * capacities["beP2"],
                          experiment="disturbed", backend="beP2", disturbance="step", rep=1)
        set_slowness("beP2", slowness(tb.BACKENDS["beP2"]))
        tb.start_antagonist("beE1", tb.BACKENDS["beE1"].cpus, 0.6)
        antagonist_cap = search(s, tb.BACKENDS["beE1"].ip, 0.3 * capacities["beE1"], 0.9 * capacities["beE1"],
                                experiment="disturbed", backend="beE1", disturbance="antagonist", rep=1)
        tb.stop_antagonist("beE1")
        if dry:  # too few probes to find anything
            step_cap, antagonist_cap = step_cap or 0.5 * capacities["beP2"], antagonist_cap or 0.6 * capacities["beE1"]
        hindsight = {"step": {**capacities, "beP2": step_cap}, "antagonist": {**capacities, "beE1": antagonist_cap}}
        for rep in (1, 2)[:1 if dry else 2]:
            for scenario in ("step", "antagonist"):
                scenario_run(s, scenario, "hindsight", rep, capacities, tuning, dry, table=hindsight[scenario])

        # Tables and the gap, interleaved. Oracle through the LB vs the same
        # shares without an LB, against the same denominators. Same search range
        # for all, wide enough that neither table hits its edge (old CPU run 2 did).
        def oracle(rep):
            lbctl("table", weights(capacities))
            shares = shares_of_table()
            pred = predicted(capacities, shares)
            with_sigmon(s, f"oracle-{rep}", lambda: search(s, tb.CFG["VIP"], 0.85 * pred, 1.15 * pred, experiment="static",
                                                           policy="oracle", rep=rep, shares=shares, predicted=pred))

        def direct(rep):
            first_ip = tb.BACKENDS[names[0]].ip
            with_sigmon(s, f"direct-{rep}", lambda: search(s, first_ip, 0.85 * total, 1.15 * total,
                                                           targets=lambda r: {b: r * capacities[b] / total for b in names},
                                                           experiment="direct-mix", policy="direct", rep=rep))

        def table(signal_name, rep):
            controller_table(s, signal_name, rep, tuning, capacities, lo=0.85, hi=1.15)

        order = [lambda: table("cpu", 1), lambda: table("rif-total", 1), lambda: oracle(1), lambda: direct(1),
                 lambda: table("rif-total", 2), lambda: table("cpu", 2), lambda: direct(2), lambda: oracle(2)]
        for step in order[:4] if dry else order:
            step()
        if not dry and (time.monotonic() - began) / 60 < S6_THIRD_PAIR_BEFORE_MIN:
            table("cpu", 3)
            table("rif-total", 3)
        else:
            print("  skipping the third pair of tables: time budget", flush=True)


def with_sigmon(s, name, measure):
    """Log every signal during measure(); the log's start goes into runs.jsonl
    so each probe's seconds can be found in it (the load spread per backend)."""
    tb.start_tool("lb", "sigmon", [str(tb.BIN / "sigmon"), "-out", str(s.dir / f"{name}-sigmon.csv")])
    s.rec.write(dict(kind="sigmon_start", name=name, started=time.time()))
    measure()
    tb.stop_tool("lb", "sigmon")


def kernel_psi(s, capacities, dry):
    """PSI with the pool backend vs the kernel-queue backend (D25): all traffic
    to beP2 at 50%, 90%, 130% of its capacity, then 50% with an antagonist."""
    phase_s = 6 if dry else 40
    phases = [("50%", 0.5, False), ("90%", 0.9, False), ("130%", 1.3, False), ("50% + antagonist", 0.5, True)]
    for mode in ("pool", "kernel"):
        print(f"=== kernel-queue PSI: {mode} mode", flush=True)
        for be in tb.BACKENDS.values():
            tb.stop_backend(be)
            tb.start_backend(be, s.work, slowness=SLOWNESS.get(be.name, 1.0), cost="exp", mode=mode)
        lbctl("table", "beP2=1")
        mon = subprocess.Popen([str(tb.BIN / "sigmon"), "-out", str(s.dir / f"kernelpsi-{mode}-sigmon.csv")], cwd=tb.REPO)
        start = time.monotonic()
        marks = []
        for label, frac, antagonist in phases:
            wait_idle()
            if antagonist:
                tb.start_antagonist("beP2", tb.BACKENDS["beP2"].cpus, 0.6)
            t0 = time.monotonic() - start
            summary = tb.loadgen(tb.CFG["VIP"], frac * capacities["beP2"], 0, phase_s, extra=CHURN)
            marks.append(dict(phase=label, start=t0, end=time.monotonic() - start, p99=summary["latency_ms"]["p99"]))
            tb.stop_antagonist("beP2")
        mon.send_signal(signal.SIGINT)
        mon.wait()
        s.rec.write(dict(kind="kernelpsi", mode=mode, phases=marks))
    for be in tb.BACKENDS.values():  # back to the normal backend
        tb.stop_backend(be)
        tb.start_backend(be, s.work, slowness=SLOWNESS.get(be.name, 1.0), cost="exp")


def cpu_ticks(cpu):
    """(busy, total) ticks of one CPU from /proc/stat."""
    for line in open("/proc/stat"):
        f = line.split()
        if f[0] == f"cpu{cpu}":
            v = list(map(int, f[1:9]))
            busy = v[0] + v[1] + v[2] + v[5] + v[6] + v[7]
            return busy, busy + v[3] + v[4]


def director_packets():
    stats = {}
    for line in lbctl("stats").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stats[parts[0]] = int(parts[1])
    return sum(stats.get(k, 0) for k in ("pass", "tx", "drop_proto", "drop_no_hop", "drop_error"))


def hook_cost(s, capacities, dry):
    """CPU time per packet on the router/LB CPU with the director at TC vs
    XDP (D35), at two fixed rates. The client computes its own checksums for
    both (D26: XDP needs it), so the only difference is the hook."""
    tb.run(tb.in_ns("cli", ["ethtool", "-K", "eth0", "tx", "off"]), capture_output=True)
    lb_cpu = int(tb.CFG["LB_CPUS"])
    for hook in ("tc", "xdp"):
        print(f"=== hook cost: {hook}", flush=True)
        lbctl("unload")
        lbctl("load", "-hook", hook)
        lbctl("table", weights(capacities))
        for rate in (2000, 4000):
            wait_idle()
            busy0, total0 = cpu_ticks(lb_cpu)
            packets0 = director_packets()
            summary = tb.loadgen(tb.CFG["VIP"], rate, 0, 5 if dry else 20, extra=CHURN)
            busy1, total1 = cpu_ticks(lb_cpu)
            packets = director_packets() - packets0
            ns = (busy1 - busy0) * 1e9 / os.sysconf("SC_CLK_TCK") / packets if packets else None
            s.rec.write(dict(kind="hookcost", hook=hook, rate=rate, packets=packets, busy_ticks=busy1 - busy0,
                             total_ticks=total1 - total0, ns_per_packet=ns, p99=summary["latency_ms"]["p99"]))
            print(f"  {rate} req/s: {packets} packets, LB CPU {ns:.0f} ns per packet" if ns else "  no packets", flush=True)
    lbctl("unload")
    lbctl("load")


def s4(root, dry):
    d, tuning = campaign_inputs(root)
    with Session(root, "s4", dry) as s:
        runs = [("flapping", "oracle", 1), ("flapping", "cpu", 1), ("flapping", "rif", 1), ("sibling", "oracle", 1)]
        if dry:
            runs = runs[1:2] + runs[3:]
        for scenario, config, rep in runs:
            scenario_run(s, scenario, config, rep, d["capacity"], tuning, dry)
        kernel_psi(s, d["capacity"], dry)
        hook_cost(s, d["capacity"], dry)


def s7(root, dry):
    """The final code's own "0 broken": the antagonist scenario with long-lived
    connections after the startup fix, CPU and total RIF in ABBA order. S1-S5
    predate the fix and S6 has no controller run with long-lived connections."""
    d, tuning = campaign_inputs(root)
    runs = [("cpu", 1), ("rif-total", 1), ("rif-total", 2), ("cpu", 2)]
    with Session(root, "s7", dry) as s:
        for config, rep in runs[:2 if dry else 4]:
            scenario_run(s, "antagonist", config, rep, d["capacity"], tuning, dry)


# --- Sessions S8, S9, S10: repeats, checkpointed ---

def finished_units(path):
    """Names of the units a session has finished. A unit that was started but
    not finished (the session was stopped in it) is cut out of the file, so its
    half-written records never reach the analysis and it runs again."""
    if not path.exists():
        return set()
    lines = path.read_text().splitlines()
    done, open_at = set(), None
    for i, line in enumerate(lines):
        r = json.loads(line)
        if r["kind"] == "unit_start":
            open_at = i
        elif r["kind"] == "unit_done":
            done.add(r["name"])
            open_at = None
    if open_at is not None:
        print(f"  dropping the unfinished unit {json.loads(lines[open_at])['name']} ({len(lines) - open_at} records)", flush=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(line + "\n" for line in lines[:open_at]))
        tmp.replace(path)
    return done


BUSY_ABOVE = 0.85  # (a desktop with a browser: 0.15-0.55 in S8)


class MachineBusy(Exception):
    pass


def wait_quiet(s):
    """Stop the session (exit code 75, rerun to continue) if the housekeeping
    CPUs are busy for 30 s before a unit (run_units also drops a unit during
    which they were busy on average). tune.sh confines everything else on the machine to
    them, and our own controller, sigmon and orchestration run there too, so a
    heavy job would delay them and the run would measure that. We stop instead
    of waiting: while the session holds the machine tuned, that job is squeezed
    onto two CPUs and may never finish."""
    cpus = tb.expand_cpus(tb.CFG["HOUSEKEEPING_CPUS"])
    for attempt in range(3):
        before = [cpu_ticks(c) for c in cpus]
        time.sleep(3)
        after = [cpu_ticks(c) for c in cpus]
        busy = sum(a[0] - b[0] for a, b in zip(after, before)) / max(1, sum(a[1] - b[1] for a, b in zip(after, before)))
        if busy <= BUSY_ABOVE:
            return
        time.sleep(9)
    s.rec.write(dict(kind="machine_busy", busy=busy, finished=time.time()))
    raise MachineBusy(f"housekeeping CPUs {busy:.0%} busy")


def run_units(root, name, dry, units):
    """A session as a list of units (name, kind, fn(session)), each one table or
    one scenario run. Finished units are skipped, so a session that was stopped
    (Ctrl-C, SIGTERM, a reboot) is continued by running the same command again.
    Every start of the session sets the machine up and calibrates anew, and
    writes its own calibration record. --dry runs the first unit of each kind."""
    if dry:
        seen = set()
        units = [u for u in units if u[1] not in seen and not seen.add(u[1])]
    done = finished_units(Path(root) / name / "runs.jsonl")
    todo = [u for u in units if u[0] not in done]
    print(f"=== {name}: {len(units) - len(todo)} of {len(units)} units done already", flush=True)
    if not todo:
        return
    with Session(root, name, dry) as s:
        for i, (unit, _, fn) in enumerate(todo, 1):
            if s.tune:
                wait_quiet(s)
            print(f"##### {name} unit {len(units) - len(todo) + i}/{len(units)}: {unit}", flush=True)
            s.rec.write(dict(kind="unit_start", name=unit, started=time.time()))
            cpus = tb.expand_cpus(tb.CFG["HOUSEKEEPING_CPUS"])
            before = [cpu_ticks(c) for c in cpus]
            fn(s)
            after = [cpu_ticks(c) for c in cpus]
            busy = sum(a[0] - b[0] for a, b in zip(after, before)) / max(1, sum(a[1] - b[1] for a, b in zip(after, before)))
            if s.tune and busy > BUSY_ABOVE:  # no unit_done: the unit is cut out and redone
                raise MachineBusy(f"housekeeping CPUs {busy:.0%} busy during {unit}; it will be redone")
            s.rec.write(dict(kind="unit_done", name=unit, finished=time.time(), housekeeping_busy=round(busy, 3)))


def table_rounds(root, dry, session, rounds, extras):
    """RQ1 with repeats: each backend alone once (denominators of this session),
    then rounds of oracle (o), CPU table (c) and total-RIF table (r) in the given
    orders, e.g. {1: "ocr"}; extras: {round: [(static policy, rep)]} after it.
    Same search range for every table (0.85-1.15 x the prediction from its
    shares), as in S6."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]
    policies = {"equal": dict.fromkeys(capacities, 1), "cores": d["cores"], "oracle": capacities}

    def alone(name):
        c = capacities[name]
        return (f"alone-{name}", "alone", lambda s: search(s, tb.BACKENDS[name].ip, 0.8 * c, 1.2 * c,
                                                          experiment="denominator-alone", backend=name, rep=1))

    def static(policy, rep):
        def fn(s):
            lbctl("table", weights(policies[policy]))
            shares = shares_of_table()
            pred = predicted(capacities, shares)
            search(s, tb.CFG["VIP"], 0.85 * pred, 1.15 * pred, experiment="static", policy=policy, rep=rep,
                   shares=shares, predicted=pred)
        return (f"{policy}-{rep}", "static", fn)

    def table(signal_name, rep):
        return (f"ctl-{signal_name}-{rep}", "controller",
                lambda s: controller_table(s, signal_name, rep, tuning, capacities, lo=0.85, hi=1.15))

    make = {"o": lambda n: static("oracle", n), "c": lambda n: table("cpu", n), "r": lambda n: table("rif-total", n)}
    units = [alone(name) for name in capacities]
    for n, order in rounds.items():
        units += [make[x](n) for x in order] + [static(*e) for e in extras.get(n, [])]
    run_units(root, session, dry, units)


def s8(root, dry):
    """RQ1 again, 5 runs each of the oracle, CPU and total-RIF tables,
    interleaved; equal and by-cores twice more."""
    table_rounds(root, dry, "s8", {1: "ocr", 2: "cro", 3: "roc", 4: "orc", 5: "cor"},
                 {2: [("equal", 1), ("cores", 1)], 4: [("cores", 2), ("equal", 2)]})


def scenario_units(root, dry, session, order):
    """Scenario runs (scenario, config, rep) as checkpointed units. The hindsight
    tables use the capacities S6 measured during each disturbance."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]
    disturbed = {r["disturbance"]: r["capacity"] for r in map(json.loads, open(Path(root) / "s6" / "runs.jsonl"))
                 if r["kind"] == "capacity" and r.get("experiment") == "disturbed"}
    hindsight = {"step": {**capacities, "beP2": disturbed["step"]}, "antagonist": {**capacities, "beE1": disturbed["antagonist"]}}

    def run(scenario, config, rep):
        table = hindsight[scenario.partition("@")[0]] if config == "hindsight" else None
        return (f"{scenario}-{config}-{rep}", f"{scenario}-{'static' if config in ('oracle', 'hindsight') else 'ctl'}",
                lambda s: scenario_run(s, scenario, config, rep, capacities, tuning, dry, table=table))

    run_units(root, session, dry, [run(*x) for x in order])


def s9(root, dry):
    """RQ2 again: step and antagonist with the CPU and total-RIF controllers,
    and the two static tables (oracle, hindsight) in the same session."""
    scenario_units(root, dry, "s9", [
        ("step", "oracle", 1), ("step", "cpu", 1), ("step", "rif-total", 1),
        ("antagonist", "cpu", 1), ("antagonist", "rif-total", 1), ("antagonist", "hindsight", 1),
        ("step", "hindsight", 1), ("step", "rif-total", 2), ("step", "cpu", 2),
        ("antagonist", "oracle", 1), ("antagonist", "rif-total", 2), ("antagonist", "cpu", 2),
        ("step", "cpu", 3), ("step", "rif-total", 3)])


def s12(root, dry):
    """S9 once more, the order mirrored: with S9, 5 runs of each controller and
    2 of each static table per scenario, from two sessions."""
    scenario_units(root, dry, "s12", [
        ("antagonist", "oracle", 2), ("antagonist", "cpu", 3), ("antagonist", "rif-total", 3),
        ("step", "rif-total", 4), ("step", "cpu", 4), ("step", "hindsight", 2),
        ("antagonist", "hindsight", 2), ("antagonist", "rif-total", 4), ("antagonist", "cpu", 4),
        ("step", "oracle", 2), ("step", "cpu", 5), ("step", "rif-total", 5),
        ("antagonist", "cpu", 5), ("antagonist", "rif-total", 5)])


def s13(root, dry):
    """The dynamic scenarios at other loads than 75% (every earlier run): the
    antagonist at 60% and 85% of the summed capacities, the step at 60% (at 85%
    the step leaves less capacity than the load, so no table could hold).
    One run of each: oracle, hindsight, CPU, total RIF."""
    order = []
    for scenario, configs in (("antagonist@60", ("oracle", "cpu", "rif-total", "hindsight")),
                              ("antagonist@85", ("rif-total", "hindsight", "oracle", "cpu")),
                              ("step@60", ("cpu", "oracle", "hindsight", "rif-total"))):
        order += [(scenario, config, 1) for config in configs]
    scenario_units(root, dry, "s13", order)


def hook_cost_rep(s, capacities, dry, rep, hooks):
    """One repeat of the hook comparison (see hook_cost), 40 s per rate, hooks
    in the given order; afterwards the normal LB is loaded again."""
    tb.run(tb.in_ns("cli", ["ethtool", "-K", "eth0", "tx", "off"]), capture_output=True)
    lb_cpu = int(tb.CFG["LB_CPUS"])
    seconds = 5 if dry else 40
    try:
        for hook in hooks:
            print(f"=== hook cost {rep}: {hook}", flush=True)
            lbctl("unload")
            lbctl("load", "-hook", hook)
            lbctl("table", weights(capacities))
            for rate in (2000, 4000):
                wait_idle()
                busy0, total0 = cpu_ticks(lb_cpu)
                packets0 = director_packets()
                summary = tb.loadgen(tb.CFG["VIP"], rate, 0, seconds, extra=CHURN)
                busy1, total1 = cpu_ticks(lb_cpu)
                packets = director_packets() - packets0
                ns = (busy1 - busy0) * 1e9 / os.sysconf("SC_CLK_TCK") / packets if packets else None
                s.rec.write(dict(kind="hookcost", hook=hook, rate=rate, rep=rep, seconds=seconds, packets=packets,
                                 busy_ticks=busy1 - busy0, total_ticks=total1 - total0, ns_per_packet=ns,
                                 p99=summary["latency_ms"]["p99"], errors=summary["errors"]))
                print(f"  {rate} req/s: {packets} packets, LB CPU {ns:.0f} ns per packet" if ns else "  no packets", flush=True)
    finally:
        subprocess.run(tb.in_ns("cli", ["ethtool", "-K", "eth0", "tx", "on"]), capture_output=True)
        tb.lb_unload()
        tb.lb_load()
        lbctl("table", weights(capacities))


def s10(root, dry):
    """Repeats of the single-run items, and one new point: the hook comparison
    4 more times with longer runs and alternating order; the cgroup-CPU
    controller under the antagonist twice; a neighbour that flaps faster than the
    control period and the connection lifetime (1 s on, 1 s off: H3's second
    half); the redirector contrast twice, last, each followed by fresh backends
    (its broken connections leave orphaned sockets that send keepalives for minutes)."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]

    def hook(rep, hooks):
        return (f"hook-{rep}", "hook", lambda s: hook_cost_rep(s, capacities, dry, rep, hooks))

    def run(scenario, config, rep):
        return (f"{scenario}-{config}-{rep}", f"{scenario.split('-')[0]}-{'static' if config == 'oracle' else config}",
                lambda s: scenario_run(s, scenario, config, rep, capacities, tuning, dry))

    def no_forward(rep):
        def fn(s):
            scenario_run(s, "antagonist", "cpu-noredirect", rep, capacities, tuning, dry)
            for be in tb.BACKENDS.values():
                tb.stop_backend(be)
                tb.start_backend(be, s.work, slowness=slowness(be), cost="exp")
        return (f"antagonist-cpu-noredirect-{rep}", "noredirect", fn)

    units = [hook(1, ("tc", "xdp")), run("antagonist", "cgroup", 1), run("fastflap-2", "oracle", 1),
             hook(2, ("xdp", "tc")), run("fastflap-2", "cpu", 1), run("fastflap-2", "rif-total", 1),
             run("antagonist", "cgroup", 2), hook(3, ("tc", "xdp")), run("fastflap-2", "rif-total", 2),
             run("fastflap-2", "cpu", 2), hook(4, ("xdp", "tc")), no_forward(1), no_forward(2)]
    run_units(root, "s10", dry, units)


FIXED_LOADS = (0.8, 0.9)  # S14: fractions of the summed capacities


def s14(root, dry):
    """A second measure of RQ1 that needs no threshold search: p99 of each table
    at two fixed loads, 60 s each, 5 rounds, interleaved (a search's result hangs
    on single 20-s probes near the SLO, where p99 isn't monotonic). And the gap
    between the oracle through the LB and the same shares sent directly, 3 more
    pairs (S6 had 2)."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]
    total = sum(capacities.values())
    names = list(capacities)
    policies = {"cores": d["cores"], "oracle": capacities}

    def fixed(policy, n):
        def fn(s):
            if policy in policies:
                lbctl("table", weights(policies[policy]))
                shares, invalid = shares_of_table(), {}
            else:
                shares, _, invalid = converge(s, policy[4:], n, tuning, capacities)
            for load in FIXED_LOADS if n % 2 else FIXED_LOADS[::-1]:
                waited = wait_idle()
                summary = tb.loadgen(tb.CFG["VIP"], load * total, P["warmup"], 6 if dry else 60, extra=CHURN)
                s.rec.write(dict(kind="fixed", policy=policy, rep=n, load=load, rate=load * total, shares=shares,
                                 meets_slo=meets_slo(summary), summary=summary, idle_wait_s=round(waited, 2),
                                 finished=time.time(), **invalid))
                print(f"    {policy} at {load:.0%}: p99 {summary['latency_ms']['p99']:.2f} ms, errors {summary['error_rate']:.4f}", flush=True)
                time.sleep(P["rest"])
        return (f"fixed-{policy}-{n}", "fixed-ctl" if policy.startswith("ctl-") else "fixed-static", fn)

    def oracle(n):
        def fn(s):
            lbctl("table", weights(capacities))
            shares = shares_of_table()
            pred = predicted(capacities, shares)
            search(s, tb.CFG["VIP"], 0.85 * pred, 1.15 * pred, experiment="static", policy="oracle", rep=n,
                   shares=shares, predicted=pred)
        return (f"oracle-{n}", "search-oracle", fn)

    def direct(n):
        return (f"direct-{n}", "search-direct",
                lambda s: search(s, tb.BACKENDS[names[0]].ip, 0.85 * total, 1.15 * total,
                                 targets=lambda r: {b: r * capacities[b] / total for b in names},
                                 experiment="direct-mix", policy="direct", rep=n))

    orders = {1: ("oracle", "ctl-cpu", "ctl-rif-total", "cores"), 2: ("ctl-rif-total", "cores", "oracle", "ctl-cpu"),
              3: ("cores", "ctl-cpu", "ctl-rif-total", "oracle"), 4: ("ctl-cpu", "oracle", "cores", "ctl-rif-total"),
              5: ("ctl-rif-total", "oracle", "ctl-cpu", "cores")}
    units = []
    for n, order in orders.items():
        units += [fixed(policy, n) for policy in order]
        if n <= 3:
            units += [oracle(n), direct(n)] if n % 2 else [direct(n), oracle(n)]
    run_units(root, "s14", dry, units)


def s15(root, dry):
    """Sensitivity and overhead: the antagonist with the CPU controller at gain
    0.1, 0.3 (ours) and 1, twice each; the no-protection controller twice more
    (S3 had 1 run); and latency through the LB against the same shares sent
    directly, at 50% and 80% load, 5 pairs of 30 s."""
    d, tuning = campaign_inputs(root)
    capacities = d["capacity"]
    total = sum(capacities.values())
    names = list(capacities)

    def run(config, n):
        return (f"antagonist-{config}-{n}", f"antagonist-{config}",
                lambda s: scenario_run(s, "antagonist", config, n, capacities, tuning, dry))

    def overhead(n):
        def fn(s):
            lbctl("table", weights(capacities))
            for load in (0.5, 0.8):
                for path in ("lb", "direct") if n % 2 else ("direct", "lb"):
                    wait_idle()
                    rates = {b: load * capacities[b] for b in names}
                    target, extra = (tb.CFG["VIP"], CHURN) if path == "lb" else (tb.BACKENDS[names[0]].ip, CHURN + direct_targets(rates))
                    summary = tb.loadgen(target, load * total, P["warmup"], 5 if dry else 30, extra=extra)
                    summary.pop("per_server", None)
                    s.rec.write(dict(kind="overhead", path=path, rep=n, load=load, summary=summary, finished=time.time()))
                    print(f"    {path} at {load:.0%}: p50 {summary['latency_ms']['p50']:.3f} ms, p99 {summary['latency_ms']['p99']:.2f} ms", flush=True)
        return (f"overhead-{n}", "overhead", fn)

    units = [overhead(1), run("cpu-gain0.1", 1), run("cpu", 1), run("cpu-gain1", 1), overhead(2), run("cpu-noprotect", 1),
             overhead(3), run("cpu-gain1", 2), run("cpu", 2), run("cpu-gain0.1", 2), overhead(4), run("cpu-noprotect", 2),
             overhead(5)]
    run_units(root, "s15", dry, units)


def stopped(*_):
    raise KeyboardInterrupt  # so that the session's cleanup runs, as for Ctrl-C


def main():
    global P
    ap = argparse.ArgumentParser()
    ap.add_argument("session", choices=["s1", "s2", "s2-extra", "s2-rif-total", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s12", "s13", "s14", "s15"])
    ap.add_argument("--root", required=True)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--noise-only", action="store_true", help="s1: keep the denominators, redo the noise runs")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGHUP, stopped)
    if args.dry:
        P = DRY
    if args.session == "s1":
        s1(args.root, args.dry, args.noise_only)
        return
    try:
        {"s2": s2, "s2-extra": s2_extra, "s2-rif-total": s2_rif_total, "s3": s3, "s4": s4, "s5": s5,
         "s6": s6, "s7": s7, "s8": s8, "s9": s9, "s10": s10, "s12": s12, "s13": s13, "s14": s14, "s15": s15}[args.session](args.root, args.dry)
    except MachineBusy as e:
        print(f"stopped: {e}. Run the same command again when the machine is quiet.", flush=True)
        sys.exit(75)


if __name__ == "__main__":
    main()
