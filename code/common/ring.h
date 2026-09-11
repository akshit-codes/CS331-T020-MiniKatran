#ifndef RING_H
#define RING_H
#include <linux/types.h>

#define RING_SIZE 65537		// prime, like katran's ch ring
#define VNODES 100		// virtual nodes per backend
#define NO_BACKEND 0xffffffff

// fills ring[RING_SIZE] with backend ids. ips/ids are parallel arrays.
void build_ring(const __u32 *ips, const __u32 *ids, int n, __u32 *ring);

#endif
