// SPDX-License-Identifier: GPL-2.0
/* Backend side: a TC ingress program on each backend's eth0 (D8).
 *
 * For every packet the director encapsulated to us, decide:
 *   keep    - remove the encapsulation and let our kernel handle the client's
 *             packet (the backend then replies to the client directly), or
 *   forward - send it, still encapsulated, to the bucket's second hop, with
 *             F_HOPPED set so that backend keeps it no matter what.
 *
 * Keep if any of these hold, else forward:
 *   - F_HOPPED is set: it was forwarded once already; never a third time,
 *   - there is no second hop,
 *   - it is a SYN: a new connection, which belongs to the first hop,
 *   - we have a socket for it (established, half-open or TIME_WAIT),
 *   - only our listener matches, but the packet completes a handshake we
 *     answered with a SYN cookie (then no half-open socket exists yet).
 *
 * Why this keeps connections alive: when the controller gives a bucket a new
 * first hop, the old first hop becomes its second hop. Connections opened
 * before the change arrive at the new first hop, which has no socket for them,
 * and get forwarded to the old one, which does.
 */
#include "lb.h"
#include <linux/pkt_cls.h>

/* Set by the loader for each backend. */
volatile const __be32 SELF_IP;               /* this backend's address: outer source when forwarding */
volatile const __u8 CHECK_SYNCOOKIE = 1;     /* 0 only to show what goes wrong without it */
volatile const __u8 FORWARD = 1;             /* 0: never forward, only decapsulate (the RQ4 contrast) */

enum {
	STAT_KEEP_HOPPED,     /* forwarded to us by another backend */
	STAT_KEEP_NO_SECOND,  /* bucket has no second hop */
	STAT_KEEP_SYN,        /* new connection */
	STAT_KEEP_SOCKET,     /* we have a socket for it */
	STAT_KEEP_COOKIE,     /* completes a handshake we answered with a SYN cookie */
	STAT_FORWARD,         /* sent on to the second hop */
	STAT_BAD,             /* encapsulated but malformed: dropped */
	NUM_STATS,
};

struct {
	__uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
	__uint(max_entries, NUM_STATS);
	__type(key, __u32);
	__type(value, __u64);
} stats SEC(".maps");

static __always_inline void count(__u32 stat)
{
	__u64 *n = bpf_map_lookup_elem(&stats, &stat);

	if (n)
		*n += 1;
}

/* Bytes we need in the linear part of the packet to read all headers
 * (outer Ethernet + encapsulation + inner IPv4 and TCP without options). */
#define HEADERS_LEN (sizeof(struct ethhdr) + ENCAP_LEN + sizeof(struct iphdr) + sizeof(struct tcphdr))

/* Should we keep this packet? inner_ip and tcp point into the packet. */
static __always_inline int keep_locally(struct __sk_buff *skb, struct encap_hdr *hdr,
					struct iphdr *inner_ip, struct tcphdr *tcp)
{
	if (hdr->flags & F_HOPPED) {
		count(STAT_KEEP_HOPPED);
		return 1;
	}
	if (hdr->second_hop == 0 || !FORWARD) {
		count(STAT_KEEP_NO_SECOND);
		return 1;
	}
	if (tcp->syn && !tcp->ack) {
		count(STAT_KEEP_SYN);
		return 1;
	}

	/* Socket lookup with the client's 4-tuple, as our kernel would do it. */
	struct bpf_sock_tuple tuple = {
		.ipv4.saddr = inner_ip->saddr,
		.ipv4.daddr = inner_ip->daddr,
		.ipv4.sport = tcp->source,
		.ipv4.dport = tcp->dest,
	};
	struct bpf_sock *sk = bpf_skc_lookup_tcp(skb, &tuple, sizeof(tuple.ipv4), BPF_F_CURRENT_NETNS, 0);
	if (!sk)
		return 0;  /* not even a listener */

	int keep = 0;
	if (sk->state != BPF_TCP_LISTEN) {
		count(STAT_KEEP_SOCKET);
		keep = 1;
	} else if (CHECK_SYNCOOKIE &&
		   bpf_tcp_check_syncookie(sk, inner_ip, sizeof(*inner_ip), tcp, sizeof(*tcp)) == 0) {
		count(STAT_KEEP_COOKIE);
		keep = 1;
	}
	bpf_sk_release(sk);
	return keep;
}

SEC("tc")
int redirector(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;

	/* Make sure the headers are in the linear part (a no-op for our veths). */
	if (data + HEADERS_LEN > data_end) {
		if (skb->len < HEADERS_LEN || bpf_skb_pull_data(skb, HEADERS_LEN))
			return TC_ACT_OK;  /* too short to be ours */
		data = (void *)(long)skb->data;
		data_end = (void *)(long)skb->data_end;
	}

	/* --- Is it encapsulated by the director? --- */
	struct ethhdr *eth = data;
	struct iphdr *outer_ip = (void *)(eth + 1);
	struct udphdr *udp = (void *)(outer_ip + 1);
	struct encap_hdr *hdr = (void *)(udp + 1);
	struct iphdr *inner_ip = (void *)(hdr + 1);
	struct tcphdr *tcp = (void *)(inner_ip + 1);
	if ((void *)(tcp + 1) > data_end)
		return TC_ACT_OK;
	if (eth->h_proto != bpf_htons(ETH_P_IP) || outer_ip->ihl != 5 ||
	    outer_ip->protocol != IPPROTO_UDP || udp->dest != bpf_htons(ENCAP_PORT))
		return TC_ACT_OK;  /* normal traffic */
	if (hdr->version != ENCAP_VERSION || inner_ip->ihl != 5 || inner_ip->protocol != IPPROTO_TCP) {
		count(STAT_BAD);  /* the director only sends TCP; it never adds IP options */
		return TC_ACT_SHOT;
	}

	if (keep_locally(skb, hdr, inner_ip, tcp)) {
		/* Remove the ENCAP_LEN bytes between the Ethernet header and the
		 * inner IPv4 header. Both are IPv4, so nothing else changes. */
		if (bpf_skb_adjust_room(skb, -(int)ENCAP_LEN, BPF_ADJ_ROOM_MAC, 0))
			return TC_ACT_SHOT;
		return TC_ACT_OK;
	}

	/* --- Forward to the second hop, still encapsulated --- */
	outer_ip->saddr = SELF_IP;
	outer_ip->daddr = hdr->second_hop;
	outer_ip->ttl = 64;
	outer_ip->check = 0;
	outer_ip->check = ipv4_csum(outer_ip);
	hdr->flags |= F_HOPPED;
	count(STAT_FORWARD);
	/* Out of the interface it came in on. The kernel looks up the second
	 * hop's MAC address (ARP) and fills in the Ethernet header. */
	return bpf_redirect_neigh(skb->ifindex, NULL, 0, 0);
}

char LICENSE[] SEC("license") = "GPL";
