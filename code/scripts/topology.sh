#!/usr/bin/env bash
# Build or tear down the single-laptop testbed out of network namespaces.
#
#   ns cli                ns rtr                              ns lb / ns beP1 ... beE2
#   eth0 10.0.0.2 ---veth--- cli0 10.0.0.1
#                            fab0 (bridge) 10.1.0.1 ---veth--- eth0 10.1.0.10 / .11 .. .14
#
# The router forwards between the client and the fabric bridge. Backends carry
# the VIP on lo for direct server return; the router sends VIP traffic to the
# LB (bin/lbctl loads it).
#
# Receive Packet Steering (RPS) decides which CPU processes received packets.
# Without it, veth traffic is processed on whichever CPU sent it, so all the
# router/LB/backend work would land on the load generator's cores.
#
# Usage: sudo scripts/topology.sh up | down | status | check
set -euo pipefail
source "$(dirname "$0")/lib.sh"

in_ns() { local ns=$1; shift; ip netns exec "$ns" "$@"; }

set_rps() {   # set_rps <ns> <dev> <cpulist>
    in_ns "$1" sh -c "echo $(cpulist_to_mask "$3") > /sys/class/net/$2/queues/rx-0/rps_cpus"
}

base_ns_setup() {   # loopback up, IPv6 off (no router solicitations or DAD noise)
    in_ns "$1" ip link set lo up
    in_ns "$1" sysctl -qw net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1
}

add_fabric_member() {   # add_fabric_member <ns> <ip>
    local ns=$1 ip=$2
    ip link add eth0 netns "$ns" type veth peer name "p-$ns" netns rtr
    in_ns rtr ip link set "p-$ns" master fab0 mtu "$FABRIC_MTU" up
    in_ns "$ns" ip link set eth0 mtu "$FABRIC_MTU" up
    in_ns "$ns" ip addr add "$ip/24" dev eth0
    in_ns "$ns" ip route add default via "$ROUTER_FABRIC_IP"
}

up() {
    need_root
    for ns in "${NAMESPACES[@]}"; do
        ip netns list | grep -qw "^$ns" && die "namespace $ns exists; run 'down' first"
    done
    for ns in "${NAMESPACES[@]}"; do ip netns add "$ns"; base_ns_setup "$ns"; done

    # client <-> router
    ip link add eth0 netns cli mtu "$CLIENT_MTU" type veth peer name cli0 netns rtr mtu "$CLIENT_MTU"
    in_ns cli ip addr add "$CLIENT_IP/24" dev eth0
    in_ns cli ip link set eth0 up
    in_ns cli ip route add default via "$ROUTER_CLIENT_IP"
    # The client closes every connection, so each one waits 60 s in TIME_WAIT.
    # At ~250 new connections/s that fills ~40% of the default 28k ephemeral
    # ports, and picking a port for a new connection then stalls now and then
    # for tens of ms (campaign S2: one 10-s window at 75 ms, the other at 11).
    # Reuse TIME_WAIT ports and use the whole port range (this namespace only).
    in_ns cli sysctl -qw net.ipv4.tcp_tw_reuse=1 net.ipv4.ip_local_port_range="1024 65535"
    in_ns rtr ip addr add "$ROUTER_CLIENT_IP/24" dev cli0
    in_ns rtr ip link set cli0 up
    in_ns rtr sysctl -qw net.ipv4.ip_forward=1

    # router <-> fabric bridge <-> LB and backends
    in_ns rtr ip link add fab0 type bridge
    in_ns rtr ip addr add "$ROUTER_FABRIC_IP/24" dev fab0
    add_fabric_member lb "$LB_IP"
    for i in "${!BE_NAMES[@]}"; do add_fabric_member "${BE_NAMES[$i]}" "${BE_IPS[$i]}"; done
    in_ns rtr ip link set fab0 mtu "$FABRIC_MTU" up
    in_ns rtr ip route add "$VIP/32" via "$LB_IP"   # clients reach the VIP through the LB

    # Direct server return on the backends: VIP on lo, never answer ARP for it,
    # and accept packets whose source isn't routed back the same way.
    for be in "${BE_NAMES[@]}"; do
        in_ns "$be" ip addr add "$VIP/32" dev lo
        in_ns "$be" sysctl -qw net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2 \
            net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 net.ipv4.conf.eth0.rp_filter=0
    done

    # RPS: put receive processing on the CPUs of the role that should pay for it.
    set_rps cli eth0 "$LOADGEN_CPUS"     # replies arriving at the client
    set_rps rtr cli0 "$LB_CPUS"          # requests entering the router (later: then the LB)
    for i in "${!BE_NAMES[@]}"; do set_rps "${BE_NAMES[$i]}" eth0 "${BE_CPUS[$i]}"; done

    echo "testbed up"
}

down() {
    need_root
    for ns in "${NAMESPACES[@]}"; do ip netns del "$ns" 2>/dev/null || true; done
    rm -rf /sys/fs/bpf/cs331   # LB programs and maps pinned by lbctl, if loaded
    echo "testbed down"
}

status() {
    for ns in "${NAMESPACES[@]}"; do
        if ! ip netns list | grep -qw "^$ns"; then echo "$ns: (missing)"; continue; fi
        printf '%s:' "$ns"
        in_ns "$ns" ip -br -4 addr show | awk '$1 != "lo" {printf "  %s %s %s", $1, $2, $3}'
        echo
    done
    printf 'rps: cli/eth0=%s rtr/cli0=%s' \
        "$(in_ns cli cat /sys/class/net/eth0/queues/rx-0/rps_cpus)" \
        "$(in_ns rtr cat /sys/class/net/cli0/queues/rx-0/rps_cpus)"
    for be in "${BE_NAMES[@]}"; do printf ' %s/eth0=%s' "$be" "$(in_ns "$be" cat /sys/class/net/eth0/queues/rx-0/rps_cpus)"; done
    echo
}

# Show which CPUs did receive processing (NET_RX softirqs) for a burst of pings.
net_rx_counts() { awk '/NET_RX/ {for (i = 2; i <= NF; i++) printf "%s ", $i; print ""}' /proc/softirqs; }

check() {
    need_root
    for i in "${!BE_NAMES[@]}"; do
        in_ns cli ping -c1 -W1 -q "${BE_IPS[$i]}" > /dev/null && echo "cli -> ${BE_NAMES[$i]} ok" \
            || die "cli cannot reach ${BE_NAMES[$i]} (${BE_IPS[$i]})"
    done
    local before after
    before=$(net_rx_counts)
    in_ns cli ping -f -c 20000 -q "${BE_IPS[0]}" > /dev/null
    after=$(net_rx_counts)
    echo "NET_RX softirqs per CPU during 20000 pings cli -> ${BE_NAMES[0]}:"
    paste <(tr ' ' '\n' <<< "$before") <(tr ' ' '\n' <<< "$after") \
        | awk 'NF == 2 {d = $2 - $1; if (d > 500) printf "  cpu%-3d %d\n", NR - 1, d}'
    echo "expected: loadgen CPUs ($LOADGEN_CPUS), LB/router CPU ($LB_CPUS), ${BE_NAMES[0]} CPUs (${BE_CPUS[0]})"
}

case "${1:-}" in
    up) up ;; down) down ;; status) status ;; check) check ;;
    *) echo "usage: $0 up|down|status|check" >&2; exit 1 ;;
esac
