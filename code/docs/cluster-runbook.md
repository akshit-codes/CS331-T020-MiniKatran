# Running the campaign on four laptops

`bench/cluster.py` runs a short version of the campaign on four real machines
over wifi, driven from the client laptop over ssh, in about 50 minutes (33 with
`--quick`). It
writes the same files as the single-laptop campaign, so
`bench/analyze_campaign.py` makes the same tables and plots. Design: D39.

```
  client ──┐                       ┌── h1: beP1, beP2 (namespaces behind a bridge, 10.11.1.0/24)
           │   wifi access point   │
  lb ──────┼───────────────────────┼── h2: beE1, beE2 (10.11.2.0/24)
           │                       │
  client → lb (director, TC on its wifi interface) → h1/h2 (routes to the backend) → backend
  backend → its machine → client (direct server return, source = the VIP)
```

**Why the backends sit behind their machine instead of on the wifi:** a wifi
client can only send frames with its own MAC address, so the backends can't
have addresses of their own on the wifi network. Each backend machine routes
to its backends' namespaces, and the director sends a backend's packets to its
machine's MAC (D39). No XDP anywhere (D35): the director is TC, and works on
any wifi driver.

## Before the day (on every laptop)

1. **Same code everywhere:** clone the repo at the same path, check out the
   same commit, `make`. Needs Go, clang, libbpf headers, kernel ≥ 6.6 with
   BTF (`ls /sys/kernel/btf/vmlinux`), `iw`, `rsync`, `curl`.
2. **ssh:** from the client, key-based login to all four machines including
   itself (`ssh-copy-id`), and passwordless sudo for that user (e.g. a file
   in `/etc/sudoers.d/`; remove it afterwards).
3. **Try `tune.sh` once:** `sudo scripts/tune.sh apply && scripts/tune.sh status
   && sudo scripts/tune.sh undo`. It finds the turbo switch for `intel_pstate`,
   `amd-pstate` or `acpi-cpufreq` (status shows the driver and the switch);
   if it warns that it found none, the clocks will follow the load on that laptop.
4. **Fill in `cluster/cluster.conf`** from `cluster/cluster.conf.example`
   and commit it, so every machine has the identical file (the driver checks).
   - addresses: `ip -4 addr` on each machine; ask for fixed addresses (DHCP
     reservation) or check them on the day;
   - interfaces: `ip link` (wlp…);
   - CPUs: `lscpu --extended` shows cores and their hyperthread siblings.
     Give each backend whole cores, keep the housekeeping CPUs free, and on the
     client give the load generator 2 cores with both their threads;
   - keep the backend names beP1, beP2, beE1, beE2 (the scenarios use them).

## The network

Use a **phone hotspot or our own cheap router**, not the college wifi. Two
things on managed networks break us:
- **Client isolation:** laptops on the same wifi can't reach each other. Test:
  `ping` every other laptop.
