#!/bin/bash
# runs the AF_PACKET user space forwarder in the lb namespace
RUN=/tmp/minikatran
VIP=10.0.0.10
BIN=$(dirname $0)/../bin/raw_fwd

start() {
	mkdir -p $RUN
	ip netns exec lb $BIN eth0 $VIP 8080 8081 5201s 10.0.2.11=02:00:00:00:02:0b 10.0.2.12=02:00:00:00:02:0c \
		>$RUN/raw.log 2>&1 &
	echo $! > $RUN/raw.pid
	sleep 0.2
	echo "raw forwarder up"
}

stop() {
	[ -f $RUN/raw.pid ] && kill $(cat $RUN/raw.pid) 2>/dev/null; rm -f $RUN/raw.pid
	echo "raw forwarder down"
}

case "$1" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: $0 start|stop"; exit 1 ;;
esac
