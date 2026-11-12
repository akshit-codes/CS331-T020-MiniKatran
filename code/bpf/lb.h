/* Definitions shared by the LB (director.h) and the backends (redirector.c).
 *
 * Anything here that user space also needs (sizes, the encapsulation header,
 * map value layouts, the hash) is mirrored in internal/lb/lb.go. If you change
 * one, change the other; the golden-packet tests catch a mismatch.
 */
#ifndef LB_H
#define LB_H

#include <linux/bpf.h>
#include <linux/if_ether.h>
#include <linux/in.h>
#include <linux/ip.h>
#include <linux/tcp.h>
#include <linux/udp.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>

#define NUM_BUCKETS   4096         /* power of two: bucket = hash & (NUM_BUCKETS - 1) */
#define GEN_SLOT      NUM_BUCKETS  /* extra table slot holding the table generation (D6) */
#define MAX_BACKENDS  64           /* backend ids 1..63; id 0 means "none" */
#define HASH_SEED     0x5eed       /* fixed, so user space can compute buckets too */

/* IPv4 frag_off bits (the kernel's are in a non-UAPI header). */
#define IP_DF         0x4000  /* don't fragment */
#define IP_MF         0x2000  /* more fragments */
#define IP_OFFSET     0x1fff  /* fragment offset */

/* Encapsulation (D7): outer IPv4 | UDP | encap_hdr | original IPv4 packet. */
#define ENCAP_PORT    7700
#define ENCAP_VERSION 1
#define F_HOPPED      0x01  /* already forwarded once by a backend (used by the redirector) */
#define ENCAP_LEN     (sizeof(struct iphdr) + sizeof(struct udphdr) + sizeof(struct encap_hdr))

struct encap_hdr {
	__u8   version;
	__u8   flags;
	__be16 bucket;      /* for debugging and attributing broken connections (RQ4) */
	__be32 second_hop;  /* IPv4 address of the second hop, 0 = none */
	__be32 table_gen;   /* generation of the table that chose the hops */
};

/* One table entry. Hops are backend ids (index into the backends map). */
struct bucket {
	__u32 first;   /* gets new connections */
	__u32 second;  /* gets packets the first hop has no connection for, 0 = none */
};

struct backend {
	__be32 ip;
	__u8   mac[ETH_ALEN];
	__u16  pad;
};

/* --- Bob Jenkins' lookup3 hash, as in the kernel's include/linux/jhash.h --- */

static __always_inline __u32 rol32(__u32 word, unsigned int shift)
{
	return (word << shift) | (word >> (32 - shift));
}

static __always_inline __u32 jhash_3words(__u32 a, __u32 b, __u32 c, __u32 initval)
{
	initval += 0xdeadbeef + (3 << 2);
	a += initval;
	b += initval;
	c += initval;
	c ^= b; c -= rol32(b, 14);
	a ^= c; a -= rol32(c, 11);
	b ^= a; b -= rol32(a, 25);
	c ^= b; c -= rol32(b, 16);
	a ^= c; a -= rol32(c, 4);
	b ^= a; b -= rol32(a, 14);
	c ^= b; c -= rol32(b, 24);
	return c;
}

/* Hash of a TCP 4-tuple, all inputs in host byte order. */
static __always_inline __u32 flow_hash(__u32 saddr, __u32 daddr, __u16 sport, __u16 dport)
{
	return jhash_3words(saddr, daddr, ((__u32)sport << 16) | dport, HASH_SEED);
}

/* Internet checksum of a 20-byte IPv4 header (no options). Set check = 0 first. */
static __always_inline __u16 ipv4_csum(struct iphdr *ip)
{
	__u16 *word = (__u16 *)ip;
	__u32 sum = 0;

#pragma unroll
	for (int i = 0; i < (int)sizeof(*ip) / 2; i++)
		sum += word[i];
	sum = (sum & 0xffff) + (sum >> 16);  /* fold carries twice: enough for 10 words */
	sum = (sum & 0xffff) + (sum >> 16);
	return ~sum;
}

#endif /* LB_H */
