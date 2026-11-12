"""Does a request cost the same at low and high load? (It should, if the machine is tuned.)

For one P and one E backend, at a few fixed rates, compares:
  - server time per request: Δ rif_seconds_total / Δ requests_total (Little's law)
  - client latency p50/p99, and the client's own share (send delay, dispatch lag)
Before tuning, idle-state wake-ups and clock ramping made requests slower at
*low* load; see docs/journal.md (2026-11-01).

Usage: sudo python3 bench/service_time.py [--no-tune]
"""

import subprocess
import sys

import testbed as tb

RATES = (20, 200, 800)


def main():
    tune = "--no-tune" not in sys.argv
    try:
        if tune:
            tb.run([str(tb.REPO / "scripts/tune.sh"), "apply"])
        tb.run([str(tb.REPO / "scripts/topology.sh"), "up"])
        p_units, e_units = tb.calibrate(8), tb.calibrate(16)
        work = round(p_units)  # 1 ms on a P-core
        print(f"\ncalibration: P {p_units:.0f} units/ms, E {e_units:.0f} units/ms "
              f"-> expected server time P 1.000 ms, E {p_units / e_units:.3f} ms\n")
        for name in ("beP1", "beE1"):
            be = tb.BACKENDS[name]
            tb.start_backend(be, work)
            for rate in RATES:
                before = tb.backend_metrics(be)
                s = tb.loadgen(be.ip, rate, warmup=2, duration=10, extra=["-arrival", "fixed"])
                after = tb.backend_metrics(be)
                server_ms = 1000 * ((after["rif_seconds_total"] - before["rif_seconds_total"])
                                    / (after["requests_total"] - before["requests_total"]))
                print(f"{name} {rate:4d} req/s: server {server_ms:.3f} ms | client p50 {s['latency_ms']['p50']:.3f} "
                      f"p99 {s['latency_ms']['p99']:.3f} ms | send delay p50 {s['send_delay_ms']['p50']:.3f} | "
                      f"dispatch lag p99 {s['dispatch_lag_ms']['p99']:.3f} ms", flush=True)
            tb.stop_backend(be)
    finally:
        subprocess.run([str(tb.REPO / "scripts/topology.sh"), "down"])
        if tune:
            subprocess.run([str(tb.REPO / "scripts/tune.sh"), "undo"])


if __name__ == "__main__":
    main()
