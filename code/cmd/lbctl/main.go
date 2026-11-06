// Command lbctl loads the load balancer's BPF programs into the testbed and
// configures them.
//
//	sudo bin/lbctl load [-hook tc|xdp] [-no-syncookie-check] [-no-forward]
//	                               attach everything (see below) and fill the backends map
//	sudo bin/lbctl table [-second previous|none] equal
//	                               publish a static table: equal share per backend
//	sudo bin/lbctl table [-second previous|none] beP1=1,beE1=1.09
//	                               ... or shares proportional to weights (unlisted = 0)
//	sudo bin/lbctl show            active table: generation and buckets per backend
//	sudo bin/lbctl dump            active table, one line per bucket: bucket first second
//	sudo bin/lbctl stats           director and redirector counters
//	sudo bin/lbctl unload          detach everything
//
// `load` attaches:
//   - the director (bpf/director_tc.c) on TC ingress of eth0 in namespace lb
//     (D35). With -hook xdp, bpf/director_xdp.c as native XDP instead, plus an
//     XDP_PASS program on p-lb, the router side of that veth, without which
//     veth drops XDP_TX'd frames. XDP is only for the hook comparison.
//   - a redirector (bpf/redirector.c) on TC ingress of every backend's eth0.
//     -no-syncookie-check turns off its SYN-cookie check, only to show what
//     breaks without it.
//
// `table -second previous` (the default) gives every bucket whose first hop
// changes the old first hop as second hop, so existing connections survive
// (lb.WithSecondHops); `-second none` publishes the table without second hops.
//
// Programs stay attached after lbctl exits because their links and maps are
// pinned (saved as files) under /sys/fs/bpf/cs331; `unload` deletes those
// files, which detaches everything. Run from the repo root.
//
// Cluster mode (CS331_CLUSTER=cluster.conf, real machines; docs/cluster-runbook.md):
//
//	sudo bin/lbctl load -role director [-netns NS]     on the LB machine: director on its interface
//	sudo bin/lbctl load -role redirectors -host H      on backend machine H: its backends' redirectors
//	sudo bin/lbctl unload [-role redirectors -host H]
//
// There the backends map holds each backend's own address and its *machine's*
// MAC (the next hop): the machine routes to the backend's namespace. -netns is
// only for emulating the machines with namespaces on one laptop.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/cilium/ebpf"
	"github.com/cilium/ebpf/link"

	"cs331lb/internal/lb"
	"cs331lb/internal/testbed"
)

const objDir = "bin/bpf"

// pinDir holds this machine's pinned links and maps. Redirectors on a cluster
// machine use their own directory, so roles never collide when emulated on one laptop.
var pinDir = "/sys/fs/bpf/cs331"

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cfg, err := testbed.Load(".")
	check(err)
	cmd := os.Args[1]
	flags := flag.NewFlagSet(cmd, flag.ExitOnError)
	noCookieCheck := flags.Bool("no-syncookie-check", false, "load: redirectors skip the SYN-cookie check")
	noForward := flags.Bool("no-forward", false, "load: redirectors never forward to the second hop (RQ4 contrast)")
	hook := flags.String("hook", "tc", "load: director hook, tc or xdp (xdp only for the hook comparison)")
	second := flags.String("second", "previous", "table: second hops from the 'previous' table, or 'none'")
	role := flags.String("role", "", "cluster: director or redirectors")
	host := flags.String("host", "", "cluster, -role redirectors: this machine's name in cluster.conf")
	netns := flags.String("netns", "", "cluster, -role director: namespace of the interface (emulation only)")
	flags.Parse(os.Args[2:])
	args := flags.Args()
	if *role == "redirectors" {
		pinDir = "/sys/fs/bpf/cs331-" + *host
	}
	switch {
	case cmd == "load" && cfg.Cluster && *role == "director" && len(args) == 0:
		check(loadClusterDirector(cfg, *netns))
	case cmd == "load" && cfg.Cluster && *role == "redirectors" && *host != "" && len(args) == 0:
		check(loadClusterRedirectors(cfg, *host, !*noCookieCheck, !*noForward))
	case cmd == "load" && len(args) == 0 && (*hook == "tc" || *hook == "xdp"):
		check(load(cfg, *hook, !*noCookieCheck, !*noForward))
	case cmd == "table" && len(args) == 1 && (*second == "previous" || *second == "none"):
		check(table(cfg, args[0], *second == "previous"))
	case cmd == "show" && len(args) == 0:
		check(show(cfg))
	case cmd == "dump" && len(args) == 0:
		check(dump(cfg))
	case cmd == "stats" && len(args) == 0:
		check(stats(cfg))
	case cmd == "unload" && len(args) == 0:
		check(os.RemoveAll(pinDir))
		fmt.Println("unloaded")
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: lbctl load [-hook tc|xdp] [-no-syncookie-check] [-no-forward] | table [-second previous|none] equal|NAME=WEIGHT,... | show | dump | stats | unload")
	os.Exit(2)
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "lbctl:", err)
		os.Exit(1)
	}
}

