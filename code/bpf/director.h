/* The load balancer ("director"), the part that doesn't depend on the hook.
 * director_tc.c (TC ingress, what we use, D35) and director_xdp.c (XDP, kept
 * for one side measurement) only differ in how they make room for the new
 * headers and send the packet; both produce byte-identical packets.
 *
 * For every TCP packet to VIP:VIP_PORT:
 *   1. hash the 4-tuple to one of NUM_BUCKETS buckets,
 *   2. look the bucket up in the active table to get its first and second hop,
 *   3. put the packet into our encapsulation (outer IPv4 + UDP + encap_hdr,
 *      see lb.h) addressed to the first hop,
 *   4. send it back out of the same interface.
 * The backend removes the encapsulation and replies to the client directly,
 * so replies never pass through here. The LB keeps no per-connection state:
 * the same 4-tuple always hashes to the same bucket, and the table only
 * changes when the controller publishes a new one.
 *
 * Everything not addressed to the VIP is passed to the kernel unchanged.
 */
#ifndef DIRECTOR_H
#define DIRECTOR_H

#include "lb.h"

/* Set by the loader before loading (internal/lb), in network byte order. */
volatile const __be32 VIP;
volatile const __be16 VIP_PORT;
volatile const __be32 LB_IP;  /* outer source address */

/* The active bucket table. The loader builds a complete new inner table and
 * then swaps it in with one update of slot 0, so a packet always sees either
 * the old table or the new one, never a half-written mix (D6). */
struct table {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, NUM_BUCKETS + 1);  /* + GEN_SLOT */
	/* Sizes, not __type(): clang 21 emits a nested map's value type only as a
	 * forward declaration in BTF, which the loader can't size. */
	__uint(key_size, sizeof(__u32));
	__uint(value_size, sizeof(struct bucket));
};

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY_OF_MAPS);
	__uint(max_entries, 1);
	__type(key, __u32);
	__array(values, struct table);
} tables SEC(".maps");

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, MAX_BACKENDS);
	__type(key, __u32);
	__type(value, struct backend);
} backends SEC(".maps");

/* Counters, one copy per CPU (no locking needed); user space adds them up. */
enum {
	STAT_PASS,         /* not for the VIP: handed to the kernel */
	STAT_TX,           /* encapsulated and sent to a backend */
	STAT_DROP_PROTO,   /* for the VIP but not TCP to VIP_PORT, or a fragment */
	STAT_DROP_NO_HOP,  /* no table published, or the bucket has no first hop */
	STAT_DROP_ERROR,   /* truncated packet or no room to add the header */
	NUM_STATS,
};

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, NUM_STATS);
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

/* Packets sent to each backend, indexed by backend id. */
struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, MAX_BACKENDS);
	__type(key, __u32);
	__type(value, __u64);
} backend_packets SEC(".maps");

static __always_inline void count(__u32 stat)
{
	__u64 *n = bpf_map_lookup_elem(&stats, &stat);

	if (n)
		*n += 1;
}

enum verdict { PASS, DROP, ENCAP };

/* Everything step 3 needs, decided in steps 1 and 2. */
struct choice {
	__u32 first_id;
	__be32 first_ip;
	__u8 first_mac[ETH_ALEN];
	__u8 lb_mac[ETH_ALEN];  /* the frame was sent to us: its destination MAC */
	__be32 second_ip;       /* 0 = no second hop */
	__u32 hash;
	__u32 bucket;
	__u32 gen;
	__u32 inner_len;        /* the IPv4 packet's length (tot_len) */
	__u32 padding;          /* bytes after it in the frame, to cut off before encapsulating */
};

/* Steps 1 and 2. data..data_end must hold the headers; frame_len is the
 * frame's full length (for TC it can be longer than data_end - data). Counts
 * PASS and DROP verdicts itself. */
