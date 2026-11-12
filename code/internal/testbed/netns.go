package testbed

import (
	"os"
	"path/filepath"
	"runtime"

	"golang.org/x/sys/unix"
)

// InNetns runs fn with this goroutine's OS thread inside network namespace
// name (as created by `ip netns add`). Interface lookups, XDP/TC attach calls
// and new sockets act on the namespace of the calling thread. An empty name
// means the current namespace (a real machine's own interface).
func InNetns(name string, fn func() error) error {
	if name == "" {
		return fn()
	}
	runtime.LockOSThread()
	orig, err := os.Open("/proc/thread-self/ns/net")
	if err != nil {
		runtime.UnlockOSThread()
		return err
	}
	defer orig.Close()
	target, err := os.Open(filepath.Join("/run/netns", name))
	if err != nil {
		runtime.UnlockOSThread()
		return err
	}
	defer target.Close()
	if err := unix.Setns(int(target.Fd()), unix.CLONE_NEWNET); err != nil {
		runtime.UnlockOSThread()
		return err
	}
	defer func() {
		// Only hand the thread back to Go if it is back in our namespace;
		// otherwise it stays locked and Go discards it when we return.
		if unix.Setns(int(orig.Fd()), unix.CLONE_NEWNET) == nil {
			runtime.UnlockOSThread()
		}
	}()
	return fn()
}