// --- load ---

func load(cfg *testbed.Config, hook string, checkSyncookie, forward bool) error {
	if _, err := os.Stat(pinDir); err == nil {
		return fmt.Errorf("%s exists: already loaded (run 'lbctl unload' first)", pinDir)
	}
	vip, port, lbIP, err := vipConfig(cfg)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(pinDir, 0o700); err != nil {
		return err
	}
	ok := false
	defer func() {
		if !ok { // don't leave half a load behind
			os.RemoveAll(pinDir)
		}
	}()

	director, err := loadDirector(cfg, hook, vip, port, lbIP, backendEntry)
	if err != nil {
		return err
	}
	defer director.Close() // the pins keep what's needed alive
	program := director.Programs[lb.DirectorProgram(hook)]
	if hook == "tc" {
		if err := attachTC("lb", "eth0", program, "director_link"); err != nil {
			return err
		}
	} else {
		if err := attachXDP("lb", "eth0", program, "director_link"); err != nil {
			return err
		}
		pass, err := ebpf.LoadCollection(filepath.Join(objDir, "xdp_pass.o"))
		if err != nil {
			return err
		}
		defer pass.Close()
		if err := attachXDP("rtr", "p-lb", pass.Programs["xdp_pass"], "peer_pass_link"); err != nil {
			return err
		}
	}

	// A redirector on every backend: loaded once per backend, because each
	// one needs its own address and has its own counters.
	for _, be := range cfg.Backends {
		if err := loadRedirector(be, checkSyncookie, forward); err != nil {
			return err
		}
	}

	ok = true
	where := "TC ingress of lb/eth0"
	if hook == "xdp" {
		where = "native XDP on lb/eth0 (+ XDP_PASS on rtr/p-lb)"
	}
	fmt.Printf("loaded: director at %s (VIP %s:%d), redirector on %d backends",
		where, vip, port, len(cfg.Backends))
	if !checkSyncookie {
		fmt.Print(" (SYN-cookie check OFF)")
	}
	if !forward {
		fmt.Print(" (forwarding to the second hop OFF)")
	}
	fmt.Println()
	fmt.Println("no table yet: VIP traffic is dropped until 'lbctl table ...'")
	return nil
}

// loadDirector loads the director, pins its maps and fills the backends map;
// entry gives each backend's address and the MAC to send its packets to.
func loadDirector(cfg *testbed.Config, hook string, vip netip.Addr, port uint16, lbIP netip.Addr,
	entry func(testbed.Backend) (lb.Backend, error)) (*ebpf.Collection, error) {
	director, err := lb.LoadDirector(objDir, hook, vip, port, lbIP)
	if err != nil {
		return nil, fmt.Errorf("loading director: %w", err)
	}
	for _, name := range []string{"tables", "backends", "stats", "backend_packets"} {
		if err := director.Maps[name].Pin(filepath.Join(pinDir, name)); err != nil {
			director.Close()
			return nil, err
		}
	}
	for i, be := range cfg.Backends {
		e, err := entry(be)
		if err == nil {
			err = director.Maps["backends"].Put(uint32(i+1), e)
		}
		if err != nil {
			director.Close()
			return nil, err
		}
	}
	return director, nil
}

