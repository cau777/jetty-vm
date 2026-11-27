// Package server implements Jetty's ConnectRPC/gRPC h2c server and dual listeners.
package server

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"strconv"
	"strings"
	"sync"
	"time"

	"connectrpc.com/connect"
	jettyvm "github.com/cau777/jetty-vm"
	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/ca"
	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/credentials"
	"github.com/cau777/jetty-vm/internal/db"
	jerr "github.com/cau777/jetty-vm/internal/errors"
	"github.com/cau777/jetty-vm/internal/events"
	"github.com/cau777/jetty-vm/internal/forwards"
	"github.com/cau777/jetty-vm/internal/handoff"
	"github.com/cau777/jetty-vm/internal/lxd"
	"github.com/cau777/jetty-vm/internal/requestlog"
	"github.com/cau777/jetty-vm/internal/validation"
)

const (
	ExpectedAgentVersion   = "1.0.0"
	DefaultMaxBodyBytes    = 32 * 1024 * 1024
	MaxUploadBytes         = 128 * 1024 * 1024
)

type connContext struct {
	vmName string
	sni    string
}

type reqCredentialMeta struct {
	credName string
	digest   string
}

// Server implements jettyv1connect.JettyHandler.
type Server struct {
	jettyv1connect.UnimplementedJettyHandler

	stateDB      *sql.DB
	requestsDB   *sql.DB
	creds        *credentials.Cache
	logs         *requestlog.Store
	events       *events.Broadcaster
	lxd          *lxd.Manager
	ports        *forwards.PortManager
	reversePorts *forwards.ReverseManager

	mu          sync.Mutex
	daemonState string
	jobActive   bool
	conns       map[int64]connContext
	reqCreds    map[int64]reqCredentialMeta

	// Hooks overridable in tests
	Now        func() time.Time
	RepushHook func(ctx context.Context) error
}

// Dependencies configures Server construction.
type Dependencies struct {
	StateDB      *sql.DB
	RequestsDB   *sql.DB
	Creds        *credentials.Cache
	LXD          *lxd.Manager
	Ports        *forwards.PortManager
	ReversePorts *forwards.ReverseManager
}

// NewServer constructs a Jetty RPC handler.
func NewServer(deps Dependencies) (*Server, error) {
	if _, err := ca.EnsureGenerated(deps.StateDB); err != nil {
		return nil, err
	}
	ev := events.NewBroadcaster()
	if deps.Creds == nil {
		deps.Creds = credentials.NewCache(0)
	}
	if deps.LXD == nil {
		deps.LXD = lxd.NewManager(deps.StateDB, config.ListenPort(), ev.PublishWatch)
	}
	if deps.Ports == nil {
		conn := forwards.NewSSHConnector(deps.LXD.PreparePortForward, "")
		deps.Ports = forwards.NewPortManager(deps.StateDB, conn, ev.PublishWatch)
	}
	if deps.ReversePorts == nil {
		conn := forwards.NewSSHConnector(deps.LXD.PreparePortForward, "")
		deps.ReversePorts = forwards.NewReverseManager(deps.StateDB, conn, ev.PublishWatch)
	}
	return &Server{
		stateDB:      deps.StateDB,
		requestsDB:   deps.RequestsDB,
		creds:        deps.Creds,
		logs:         requestlog.NewStore(deps.RequestsDB, 0),
		events:       ev,
		lxd:          deps.LXD,
		ports:        deps.Ports,
		reversePorts: deps.ReversePorts,
		daemonState:  "running",
		conns:        make(map[int64]connContext),
		reqCreds:     make(map[int64]reqCredentialMeta),
		Now:          time.Now,
	}, nil
}

func (s *Server) SetDaemonState(st string) {
	s.mu.Lock()
	s.daemonState = st
	s.mu.Unlock()
	s.events.PublishWatch("status", st)
}

func (s *Server) Events() *events.Broadcaster { return s.events }
func (s *Server) Ports() *forwards.PortManager { return s.ports }
func (s *Server) ReversePorts() *forwards.ReverseManager { return s.reversePorts }

// --- Status & Watch ---

func (s *Server) GetStatus(ctx context.Context, _ *connect.Request[jettyv1.GetStatusRequest]) (*connect.Response[jettyv1.Status], error) {
	s.mu.Lock()
	daemonState := s.daemonState
	s.mu.Unlock()

	gw := s.lxd.Status(ctx)
	networkReady := config.HostHasIPv4(lxd.UpAddr)
	overall := "not_configured"
	if gw.LXDAvailable {
		if !networkReady {
			overall = "waiting_for_network"
		} else if gw.Gateway == "running" && gw.GatewayService == "active" && gw.GatewayAgentService == "active" {
			overall = "running"
		} else if gw.Gateway == "running" {
			overall = gw.GatewayAgentService
		} else {
			overall = gw.Gateway
		}
	}
	var vms []*jettyv1.Vm
	for _, v := range gw.VMs {
		vms = append(vms, &jettyv1.Vm{
			Name:         v.Name,
			IpAddress:    v.IPAddress,
			InstanceType: v.InstanceType,
			Status:       v.Status,
			Cpus:         v.CPUs,
			Memory:       v.Memory,
			Disk:         v.Disk,
		})
	}
	return connect.NewResponse(&jettyv1.Status{
		State:               overall,
		Daemon:              daemonState,
		LxdAvailable:        gw.LXDAvailable,
		LxdSocket:           gw.Socket,
		NetworkReady:        networkReady,
		Gateway:             gw.Gateway,
		GatewayService:      gw.GatewayService,
		GatewayAgentService: gw.GatewayAgentService,
		GatewayAgentVersion: ExpectedAgentVersion,
		Vms:                 vms,
		Message:             gw.Message,
	}), nil
}

