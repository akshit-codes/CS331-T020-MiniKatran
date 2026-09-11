#ifndef HASH_H
#define HASH_H
// shared hash, bpf side and user space must agree
#include <linux/types.h>

#ifndef __always_inline
#define __always_inline inline __attribute__((always_inline))
#endif

// fnv-1a with a murmur style finaliser
static __always_inline __u32 hash_words(__u32 a, __u32 b, __u32 c)
{
	__u32 h = 2166136261u;
	h = (h ^ a) * 16777619u;
	h = (h ^ b) * 16777619u;
	h = (h ^ c) * 16777619u;
	h ^= h >> 16;
	h *= 0x85ebca6b;
	h ^= h >> 13;
	h *= 0xc2b2ae35;
	h ^= h >> 16;
	return h;
}

// flow -> ring slot. the vip number stands in for dst ip/port/proto.
// sport can be passed as 0 to get "same client ip -> same backend" behaviour
static __always_inline __u32 flow_hash(__u32 saddr, __u16 sport, __u16 dport, __u32 vip)
{
	return hash_words(saddr, ((__u32)sport << 16) | dport, vip);
}

#endif