// loadClusterDirector: the director at TC ingress of the LB machine's interface.
func loadClusterDirector(cfg *testbed.Config, netns string) error {
	if _, err := os.Stat(pinDir); err == nil {
		return fmt.Errorf("%s exists: already loaded (run 'lbctl unload' first)", pinDir)
	}
	vip, port, lbIP, err := vipConfig(cfg)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(pinDir, 0o700); err != nil {
		return err
	}
	ok := false
	defer func() {
		if !ok {
			os.RemoveAll(pinDir)
		}
	}()
	dev := cfg.Hosts[cfg.Vars["LB"]].Interface
	director, err := loadDirector(cfg, "tc", vip, port, lbIP, func(be testbed.Backend) (lb.Backend, error) {
		return neighbourEntry(cfg, be, netns, dev)
	})
	if err != nil {
		return err
	}
	defer director.Close()
	if err := attachTC(netns, dev, director.Programs[lb.DirectorProgram("tc")], "director_link"); err != nil {
		return err
	}
	ok = true
	fmt.Printf("loaded: director at TC ingress of %s (VIP %s:%d), %d backends\n", dev, vip, port, len(cfg.Backends))
	return nil
}

// neighbourEntry: the backend's own address, and the MAC of the machine it
// runs on (looked up in the LB machine's neighbour table, after a ping).
func neighbourEntry(cfg *testbed.Config, be testbed.Backend, netns, dev string) (lb.Backend, error) {
	ip, err := netip.ParseAddr(be.IP)
	if err != nil {
		return lb.Backend{}, err
	}
	host := cfg.Hosts[be.Host]
	ns := []string{}
	if netns != "" {
		ns = []string{"ip", "netns", "exec", netns}
	}
	ping := append(append([]string{}, ns...), "ping", "-c", "1", "-W", "1", host.Address)
	exec.Command(ping[0], ping[1:]...).Run() // fills the neighbour table; the result doesn't matter
	show := append(append([]string{}, ns...), "ip", "-j", "neigh", "show", host.Address, "dev", dev)
	out, err := exec.Command(show[0], show[1:]...).Output()
	if err != nil {
		return lb.Backend{}, fmt.Errorf("neighbour table for %s: %w", host.Address, err)
	}
	var neigh []struct{ Lladdr string }
	if err := json.Unmarshal(out, &neigh); err != nil || len(neigh) == 0 || neigh[0].Lladdr == "" {
		return lb.Backend{}, fmt.Errorf("no MAC for %s (%s) on %s: is it reachable?", be.Host, host.Address, dev)
	}
	hw, err := net.ParseMAC(neigh[0].Lladdr)
	if err != nil {
		return lb.Backend{}, err
	}
	entry := lb.Backend{IP: ip.As4()}
	copy(entry.MAC[:], hw)
	return entry, nil
}

// loadClusterRedirectors: the redirectors of the backends on this machine.
func loadClusterRedirectors(cfg *testbed.Config, host string, checkSyncookie, forward bool) error {
	if _, err := os.Stat(pinDir); err == nil {
		return fmt.Errorf("%s exists: already loaded (run 'lbctl unload -role redirectors -host %s' first)", pinDir, host)
	}
	if err := os.MkdirAll(pinDir, 0o700); err != nil {
		return err
	}
	n := 0
	for _, be := range cfg.Backends {
		if be.Host != host {
			continue
		}
		if err := loadRedirector(be, checkSyncookie, forward); err != nil {
			os.RemoveAll(pinDir)
			return err
		}
		n++
	}
	fmt.Printf("loaded: %d redirectors on %s (forwarding %v, SYN-cookie check %v)\n", n, host, forward, checkSyncookie)
	return nil
}

func loadRedirector(be testbed.Backend, checkSyncookie, forward bool) error {
	self, err := netip.ParseAddr(be.IP)
	if err != nil {
		return err
	}
	redirector, err := lb.LoadRedirector(objDir, self, checkSyncookie, forward)
	if err != nil {
		return fmt.Errorf("loading redirector for %s: %w", be.Name, err)
	}
	defer redirector.Close()
	if err := redirector.Maps["stats"].Pin(filepath.Join(pinDir, "redirector_"+be.Name+"_stats")); err != nil {
		return err
	}
	return attachTC(be.Name, "eth0", redirector.Programs["redirector"], "redirector_"+be.Name+"_link")
}

