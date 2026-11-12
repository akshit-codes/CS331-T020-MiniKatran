#!/usr/bin/env bash
# The repeat sessions S8-S15 (no S11) (docs/campaign.md), one after the other, checkpointed:
# run it again after a stop (Ctrl-C, a reboot) and it continues with the runs that are left.
# It waits while other work keeps the machine busy, also between runs.
#
#   sudo bench/repeats.sh results/campaign-20261102 [session ...]     (default: s8 s9 s10 s14 s12 s13 s15)
set -u
cd "$(dirname "$0")/.."
ROOT=${1:?usage: sudo bench/repeats.sh results/campaign-<date> [session ...]}
shift
(( $# )) || set -- s8 s9 s10 s14 s12 s13 s15

busy() {   # share of all CPUs' time spent busy over 10 s, in percent
    python3 - <<'PY'
import time
def snap():
    v = list(map(int, open("/proc/stat").readline().split()[1:9]))
    b = v[0] + v[1] + v[2] + v[5] + v[6] + v[7]
    return b, b + v[3] + v[4]
a = snap(); time.sleep(10); b = snap()
print(round(100 * (b[0] - a[0]) / max(1, b[1] - a[1])))
PY
}

wait_quiet() {   # 3 minutes of quiet samples in a row: a job that runs in batches has short gaps
    local quiet=0 said=0
    while (( quiet < 18 )); do
        local now; now=$(busy)
        if (( now <= 8 )); then quiet=$((quiet + 1)); else
            quiet=0
            (( said % 30 == 0 )) && echo "$(date +%T) machine ${now}% busy: waiting"
            said=$((said + 1))
        fi
    done
}

for session in "$@"; do
    while true; do
        wait_quiet
        echo "$(date +%T) === $session"
        python3 bench/campaign.py "$session" --root "$ROOT" >> "$ROOT/$session.log" 2>&1
        code=$?
        [[ $code == 75 ]] && continue   # the machine got busy: wait and go on
        [[ $code == 0 ]] || { echo "$session failed (exit $code): see $ROOT/$session.log"; exit $code; }
        break
    done
done
echo "$(date +%T) all sessions done"
