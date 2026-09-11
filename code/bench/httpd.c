// minimal http server for the benchmarks (python's was too slow)
//   httpd <port> <name>
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/epoll.h>

static char resp[256];
static int rlen;

int main(int argc, char **argv)
{
	struct sockaddr_in a = { .sin_family = AF_INET, .sin_addr.s_addr = INADDR_ANY };
	int lfd, epfd, one = 1;
	char body[64];

	if (argc < 3) { fprintf(stderr, "usage: %s port name\n", argv[0]); return 1; }
	a.sin_port = htons(atoi(argv[1]));
	snprintf(body, sizeof body, "hello from %s\n", argv[2]);
	rlen = snprintf(resp, sizeof resp,
			"HTTP/1.1 200 OK\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s", strlen(body), body);

	signal(SIGPIPE, SIG_IGN);
	lfd = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0);
	setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
	if (bind(lfd, (struct sockaddr *)&a, sizeof a) < 0 || listen(lfd, 4096) < 0) { perror("listen"); return 1; }

	epfd = epoll_create1(0);
	struct epoll_event ev = { .events = EPOLLIN, .data.fd = lfd };
	epoll_ctl(epfd, EPOLL_CTL_ADD, lfd, &ev);

	for (;;) {
		struct epoll_event evs[256];
		int n = epoll_wait(epfd, evs, 256, -1);
		for (int i = 0; i < n; i++) {
			int fd = evs[i].data.fd;
			if (fd == lfd) {
				int c;
				while ((c = accept4(lfd, NULL, NULL, SOCK_NONBLOCK)) >= 0) {
					struct epoll_event ce = { .events = EPOLLIN, .data.fd = c };
					epoll_ctl(epfd, EPOLL_CTL_ADD, c, &ce);
				}
				continue;
			}
			char buf[1024];
			int r = read(fd, buf, sizeof buf);
			if (r < 0 && errno == EAGAIN) continue;
			// any request gets the answer, no keep-alive
			if (r > 0) write(fd, resp, rlen);
			close(fd);
		}
	}
}
