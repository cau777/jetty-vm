// Package forwards supervises SSH port forwards (-L) and reverse forwards (-R).
package forwards

import (
	"bufio"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/db"
	jerr "github.com/cau777/jetty-vm/internal/errors"
	"github.com/cau777/jetty-vm/internal/validation"
)

const (
	ReadyTimeout = 15 * time.Second
	RetryMin     = 2 * time.Second
	RetryMax     = 30 * time.Second
)

// Tunnel represents an active forward process.
type Tunnel interface {
	Wait() error
	Stop() error
	LastError() string
}

// Preparer authorizes the forwarding key on the VM and returns (sshPort, hostKey).
type Preparer func(ctx context.Context, vmName, publicKey string) (int, string, error)

// Connector opens a Tunnel for a forward.
type Connector func(ctx context.Context, fwd *Forward) (Tunnel, error)

// Forward describes a supervised forward entry.
type Forward struct {
	mu         sync.Mutex
	VMName     string
	VMPort     int
	HostPort   int
	Persistent bool
	Reverse    bool
	State      string
	Error      string
	cancel     context.CancelFunc
	done       chan struct{}
}

// Snapshot is an immutable view of a Forward.
type Snapshot struct {
	VMName     string
	VMPort     int
	HostPort   int
	Persistent bool
	Reverse    bool
	State      string
	Error      string
	URL        string
}

func (f *Forward) Snapshot() Snapshot {
	f.mu.Lock()
	defer f.mu.Unlock()
	s := Snapshot{
		VMName:     f.VMName,
		VMPort:     f.VMPort,
		HostPort:   f.HostPort,
		Persistent: f.Persistent,
		Reverse:    f.Reverse,
		State:      f.State,
		Error:      f.Error,
	}
	if !f.Reverse {
		s.URL = fmt.Sprintf("http://localhost:%d", f.HostPort)
	}
	return s
}

func (f *Forward) setState(state, errMsg string) {
	f.mu.Lock()
	f.State = state
	f.Error = errMsg
	f.mu.Unlock()
}

// sshTunnel wraps an `ssh -N` subprocess.
type sshTunnel struct {
	cmd         *exec.Cmd
	mu          sync.Mutex
	lines       []string
	remoteReady chan struct{}
	readyOnce   sync.Once
	waitDone    chan error
	drainDone   chan struct{}
}

var sshChatterPrefixes = []string{"debug", "Authenticated to ", "Transferred: ", "Bytes per second: "}

func (t *sshTunnel) readStderr(r *bufio.Reader) {
	defer close(t.drainDone)
	for {
		line, err := r.ReadString('\n')
		trimmed := strings.TrimSpace(line)
		if trimmed != "" {
			if strings.HasPrefix(trimmed, "debug1: remote forward success") {
				t.readyOnce.Do(func() { close(t.remoteReady) })
			} else {
				chatter := false
				for _, p := range sshChatterPrefixes {
					if strings.HasPrefix(trimmed, p) {
						chatter = true
						break
					}
				}
				if !chatter {
					t.mu.Lock()
					if len(t.lines) >= 5 {
						t.lines = t.lines[1:]
					}
					t.lines = append(t.lines, trimmed)
					t.mu.Unlock()
				}
			}
		}
		if err != nil {
			return
		}
	}
}

func (t *sshTunnel) LastError() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	if len(t.lines) == 0 {
		return ""
	}
	return t.lines[len(t.lines)-1]
}

func (t *sshTunnel) Wait() error {
	err := <-t.waitDone
	<-t.drainDone
	return err
}

func (t *sshTunnel) Stop() error {
	if t.cmd.Process != nil {
		_ = t.cmd.Process.Signal(syscall.SIGTERM)
		select {
		case <-t.waitDone:
		case <-time.After(5 * time.Second):
			_ = t.cmd.Process.Kill()
			<-t.waitDone
		}
	}
	<-t.drainDone
	return nil
}

