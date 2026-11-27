package lxd

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/json"
	"fmt"
	"io/fs"
	"net"
	"net/http"
	"net/url"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	jettyvm "github.com/cau777/jetty-vm"
	"github.com/cau777/jetty-vm/internal/ca"
	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/db"
	jerr "github.com/cau777/jetty-vm/internal/errors"
	"github.com/cau777/jetty-vm/internal/sshconfig"
	"github.com/cau777/jetty-vm/internal/validation"
)

const (
	Project           = "jetty"
	UpNet             = "jettyup0"
	UpAddr            = "10.201.0.1"
	PrivNet           = "jettypriv0"
	PrivPrefix        = "10.202.0"
	GatewayName       = "jetty-gw"
	GatewayUpIP       = "10.201.0.2"
	GatewayPrivIP     = "10.202.0.1"
	GatewayCPUs       = "2"
	GatewayMemory     = "1GiB"
	GatewayProxyPort  = 8080
	AgentImage        = "ubuntu:24.04"
	SSHPortBase       = 2200
	FreePageReporting = "[device \"qemu_balloon\"]\nfree-page-reporting = \"on\"\n"

	PinnedMitmproxyPkg = "mitmproxy==10.4.2"
	PinnedGrpcioPkg    = "grpcio==1.68.1"
	PinnedProtobufPkg  = "protobuf==5.27.3"
)

var gatewayImageSource = map[string]string{
	"type":     "image",
	"mode":     "pull",
	"protocol": "simplestreams",
	"server":   "https://cloud-images.ubuntu.com/minimal/releases/",
	"alias":    "24.04",
}

// VMSummary describes an LXD instance.
type VMSummary struct {
	Name         string
	IPAddress    string
	InstanceType string
	Status       string
	CPUs         string
	Memory       string
	Disk         string
}

// GatewayStatus summarizes LXD, gateway, and VM state.
type GatewayStatus struct {
	Socket              string
	LXDAvailable        bool
	Gateway             string
	GatewayService      string
	GatewayAgentService string
	VMs                 []VMSummary
	Message             string
}

// Manager owns Jetty's LXD project, networks, gateway, and registered guests.
type Manager struct {
	DB          *sql.DB
	ListenPort  int
	OpenClient  func() (Client, error)
	SSHDir      string
	repushMu    sync.Mutex
	repushing   bool
	onVMChanged func(kind, id string)
}

// NewManager creates an LXD Manager using the real Unix socket.
func NewManager(dbConn *sql.DB, listenPort int, onChange func(kind, id string)) *Manager {
	if listenPort <= 0 {
		listenPort = config.ListenPort()
	}
	return &Manager{
		DB:         dbConn,
		ListenPort: listenPort,
		OpenClient: func() (Client, error) {
			return NewUnixClient(Project, "")
		},
		onVMChanged: onChange,
	}
}

func (m *Manager) notify(kind, id string) {
	if m.onVMChanged != nil {
		m.onVMChanged(kind, id)
	}
}

func DefaultSSHKey(keyPath string) (string, error) {
	var candidates []string
	if keyPath != "" {
		candidates = []string{keyPath}
	} else {
		home, _ := os.UserHomeDir()
		candidates = []string{
			filepath.Join(home, ".ssh", "id_ed25519.pub"),
			filepath.Join(home, ".ssh", "id_ecdsa.pub"),
			filepath.Join(home, ".ssh", "id_rsa.pub"),
		}
	}
	for _, c := range candidates {
		raw, err := os.ReadFile(c)
		if err != nil {
			continue
		}
		return validation.ValidateSSHKey(string(raw))
	}
	return "", jerr.Validation("No SSH public key was found. Add ~/.ssh/id_ed25519.pub or paste a public key in setup.", map[string]string{"ssh_public_key": "required"})
}

func imageSource(image string) (map[string]string, error) {
	alias, err := validation.ValidateImage(image)
	if err != nil {
		return nil, err
	}
	return map[string]string{
		"type":     "image",
		"mode":     "pull",
		"protocol": "simplestreams",
		"server":   "https://cloud-images.ubuntu.com/releases/",
		"alias":    alias,
	}, nil
}

func cloudConfig(sshKey string, packages []string, caCertPEM string) string {
	var b strings.Builder
	b.WriteString("#cloud-config\nssh_authorized_keys:\n")
	keyJSON, _ := json.Marshal(sshKey)
	b.WriteString("  - " + string(keyJSON) + "\n")
	if len(packages) > 0 {
		b.WriteString("packages:\n")
		for _, p := range packages {
			pj, _ := json.Marshal(p)
			b.WriteString("  - " + string(pj) + "\n")
		}
	}
	if caCertPEM != "" {
		b.WriteString("write_files:\n")
		b.WriteString("  - path: /usr/local/share/ca-certificates/jetty-interception-ca.crt\n")
		b.WriteString("    owner: root:root\n")
		b.WriteString("    permissions: '0644'\n")
		b.WriteString("    content: |\n")
		for _, line := range strings.Split(strings.TrimRight(caCertPEM, "\r\n"), "\n") {
			b.WriteString("      " + line + "\n")
		}
		b.WriteString("runcmd:\n  - [update-ca-certificates]\n")
	}
	return b.String()
}

