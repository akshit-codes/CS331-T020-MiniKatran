// consistent hash ring with virtual nodes
#include <stdlib.h>
#include "hash.h"
#include "ring.h"

struct point {
	__u32 h;
	__u32 id;
};

static int cmp(const void *a, const void *b)
{
	__u32 x = ((const struct point *)a)->h, y = ((const struct point *)b)->h;
	return x < y ? -1 : x > y;
}

void build_ring(const __u32 *ips, const __u32 *ids, int n, __u32 *ring)
{
	int np = n * VNODES, k = 0;
	struct point *pts;

	if (n == 0) {
		for (int j = 0; j < RING_SIZE; j++) ring[j] = NO_BACKEND;
		return;
	}
	pts = malloc(np * sizeof *pts);
	for (int i = 0; i < n; i++)
		for (int v = 0; v < VNODES; v++) {
			pts[i * VNODES + v].h = hash_words(ips[i], v, 0);
			pts[i * VNODES + v].id = ids[i];
		}
	qsort(pts, np, sizeof *pts, cmp);

	// slot positions increase monotonically so one pass over the sorted points is enough
	for (int j = 0; j < RING_SIZE; j++) {
		__u32 pos = ((__u64)j << 32) / RING_SIZE;
		while (k < np && pts[k].h < pos) k++;
		ring[j] = pts[k < np ? k : 0].id;
	}
	free(pts);
}
