"""Shared helpers for the benchmark scripts.

Reads config.env and backends.conf, and starts our programs inside the right
network namespace, on the right CPUs, each in its own cgroup (so per-backend
cpu.stat and cpu.pressure exist for the controller later).
All functions assume they run as root (the scripts are started with sudo).

Cluster mode (use_cluster, bench/cluster.py): the same helpers run each
command on the machine its role lives on, over ssh. command() is the one place
that decides how; the experiments in bench/campaign.py don't change.
"""

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
SLICE = "cs331.slice"  # parent cgroup for everything the experiment starts


def load_config():
    cfg = {}
    for line in (REPO / "config.env").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
    return cfg


def expand_cpus(cpus):
    """'8,10' -> [8, 10]; '16-19' -> [16, 17, 18, 19]"""
    out = []
    for part in cpus.split(","):
        lo, _, hi = part.partition("-")
        out += range(int(lo), int(hi or lo) + 1)
    return out


@dataclass
class Backend:
    name: str
    ip: str
    cpus: str
    kind: str  # "P" or "E"
    host: str = ""        # cluster only: the machine it runs on
    slowness: float = 1.0  # cluster only (the namespace testbed's is in bench/campaign.py)

    @property
    def ncpus(self):
        return len(expand_cpus(self.cpus))


def load_backends():
    backends = {}
    for line in (REPO / "backends.conf").read_text().splitlines():
        fields = line.split("#", 1)[0].split()
        if fields:
            backends[fields[0]] = Backend(*fields)
    return backends


CFG = load_config()
BACKENDS = load_backends()


# --- Cluster mode ---

CLUSTER = None  # set by use_cluster(): the machines of cluster.conf
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ControlMaster=auto", "-o", "ControlPersist=10m",
            "-o", "ControlPath=~/.ssh/cs331-%r@%h:%p"]  # one connection per machine, reused