func (s *Server) Watch(ctx context.Context, _ *connect.Request[jettyv1.WatchRequest], stream *connect.ServerStream[jettyv1.Event]) error {
	ch, unsub := s.events.SubscribeWatch()
	defer unsub()
	if err := stream.Send(&jettyv1.Event{
		Kind:       "status",
		ResourceId: "connected",
		OccurredAt: s.Now().UTC().Format(time.RFC3339Nano),
	}); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return nil
		case ev, ok := <-ch:
			if !ok {
				return nil
			}
			if err := stream.Send(ev); err != nil {
				return err
			}
		}
	}
}

// --- VMs & Gateway ---

func (s *Server) ListVms(ctx context.Context, _ *connect.Request[jettyv1.ListVmsRequest]) (*connect.Response[jettyv1.ListVmsResponse], error) {
	items, err := s.lxd.ListVMs(ctx)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	rows, _ := db.ListVMs(s.stateDB)
	rowMap := make(map[string]db.VMRow, len(rows))
	for _, r := range rows {
		rowMap[r.Name] = r
	}
	out := make([]*jettyv1.Vm, 0, len(items))
	for _, v := range items {
		row := rowMap[v.Name]
		out = append(out, &jettyv1.Vm{
			Name:         v.Name,
			IpAddress:    v.IPAddress,
			InstanceType: v.InstanceType,
			Status:       v.Status,
			Cpus:         v.CPUs,
			Memory:       v.Memory,
			Disk:         v.Disk,
			CreatedAt:    row.CreatedAt,
			UpdatedAt:    row.UpdatedAt,
		})
	}
	return connect.NewResponse(&jettyv1.ListVmsResponse{Vms: out}), nil
}

func (s *Server) GetVm(_ context.Context, req *connect.Request[jettyv1.GetVmRequest]) (*connect.Response[jettyv1.Vm], error) {
	row, err := db.GetVM(s.stateDB, req.Msg.GetName())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if row == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("VM '%s' not found", req.Msg.GetName())))
	}
	return connect.NewResponse(&jettyv1.Vm{
		Name:      row.Name,
		IpAddress: row.IPAddress,
		CreatedAt: row.CreatedAt,
		UpdatedAt: row.UpdatedAt,
	}), nil
}

func (s *Server) PutVm(_ context.Context, req *connect.Request[jettyv1.PutVmRequest]) (*connect.Response[jettyv1.Vm], error) {
	name, err := validation.ValidateName(req.Msg.GetName(), "name")
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	ip, err := validation.ValidateIPv4(req.Msg.GetIpAddress(), "ip_address")
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	inUse, err := db.IPInUseByOtherVM(s.stateDB, ip, name)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if inUse {
		return nil, jerr.ToConnect(jerr.Conflict(fmt.Sprintf("ip_address '%s' is already in use by another VM", ip), nil))
	}
	row, _, err := db.PutVM(s.stateDB, name, ip)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.events.PublishWatch("vms", name)
	return connect.NewResponse(&jettyv1.Vm{
		Name:      row.Name,
		IpAddress: row.IPAddress,
		CreatedAt: row.CreatedAt,
		UpdatedAt: row.UpdatedAt,
	}), nil
}

func (s *Server) acquireJob() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.jobActive {
		return jerr.Conflict("Another Jetty provisioning job is already in progress", nil)
	}
	s.jobActive = true
	return nil
}

func (s *Server) releaseJob() {
	s.mu.Lock()
	s.jobActive = false
	s.mu.Unlock()
}

func buildSteps(defs [][2]string, activeKey, state string) []*jettyv1.JobStep {
	out := make([]*jettyv1.JobStep, len(defs))
	seenActive := false
	for i, d := range defs {
		st := "pending"
		if state == "done" {
			st = "done"
		} else if d[0] == activeKey {
			seenActive = true
			if state == "failed" {
				st = "failed"
			} else {
				st = "running"
			}
		} else if activeKey != "" && !seenActive {
			st = "done"
		}
		out[i] = &jettyv1.JobStep{Key: d[0], Label: d[1], Status: st}
	}
	return out
}

