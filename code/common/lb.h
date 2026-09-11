#ifndef LB_H
#define LB_H
// structs shared between the xdp program and the control plane (map keys/values)
#include <linux/types.h>

#define MAX_VIPS 8
#define MAX_BACKENDS 32
#define MAX_FLOWS 65536

#define VIP_NO_SPORT 1		// hash without the client port (iperf3 style clients)

struct vip_key {
	__be32 addr;
	__be16 port;
	__u8 proto;
	__u8 pad;
};

struct vip_info {
	__u32 num;		// which ring this vip uses
	__u32 flags;
};

struct backend {
	__be32 addr;
	__u8 mac[6];
	__u8 pad[2];
};

struct flow_key {
	__be32 saddr, daddr;
	__be16 sport, dport;
	__u8 proto;
	__u8 pad[3];
};

#define DIR_FWD 0		// client -> backend: rewrite dst
#define DIR_REV 1		// backend -> client: rewrite src

struct flow_val {
	__be32 addr;		// fwd: backend ip, rev: the vip
	__u8 mac[6];		// where the packet goes next
	__u8 dir;
	__u8 pad;
	__u32 be;
	__u64 ts;
};

enum { ST_PKTS, ST_PASS, ST_NEW, ST_HIT, ST_TX, ST_NOBE, ST_MAX };

struct counter {
	__u64 pkts, bytes;
};

#endif
