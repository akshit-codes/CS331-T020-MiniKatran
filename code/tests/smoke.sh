#!/bin/bash
# end to end test of all modes, run as root
cd "$(dirname $0)/.."
VIP=10.0.0.10
fail=0

check() {
	echo "== $1 =="
	hits=$(for i in $(seq 20); do ip netns exec client curl -s --max-time 2 http://$VIP:8080/ || echo "fail"; done \
		| sort | uniq -c | awk '{print $NF"="$1}' | paste -sd' ')
	echo "   http x20: $hits"
	case "$hits" in *be1*be2*) ;; *) echo "   FAIL: not both backends"; fail=1 ;; esac
	want=$(md5sum /tmp/minikatran/www-be1/big.bin | cut -c1-32)
	got=$(ip netns exec client curl -s --max-time 30 http://$VIP:8080/big.bin | md5sum | cut -c1-32)
	if [ "$want" = "$got" ]; then echo "   10MB integrity: ok"; else echo "   FAIL: integrity"; fail=1; fi
	r=$(ip netns exec client timeout 20 iperf3 -c $VIP -t 2 2>&1 | grep receiver | awk '{print $7 " " $8}')
	[ -n "$r" ] && echo "   iperf3: $r" || { echo "   FAIL: iperf3"; fail=1; }
	c=$(ip netns exec client bin/cps $VIP:8081 32 2 | head -1)
	echo "   cps: $c"
}

scripts/backends.sh stop >/dev/null 2>&1
scripts/topo.sh down 2>/dev/null
scripts/topo.sh up
scripts/backends.sh start

scripts/run_proxy.sh start >/dev/null
check "tcp proxy"
scripts/run_proxy.sh stop >/dev/null

scripts/run_raw.sh start >/dev/null
check "raw af_packet forwarder"
scripts/run_raw.sh stop >/dev/null

scripts/run_xdp.sh start >/dev/null
check "xdp"
echo "   $(bin/lb flows | tail -1) in the flow table"
scripts/run_xdp.sh stop >/dev/null

scripts/backends.sh stop >/dev/null
scripts/topo.sh down
[ $fail = 0 ] && echo "ALL OK" || echo "FAILURES"
exit $fail
