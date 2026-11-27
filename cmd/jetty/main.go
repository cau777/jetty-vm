// Command jetty is the unified Jetty CLI and central daemon (`jetty daemon`).
package main

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"connectrpc.com/connect"
	"golang.org/x/net/http2"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	jettyvm "github.com/cau777/jetty-vm"
	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/db"
	"github.com/cau777/jetty-vm/internal/server"
	"github.com/cau777/jetty-vm/internal/validation"
)

func installedVersion() string {
	if v := os.Getenv("JETTY_RELEASE_VERSION"); v != "" {
		return v
	}
	raw, err := jettyvm.Assets.ReadFile("VERSION")
	if err != nil {
		return "development"
	}
	return strings.TrimSpace(string(raw))
}

func newClient() jettyv1connect.JettyClient {
	tr := &http2.Transport{
		AllowHTTP: true,
		DialTLSContext: func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, network, addr)
		},
	}
	httpClient := &http.Client{Transport: tr}
	baseURL := "http://" + config.ClientAddr()
	return jettyv1connect.NewJettyClient(httpClient, baseURL, connect.WithGRPC())
}

func printProtoJSON(msg proto.Message) error {
	b, err := protojson.MarshalOptions{
		EmitUnpopulated: true,
		Indent:          "  ",
		UseProtoNames:   true,
	}.Marshal(msg)
	if err != nil {
		return err
	}
	fmt.Println(string(b))
	return nil
}

// extractJSONFlag strips --json anywhere in args and returns (cleanedArgs, jsonMode).
func extractJSONFlag(args []string) ([]string, bool) {
	jsonMode := false
	out := make([]string, 0, len(args))
	passthrough := false
	for _, a := range args {
		if a == "--" {
			passthrough = true
		}
		if !passthrough && a == "--json" {
			jsonMode = true
			continue
		}
		out = append(out, a)
	}
	return out, jsonMode
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(rawArgs []string) int {
	args, jsonMode := extractJSONFlag(rawArgs)
	if len(args) == 0 {
		printUsage()
		return 2
	}
	switch args[0] {
	case "-h", "--help", "help":
		printUsage()
		return 0
	case "--version", "version":
		fmt.Printf("Jetty %s\n", installedVersion())
		return 0
	case "daemon":
		if err := runDaemonCmd(); err != nil {
			fmt.Fprintf(os.Stderr, "jetty daemon: %v\n", err)
			return 1
		}
		return 0
	case "setup":
		if err := runSetupCmd(); err != nil {
			fmt.Fprintf(os.Stderr, "Jetty setup failed: %v\n", err)
			return 1
		}
		return 0
	case "update":
		if err := runUpdateCmd(); err != nil {
			fmt.Fprintf(os.Stderr, "Jetty update failed: %v\n", err)
			return 1
		}
		return 0
	}

	ctx := context.Background()
	client := newClient()

	var err error
	var code int
	switch args[0] {
	case "status":
		err = cmdStatus(ctx, client, jsonMode)
	case "gateway":
		err = cmdGateway(ctx, client, args[1:], jsonMode)
	case "vm":
		code, err = cmdVM(ctx, client, args[1:], jsonMode)
	case "ssh-config":
		err = cmdSSHConfig(ctx, client, args[1:], jsonMode)
	case "rule":
		err = cmdRule(ctx, client, args[1:], jsonMode)
	case "credential":
		err = cmdCredential(ctx, client, args[1:], jsonMode)
	case "ca":
		err = cmdCA(ctx, client, args[1:], jsonMode)
	case "log":
		err = cmdLog(ctx, client, args[1:], jsonMode)
	default:
		printUsage()
		return 2
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "jetty: %v\n", err)
		if code != 0 {
			return code
		}
		return 1
	}
	return code
}

func printUsage() {
	fmt.Fprintln(os.Stderr, `Usage: jetty [--json] <command> ...

Commands:
  daemon                       Run the central jettyd service
  setup                        Install jetty binary and systemd user service
  update                       Download and install the latest verified release
  status                       Show daemon, gateway, and VM status
  gateway setup [--ssh-key]    Create or repair Jetty networks and gateway VM
  vm <create|delete|start|stop|restart|list|ip|put|exec|upload|port|reverse-port>
  rule <list|get|put|delete>   Manage traffic & credential injection rules
  credential <list|get|put|delete|refresh|catalog>
  ca show                      Show the Interception CA certificate
  log <list|show>              Inspect connection and HTTP request logs
  ssh-config [NAME...]         Print OpenSSH entries for gateway and VMs`)
}