// NewSSHConnector builds a Connector that runs `ssh -N` using Jetty's dedicated key.
func NewSSHConnector(prepare Preparer, dir string) Connector {
	var keyMu sync.Mutex
	return func(ctx context.Context, fwd *Forward) (Tunnel, error) {
		workDir := dir
		if workDir == "" {
			d, err := config.DataDir()
			if err != nil {
				return nil, err
			}
			workDir = filepath.Join(d, "port-forwards")
		}
		if err := os.MkdirAll(workDir, 0o700); err != nil {
			return nil, err
		}
		keyPath := filepath.Join(workDir, "id_ed25519")
		keyMu.Lock()
		if _, err := os.Stat(keyPath); os.IsNotExist(err) {
			out, err := exec.CommandContext(ctx, "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "jetty-port-forward", "-f", keyPath).CombinedOutput()
			if err != nil {
				keyMu.Unlock()
				return nil, fmt.Errorf("could not create forwarding key: %s", strings.TrimSpace(string(out)))
			}
		}
		keyMu.Unlock()

		pubBytes, err := os.ReadFile(keyPath + ".pub")
		if err != nil {
			return nil, err
		}
		sshPort, hostKey, err := prepare(ctx, fwd.VMName, strings.TrimSpace(string(pubBytes)))
		if err != nil {
			return nil, err
		}
		knownHosts := filepath.Join(workDir, fwd.VMName+".known_hosts")
		if err := os.WriteFile(knownHosts, []byte(fmt.Sprintf("%s %s\n", fwd.VMName, hostKey)), 0o600); err != nil {
			return nil, err
		}

		args := []string{
			"-F", "/dev/null", "-N", "-T",
			"-i", keyPath,
			"-o", "IdentitiesOnly=yes",
			"-o", "BatchMode=yes",
			"-o", "ExitOnForwardFailure=yes",
			"-o", "ServerAliveInterval=15",
			"-o", "ServerAliveCountMax=3",
			"-o", "StrictHostKeyChecking=yes",
			"-o", "UserKnownHostsFile=" + knownHosts,
			"-o", "GlobalKnownHostsFile=/dev/null",
			"-o", "HostKeyAlias=" + fwd.VMName,
			"-o", "ForwardAgent=no",
			"-o", "ForwardX11=no",
		}
		if fwd.Reverse {
			args = append(args, "-o", "LogLevel=DEBUG1", "-R", fmt.Sprintf("127.0.0.1:%d:localhost:%d", fwd.VMPort, fwd.HostPort))
		} else {
			args = append(args, "-o", "LogLevel=ERROR", "-L", fmt.Sprintf("127.0.0.1:%d:localhost:%d", fwd.HostPort, fwd.VMPort))
		}
		args = append(args, "-p", fmt.Sprintf("%d", sshPort), "ubuntu@"+config.DefaultGatewayIP)

		cmd := exec.Command("ssh", args...)
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		stderrPipe, err := cmd.StderrPipe()
		if err != nil {
			return nil, err
		}
		if err := cmd.Start(); err != nil {
			return nil, err
		}
		tun := &sshTunnel{
			cmd:         cmd,
			remoteReady: make(chan struct{}),
			waitDone:    make(chan error, 1),
			drainDone:   make(chan struct{}),
		}
		go tun.readStderr(bufio.NewReader(stderrPipe))
		go func() {
			tun.waitDone <- cmd.Wait()
		}()

		readyCtx, cancel := context.WithTimeout(ctx, ReadyTimeout)
		defer cancel()
		if err := waitTunnelReady(readyCtx, tun, fwd); err != nil {
			_ = tun.Stop()
			return nil, err
		}
		return tun, nil
	}
}

func waitTunnelReady(ctx context.Context, tun *sshTunnel, fwd *Forward) error {
	if fwd.Reverse {
		select {
		case <-tun.remoteReady:
			return nil
		case err := <-tun.waitDone:
			<-tun.drainDone
			if msg := tun.LastError(); msg != "" {
				return errors.New(msg)
			}
			return fmt.Errorf("SSH exited: %v", err)
		case <-ctx.Done():
			return errors.New("SSH did not open the VM port in time")
		}
	}
	ticker := time.NewTicker(200 * time.Millisecond)
	defer ticker.Stop()
	addr := fmt.Sprintf("127.0.0.1:%d", fwd.HostPort)
	for {
		select {
		case err := <-tun.waitDone:
			<-tun.drainDone
			if msg := tun.LastError(); msg != "" {
				return errors.New(msg)
			}
			return fmt.Errorf("SSH exited: %v", err)
		case <-ctx.Done():
			return errors.New("SSH did not open the host port in time")
		case <-ticker.C:
			c, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
			if err == nil {
				c.Close()
				return nil
			}
		}
	}
}

type forwardKey struct {
	hostPort int
	vmName   string
	vmPort   int
	reverse  bool
}

type supervisor struct {
	mu        sync.Mutex
	conn      *sql.DB
	connector Connector
	forwards  map[forwardKey]*Forward
	onChange  func(kind, id string)
}

func (s *supervisor) startForward(fwd *Forward) *Forward {
	key := forwardKey{reverse: fwd.Reverse}
	if fwd.Reverse {
		key.vmName = fwd.VMName
		key.vmPort = fwd.VMPort
	} else {
		key.hostPort = fwd.HostPort
	}
	ctx, cancel := context.WithCancel(context.Background())
	fwd.cancel = cancel
	fwd.done = make(chan struct{})
	s.forwards[key] = fwd
	go s.supervise(ctx, fwd)
	return fwd
}

func (s *supervisor) supervise(ctx context.Context, fwd *Forward) {
	defer close(fwd.done)
	delay := RetryMin
	for {
		if ctx.Err() != nil {
			return
		}
		fwd.setState("starting", fwd.Snapshot().Error)
		s.notify(fwd)
		tun, err := s.connector(ctx, fwd)
		if ctx.Err() != nil {
			if tun != nil {
				_ = tun.Stop()
			}
			return
		}
		if err != nil {
			fwd.setState("retrying", err.Error())
			s.notify(fwd)
		} else {
			delay = RetryMin
			fwd.setState("active", "")
			s.notify(fwd)
			waitCh := make(chan error, 1)
			go func() {
				waitCh <- tun.Wait()
			}()
			var waitErr error
			select {
			case <-ctx.Done():
				_ = tun.Stop()
				<-waitCh
				return
			case waitErr = <-waitCh:
				_ = tun.Stop()
			}
			msg := tun.LastError()
			if msg == "" && waitErr != nil {
				msg = fmt.Sprintf("SSH exited: %v", waitErr)
			} else if msg == "" {
				msg = "SSH exited"
			}
			fwd.setState("retrying", msg)
			s.notify(fwd)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay):
		}
		delay *= 2
		if delay > RetryMax {
			delay = RetryMax
		}
	}
}

