"""Sample machine state once per second into a CSV until stopped (SIGTERM or Ctrl-C).

Columns: unix time, package temperature (C), package power (W), cumulative
thermal-throttle counters, then per CPU: busy %, softirq % (where packet
processing ran) and MHz. Used to spot runs disturbed by throttling and to
check that RPS put packet processing on the intended CPUs.
Run as root: the RAPL energy counter is root-only.

Usage: sudo python3 bench/sysmon.py out.csv
"""

import csv
import glob
import os
import signal
import sys
import time

NCPU = os.cpu_count()
RAPL = "/sys/class/powercap/intel-rapl:0"


def read_int(path, default=0):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return default


def cpu_times():
    """Per CPU: (busy, softirq, total) in clock ticks since boot."""
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and line[3].isdigit():
                fields = line.split()
                user, nice, system, idle, iowait, irq, softirq, steal = map(int, fields[1:9])
                total = user + nice + system + idle + iowait + irq + softirq + steal
                out[int(fields[0][3:])] = (total - idle - iowait, softirq, total)
    return out


def coretemp_path():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        with open(d + "/name") as f:
            if f.read().strip() == "coretemp":
                return d + "/temp1_input"  # temp1 = whole package
    return None


def throttle_counts():
    base = "/sys/devices/system/cpu/cpu{}/thermal_throttle/{}"
    package = read_int(base.format(0, "package_throttle_count"))
    core = sum(read_int(base.format(c, "core_throttle_count")) for c in range(NCPU))
    return package, core


def main(path):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    temp_path = coretemp_path()
    energy_max = read_int(RAPL + "/max_energy_range_uj", 1 << 62)

    with open(path, "w", newline="") as f:
        out = csv.writer(f)
        out.writerow(["time", "temp_c", "watts", "pkg_throttle", "core_throttle"]
                     + [f"busy{c}" for c in range(NCPU)]
                     + [f"soft{c}" for c in range(NCPU)]
                     + [f"mhz{c}" for c in range(NCPU)])
        prev_times, prev_energy = cpu_times(), read_int(RAPL + "/energy_uj")
        prev_t = time.time()
        try:
            while True:
                time.sleep(1)
                now, times, energy = time.time(), cpu_times(), read_int(RAPL + "/energy_uj")
                busy, soft = [], []
                for c in range(NCPU):
                    d_busy, d_soft, d_total = (a - b for a, b in zip(times[c], prev_times[c]))
                    busy.append(round(100 * d_busy / max(d_total, 1), 1))
                    soft.append(round(100 * d_soft / max(d_total, 1), 1))
                watts = ((energy - prev_energy) % energy_max) / 1e6 / (now - prev_t)
                mhz = [read_int(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq") // 1000
                       for c in range(NCPU)]
                temp = read_int(temp_path) / 1000 if temp_path else ""
                out.writerow([round(now, 1), temp, round(watts, 1), *throttle_counts()] + busy + soft + mhz)
                f.flush()
                prev_times, prev_energy, prev_t = times, energy, now
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main(sys.argv[1])
