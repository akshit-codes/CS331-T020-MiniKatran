// SPDX-License-Identifier: GPL-2.0
/* The director as an XDP program. Not used in experiments (D35): kept only
 * to compare the per-packet cost of the two hooks on veth. The logic is in
 * director.h; this file only makes room for the headers and sends.
 */
#include "director.h"

SEC("xdp")
int director_xdp(struct xdp_md *ctx)
{
	void *data = (void *)(long)ctx->data;
	void *data_end = (void *)(long)ctx->data_end;
	struct choice c;

	switch (choose(data, data_end, data_end - data, &c)) {
	case PASS:
		return XDP_PASS;
	case DROP:
		return XDP_DROP;
	case ENCAP:
		break;
	}

	/* Cut off any padding, then grow the frame at the front by ENCAP_LEN.
	 * The Ethernet header is rewritten anyway, so it may move with the front. */
	if ((c.padding && bpf_xdp_adjust_tail(ctx, -(int)c.padding)) ||
	    bpf_xdp_adjust_head(ctx, -(int)ENCAP_LEN))
		goto error;
	/* Earlier packet pointers are invalid after adjusting: reload. */
	if (write_headers((void *)(long)ctx->data, (void *)(long)ctx->data_end, &c))
		goto error;
	count_sent(&c);
	return XDP_TX;  /* back out of the interface it came in on */

error:
	count(STAT_DROP_ERROR);
	return XDP_DROP;
}

char LICENSE[] SEC("license") = "GPL";