func (s *supervisor) notify(fwd *Forward) {
	if s.onChange == nil {
		return
	}
	if fwd.Reverse {
		s.onChange("reverse_ports", fmt.Sprintf("%s:%d", fwd.VMName, fwd.VMPort))
	} else {
		s.onChange("ports", fmt.Sprintf("%d", fwd.HostPort))
	}
}

func (s *supervisor) list(vmName string) []Snapshot {
	s.mu.Lock()
	var list []*Forward
	for _, f := range s.forwards {
		if vmName == "" || f.VMName == vmName {
			list = append(list, f)
		}
	}
	s.mu.Unlock()
	out := make([]Snapshot, 0, len(list))
	for _, f := range list {
		out = append(out, f.Snapshot())
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].VMName != out[j].VMName {
			return out[i].VMName < out[j].VMName
		}
		if out[i].HostPort != out[j].HostPort {
			return out[i].HostPort < out[j].HostPort
		}
		return out[i].VMPort < out[j].VMPort
	})
	return out
}

func (s *supervisor) stopAndWait(fwd *Forward) {
	fwd.cancel()
	<-fwd.done
}

// DropVM stops all forwards belonging to a deleted VM.
func (s *supervisor) DropVM(vmName string) {
	s.mu.Lock()
	var victim []*Forward
	for k, f := range s.forwards {
		if f.VMName == vmName {
			delete(s.forwards, k)
			victim = append(victim, f)
		}
	}
	s.mu.Unlock()
	for _, f := range victim {
		s.stopAndWait(f)
	}
}

// Shutdown stops all supervised forwards without deleting persistent SQLite rows.
func (s *supervisor) Shutdown() {
	s.mu.Lock()
	var all []*Forward
	for k, f := range s.forwards {
		delete(s.forwards, k)
		all = append(all, f)
	}
	s.mu.Unlock()
	for _, f := range all {
		s.stopAndWait(f)
	}
}

// PortManager manages host->VM Port Forwards.
type PortManager struct {
	supervisor
}

