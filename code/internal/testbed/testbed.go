// Package testbed reads the testbed layout: config.env and backends.conf for
// the single-laptop namespace testbed (the same files the shell scripts and
// bench/testbed.py read), or, if the environment variable CS331_CLUSTER names
// one, a cluster.conf describing real machines (docs/cluster-runbook.md).
package testbed

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type Backend struct {
	Name, IP, CPUs, Kind string
	Host                 string // cluster only: the machine it runs on
}

// Host is a machine of the cluster.
type Host struct {
	Name, Address, Interface string
}

type Config struct {
	Vars     map[string]string // KEY=VALUE lines of config.env (or cluster.conf)
	Backends []Backend         // in file order; backend id = index + 1
	Cluster  bool              // real machines: backends are reached over the network
	Hosts    map[string]Host   // cluster only
}

// Load reads config.env and backends.conf from the repo root, or the file
// named by CS331_CLUSTER.
func Load(repo string) (*Config, error) {
	if path := os.Getenv("CS331_CLUSTER"); path != "" {
		if !filepath.IsAbs(path) {
			path = filepath.Join(repo, path)
		}
		return loadCluster(path)
	}
	c := &Config{Vars: map[string]string{}}
	err := eachLine(filepath.Join(repo, "config.env"), func(line string) error {
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			return fmt.Errorf("not KEY=VALUE: %q", line)
		}
		c.Vars[strings.TrimSpace(key)] = strings.TrimSpace(value)
		return nil
	})
	if err != nil {
		return nil, err
	}
	err = eachLine(filepath.Join(repo, "backends.conf"), func(line string) error {
		f := strings.Fields(line)
		if len(f) != 4 {
			return fmt.Errorf("want 'name ip cpus kind': %q", line)
		}
		c.Backends = append(c.Backends, Backend{Name: f[0], IP: f[1], CPUs: f[2], Kind: f[3]})
		return nil
	})
	return c, err
}

// loadCluster reads a cluster.conf: KEY=VALUE settings, plus lines
// "host NAME ADDRESS INTERFACE HOUSEKEEPING_CPUS" and
// "backend NAME HOST ADDRESS CPUS KIND [SLOWNESS]".
func loadCluster(path string) (*Config, error) {
	c := &Config{Vars: map[string]string{}, Cluster: true, Hosts: map[string]Host{}}
	err := eachLine(path, func(line string) error {
		f := strings.Fields(line)
		switch {
		case f[0] == "host" && len(f) == 5:
			c.Hosts[f[1]] = Host{Name: f[1], Address: f[2], Interface: f[3]}
		case f[0] == "backend" && len(f) >= 6:
			c.Backends = append(c.Backends, Backend{Name: f[1], Host: f[2], IP: f[3], CPUs: f[4], Kind: f[5]})
		case strings.Contains(line, "="):
			key, value, _ := strings.Cut(line, "=")
			c.Vars[strings.TrimSpace(key)] = strings.TrimSpace(value)
		default:
			return fmt.Errorf("can't read %q", line)
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	lb, ok := c.Hosts[c.Vars["LB"]]
	if !ok {
		return nil, fmt.Errorf("%s: LB=%q is not a host", path, c.Vars["LB"])
	}
	c.Vars["LB_IP"] = lb.Address // the director's outer source address
	return c, nil
}

// Get returns a config.env value, or an error naming the missing key.
func (c *Config) Get(key string) (string, error) {
	v, ok := c.Vars[key]
	if !ok {
		return "", fmt.Errorf("config.env has no %s", key)
	}
	return v, nil
}

// eachLine calls fn for every line with comments (# ...) and blanks removed.
func eachLine(path string, fn func(string) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	s := bufio.NewScanner(f)
	for s.Scan() {
		line, _, _ := strings.Cut(s.Text(), "#")
		if line = strings.TrimSpace(line); line != "" {
			if err := fn(line); err != nil {
				return fmt.Errorf("%s: %w", path, err)
			}
		}
	}
	return s.Err()
}

// ExpandCPUs turns "8,10" or "16-19" into the list of CPU numbers.
func ExpandCPUs(list string) []int {
	var cpus []int
	for _, part := range strings.Split(list, ",") {
		lo, hi, isRange := strings.Cut(part, "-")
		a, _ := strconv.Atoi(lo)
		b := a
		if isRange {
			b, _ = strconv.Atoi(hi)
		}
		for c := a; c <= b; c++ {
			cpus = append(cpus, c)
		}
	}
	return cpus
}