func runDaemonCmd() error {
	statePath, err := config.StateDBPath()
	if err != nil {
		return err
	}
	reqPath, err := config.RequestsDBPath()
	if err != nil {
		return err
	}
	stateConn, err := db.Open(statePath)
	if err != nil {
		return err
	}
	defer stateConn.Close()
	if err := db.MigrateState(stateConn); err != nil {
		return err
	}

	reqConn, err := db.Open(reqPath)
	if err != nil {
		return err
	}
	defer reqConn.Close()
	if err := db.MigrateRequests(reqConn); err != nil {
		return err
	}

	srv, err := server.NewServer(server.Dependencies{
		StateDB:    stateConn,
		RequestsDB: reqConn,
	})
	if err != nil {
		return err
	}
	return server.RunDaemonWithSignals(srv, config.ListenPort())
}

func cmdStatus(ctx context.Context, client jettyv1connect.JettyClient, jsonMode bool) error {
	resp, err := client.GetStatus(ctx, connect.NewRequest(&jettyv1.GetStatusRequest{}))
	if err != nil {
		return err
	}
	if jsonMode {
		return printProtoJSON(resp.Msg)
	}
	st := resp.Msg
	fmt.Printf("daemon: %s\n", st.GetDaemon())
	fmt.Printf("state: %s\n", st.GetState())
	lxdText := "unavailable"
	if st.GetLxdAvailable() {
		lxdText = "available"
	}
	fmt.Printf("lxd: %s\n", lxdText)
	fmt.Printf("gateway: %s\n", st.GetGateway())
	fmt.Printf("gateway service: %s\n", st.GetGatewayService())
	fmt.Printf("gateway agent: %s (v%s)\n", st.GetGatewayAgentService(), st.GetGatewayAgentVersion())
	for _, vm := range st.GetVms() {
		ip := vm.GetIpAddress()
		if ip == "" {
			ip = "—"
		}
		fmt.Printf("%s\t%s\t%s\n", vm.GetName(), ip, vm.GetStatus())
	}
	if st.GetMessage() != "" {
		fmt.Println(st.GetMessage())
	}
	return nil
}

func followJobStream(stream *connect.ServerStreamForClient[jettyv1.JobEvent], jsonMode bool) (*jettyv1.JobEvent, error) {
	var lastStep string
	var lastEv *jettyv1.JobEvent
	for stream.Receive() {
		ev := stream.Msg()
		lastEv = ev
		if !jsonMode && ev.GetCurrentStep() != "" && ev.GetCurrentStep() != lastStep {
			for _, st := range ev.GetSteps() {
				if st.GetKey() == ev.GetCurrentStep() {
					fmt.Fprintln(os.Stderr, st.GetLabel())
					break
				}
			}
			lastStep = ev.GetCurrentStep()
		}
		if ev.GetState() == "failed" && ev.GetErrorMessage() != "" {
			return ev, fmt.Errorf("%s", ev.GetErrorMessage())
		}
	}
	if err := stream.Err(); err != nil {
		return lastEv, err
	}
	return lastEv, nil
}

func cmdGateway(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 || args[0] != "setup" {
		return fmt.Errorf("usage: jetty gateway setup [--ssh-key PATH]")
	}
	fs := flag.NewFlagSet("gateway setup", flag.ContinueOnError)
	sshKeyPath := fs.String("ssh-key", "", "path to an SSH public key")
	if err := fs.Parse(args[1:]); err != nil {
		return err
	}
	var sshKey string
	if *sshKeyPath != "" {
		b, err := os.ReadFile(*sshKeyPath)
		if err != nil {
			return fmt.Errorf("could not read SSH public key %s: %w", *sshKeyPath, err)
		}
		sshKey = strings.TrimSpace(string(b))
	}
	stream, err := client.SetupGateway(ctx, connect.NewRequest(&jettyv1.SetupGatewayRequest{SshPublicKey: sshKey}))
	if err != nil {
		return err
	}
	defer stream.Close()
	ev, err := followJobStream(stream, jsonMode)
	if err != nil {
		return err
	}
	if jsonMode && ev != nil {
		return printProtoJSON(ev)
	}
	fmt.Printf("Gateway %s is ready.\n", ev.GetGateway())
	return nil
}