def use_cluster(path):
    """Switch every helper to the machines described in cluster.conf."""
    global CLUSTER
    settings, hosts, backends = {}, {}, {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        f = line.split()
        if not f:
            continue
        if f[0] == "host":
            hosts[f[1]] = dict(address=f[2], interface=f[3], housekeeping=f[4])
        elif f[0] == "backend":
            backends[f[1]] = Backend(f[1], f[3], f[4], f[5], host=f[2], slowness=float(f[6]) if len(f) > 6 else 1.0)
        else:
            key, value = line.split("=", 1)
            settings[key.strip()] = value.strip()
    emulated = settings.get("TRANSPORT", "ssh") == "netns"
    CLUSTER = dict(path=os.path.relpath(Path(path).resolve(), REPO), hosts=hosts, emulated=emulated,
                   repo=str(REPO) if emulated else settings["REPO"], user=settings.get("SSH_USER", ""))
    settings["LB_IP"] = hosts[settings["LB"]]["address"]
    CFG.update(settings)
    BACKENDS.clear()
    BACKENDS.update(backends)


def host_of(where):
    """(machine, network namespace or None) of a role: "cli", "lb", a backend, or "host:NAME"."""
    if where.startswith("host:"):
        return where[5:], None
    if where in ("cli", "lb"):
        return CFG["CLIENT" if where == "cli" else "LB"], None
    return BACKENDS[where].host, where


def command(where, cmd, cpus=None, service=None):
    """The command line that runs cmd at `where` ("cli": the load generator's
    side, "lb": where lbctl, the controller and sigmon run, a backend's name, or
    in cluster mode "host:NAME"). With cpus and/or service it runs in its own
    cgroup in SLICE (service: as a background systemd service named so)."""
    if CLUSTER is None:  # the namespace testbed on this machine
        inner = cmd if where == "lb" else in_ns(where, cmd)  # lbctl & co. run in the root namespace
        return on_cpus(cpus, inner, service) if cpus else inner

    host, netns = host_of(where)
    repo = CLUSTER["repo"]
    cmd = [a.replace(str(REPO), repo) for a in map(str, cmd)]  # paths in the repo, on that machine
    host_ns = f"m-{host}" if CLUSTER["emulated"] else None  # emulation: each machine is a namespace
    env = ["env", f"CS331_CLUSTER={CLUSTER['path']}", f"CS331_HOST_NETNS={host_ns or ''}"]
    # Tools that create namespaces or pin BPF objects must see the real /run/netns
    # and /sys/fs/bpf, which `ip netns exec` hides; in emulation they're told
    # their machine's namespace instead (CS331_HOST_NETNS, lbctl -netns).
    self_placed = Path(cmd[0]).name in ("lbctl", "setup.sh")
    if cpus or service:
        unit = ["systemd-run", "--quiet", f"--slice={SLICE}", f"--working-directory={repo}"]
        if cpus:
            unit += ["-p", f"AllowedCPUs={cpus}"]
        if service:
            # A service doesn't start from our shell: it gets its namespace by
            # path, which also keeps /sys (cgroup files, pinned maps) visible.
            ns = netns or host_ns
            unit += ["--collect", f"--unit={service}"] + (["-p", f"NetworkNamespacePath=/run/netns/{ns}"] if ns else [])
            inner = unit + env + cmd
        else:
            inner = unit + ["--scope"] + env + (in_ns(netns, cmd) if netns else cmd)
            if host_ns and not netns:
                inner = in_ns(host_ns, inner)
    else:
        inner = env + (in_ns(netns, cmd) if netns else cmd)
        if host_ns and not netns and not self_placed:
            inner = in_ns(host_ns, inner)
    if CLUSTER["emulated"]:
        return ["sudo", *inner]  # like ssh below: the driver is a normal user, only its commands are root
    target = CLUSTER["hosts"][host]["address"]
    if CLUSTER["user"]:
        target = f"{CLUSTER['user']}@{target}"
    return ["ssh", *SSH_OPTS, target, f"cd {shlex.quote(repo)} && sudo {shlex.join(inner)}"]


def tune(action):
    """scripts/tune.sh apply|undo here, or on every machine of the cluster
    (each with its own housekeeping CPUs)."""
    script = str(REPO / "scripts/tune.sh")
    if CLUSTER is None:
        (run if action == "apply" else subprocess.run)([script, action])
        return
    for name, info in CLUSTER["hosts"].items():
        cmd = command(f"host:{name}", ["env", f"CS331_HOUSEKEEPING_CPUS={info['housekeeping']}", script, action])
        (run if action == "apply" else subprocess.run)(cmd, cwd=REPO)


def lb_load(*flags):
    """Load the LB: here (lbctl load), or in a cluster the director on the LB
    machine and the redirectors on each backend machine (flags go to those)."""
    lbctl = str(BIN / "lbctl")
    if CLUSTER is None:
        run([lbctl, "load", *flags], cwd=REPO, capture_output=True)
        return
    netns = ["-netns", f"m-{CFG['LB']}"] if CLUSTER["emulated"] else []
    run(command("lb", [lbctl, "load", "-role", "director", *netns]), cwd=REPO)
    for host in sorted({be.host for be in BACKENDS.values()}):
        run(command(f"host:{host}", [lbctl, "load", "-role", "redirectors", "-host", host, *flags]), cwd=REPO)


def lb_unload():
    lbctl = str(BIN / "lbctl")
    if CLUSTER is None:
        subprocess.run([lbctl, "unload"], cwd=REPO, capture_output=True)
        return
    subprocess.run(command("lb", [lbctl, "unload"]), cwd=REPO, capture_output=True)
    for host in sorted({be.host for be in BACKENDS.values()}):
        subprocess.run(command(f"host:{host}", [lbctl, "unload", "-role", "redirectors", "-host", host]),
                       cwd=REPO, capture_output=True)


_tools = {}


def start_tool(where, name, cmd):
    """Start a long-running tool (controller, sigmon, resetmon): a child process
    here, or a service on its machine in cluster mode."""
    if CLUSTER is None:
        _tools[name] = subprocess.Popen(command(where, cmd), cwd=REPO, stderr=subprocess.PIPE, text=True)
    else:
        run(command(where, cmd, service=f"cs331-tool-{name}"), cwd=REPO)


def stop_tool(where, name):
    """Stop it (our tools flush their output on SIGINT and SIGTERM); returns
    the last line of its stderr where we can see it."""
    if CLUSTER is None:
        proc = _tools.pop(name)
        proc.send_signal(2)  # SIGINT
        lines = proc.communicate()[1].strip().splitlines()
        return lines[-1] if lines else ""
    subprocess.run(command(where, ["systemctl", "stop", f"cs331-tool-{name}"]), cwd=REPO)
    return ""


def tool_alive(where, name):
    """Is the tool still running? (Our tools exit on the first failed read.)"""
    if CLUSTER is None:
        return _tools[name].poll() is None
    return subprocess.run(command(where, ["systemctl", "is-active", "--quiet", f"cs331-tool-{name}"]), cwd=REPO).returncode == 0


def give_back(path):
    """Give results back to the user who ran sudo."""
    if "SUDO_UID" in os.environ:
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ['SUDO_GID']}", str(path)])


def run(cmd, **kwargs):
    print("+", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, **kwargs)


def in_ns(ns, cmd):
    return ["ip", "netns", "exec", ns] + cmd


def on_cpus(cpus, cmd, service_name=None):
    """Wrap cmd so it runs in its own cgroup under SLICE, restricted to `cpus`.
    With service_name it becomes a background systemd service (stop it with
    stop_service); without, it runs in the foreground like a normal command."""
    wrapper = ["systemd-run", "--quiet", f"--slice={SLICE}", "-p", f"AllowedCPUs={cpus}"]
    if service_name:
        wrapper += ["--collect", f"--unit={service_name}"]
    else:
        wrapper += ["--scope"]
    return wrapper + cmd


