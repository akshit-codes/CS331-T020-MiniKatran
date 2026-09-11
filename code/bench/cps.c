// connection rate generator: C connections in flight, GET, read till close, repeat
//   cps <ip:port> <concurrency> <seconds>
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <signal.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/epoll.h>

#define MAXC 4096
#define MAXLAT 2000000

static const char req[] = "GET / HTTP/1.0\r\n\r\n";
static struct sockaddr_in dst;
static int epfd;
static struct { int fd; double t0; int sent; } conns[MAXC];
static double *lat;
static long nlat, ndone, nerr;

static double now(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

static void open_conn(int i)
{
	int fd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
	if (connect(fd, (struct sockaddr *)&dst, sizeof dst) < 0 && errno != EINPROGRESS) {
		nerr++;
		close(fd);
		conns[i].fd = -1;
		return;
	}
	conns[i].fd = fd;
	conns[i].t0 = now();
	conns[i].sent = 0;
	struct epoll_event ev = { .events = EPOLLOUT | EPOLLIN, .data.u32 = i };
	epoll_ctl(epfd, EPOLL_CTL_ADD, fd, &ev);
}

static void done(int i, int ok)
{
	if (ok) {
		if (nlat < MAXLAT) lat[nlat++] = now() - conns[i].t0;
		ndone++;
	} else
		nerr++;
	close(conns[i].fd);
	open_conn(i);
}

static int cmp(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;
	return x < y ? -1 : x > y;
}

int main(int argc, char **argv)
{
	char ip[64], *p;
	int conc, secs;
	double end, start;

	if (argc < 4) { fprintf(stderr, "usage: %s ip:port concurrency seconds\n", argv[0]); return 1; }
	snprintf(ip, sizeof ip, "%s", argv[1]);
	p = strchr(ip, ':');
	*p++ = 0;
	dst.sin_family = AF_INET;
	dst.sin_port = htons(atoi(p));
	inet_pton(AF_INET, ip, &dst.sin_addr);
	conc = atoi(argv[2]);
	if (conc > MAXC) conc = MAXC;
	secs = atoi(argv[3]);
	lat = malloc(MAXLAT * sizeof *lat);

	signal(SIGPIPE, SIG_IGN);
	epfd = epoll_create1(0);
	for (int i = 0; i < conc; i++) open_conn(i);
	start = now();
	end = start + secs;

	while (now() < end) {
		struct epoll_event evs[256];
		int n = epoll_wait(epfd, evs, 256, 100);
		for (int k = 0; k < n; k++) {
			int i = evs[k].data.u32, fd = conns[i].fd;
			char buf[1024];
			if (evs[k].events & (EPOLLERR | EPOLLHUP) && !(evs[k].events & EPOLLIN)) {
				done(i, 0);
				continue;
			}
			if ((evs[k].events & EPOLLOUT) && !conns[i].sent) {
				if (write(fd, req, sizeof req - 1) < 0) { done(i, 0); continue; }
				conns[i].sent = 1;
				struct epoll_event ev = { .events = EPOLLIN, .data.u32 = i };
				epoll_ctl(epfd, EPOLL_CTL_MOD, fd, &ev);
			}
			if (evs[k].events & EPOLLIN) {
				int r = read(fd, buf, sizeof buf);
				if (r == 0) done(i, 1);		// server closed after the answer
				else if (r < 0 && errno != EAGAIN) done(i, 0);
			}
		}
	}
	double el = now() - start;
	qsort(lat, nlat, sizeof *lat, cmp);
	printf("%ld connections in %.1fs = %.0f conn/s, %ld errors\n", ndone, el, ndone / el, nerr);
	if (nlat)
		printf("latency p50 %.0f us, p90 %.0f us, p99 %.0f us\n", lat[nlat / 2] * 1e6,
		       lat[nlat * 9 / 10] * 1e6, lat[nlat * 99 / 100] * 1e6);
	return 0;
}
