#!/usr/bin/env bash
# Prepare one machine of the cluster for its role, or undo it (docs/cluster-runbook.md).
#
#   sudo CS331_CLUSTER=cluster.conf scripts/cluster/setup.sh up NAME    (NAME: this machine in cluster.conf)
#   sudo CS331_CLUSTER=cluster.conf scripts/cluster/setup.sh down NAME
#
# The role comes from cluster.conf: CLIENT=NAME, LB=NAME, or a machine with
# backends. Everything is runtime-only; every changed value is recorded in
# /run/cs331-lb/cluster-NAME.state and restored by `down`, like scripts/tune.sh.
#
#   all:     wifi power save off, rp_filter 0 (replies come from the VIP, which
#            isn't this network's), warn about firewalls that drop forwarding
#   client:  route to the VIP via the LB with a lower MTU (our encapsulation
#            adds 40 bytes and wifi can't raise its MTU), TIME_WAIT reuse
#   backend: a namespace per backend behind a bridge (like the namespace
#            testbed), forwarding on, routes to the other machines' backends
#   client, lb, backend: routes to every other machine's backends
#
# Emulation (TRANSPORT=netns): the machine is the namespace CS331_HOST_NETNS;
# host-level commands run inside it, namespaces are created from outside.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
conf="$REPO/${CS331_CLUSTER:?set CS331_CLUSTER to the cluster.conf}"
action=${1:?up or down}
me=${2:?this machine\'s name in cluster.conf}
state=/run/cs331-lb/cluster-$me.state  # per machine: emulated machines share /run
H=()  # prefix for commands on this machine (emulation: inside its namespace)
[[ -n "${CS331_HOST_NETNS:-}" ]] && H=(ip netns exec "$CS331_HOST_NETNS")

die() { echo "setup.sh: $*" >&2; exit 1; }

# --- read cluster.conf ---
declare -A addr iface value
backends=()  # "name host ip cpus" lines
while read -r line; do
    line=${line%%#*}
    read -r -a f <<< "$line"
    [[ ${#f[@]} -eq 0 ]] && continue
    case "${f[0]}" in
        host) addr[${f[1]}]=${f[2]}; iface[${f[1]}]=${f[3]} ;;
        backend) backends+=("${f[1]} ${f[2]} ${f[3]} ${f[4]}") ;;
        *=*) value[${line%%=*}]=$(echo "${line#*=}" | xargs) ;;
    esac
done < "$conf"
[[ -n "${addr[$me]:-}" ]] || die "no host $me in $conf"
dev=${iface[$me]}

cpulist_expand() {                     # "8,10" or "16-19" -> one CPU per line
    local part
    for part in ${1//,/ }; do
        if [[ "$part" == *-* ]]; then seq "${part%-*}" "${part#*-}"; else echo "$part"; fi
    done
}
subnet_of() { echo "${1%.*}.0/24"; }   # backends of one machine share a /24 ...
gateway_of() { echo "${1%.*}.1"; }     # ... whose .1 is the machine itself
declare -A host_subnet
for b in "${backends[@]}"; do read -r name host ip cpus <<< "$b"; host_subnet[$host]=$(subnet_of "$ip"); done

# --- recording what we change ---
record() { echo "$*" >> "$state"; }          # one undo command per line
set_sysctl() {                               # set_sysctl key value
    local old; old=$("${H[@]}" sysctl -n "$1")
    record "${H[*]} sysctl -qw $1=$old"
    "${H[@]}" sysctl -qw "$1=$2"
}

up() {
    [[ -e "$state" ]] && die "already set up ($state exists); run 'down' first"
    mkdir -p "$(dirname "$state")"
    : > "$state"

    # All roles.
    if command -v iw > /dev/null && "${H[@]}" iw dev "$dev" info > /dev/null 2>&1; then
        local ps; ps=$("${H[@]}" iw dev "$dev" get power_save | awk '{print $3}')
        record "${H[*]} iw dev $dev set power_save $ps"
        "${H[@]}" iw dev "$dev" set power_save off
    fi
    set_sysctl net.ipv4.conf.all.rp_filter 0
    set_sysctl net.ipv4.conf.default.rp_filter 0
    set_sysctl "net.ipv4.conf.$dev.rp_filter" 0
    if command -v nft > /dev/null && nft list ruleset 2> /dev/null | grep -q "policy drop"; then
        echo "setup.sh: WARNING: a firewall with 'policy drop' is active on $me; it may drop our UDP port ${value[ENCAP_PORT]:-7700} or forwarded packets" >&2
    fi

    # Routes to every other machine's backends.
    for host in "${!host_subnet[@]}"; do
        [[ "$host" == "$me" ]] && continue
        "${H[@]}" ip route add "${host_subnet[$host]}" via "${addr[$host]}" dev "$dev"
        record "${H[*]} ip route del ${host_subnet[$host]}"
    done

    if [[ "$me" == "${value[CLIENT]}" ]]; then
        local lb=${addr[${value[LB]}]}
        "${H[@]}" ip route add "${value[VIP]}/32" via "$lb" dev "$dev" mtu lock "${value[VIP_ROUTE_MTU]:-1460}"
        record "${H[*]} ip route del ${value[VIP]}/32"
        set_sysctl net.ipv4.tcp_tw_reuse 1
        local range; range=$("${H[@]}" sysctl -n net.ipv4.ip_local_port_range | xargs)
        record "${H[*]} sysctl -qw net.ipv4.ip_local_port_range=\"$range\""
        "${H[@]}" sysctl -qw net.ipv4.ip_local_port_range="1024 65535"
    fi

    local mine=()
    for b in "${backends[@]}"; do read -r name host ip cpus <<< "$b"; [[ "$host" == "$me" ]] && mine+=("$b"); done
    if [[ ${#mine[@]} -gt 0 ]]; then
        set_sysctl net.ipv4.ip_forward 1
        local gw; gw=$(gateway_of "$(echo "${mine[0]}" | awk '{print $3}')")
        "${H[@]}" ip link add cs331br type bridge
        record "${H[*]} ip link del cs331br"
        "${H[@]}" ip addr add "$gw/24" dev cs331br
        "${H[@]}" ip link set cs331br up
        for b in "${mine[@]}"; do
            read -r name host ip cpus <<< "$b"
            ip netns add "$name"
            record "ip netns del $name"
            local hostend="c-$name"
            if [[ ${#H[@]} -gt 0 ]]; then  # emulation: the machine's end goes into its namespace
                ip link add "$hostend" netns "$CS331_HOST_NETNS" type veth peer name eth0 netns "$name"
            else
                ip link add "$hostend" type veth peer name eth0 netns "$name"
            fi
            "${H[@]}" ip link set "$hostend" master cs331br up
            ip netns exec "$name" ip link set lo up
            ip netns exec "$name" ip link set eth0 up
            ip netns exec "$name" ip addr add "$ip/24" dev eth0
            ip netns exec "$name" ip route add default via "$gw"
            # Direct server return: the VIP on lo, never answered by ARP.
            ip netns exec "$name" ip addr add "${value[VIP]}/32" dev lo
            ip netns exec "$name" sysctl -qw net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2 \
                net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 net.ipv4.conf.eth0.rp_filter=0
            # Receive processing on the backend's own CPUs (D14).
            local mask=0 c
            for c in $(cpulist_expand "$cpus"); do mask=$((mask | (1 << c))); done
            ip netns exec "$name" sh -c "printf '%x' $mask > /sys/class/net/eth0/queues/rx-0/rps_cpus"
        done
    fi
    echo "set up $me ($(wc -l < "$state") changes recorded)"
}

down() {
    [[ -e "$state" ]] || { echo "nothing to undo on $me"; return; }
    tac "$state" | while read -r undo; do eval "$undo" 2> /dev/null || echo "setup.sh: could not undo: $undo" >&2; done
    rm -f "$state"
    echo "undone on $me"
}

case "$action" in up) up ;; down) down ;; *) die "usage: setup.sh up|down NAME" ;; esac