func NewPortManager(conn *sql.DB, connector Connector, onChange func(kind, id string)) *PortManager {
	return &PortManager{
		supervisor: supervisor{
			conn:      conn,
			connector: connector,
			forwards:  make(map[forwardKey]*Forward),
			onChange:  onChange,
		},
	}
}

func hostPortFree(port int) bool {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return false
	}
	ln.Close()
	return true
}

func (m *PortManager) Open(vmName string, vmPort, hostPort int, persistent bool) (Snapshot, error) {
	row, err := db.GetVM(m.conn, vmName)
	if err != nil {
		return Snapshot{}, err
	}
	if row == nil {
		return Snapshot{}, jerr.NotFound(fmt.Sprintf("VM '%s' not found", vmName))
	}
	if err := validation.ValidatePort(vmPort, "vm_port", 1); err != nil {
		return Snapshot{}, err
	}
	if hostPort == 0 {
		if vmPort < 1024 {
			return Snapshot{}, jerr.Validation("Choose a host port of 1024 or higher for this privileged VM port", map[string]string{"host_port": "required"})
		}
		hostPort = vmPort
	}
	if err := validation.ValidatePort(hostPort, "host_port", 1024); err != nil {
		return Snapshot{}, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	key := forwardKey{hostPort: hostPort}
	if existing, ok := m.forwards[key]; ok {
		return Snapshot{}, jerr.Conflict(fmt.Sprintf("Host port %d already forwards to %s:%d", hostPort, existing.VMName, existing.VMPort), map[string]string{"host_port": "in use"})
	}
	if !hostPortFree(hostPort) {
		return Snapshot{}, jerr.Conflict(fmt.Sprintf("Host port %d is already in use on this computer", hostPort), map[string]string{"host_port": "in use"})
	}
	if persistent {
		if err := db.PutPortForward(m.conn, hostPort, vmName, vmPort); err != nil {
			return Snapshot{}, err
		}
	}
	fwd := m.startForward(&Forward{
		VMName:     vmName,
		VMPort:     vmPort,
		HostPort:   hostPort,
		Persistent: persistent,
		State:      "starting",
	})
	return fwd.Snapshot(), nil
}

func (m *PortManager) List(vmName string) []Snapshot {
	return m.list(vmName)
}

func (m *PortManager) SetPersistent(vmName string, hostPort int, persistent bool) (Snapshot, error) {
	m.mu.Lock()
	fwd, ok := m.forwards[forwardKey{hostPort: hostPort}]
	if !ok || fwd.VMName != vmName {
		m.mu.Unlock()
		return Snapshot{}, jerr.NotFound(fmt.Sprintf("No port forward on host port %d for VM '%s'", hostPort, vmName))
	}
	if persistent {
		if err := db.PutPortForward(m.conn, hostPort, vmName, fwd.VMPort); err != nil {
			m.mu.Unlock()
			return Snapshot{}, err
		}
	} else {
		if err := db.DeletePortForward(m.conn, hostPort); err != nil {
			m.mu.Unlock()
			return Snapshot{}, err
		}
	}
	fwd.mu.Lock()
	fwd.Persistent = persistent
	fwd.mu.Unlock()
	m.mu.Unlock()
	m.notify(fwd)
	return fwd.Snapshot(), nil
}

func (m *PortManager) Close(vmName string, hostPort int) error {
	m.mu.Lock()
	key := forwardKey{hostPort: hostPort}
	fwd, ok := m.forwards[key]
	if !ok || fwd.VMName != vmName {
		m.mu.Unlock()
		return jerr.NotFound(fmt.Sprintf("No port forward on host port %d for VM '%s'", hostPort, vmName))
	}
	delete(m.forwards, key)
	m.mu.Unlock()
	_ = db.DeletePortForward(m.conn, hostPort)
	m.stopAndWait(fwd)
	m.notify(fwd)
	return nil
}

func (m *PortManager) StartPersistent() error {
	rows, err := db.ListPortForwards(m.conn)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range rows {
		key := forwardKey{hostPort: r.HostPort}
		if _, ok := m.forwards[key]; !ok {
			m.startForward(&Forward{
				VMName:     r.VMName,
				VMPort:     r.VMPort,
				HostPort:   r.HostPort,
				Persistent: true,
				State:      "starting",
			})
		}
	}
	return nil
}

// ReverseManager manages VM->host Reverse Forwards.
type ReverseManager struct {
	supervisor
}

func NewReverseManager(conn *sql.DB, connector Connector, onChange func(kind, id string)) *ReverseManager {
	return &ReverseManager{
		supervisor: supervisor{
			conn:      conn,
			connector: connector,
			forwards:  make(map[forwardKey]*Forward),
			onChange:  onChange,
		},
	}
}

func (m *ReverseManager) Open(vmName string, hostPort, vmPort int, persistent bool) (Snapshot, error) {
	row, err := db.GetVM(m.conn, vmName)
	if err != nil {
		return Snapshot{}, err
	}
	if row == nil {
		return Snapshot{}, jerr.NotFound(fmt.Sprintf("VM '%s' not found", vmName))
	}
	if err := validation.ValidatePort(hostPort, "host_port", 1); err != nil {
		return Snapshot{}, err
	}
	if vmPort == 0 {
		if hostPort < 1024 {
			return Snapshot{}, jerr.Validation("Choose a VM port of 1024 or higher for this privileged host port", map[string]string{"vm_port": "required"})
		}
		vmPort = hostPort
	}
	if err := validation.ValidatePort(vmPort, "vm_port", 1024); err != nil {
		return Snapshot{}, err
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	key := forwardKey{vmName: vmName, vmPort: vmPort, reverse: true}
	if existing, ok := m.forwards[key]; ok {
		return Snapshot{}, jerr.Conflict(fmt.Sprintf("VM port %d on %s already reaches host port %d", vmPort, vmName, existing.HostPort), map[string]string{"vm_port": "in use"})
	}
	if persistent {
		if err := db.PutReverseForward(m.conn, vmName, vmPort, hostPort); err != nil {
			return Snapshot{}, err
		}
	}
	fwd := m.startForward(&Forward{
		VMName:     vmName,
		VMPort:     vmPort,
		HostPort:   hostPort,
		Persistent: persistent,
		Reverse:    true,
		State:      "starting",
	})
	return fwd.Snapshot(), nil
}

func (m *ReverseManager) List(vmName string) []Snapshot {
	return m.list(vmName)
}

func (m *ReverseManager) SetPersistent(vmName string, vmPort int, persistent bool) (Snapshot, error) {
	m.mu.Lock()
	fwd, ok := m.forwards[forwardKey{vmName: vmName, vmPort: vmPort, reverse: true}]
	if !ok {
		m.mu.Unlock()
		return Snapshot{}, jerr.NotFound(fmt.Sprintf("No reverse forward on VM port %d for VM '%s'", vmPort, vmName))
	}
	if persistent {
		if err := db.PutReverseForward(m.conn, vmName, vmPort, fwd.HostPort); err != nil {
			m.mu.Unlock()
			return Snapshot{}, err
		}
	} else {
		if err := db.DeleteReverseForward(m.conn, vmName, vmPort); err != nil {
			m.mu.Unlock()
			return Snapshot{}, err
		}
	}
	fwd.mu.Lock()
	fwd.Persistent = persistent
	fwd.mu.Unlock()
	m.mu.Unlock()
	m.notify(fwd)
	return fwd.Snapshot(), nil
}

func (m *ReverseManager) Close(vmName string, vmPort int) error {
	m.mu.Lock()
	key := forwardKey{vmName: vmName, vmPort: vmPort, reverse: true}
	fwd, ok := m.forwards[key]
	if !ok {
		m.mu.Unlock()
		return jerr.NotFound(fmt.Sprintf("No reverse forward on VM port %d for VM '%s'", vmPort, vmName))
	}
	delete(m.forwards, key)
	m.mu.Unlock()
	_ = db.DeleteReverseForward(m.conn, vmName, vmPort)
	m.stopAndWait(fwd)
	m.notify(fwd)
	return nil
}

func (m *ReverseManager) StartPersistent() error {
	rows, err := db.ListReverseForwards(m.conn)
	if err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range rows {
		key := forwardKey{vmName: r.VMName, vmPort: r.VMPort, reverse: true}
		if _, ok := m.forwards[key]; !ok {
			m.startForward(&Forward{
				VMName:     r.VMName,
				VMPort:     r.VMPort,
				HostPort:   r.HostPort,
				Persistent: true,
				Reverse:    true,
				State:      "starting",
			})
		}
	}
	return nil
}