func (s *Server) CreateVm(ctx context.Context, req *connect.Request[jettyv1.CreateVmRequest], stream *connect.ServerStream[jettyv1.JobEvent]) error {
	name, err := validation.ValidateName(req.Msg.GetName(), "name")
	if err != nil {
		return jerr.ToConnect(err)
	}
	if err := s.acquireJob(); err != nil {
		return jerr.ToConnect(err)
	}
	defer s.releaseJob()

	defs := [][2]string{
		{"validate", "Validating instance settings"},
		{"instance", "Pulling the Ubuntu image and creating the instance"},
		{"register", "Registering the instance and SSH access"},
		{"start", "Starting the instance"},
		{"guest_agent", "Waiting for guest command execution"},
		{"cloud_init", "Running cloud-init"},
		{"ready", "Checking the instance is ready"},
	}
	jobID := fmt.Sprintf("vm-create-%d", s.Now().UnixNano())
	lastStep := ""
	vm, err := s.lxd.CreateVM(
		ctx, name, int(req.Msg.GetCpus()), req.Msg.GetMemory(), req.Msg.GetDisk(),
		req.Msg.GetImage(), req.Msg.GetInstanceType(), req.Msg.GetSshPublicKey(),
		func(step string) {
			lastStep = step
			_ = stream.Send(&jettyv1.JobEvent{
				JobId:       jobID,
				Kind:        "vm_create",
				Target:      name,
				State:       "running",
				CurrentStep: step,
				Steps:       buildSteps(defs, step, "running"),
			})
		},
	)
	if err != nil {
		_ = stream.Send(&jettyv1.JobEvent{
			JobId:        jobID,
			Kind:         "vm_create",
			Target:       name,
			State:        "failed",
			CurrentStep:  lastStep,
			Steps:        buildSteps(defs, lastStep, "failed"),
			ErrorMessage: err.Error(),
		})
		return jerr.ToConnect(err)
	}
	_ = stream.Send(&jettyv1.JobEvent{
		JobId:       jobID,
		Kind:        "vm_create",
		Target:      name,
		State:       "done",
		CurrentStep: "ready",
		Steps:       buildSteps(defs, "ready", "done"),
		Vm: &jettyv1.Vm{
			Name:         vm.Name,
			IpAddress:    vm.IPAddress,
			InstanceType: vm.InstanceType,
			Status:       vm.Status,
		},
	})
	return nil
}

func (s *Server) SetupGateway(ctx context.Context, req *connect.Request[jettyv1.SetupGatewayRequest], stream *connect.ServerStream[jettyv1.JobEvent]) error {
	if err := s.acquireJob(); err != nil {
		return jerr.ToConnect(err)
	}
	defer s.releaseJob()

	defs := [][2]string{
		{"validate", "Checking the SSH key"},
		{"project", "Preparing the LXD project"},
		{"profile", "Preparing the VM profile"},
		{"networks", "Preparing the Jetty networks"},
		{"instance", "Creating the gateway if needed"},
		{"start", "Starting the gateway"},
		{"guest_agent", "Waiting for the LXD guest agent"},
		{"cloud_init", "Running cloud-init"},
		{"configure", "Installing gateway networking, mitmproxy, and firewall rules"},
		{"ssh_config", "Updating host SSH configuration"},
	}
	jobID := fmt.Sprintf("gw-setup-%d", s.Now().UnixNano())
	lastStep := ""
	err := s.lxd.SetupGateway(ctx, req.Msg.GetSshPublicKey(), func(step string) {
		lastStep = step
		_ = stream.Send(&jettyv1.JobEvent{
			JobId:       jobID,
			Kind:        "gateway_setup",
			Target:      lxd.GatewayName,
			State:       "running",
			CurrentStep: step,
			Steps:       buildSteps(defs, step, "running"),
		})
	})
	if err != nil {
		_ = stream.Send(&jettyv1.JobEvent{
			JobId:        jobID,
			Kind:         "gateway_setup",
			Target:       lxd.GatewayName,
			State:        "failed",
			CurrentStep:  lastStep,
			Steps:        buildSteps(defs, lastStep, "failed"),
			ErrorMessage: err.Error(),
		})
		return jerr.ToConnect(err)
	}
	s.events.PublishWatch("status", "gateway_ready")
	_ = stream.Send(&jettyv1.JobEvent{
		JobId:       jobID,
		Kind:        "gateway_setup",
		Target:      lxd.GatewayName,
		State:       "done",
		CurrentStep: "ssh_config",
		Steps:       buildSteps(defs, "ssh_config", "done"),
		Gateway:     lxd.GatewayName,
	})
	return nil
}

func (s *Server) DeleteVm(ctx context.Context, req *connect.Request[jettyv1.DeleteVmRequest]) (*connect.Response[jettyv1.Empty], error) {
	name := req.Msg.GetName()
	if err := s.lxd.DeleteVM(ctx, name); err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.ports.DropVM(name)
	s.reversePorts.DropVM(name)
	s.events.PublishWatch("vms", name)
	return connect.NewResponse(&jettyv1.Empty{}), nil
}

