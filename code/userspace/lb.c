// lb control plane, talks to the xdp program through pinned maps
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <libgen.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <linux/if_link.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include "../common/lb.h"
#include "../common/ring.h"

#define PIN "/sys/fs/bpf/mklb"

static const char *maps[] = { "vips", "ring", "backends", "flows", "stats", "be_stats", NULL };

static void die(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	exit(1);
}

// bpf/lb_kern.o relative to where the binary lives
static const char *obj_path(void)
{
	static char exe[512], path[512];
	ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
	if (n < 0) die("readlink: %s", strerror(errno));
	exe[n] = 0;
	snprintf(path, sizeof path, "%s/../bpf/lb_kern.o", dirname(exe));
	return path;
}

static int map_fd(const char *name)
{
	char p[128];
	int fd;
	snprintf(p, sizeof p, PIN "/%s", name);
	fd = bpf_obj_get(p);
	if (fd < 0) die("map %s: %s (is the lb loaded?)", name, strerror(errno));
	return fd;
}

static void parse_vip(const char *s, struct vip_key *k)
{
	char buf[64], *p;
	snprintf(buf, sizeof buf, "%s", s);
	p = strchr(buf, ':');
	if (!p) die("vip should be ip:port");
	*p++ = 0;
	memset(k, 0, sizeof *k);
	if (inet_pton(AF_INET, buf, &k->addr) != 1) die("bad ip %s", buf);
	k->port = htons(atoi(p));
	k->proto = IPPROTO_TCP;
}

static __be32 parse_ip(const char *s)
{
	__be32 a;
	if (inet_pton(AF_INET, s, &a) != 1) die("bad ip %s", s);
	return a;
}

