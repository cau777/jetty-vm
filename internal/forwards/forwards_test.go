package forwards

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/cau777/jetty-vm/internal/db"
)

type fakeTunnel struct {
	done chan struct{}
}

func (f *fakeTunnel) Wait() error {
	<-f.done
	return nil
}

func (f *fakeTunnel) Stop() error {
	select {
	case <-f.done:
	default:
		close(f.done)
	}
	return nil
}

func (f *fakeTunnel) LastError() string { return "" }

func TestPortAndReverseForwardLifecycle(t *testing.T) {
	dir := t.TempDir()
	conn, err := db.Open(filepath.Join(dir, "state.sqlite"))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := db.MigrateState(conn); err != nil {
		t.Fatal(err)
	}
	if _, _, err := db.PutVM(conn, "dev", "10.202.0.11"); err != nil {
		t.Fatal(err)
	}

	fakeConn := func(_ context.Context, _ *Forward) (Tunnel, error) {
		return &fakeTunnel{done: make(chan struct{})}, nil
	}
	pm := NewPortManager(conn, fakeConn, nil)
	defer pm.Shutdown()
	rm := NewReverseManager(conn, fakeConn, nil)
	defer rm.Shutdown()

	snap, err := pm.Open("dev", 3000, 13000, true)
	if err != nil || snap.HostPort != 13000 || !snap.Persistent {
		t.Fatalf("Open port failed: %+v, %v", snap, err)
	}
	rsnap, err := rm.Open("dev", 8080, 18080, true)
	if err != nil || rsnap.VMPort != 18080 {
		t.Fatalf("Open reverse failed: %+v, %v", rsnap, err)
	}

	time.Sleep(30 * time.Millisecond)
	if len(pm.List("dev")) != 1 || len(rm.List("dev")) != 1 {
		t.Fatal("expected 1 port and 1 reverse forward")
	}
	if err := pm.Close("dev", 13000); err != nil {
		t.Fatal(err)
	}
	if err := rm.Close("dev", 18080); err != nil {
		t.Fatal(err)
	}
}