static __always_inline enum verdict choose(void *data, void *data_end, __u32 frame_len, struct choice *c)
{
	/* --- 1. Is it TCP for VIP:VIP_PORT? --- */
	struct ethhdr *eth = data;
	if ((void *)(eth + 1) > data_end || eth->h_proto != bpf_htons(ETH_P_IP))
		goto pass;
	struct iphdr *ip = (void *)(eth + 1);
	if ((void *)(ip + 1) > data_end || ip->daddr != VIP)
		goto pass;
	/* Fragments have no (or not all) port numbers, so they can't be hashed (D7).
	 * IP options (ihl > 5): the redirector can't look inside such a packet and
	 * drops it, so a connection using them could never work; drop it here (D40). */
	if (ip->protocol != IPPROTO_TCP || ip->ihl != 5 ||
	    (ip->frag_off & bpf_htons(IP_MF | IP_OFFSET))) {
		count(STAT_DROP_PROTO);
		return DROP;
	}
	struct tcphdr *tcp = (void *)(ip + 1);
	if ((void *)(tcp + 1) > data_end) {
		count(STAT_DROP_ERROR);
		return DROP;
	}
	if (tcp->dest != VIP_PORT) {
		count(STAT_DROP_PROTO);
		return DROP;
	}

	/* The IPv4 packet is tot_len bytes long. The frame may hold more: Ethernet
	 * pads short frames to 60 bytes (a bare ACK is 54), and we must not carry
	 * that padding inside our packet. */
	__u32 received = frame_len - sizeof(struct ethhdr);
	c->inner_len = bpf_ntohs(ip->tot_len);
	if (c->inner_len < sizeof(struct iphdr) + sizeof(struct tcphdr) || c->inner_len > received) {
		count(STAT_DROP_ERROR);
		return DROP;
	}
	c->padding = received - c->inner_len;

	/* --- 2. Bucket -> hops --- */
	c->hash = flow_hash(bpf_ntohl(ip->saddr), bpf_ntohl(ip->daddr),
			    bpf_ntohs(tcp->source), bpf_ntohs(tcp->dest));
	c->bucket = c->hash & (NUM_BUCKETS - 1);
	__u32 zero = 0, gen_slot = GEN_SLOT;

	void *table = bpf_map_lookup_elem(&tables, &zero);
	if (!table)
		goto no_hop;
	struct bucket *b = bpf_map_lookup_elem(table, &c->bucket);
	struct bucket *gen = bpf_map_lookup_elem(table, &gen_slot);
	if (!b || !gen)
		goto no_hop;
	c->gen = gen->first;  /* GEN_SLOT keeps the generation in .first */

	c->first_id = b->first;
	struct backend *first = bpf_map_lookup_elem(&backends, &c->first_id);
	if (!first || first->ip == 0)
		goto no_hop;
	c->first_ip = first->ip;
	__builtin_memcpy(c->first_mac, first->mac, ETH_ALEN);

	__u32 second_id = b->second;
	struct backend *second = bpf_map_lookup_elem(&backends, &second_id);
	c->second_ip = (second_id != 0 && second) ? second->ip : 0;

	__builtin_memcpy(c->lb_mac, eth->h_dest, ETH_ALEN);
	return ENCAP;

pass:
	count(STAT_PASS);
	return PASS;
no_hop:
	count(STAT_DROP_NO_HOP);
	return DROP;
}

/* Step 3: write Ethernet + outer IPv4 + UDP + encap_hdr at the start of a
 * frame that already has ENCAP_LEN bytes of room between the Ethernet header
 * and the original IPv4 packet. Returns -1 if the frame is too short. */
static __always_inline int write_headers(void *data, void *data_end, const struct choice *c)
{
	struct ethhdr *eth = data;
	struct iphdr *ip = (void *)(eth + 1);
	struct udphdr *udp = (void *)(ip + 1);
	struct encap_hdr *hdr = (void *)(udp + 1);
	if ((void *)(hdr + 1) > data_end)
		return -1;

	__builtin_memcpy(eth->h_dest, c->first_mac, ETH_ALEN);
	__builtin_memcpy(eth->h_source, c->lb_mac, ETH_ALEN);
	eth->h_proto = bpf_htons(ETH_P_IP);

	ip->version = 4;
	ip->ihl = 5;
	ip->tos = 0;
	ip->tot_len = bpf_htons(ENCAP_LEN + c->inner_len);
	ip->id = 0;
	ip->frag_off = bpf_htons(IP_DF);
	ip->ttl = 64;
	ip->protocol = IPPROTO_UDP;
	ip->saddr = LB_IP;
	ip->daddr = c->first_ip;
	ip->check = 0;
	ip->check = ipv4_csum(ip);

	/* Source port from the flow hash: different flows get different outer
	 * ports, so the backend's RPS spreads them over its CPUs. Range 49152-65535. */
	udp->source = bpf_htons(0xc000 | (c->hash >> 18));
	udp->dest = bpf_htons(ENCAP_PORT);
	udp->len = bpf_htons(sizeof(struct udphdr) + sizeof(struct encap_hdr) + c->inner_len);
	udp->check = 0;  /* optional for UDP over IPv4; the inner TCP checksum protects the data */

	hdr->version = ENCAP_VERSION;
	hdr->flags = 0;
	hdr->bucket = bpf_htons(c->bucket);
	hdr->second_hop = c->second_ip;
	hdr->table_gen = bpf_htonl(c->gen);
	return 0;
}

/* Step 4's bookkeeping: count the packet for its backend. */
static __always_inline void count_sent(const struct choice *c)
{
	__u64 *sent = bpf_map_lookup_elem(&backend_packets, &c->first_id);

	if (sent)
		*sent += 1;
	count(STAT_TX);
}

#endif /* DIRECTOR_H */