func (s *Server) VmAction(ctx context.Context, req *connect.Request[jettyv1.VmActionRequest]) (*connect.Response[jettyv1.VmActionResponse], error) {
	status, err := s.lxd.VMAction(ctx, req.Msg.GetName(), req.Msg.GetAction())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.VmActionResponse{Name: req.Msg.GetName(), Status: status}), nil
}

func (s *Server) ExecVm(ctx context.Context, stream *connect.BidiStream[jettyv1.ExecVmRequest, jettyv1.ExecVmResponse]) error {
	first, err := stream.Receive()
	if err != nil {
		return err
	}
	start := first.GetStart()
	if start == nil || start.GetName() == "" || len(start.GetCommand()) == 0 {
		return jerr.ToConnect(jerr.Validation("first ExecVm message must contain start with name and command", nil))
	}
	res, err := s.lxd.ExecuteVM(ctx, start.GetName(), start.GetCommand())
	if err != nil {
		return jerr.ToConnect(err)
	}
	if len(res.Stdout) > 0 || len(res.Stderr) > 0 {
		if err := stream.Send(&jettyv1.ExecVmResponse{Stdout: res.Stdout, Stderr: res.Stderr}); err != nil {
			return err
		}
	}
	return stream.Send(&jettyv1.ExecVmResponse{Exited: true, ExitCode: int32(res.ExitCode)})
}

func (s *Server) UploadToVm(ctx context.Context, stream *connect.ClientStream[jettyv1.UploadToVmRequest]) (*connect.Response[jettyv1.UploadToVmResponse], error) {
	var meta *jettyv1.UploadMetadata
	var content []byte
	for stream.Receive() {
		msg := stream.Msg()
		switch p := msg.Payload.(type) {
		case *jettyv1.UploadToVmRequest_Metadata:
			meta = p.Metadata
		case *jettyv1.UploadToVmRequest_Chunk:
			if len(content)+len(p.Chunk) > MaxUploadBytes {
				return nil, jerr.ToConnect(jerr.Validation("upload exceeds 128 MiB limit", nil))
			}
			content = append(content, p.Chunk...)
		}
	}
	if err := stream.Err(); err != nil && !errors.Is(err, io.EOF) {
		return nil, err
	}
	if meta == nil || meta.GetName() == "" || meta.GetPath() == "" {
		return nil, jerr.ToConnect(jerr.Validation("upload requires metadata with name and path", nil))
	}
	mode := int(meta.GetMode())
	if mode == 0 {
		mode = 0o644
	}
	owner := meta.GetOwner()
	if owner == "" {
		owner = "ubuntu"
	}
	if err := s.lxd.UploadVMFile(ctx, meta.GetName(), meta.GetPath(), content, mode, owner); err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.UploadToVmResponse{
		Path:         meta.GetPath(),
		BytesWritten: int64(len(content)),
	}), nil
}

func (s *Server) HandoffVm(_ context.Context, req *connect.Request[jettyv1.HandoffVmRequest]) (*connect.Response[jettyv1.HandoffVmResponse], error) {
	vm, err := db.GetVM(s.stateDB, req.Msg.GetName())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if vm == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("VM '%s' is not registered", req.Msg.GetName())))
	}
	res, err := handoff.Launch(req.Msg.GetAgent(), vm.Name, vm.IPAddress)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.HandoffVmResponse{
		Agent:        res.Agent,
		Label:        res.Label,
		SkillInstall: res.SkillInstall,
		Terminal:     res.Terminal,
		ProjectDir:   res.ProjectDir,
		Jetty:        res.Jetty,
	}), nil
}

func (s *Server) GetSshConfig(ctx context.Context, req *connect.Request[jettyv1.GetSshConfigRequest]) (*connect.Response[jettyv1.GetSshConfigResponse], error) {
	cfg, err := s.lxd.SSHConfig(ctx, req.Msg.GetNames())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.GetSshConfigResponse{Config: cfg}), nil
}

// --- Port & Reverse Forwards ---

func toProtoPort(f forwards.Snapshot) *jettyv1.PortForward {
	return &jettyv1.PortForward{
		VmName:     f.VMName,
		VmPort:     int32(f.VMPort),
		HostPort:   int32(f.HostPort),
		Persistent: f.Persistent,
		State:      f.State,
		Error:      f.Error,
		Url:        f.URL,
	}
}

func toProtoReverse(f forwards.Snapshot) *jettyv1.ReverseForward {
	return &jettyv1.ReverseForward{
		VmName:     f.VMName,
		VmPort:     int32(f.VMPort),
		HostPort:   int32(f.HostPort),
		Persistent: f.Persistent,
		State:      f.State,
		Error:      f.Error,
	}
}

func (s *Server) ListPorts(_ context.Context, req *connect.Request[jettyv1.ListPortsRequest]) (*connect.Response[jettyv1.ListPortsResponse], error) {
	items := s.ports.List(req.Msg.GetVmName())
	out := make([]*jettyv1.PortForward, 0, len(items))
	for _, f := range items {
		out = append(out, toProtoPort(f))
	}
	return connect.NewResponse(&jettyv1.ListPortsResponse{Ports: out}), nil
}

