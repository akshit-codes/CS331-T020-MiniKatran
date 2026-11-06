#!/usr/bin/env bash
# Runtime-only system tweaks for measurement runs, and their exact undo.
#
#   sudo scripts/tune.sh apply    before a measurement run
#   sudo scripts/tune.sh undo     after it (safe to run any time; no-op if nothing applied)
#        scripts/tune.sh status   show the current values
#
# What "apply" changes (nothing is written to disk, so a reboot also undoes it):
#   - turbo off                      (clock speed no longer depends on load or temperature);
#                                      intel_pstate, amd-pstate or acpi-cpufreq, see turbo_off
#   - cpufreq governor "performance" (no frequency ramping between requests)
#   - deep idle states off on the experiment CPUs (all but housekeeping): waking
#     from them takes up to ~1 ms on this laptop and flushes the caches, which
#     made requests *slower at low load*. Shallow C1 (1 us) stays allowed.
#   - fixed clock on the experiment CPUs (min frequency = max frequency)
#   - user.slice, system.slice and init.scope confined to the housekeeping CPUs
#     (the desktop, VS Code, browsers etc. stop competing with the experiment)
#   - device interrupts moved to the housekeeping CPUs
#   - irqbalance stopped, if it runs (it would move the interrupts back)
#
# Every original value is recorded in $STATE *before* it is changed, one line
# per change, so "undo" restores exactly what was there, even after a partial apply.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

STATE=/run/cs331-lb/tune.state    # /run is a tmpfs: it disappears at reboot, like the tweaks
UNITS=(user.slice system.slice init.scope)
SYS=/sys/devices/system/cpu
CPUFREQ=$SYS/cpufreq
MAX_IDLE_EXIT_US=10    # idle states that take longer than this to wake from are disabled

record() { printf '%s\t%s\t%s\n' "$1" "$2" "$3" >> "$STATE"; }   # record <kind> <key> <original value>

set_file() {   # set_file <path> <new value>: remember the old value, then write the new one
    local old; old=$(cat "$1")
    [[ "$old" == "$2" ]] && return 0
    record file "$1" "$old"
    echo "$2" > "$1"
}

# The turbo switch depends on the cpufreq driver: intel_pstate has no_turbo
# (1 = off); acpi-cpufreq and amd-pstate have boost (0 = off), for all CPUs or,
# on newer kernels, per policy; some 6.11 kernels had amd_pstate/cpb_boost.
turbo_files() {
    if [[ -e $SYS/intel_pstate/no_turbo ]]; then echo "$SYS/intel_pstate/no_turbo 1"
    elif [[ -e $CPUFREQ/boost ]]; then echo "$CPUFREQ/boost 0"
    elif [[ -e $SYS/amd_pstate/cpb_boost ]]; then echo "$SYS/amd_pstate/cpb_boost 0"
    else for f in "$CPUFREQ"/policy*/boost; do [[ -e "$f" ]] && echo "$f 0"; done
    fi
    return 0
}

turbo_off() {
    local found=0 f off
    while read -r f off; do set_file "$f" "$off"; found=1; done < <(turbo_files)
    (( found )) || echo "warning: no turbo switch found (driver $(cat "$CPUFREQ"/policy0/scaling_driver)); clocks may follow load" >&2
}