func vipConfig(cfg *testbed.Config) (vip netip.Addr, port uint16, lbIP netip.Addr, err error) {
	values := map[string]string{}
	for _, key := range []string{"VIP", "BACKEND_PORT", "LB_IP"} {
		if values[key], err = cfg.Get(key); err != nil {
			return
		}
	}
	if vip, err = netip.ParseAddr(values["VIP"]); err != nil {
		return
	}
	if lbIP, err = netip.ParseAddr(values["LB_IP"]); err != nil {
		return
	}
	p, err := strconv.ParseUint(values["BACKEND_PORT"], 10, 16)
	return vip, uint16(p), lbIP, err // direct server return: clients use the backends' port
}

// backendEntry builds a backends-map entry: the backend's IP and its eth0 MAC
// (the director writes that MAC as the destination of the outer frame).
func backendEntry(be testbed.Backend) (lb.Backend, error) {
	ip, err := netip.ParseAddr(be.IP)
	if err != nil {
		return lb.Backend{}, err
	}
	out, err := exec.Command("ip", "-n", be.Name, "-j", "link", "show", "dev", "eth0").Output()
	if err != nil {
		return lb.Backend{}, fmt.Errorf("reading %s's MAC (is the testbed up?): %w", be.Name, err)
	}
	var links []struct{ Address string }
	if err := json.Unmarshal(out, &links); err != nil || len(links) != 1 {
		return lb.Backend{}, fmt.Errorf("parsing 'ip link' output for %s: %v", be.Name, err)
	}
	hw, err := net.ParseMAC(links[0].Address)
	if err != nil {
		return lb.Backend{}, err
	}
	entry := lb.Backend{IP: ip.As4()}
	copy(entry.MAC[:], hw)
	return entry, nil
}

func attachXDP(netns, dev string, prog *ebpf.Program, pinName string) error {
	return testbed.InNetns(netns, func() error {
		ifindex, err := ifindexOf(dev)
		if err != nil {
			return err
		}
		// Native (driver) mode only: if that fails we want to know, not
		// silently fall back to the slower generic mode.
		l, err := link.AttachXDP(link.XDPOptions{Program: prog, Interface: ifindex, Flags: link.XDPDriverMode})
		if err != nil {
			return fmt.Errorf("attaching XDP to %s/%s: %w", netns, dev, err)
		}
		defer l.Close()
		return l.Pin(filepath.Join(pinDir, pinName))
	})
}

// attachTC attaches prog to TC ingress (TCX) of dev in namespace netns and pins the link.
func attachTC(netns, dev string, prog *ebpf.Program, pinName string) error {
	return testbed.InNetns(netns, func() error {
		ifindex, err := ifindexOf(dev)
		if err != nil {
			return err
		}
		l, err := link.AttachTCX(link.TCXOptions{Interface: ifindex, Program: prog, Attach: ebpf.AttachTCXIngress})
		if err != nil {
			return fmt.Errorf("attaching TC program to %s/%s: %w", netns, dev, err)
		}
		defer l.Close()
		return l.Pin(filepath.Join(pinDir, pinName))
	})
}

func ifindexOf(dev string) (int, error) {
	iface, err := net.InterfaceByName(dev)
	if err != nil {
		return 0, err
	}
	return iface.Index, nil
}

// --- table, show, stats ---

func pinnedMap(name string) (*ebpf.Map, error) {
	m, err := ebpf.LoadPinnedMap(filepath.Join(pinDir, name), nil)
	if err != nil {
		return nil, fmt.Errorf("%w (run 'lbctl load' first)", err)
	}
	return m, nil
}