func cloudNetworkConfig(mac, address, instanceType string) string {
	matchLine := fmt.Sprintf("    match: {macaddress: %s}", mustJSON(mac))
	if instanceType == "container" {
		matchLine = "    match: {name: eth0}"
	}
	return strings.Join([]string{
		"version: 2",
		"ethernets:",
		"  lan:",
		matchLine,
		fmt.Sprintf("    addresses: [%s/24]", address),
		fmt.Sprintf("    routes: [{to: default, via: %s}]", GatewayPrivIP),
		"    nameservers: {addresses: [1.1.1.1, 9.9.9.9]}",
		"",
	}, "\n")
}

func mustJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}

func requireSuccess(res ExecResult, cmd []string, instance string) error {
	if res.ExitCode == 0 {
		return nil
	}
	detail := strings.TrimSpace(string(res.Stderr))
	if detail == "" {
		detail = strings.TrimSpace(string(res.Stdout))
	}
	msg := fmt.Sprintf("%s: %s exited %d", instance, strings.Join(cmd, " "), res.ExitCode)
	if detail != "" {
		msg += ": " + detail
	}
	return jerr.ServiceUnavailable(msg)
}

// Status reports live LXD, gateway, and VM status.
func (m *Manager) Status(ctx context.Context) GatewayStatus {
	res := GatewayStatus{
		Socket:              SocketPath(),
		Gateway:             "missing",
		GatewayService:      "unknown",
		GatewayAgentService: "unknown",
	}
	if !SocketAccessible(res.Socket) {
		res.Message = "LXD is unavailable or this login cannot access its socket."
		return res
	}
	client, err := m.OpenClient()
	if err != nil {
		res.Message = err.Error()
		return res
	}
	emptyProj := ""
	if _, err := client.Request(ctx, http.MethodGet, "/1.0", nil, &emptyProj, true); err != nil {
		res.Message = err.Error()
		return res
	}
	res.LXDAvailable = true
	instances, err := m.instances(ctx, client)
	if err != nil {
		res.Message = err.Error()
		return res
	}
	var gw map[string]any
	for _, inst := range instances {
		name, _ := inst["name"].(string)
		if name == GatewayName {
			gw = inst
			continue
		}
		res.VMs = append(res.VMs, vmSummary(inst))
	}
	if gw == nil {
		return res
	}
	st, _ := gw["status"].(string)
	if st == "" {
		st = "unknown"
	}
	res.Gateway = strings.ToLower(st)
	if res.Gateway != "running" {
		return res
	}
	if svc, err := client.Execute(ctx, GatewayName, []string{"systemctl", "is-active", "jetty-gateway.service"}, ExecOpts{}); err == nil {
		out := strings.TrimSpace(string(svc.Stdout))
		if out == "" {
			out = "inactive"
		}
		res.GatewayService = out
	}
	if agentSvc, err := client.Execute(ctx, GatewayName, []string{"systemctl", "is-active", "jetty-gw-agent.service"}, ExecOpts{}); err == nil {
		out := strings.TrimSpace(string(agentSvc.Stdout))
		if out == "" {
			out = "inactive"
		}
		res.GatewayAgentService = out
	}
	return res
}

// SetupGateway provisions LXD project/networks, creates/starts jetty-gw, installs jetty-gw-agent, and syncs ssh_config.
func (m *Manager) SetupGateway(ctx context.Context, sshKey string, progress func(step string)) error {
	report := func(step string) {
		if progress != nil {
			progress(step)
		}
	}
	report("validate")
	var err error
	if sshKey != "" {
		sshKey, err = validation.ValidateSSHKey(sshKey)
	} else {
		sshKey, err = DefaultSSHKey("")
	}
	if err != nil {
		return err
	}
	client, err := m.OpenClient()
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}

	report("project")
	if err := m.ensureProject(ctx, client); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	report("profile")
	if err := m.ensureProfile(ctx, client); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	report("networks")
	if err := m.ensureNetwork(ctx, client, UpNet, map[string]string{
		"ipv4.address": UpAddr + "/24",
		"ipv4.nat":     "true",
		"ipv6.address": "none",
	}); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if err := m.ensureNetwork(ctx, client, PrivNet, map[string]string{
		"ipv4.address": "none",
		"ipv6.address": "none",
	}); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}

	gw, err := m.instance(ctx, client, GatewayName)
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if gw == nil {
		report("instance")
		userData := cloudConfig(sshKey, []string{"iptables", "python3", "python3-venv"}, "")
		_, err = client.Request(ctx, http.MethodPost, "/1.0/instances", map[string]any{
			"name":     GatewayName,
			"type":     "virtual-machine",
			"source":   gatewayImageSource,
			"profiles": []string{"default"},
			"config": map[string]any{
				"limits.cpu":          GatewayCPUs,
				"limits.memory":       GatewayMemory,
				"cloud-init.user-data": userData,
			},
			"devices": map[string]any{
				"root": map[string]any{"type": "disk", "pool": "default", "path": "/", "size": "10GiB"},
				"eth0": map[string]any{"type": "nic", "network": UpNet, "ipv4.address": GatewayUpIP},
				"eth1": map[string]any{"type": "nic", "network": PrivNet},
			},
		}, nil, true)
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
	}

	report("start")
	if err := m.startInstance(ctx, client, GatewayName); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	report("guest_agent")
	if err := m.waitForAgent(ctx, client, GatewayName); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	report("cloud_init")
	ciRes, err := client.Execute(ctx, GatewayName, []string{"cloud-init", "status", "--wait"}, ExecOpts{})
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if err := requireSuccess(ciRes, []string{"cloud-init", "status", "--wait"}, GatewayName); err != nil {
		return err
	}

	report("configure")
	if err := m.configureGateway(ctx, client, true); err != nil {
		return err
	}
	report("ssh_config")
	return m.syncSSHConfig(ctx)
}