func (s *Server) OpenPort(_ context.Context, req *connect.Request[jettyv1.OpenPortRequest]) (*connect.Response[jettyv1.PortForward], error) {
	f, err := s.ports.Open(req.Msg.GetVmName(), int(req.Msg.GetVmPort()), int(req.Msg.GetHostPort()), req.Msg.GetPersistent())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.events.PublishWatch("ports", strconv.Itoa(f.HostPort))
	return connect.NewResponse(toProtoPort(f)), nil
}

func (s *Server) UpdatePort(_ context.Context, req *connect.Request[jettyv1.UpdatePortRequest]) (*connect.Response[jettyv1.PortForward], error) {
	f, err := s.ports.SetPersistent(req.Msg.GetVmName(), int(req.Msg.GetHostPort()), req.Msg.GetPersistent())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(toProtoPort(f)), nil
}

func (s *Server) ClosePort(_ context.Context, req *connect.Request[jettyv1.ClosePortRequest]) (*connect.Response[jettyv1.Empty], error) {
	if err := s.ports.Close(req.Msg.GetVmName(), int(req.Msg.GetHostPort())); err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.Empty{}), nil
}

func (s *Server) ListReversePorts(_ context.Context, req *connect.Request[jettyv1.ListReversePortsRequest]) (*connect.Response[jettyv1.ListReversePortsResponse], error) {
	items := s.reversePorts.List(req.Msg.GetVmName())
	out := make([]*jettyv1.ReverseForward, 0, len(items))
	for _, f := range items {
		out = append(out, toProtoReverse(f))
	}
	return connect.NewResponse(&jettyv1.ListReversePortsResponse{Ports: out}), nil
}

func (s *Server) OpenReversePort(_ context.Context, req *connect.Request[jettyv1.OpenReversePortRequest]) (*connect.Response[jettyv1.ReverseForward], error) {
	f, err := s.reversePorts.Open(req.Msg.GetVmName(), int(req.Msg.GetHostPort()), int(req.Msg.GetVmPort()), req.Msg.GetPersistent())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.events.PublishWatch("reverse_ports", fmt.Sprintf("%s:%d", f.VMName, f.VMPort))
	return connect.NewResponse(toProtoReverse(f)), nil
}

func (s *Server) UpdateReversePort(_ context.Context, req *connect.Request[jettyv1.UpdateReversePortRequest]) (*connect.Response[jettyv1.ReverseForward], error) {
	f, err := s.reversePorts.SetPersistent(req.Msg.GetVmName(), int(req.Msg.GetVmPort()), req.Msg.GetPersistent())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(toProtoReverse(f)), nil
}

func (s *Server) CloseReversePort(_ context.Context, req *connect.Request[jettyv1.CloseReversePortRequest]) (*connect.Response[jettyv1.Empty], error) {
	if err := s.reversePorts.Close(req.Msg.GetVmName(), int(req.Msg.GetVmPort())); err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.Empty{}), nil
}

// --- Policy: Rules, Credentials, Catalog, CA ---

func toProtoRule(r db.Rule) *jettyv1.Rule {
	act := &jettyv1.RuleAction{
		Type:       r.Action.Type,
		Credential: r.Action.Credential,
		PathPrefix: r.Action.PathPrefix,
	}
	if r.Action.Injection != nil {
		act.Injection = &jettyv1.Injection{
			Type:     r.Action.Injection.Type,
			Username: r.Action.Injection.Username,
			Name:     r.Action.Injection.Name,
			Service:  r.Action.Injection.Service,
			Region:   r.Action.Injection.Region,
		}
	}
	return &jettyv1.Rule{
		Name:     r.Name,
		Scope:    r.Scope,
		Priority: int32(r.Priority),
		VmSelector: &jettyv1.VmSelector{
			Type: r.VMSelector.Type,
			Vms:  r.VMSelector.VMs,
		},
		Hostname:  r.Hostname,
		Action:    act,
		CreatedAt: r.CreatedAt,
		UpdatedAt: r.UpdatedAt,
	}
}

func (s *Server) ListRules(_ context.Context, req *connect.Request[jettyv1.ListRulesRequest]) (*connect.Response[jettyv1.ListRulesResponse], error) {
	var scope *string
	if req.Msg.GetVmName() != "" {
		vm := req.Msg.GetVmName()
		row, err := db.GetVM(s.stateDB, vm)
		if err != nil {
			return nil, jerr.ToConnect(err)
		}
		if row == nil {
			return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("VM '%s' not found", vm)))
		}
		scope = &vm
	} else if req.Msg.GetGlobalOnly() {
		g := ""
		scope = &g
	}
	rows, err := db.ListRules(s.stateDB, scope)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	out := make([]*jettyv1.Rule, 0, len(rows))
	for _, r := range rows {
		out = append(out, toProtoRule(r))
	}
	return connect.NewResponse(&jettyv1.ListRulesResponse{Rules: out}), nil
}

