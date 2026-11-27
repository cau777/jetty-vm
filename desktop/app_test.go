package main

import (
	"context"
	"net"
	"path/filepath"
	"testing"
	"time"

	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/db"
	"github.com/cau777/jetty-vm/internal/server"
)

func TestDesktopAppWatchAndCRUD(t *testing.T) {
	dir := t.TempDir()
	stateConn, err := db.Open(filepath.Join(dir, "state.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer stateConn.Close()
	if err := db.MigrateState(stateConn); err != nil {
		t.Fatal(err)
	}
	reqConn, err := db.Open(filepath.Join(dir, "requests.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer reqConn.Close()
	if err := db.MigrateRequests(reqConn); err != nil {
		t.Fatal(err)
	}

	srv, err := server.NewServer(server.Dependencies{StateDB: stateConn, RequestsDB: reqConn})
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Ports().Shutdown()
	defer srv.ReversePorts().Shutdown()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	_, handler := jettyv1connect.NewJettyHandler(srv)
	hs := server.NewHTTPServer(server.WrapHandler(handler, []string{ln.Addr().String()}, nil))
	go hs.Serve(ln)
	defer hs.Close()

	app := NewApp(ln.Addr().String())
	eventsCh := make(chan string, 16)
	app.emitEvent = func(name string, data any) {
		eventsCh <- name
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	app.startup(ctx)
	defer app.shutdown(ctx)

	if _, err := app.PutCredential("gh", "printf ok", 300); err != nil {
		t.Fatalf("PutCredential failed: %v", err)
	}
	if _, err := app.PutRule(RuleInput{
		Name: "allow-gh", Priority: 10, Hostname: "api.github.com",
		ActionType: "allow_with_credential", Credential: "gh", PathPrefix: "/", InjType: "bearer",
	}); err != nil {
		t.Fatalf("PutRule failed: %v", err)
	}
	if _, err := app.GetQuickAddCatalog(); err != nil {
		t.Fatalf("GetQuickAddCatalog failed: %v", err)
	}
	if _, err := app.GetCa(); err != nil {
		t.Fatalf("GetCa failed: %v", err)
	}

	// The desktop receives protobuf int64 IDs as JSON strings. A JS number
	// would round this connection ID before the inspector sent it back.
	const connectionID = "9007199254740993"
	if _, err := reqConn.Exec(`INSERT INTO connections
		(id, started_at, vm_name, destination_ip, destination_port, sni_present, ech_present, intercepted)
		VALUES (?, ?, 'test-vm', '192.0.2.1', 443, 0, 0, 0)`,
		connectionID, time.Now().UTC().Format(time.RFC3339)); err != nil {
		t.Fatal(err)
	}
	detail, err := app.GetRequest(connectionID)
	if err != nil {
		t.Fatalf("GetRequest failed: %v", err)
	}
	connection := detail.(map[string]any)["connection"].(map[string]any)
	if connection["id"] != connectionID {
		t.Fatalf("connection ID = %v, want %s", connection["id"], connectionID)
	}

	select {
	case ev := <-eventsCh:
		if ev == "" {
			t.Fatal("empty event")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("expected watch/connection event from desktop App")
	}
}
