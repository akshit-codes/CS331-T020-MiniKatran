// xdp load balancer: parse, look up flow, rewrite dst (or src for replies), fix checksums, XDP_TX
#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/in.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "../common/lb.h"
#include "../common/hash.h"
#include "../common/ring.h"

struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, MAX_VIPS);
	__type(key, struct vip_key);
	__type(value, struct vip_info);
} vips SEC(".maps");

// one ring per vip, laid out back to back: slot = vip num * RING_SIZE + hash % RING_SIZE
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, MAX_VIPS * RING_SIZE);
	__type(key, __u32);
	__type(value, __u32);
} ring SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, MAX_BACKENDS);
	__type(key, __u32);
	__type(value, struct backend);
} backends SEC(".maps");

// connection table. LRU so old flows fall out by themselves when it fills up
struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, MAX_FLOWS);
	__type(key, struct flow_key);
	__type(value, struct flow_val);
} flows SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, ST_MAX);
	__type(key, __u32);
	__type(value, struct counter);
} stats SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, MAX_BACKENDS);
	__type(key, __u32);
	__type(value, struct counter);
} be_stats SEC(".maps");

static __always_inline void count(void *map, __u32 idx, __u32 len)
{
	struct counter *c = bpf_map_lookup_elem(map, &idx);
	if (c) {
		c->pkts++;
		c->bytes += len;
	}
}

// rfc 1624 incremental update: HC' = ~(~HC + ~m + m'), done in 16 bit halves.
// works for both the ip header checksum and the tcp one (the ip addresses are
// part of the tcp pseudo header) and doesn't care about byte order
static __always_inline __u16 csum_replace4(__u16 check, __be32 old, __be32 new)
{
	__u32 sum = (__u16)~check;
	sum += (__u16)~old + (__u16)(~old >> 16);
	sum += (__u16)new + (__u16)(new >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	return ~sum;
}

SEC("xdp")
int lb(struct xdp_md *ctx)
{
	void *data = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;
	__u32 len = data_end - data;
	struct ethhdr *eth = data;
	struct iphdr *ip;
	struct tcphdr *tcp;
	struct flow_key fk = {};
	struct flow_val f, *fv;
	__be32 old;

	count(&stats, ST_PKTS, len);

	// verifier needs a bounds check before every header access
	if ((void *)(eth + 1) > data_end)
		goto pass;
	if (eth->h_proto != bpf_htons(ETH_P_IP))
		goto pass;
	ip = (void *)(eth + 1);
	if ((void *)(ip + 1) > data_end)
		goto pass;
	// no ip options, no fragments, tcp only
	if (ip->ihl != 5 || ip->protocol != IPPROTO_TCP || ip->frag_off & bpf_htons(0x3fff))
		goto pass;
	tcp = (void *)(ip + 1);
	if ((void *)(tcp + 1) > data_end)
		goto pass;

	fk.saddr = ip->saddr;
	fk.daddr = ip->daddr;
	fk.sport = tcp->source;
	fk.dport = tcp->dest;
	fk.proto = ip->protocol;

	// SYN = new connection, don't trust the table (same as katran)
	fv = tcp->syn && !tcp->ack ? NULL : bpf_map_lookup_elem(&flows, &fk);
	if (fv) {
		fv->ts = bpf_ktime_get_ns();
		f = *fv;
		count(&stats, ST_HIT, len);
	} else {
		struct vip_key vk = { .addr = ip->daddr, .port = tcp->dest, .proto = IPPROTO_TCP };
		struct vip_info *vip = bpf_map_lookup_elem(&vips, &vk);
		struct flow_key rk = {};
		struct flow_val r = {};
		struct backend *be;
		__u32 slot, *id, h;
		__u16 sport;

		// only packets for a vip can start a flow, anything else is not ours
		if (!vip)
			goto pass;
		sport = vip->flags & VIP_NO_SPORT ? 0 : bpf_ntohs(tcp->source);
		h = flow_hash(ip->saddr, sport, bpf_ntohs(tcp->dest), vip->num);
		slot = vip->num * RING_SIZE + h % RING_SIZE;
		id = bpf_map_lookup_elem(&ring, &slot);
		if (!id || *id == NO_BACKEND)
			goto nobe;
		be = bpf_map_lookup_elem(&backends, id);
		if (!be || !be->addr)
			goto nobe;

		f.addr = be->addr;
		__builtin_memcpy(f.mac, be->mac, 6);
		f.dir = DIR_FWD;
		f.pad = 0;
		f.be = *id;
		f.ts = bpf_ktime_get_ns();

		// reverse entry for the replies, keeps the client mac so no arp needed
		rk.saddr = be->addr;
		rk.daddr = ip->saddr;
		rk.sport = tcp->dest;
		rk.dport = tcp->source;
		rk.proto = IPPROTO_TCP;
		r.addr = ip->daddr;
		__builtin_memcpy(r.mac, eth->h_source, 6);
		r.dir = DIR_REV;
		r.be = *id;
		r.ts = f.ts;

		bpf_map_update_elem(&flows, &fk, &f, BPF_ANY);
		bpf_map_update_elem(&flows, &rk, &r, BPF_ANY);
		count(&stats, ST_NEW, len);
	}

	if (f.dir == DIR_FWD) {
		old = ip->daddr;
		ip->daddr = f.addr;
	} else {
		old = ip->saddr;
		ip->saddr = f.addr;
	}
	ip->check = csum_replace4(ip->check, old, f.addr);
	tcp->check = csum_replace4(tcp->check, old, f.addr);
	__builtin_memcpy(eth->h_source, eth->h_dest, 6);
	__builtin_memcpy(eth->h_dest, f.mac, 6);

	count(&be_stats, f.be, len);
	count(&stats, ST_TX, len);
	return XDP_TX;

nobe:
	count(&stats, ST_NOBE, len);
	return XDP_DROP;
pass:
	count(&stats, ST_PASS, len);
	return XDP_PASS;
}

char _license[] SEC("license") = "GPL";
