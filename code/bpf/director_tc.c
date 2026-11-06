// SPDX-License-Identifier: GPL-2.0
/* The director as a TC ingress program on the LB's eth0: the one we use (D35).
 * The logic is in director.h; this file only makes room for the headers and
 * sends.
 *
 * Unlike XDP, TC works on the kernel's packet buffer (sk_buff), so the packet
 * keeps what the kernel knows about it: a large TCP segment that will only be
 * cut into MTU-sized packets later (GSO), or a checksum the sender left for
 * "hardware" to fill in. bpf_skb_adjust_room's ENCAP flags tell the kernel the
 * packet is now a UDP tunnel, so it can still do both correctly afterwards.
 */
#include "director.h"
#include <linux/pkt_cls.h>

/* Headers we must be able to read directly: Ethernet + IPv4 with up to 40
 * bytes of options + TCP without options. */
#define PULL_LEN (sizeof(struct ethhdr) + 60 + sizeof(struct tcphdr))

SEC("tc")
int director_tc(struct __sk_buff *skb)
{
	void *data = (void *)(long)skb->data;
	void *data_end = (void *)(long)skb->data_end;
	struct choice c;

	/* A large packet's headers may not all be in the directly readable
	 * (linear) part yet; pull them in. A no-op for small packets. */
	if (data + PULL_LEN > data_end && skb->len > data_end - data) {
		if (bpf_skb_pull_data(skb, skb->len < PULL_LEN ? skb->len : PULL_LEN))
			return TC_ACT_OK;
		data = (void *)(long)skb->data;
		data_end = (void *)(long)skb->data_end;
	}

	switch (choose(data, data_end, skb->len, &c)) {
	case PASS:
		return TC_ACT_OK;
	case DROP:
		return TC_ACT_SHOT;
	case ENCAP:
		break;
	}

	/* Cut off any padding, then insert ENCAP_LEN bytes right after the
	 * Ethernet header. The flags say what the new bytes are: outer IPv4 and
	 * UDP, followed by our sizeof(encap_hdr)-byte header, before the inner
	 * packet. */
	__u64 flags = BPF_F_ADJ_ROOM_ENCAP_L3_IPV4 | BPF_F_ADJ_ROOM_ENCAP_L4_UDP |
		      BPF_F_ADJ_ROOM_ENCAP_L2(sizeof(struct encap_hdr));
	if ((c.padding && bpf_skb_change_tail(skb, skb->len - c.padding, 0)) ||
	    bpf_skb_adjust_room(skb, ENCAP_LEN, BPF_ADJ_ROOM_MAC, flags))
		goto error;
	/* Earlier packet pointers are invalid after adjusting: reload. */
	if (write_headers((void *)(long)skb->data, (void *)(long)skb->data_end, &c))
		goto error;
	count_sent(&c);
	return bpf_redirect(skb->ifindex, 0);  /* out of the interface it came in on */

error:
	count(STAT_DROP_ERROR);
	return TC_ACT_SHOT;
}

char LICENSE[] SEC("license") = "GPL";
