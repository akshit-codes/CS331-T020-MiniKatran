#!/usr/bin/env python3
"""Live demo: do connections survive a big table change? (RQ4)

300 long-lived connections through the LB, and half of the buckets get a new
first hop while they run: beP1 loses all its buckets. Twice:
  A. with second hops: beP1 keeps serving its open connections, nothing breaks;
  B. without second hops: those connections hit a server with no socket for
     them and break.
It counts broken connections, not latency, so it needs no tune.sh and a busy
desktop doesn't change the outcome. It is a demo, not a measurement.

Brings up the testbed, cleans up after itself (also on Ctrl-C), and exits 1 if
the numbers aren't as expected, so a rehearsal can check them with $?. Takes
about 40 s without pauses. If the numbers are wrong or it hangs on stage,
switch to the recording: don't debug it live.

Usage: make && sudo python3 scripts/demo.py [--no-pause]
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
import testbed as tb

WORK = 20000            # ~0.2 ms per request on a P-core: the backends stay far from busy
CONNS, RATE = 300, 3000 # as in bench/move_test.py: each connection sends about every 100 ms
WARMUP, MOVE_AT, AFTER = 2, 8, 8  # seconds: warm-up, then the change, then how long we keep watching
NEW_TABLE = "beP1=0,beP2=1,beE1=1,beE2=1"
GIVE_UP_AFTER = 20      # seconds past its planned end before a load generator counts as hung

GREEN, RED, BOLD, OFF = ("\033[32m", "\033[31m", "\033[1m", "\033[0m") if sys.stdout.isatty() else ("", "", "", "")
DIGITS = {  # 3 x 5 pixels, each two characters wide
    "0": ["###", "# #", "# #", "# #", "###"], "1": [" # ", "## ", " # ", " # ", "###"],
    "2": ["###", "  #", "###", "#  ", "###"], "3": ["###", "  #", "###", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"], "9": ["###", "# #", "###", "  #", "###"],
    "/": ["  #", "  #", " # ", "#  ", "#  "], " ": ["   "] * 5,
}


def big(text, color=""):
    """Print text in large block digits, or plainly if the terminal is too narrow for them."""
    if len(text) * 8 > shutil.get_terminal_size().columns:
        print(f"{color}{BOLD}{text}{OFF}")
        return
    for row in range(5):
        line = "  ".join(DIGITS[c][row].replace("#", "██").replace(" ", "  ") for c in text)
        print(f"{color}{line}{OFF}")


def heading(text):
    print(f"\n{BOLD}{text}{OFF}", flush=True)


def lbctl(*args):
    return subprocess.run([str(tb.BIN / "lbctl"), *args], cwd=tb.REPO, capture_output=True, text=True, check=True).stdout


def first_hops():
    return [line.split()[1] for line in lbctl("dump").splitlines() if not line.startswith("#")]


def director_packets():
    """Packets the director has seen: the sum of its per-backend counters."""
    lines = lbctl("stats").splitlines()
    start = lines.index("packets sent per backend:") + 1
    return sum(int(line.split()[1]) for line in lines[start:start + len(tb.BACKENDS)])


def hopped_packets():
    """Packets that reached a backend through its second-hop neighbour: the sum of keep_hopped."""
    lines = lbctl("stats").splitlines()
    header = lines.index("redirectors:") + 1
    column = lines[header].split().index("keep_hopped") + 1
    return sum(int(line.split()[column]) for line in lines[header + 1:header + 1 + len(tb.BACKENDS)])


def requests_served():
    return {name: tb.backend_metrics(be)["requests_total"] for name, be in tb.BACKENDS.items()}


def setup():
    heading("Starting the testbed: a client, a router, the LB and 4 backends, in namespaces")
    subprocess.run([str(tb.REPO / "scripts/topology.sh"), "up"], capture_output=True, text=True, check=True)
    with contextlib.redirect_stdout(io.StringIO()):  # the helpers print their commands
        for be in tb.BACKENDS.values():
            tb.start_backend(be, WORK)
    lbctl("load")
    lbctl("table", "equal")


def cleanup():
    print("\nCleaning up...", flush=True)
    for be in tb.BACKENDS.values():
        tb.stop_backend(be)
    subprocess.run([str(tb.BIN / "lbctl"), "unload"], cwd=tb.REPO, capture_output=True)
    subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"], capture_output=True)
    left = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout.strip()
    print("Testbed removed." if not left else f"Namespaces still there: {left}")


def one_request():
    heading("One request through the load balancer")
    served, packets = requests_served(), director_packets()
    out = subprocess.run(tb.in_ns("cli", ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5",
                                          f"http://{tb.CFG['VIP']}:{tb.CFG['BACKEND_PORT']}/work"]),
                         capture_output=True, text=True).stdout
    if out != "200":
        raise RuntimeError(f"a request through the LB returned {out or 'nothing'}")
    time.sleep(0.2)  # the client's last packets
    now = requests_served()
    who = [name for name in now if now[name] > served[name]]
    print(f"  client asks the LB's address {tb.CFG['VIP']}: HTTP {out}, answered by {', '.join(who)}")
    print(f"  the LB saw {director_packets() - packets} packets, all client to server: replies skip it")


def move_run(title, second):
    """Long-lived connections through a table change; returns the broken count."""
    heading(title)
    lbctl("table", "equal")
    time.sleep(1)  # the previous run's connections have closed
    before_table = first_hops()
    hopped = hopped_packets()
    loadgen = subprocess.Popen(tb.loadgen_command(tb.CFG["VIP"], RATE, WARMUP, MOVE_AT + AFTER, ["-conns", str(CONNS)]),
                               stdout=subprocess.PIPE, text=True, cwd=tb.REPO)
    try:
        start = time.monotonic()
        print(f"  {CONNS} connections open. Requests served per second:")
        print("  " + f"{'t':>4}" + "".join(f"{name:>7}" for name in tb.BACKENDS))
        last = requests_served()
        moved = None
        for t in range(1, WARMUP + MOVE_AT + AFTER):
            time.sleep(max(0, start + t - time.monotonic()))
            now = requests_served()
            print("  " + f"{t:>3}s" + "".join(f"{now[n] - last[n]:>7.0f}" for n in now), flush=True)
            last = now
            if t == WARMUP + MOVE_AT:
                lbctl("table", "-second", second, NEW_TABLE)
                moved = sum(a != b for a, b in zip(before_table, first_hops()))
                print(f"  {BOLD}--- table change: {moved} of {len(before_table)} buckets get a new first hop, "
                      f"beP1 has none left ---{OFF}", flush=True)
        try:
            summary = json.loads(loadgen.communicate(timeout=GIVE_UP_AFTER)[0])
        except subprocess.TimeoutExpired:
            raise RuntimeError("the load generator hung")
    finally:  # also when interrupted: it would run on after the testbed is gone
        loadgen.kill()
    broken = summary["errors"]
    print(f"\n  {summary['requests']} requests, {hopped_packets() - hopped} packets reached their old server "
          "through the second hop")
    print(f"  {BOLD}connections broken, of {CONNS}:{OFF}")
    big(f"{broken} / {CONNS}", GREEN if broken == 0 else RED)
    return broken


def main():
    pause = sys.stdin.isatty() and "--no-pause" not in sys.argv
    if os.geteuid() != 0:
        sys.exit("run it with sudo")
    if not (tb.BIN / "lbctl").exists():
        sys.exit("build it first: make")
    if subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout.strip():
        sys.exit("a testbed is already up. If nothing is using it: sudo scripts/topology.sh down")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))

    def wait():
        if pause:
            input("\n  (Enter to go on) ")

    began = time.monotonic()
    code = 1
    try:
        setup()
        one_request()
        wait()
        with_hops = move_run("A. The table changes, WITH second hops", "previous")
        wait()
        without = move_run("B. The same change, WITHOUT second hops", "none")
        heading("Connections broken by the same table change")
        print(f"  {BOLD}with second hops{OFF}")
        big(f"{with_hops}", GREEN if with_hops == 0 else RED)
        print(f"  {BOLD}without{OFF}")
        big(f"{without}", RED)
        if with_hops == 0 and without > 0:
            code = 0
        else:
            print(f"\n{RED}Not what we expected (0, then more than 0): use the recording.{OFF}")
    except (subprocess.CalledProcessError, RuntimeError, ValueError, KeyError) as e:
        print(f"\n{RED}The demo failed: {e}\nUse the recording.{OFF}")
        if getattr(e, "stderr", None):
            print(e.stderr.strip()[-500:])
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cleanup()
    print(f"Took {time.monotonic() - began:.0f} s.")
    sys.exit(code)


if __name__ == "__main__":
    main()
