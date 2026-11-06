// SPDX-License-Identifier: GPL-2.0
/* Attached to p-lb, the router-side peer of the LB's veth (D9). A veth can
 * only receive frames sent with XDP_TX from its peer if it runs in NAPI mode,
 * and attaching any XDP program turns that on. This one lets everything pass.
 */
#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>

SEC("xdp")
int xdp_pass(struct xdp_md *ctx)
{
	return XDP_PASS;
}

char LICENSE[] SEC("license") = "GPL";
