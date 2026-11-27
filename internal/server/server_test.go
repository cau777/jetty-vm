package server

import (
	"context"
	"crypto/sha256"
	"crypto/tls"
	"net"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"connectrpc.com/connect"
	"golang.org/x/net/http2"

	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/db"
)

func newTestServer(t *testing.T) (*Server, func()) {
	t.Helper()
	dir := t.TempDir()
	stateConn, err := db.Open(filepath.Join(dir, "state.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.MigrateState(stateConn); err != nil {
		t.Fatal(err)
	}
	reqConn, err := db.Open(filepath.Join(dir, "requests.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	if err := db.MigrateRequests(reqConn); err != nil {
		t.Fatal(err)
	}
	srv, err := NewServer(Dependencies{StateDB: stateConn, RequestsDB: reqConn})
	if err != nil {
		t.Fatal(err)
	}
	return srv, func() {
		srv.Ports().Shutdown()
		srv.ReversePorts().Shutdown()
		stateConn.Close()
		reqConn.Close()
	}
}

func newH2CClient(baseURL string, extraHeaders http.Header) jettyv1connect.JettyClient {
	tr := &http2.Transport{
		AllowHTTP: true,
		DialTLSContext: func(ctx context.Context, network, addr string, _ *tls.Config) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, addr)
		},
	}
	hc := &http.Client{Transport: tr}
	if len(extraHeaders) > 0 {
		orig := hc.Transport
		hc.Transport = roundTripFunc(func(r *http.Request) (*http.Response, error) {
			r2 := r.Clone(r.Context())
			for k, vs := range extraHeaders {
				for _, v := range vs {
					r2.Header.Add(k, v)
				}
			}
			return orig.RoundTrip(r2)
		})
	}
	return jettyv1connect.NewJettyClient(hc, baseURL, connect.WithGRPC())
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestSecurityHostHeaderAndGatewayAuth(t *testing.T) {
	srv, cleanup := newTestServer(t)
	defer cleanup()

	_, handler := jettyv1connect.NewJettyHandler(srv)

	// Loopback listener with strict Host check
	lnLoop, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lnLoop.Close()
	loopAddr := lnLoop.Addr().String()
	httpLoop := NewHTTPServer(WrapHandler(handler, []string{loopAddr}, nil))
	go httpLoop.Serve(lnLoop)
	defer httpLoop.Close()

	// Wrong Host header -> 421 Misdirected Request and no CORS headers
	req, _ := http.NewRequest(http.MethodPost, "http://"+loopAddr+"/jetty.v1.Jetty/GetCa", strings.NewReader("{}"))
	req.Host = "evil.example:7700"
	resp, err := (&http.Client{}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusMisdirectedRequest {
		t.Fatalf("expected 421 for bad Host, got %d", resp.StatusCode)
	}
	if resp.Header.Get("Access-Control-Allow-Origin") != "" {
		t.Fatalf("expected no CORS headers")
	}

	// Gateway listener requiring peer 127.0.0.1 and bearer token "secret-gw-token"
	lnGW, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lnGW.Close()
	gwAddr := lnGW.Addr().String()
	httpGW := NewHTTPServer(WrapHandler(handler, []string{gwAddr}, &GatewayOptions{
		AllowedPeerIP: "127.0.0.1",
		Token:         "secret-gw-token",
	}))
	go httpGW.Serve(lnGW)
	defer httpGW.Close()

	// Missing token -> error
	unauthClient := newH2CClient("http://"+gwAddr, nil)
	if _, err := unauthClient.Hello(context.Background(), connect.NewRequest(&jettyv1.GatewayHello{AgentVersion: ExpectedAgentVersion})); err == nil {
		t.Fatal("expected missing token rejection")
	}

	// Valid token -> success
	authClient := newH2CClient("http://"+gwAddr, http.Header{"Authorization": []string{"Bearer secret-gw-token"}})
	helloResp, err := authClient.Hello(context.Background(), connect.NewRequest(&jettyv1.GatewayHello{AgentVersion: ExpectedAgentVersion}))
	if err != nil {
		t.Fatalf("Hello failed: %v", err)
	}
	if helloResp.Msg.GetExpectedVersion() != ExpectedAgentVersion {
		t.Fatalf("unexpected version: %q", helloResp.Msg.GetExpectedVersion())
	}

	// Wrong peer IP enforced on handler
	lnBadPeer, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer lnBadPeer.Close()
	badPeerAddr := lnBadPeer.Addr().String()
	httpBadPeer := NewHTTPServer(WrapHandler(handler, []string{badPeerAddr}, &GatewayOptions{
		AllowedPeerIP: "10.201.0.2",
		Token:         "secret-gw-token",
	}))
	go httpBadPeer.Serve(lnBadPeer)
	defer httpBadPeer.Close()

	badPeerClient := newH2CClient("http://"+badPeerAddr, http.Header{"Authorization": []string{"Bearer secret-gw-token"}})
	if _, err := badPeerClient.Hello(context.Background(), connect.NewRequest(&jettyv1.GatewayHello{AgentVersion: ExpectedAgentVersion})); err == nil {
		t.Fatal("expected wrong peer IP rejection")
	}
}

func TestHelloVersionMismatchTriggersRepush(t *testing.T) {
	srv, cleanup := newTestServer(t)
	defer cleanup()

	var called atomic.Int32
	repushDone := make(chan struct{}, 1)
	srv.RepushHook = func(ctx context.Context) error {
		called.Add(1)
		select {
		case repushDone <- struct{}{}:
		default:
		}
		return nil
	}

	_, err := srv.Hello(context.Background(), connect.NewRequest(&jettyv1.GatewayHello{AgentVersion: "0.9.0"}))
	if connect.CodeOf(err) != connect.CodeFailedPrecondition {
		t.Fatalf("expected FailedPrecondition, got %v", err)
	}
	select {
	case <-repushDone:
	case <-time.After(time.Second):
		t.Fatal("expected RepushHook to be invoked")
	}
	if called.Load() != 1 {
		t.Fatalf("expected 1 repush call, got %d", called.Load())
	}
}

func TestTrafficDecisionsAnd401Invalidation(t *testing.T) {
	srv, cleanup := newTestServer(t)
	defer cleanup()
	ctx := context.Background()

	// Register VM & Credential & Rules
	if _, err := srv.PutVm(ctx, connect.NewRequest(&jettyv1.PutVmRequest{Name: "dev", IpAddress: "10.202.0.11"})); err != nil {
		t.Fatal(err)
	}
	if _, err := srv.PutCredential(ctx, connect.NewRequest(&jettyv1.PutCredentialRequest{
		Name:       "gh-tok",
		Command:    "printf secret-val",
		TtlSeconds: 300,
	})); err != nil {
		t.Fatal(err)
	}
	if _, err := srv.PutRule(ctx, connect.NewRequest(&jettyv1.PutRuleRequest{
		Name:       "gh-api",
		Priority:   10,
		VmSelector: &jettyv1.VmSelector{Type: "only", Vms: []string{"dev"}},
		Hostname:   "api.github.com",
		Action: &jettyv1.RuleAction{
			Type:       "allow_with_credential",
			Credential: "gh-tok",
			PathPrefix: "/repos/cau777",
			Injection:  &jettyv1.Injection{Type: "bearer"},
		},
	})); err != nil {
		t.Fatal(err)
	}
	if _, err := srv.PutRule(ctx, connect.NewRequest(&jettyv1.PutRuleRequest{
		Name:       "block-evil",
		Priority:   20,
		VmSelector: &jettyv1.VmSelector{Type: "all"},
		Hostname:   "evil.example",
		Action:     &jettyv1.RuleAction{Type: "block"},
	})); err != nil {
		t.Fatal(err)
	}

	// Unknown source rejected & logged
	srcBad, err := srv.ResolveSource(ctx, connect.NewRequest(&jettyv1.SourceInfo{SourceIp: "10.202.0.99", DestIp: "1.1.1.1", DestPort: 443}))
	if err != nil || srcBad.Msg.GetAllowed() {
		t.Fatalf("expected unknown VM blocked, got %+v, %v", srcBad, err)
	}

	// Known source allowed
	srcOK, err := srv.ResolveSource(ctx, connect.NewRequest(&jettyv1.SourceInfo{SourceIp: "10.202.0.11", DestIp: "140.82.121.6", DestPort: 443}))
	if err != nil || !srcOK.Msg.GetAllowed() || srcOK.Msg.GetVmName() != "dev" {
		t.Fatalf("expected dev allowed, got %+v, %v", srcOK, err)
	}

	// DecideConnection: passthrough for example.com
	connPass, err := srv.DecideConnection(ctx, connect.NewRequest(&jettyv1.ConnectionInfo{
		SourceIp: "10.202.0.11", DestIp: "93.184.216.34", DestPort: 443, Sni: "example.com",
	}))
	if err != nil || connPass.Msg.GetPassthrough() == nil {
		t.Fatalf("expected passthrough, got %+v, %v", connPass, err)
	}

	// DecideConnection: block rule intercepts so DecideRequest returns 403
	connBlk, err := srv.DecideConnection(ctx, connect.NewRequest(&jettyv1.ConnectionInfo{
		SourceIp: "10.202.0.11", DestIp: "1.2.3.4", DestPort: 443, Sni: "evil.example",
	}))
	if err != nil || connBlk.Msg.GetIntercept() == nil {
		t.Fatalf("expected intercept for block rule, got %+v, %v", connBlk, err)
	}
	reqBlk, err := srv.DecideRequest(ctx, connect.NewRequest(&jettyv1.RequestInfo{
		ConnectionId: connBlk.Msg.GetConnectionId(),
		SourceIp:     "10.202.0.11",
		Method:       "GET",
		Host:         "evil.example",
		Path:         "/",
		Scheme:       "https",
	}))
	if err != nil || reqBlk.Msg.GetRespond().GetStatusCode() != 403 {
		t.Fatalf("expected 403 for block rule, got %+v, %v", reqBlk, err)
	}

	// DecideConnection + DecideRequest: inject credential
	connCred, err := srv.DecideConnection(ctx, connect.NewRequest(&jettyv1.ConnectionInfo{
		SourceIp: "10.202.0.11", DestIp: "140.82.121.6", DestPort: 443, Sni: "api.github.com",
	}))
	if err != nil || connCred.Msg.GetIntercept() == nil {
		t.Fatalf("expected intercept for api.github.com, got %+v, %v", connCred, err)
	}
	sum := sha256.Sum256(nil)
	reqCred, err := srv.DecideRequest(ctx, connect.NewRequest(&jettyv1.RequestInfo{
		ConnectionId: connCred.Msg.GetConnectionId(),
		SourceIp:     "10.202.0.11",
		Method:       "GET",
		Host:         "api.github.com",
		Path:         "/repos/cau777/jetty-vm/issues",
		RawPath:      "/repos/cau777/jetty-vm/issues?state=open",
		Scheme:       "https",
		Headers:      []*jettyv1.Header{{Name: "Host", Value: "api.github.com"}, {Name: "Authorization", Value: "Bearer placeholder"}},
		BodySha256:   sum[:],
		HasBody:      true,
	}))
	if err != nil || reqCred.Msg.GetForward() == nil {
		t.Fatalf("expected forward, got %+v, %v", reqCred, err)
	}
	if got := reqCred.Msg.GetForward().GetSetHeaders()[0].GetValue(); got != "Bearer secret-val" {
		t.Fatalf("expected injected secret, got %q", got)
	}

	// Plain HTTP credential attempt -> 403
	reqPlain, err := srv.DecideRequest(ctx, connect.NewRequest(&jettyv1.RequestInfo{
		ConnectionId: "",
		SourceIp:     "10.202.0.11",
		Method:       "GET",
		Host:         "api.github.com",
		Path:         "/repos/cau777/jetty-vm",
		Scheme:       "http",
	}))
	if err != nil || reqPlain.Msg.GetRespond().GetStatusCode() != 403 {
		t.Fatalf("expected 403 on plaintext cred, got %+v, %v", reqPlain, err)
	}

	// Verify 401 via Report invalidates cached credential
	if st := srv.creds.GetStatus("gh-tok"); st.Status != "valid" {
		t.Fatalf("expected valid credential, got %+v", st)
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, handler := jettyv1connect.NewJettyHandler(srv)
	hs := NewHTTPServer(WrapHandler(handler, []string{ln.Addr().String()}, nil))
	go hs.Serve(ln)
	defer hs.Close()

	client := newH2CClient("http://"+ln.Addr().String(), nil)
	repStream := client.Report(ctx)
	_ = repStream.Send(&jettyv1.GatewayEvent{
		Event: &jettyv1.GatewayEvent_RequestCompleted{
			RequestCompleted: &jettyv1.RequestCompleted{
				RequestId:    reqCred.Msg.GetRequestId(),
				ConnectionId: connCred.Msg.GetConnectionId(),
				StatusCode:   401,
				HasStatus:    true,
				StatusOrigin: "upstream",
				LatencyMs:    42,
				HasLatencyMs: true,
			},
		},
	})
	_ = repStream.Send(&jettyv1.GatewayEvent{
		Event: &jettyv1.GatewayEvent_ConnectionClosed{
			ConnectionClosed: &jettyv1.ConnectionClosed{
				ConnectionId: connCred.Msg.GetConnectionId(),
				DurationMs:   100,
			},
		},
	})
	if _, err := repStream.CloseAndReceive(); err != nil {
		t.Fatalf("Report failed: %v", err)
	}
	if st := srv.creds.GetStatus("gh-tok"); st.Status != "empty" {
		t.Fatalf("expected credential invalidated to empty on 401, got %+v", st)
	}

	// Verify GetRequest shows redacted header & upstream status
	connIDInt, _ := strconv.ParseInt(connCred.Msg.GetConnectionId(), 10, 64)
	detail, err := client.GetRequest(ctx, connect.NewRequest(&jettyv1.GetRequestRequest{Id: connIDInt}))
	if err != nil {
		t.Fatalf("GetRequest failed: %v", err)
	}
	if len(detail.Msg.GetHttpRequests()) != 1 || detail.Msg.GetHttpRequests()[0].GetStatus() != 401 {
		t.Fatalf("unexpected request log: %+v", detail.Msg)
	}
}
