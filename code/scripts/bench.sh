#!/bin/bash
# runs the benchmark for every mode, one csv line per number (mode,test,rep,metric,value)
cd "$(dirname $0)/.."
VIP=10.0.0.10
DUR=${DUR:-5}
REPS=${REPS:-3}
MODES=${MODES:-direct proxy raw xdp}
OUT=results
CSV=$OUT/data.csv
mkdir -p $OUT

busy() { awk '/^cpu /{print $2+$3+$4+$6+$7+$8}' /proc/stat; }	# jiffies all cpus spent not idle
pidcpu() { local t=0; for p in "$@"; do t=$((t + $(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null || echo 0))); done; echo $t; }
bpfstat() { bpftool prog show name lb 2>/dev/null | grep -o "run_time_ns [0-9]* run_cnt [0-9]*" | awk '{print $2, $4}'; }
rec() { echo "$MODE,$1,$REP,$2,$3" >> $CSV; }

# $1 test name, rest = command. runs it with output in a file, records cpu around it
run_test() {
	local name=$1; shift
	local out=$OUT/$MODE-$name-$REP.out
	local b0=$(busy) p0=$(pidcpu $PIDS) s0=$(bpfstat) t0=$(date +%s.%N)
	"$@" > $out 2>&1
	local t1=$(date +%s.%N) b1=$(busy) p1=$(pidcpu $PIDS) s1=$(bpfstat)
	rec $name elapsed_s $(awk "BEGIN{print $t1 - $t0}")
	rec $name sys_cpu_s $(awk "BEGIN{print ($b1 - $b0) / 100}")
	rec $name lb_cpu_s $(awk "BEGIN{print ($p1 - $p0) / 100}")
	if [ -n "$s1" ]; then
		set -- $s0 $s1
		rec $name xdp_ns_per_pkt $(awk "BEGIN{print ($3 - $1) / ($4 - $2)}")
		rec $name xdp_cpu_s $(awk "BEGIN{print ($3 - $1) / 1e9}")
	fi
}

do_tput() {
	run_test tput$1 ip netns exec client iperf3 -c $TARGET -t $DUR -P $1 -J
	python3 - "$OUT/$MODE-tput$1-$REP.out" <<'PY' | while read m v; do rec tput$1 $m $v; done
import json, sys
e = json.load(open(sys.argv[1]))['end']
print("gbps", e['sum_received']['bits_per_second'] / 1e9)
print("retrans", e['sum_sent']['retransmits'])
PY
}

do_cps() {
	local f=$OUT/$MODE-cps$1-$REP.out
	run_test cps$1 ip netns exec client bin/cps $TARGET:8081 $1 $DUR
	rec cps$1 conn_per_s $(awk '/conn\/s/{print $6}' $f)
	rec cps$1 errors $(awk '/conn\/s/{print $8}' $f)
	rec cps$1 p50_us $(awk '/latency/{print $3}' $f)
	rec cps$1 p99_us $(awk '/latency/{print $9}' $f)
}

setup() {
	TARGET=$VIP; PIDS=""
	case $MODE in
	direct) ip netns exec lb sysctl -qw net.ipv4.ip_forward=1; TARGET=10.0.2.11 ;;
	proxy) scripts/run_proxy.sh start >/dev/null; PIDS=$(cat /tmp/minikatran/proxy-*.pid) ;;
	raw) scripts/run_raw.sh start >/dev/null; PIDS=$(cat /tmp/minikatran/raw.pid) ;;
	xdp) scripts/run_xdp.sh start >/dev/null ;;
	esac
	sleep 0.5
}

teardown() {
	case $MODE in
	direct) ip netns exec lb sysctl -qw net.ipv4.ip_forward=0 ;;
	proxy) scripts/run_proxy.sh stop >/dev/null ;;
	raw) scripts/run_raw.sh stop >/dev/null ;;
	xdp) scripts/run_xdp.sh stop >/dev/null ;;
	esac
}

# pin the cpu governor, laptop throttling was messing with the numbers
GOV=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $g; done
trap 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo $GOV > $g; done' EXIT

sysctl -qw kernel.bpf_stats_enabled=1
[ -f $CSV ] || echo "mode,test,rep,metric,value" > $CSV
# interleave the modes so heat affects all of them equally
for REP in $(seq $REPS); do
	for MODE in $MODES; do
		echo "$MODE rep $REP  ($(awk '{print $1/1000}' /sys/class/thermal/thermal_zone*/temp | sort -n | tail -1)C)"
		setup
		do_tput 1
		do_tput 4
		do_cps 1
		do_cps 64
		teardown
	done
done
sysctl -qw kernel.bpf_stats_enabled=0
python3 scripts/summarize.py
