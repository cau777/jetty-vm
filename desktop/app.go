package main

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os/exec"
	"strconv"
	"sync"
	"time"

	"connectrpc.com/connect"
	"github.com/wailsapp/wails/v2/pkg/runtime"
	"golang.org/x/net/http2"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/validation"
)

// App exposes Jetty daemon RPCs to the Wails TypeScript frontend over a single ConnectRPC client.
type App struct {
	ctx       context.Context
	cancel    context.CancelFunc
	client    jettyv1connect.JettyClient
	emitEvent func(name string, data any)
	watchMu   sync.Mutex
	addr      string
}

// NewApp constructs a Wails App targeting addr (defaults to config.ClientAddr()).
func NewApp(addr string) *App {
	if addr == "" {
		addr = config.ClientAddr()
	}
	tr := &http2.Transport{
		AllowHTTP: true,
		DialTLSContext: func(ctx context.Context, network, target string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, target)
		},
	}
	client := jettyv1connect.NewJettyClient(&http.Client{Transport: tr}, "http://"+addr, connect.WithGRPC())
	return &App{client: client, addr: addr}
}

func (a *App) startup(ctx context.Context) {
	watchCtx, cancel := context.WithCancel(ctx)
	a.ctx = watchCtx
	a.cancel = cancel
	if a.emitEvent == nil {
		a.emitEvent = func(name string, data any) {
			runtime.EventsEmit(watchCtx, name, data)
		}
	}
	go a.watchLoop(watchCtx)
}

func (a *App) shutdown(_ context.Context) {
	if a.cancel != nil {
		a.cancel()
	}
}

func (a *App) watchLoop(ctx context.Context) {
	for {
		if ctx.Err() != nil {
			return
		}
		stream, err := a.client.Watch(ctx, connect.NewRequest(&jettyv1.WatchRequest{}))
		if err != nil {
			if a.emitEvent != nil {
				a.emitEvent("jetty:connection", map[string]any{"connected": false, "error": err.Error()})
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(2 * time.Second):
				continue
			}
		}
		if a.emitEvent != nil {
			a.emitEvent("jetty:connection", map[string]any{"connected": true})
		}
		for stream.Receive() {
			ev := stream.Msg()
			if a.emitEvent != nil {
				a.emitEvent("jetty:event", map[string]any{
					"kind":        ev.GetKind(),
					"resource_id": ev.GetResourceId(),
					"occurred_at": ev.GetOccurredAt(),
				})
			}
		}
		stream.Close()
		select {
		case <-ctx.Done():
			return
		case <-time.After(1 * time.Second):
		}
	}
}

func protoToAny(msg proto.Message) (any, error) {
	b, err := protojson.MarshalOptions{
		EmitUnpopulated: true,
		UseProtoNames:   true,
	}.Marshal(msg)
	if err != nil {
		return nil, err
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		return nil, err
	}
	return out, nil
}

func (a *App) reqCtx() context.Context {
	if a.ctx != nil {
		return a.ctx
	}
	return context.Background()
}

