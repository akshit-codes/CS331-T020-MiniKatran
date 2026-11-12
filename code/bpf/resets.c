// SPDX-License-Identifier: GPL-2.0
/* Reset monitor: one event per TCP reset sent or received anywhere on this
 * machine (all namespaces), with the connection's addresses and ports, so
 * user space (cmd/resetmon) can work out which bucket each reset belongs to.
 *
 * Sent resets: the typed tracepoint tcp_send_reset(sk, skb, reason). Most
 * resets answer a received packet (skb) for which there is no connection; we
 * read the addresses from that packet's headers, because `sk` is then often
 * just the listener, whose addresses (e.g. [::]:8080) say nothing about the
 * connection. For resets without a packet (a socket aborting) we use `sk`.
 * The few kernel struct fields we need are declared below with
 * preserve_access_index: the loader finds their real offsets in the kernel's
 * BTF type information (CO-RE), so we need no generated kernel headers.
 *
 * Received resets: the classic tracepoint tcp:tcp_receive_reset. Its argument
 * layout below is copied from /sys/kernel/tracing/events/tcp/tcp_receive_reset/format
 * on our kernel (7.2); other kernel versions may lay it out differently.
 */
#include <linux/bpf.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

struct sk_buff {
	unsigned char *head;
	__u16 network_header;  /* offsets from head */
	__u16 transport_header;
} __attribute__((preserve_access_index));

struct sock_common {
	__be32 skc_daddr;      /* remote */
	__be32 skc_rcv_saddr;  /* local; also set for IPv4 on dual-stack (IPv6) sockets */
	__be16 skc_dport;
	__u16 skc_num;         /* local port, host byte order */
} __attribute__((preserve_access_index));

struct sock {
	struct sock_common __sk_common;
} __attribute__((preserve_access_index));

struct receive_reset_args {
	__u64 common;
	const void *skaddr;
	__u16 sport;           /* host byte order; s = the side receiving the reset */
	__u16 dport;
	__u16 family;
	__u8 saddr[4];
	__u8 daddr[4];
	__u8 saddr_v6[16];
	__u8 daddr_v6[16];
	__u64 sock_cookie;     /* unique per socket: tells connections apart */
};

enum { RESET_SENT, RESET_RECEIVED };

/* One reset. Addresses in network byte order, ports in host byte order. */
struct event {
	__u64 time_ns;      /* CLOCK_MONOTONIC */
	__u64 sock_cookie;  /* received only, else 0 */
	__be32 saddr;       /* the side sending (RESET_SENT) or receiving (RESET_RECEIVED) */
	__be32 daddr;
	__u16 sport;
	__u16 dport;
	__u8 kind;
	__u8 pad[3];
};

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, 1 << 20);
} events SEC(".maps");

/* tp_btf programs get the tracepoint's arguments as an array of u64. */
SEC("tp_btf/tcp_send_reset")
int on_send_reset(__u64 *ctx)
{
	struct sock *sk = (void *)ctx[0];
	struct sk_buff *skb = (void *)ctx[1];
	struct event e = { .kind = RESET_SENT };

	if (skb) {
		/* The packet we are answering: the reset goes back to its source. */
		unsigned char *head = BPF_CORE_READ(skb, head);
		struct iphdr ip;
		struct tcphdr tcp;
		if (bpf_probe_read_kernel(&ip, sizeof(ip), head + BPF_CORE_READ(skb, network_header)) ||
		    bpf_probe_read_kernel(&tcp, sizeof(tcp), head + BPF_CORE_READ(skb, transport_header)) ||
		    ip.version != 4)
			return 0;
		e.saddr = ip.daddr;
		e.daddr = ip.saddr;
		e.sport = bpf_ntohs(tcp.dest);
		e.dport = bpf_ntohs(tcp.source);
	} else if (sk) {
		e.saddr = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
		e.daddr = BPF_CORE_READ(sk, __sk_common.skc_daddr);
		e.sport = BPF_CORE_READ(sk, __sk_common.skc_num);
		e.dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	}
	if (!e.saddr)
		return 0;  /* not IPv4 */

	struct event *out = bpf_ringbuf_reserve(&events, sizeof(*out), 0);
	if (!out)
		return 0;
	e.time_ns = bpf_ktime_get_ns();
	*out = e;
	bpf_ringbuf_submit(out, 0);
	return 0;
}

SEC("tracepoint/tcp/tcp_receive_reset")
int on_receive_reset(struct receive_reset_args *args)
{
	__be32 saddr, daddr;

	/* Copy out: the context may only be read at aligned offsets, and these
	 * fields are not 4-byte aligned. */
	__builtin_memcpy(&saddr, args->saddr, 4);
	__builtin_memcpy(&daddr, args->daddr, 4);
	if (!saddr)
		return 0;  /* IPv6-only connection (dual-stack sockets fill the IPv4 fields too) */
	struct event *e = bpf_ringbuf_reserve(&events, sizeof(*e), 0);
	if (!e)
		return 0;
	*e = (struct event){
		.time_ns = bpf_ktime_get_ns(),
		.sock_cookie = args->sock_cookie,
		.saddr = saddr, .daddr = daddr,
		.sport = args->sport, .dport = args->dport,
		.kind = RESET_RECEIVED,
	};
	bpf_ringbuf_submit(e, 0);
	return 0;
}

char LICENSE[] SEC("license") = "GPL";
