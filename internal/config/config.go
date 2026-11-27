// Package config resolves Jetty host paths, network addresses, and gateway tokens.
package config

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const (
	DefaultListenPort = 7700
	DefaultGatewayUp  = "10.201.0.1"
	DefaultGatewayIP  = "10.201.0.2"
)

// DataDir returns Jetty's host data directory, honoring ORCA_PROXY_HOME.
func DataDir() (string, error) {
	dir := os.Getenv("ORCA_PROXY_HOME")
	if dir == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", err
		}
		dir = filepath.Join(home, ".orca-proxy")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	return dir, nil
}

// StateDBPath returns the path to state.sqlite inside DataDir.
func StateDBPath() (string, error) {
	dir, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "state.sqlite"), nil
}

// RequestsDBPath returns the path to requests.sqlite inside DataDir.
func RequestsDBPath() (string, error) {
	dir, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "requests.sqlite"), nil
}

// ListenPort returns the daemon TCP port (JETTY_LISTEN_PORT, default 7700).
func ListenPort() int {
	raw := strings.TrimSpace(os.Getenv("JETTY_LISTEN_PORT"))
	if raw == "" {
		return DefaultListenPort
	}
	p, err := strconv.Atoi(raw)
	if err != nil || p <= 0 || p > 65535 {
		return DefaultListenPort
	}
	return p
}

// ClientAddr returns the host:port CLI/desktop connects to, honoring JETTY_ADDR.
func ClientAddr() string {
	if raw := strings.TrimSpace(os.Getenv("JETTY_ADDR")); raw != "" {
		return strings.TrimPrefix(strings.TrimPrefix(raw, "http://"), "https://")
	}
	return fmt.Sprintf("127.0.0.1:%d", ListenPort())
}

// GatewayTokenPath returns the host path storing the gateway bearer token.
func GatewayTokenPath() (string, error) {
	dir, err := DataDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, "gateway-token"), nil
}

// EnsureGatewayToken loads or creates the 32-byte hex bearer token with mode 0600.
func EnsureGatewayToken() (string, error) {
	path, err := GatewayTokenPath()
	if err != nil {
		return "", err
	}
	if raw, err := os.ReadFile(path); err == nil {
		tok := strings.TrimSpace(string(raw))
		if tok != "" {
			return tok, nil
		}
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(buf)
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return "", err
	}
	if _, err := f.WriteString(tok + "\n"); err != nil {
		f.Close()
		os.Remove(tmp)
		return "", err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return "", err
	}
	if err := os.Rename(tmp, path); err != nil {
		os.Remove(tmp)
		return "", err
	}
	return tok, nil
}

// HostHasIPv4 checks whether any local network interface currently owns addr.
func HostHasIPv4(addr string) bool {
	target := net.ParseIP(addr).To4()
	if target == nil {
		return false
	}
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return false
	}
	for _, a := range addrs {
		ipNet, ok := a.(*net.IPNet)
		if !ok {
			continue
		}
		if ip4 := ipNet.IP.To4(); ip4 != nil && ip4.Equal(target) {
			return true
		}
	}
	return false
}
