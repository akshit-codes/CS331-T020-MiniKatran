package signals

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"time"

	"cs331lb/internal/testbed"
)

// Metrics gets backend i's /metrics: over the network in cluster mode, from
// inside its network namespace on the single-laptop testbed.
func Metrics(cfg *testbed.Config, i int, port string) (string, error) {
	if !cfg.Cluster {
		return FetchMetrics(cfg.Backends[i].Name, port)
	}
	client := http.Client{Timeout: time.Second}
	resp, err := client.Get("http://" + net.JoinHostPort(cfg.Backends[i].IP, port) + "/metrics")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return string(body), err
}

// FetchMetrics gets a backend's /metrics from inside its network namespace
// (the controller runs outside the testbed and can't route to it). The socket
// is created on a thread switched into the namespace, so it lives there; a
// plain HTTP/1.0 request keeps everything on that one connection.
func FetchMetrics(netns, port string) (string, error) {
	var text string
	err := testbed.InNetns(netns, func() error {
		conn, err := net.DialTimeout("tcp", "127.0.0.1:"+port, time.Second)
		if err != nil {
			return err
		}
		defer conn.Close()
		conn.SetDeadline(time.Now().Add(time.Second))
		if _, err := fmt.Fprint(conn, "GET /metrics HTTP/1.0\r\n\r\n"); err != nil {
			return err
		}
		reply, err := io.ReadAll(conn)
		if err != nil {
			return err
		}
		_, body, ok := strings.Cut(string(reply), "\r\n\r\n")
		if !ok {
			return fmt.Errorf("bad reply from %s", netns)
		}
		text = body
		return nil
	})
	return text, err
}