// RepushGatewayAgent pushes updated jetty-gw-agent code and restarts jetty-gw-agent.service (single-flight).
func (m *Manager) RepushGatewayAgent(ctx context.Context) error {
	m.repushMu.Lock()
	if m.repushing {
		m.repushMu.Unlock()
		return nil
	}
	m.repushing = true
	m.repushMu.Unlock()
	defer func() {
		m.repushMu.Lock()
		m.repushing = false
		m.repushMu.Unlock()
	}()

	client, err := m.OpenClient()
	if err != nil {
		return err
	}
	return m.configureGateway(ctx, client, false)
}

func (m *Manager) configureGateway(ctx context.Context, client Client, installDeps bool) error {
	gw, err := m.instance(ctx, client, GatewayName)
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if gw == nil {
		return jerr.ServiceUnavailable("LXD did not return the Jetty gateway")
	}
	cfgMap, _ := gw["config"].(map[string]any)
	upMAC, _ := cfgMap["volatile.eth0.hwaddr"].(string)
	privMAC, _ := cfgMap["volatile.eth1.hwaddr"].(string)
	if upMAC == "" || privMAC == "" {
		return jerr.ServiceUnavailable("LXD did not assign network MAC addresses to the Jetty gateway")
	}

	token, err := config.EnsureGatewayToken()
	if err != nil {
		return err
	}

	for _, cmd := range [][]string{
		{"install", "-d", "-m", "700", "/etc/jetty-gateway"},
		{"install", "-d", "-m", "755", "/opt/jetty-gw-agent/src/jetty_gw_agent/proto/jetty/v1"},
	} {
		res, err := client.Execute(ctx, GatewayName, cmd, ExecOpts{})
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
		if err := requireSuccess(res, cmd, GatewayName); err != nil {
			return err
		}
	}

	if installDeps {
		installCmd := []string{
			"bash", "-c",
			fmt.Sprintf(
				"set -euo pipefail; "+
					"if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then "+
					"DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables python3 python3-venv; "+
					"fi; "+
					"if [ ! -x /opt/jetty-gw-agent/.venv/bin/mitmdump ]; then "+
					"python3 -m venv /opt/jetty-gw-agent/.venv; "+
					"/opt/jetty-gw-agent/.venv/bin/pip install --quiet --upgrade pip; "+
					"/opt/jetty-gw-agent/.venv/bin/pip install --quiet %s %s %s; "+
					"fi",
				PinnedMitmproxyPkg, PinnedGrpcioPkg, PinnedProtobufPkg,
			),
		}
		res, err := client.Execute(ctx, GatewayName, installCmd, ExecOpts{})
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
		if err := requireSuccess(res, []string{"install gateway dependencies"}, GatewayName); err != nil {
			return err
		}
	}

	// Push token and env
	if err := client.PutFile(ctx, GatewayName, "/etc/jetty-gateway/token", []byte(token+"\n"), 0o600, 0, 0); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	envContent := fmt.Sprintf(
		"UP_MAC=%s\nPRIV_MAC=%s\nPRIV_ADDR=%s/24\nHOST_ADDR=%s\nSSH_PORT_BASE=%d\nPROXY_PORT=%d\nJETTY_DAEMON_ADDR=%s:%d\nJETTY_GATEWAY_TOKEN_FILE=/etc/jetty-gateway/token\nPYTHONPATH=/opt/jetty-gw-agent/src\n",
		upMAC, privMAC, GatewayPrivIP, UpAddr, SSHPortBase, GatewayProxyPort, UpAddr, m.ListenPort,
	)
	if err := client.PutFile(ctx, GatewayName, "/etc/jetty-gateway/env", []byte(envContent), 0o600, 0, 0); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}

	// Push gateway-agent files from embedded FS
	agentFiles := []string{
		"pyproject.toml",
		"run_addon.py",
		"src/jetty_gw_agent/__init__.py",
		"src/jetty_gw_agent/__main__.py",
		"src/jetty_gw_agent/addon.py",
		"src/jetty_gw_agent/version.py",
		"src/jetty_gw_agent/proto/__init__.py",
		"src/jetty_gw_agent/proto/jetty/__init__.py",
		"src/jetty_gw_agent/proto/jetty/v1/__init__.py",
		"src/jetty_gw_agent/proto/jetty/v1/jetty_pb2.py",
		"src/jetty_gw_agent/proto/jetty/v1/jetty_pb2_grpc.py",
	}
	for _, rel := range agentFiles {
		data, err := fs.ReadFile(jettyvm.Assets, "gateway-agent/"+rel)
		if err != nil {
			return err
		}
		if err := client.PutFile(ctx, GatewayName, "/opt/jetty-gw-agent/"+rel, data, 0o644, 0, 0); err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
	}
	if installDeps {
		res, err := client.Execute(ctx, GatewayName, []string{"/opt/jetty-gw-agent/.venv/bin/pip", "install", "--quiet", "--no-deps", "--editable", "/opt/jetty-gw-agent"}, ExecOpts{})
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
		if err := requireSuccess(res, []string{"install jetty-gw-agent"}, GatewayName); err != nil {
			return err
		}
	}

	// Push iptables script and systemd units
	gwUp, err := fs.ReadFile(jettyvm.Assets, "deploy/gateway/jetty-gateway-up")
	if err != nil {
		return err
	}
	gwSvc, err := fs.ReadFile(jettyvm.Assets, "deploy/gateway/jetty-gateway.service")
	if err != nil {
		return err
	}
	agentSvc, err := fs.ReadFile(jettyvm.Assets, "deploy/gateway/jetty-gw-agent.service")
	if err != nil {
		return err
	}
	if err := client.PutFile(ctx, GatewayName, "/usr/local/sbin/jetty-gateway-up", gwUp, 0o755, 0, 0); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if err := client.PutFile(ctx, GatewayName, "/etc/systemd/system/jetty-gateway.service", gwSvc, 0o644, 0, 0); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if err := client.PutFile(ctx, GatewayName, "/etc/systemd/system/jetty-gw-agent.service", agentSvc, 0o644, 0, 0); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}

	cmds := [][]string{
		{"systemctl", "daemon-reload"},
		{"systemctl", "enable", "jetty-gateway.service", "jetty-gw-agent.service"},
		{"systemctl", "restart", "jetty-gateway.service"},
		{"systemctl", "restart", "jetty-gw-agent.service"},
	}
	if !installDeps {
		cmds = [][]string{
			{"systemctl", "daemon-reload"},
			{"systemctl", "restart", "jetty-gw-agent.service"},
		}
	}
	for _, cmd := range cmds {
		res, err := client.Execute(ctx, GatewayName, cmd, ExecOpts{})
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
		if err := requireSuccess(res, cmd, GatewayName); err != nil {
			return err
		}
	}
	return nil
}