func (s *Server) findRule(name, scope string, scopeSpecified bool) (*db.Rule, error) {
	if scopeSpecified {
		if scope != "" {
			vm, err := db.GetVM(s.stateDB, scope)
			if err != nil {
				return nil, err
			}
			if vm == nil {
				return nil, jerr.NotFound(fmt.Sprintf("VM '%s' not found", scope))
			}
		}
		r, err := db.GetRule(s.stateDB, name, scope)
		if err != nil {
			return nil, err
		}
		if r == nil {
			return nil, jerr.NotFound(fmt.Sprintf("Rule '%s' not found", name))
		}
		return r, nil
	}
	rows, err := db.ListRulesNamed(s.stateDB, name)
	if err != nil {
		return nil, err
	}
	if len(rows) > 1 {
		return nil, jerr.Conflict(fmt.Sprintf("Rule '%s' exists in multiple scopes; specify its VM or global scope", name), nil)
	}
	if len(rows) == 0 {
		return nil, jerr.NotFound(fmt.Sprintf("Rule '%s' not found", name))
	}
	return &rows[0], nil
}

func (s *Server) GetRule(_ context.Context, req *connect.Request[jettyv1.GetRuleRequest]) (*connect.Response[jettyv1.Rule], error) {
	r, err := s.findRule(req.Msg.GetName(), req.Msg.GetScope(), req.Msg.GetScopeSpecified())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(toProtoRule(*r)), nil
}

func (s *Server) PutRule(_ context.Context, req *connect.Request[jettyv1.PutRuleRequest]) (*connect.Response[jettyv1.PutRuleResponse], error) {
	name, err := validation.ValidateRuleName(req.Msg.GetName())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if req.Msg.GetPriority() < 0 {
		return nil, jerr.ToConnect(jerr.Validation("'priority' must be a non-negative integer", map[string]string{"priority": "must be >= 0"}))
	}
	hostname, err := validation.ValidateHostname(req.Msg.GetHostname(), "hostname")
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	vmNames, err := db.VMNames(s.stateDB)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	selMsg := req.Msg.GetVmSelector()
	if selMsg == nil {
		return nil, jerr.ToConnect(jerr.Validation("'vm_selector' must be provided", map[string]string{"vm_selector": "required"}))
	}
	var sel db.VMSelector
	switch selMsg.GetType() {
	case "all":
		if len(selMsg.GetVms()) > 0 {
			return nil, jerr.ToConnect(jerr.Validation("'vm_selector' of type 'all' must not include vms", nil))
		}
		sel = db.VMSelector{Type: "all"}
	case "only":
		vms := selMsg.GetVms()
		if len(vms) != 1 {
			return nil, jerr.ToConnect(jerr.Validation("'vm_selector.vms' must contain exactly one VM", nil))
		}
		if _, ok := vmNames[vms[0]]; !ok {
			return nil, jerr.ToConnect(jerr.Validation(fmt.Sprintf("'vm_selector.vms' references unknown VM(s): %s", vms[0]), nil))
		}
		sel = db.VMSelector{Type: "only", VMs: []string{vms[0]}}
	default:
		return nil, jerr.ToConnect(jerr.Validation("'vm_selector.type' must be 'all' or 'only'", nil))
	}

	actMsg := req.Msg.GetAction()
	if actMsg == nil {
		return nil, jerr.ToConnect(jerr.Validation("'action' must be provided", nil))
	}
	credNames, err := db.CredentialNames(s.stateDB)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	var act db.RuleAction
	switch actMsg.GetType() {
	case "allow":
		act = db.RuleAction{Type: "allow"}
	case "block":
		act = db.RuleAction{Type: "block"}
	case "allow_with_credential":
		credName, err := validation.ValidateName(actMsg.GetCredential(), "action.credential")
		if err != nil {
			return nil, jerr.ToConnect(err)
		}
		if _, ok := credNames[credName]; !ok {
			return nil, jerr.ToConnect(jerr.Validation(fmt.Sprintf("'action.credential' references unknown Credential '%s'", credName), nil))
		}
		prefix, err := validation.ValidatePathPrefix(actMsg.GetPathPrefix(), "action.path_prefix")
		if err != nil {
			return nil, jerr.ToConnect(err)
		}
		injMsg := actMsg.GetInjection()
		if injMsg == nil {
			return nil, jerr.ToConnect(jerr.Validation("'action.injection' must be provided", nil))
		}
		var inj db.Injection
		switch injMsg.GetType() {
		case "bearer":
			inj = db.Injection{Type: "bearer"}
		case "basic":
			if injMsg.GetUsername() == "" {
				return nil, jerr.ToConnect(jerr.Validation("'action.injection.username' must be non-empty", nil))
			}
			inj = db.Injection{Type: "basic", Username: injMsg.GetUsername()}
		case "header":
			if err := validation.ValidateHeaderName(injMsg.GetName()); err != nil {
				return nil, jerr.ToConnect(err)
			}
			inj = db.Injection{Type: "header", Name: injMsg.GetName()}
		case "aws_sigv4":
			if err := validation.ValidateAWSIdentifier(injMsg.GetService(), "service"); err != nil {
				return nil, jerr.ToConnect(err)
			}
			if err := validation.ValidateAWSIdentifier(injMsg.GetRegion(), "region"); err != nil {
				return nil, jerr.ToConnect(err)
			}
			inj = db.Injection{Type: "aws_sigv4", Service: injMsg.GetService(), Region: injMsg.GetRegion()}
		default:
			return nil, jerr.ToConnect(jerr.Validation("'action.injection.type' must be 'bearer', 'basic', 'header', or 'aws_sigv4'", nil))
		}
		act = db.RuleAction{Type: "allow_with_credential", Credential: credName, PathPrefix: prefix, Injection: &inj}
	default:
		return nil, jerr.ToConnect(jerr.Validation("'action.type' must be 'allow', 'block', or 'allow_with_credential'", nil))
	}

	if hostname == "@aws" && act.Type == "allow_with_credential" && act.Injection.Type != "aws_sigv4" {
		return nil, jerr.ToConnect(jerr.Validation("The AWS endpoint selector requires AWS SigV4 injection", nil))
	}

	scope := ""
	if sel.Type == "only" {
		scope = sel.VMs[0]
	}
	inUse, err := db.RulePriorityInUseByOther(s.stateDB, int(req.Msg.GetPriority()), name, scope)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if inUse {
		return nil, jerr.ToConnect(jerr.Conflict(fmt.Sprintf("priority %d is already in use in this rule scope", req.Msg.GetPriority()), nil))
	}
	row, created, err := db.PutRule(s.stateDB, name, int(req.Msg.GetPriority()), sel, hostname, act)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.events.PublishWatch("rules", scope+":"+name)
	return connect.NewResponse(&jettyv1.PutRuleResponse{Rule: toProtoRule(*row), Created: created}), nil
}

