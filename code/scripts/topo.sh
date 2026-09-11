#!/bin/bash
# test network for the load balancer, everything lives in namespaces
#
#   client (10.0.1.2) ---+                  +--- be1 (10.0.2.11, gw 10.0.2.1)
#                        |   [sw: br0]      |
#                        +----- lb ---------+--- be2 (10.0.2.12, gw 10.0.2.1)
#                   (10.0.1.1 + 10.0.2.1 on eth0)
#
#   VIP 10.0.0.10 is routed to the lb from the client side
#
# usage: topo.sh up | down | check

VIP=10.0.0.10
NSES="sw client lb be1 be2"
Q=8	# queues per veth, with 1 all xdp work ends up on one cpu

ns() { ip netns exec "$@"; }

up() {
	for n in $NSES; do ip netns add $n; done
	ns sw ip link add br0 type bridge
	ns sw ip link set br0 up

	for n in client lb be1 be2; do
		ip link add sw-$n numrxqueues $Q numtxqueues $Q type veth peer name eth0 numrxqueues $Q numtxqueues $Q
		ip link set sw-$n netns sw
		ip link set eth0 netns $n
		ns sw ip link set sw-$n master br0
		ns sw ip link set sw-$n up
		ns $n ip link set lo up
		# offloads off, we want real packets with real checksums
		ns $n ethtool -K eth0 tx off tso off gso off gro off >/dev/null
		ns $n sysctl -qw net.ipv6.conf.all.disable_ipv6=1
		# rps
		ns $n sh -c 'for q in /sys/class/net/eth0/queues/rx-*/rps_cpus; do echo ffffff > $q; done'
	done

	ns client ip link set eth0 address 02:00:00:00:01:02
	ns lb     ip link set eth0 address 02:00:00:00:00:01
	ns be1    ip link set eth0 address 02:00:00:00:02:0b
	ns be2    ip link set eth0 address 02:00:00:00:02:0c

	ns client ip addr add 10.0.1.2/24 dev eth0
	ns lb     ip addr add 10.0.1.1/24 dev eth0
	ns lb     ip addr add 10.0.2.1/24 dev eth0
	ns be1    ip addr add 10.0.2.11/24 dev eth0
	ns be2    ip addr add 10.0.2.12/24 dev eth0
	for n in client lb be1 be2; do ns $n ip link set eth0 up; done

	ns client ip route add default via 10.0.1.1
	ns be1    ip route add default via 10.0.2.1
	ns be2    ip route add default via 10.0.2.1
	# lb is not a router. anything that is not for the lb itself and is not
	# grabbed by the load balancer gets dropped quietly
	ns lb sysctl -qw net.ipv4.ip_forward=0
	ns lb ip route add blackhole 10.0.0.0/24
	# the VIP is on the client's side of the lb
	ns client ip route add $VIP/32 via 10.0.1.1 2>/dev/null || true
	# second client ip for the no-lb runs, otherwise TIME_WAIT/PAWS on the backends
	# rejects reused ports (tcp timestamp offset differs per src/dst pair)
	ns client ip addr add 10.0.1.3/24 dev eth0
	ns client ip route add 10.0.2.0/24 via 10.0.1.1 src 10.0.1.3
}

down() {
	for n in $NSES; do ip netns del $n 2>/dev/null; done
}

check() {
	echo "client -> lb"; ns client ping -c1 -W1 10.0.1.1 | tail -1
	echo "be1 -> lb";    ns be1 ping -c1 -W1 10.0.2.1 | tail -1
	echo "be2 -> lb";    ns be2 ping -c1 -W1 10.0.2.1 | tail -1
	echo "be1 -> be2";   ns be1 ping -c1 -W1 10.0.2.12 | tail -1
	echo "client -> be1 (should fail, lb does not route)"; ns client ping -c1 -W1 10.0.2.11 | tail -1
}

case "$1" in
	up) up ;;
	down) down ;;
	check) check ;;
	*) echo "usage: $0 up|down|check"; exit 1 ;;
esac
