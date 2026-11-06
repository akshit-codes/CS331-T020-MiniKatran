#!/usr/bin/env bash
# Emulate the cluster's machines on this laptop, to test bench/cluster.py
# without real machines: every host of the cluster.conf (TRANSPORT=netns)
# becomes a network namespace m-NAME, attached to one bridge that stands in
# for the wifi access point. Each gets the host's address on an interface with
# the host's interface name. Not a wifi network: no airtime, no power save, no
# MTU limit, no client isolation (docs/cluster-runbook.md, "What is untested").
#
#   sudo CS331_CLUSTER=cluster-emulated.conf scripts/cluster/emulate.sh up|down
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
conf="$REPO/${CS331_CLUSTER:?set CS331_CLUSTER to the cluster.conf}"
hosts=()
while read -r kind name address dev _; do
    [[ "$kind" == host ]] && hosts+=("$name $address $dev")
done < <(sed 's/#.*//' "$conf")

case "${1:-}" in
up)
    ip link add cs331air type bridge
    ip link set cs331air up
    for h in "${hosts[@]}"; do
        read -r name address dev <<< "$h"
        ip netns add "m-$name"
        ip link add "air-$name" type veth peer name "$dev" netns "m-$name"
        ip link set "air-$name" master cs331air up
        ip -n "m-$name" link set lo up
        ip -n "m-$name" link set "$dev" up
        ip -n "m-$name" addr add "$address/24" dev "$dev"
    done
    echo "emulated ${#hosts[@]} machines on bridge cs331air" ;;
down)
    for h in "${hosts[@]}"; do read -r name _ <<< "$h"; ip netns del "m-$name" 2> /dev/null || true; done
    ip link del cs331air 2> /dev/null || true
    echo "emulation removed" ;;
*) echo "usage: emulate.sh up|down" >&2; exit 1 ;;
esac