func cmdVM(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) (int, error) {
	if len(args) == 0 {
		return 2, fmt.Errorf("usage: jetty vm <create|delete|start|stop|restart|list|ip|put|exec|upload|port|reverse-port>")
	}
	switch args[0] {
	case "create":
		fs := flag.NewFlagSet("vm create", flag.ContinueOnError)
		cpus := fs.Int("cpus", 4, "")
		mem := fs.String("memory", "8GiB", "")
		disk := fs.String("disk", "40GiB", "")
		instType := fs.String("type", "virtual-machine", "")
		image := fs.String("image", "ubuntu:24.04", "")
		sshKeyPath := fs.String("ssh-key", "", "")
		name, rest, err := splitLeadingArg(args[1:])
		if err != nil {
			return 2, fmt.Errorf("usage: jetty vm create NAME [flags]")
		}
		if err := fs.Parse(rest); err != nil {
			return 2, err
		}
		var sshKey string
		if *sshKeyPath != "" {
			b, err := os.ReadFile(*sshKeyPath)
			if err != nil {
				return 1, err
			}
			sshKey = strings.TrimSpace(string(b))
		}
		stream, err := client.CreateVm(ctx, connect.NewRequest(&jettyv1.CreateVmRequest{
			Name:         name,
			Cpus:         int32(*cpus),
			Memory:       *mem,
			Disk:         *disk,
			Image:        *image,
			InstanceType: *instType,
			SshPublicKey: sshKey,
		}))
		if err != nil {
			return 1, err
		}
		defer stream.Close()
		ev, err := followJobStream(stream, jsonMode)
		if err != nil {
			return 1, err
		}
		if jsonMode && ev != nil {
			return 0, printProtoJSON(ev)
		}
		vm := ev.GetVm()
		fmt.Printf("%s is %s at %s\n", vm.GetName(), vm.GetStatus(), vm.GetIpAddress())
		return 0, nil

	case "list":
		resp, err := client.ListVms(ctx, connect.NewRequest(&jettyv1.ListVmsRequest{}))
		if err != nil {
			return 1, err
		}
		if jsonMode {
			return 0, printProtoJSON(resp.Msg)
		}
		for _, vm := range resp.Msg.GetVms() {
			ip := vm.GetIpAddress()
			if ip == "" {
				ip = "—"
			}
			fmt.Printf("%s\t%s\t%s\n", vm.GetName(), ip, vm.GetStatus())
		}
		return 0, nil

	case "ip":
		if len(args) < 2 {
			return 2, fmt.Errorf("usage: jetty vm ip NAME")
		}
		resp, err := client.ListVms(ctx, connect.NewRequest(&jettyv1.ListVmsRequest{}))
		if err != nil {
			return 1, err
		}
		for _, vm := range resp.Msg.GetVms() {
			if vm.GetName() == args[1] {
				if jsonMode {
					return 0, printProtoJSON(vm)
				}
				fmt.Println(vm.GetIpAddress())
				return 0, nil
			}
		}
		return 1, fmt.Errorf("VM '%s' was not found", args[1])

	case "put":
		if len(args) < 3 {
			return 2, fmt.Errorf("usage: jetty vm put NAME IP")
		}
		resp, err := client.PutVm(ctx, connect.NewRequest(&jettyv1.PutVmRequest{Name: args[1], IpAddress: args[2]}))
		if err != nil {
			return 1, err
		}
		if jsonMode {
			return 0, printProtoJSON(resp.Msg)
		}
		fmt.Printf("%s\t%s\n", resp.Msg.GetName(), resp.Msg.GetIpAddress())
		return 0, nil

	case "delete":
		if len(args) < 2 {
			return 2, fmt.Errorf("usage: jetty vm delete NAME")
		}
		resp, err := client.DeleteVm(ctx, connect.NewRequest(&jettyv1.DeleteVmRequest{Name: args[1]}))
		if err != nil {
			return 1, err
		}
		if jsonMode {
			return 0, printProtoJSON(resp.Msg)
		}
		fmt.Printf("%s deleted\n", args[1])
		return 0, nil

	case "start", "stop", "restart":
		if len(args) < 2 {
			return 2, fmt.Errorf("usage: jetty vm %s NAME", args[0])
		}
		resp, err := client.VmAction(ctx, connect.NewRequest(&jettyv1.VmActionRequest{Name: args[1], Action: args[0]}))
		if err != nil {
			return 1, err
		}
		if jsonMode {
			return 0, printProtoJSON(resp.Msg)
		}
		fmt.Printf("%s: %s\n", args[1], resp.Msg.GetStatus())
		return 0, nil

	case "exec":
		if len(args) < 3 {
			return 2, fmt.Errorf("usage: jetty vm exec NAME -- COMMAND [ARGS...]")
		}
		name := args[1]
		command := args[2:]
		if command[0] == "--" {
			command = command[1:]
		}
		if len(command) == 0 {
			return 2, fmt.Errorf("usage: jetty vm exec NAME -- COMMAND [ARGS...]")
		}
		stream := client.ExecVm(ctx)
		if err := stream.Send(&jettyv1.ExecVmRequest{
			Payload: &jettyv1.ExecVmRequest_Start{
				Start: &jettyv1.ExecVmStart{Name: name, Command: command},
			},
		}); err != nil {
			return 1, err
		}
		_ = stream.CloseRequest()
		exitCode := 0
		for {
			msg, err := stream.Receive()
			if err != nil {
				if err == io.EOF {
					break
				}
				return 1, err
			}
			if len(msg.GetStdout()) > 0 {
				_, _ = os.Stdout.Write(msg.GetStdout())
			}
			if len(msg.GetStderr()) > 0 {
				_, _ = os.Stderr.Write(msg.GetStderr())
			}
			if msg.GetExited() {
				exitCode = int(msg.GetExitCode())
			}
		}
		return exitCode, nil

	case "upload":
		if len(args) < 3 {
			return 2, fmt.Errorf("usage: jetty vm upload NAME GUEST_PATH [--file HOST_PATH] [--mode 0644] [--owner ubuntu|root]")
		}
		name := args[1]
		guestPath := args[2]
		fs := flag.NewFlagSet("vm upload", flag.ContinueOnError)
		filePath := fs.String("file", "", "")
		modeStr := fs.String("mode", "0644", "")
		owner := fs.String("owner", "ubuntu", "")
		if err := fs.Parse(args[3:]); err != nil {
			return 2, err
		}
		modeVal, err := strconv.ParseUint(*modeStr, 8, 32)
		if err != nil {
			return 2, fmt.Errorf("invalid mode %q: %w", *modeStr, err)
		}
		var content []byte
		if *filePath != "" {
			content, err = os.ReadFile(*filePath)
		} else {
			content, err = io.ReadAll(os.Stdin)
		}
		if err != nil {
			return 1, err
		}
		stream := client.UploadToVm(ctx)
		if err := stream.Send(&jettyv1.UploadToVmRequest{
			Payload: &jettyv1.UploadToVmRequest_Metadata{
				Metadata: &jettyv1.UploadMetadata{
					Name:  name,
					Path:  guestPath,
					Mode:  uint32(modeVal),
					Owner: *owner,
				},
			},
		}); err != nil {
			return 1, err
		}
		const chunkSize = 256 * 1024
		for off := 0; off < len(content); off += chunkSize {
			end := off + chunkSize
			if end > len(content) {
				end = len(content)
			}
			if err := stream.Send(&jettyv1.UploadToVmRequest{
				Payload: &jettyv1.UploadToVmRequest_Chunk{Chunk: content[off:end]},
			}); err != nil {
				return 1, err
			}
		}
		resp, err := stream.CloseAndReceive()
		if err != nil {
			return 1, err
		}
		if jsonMode {
			return 0, printProtoJSON(resp.Msg)
		}
		fmt.Printf("Uploaded %d bytes to %s:%s\n", resp.Msg.GetBytesWritten(), name, resp.Msg.GetPath())
		return 0, nil

	case "port":
		return 0, cmdVMPort(ctx, client, args[1:], jsonMode)
	case "reverse-port":
		return 0, cmdVMReversePort(ctx, client, args[1:], jsonMode)
	default:
		return 2, fmt.Errorf("unknown vm subcommand %q", args[0])
	}
}

