#define _GNU_SOURCE
// epoll tcp proxy, the user space baseline
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/epoll.h>
#include "../common/hash.h"
#include "../common/ring.h"

#define MAXEV 256
#define BUFSZ 16384
#define MAXBE 16

struct conn {
	int fd;
	struct conn *peer;
	int connecting;		// backend side, connect() not finished yet
	int rd_done;		// got EOF from this fd
	int dead;
	char out[BUFSZ];	// bytes waiting to be written to fd
	int outlen, outoff;
};

static int epfd;
static struct sockaddr_in backends[MAXBE];
static int nbe;
static long nconn, nfail;
static __u32 ring[RING_SIZE];
static int no_sport;		// -s: hash on client ip only
static struct conn *dead[2 * MAXEV];
static int ndead;

static void die(const char *s) { perror(s); exit(1); }

static int parse_addr(const char *s, struct sockaddr_in *a)
{
	char buf[64], *p;
	snprintf(buf, sizeof buf, "%s", s);
	p = strchr(buf, ':');
	if (!p) return -1;
	*p++ = 0;
	memset(a, 0, sizeof *a);
	a->sin_family = AF_INET;
	a->sin_port = htons(atoi(p));
	return inet_pton(AF_INET, buf, &a->sin_addr) == 1 ? 0 : -1;
}

static void ep_ctl(int op, struct conn *c, unsigned ev)
{
	struct epoll_event e = { .events = ev, .data.ptr = c };
	epoll_ctl(epfd, op, c->fd, &e);
}

// what we want to be woken up for depends on both sides of the pair
static void update(struct conn *c)
{
	unsigned ev = 0;
	if (!c->rd_done && c->peer->outlen == 0) ev |= EPOLLIN;
	if (c->outlen > 0 || c->connecting) ev |= EPOLLOUT;
	ep_ctl(EPOLL_CTL_MOD, c, ev);
}

static void close_pair(struct conn *c)
{
	struct conn *p = c->peer;
	if (c->dead) return;
	c->dead = p->dead = 1;
	ep_ctl(EPOLL_CTL_DEL, c, 0);
	ep_ctl(EPOLL_CTL_DEL, p, 0);
	close(c->fd);
	close(p->fd);
	// free later, the peer may still be in this batch of events
	dead[ndead++] = c;
	dead[ndead++] = p;
}

static int pick_backend(struct sockaddr_in *cli, __u16 dport)
{
	__u16 sport = no_sport ? 0 : ntohs(cli->sin_port);
	__u32 h = flow_hash(cli->sin_addr.s_addr, sport, dport, 0);
	return ring[h % RING_SIZE];
}

static void new_clients(int lfd, __u16 dport)
{
	for (;;) {
		struct sockaddr_in ca;
		socklen_t cl = sizeof ca;
		int cfd = accept4(lfd, (struct sockaddr *)&ca, &cl, SOCK_NONBLOCK);
		if (cfd < 0) {
			if (errno != EAGAIN) perror("accept");
			return;
		}
		int b = pick_backend(&ca, dport);
		int bfd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
		if (connect(bfd, (struct sockaddr *)&backends[b], sizeof backends[b]) < 0 && errno != EINPROGRESS) {
			close(cfd);
			close(bfd);
			nfail++;
			continue;
		}
		struct conn *c = calloc(1, sizeof *c), *s = calloc(1, sizeof *s);
		c->fd = cfd;
		s->fd = bfd;
		c->peer = s;
		s->peer = c;
		s->connecting = 1;
		ep_ctl(EPOLL_CTL_ADD, c, 0);	// nothing from the client until the backend is up
		ep_ctl(EPOLL_CTL_ADD, s, EPOLLOUT);
		nconn++;
	}
}