// CreateVM provisions, registers, starts, and waits for cloud-init on a new agent VM.
func (m *Manager) CreateVM(
	ctx context.Context,
	name string,
	cpus int,
	memory, disk, image, instanceType, sshKey string,
	progress func(step string),
) (VMSummary, error) {
	report := func(step string) {
		if progress != nil {
			progress(step)
		}
	}
	report("validate")
	name, err := validation.ValidateName(name, "name")
	if err != nil {
		return VMSummary{}, err
	}
	if name == GatewayName {
		return VMSummary{}, jerr.Validation("The gateway name is reserved", map[string]string{"name": "reserved"})
	}
	if cpus <= 0 {
		cpus = 4
	}
	if cpus < 1 || cpus > 128 {
		return VMSummary{}, jerr.Validation("cpus must be between 1 and 128", map[string]string{"cpus": "invalid value"})
	}
	if memory == "" {
		memory = "8GiB"
	}
	if _, err := validation.ValidateSize(memory, "memory"); err != nil {
		return VMSummary{}, err
	}
	if disk == "" {
		disk = "40GiB"
	}
	if _, err := validation.ValidateSize(disk, "disk"); err != nil {
		return VMSummary{}, err
	}
	if image == "" {
		image = AgentImage
	}
	imgSrc, err := imageSource(image)
	if err != nil {
		return VMSummary{}, err
	}
	if instanceType == "" {
		instanceType = "virtual-machine"
	}
	if instanceType != "virtual-machine" && instanceType != "container" {
		return VMSummary{}, jerr.Validation("instance_type must be virtual-machine or container", map[string]string{"instance_type": "invalid value"})
	}
	if sshKey != "" {
		sshKey, err = validation.ValidateSSHKey(sshKey)
	} else {
		sshKey, err = DefaultSSHKey("")
	}
	if err != nil {
		return VMSummary{}, err
	}

	client, err := m.OpenClient()
	if err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	gw, err := m.instance(ctx, client, GatewayName)
	if err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	if gw == nil {
		return VMSummary{}, jerr.Conflict("Run Jetty setup before creating a VM", nil)
	}
	existing, err := m.instance(ctx, client, name)
	if err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	if existing != nil {
		return VMSummary{}, jerr.Conflict(fmt.Sprintf("VM '%s' already exists", name), nil)
	}

	ip, err := m.nextIP(ctx, client)
	if err != nil {
		return VMSummary{}, err
	}
	if instanceType == "container" {
		if err := m.ensureContainerProfile(ctx, client); err != nil {
			return VMSummary{}, jerr.ServiceUnavailable(err.Error())
		}
	} else {
		if err := m.ensureProfile(ctx, client); err != nil {
			return VMSummary{}, jerr.ServiceUnavailable(err.Error())
		}
	}
	caRow, err := ca.Get(m.DB)
	if err != nil || caRow == nil {
		return VMSummary{}, jerr.ServiceUnavailable("Jetty's interception CA is not initialized")
	}

	rootDisk := map[string]any{"type": "disk", "pool": "default", "path": "/", "size": disk}
	if instanceType == "container" {
		emptyProj := ""
		poolRaw, err := client.Request(ctx, http.MethodGet, "/1.0/storage-pools/default", nil, &emptyProj, true)
		if err == nil {
			if pool, ok := poolRaw.(map[string]any); ok && pool["driver"] == "dir" {
				delete(rootDisk, "size")
			}
		}
	}
	rnd := make([]byte, 3)
	_, _ = rand.Read(rnd)
	mac := fmt.Sprintf("00:16:3e:%02x:%02x:%02x", rnd[0], rnd[1], rnd[2])

	cfg := map[string]any{
		"limits.cpu":                 strconv.Itoa(cpus),
		"limits.memory":              memory,
		"cloud-init.user-data":       cloudConfig(sshKey, nil, caRow.CertificatePEM),
		"cloud-init.network-config":  cloudNetworkConfig(mac, ip, instanceType),
	}
	eth0 := map[string]any{
		"type":                    "nic",
		"network":                 PrivNet,
		"hwaddr":                  mac,
		"ipv4.address":            ip,
		"security.ipv4_filtering": "true",
		"security.ipv6_filtering": "true",
		"security.mac_filtering":  "true",
		"security.port_isolation": "true",
	}
	profile := "default"
	if instanceType == "container" {
		profile = "container"
		cfg["security.privileged"] = "false"
		eth0["name"] = "eth0"
	}

	report("instance")
	_, err = client.Request(ctx, http.MethodPost, "/1.0/instances", map[string]any{
		"name":     name,
		"type":     instanceType,
		"source":   imgSrc,
		"profiles": []string{profile},
		"config":   cfg,
		"devices":  map[string]any{"root": rootDisk, "eth0": eth0},
	}, nil, true)
	if err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}

	report("register")
	if _, _, err := db.PutVM(m.DB, name, ip); err != nil {
		return VMSummary{}, err
	}
	_ = m.syncSSHConfig(ctx)
	m.notify("vms", name)

	report("start")
	if err := m.startInstance(ctx, client, name); err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	report("guest_agent")
	if err := m.waitForAgent(ctx, client, name); err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	report("cloud_init")
	ciRes, err := client.Execute(ctx, name, []string{"cloud-init", "status", "--wait"}, ExecOpts{})
	if err != nil {
		return VMSummary{}, jerr.ServiceUnavailable(err.Error())
	}
	if err := requireSuccess(ciRes, []string{"cloud-init", "status", "--wait"}, name); err != nil {
		return VMSummary{}, err
	}
	report("ready")
	inst, _ := m.instance(ctx, client, name)
	status := "Running"
	if inst != nil {
		if st, _ := inst["status"].(string); st != "" {
			status = st
		}
	}
	m.notify("vms", name)
	return VMSummary{Name: name, IPAddress: ip, InstanceType: instanceType, Status: status}, nil
}

