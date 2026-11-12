# Shared helpers for the shell scripts. Source it, don't run it.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$REPO/config.env"
# On a cluster machine (bench/cluster.py) the housekeeping CPUs are that machine's.
HOUSEKEEPING_CPUS=${CS331_HOUSEKEEPING_CPUS:-$HOUSEKEEPING_CPUS}

# Backends from backends.conf, as parallel arrays.
BE_NAMES=() BE_IPS=() BE_CPUS=() BE_KINDS=()
while read -r name ip cpus kind; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    BE_NAMES+=("$name") BE_IPS+=("$ip") BE_CPUS+=("$cpus") BE_KINDS+=("$kind")
done < "$REPO/backends.conf"

NAMESPACES=(cli rtr lb "${BE_NAMES[@]}")

die() { echo "error: $*" >&2; exit 1; }
need_root() { [[ $EUID -eq 0 ]] || die "run with sudo"; }

# "8,10" or "16-19" -> one CPU number per line.
cpulist_expand() {
    local part lo hi c
    IFS=',' read -ra parts <<< "$1"
    for part in "${parts[@]}"; do
        if [[ "$part" == *-* ]]; then lo=${part%-*} hi=${part#*-}; else lo=$part hi=$part; fi
        for ((c = lo; c <= hi; c++)); do echo "$c"; done
    done
}

# "8,10" or "16-19" -> hex bitmask for sysfs (e.g. rps_cpus).
cpulist_to_mask() {
    local mask=0 c
    for c in $(cpulist_expand "$1"); do mask=$((mask | (1 << c))); done
    printf '%x' "$mask"
}
