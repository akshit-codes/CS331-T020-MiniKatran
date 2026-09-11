#!/bin/bash
# runs the user space proxy in the lb namespace, one instance per VIP port
# (8080 for the http test, 5201 for iperf3)
RUN=/tmp/minikatran
VIP=10.0.0.10
BIN=$(dirname $0)/../bin/tcp_proxy

start() {
	mkdir -p $RUN
	# the proxy needs the VIP to be a local address so it can bind it
	ip netns exec lb ip addr add $VIP/32 dev lo 2>/dev/null
	for port in 8080 8081 5201; do
		# iperf3 opens a control and a data connection, both must land on the same backend
		opt=""; [ $port = 5201 ] && opt="-s"
		ip netns exec lb $BIN $opt $VIP:$port 10.0.2.11:$port 10.0.2.12:$port >$RUN/proxy-$port.log 2>&1 &
		echo $! > $RUN/proxy-$port.pid
	done
	sleep 0.2
	echo "proxy up"
}

stop() {
	for port in 8080 8081 5201; do
		[ -f $RUN/proxy-$port.pid ] && kill $(cat $RUN/proxy-$port.pid) 2>/dev/null; rm -f $RUN/proxy-$port.pid
	done
	ip netns exec lb ip addr del $VIP/32 dev lo 2>/dev/null
	echo "proxy down"
}

case "$1" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: $0 start|stop"; exit 1 ;;
esac