// DeleteVM removes the LXD instance (if present), deletes its DB row, and updates ssh_config.
func (m *Manager) DeleteVM(ctx context.Context, name string) error {
	name, err := validation.ValidateName(name, "name")
	if err != nil {
		return err
	}
	if name == GatewayName {
		return jerr.Conflict("The Jetty gateway cannot be deleted as an agent VM", nil)
	}
	if client, err := m.OpenClient(); err == nil {
		inst, err := m.instance(ctx, client, name)
		if err != nil {
			return jerr.ServiceUnavailable(err.Error())
		}
		if inst != nil {
			if st, _ := inst["status"].(string); st != "Stopped" {
				_, err = client.Request(ctx, http.MethodPut, fmt.Sprintf("/1.0/instances/%s/state", url.PathEscape(name)), map[string]any{
					"action": "stop", "force": true, "timeout": -1,
				}, nil, true)
				if err != nil {
					return jerr.ServiceUnavailable(err.Error())
				}
			}
			_, err = client.Request(ctx, http.MethodDelete, fmt.Sprintf("/1.0/instances/%s", url.PathEscape(name)), nil, nil, true)
			if err != nil {
				return jerr.ServiceUnavailable(err.Error())
			}
		}
	} else {
		// Allow DB deletion when LXD isn't in use (e.g. unit/contract environments).
		deleted, delErr := db.DeleteVM(m.DB, name)
		if delErr != nil {
			return delErr
		}
		if !deleted {
			return jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
		}
		m.notify("vms", name)
		return nil
	}
	_, err = db.DeleteVM(m.DB, name)
	if err != nil {
		return err
	}
	_ = m.syncSSHConfig(ctx)
	m.notify("vms", name)
	return nil
}

// VMAction executes start, stop, or restart on an agent VM.
func (m *Manager) VMAction(ctx context.Context, name, action string) (string, error) {
	name, err := validation.ValidateName(name, "name")
	if err != nil {
		return "", err
	}
	if name == GatewayName {
		return "", jerr.Conflict("Use Jetty setup to manage the gateway", nil)
	}
	if action != "start" && action != "stop" && action != "restart" {
		return "", jerr.Validation("action must be start, stop, or restart", map[string]string{"action": "invalid value"})
	}
	client, err := m.OpenClient()
	if err != nil {
		return "", jerr.ServiceUnavailable(err.Error())
	}
	inst, err := m.instance(ctx, client, name)
	if err != nil {
		return "", jerr.ServiceUnavailable(err.Error())
	}
	if inst == nil {
		return "", jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
	}
	instType, _ := inst["type"].(string)
	if action != "stop" && (instType == "" || instType == "virtual-machine") {
		_ = m.ensureProfile(ctx, client)
	}
	status, _ := inst["status"].(string)
	if action == "start" || (action == "restart" && status == "Stopped") {
		if err := m.startInstance(ctx, client, name); err != nil {
			return "", jerr.ServiceUnavailable(err.Error())
		}
	} else if action == "stop" && status == "Stopped" {
		// already stopped
	} else {
		_, err = client.Request(ctx, http.MethodPut, fmt.Sprintf("/1.0/instances/%s/state", url.PathEscape(name)), map[string]any{
			"action": action, "force": true, "timeout": 60,
		}, nil, true)
		if err != nil {
			return "", jerr.ServiceUnavailable(err.Error())
		}
	}
	updated, _ := m.instance(ctx, client, name)
	outStatus := "unknown"
	if updated != nil {
		if st, _ := updated["status"].(string); st != "" {
			outStatus = st
		}
	}
	m.notify("vms", name)
	return outStatus, nil
}