static void parse_mac(const char *s, __u8 *m)
{
	if (sscanf(s, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", m, m + 1, m + 2, m + 3, m + 4, m + 5) != 6)
		die("bad mac %s", s);
}

static const char *ipstr(__be32 a)
{
	static char buf[4][32];
	static int i;
	i = (i + 1) % 4;
	inet_ntop(AF_INET, &a, buf[i], 32);
	return buf[i];
}

static const char *macstr(const __u8 *m)
{
	static char buf[32];
	snprintf(buf, sizeof buf, "%02x:%02x:%02x:%02x:%02x:%02x", m[0], m[1], m[2], m[3], m[4], m[5]);
	return buf;
}

static int cmd_load(const char *iface)
{
	struct bpf_object *obj;
	struct bpf_program *prog;
	int ifindex, err;

	obj = bpf_object__open_file(obj_path(), NULL);
	if (!obj) die("open %s: %s", obj_path(), strerror(errno));
	if (bpf_object__load(obj)) die("load failed (see verifier log above)");
	if (bpf_object__pin_maps(obj, PIN)) die("pin maps in " PIN ": %s", strerror(errno));

	prog = bpf_object__find_program_by_name(obj, "lb");
	ifindex = if_nametoindex(iface);
	if (!ifindex) die("no interface %s", iface);
	err = bpf_xdp_attach(ifindex, bpf_program__fd(prog), XDP_FLAGS_DRV_MODE, NULL);
	if (err) die("attach to %s: %s", iface, strerror(-err));
	printf("lb attached to %s (native xdp), maps pinned under %s\n", iface, PIN);
	return 0;
}

static int cmd_unload(const char *iface)
{
	int ifindex = if_nametoindex(iface);
	char p[128];
	if (ifindex) bpf_xdp_detach(ifindex, XDP_FLAGS_DRV_MODE, NULL);
	for (int i = 0; maps[i]; i++) {
		snprintf(p, sizeof p, PIN "/%s", maps[i]);
		unlink(p);
	}
	rmdir(PIN);
	return 0;
}

static int cmd_add_vip(const char *s, __u32 flags)
{
	struct vip_key k, cur, next;
	struct vip_info v = { .flags = flags };
	int fd = map_fd("vips"), used[MAX_VIPS] = { 0 };
	void *prev = NULL;

	parse_vip(s, &k);
	// lowest ring number nobody uses yet
	while (!bpf_map_get_next_key(fd, prev, &next)) {
		struct vip_info vi;
		if (!bpf_map_lookup_elem(fd, &next, &vi) && vi.num < MAX_VIPS) used[vi.num] = 1;
		cur = next;
		prev = &cur;
	}
	while (v.num < MAX_VIPS && used[v.num]) v.num++;
	if (v.num == MAX_VIPS) die("no free vip slots");
	if (bpf_map_update_elem(fd, &k, &v, BPF_NOEXIST)) die("add vip: %s", strerror(errno));
	printf("vip %s -> ring %u%s\n", s, v.num, flags & VIP_NO_SPORT ? " (no sport)" : "");
	return 0;
}

// backend id for an ip, optionally creating it in the first free slot
static int find_backend(int bfd, __be32 addr, const __u8 *mac)
{
	struct backend b;
	__u32 i, free = MAX_BACKENDS;

	for (i = 0; i < MAX_BACKENDS; i++) {
		bpf_map_lookup_elem(bfd, &i, &b);
		if (b.addr == addr) {
			if (mac && memcmp(b.mac, mac, 6)) {
				memcpy(b.mac, mac, 6);
				bpf_map_update_elem(bfd, &i, &b, BPF_ANY);
			}
			return i;
		}
		if (!b.addr && free == MAX_BACKENDS) free = i;
	}
	if (!mac || free == MAX_BACKENDS) return -1;
	memset(&b, 0, sizeof b);
	b.addr = addr;
	memcpy(b.mac, mac, 6);
	bpf_map_update_elem(bfd, &free, &b, BPF_ANY);
	return free;
}

// which backends are on a vip's ring
static int ring_backends(int rfd, __u32 num, __u32 *ids)
{
	int n = 0;
	for (__u32 j = 0; j < RING_SIZE; j++) {
		__u32 slot = num * RING_SIZE + j, id, k;
		bpf_map_lookup_elem(rfd, &slot, &id);
		if (id == NO_BACKEND) continue;
		for (k = 0; k < n; k++)
			if (ids[k] == id) break;
		if (k == n) ids[n++] = id;
	}
	return n;
}

static void write_ring(int rfd, int bfd, __u32 num, __u32 *ids, int n)
{
	static __u32 ring[RING_SIZE];
	__u32 ips[MAX_BACKENDS];
	struct backend b;

	for (int i = 0; i < n; i++) {
		bpf_map_lookup_elem(bfd, &ids[i], &b);
		ips[i] = b.addr;
	}
	build_ring(ips, ids, n, ring);
	for (__u32 j = 0; j < RING_SIZE; j++) {
		__u32 slot = num * RING_SIZE + j;
		bpf_map_update_elem(rfd, &slot, &ring[j], BPF_ANY);
	}
}

static __u32 vip_num(int vfd, const char *s)
{
	struct vip_key k;
	struct vip_info vi;
	parse_vip(s, &k);
	if (bpf_map_lookup_elem(vfd, &k, &vi)) die("no such vip %s", s);
	return vi.num;
}

static int cmd_add_backend(const char *vip, const char *ip, const char *macs)
{
	int vfd = map_fd("vips"), rfd = map_fd("ring"), bfd = map_fd("backends");
	__u32 num = vip_num(vfd, vip), ids[MAX_BACKENDS];
	__u8 mac[6];
	int id, n, k;

	parse_mac(macs, mac);
	id = find_backend(bfd, parse_ip(ip), mac);
	if (id < 0) die("backend table full");
	n = ring_backends(rfd, num, ids);
	for (k = 0; k < n; k++)
		if (ids[k] == id) break;
	if (k == n) ids[n++] = id;
	write_ring(rfd, bfd, num, ids, n);
	printf("vip %s: backend %d (%s %s) added, %d backends on ring %u\n", vip, id, ip, macs, n, num);
	return 0;
}

static int cmd_del_backend(const char *vip, const char *ip)
{
	int vfd = map_fd("vips"), rfd = map_fd("ring"), bfd = map_fd("backends");
	__u32 num = vip_num(vfd, vip), ids[MAX_BACKENDS];
	int id = find_backend(bfd, parse_ip(ip), NULL), n, k;

	if (id < 0) die("unknown backend %s", ip);
	n = ring_backends(rfd, num, ids);
	for (k = 0; k < n; k++)
		if (ids[k] == id) break;
	if (k == n) die("%s is not a backend of %s", ip, vip);
	ids[k] = ids[--n];
	write_ring(rfd, bfd, num, ids, n);
	printf("vip %s: backend %d (%s) removed, %d backends left on ring %u\n", vip, id, ip, n, num);
	return 0;
}

static int cmd_list(void)
{
	struct vip_key cur, next;
	struct vip_info vi;
	int vfd = map_fd("vips"), rfd = map_fd("ring"), bfd = map_fd("backends");
	void *prev = NULL;

	while (!bpf_map_get_next_key(vfd, prev, &next)) {
		__u32 cnt[MAX_BACKENDS] = { 0 };
		bpf_map_lookup_elem(vfd, &next, &vi);
		printf("vip %s:%u  ring %u  flags %s\n", ipstr(next.addr), ntohs(next.port), vi.num,
		       vi.flags & VIP_NO_SPORT ? "no-sport" : "-");
		for (__u32 j = 0; j < RING_SIZE; j++) {
			__u32 slot = vi.num * RING_SIZE + j, id;
			bpf_map_lookup_elem(rfd, &slot, &id);
			if (id < MAX_BACKENDS) cnt[id]++;
		}
		for (__u32 i = 0; i < MAX_BACKENDS; i++) {
			struct backend b;
			if (!cnt[i]) continue;
			bpf_map_lookup_elem(bfd, &i, &b);
			printf("   backend %-2u %-15s %s  %5.1f%% of ring\n", i, ipstr(b.addr), macstr(b.mac),
			       100.0 * cnt[i] / RING_SIZE);
		}
		cur = next;
		prev = &cur;
	}
	return 0;
}

static int cmd_flows(void)
{
	struct flow_key cur, next;
	struct flow_val v;
	struct timespec ts;
	int fd = map_fd("flows"), n = 0;
	void *prev = NULL;
	__u64 now;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	now = ts.tv_sec * 1000000000ull + ts.tv_nsec;
	while (!bpf_map_get_next_key(fd, prev, &next)) {
		if (!bpf_map_lookup_elem(fd, &next, &v)) {
			long long age = v.ts > now ? 0 : now - v.ts;
			printf("%-15s:%-5u -> %-15s:%-5u  %s  backend %u  %5.1fs ago\n", ipstr(next.saddr),
			       ntohs(next.sport), ipstr(next.daddr), ntohs(next.dport),
			       v.dir == DIR_FWD ? "fwd" : "rev", v.be, age / 1e9);
			n++;
		}
		cur = next;
		prev = &cur;
	}
	printf("%d entries\n", n);
	return 0;
}

static void sum_counter(int fd, __u32 i, int ncpu, __u64 *pkts, __u64 *bytes)
{
	struct counter vals[ncpu];
	*pkts = *bytes = 0;
	if (bpf_map_lookup_elem(fd, &i, vals)) return;
	for (int c = 0; c < ncpu; c++) {
		*pkts += vals[c].pkts;
		*bytes += vals[c].bytes;
	}
}

static int cmd_stats(void)
{
	static const char *names[ST_MAX] = { "packets", "passed", "new flows", "flow hits", "sent (tx)", "no backend" };
	int fd = map_fd("stats"), bsfd = map_fd("be_stats"), bfd = map_fd("backends");
	int ncpu = libbpf_num_possible_cpus();
	__u64 pkts, bytes;

	for (__u32 i = 0; i < ST_MAX; i++) {
		sum_counter(fd, i, ncpu, &pkts, &bytes);
		printf("%-12s %12llu pkts %15llu bytes\n", names[i], pkts, bytes);
	}
	for (__u32 i = 0; i < MAX_BACKENDS; i++) {
		struct backend b;
		bpf_map_lookup_elem(bfd, &i, &b);
		if (!b.addr) continue;
		sum_counter(bsfd, i, ncpu, &pkts, &bytes);
		printf("backend %-2u %-15s %8llu pkts %15llu bytes\n", i, ipstr(b.addr), pkts, bytes);
	}
	return 0;
}

static void usage(void)
{
	fprintf(stderr, "usage: lb load <iface> | unload <iface>\n"
			"          add-vip <ip:port> [-s]\n"
			"          add-backend <vip ip:port> <ip> <mac>\n"
			"          del-backend <vip ip:port> <ip>\n"
			"          list | flows | stats\n");
	exit(1);
}

int main(int argc, char **argv)
{
	if (argc < 2) usage();
	if (!strcmp(argv[1], "load") && argc == 3) return cmd_load(argv[2]);
	if (!strcmp(argv[1], "unload") && argc == 3) return cmd_unload(argv[2]);
	if (!strcmp(argv[1], "add-vip") && argc >= 3)
		return cmd_add_vip(argv[2], argc > 3 && !strcmp(argv[3], "-s") ? VIP_NO_SPORT : 0);
	if (!strcmp(argv[1], "add-backend") && argc == 5) return cmd_add_backend(argv[2], argv[3], argv[4]);
	if (!strcmp(argv[1], "del-backend") && argc == 4) return cmd_del_backend(argv[2], argv[3]);
	if (!strcmp(argv[1], "list")) return cmd_list();
	if (!strcmp(argv[1], "flows")) return cmd_flows();
	if (!strcmp(argv[1], "stats")) return cmd_stats();
	usage();
	return 1;
}
