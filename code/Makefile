CLANG ?= clang
CC ?= gcc
CFLAGS = -O2 -g -Wall
BPF_CFLAGS = -O2 -g -target bpf -Wall

BPF_OBJS = bpf/xdp_pass.o bpf/lb_kern.o
BINS = bin/tcp_proxy bin/raw_fwd bin/ring_test bin/lb bin/httpd bin/cps

all: $(BPF_OBJS) $(BINS)

bpf/%.o: bpf/%.c common/lb.h common/hash.h
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

bin/tcp_proxy: baseline/tcp_proxy.c common/ring.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $^

bin/raw_fwd: baseline/raw_fwd.c common/ring.c common/hash.h common/ring.h
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ baseline/raw_fwd.c common/ring.c

bin/httpd: bench/httpd.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $^

bin/cps: bench/cps.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $^

bin/ring_test: tests/ring_test.c common/ring.c
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $^

bin/lb: userspace/lb.c common/ring.c common/lb.h common/ring.h
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ userspace/lb.c common/ring.c -lbpf

clean:
	rm -f bpf/*.o bin/*

.PHONY: all clean
