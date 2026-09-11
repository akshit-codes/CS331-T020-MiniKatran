// sanity check for the ring: is the split fair and how much moves when a backend is added
#include <stdio.h>
#include <arpa/inet.h>
#include "../common/ring.h"

static __u32 ring1[RING_SIZE], ring2[RING_SIZE];

int main(void)
{
	__u32 ips[3], ids[3] = { 0, 1, 2 };
	int cnt[3] = { 0 }, moved = 0;

	inet_pton(AF_INET, "10.0.2.11", &ips[0]);
	inet_pton(AF_INET, "10.0.2.12", &ips[1]);
	inet_pton(AF_INET, "10.0.2.13", &ips[2]);

	build_ring(ips, ids, 2, ring1);
	for (int j = 0; j < RING_SIZE; j++) cnt[ring1[j]]++;
	printf("2 backends: be0 %.1f%%  be1 %.1f%%\n", 100.0 * cnt[0] / RING_SIZE, 100.0 * cnt[1] / RING_SIZE);

	build_ring(ips, ids, 3, ring2);
	cnt[0] = cnt[1] = cnt[2] = 0;
	for (int j = 0; j < RING_SIZE; j++) {
		cnt[ring2[j]]++;
		if (ring1[j] != ring2[j]) moved++;
	}
	printf("3 backends: be0 %.1f%%  be1 %.1f%%  be2 %.1f%%\n", 100.0 * cnt[0] / RING_SIZE,
	       100.0 * cnt[1] / RING_SIZE, 100.0 * cnt[2] / RING_SIZE);
	printf("slots that changed owner when be2 was added: %.1f%% (ideal 33.3%%)\n", 100.0 * moved / RING_SIZE);
	return 0;
}