- **Source-address filtering** (IP source guard, ARP inspection, "anti
  spoofing"): replies from the backends carry the VIP (10.2.0.1) as source
  address, which isn't any device's address on that network, and the access
  point may drop them. The smoke test shows it: SYN-ACKs leave the backend
  machine (`sudo tcpdump -ni wlp… host 10.2.0.1` there) but never reach the client.

A hotspot shares its airtime with the phone itself; keep other devices off it.

## Checks on the day

| Check | How | Why |
|---|---|---|
| Power | **first:** `powerprofilesctl set performance` on each, all on AC, lids open, browsers closed. `tune.sh` only warns about the profile | clocks and throttling (D20); our run left three laptops on other profiles |
| Reachability | `ping` between all four | client isolation |
| ssh + sudo | `ssh MACHINE sudo true` from the client, for all four | the driver's transport |
| Firewall | `sudo nft list ruleset`, `ufw status`: stop it for the run, or allow forwarding and UDP 7700 | setup.sh only warns |
| Same commit + config | the driver's preflight checks both | |
| MTU | automatic: the client's route to the VIP gets MTU 1460, so our 40-byte encapsulation still fits the wifi's 1500 (wifi drivers often can't raise their MTU) | otherwise large packets are dropped silently |
| Wifi power save | automatic: `setup.sh` turns it off and back on afterwards; check `iw dev wlp… get power_save` | power save adds tens of ms of latency |
| Dry run first | `python3 bench/cluster.py cluster/cluster.conf --dry` (~10 min) | finds most problems quickly |

## Running

```bash
python3 bench/cluster.py cluster/cluster.conf --dry   # plumbing, ~10 min
python3 bench/cluster.py cluster/cluster.conf         # the real thing, ~50 min
python3 bench/cluster.py cluster/cluster.conf --quick # shorter, ~33 min, see below
```

**What `--quick` gives up.** It took 32 min on the real laptops (the estimate from the step
lengths was 33: S1 9 min, S2 10, S3 10, plus about 4 for tuning, setup and analysis).
- **Single runs:** one oracle search instead of two. Every table and scenario config runs
  once (as most already do in the full run), so nothing in the run says how much of a
  difference is noise.
- **A coarser search:** 4 probes instead of 5 doubles the resolution, to about 2% in S2 and
  4% in S1. `summary.md` prints what it was.
- **No by-cores table,** so H1 can't be checked. Oracle vs equal remains.
- **A shorter antagonist:** on at 30 s, off at 120 s, end at 150 s, instead of 60, 180 and
  240. A recovery slower than 75 s reads as "never", late p99 rests on 30 s instead of 120 s,
  and "before" on 15 s. So its numbers aren't directly comparable with a full run's or the
  single laptop's.

Run as your user on the client (not with sudo). Results:
`results/cluster-<time>/summary.md` and plots. Steps, all undone at the end
whatever happens: preflight checks, `setup.sh up` on every machine, S1 (smoke
test through the VIP, each backend's capacity, signal noise), tuning, S2
(static policies, controller tables), S3 (antagonist with the static oracle,
CPU, total RIF and the redirector contrast), collecting the files from the LB
and client machines, `setup.sh down`, analysis.

A run whose controller died (one failed signal read ends it, and more likely on
wifi) is marked `invalid` in `runs.jsonl`. It is listed at the top of `summary.md`
and left out of the tables and plots, so check that line.

If a run dies so hard that it can't clean up: on each machine
`sudo CS331_CLUSTER=cluster/cluster.conf scripts/cluster/setup.sh down NAME`,
`sudo scripts/tune.sh undo`, `sudo bin/lbctl unload` (LB) or
`sudo CS331_CLUSTER=cluster/cluster.conf bin/lbctl unload -role redirectors -host NAME`.

## What to expect to differ from the single laptop

- **Request rates ~20× lower.** Wifi carries a few hundred requests per
  second at best (each request crosses the air several times), so a request
  costs 40 ms of CPU instead of 1 ms and the SLO is 800 ms instead of 20.
  Ratios (usable capacity fraction, recovery relative to the SLO) are
  comparable; absolute numbers aren't.
- **A network share in every latency:** on a phone hotspot we saw an idle ping of ~140 ms,
  up to 362, so p99 has a floor that the single laptop doesn't have, and the
  oracle's usable fraction will likely be lower.
- **More noise:** other traffic on the air, roaming, power management. Expect
  the capacities to vary more than 2.5% between runs, so the smallest
  difference we can claim grows; repeats matter more than on the laptop.
- **Real heterogeneity:** different laptops, not only P vs E cores plus a
  slowness factor. How far *by cores* is from the oracle depends on which
  laptops we have.
- **Signals:** fewer, longer requests change the noise, so the tuning rule
  will pick different periods and bands (that's the point of having a rule).
- **Second hops cost airtime:** a forwarded packet crosses the air twice more
  (backend machine → access point → other backend machine). It still keeps
  connections alive; it's slower.
- **Resets** are only watched on the client (broken connections); backend-side
  resets come from each backend's own counters, as before.

## What we learned on the real laptops (2026-11-06)

Results and what they do and don't show: [results.md](results.md#real-laptops).

- **On a phone hotspot, start from `COST_MS=40`, `SLO_P99_MS=800`, `CONNS=32`.** With 20 ms, 400 ms
  and 64, capacity through the LB in the dry run was well under the sum of the backends. With 32
  connections the generator's pool fills about when the backends do, so if send delays appear
  well below capacity, try more connections.
- **Paste lines one at a time around `sudo -iu`, `ufw` and `ssh`.** Each can start a new shell or
  prompt or cut the session, and the rest of a pasted block then goes to the wrong place or nowhere.
- **After `sudo -iu student`,** `chown: … Operation not permitted` lines from the driver are
  harmless: `SUDO_UID` is still set, so the driver tries to give its results to the original user.
- **`hostname` is missing on Arch** (package `inetutils`). Harmless: nothing in the repo uses it.
- **Set the power profile first** (checks above). Check `lscpu` and `scripts/tune.sh status` on the
  backend machines too: with clocks fixed, similar laptops needn't run at the same speed. h2 ran
  at ~72% of h1's speed, and that's not visible in `summary.md`, since calibration runs only on the
  first backend and "theoretical" assumes that speed everywhere.
- **A dry run's speeds aren't the real run's:** `--dry` doesn't apply `tune.sh`, so turbo is on
  (182 thousand work units per ms on h1, against 116 in the real run). Check the plumbing with it, not
  the capacities.
- **`nan` everywhere in a dry run's tuning table, then a `TypeError`,** means too few signal samples
  in its short noise window (samples ~2 s apart leave at most 4), not broken signals. The real run's
  samples were ~1 s apart, up to 2.6.

## What is untested

Tested here: the whole driver on four *emulated* machines
(`scripts/cluster/emulate.sh`: network namespaces on one bridge standing in
for the access point; `cluster/emulated.conf`): preflight, setup and undo,
the smoke test through the VIP, S1, tuning, S2 and S3 including the redirector
contrast (14–15 long-lived connections broken without forwarding, 0 with it), file
layout and analysis, in dry mode. The single-laptop campaign still runs
unchanged. Rerun on 2026-11-06 with the startup fix and the dead-controller check:
healthy runs aren't flagged. The flagging itself was only tried on the single laptop
(kill -9); here it rests on `systemctl is-active`.

The emulated driver now runs as a normal user with `sudo` per command, like the real
transport, and refuses to run as root. Running it as root had hidden a bug: `sudo mkdir` on
the client left the results directory root-owned, so the driver couldn't write `runs.jsonl`.
Also tried: `setup.sh up` failing on the third machine (a namespace in the way) undoes the
machines before it, so the next attempt starts clean.

The real run tested the ssh transport, `rsync` and the ownership fix on a separate LB machine, TC
and the MTU on real wifi drivers, and the `--quick` timing, on one phone hotspot with Intel
laptops. Still not tested:
- undoing `setup.sh` on a machine that has become unreachable: `down` reports nothing and
  the manual steps above are the way out;
- other access points (isolation, address filtering) and roaming;
- `tune.sh` on AMD / `acpi-cpufreq` laptops: its turbo and EPP handling and
  undo were only checked against fake sysfs trees of those drivers;
- the full-length run (~50 min is an estimate from the step lengths).