func (s *Server) DeleteRule(_ context.Context, req *connect.Request[jettyv1.DeleteRuleRequest]) (*connect.Response[jettyv1.Empty], error) {
	r, err := s.findRule(req.Msg.GetName(), req.Msg.GetScope(), req.Msg.GetScopeSpecified())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if err := db.DeleteRule(s.stateDB, r.Name, r.Scope); err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.events.PublishWatch("rules", r.Scope+":"+r.Name)
	return connect.NewResponse(&jettyv1.Empty{}), nil
}

func (s *Server) toProtoCredential(row db.CredentialRow) *jettyv1.Credential {
	st := s.creds.GetStatus(row.Name)
	out := &jettyv1.Credential{
		Name:            row.Name,
		Command:         row.Command,
		TtlSeconds:      int32(row.TTLSeconds),
		CreatedAt:       row.CreatedAt,
		UpdatedAt:       row.UpdatedAt,
		Status:          st.Status,
		FailureCategory: st.FailureCategory,
	}
	if st.ExpiresAt != nil {
		out.ExpiresAt = *st.ExpiresAt
		out.HasExpiresAt = true
	}
	if st.LastSuccessAt != nil {
		out.LastSuccessAt = *st.LastSuccessAt
		out.HasLastSuccessAt = true
	}
	if st.LastFailureAt != nil {
		out.LastFailureAt = *st.LastFailureAt
		out.HasLastFailureAt = true
	}
	return out
}

func (s *Server) ListCredentials(_ context.Context, _ *connect.Request[jettyv1.ListCredentialsRequest]) (*connect.Response[jettyv1.ListCredentialsResponse], error) {
	rows, err := db.ListCredentials(s.stateDB)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	out := make([]*jettyv1.Credential, 0, len(rows))
	for _, r := range rows {
		out = append(out, s.toProtoCredential(r))
	}
	return connect.NewResponse(&jettyv1.ListCredentialsResponse{Credentials: out}), nil
}

func (s *Server) GetCredential(_ context.Context, req *connect.Request[jettyv1.GetCredentialRequest]) (*connect.Response[jettyv1.Credential], error) {
	row, err := db.GetCredential(s.stateDB, req.Msg.GetName())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if row == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("Credential '%s' not found", req.Msg.GetName())))
	}
	return connect.NewResponse(s.toProtoCredential(*row)), nil
}

func (s *Server) PutCredential(_ context.Context, req *connect.Request[jettyv1.PutCredentialRequest]) (*connect.Response[jettyv1.PutCredentialResponse], error) {
	name, err := validation.ValidateName(req.Msg.GetName(), "name")
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if strings.TrimSpace(req.Msg.GetCommand()) == "" {
		return nil, jerr.ToConnect(jerr.Validation("'command' must be a non-empty string", nil))
	}
	if req.Msg.GetTtlSeconds() < 0 {
		return nil, jerr.ToConnect(jerr.Validation("'ttl_seconds' must be a non-negative integer", nil))
	}
	row, created, err := db.PutCredential(s.stateDB, name, req.Msg.GetCommand(), int(req.Msg.GetTtlSeconds()))
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.creds.Invalidate(name)
	s.events.PublishWatch("credentials", name)
	return connect.NewResponse(&jettyv1.PutCredentialResponse{
		Credential: s.toProtoCredential(*row),
		Created:    created,
	}), nil
}

