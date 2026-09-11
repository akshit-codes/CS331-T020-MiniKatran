#!/bin/bash
# http (8080, 8081) and iperf3 (5201) servers in each backend ns

RUN=/tmp/minikatran
BACKENDS="be1 be2"

start() {
	stop >/dev/null
	mkdir -p $RUN
	for b in $BACKENDS; do
		mkdir -p $RUN/www-$b
		echo "hello from $b" > $RUN/www-$b/index.html
		[ -f $RUN/big.bin ] || head -c 10000000 /dev/urandom > $RUN/big.bin
		cp $RUN/big.bin $RUN/www-$b/big.bin
		ip netns exec $b python3 -m http.server 8080 --bind 0.0.0.0 --directory $RUN/www-$b \
			>$RUN/http-$b.log 2>&1 &
		echo $! > $RUN/http-$b.pid
		ip netns exec $b iperf3 -s -D -I $RUN/iperf-$b.pid >/dev/null 2>&1
		# small C server on 8081 for the connection rate benchmark
		ip netns exec $b $(dirname $0)/../bin/httpd 8081 $b >/dev/null 2>&1 &
		echo $! > $RUN/httpd-$b.pid
	done
	sleep 0.5
	echo "backends up"
}

stop() {
	for b in $BACKENDS; do
		for f in $RUN/http-$b.pid $RUN/iperf-$b.pid $RUN/httpd-$b.pid; do
			[ -f $f ] && kill $(cat $f) 2>/dev/null; rm -f $f
		done
	done
	echo "backends down"
}

case "$1" in
	start) start ;;
	stop) stop ;;
	*) echo "usage: $0 start|stop"; exit 1 ;;
esac