def stop_service(name):
    subprocess.run(["systemctl", "stop", name], stderr=subprocess.DEVNULL)


def calibrate(cpu):
    """Work units per millisecond on one CPU."""
    out = run(on_cpus(str(cpu), [str(BIN / "backend"), "-calibrate"]), capture_output=True, text=True)
    return json.loads(out.stdout)["units_per_ms"]


def calibrate_backend(be):
    """Work units per millisecond on the first CPU of a backend (on its machine)."""
    cpu = str(expand_cpus(be.cpus)[0])
    out = run(command(be.name, [str(BIN / "backend"), "-calibrate"], cpus=cpu), capture_output=True, text=True, cwd=REPO)
    return json.loads(out.stdout)["units_per_ms"]


def start_backend(be, work, slowness=1.0, cost="fixed", mode="pool"):
    cmd = [str(BIN / "backend"), "-work", str(work), "-slowness", str(slowness), "-cost", cost, "-mode", mode,
           "-listen", f":{CFG['BACKEND_PORT']}", "-metrics", f":{CFG['METRICS_PORT']}"]
    run(command(be.name, cmd, cpus=be.cpus, service=f"cs331-{be.name}"), cwd=REPO)
    for _ in range(50):  # wait until it answers
        probe = subprocess.run(command(be.name, ["curl", "-sf", f"localhost:{CFG['METRICS_PORT']}/metrics"]),
                               capture_output=True)
        if probe.returncode == 0:
            return
        time.sleep(0.1)
    raise RuntimeError(f"backend {be.name} did not start")


def stop_backend(be):
    subprocess.run(command(be.name, ["systemctl", "stop", f"cs331-{be.name}"]), stderr=subprocess.DEVNULL)


def loadgen_cmd(target_ip, rate, warmup, duration, extra=()):
    return in_ns("cli", [str(BIN / "loadgen"), "-target", f"{target_ip}:{CFG['BACKEND_PORT']}",
                         "-rate", f"{rate:.1f}", "-warmup", f"{warmup}s", "-duration", f"{duration}s",
                         *extra])


def loadgen_command(target_ip, rate, warmup, duration, extra=()):
    """The full command line of one load-generator run, on the client's CPUs."""
    return command("cli", [str(BIN / "loadgen"), "-target", f"{target_ip}:{CFG['BACKEND_PORT']}",
                           "-rate", f"{rate:.1f}", "-warmup", f"{warmup}s", "-duration", f"{duration}s", *extra],
                   cpus=CFG["LOADGEN_CPUS"])


def loadgen(target_ip, rate, warmup, duration, extra=()):
    """Run one load-generator step against target_ip (a backend directly, or the VIP
    through the LB); return its JSON summary."""
    out = run(loadgen_command(target_ip, rate, warmup, duration, extra), capture_output=True, text=True, cwd=REPO)
    return json.loads(out.stdout)


def backend_metrics(be):
    """The backend's /metrics as a dict of name -> value."""
    out = subprocess.run(command(be.name, ["curl", "-s", f"localhost:{CFG['METRICS_PORT']}/metrics"]),
                         capture_output=True, text=True, check=True).stdout
    return {line.split()[0]: float(line.split()[1]) for line in out.splitlines()}


def tcp_counters(ns):
    """The kernel's TCP/IP counters of a namespace (/proc/net/snmp and netstat),
    as a dict like {"TcpOutRsts": 3, "TcpExtSyncookiesSent": 0, ...}."""
    counters = {}
    for path in ("/proc/net/snmp", "/proc/net/netstat"):
        text = subprocess.run(command(ns, ["cat", path]), capture_output=True, text=True, check=True).stdout
        lines = text.splitlines()
        for names, values in zip(lines[0::2], lines[1::2]):  # pairs: header line, value line
            prefix, names = names.split(":", 1)
            for name, value in zip(names.split(), values.split(":", 1)[1].split()):
                counters[prefix + name] = int(value)
    return counters


def start_antagonist(name, cpus, duty, kind="burn", on=None):
    """Burn CPU on `cpus` for `duty` of the time, in its own cgroup (not the
    backend's). kind: burn, mul or stream (cmd/antagonist). on: the backend
    whose machine that is (cluster mode; default: name)."""
    cmd = [str(BIN / "antagonist"), "-duty", str(duty), "-kind", kind]
    if CLUSTER is None:
        run(on_cpus(cpus, cmd, service_name=f"cs331-antagonist-{name}"))
    else:
        run(command("host:" + BACKENDS[on or name].host, cmd, cpus=cpus, service=f"cs331-antagonist-{name}"), cwd=REPO)


def stop_antagonist(name, on=None):
    if CLUSTER is None:
        stop_service(f"cs331-antagonist-{name}")
    else:
        subprocess.run(command("host:" + BACKENDS[on or name].host, ["systemctl", "stop", f"cs331-antagonist-{name}"]),
                       stderr=subprocess.DEVNULL)