func (s *Server) RefreshCredential(_ context.Context, req *connect.Request[jettyv1.RefreshCredentialRequest]) (*connect.Response[jettyv1.Credential], error) {
	row, err := db.GetCredential(s.stateDB, req.Msg.GetName())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if row == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("Credential '%s' not found", req.Msg.GetName())))
	}
	s.creds.Invalidate(row.Name)
	s.events.PublishWatch("credentials", row.Name)
	return connect.NewResponse(s.toProtoCredential(*row)), nil
}

func (s *Server) DeleteCredential(_ context.Context, req *connect.Request[jettyv1.DeleteCredentialRequest]) (*connect.Response[jettyv1.Empty], error) {
	name := req.Msg.GetName()
	row, err := db.GetCredential(s.stateDB, name)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if row == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("Credential '%s' not found", name)))
	}
	ref, err := db.CredentialReferencedByRule(s.stateDB, name)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if ref {
		return nil, jerr.ToConnect(jerr.Conflict(fmt.Sprintf("Credential '%s' is referenced by a Rule; update or delete it first", name), nil))
	}
	if err := db.DeleteCredential(s.stateDB, name); err != nil {
		return nil, jerr.ToConnect(err)
	}
	s.creds.Drop(name)
	s.events.PublishWatch("credentials", name)
	return connect.NewResponse(&jettyv1.Empty{}), nil
}

func (s *Server) GetQuickAddCatalog(_ context.Context, _ *connect.Request[jettyv1.Empty]) (*connect.Response[jettyv1.QuickAddCatalogResponse], error) {
	raw, err := fs.ReadFile(jettyvm.Assets, "resources/quick-add-catalog.json")
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	var items []struct {
		Key         string `json:"key"`
		DisplayName string `json:"display_name"`
		Command     string `json:"command"`
		TTLSeconds  int32  `json:"ttl_seconds"`
		Notes       string `json:"notes"`
	}
	if err := json.Unmarshal(raw, &items); err != nil {
		return nil, jerr.ToConnect(err)
	}
	entries := make([]*jettyv1.QuickAddEntry, 0, len(items))
	for _, it := range items {
		entries = append(entries, &jettyv1.QuickAddEntry{
			Key:         it.Key,
			DisplayName: it.DisplayName,
			Command:     it.Command,
			TtlSeconds:  it.TTLSeconds,
			Notes:       it.Notes,
		})
	}
	return connect.NewResponse(&jettyv1.QuickAddCatalogResponse{Entries: entries}), nil
}

func (s *Server) GetCa(_ context.Context, req *connect.Request[jettyv1.GetCaRequest]) (*connect.Response[jettyv1.Ca], error) {
	row, err := ca.EnsureGenerated(s.stateDB)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	out := &jettyv1.Ca{
		CertificatePem:    row.CertificatePEM,
		FingerprintSha256: row.FingerprintSHA256,
		Subject:           ca.SubjectName,
		NotBefore:         row.NotBefore,
		NotAfter:          row.NotAfter,
	}
	if req.Msg.GetIncludePrivateKey() {
		out.PrivateKeyPem = row.PrivateKeyPEM
	}
	return connect.NewResponse(out), nil
}

// --- Logs ---

func (s *Server) ListRequests(_ context.Context, req *connect.Request[jettyv1.ListRequestsRequest]) (*connect.Response[jettyv1.ListRequestsResponse], error) {
	conns, err := s.logs.ListConnections(req.Msg)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	return connect.NewResponse(&jettyv1.ListRequestsResponse{Connections: conns}), nil
}

func (s *Server) GetRequest(_ context.Context, req *connect.Request[jettyv1.GetRequestRequest]) (*connect.Response[jettyv1.RequestDetail], error) {
	detail, err := s.logs.GetConnection(req.Msg.GetId())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if detail == nil {
		return nil, jerr.ToConnect(jerr.NotFound(fmt.Sprintf("connection '%d' not found", req.Msg.GetId())))
	}
	return connect.NewResponse(detail), nil
}

func (s *Server) TailRequests(ctx context.Context, req *connect.Request[jettyv1.TailRequestsRequest], stream *connect.ServerStream[jettyv1.RequestEvent]) error {
	ch, unsub := s.events.SubscribeTail()
	defer unsub()
	filterVM := req.Msg.GetVmName()
	filterDecision := req.Msg.GetDecision()
	for {
		select {
		case <-ctx.Done():
			return nil
		case ev, ok := <-ch:
			if !ok {
				return nil
			}
			if filterVM != "" && ev.GetConnection() != nil && ev.GetConnection().GetVmName() != filterVM {
				continue
			}
			if filterDecision != "" {
				if ev.GetHttpRequest() != nil && ev.GetHttpRequest().GetOutcome() != filterDecision {
					continue
				}
				if ev.GetConnection() != nil && ev.GetConnection().GetOutcome() != filterDecision {
					continue
				}
			}
			if err := stream.Send(ev); err != nil {
				return err
			}
		}
	}
}
