#!/bin/bash
# load the xdp load balancer into the lb namespace and configure it
BIN=$(dirname $0)/../bin/lb
VIP=10.0.0.10

start() {
	# ip netns exec hides /sys/fs/bpf, nsenter --net doesn't
	nsenter --net=/run/netns/lb $BIN load eth0 || exit 1
	# veth needs an xdp program on the peer for XDP_TX to work
	ip netns exec sw ip link set dev sw-lb xdpdrv obj $(dirname $0)/../bpf/xdp_pass.o sec xdp
	$BIN add-vip $VIP:8080
	$BIN add-vip $VIP:8081
	$BIN add-vip $VIP:5201 -s
	for port in 8080 8081 5201; do
		$BIN add-backend $VIP:$port 10.0.2.11 02:00:00:00:02:0b
		$BIN add-backend $VIP:$port 10.0.2.12 02:00:00:00:02:0c
	done
}

stop() {
	nsenter --net=/run/netns/lb $BIN unload eth0
	ip netns exec sw ip link set dev sw-lb xdpdrv off
}

case "$1" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: $0 start|stop"; exit 1 ;;
esac