func (a *App) GetStatus() (any, error) {
	resp, err := a.client.GetStatus(a.reqCtx(), connect.NewRequest(&jettyv1.GetStatusRequest{}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) ListVms() (any, error) {
	resp, err := a.client.ListVms(a.reqCtx(), connect.NewRequest(&jettyv1.ListVmsRequest{}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) SetupGateway(sshPublicKey string) (any, error) {
	stream, err := a.client.SetupGateway(a.reqCtx(), connect.NewRequest(&jettyv1.SetupGatewayRequest{SshPublicKey: sshPublicKey}))
	if err != nil {
		return nil, err
	}
	defer stream.Close()
	var last *jettyv1.JobEvent
	for stream.Receive() {
		last = stream.Msg()
		if payload, err := protoToAny(last); err == nil && a.emitEvent != nil {
			a.emitEvent("jetty:job", payload)
		}
	}
	if err := stream.Err(); err != nil {
		return nil, err
	}
	if last == nil {
		return map[string]any{}, nil
	}
	return protoToAny(last)
}

func (a *App) CreateVm(name string, cpus int, memory, disk, image, instanceType, sshPublicKey string) (any, error) {
	stream, err := a.client.CreateVm(a.reqCtx(), connect.NewRequest(&jettyv1.CreateVmRequest{
		Name:         name,
		Cpus:         int32(cpus),
		Memory:       memory,
		Disk:         disk,
		Image:        image,
		InstanceType: instanceType,
		SshPublicKey: sshPublicKey,
	}))
	if err != nil {
		return nil, err
	}
	defer stream.Close()
	var last *jettyv1.JobEvent
	for stream.Receive() {
		last = stream.Msg()
		if payload, err := protoToAny(last); err == nil && a.emitEvent != nil {
			a.emitEvent("jetty:job", payload)
		}
	}
	if err := stream.Err(); err != nil {
		return nil, err
	}
	if last == nil {
		return map[string]any{}, nil
	}
	return protoToAny(last)
}

func (a *App) DeleteVm(name string) error {
	_, err := a.client.DeleteVm(a.reqCtx(), connect.NewRequest(&jettyv1.DeleteVmRequest{Name: name}))
	return err
}

func (a *App) VmAction(name, action string) (any, error) {
	resp, err := a.client.VmAction(a.reqCtx(), connect.NewRequest(&jettyv1.VmActionRequest{Name: name, Action: action}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) HandoffVm(name, agent string) (any, error) {
	resp, err := a.client.HandoffVm(a.reqCtx(), connect.NewRequest(&jettyv1.HandoffVmRequest{Name: name, Agent: agent}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

// OpenFiles asks the desktop file manager to open the VM's SFTP location.
func (a *App) OpenFiles(name string) error {
	if _, err := validation.ValidateName(name, "name"); err != nil {
		return err
	}
	location := "sftp://" + name + "/home/ubuntu"
	ctx, cancel := context.WithTimeout(a.reqCtx(), 10*time.Second)
	defer cancel()
	err := exec.CommandContext(ctx, "gdbus", "call", "--session",
		"--dest", "org.freedesktop.FileManager1",
		"--object-path", "/org/freedesktop/FileManager1",
		"--method", "org.freedesktop.FileManager1.ShowFolders",
		fmt.Sprintf("['%s']", location), "").Run()
	if err == nil {
		return nil
	}
	if err := exec.CommandContext(a.reqCtx(), "xdg-open", location).Run(); err != nil {
		return fmt.Errorf("could not open %s in the file manager: %w", location, err)
	}
	return nil
}

func (a *App) OpenPortUrl(hostPort int) error {
	if hostPort < 1 || hostPort > 65535 {
		return fmt.Errorf("invalid host port")
	}
	runtime.BrowserOpenURL(a.reqCtx(), fmt.Sprintf("http://localhost:%d", hostPort))
	return nil
}

func (a *App) ListPorts(vmName string) (any, error) {
	resp, err := a.client.ListPorts(a.reqCtx(), connect.NewRequest(&jettyv1.ListPortsRequest{VmName: vmName}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) OpenPort(vmName string, vmPort, hostPort int, persistent bool) (any, error) {
	resp, err := a.client.OpenPort(a.reqCtx(), connect.NewRequest(&jettyv1.OpenPortRequest{
		VmName: vmName, VmPort: int32(vmPort), HostPort: int32(hostPort), Persistent: persistent,
	}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) UpdatePort(vmName string, hostPort int, persistent bool) error {
	_, err := a.client.UpdatePort(a.reqCtx(), connect.NewRequest(&jettyv1.UpdatePortRequest{
		VmName: vmName, HostPort: int32(hostPort), Persistent: persistent,
	}))
	return err
}

func (a *App) ClosePort(vmName string, hostPort int) error {
	_, err := a.client.ClosePort(a.reqCtx(), connect.NewRequest(&jettyv1.ClosePortRequest{VmName: vmName, HostPort: int32(hostPort)}))
	return err
}

func (a *App) ListReversePorts(vmName string) (any, error) {
	resp, err := a.client.ListReversePorts(a.reqCtx(), connect.NewRequest(&jettyv1.ListReversePortsRequest{VmName: vmName}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) OpenReversePort(vmName string, hostPort, vmPort int, persistent bool) (any, error) {
	resp, err := a.client.OpenReversePort(a.reqCtx(), connect.NewRequest(&jettyv1.OpenReversePortRequest{
		VmName: vmName, HostPort: int32(hostPort), VmPort: int32(vmPort), Persistent: persistent,
	}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) UpdateReversePort(vmName string, vmPort int, persistent bool) error {
	_, err := a.client.UpdateReversePort(a.reqCtx(), connect.NewRequest(&jettyv1.UpdateReversePortRequest{
		VmName: vmName, VmPort: int32(vmPort), Persistent: persistent,
	}))
	return err
}

func (a *App) CloseReversePort(vmName string, vmPort int) error {
	_, err := a.client.CloseReversePort(a.reqCtx(), connect.NewRequest(&jettyv1.CloseReversePortRequest{VmName: vmName, VmPort: int32(vmPort)}))
	return err
}

func (a *App) ListRules(vmName string, globalOnly bool) (any, error) {
	resp, err := a.client.ListRules(a.reqCtx(), connect.NewRequest(&jettyv1.ListRulesRequest{VmName: vmName, GlobalOnly: globalOnly}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

type RuleInput struct {
	Name       string `json:"name"`
	Priority   int32  `json:"priority"`
	Scope      string `json:"scope"`
	Hostname   string `json:"hostname"`
	ActionType string `json:"action_type"`
	Credential string `json:"credential"`
	PathPrefix string `json:"path_prefix"`
	InjType    string `json:"injection_type"`
	Username   string `json:"username"`
	HeaderName string `json:"header_name"`
	Service    string `json:"service"`
	Region     string `json:"region"`
}

func (a *App) PutRule(in RuleInput) (any, error) {
	sel := &jettyv1.VmSelector{Type: "all"}
	if in.Scope != "" {
		sel = &jettyv1.VmSelector{Type: "only", Vms: []string{in.Scope}}
	}
	act := &jettyv1.RuleAction{Type: in.ActionType}
	if in.ActionType == "allow_with_credential" {
		act.Credential = in.Credential
		act.PathPrefix = in.PathPrefix
		act.Injection = &jettyv1.Injection{
			Type:     in.InjType,
			Username: in.Username,
			Name:     in.HeaderName,
			Service:  in.Service,
			Region:   in.Region,
		}
	}
	resp, err := a.client.PutRule(a.reqCtx(), connect.NewRequest(&jettyv1.PutRuleRequest{
		Name:       in.Name,
		Priority:   in.Priority,
		VmSelector: sel,
		Hostname:   in.Hostname,
		Action:     act,
	}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) DeleteRule(name, scope string) error {
	_, err := a.client.DeleteRule(a.reqCtx(), connect.NewRequest(&jettyv1.DeleteRuleRequest{
		Name: name, Scope: scope, ScopeSpecified: true,
	}))
	return err
}

func (a *App) ListCredentials() (any, error) {
	resp, err := a.client.ListCredentials(a.reqCtx(), connect.NewRequest(&jettyv1.ListCredentialsRequest{}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) PutCredential(name, command string, ttlSeconds int) (any, error) {
	resp, err := a.client.PutCredential(a.reqCtx(), connect.NewRequest(&jettyv1.PutCredentialRequest{
		Name: name, Command: command, TtlSeconds: int32(ttlSeconds),
	}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) DeleteCredential(name string) error {
	_, err := a.client.DeleteCredential(a.reqCtx(), connect.NewRequest(&jettyv1.DeleteCredentialRequest{Name: name}))
	return err
}

func (a *App) RefreshCredential(name string) (any, error) {
	resp, err := a.client.RefreshCredential(a.reqCtx(), connect.NewRequest(&jettyv1.RefreshCredentialRequest{Name: name}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) GetQuickAddCatalog() (any, error) {
	resp, err := a.client.GetQuickAddCatalog(a.reqCtx(), connect.NewRequest(&jettyv1.Empty{}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) GetCa() (any, error) {
	resp, err := a.client.GetCa(a.reqCtx(), connect.NewRequest(&jettyv1.GetCaRequest{IncludePrivateKey: false}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) ListRequests(vmName, decision, host string, limit int) (any, error) {
	resp, err := a.client.ListRequests(a.reqCtx(), connect.NewRequest(&jettyv1.ListRequestsRequest{
		VmName: vmName, Decision: decision, Host: host, Limit: int32(limit),
	}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}

func (a *App) GetRequest(key string) (any, error) {
	id, err := strconv.ParseInt(key, 10, 64)
	if err != nil || id < 1 {
		return nil, fmt.Errorf("invalid connection ID")
	}
	resp, err := a.client.GetRequest(a.reqCtx(), connect.NewRequest(&jettyv1.GetRequestRequest{Id: id}))
	if err != nil {
		return nil, err
	}
	return protoToAny(resp.Msg)
}
