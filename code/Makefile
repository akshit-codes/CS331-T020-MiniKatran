# make           build the BPF programs and the Go tools into bin/
# make test      unit tests that need no privileges
# make test-bpf  golden-packet tests: run the BPF programs in the kernel (needs sudo)

BPF_OBJS := bin/bpf/director_tc.o bin/bpf/director_xdp.o bin/bpf/redirector.o bin/bpf/xdp_pass.o bin/bpf/resets.o
GO_CMDS  := backend loadgen antagonist lbctl resetmon controller ctlsim ctltune sigmon

all: $(BPF_OBJS) $(addprefix bin/,$(GO_CMDS))

bin/bpf/%.o: bpf/%.c bpf/lb.h bpf/director.h
	@mkdir -p bin/bpf
	clang -O2 -g -Wall -target bpf -c $< -o $@

bin/%: FORCE
	go build -o $@ ./cmd/$*

test:
	go vet ./...
	go test ./...

test-bpf: $(BPF_OBJS)
	go test -c -o bin/lb.test ./internal/lb
	cd internal/lb && sudo ../../bin/lb.test -test.v

.PHONY: all test test-bpf FORCE