func splitLeadingArg(args []string) (string, []string, error) {
	if len(args) == 0 || strings.HasPrefix(args[0], "-") {
		return "", nil, fmt.Errorf("missing required positional argument")
	}
	return args[0], args[1:], nil
}

func cmdVMPort(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: jetty vm port <open|list|close>")
	}
	switch args[0] {
	case "open":
		if len(args) < 3 {
			return fmt.Errorf("usage: jetty vm port open NAME VM_PORT [--host-port N] [--persistent]")
		}
		vmName := args[1]
		vmPort, err := strconv.Atoi(args[2])
		if err != nil {
			return err
		}
		fs := flag.NewFlagSet("port open", flag.ContinueOnError)
		hostPort := fs.Int("host-port", 0, "")
		persistent := fs.Bool("persistent", false, "")
		if err := fs.Parse(args[3:]); err != nil {
			return err
		}
		resp, err := client.OpenPort(ctx, connect.NewRequest(&jettyv1.OpenPortRequest{
			VmName:     vmName,
			VmPort:     int32(vmPort),
			HostPort:   int32(*hostPort),
			Persistent: *persistent,
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		kind := "one-time"
		if resp.Msg.GetPersistent() {
			kind = "persistent"
		}
		fmt.Printf("%s -> %s:%d (%s)\n", resp.Msg.GetUrl(), vmName, resp.Msg.GetVmPort(), kind)
		return nil
	case "list":
		vmName := ""
		if len(args) > 1 {
			vmName = args[1]
		}
		resp, err := client.ListPorts(ctx, connect.NewRequest(&jettyv1.ListPortsRequest{VmName: vmName}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		for _, p := range resp.Msg.GetPorts() {
			kind := "one-time"
			if p.GetPersistent() {
				kind = "persistent"
			}
			line := fmt.Sprintf("%s\t%s:%d\t%s\t%s", p.GetUrl(), p.GetVmName(), p.GetVmPort(), kind, p.GetState())
			if p.GetError() != "" {
				line += "\t" + p.GetError()
			}
			fmt.Println(line)
		}
		return nil
	case "close":
		if len(args) < 3 {
			return fmt.Errorf("usage: jetty vm port close NAME HOST_PORT")
		}
		hostPort, err := strconv.Atoi(args[2])
		if err != nil {
			return err
		}
		resp, err := client.ClosePort(ctx, connect.NewRequest(&jettyv1.ClosePortRequest{
			VmName:   args[1],
			HostPort: int32(hostPort),
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("Closed localhost:%d\n", hostPort)
		return nil
	}
	return fmt.Errorf("unknown port subcommand %q", args[0])
}

func cmdVMReversePort(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	if len(args) == 0 {
		return fmt.Errorf("usage: jetty vm reverse-port <open|list|close>")
	}
	switch args[0] {
	case "open":
		if len(args) < 3 {
			return fmt.Errorf("usage: jetty vm reverse-port open NAME HOST_PORT [--vm-port N] [--persistent]")
		}
		vmName := args[1]
		hostPort, err := strconv.Atoi(args[2])
		if err != nil {
			return err
		}
		fs := flag.NewFlagSet("reverse-port open", flag.ContinueOnError)
		vmPort := fs.Int("vm-port", 0, "")
		persistent := fs.Bool("persistent", false, "")
		if err := fs.Parse(args[3:]); err != nil {
			return err
		}
		resp, err := client.OpenReversePort(ctx, connect.NewRequest(&jettyv1.OpenReversePortRequest{
			VmName:     vmName,
			HostPort:   int32(hostPort),
			VmPort:     int32(*vmPort),
			Persistent: *persistent,
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		kind := "one-time"
		if resp.Msg.GetPersistent() {
			kind = "persistent"
		}
		fmt.Printf("%s:localhost:%d -> localhost:%d (%s)\n", vmName, resp.Msg.GetVmPort(), resp.Msg.GetHostPort(), kind)
		return nil
	case "list":
		vmName := ""
		if len(args) > 1 {
			vmName = args[1]
		}
		resp, err := client.ListReversePorts(ctx, connect.NewRequest(&jettyv1.ListReversePortsRequest{VmName: vmName}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		for _, p := range resp.Msg.GetPorts() {
			kind := "one-time"
			if p.GetPersistent() {
				kind = "persistent"
			}
			line := fmt.Sprintf("%s:%d\tlocalhost:%d\t%s\t%s", p.GetVmName(), p.GetVmPort(), p.GetHostPort(), kind, p.GetState())
			if p.GetError() != "" {
				line += "\t" + p.GetError()
			}
			fmt.Println(line)
		}
		return nil
	case "close":
		if len(args) < 3 {
			return fmt.Errorf("usage: jetty vm reverse-port close NAME VM_PORT")
		}
		vmPort, err := strconv.Atoi(args[2])
		if err != nil {
			return err
		}
		resp, err := client.CloseReversePort(ctx, connect.NewRequest(&jettyv1.CloseReversePortRequest{
			VmName: args[1],
			VmPort: int32(vmPort),
		}))
		if err != nil {
			return err
		}
		if jsonMode {
			return printProtoJSON(resp.Msg)
		}
		fmt.Printf("Closed %s:localhost:%d\n", args[1], vmPort)
		return nil
	}
	return fmt.Errorf("unknown reverse-port subcommand %q", args[0])
}

func cmdSSHConfig(ctx context.Context, client jettyv1connect.JettyClient, args []string, jsonMode bool) error {
	resp, err := client.GetSshConfig(ctx, connect.NewRequest(&jettyv1.GetSshConfigRequest{Names: args}))
	if err != nil {
		return err
	}
	if jsonMode {
		return printProtoJSON(resp.Msg)
	}
	fmt.Print(resp.Msg.GetConfig())
	return nil
}

func runUpdateCmd() error {
	if runtime.GOARCH != "amd64" || runtime.GOOS != "linux" {
		return fmt.Errorf("published Jetty releases currently support linux/amd64 only")
	}
	repo := os.Getenv("JETTY_REPOSITORY")
	if repo == "" {
		repo = "cau777/jetty-vm"
	}
	httpClient := &http.Client{Timeout: 60 * time.Second}
	req, _ := http.NewRequest(http.MethodGet, "https://api.github.com/repos/"+repo+"/releases/latest", nil)
	req.Header.Set("User-Agent", "Jetty-Updater")
	resp, err := httpClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("release lookup returned %s", resp.Status)
	}
	var release struct {
		TagName string `json:"tag_name"`
		Assets  []struct {
			Name               string `json:"name"`
			BrowserDownloadURL string `json:"browser_download_url"`
		} `json:"assets"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&release); err != nil {
		return err
	}
	if release.TagName == installedVersion() {
		fmt.Printf("Jetty %s is already current.\n", release.TagName)
		return nil
	}
	const assetName = "jetty-linux-amd64"
	const sumName = assetName + ".sha256"
	var binURL, sumURL string
	for _, a := range release.Assets {
		if a.Name == assetName {
			binURL = a.BrowserDownloadURL
		} else if a.Name == sumName {
			sumURL = a.BrowserDownloadURL
		}
	}
	if binURL == "" || sumURL == "" {
		return fmt.Errorf("release %s does not contain %s and %s", release.TagName, assetName, sumName)
	}
	sumResp, err := httpClient.Get(sumURL)
	if err != nil {
		return err
	}
	sumBytes, err := io.ReadAll(sumResp.Body)
	sumResp.Body.Close()
	if err != nil {
		return err
	}
	expected := strings.Fields(string(sumBytes))
	if len(expected) == 0 {
		return fmt.Errorf("empty checksum file")
	}

	home, _ := os.UserHomeDir()
	target := filepath.Join(home, ".local", "bin", "jetty")
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(target), ".jetty-update-*")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	binResp, err := httpClient.Get(binURL)
	if err != nil {
		tmp.Close()
		return err
	}
	h := sha256.New()
	_, err = io.Copy(io.MultiWriter(tmp, h), binResp.Body)
	binResp.Body.Close()
	tmp.Close()
	if err != nil {
		return err
	}
	if !strings.EqualFold(hex.EncodeToString(h.Sum(nil)), expected[0]) {
		return fmt.Errorf("checksum verification failed")
	}
	if err := os.Chmod(tmpPath, 0o755); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, target); err != nil {
		return err
	}
	_ = exec.Command("systemctl", "--user", "restart", "jetty-daemon.service").Run()
	fmt.Printf("Updated Jetty to %s.\n", release.TagName)
	return nil
}

var _ = validation.ValidateName