apply() {
    need_root
    [[ -e "$STATE" ]] && die "already applied ($STATE exists); run 'undo' first"
    mkdir -p "$(dirname "$STATE")"
    : > "$STATE"

    turbo_off
    for policy in "$CPUFREQ"/policy*; do
        # EPP (intel_pstate, amd-pstate in active mode; not acpi-cpufreq) can't
        # be written while the governor is "performance", so we only record it
        # here; undo restores the governor first, then EPP.
        local epp=$policy/energy_performance_preference
        [[ -e "$epp" ]] && record file "$epp" "$(cat "$epp")"
        set_file "$policy/scaling_governor" performance
    done

    local housekeeping=" $(cpulist_expand "$HOUSEKEEPING_CPUS" | xargs) " deep=0
    for cpu in "$SYS"/cpu[0-9]*; do
        [[ "$housekeeping" == *" ${cpu##*cpu} "* ]] && continue
        # Fixed clock: min = max (= base clock now that turbo is off). Otherwise
        # the hardware still lowers the clock of a lightly loaded P-core and
        # requests get slower at low load.
        set_file "$cpu/cpufreq/scaling_min_freq" "$(cat "$cpu/cpufreq/scaling_max_freq")"
        for state in "$cpu"/cpuidle/state*; do
            [[ -e "$state/latency" ]] || continue
            if (( $(cat "$state/latency") > MAX_IDLE_EXIT_US )); then
                set_file "$state/disable" 1
                deep=$((deep + 1))
            fi
        done
    done

    for unit in "${UNITS[@]}"; do
        record allowedcpus "$unit" "$(systemctl show -p AllowedCPUs --value "$unit")"
        systemctl set-property --runtime "$unit" AllowedCPUs="$HOUSEKEEPING_CPUS"
    done

    if systemctl is-active -q irqbalance 2>/dev/null; then
        record service irqbalance active
        systemctl stop irqbalance
    fi
    local moved=0
    for f in /proc/irq/*/smp_affinity_list; do
        local old; old=$(cat "$f")
        [[ "$old" == "$HOUSEKEEPING_CPUS" ]] && continue
        # Some interrupts are kernel-managed and refuse to move; that's fine.
        if echo "$HOUSEKEEPING_CPUS" > "$f" 2>/dev/null; then record file "$f" "$old"; moved=$((moved + 1)); fi
    done

    echo "applied (disabled $deep deep idle states, moved $moved interrupts); undo with: sudo $0 undo"
    warnings
}

undo() {
    need_root
    [[ -e "$STATE" ]] || { echo "nothing to undo"; return 0; }
    # Restore in reverse order of the changes.
    tac "$STATE" | while IFS=$'\t' read -r kind key value; do
        case "$kind" in
            file) echo "$value" > "$key" 2>/dev/null || echo "warning: could not restore $key" >&2 ;;
            allowedcpus) systemctl set-property --runtime "$key" AllowedCPUs="$value" ;;
            service) systemctl start "$key" ;;
        esac
    done
    rm -f "$STATE"
    echo "undone"
}

warnings() {
    local profile
    profile=$(powerprofilesctl get 2>/dev/null || echo unknown)
    [[ "$profile" == performance ]] || echo "warning: power profile is '$profile', not 'performance'" >&2
    for ac in /sys/class/power_supply/{AC,ADP,ACAD}*/online; do
        [[ -e "$ac" && "$(cat "$ac")" == 0 ]] && echo "warning: running on battery" >&2
    done
    return 0
}

status() {
    echo "applied:     $([[ -e "$STATE" ]] && echo "yes ($(wc -l < "$STATE") changes recorded)" || echo no)"
    echo "driver:      $(cat "$CPUFREQ"/policy*/scaling_driver | sort -u | xargs)"
    echo "turbo:       $(turbo_files | while read -r f off; do printf "%s=%s (off: %s) " "${f#"$SYS"/}" "$(cat "$f")" "$off"; done)"
    echo "governors:   $(cat "$CPUFREQ"/policy*/scaling_governor | sort | uniq -c | xargs)"
    echo "epp:         $(cat "$CPUFREQ"/policy*/energy_performance_preference 2>/dev/null | sort | uniq -c | xargs)"
    echo "idle states disabled: $(cat "$SYS"/cpu*/cpuidle/state*/disable 2>/dev/null | grep -c 1)"
    for unit in "${UNITS[@]}"; do echo "$unit: AllowedCPUs=$(systemctl show -p AllowedCPUs --value "$unit")"; done
    echo "power:       $(powerprofilesctl get 2>/dev/null || echo unknown)"
}

case "${1:-}" in
    apply) apply ;; undo) undo ;; status) status ;;
    *) echo "usage: $0 apply|undo|status" >&2; exit 1 ;;
esac