func table(cfg *testbed.Config, spec string, secondHops bool) error {
	ids := make([]uint32, len(cfg.Backends))
	weights := make([]float64, len(cfg.Backends))
	for i := range cfg.Backends {
		ids[i] = uint32(i + 1)
		if spec == "equal" {
			weights[i] = 1
		}
	}
	if spec != "equal" {
		for _, part := range strings.Split(spec, ",") {
			name, w, ok := strings.Cut(part, "=")
			i := backendIndex(cfg, name)
			weight, err := strconv.ParseFloat(w, 64)
			if !ok || i < 0 || err != nil {
				return fmt.Errorf("bad weight %q (want NAME=WEIGHT with a name from backends.conf)", part)
			}
			weights[i] = weight
		}
	}
	buckets, err := lb.StaticTable(ids, weights)
	if err != nil {
		return err
	}
	tables, err := pinnedMap("tables")
	if err != nil {
		return err
	}
	defer tables.Close()
	if prev, _, err := lb.Active(tables); err == nil && secondHops {
		buckets = lb.WithSecondHops(prev, buckets)
	}
	gen, err := lb.Publish(tables, buckets)
	if err != nil {
		return err
	}
	fmt.Printf("published table generation %d\n", gen)
	return show(cfg)
}

func backendIndex(cfg *testbed.Config, name string) int {
	for i, be := range cfg.Backends {
		if be.Name == name {
			return i
		}
	}
	return -1
}

func backendName(cfg *testbed.Config, id uint32) string {
	if id >= 1 && int(id) <= len(cfg.Backends) {
		return cfg.Backends[id-1].Name
	}
	return fmt.Sprintf("id%d", id)
}

func show(cfg *testbed.Config) error {
	tables, err := pinnedMap("tables")
	if err != nil {
		return err
	}
	defer tables.Close()
	buckets, gen, err := lb.Active(tables)
	if err != nil {
		return err
	}
	first, second := map[uint32]int{}, map[uint32]int{}
	for _, b := range buckets {
		first[b.First]++
		second[b.Second]++
	}
	fmt.Printf("table generation %d, %d buckets\n", gen, len(buckets))
	fmt.Printf("  %-8s %13s %13s\n", "backend", "first hop", "second hop")
	for i := range cfg.Backends {
		id := uint32(i + 1)
		fmt.Printf("  %-8s %6d %5.1f%% %13d\n", backendName(cfg, id),
			first[id], 100*float64(first[id])/float64(len(buckets)), second[id])
	}
	if first[0] > 0 {
		fmt.Printf("  %d buckets have no first hop (their traffic is dropped)\n", first[0])
	}
	return nil
}

func dump(cfg *testbed.Config) error {
	tables, err := pinnedMap("tables")
	if err != nil {
		return err
	}
	defer tables.Close()
	buckets, gen, err := lb.Active(tables)
	if err != nil {
		return err
	}
	fmt.Printf("# generation %d\n# bucket first second\n", gen)
	for i, b := range buckets {
		second := "-"
		if b.Second != 0 {
			second = backendName(cfg, b.Second)
		}
		fmt.Printf("%d %s %s\n", i, backendName(cfg, b.First), second)
	}
	return nil
}

func stats(cfg *testbed.Config) error {
	counters, err := pinnedMap("stats")
	if err != nil {
		return err
	}
	defer counters.Close()
	for i, name := range lb.StatNames {
		n, err := lb.SumPerCPU(counters, uint32(i))
		if err != nil {
			return err
		}
		fmt.Printf("%-12s %d\n", name, n)
	}
	perBackend, err := pinnedMap("backend_packets")
	if err != nil {
		return err
	}
	defer perBackend.Close()
	fmt.Println("packets sent per backend:")
	for i := range cfg.Backends {
		n, err := lb.SumPerCPU(perBackend, uint32(i+1))
		if err != nil {
			return err
		}
		fmt.Printf("  %-8s %d\n", cfg.Backends[i].Name, n)
	}

	fmt.Printf("redirectors:\n  %-8s", "")
	for _, name := range lb.RedirectorStatNames {
		fmt.Printf(" %14s", name)
	}
	fmt.Println()
	for _, be := range cfg.Backends {
		m, err := pinnedMap("redirector_" + be.Name + "_stats")
		if err != nil { // cluster: the redirectors' counters are on the backend machines
			fmt.Printf("  %-8s (not on this machine)\n", be.Name)
			continue
		}
		fmt.Printf("  %-8s", be.Name)
		for i := range lb.RedirectorStatNames {
			n, err := lb.SumPerCPU(m, uint32(i))
			if err != nil {
				m.Close()
				return err
			}
			fmt.Printf(" %14d", n)
		}
		fmt.Println()
		m.Close()
	}
	return nil
}
