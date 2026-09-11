// same logic as lb_kern.c but on an AF_PACKET socket in user space
//
//   raw_fwd <iface> <vip> <port>[s]... <backend_ip>=<mac>...     (s = hash without source port)
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <linux/if_packet.h>
#include <linux/if_ether.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include "../common/hash.h"
#include "../common/ring.h"

#define MAXBE 16
#define MAXPORT 8
#define TABLE (1 << 18)		// flow table slots, power of two

struct backend {
	__be32 addr;
	__u8 mac[6];
};

struct flow {
	__be32 saddr, daddr;
	__be16 sport, dport;
	__be32 new_addr;	// what the address becomes
	__u8 mac[6];
	__u8 dir;		// 0 rewrite dst, 1 rewrite src
	__u8 used;
	__u64 seen;		// packet counter when last used
};

static struct backend be[MAXBE];
static int nbe;
static __be16 ports[MAXPORT];
static int nosport[MAXPORT], nports;
static __be32 vip;
static __u32 ring[RING_SIZE];
static struct flow table[TABLE];
static long npkts, nfwd, nnew, nevict;

static __u16 csum_replace4(__u16 check, __be32 old, __be32 new)
{
	__u32 sum = (__u16)~check;
	sum += (__u16)~old + (__u16)(~old >> 16);
	sum += (__u16)new + (__u16)(new >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	sum = (sum & 0xffff) + (sum >> 16);
	return ~sum;
}

static struct flow *lookup(__be32 s, __be32 d, __be16 sp, __be16 dp, int create)
{
	__u32 h = hash_words(s, d, ((__u32)sp << 16) | dp);
	struct flow *oldest = NULL;

	// linear probing, replace the least recently seen entry if the window is full
	for (int i = 0; i < 8; i++) {
		struct flow *f = &table[(h + i) & (TABLE - 1)];
		if (f->used && f->saddr == s && f->daddr == d && f->sport == sp && f->dport == dp) {
			f->seen = npkts;
			return f;
		}
		if (!f->used) {
			if (!create) return NULL;
			oldest = f;
			break;
		}
		if (!oldest || f->seen < oldest->seen) oldest = f;
	}
	if (!create) return NULL;
	if (oldest->used) nevict++;
	oldest->used = 1;
	oldest->seen = npkts;
	oldest->saddr = s; oldest->daddr = d; oldest->sport = sp; oldest->dport = dp;
	return oldest;
}

static int vip_port(__be16 p)
{
	for (int i = 0; i < nports; i++)
		if (ports[i] == p) return i;
	return -1;
}

// returns 1 if the packet in buf was rewritten and should be sent
static int handle(unsigned char *buf, int len)
{
	struct ethhdr *eth = (void *)buf;
	struct iphdr *ip = (void *)(eth + 1);
	struct tcphdr *tcp;
	struct flow *f;
	__be32 old;

	if (len < (int)(sizeof *eth + sizeof *ip + sizeof *tcp)) return 0;
	if (eth->h_proto != htons(ETH_P_IP) || ip->ihl != 5 || ip->protocol != IPPROTO_TCP) return 0;
	tcp = (void *)(ip + 1);

	// syn = new connection, same as lb_kern.c
	f = tcp->syn && !tcp->ack ? NULL : lookup(ip->saddr, ip->daddr, tcp->source, tcp->dest, 0);
	if (!f) {
		int p = vip_port(tcp->dest);
		if (ip->daddr != vip || p < 0) return 0;
		__u16 sport = nosport[p] ? 0 : ntohs(tcp->source);
		__u32 id = ring[flow_hash(ip->saddr, sport, ntohs(tcp->dest), 0) % RING_SIZE];
		if (id == NO_BACKEND) return 0;

		f = lookup(ip->saddr, ip->daddr, tcp->source, tcp->dest, 1);
		f->new_addr = be[id].addr;
		memcpy(f->mac, be[id].mac, 6);
		f->dir = 0;
		struct flow *r = lookup(be[id].addr, ip->saddr, tcp->dest, tcp->source, 1);
		r->new_addr = vip;
		memcpy(r->mac, eth->h_source, 6);
		r->dir = 1;
		nnew++;
	}
	if (f->dir == 0) {
		old = ip->daddr;
		ip->daddr = f->new_addr;
	} else {
		old = ip->saddr;
		ip->saddr = f->new_addr;
	}
	ip->check = csum_replace4(ip->check, old, f->new_addr);
	tcp->check = csum_replace4(tcp->check, old, f->new_addr);
	memcpy(eth->h_source, eth->h_dest, 6);
	memcpy(eth->h_dest, f->mac, 6);
	return 1;
}

static void stats(int sig)
{
	fprintf(stderr, "packets seen: %ld, forwarded: %ld, new flows: %ld, evicted: %ld\n", npkts, nfwd, nnew, nevict);
	exit(0);
}

int main(int argc, char **argv)
{
	struct sockaddr_ll sll = { .sll_family = AF_PACKET, .sll_protocol = htons(ETH_P_ALL) };
	unsigned char buf[2048];
	int fd;

	if (argc < 4) {
		fprintf(stderr, "usage: %s iface vip port[s]... ip=mac...\n", argv[0]);
		return 1;
	}
	sll.sll_ifindex = if_nametoindex(argv[1]);
	if (!sll.sll_ifindex) { perror("iface"); return 1; }
	inet_pton(AF_INET, argv[2], &vip);
	for (int i = 3; i < argc; i++) {
		char *eq = strchr(argv[i], '=');
		if (eq && nbe < MAXBE) {
			*eq = 0;
			inet_pton(AF_INET, argv[i], &be[nbe].addr);
			__u8 *m = be[nbe].mac;
			sscanf(eq + 1, "%hhx:%hhx:%hhx:%hhx:%hhx:%hhx", m, m + 1, m + 2, m + 3, m + 4, m + 5);
			nbe++;
		} else if (nports < MAXPORT) {
			ports[nports] = htons(atoi(argv[i]));
			nosport[nports] = argv[i][strlen(argv[i]) - 1] == 's';
			nports++;
		}
	}
	{
		__u32 ips[MAXBE], ids[MAXBE];
		for (int i = 0; i < nbe; i++) { ips[i] = be[i].addr; ids[i] = i; }
		build_ring(ips, ids, nbe, ring);
	}

	fd = socket(AF_PACKET, SOCK_RAW, htons(ETH_P_ALL));
	if (fd < 0 || bind(fd, (struct sockaddr *)&sll, sizeof sll) < 0) { perror("socket"); return 1; }
	signal(SIGINT, stats);
	signal(SIGTERM, stats);
	fprintf(stderr, "raw forwarder on %s, vip %s, %d ports, %d backends\n", argv[1], argv[2], nports, nbe);

	for (;;) {
		struct sockaddr_ll from;
		socklen_t fl = sizeof from;
		int n = recvfrom(fd, buf, sizeof buf, 0, (struct sockaddr *)&from, &fl);
		if (n < 0) {
			if (errno == EINTR) continue;
			perror("recv");
			break;
		}
		// the socket also shows us what we send, don't loop those back
		if (from.sll_pkttype == PACKET_OUTGOING) continue;
		npkts++;
		if (!handle(buf, n)) continue;
		if (sendto(fd, buf, n, 0, (struct sockaddr *)&sll, sizeof sll) < 0) perror("send");
		else nfwd++;
	}
	return 0;
}