// ExecuteVM runs a command as ubuntu inside a registered VM.
func (m *Manager) ExecuteVM(ctx context.Context, name string, command []string) (ExecResult, error) {
	name, err := validation.ValidateName(name, "name")
	if err != nil {
		return ExecResult{}, err
	}
	if name == GatewayName {
		return ExecResult{}, jerr.Conflict("The gateway is managed by Jetty", nil)
	}
	if len(command) == 0 {
		return ExecResult{}, jerr.Validation("command must be a non-empty list of strings", map[string]string{"command": "invalid value"})
	}
	client, err := m.OpenClient()
	if err != nil {
		return ExecResult{}, jerr.ServiceUnavailable(err.Error())
	}
	inst, err := m.instance(ctx, client, name)
	if err != nil {
		return ExecResult{}, jerr.ServiceUnavailable(err.Error())
	}
	if inst == nil {
		return ExecResult{}, jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
	}
	res, err := client.Execute(ctx, name, command, ExecOpts{
		User:  1000,
		Group: 1000,
		Cwd:   "/home/ubuntu",
		Environment: map[string]string{
			"HOME":    "/home/ubuntu",
			"USER":    "ubuntu",
			"LOGNAME": "ubuntu",
			"PATH":    "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		},
	})
	if err != nil {
		return ExecResult{}, jerr.ServiceUnavailable(err.Error())
	}
	return res, nil
}

// UploadVMFile uploads bytes to an absolute guest path.
func (m *Manager) UploadVMFile(ctx context.Context, name, guestPath string, content []byte, mode int, owner string) error {
	name, err := validation.ValidateName(name, "name")
	if err != nil {
		return err
	}
	if name == GatewayName {
		return jerr.Conflict("The gateway is managed by Jetty", nil)
	}
	if !strings.HasPrefix(guestPath, "/") || guestPath == "/" || strings.Contains(guestPath, "..") {
		return jerr.Validation("path must be an absolute guest file path", map[string]string{"path": "invalid path"})
	}
	if owner == "" {
		owner = "ubuntu"
	}
	if owner != "ubuntu" && owner != "root" {
		return jerr.Validation("owner must be ubuntu or root", map[string]string{"owner": "invalid value"})
	}
	if mode < 0 || mode > 0o777 {
		return jerr.Validation("mode must be a file mode from 0000 to 0777", map[string]string{"mode": "invalid mode"})
	}
	uid := 1000
	if owner == "root" {
		uid = 0
	}
	client, err := m.OpenClient()
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	inst, err := m.instance(ctx, client, name)
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if inst == nil {
		return jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
	}
	parent := path.Dir(guestPath)
	mkRes, err := client.Execute(ctx, name, []string{"install", "-d", "-m", "0755", parent}, ExecOpts{})
	if err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	if err := requireSuccess(mkRes, []string{"install", "-d", "-m", "0755", parent}, name); err != nil {
		return err
	}
	if err := client.PutFile(ctx, name, guestPath, content, mode, uid, uid); err != nil {
		return jerr.ServiceUnavailable(err.Error())
	}
	return nil
}

// ListVMs lists agent VMs from LXD (falling back to SQLite rows if LXD is unavailable).
func (m *Manager) ListVMs(ctx context.Context) ([]VMSummary, error) {
	rows, err := db.ListVMs(m.DB)
	if err != nil {
		return nil, err
	}
	dbMap := make(map[string]db.VMRow, len(rows))
	for _, r := range rows {
		dbMap[r.Name] = r
	}
	client, err := m.OpenClient()
	if err != nil {
		// Fall back to registered SQLite rows when LXD isn't available.
		out := make([]VMSummary, 0, len(rows))
		for _, r := range rows {
			out = append(out, VMSummary{
				Name:         r.Name,
				IPAddress:    r.IPAddress,
				InstanceType: "virtual-machine",
				Status:       "Unknown",
			})
		}
		return out, nil
	}
	instances, err := m.instances(ctx, client)
	if err != nil {
		return nil, jerr.ServiceUnavailable(err.Error())
	}
	var out []VMSummary
	seen := make(map[string]bool)
	for _, inst := range instances {
		name, _ := inst["name"].(string)
		if name == GatewayName {
			continue
		}
		seen[name] = true
		out = append(out, vmSummary(inst))
	}
	for _, r := range rows {
		if !seen[r.Name] {
			out = append(out, VMSummary{
				Name:         r.Name,
				IPAddress:    r.IPAddress,
				InstanceType: "virtual-machine",
				Status:       "Registered",
			})
		}
	}
	return out, nil
}

// SSHConfig generates OpenSSH config entries for the gateway and selected VMs.
func (m *Manager) SSHConfig(ctx context.Context, names []string) (string, error) {
	vms, err := m.ListVMs(ctx)
	if err != nil {
		return "", err
	}
	byName := make(map[string]VMSummary, len(vms))
	for _, v := range vms {
		byName[v.Name] = v
	}
	var selected []VMSummary
	if len(names) > 0 {
		for _, n := range names {
			v, ok := byName[n]
			if !ok {
				return "", jerr.NotFound(fmt.Sprintf("VM(s) not found: %s", n))
			}
			selected = append(selected, v)
		}
	} else {
		selected = vms
	}
	var lines []string
	lines = append(lines, fmt.Sprintf("Host %s", GatewayName), fmt.Sprintf("  HostName %s", GatewayUpIP), "  User ubuntu", "")
	for _, vm := range selected {
		ip := net.ParseIP(vm.IPAddress).To4()
		if ip == nil {
			continue
		}
		octet := int(ip[3])
		lines = append(lines,
			fmt.Sprintf("Host %s", vm.Name),
			fmt.Sprintf("  HostName %s", GatewayUpIP),
			fmt.Sprintf("  Port %d", SSHPortBase+octet),
			"  User ubuntu",
			fmt.Sprintf("  HostKeyAlias %s", vm.Name),
			"",
		)
	}
	return strings.Join(lines, "\n"), nil
}

func (m *Manager) syncSSHConfig(ctx context.Context) error {
	cfg, err := m.SSHConfig(ctx, nil)
	if err != nil {
		return err
	}
	_, err = sshconfig.WriteJettyConfig(cfg, m.SSHDir)
	return err
}

// PreparePortForward authorizes Jetty's forwarding key and returns (gateway SSH port, host ed25519 key).
func (m *Manager) PreparePortForward(ctx context.Context, name, publicKey string) (int, string, error) {
	row, err := db.GetVM(m.DB, name)
	if err != nil {
		return 0, "", err
	}
	if row == nil {
		return 0, "", jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
	}
	client, err := m.OpenClient()
	if err != nil {
		return 0, "", jerr.ServiceUnavailable(err.Error())
	}
	inst, err := m.instance(ctx, client, name)
	if err != nil {
		return 0, "", jerr.ServiceUnavailable(err.Error())
	}
	if inst == nil {
		return 0, "", jerr.NotFound(fmt.Sprintf("VM '%s' not found", name))
	}
	if st, _ := inst["status"].(string); st != "Running" {
		return 0, "", jerr.ServiceUnavailable(fmt.Sprintf("VM '%s' is not running", name))
	}
	script := `set -e; d=/home/ubuntu/.ssh; f="$d/authorized_keys"; ` +
		`install -d -m 700 -o ubuntu -g ubuntu "$d"; touch "$f"; ` +
		`grep -qxF "$JETTY_KEY" "$f" || printf "%s\n" "$JETTY_KEY" >> "$f"; ` +
		`chown ubuntu:ubuntu "$f"; chmod 600 "$f"; ` +
		`cat /etc/ssh/ssh_host_ed25519_key.pub`
	res, err := client.Execute(ctx, name, []string{"sh", "-c", script}, ExecOpts{
		Environment: map[string]string{"JETTY_KEY": publicKey},
	})
	if err != nil {
		return 0, "", jerr.ServiceUnavailable(err.Error())
	}
	if err := requireSuccess(res, []string{"authorize Jetty port forwarding key"}, name); err != nil {
		return 0, "", err
	}
	fields := strings.Fields(string(res.Stdout))
	if len(fields) < 2 {
		return 0, "", jerr.ServiceUnavailable("invalid SSH host key returned by VM")
	}
	hostKey := fields[0] + " " + fields[1]
	ip := net.ParseIP(row.IPAddress).To4()
	if ip == nil {
		return 0, "", jerr.ServiceUnavailable("invalid VM IP address")
	}
	return SSHPortBase + int(ip[3]), hostKey, nil
}

func (m *Manager) ensureProject(ctx context.Context, client Client) error {
	emptyProj := ""
	raw, err := client.Request(ctx, http.MethodGet, "/1.0/projects?recursion=1", nil, &emptyProj, true)
	if err != nil {
		return err
	}
	if list, ok := raw.([]any); ok {
		for _, item := range list {
			if obj, ok := item.(map[string]any); ok && obj["name"] == Project {
				return nil
			}
		}
	}
	_, err = client.Request(ctx, http.MethodPost, "/1.0/projects", map[string]any{
		"name":        Project,
		"description": "Jetty coding agent virtual machines",
		"config":      map[string]string{"features.images": "false", "features.profiles": "true"},
	}, &emptyProj, true)
	return err
}

func (m *Manager) ensureContainerProfile(ctx context.Context, client Client) error {
	_, err := client.Request(ctx, http.MethodGet, "/1.0/profiles/container", nil, nil, true)
	if err == nil {
		return nil
	}
	if !IsNotFound(err) {
		return err
	}
	_, err = client.Request(ctx, http.MethodPost, "/1.0/profiles", map[string]any{
		"name":        "container",
		"description": "Jetty unprivileged agent containers",
		"config":      map[string]any{},
		"devices":     map[string]any{"root": map[string]any{"type": "disk", "pool": "default", "path": "/"}},
	}, nil, true)
	return err
}

func (m *Manager) ensureProfile(ctx context.Context, client Client) error {
	raw, err := client.Request(ctx, http.MethodGet, "/1.0/profiles/default", nil, nil, true)
	if err != nil {
		return err
	}
	prof, _ := raw.(map[string]any)
	if prof == nil {
		return &Error{Message: "invalid profile response"}
	}
	devices, _ := prof["devices"].(map[string]any)
	if devices == nil {
		devices = map[string]any{}
	}
	cfg, _ := prof["config"].(map[string]any)
	if cfg == nil {
		cfg = map[string]any{}
	}
	if _, hasRoot := devices["root"]; hasRoot {
		if _, hasQemu := cfg["raw.qemu.conf"]; hasQemu {
			return nil
		}
	}
	if _, hasRoot := devices["root"]; !hasRoot {
		devices["root"] = map[string]any{"type": "disk", "pool": "default", "path": "/"}
	}
	if _, hasQemu := cfg["raw.qemu.conf"]; !hasQemu {
		cfg["raw.qemu.conf"] = FreePageReporting
	}
	prof["devices"] = devices
	prof["config"] = cfg
	_, err = client.Request(ctx, http.MethodPut, "/1.0/profiles/default", prof, nil, true)
	if err != nil {
		savedRaw, gErr := client.Request(ctx, http.MethodGet, "/1.0/profiles/default", nil, nil, true)
		if gErr == nil {
			if saved, ok := savedRaw.(map[string]any); ok {
				if savedCfg, ok := saved["config"].(map[string]any); ok && savedCfg["raw.qemu.conf"] == cfg["raw.qemu.conf"] {
					return nil
				}
			}
		}
		return err
	}
	return nil
}

func (m *Manager) ensureNetwork(ctx context.Context, client Client, name string, desired map[string]string) error {
	_, err := client.Request(ctx, http.MethodGet, "/1.0/networks/"+url.PathEscape(name), nil, nil, true)
	if err == nil {
		return nil
	}
	if !IsNotFound(err) {
		return err
	}
	_, err = client.Request(ctx, http.MethodPost, "/1.0/networks", map[string]any{
		"name":        name,
		"type":        "bridge",
		"description": fmt.Sprintf("Jetty %s network", name),
		"config":      desired,
	}, nil, true)
	return err
}

func (m *Manager) instances(ctx context.Context, client Client) ([]map[string]any, error) {
	raw, err := client.Request(ctx, http.MethodGet, "/1.0/instances?recursion=1", nil, nil, true)
	if err != nil {
		return nil, err
	}
	list, _ := raw.([]any)
	out := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out, nil
}

func (m *Manager) instance(ctx context.Context, client Client, name string) (map[string]any, error) {
	raw, err := client.Request(ctx, http.MethodGet, "/1.0/instances/"+url.PathEscape(name), nil, nil, true)
	if err != nil {
		if IsNotFound(err) {
			return nil, nil
		}
		return nil, err
	}
	inst, _ := raw.(map[string]any)
	return inst, nil
}

func vmSummary(inst map[string]any) VMSummary {
	devices, _ := inst["expanded_devices"].(map[string]any)
	if devices == nil {
		devices, _ = inst["devices"].(map[string]any)
	}
	cfg, _ := inst["expanded_config"].(map[string]any)
	if cfg == nil {
		cfg, _ = inst["config"].(map[string]any)
	}
	eth0, _ := devices["eth0"].(map[string]any)
	root, _ := devices["root"].(map[string]any)
	name, _ := inst["name"].(string)
	ip, _ := eth0["ipv4.address"].(string)
	instType, _ := inst["type"].(string)
	if instType == "" {
		instType = "virtual-machine"
	}
	status, _ := inst["status"].(string)
	if status == "" {
		status = "unknown"
	}
	cpus, _ := cfg["limits.cpu"].(string)
	mem, _ := cfg["limits.memory"].(string)
	disk, _ := root["size"].(string)
	return VMSummary{
		Name:         name,
		IPAddress:    ip,
		InstanceType: instType,
		Status:       status,
		CPUs:         cpus,
		Memory:       mem,
		Disk:         disk,
	}
}

func (m *Manager) nextIP(ctx context.Context, client Client) (string, error) {
	instances, err := m.instances(ctx, client)
	if err != nil {
		return "", err
	}
	used := make(map[string]struct{}, len(instances))
	for _, inst := range instances {
		if ip := vmSummary(inst).IPAddress; ip != "" {
			used[ip] = struct{}{}
		}
	}
	for octet := 11; octet <= 250; octet++ {
		addr := fmt.Sprintf("%s.%d", PrivPrefix, octet)
		if _, ok := used[addr]; ok {
			continue
		}
		inUse, err := db.IPInUseByOtherVM(m.DB, addr, "")
		if err != nil {
			return "", err
		}
		if !inUse {
			return addr, nil
		}
	}
	return "", jerr.Conflict(fmt.Sprintf("No free agent addresses remain in %s.0/24", PrivPrefix), nil)
}

func (m *Manager) startInstance(ctx context.Context, client Client, name string) error {
	inst, err := m.instance(ctx, client, name)
	if err != nil {
		return err
	}
	if inst != nil && inst["status"] == "Running" {
		return nil
	}
	_, err = client.Request(ctx, http.MethodPut, fmt.Sprintf("/1.0/instances/%s/state", url.PathEscape(name)), map[string]any{
		"action": "start", "timeout": 60,
	}, nil, true)
	return err
}

func (m *Manager) waitForAgent(ctx context.Context, client Client, name string) error {
	deadline := time.Now().Add(5 * time.Minute)
	var lastErr error
	for time.Now().Before(deadline) {
		tryCtx, cancel := context.WithTimeout(ctx, 15*time.Second)
		res, err := client.Execute(tryCtx, name, []string{"true"}, ExecOpts{})
		cancel()
		if err == nil && res.ExitCode == 0 {
			return nil
		}
		if err != nil {
			lastErr = err
		} else {
			lastErr = fmt.Errorf("guest agent readiness exited %d", res.ExitCode)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return jerr.ServiceUnavailable(fmt.Sprintf("%s: LXD guest agent did not become available within five minutes: %v", name, lastErr))
}