// flush c->out into c->fd, returns 0 when everything is gone
static int flush(struct conn *c)
{
	while (c->outoff < c->outlen) {
		int n = write(c->fd, c->out + c->outoff, c->outlen - c->outoff);
		if (n < 0) {
			if (errno == EAGAIN) return 1;
			return -1;
		}
		c->outoff += n;
	}
	c->outlen = c->outoff = 0;
	if (c->peer->rd_done)
		shutdown(c->fd, SHUT_WR);
	return 0;
}

static void on_readable(struct conn *c)
{
	struct conn *p = c->peer;
	int n = read(c->fd, p->out, BUFSZ);
	if (n < 0) {
		if (errno != EAGAIN) close_pair(c);
		return;
	}
	if (n == 0) {
		c->rd_done = 1;
		if (p->rd_done) {
			close_pair(c);
			return;
		}
		if (p->outlen == 0) shutdown(p->fd, SHUT_WR);
		update(c);
		return;
	}
	p->outlen = n;
	p->outoff = 0;
	if (flush(p) < 0) {
		close_pair(c);
		return;
	}
	update(c);
	update(p);
}

static void on_writable(struct conn *c)
{
	if (c->connecting) {
		int err = 0;
		socklen_t l = sizeof err;
		getsockopt(c->fd, SOL_SOCKET, SO_ERROR, &err, &l);
		if (err) {
			nfail++;
			close_pair(c);
			return;
		}
		c->connecting = 0;
	} else if (flush(c) < 0) {
		close_pair(c);
		return;
	}
	update(c);
	update(c->peer);
}

static void stats(int sig)
{
	fprintf(stderr, "connections: %ld, failed: %ld\n", nconn, nfail);
	exit(0);
}

int main(int argc, char **argv)
{
	struct sockaddr_in la;
	int lfd, one = 1;

	if (argc > 1 && !strcmp(argv[1], "-s")) {
		no_sport = 1;
		argv++;
		argc--;
	}
	if (argc < 3 || parse_addr(argv[1], &la) < 0) {
		fprintf(stderr, "usage: %s [-s] listen_ip:port backend_ip:port...\n", argv[0]);
		return 1;
	}
	for (int i = 2; i < argc && nbe < MAXBE; i++)
		if (parse_addr(argv[i], &backends[nbe++]) < 0) die("bad backend");
	{
		__u32 ips[MAXBE], ids[MAXBE];
		for (int i = 0; i < nbe; i++) {
			ips[i] = backends[i].sin_addr.s_addr;
			ids[i] = i;
		}
		build_ring(ips, ids, nbe, ring);
	}

	signal(SIGPIPE, SIG_IGN);
	signal(SIGINT, stats);
	signal(SIGTERM, stats);

	lfd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
	setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
	if (bind(lfd, (struct sockaddr *)&la, sizeof la) < 0) die("bind");
	if (listen(lfd, 1024) < 0) die("listen");

	epfd = epoll_create1(0);
	struct epoll_event le = { .events = EPOLLIN, .data.ptr = NULL };
	epoll_ctl(epfd, EPOLL_CTL_ADD, lfd, &le);

	fprintf(stderr, "proxy on %s, %d backends\n", argv[1], nbe);
	for (;;) {
		struct epoll_event ev[MAXEV];
		int n = epoll_wait(epfd, ev, MAXEV, -1);
		if (n < 0 && errno == EINTR) continue;
		if (n < 0) die("epoll_wait");
		for (int i = 0; i < n; i++) {
			struct conn *c = ev[i].data.ptr;
			if (!c) {
				new_clients(lfd, ntohs(la.sin_port));
				continue;
			}
			if (c->dead) continue;
			if (ev[i].events & EPOLLOUT) on_writable(c);
			if (c->dead) continue;
			if (ev[i].events & EPOLLIN) on_readable(c);
			if (c->dead) continue;
			if (ev[i].events & (EPOLLERR | EPOLLHUP)) close_pair(c);
		}
		while (ndead) free(dead[--ndead]);
	}
}
