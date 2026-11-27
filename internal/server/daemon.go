package server

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/net/http2"
	"golang.org/x/net/http2/h2c"

	"github.com/cau777/jetty-vm/gen/go/jetty/v1/jettyv1connect"
	"github.com/cau777/jetty-vm/internal/config"
	"github.com/cau777/jetty-vm/internal/lxd"
)

// GatewayOptions configures the gateway listener's peer IP and bearer token enforcement.
type GatewayOptions struct {
	AllowedPeerIP string
	Token         string
}

// WrapHandler wraps a Connect handler with strict Host validation, optional gateway
// peer+bearer auth, and zero CORS headers (DNS rebinding protection).
func WrapHandler(next http.Handler, allowedHosts []string, gw *GatewayOptions) http.Handler {
	hostSet := make(map[string]struct{}, len(allowedHosts))
	for _, h := range allowedHosts {
		hostSet[strings.ToLower(h)] = struct{}{}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, ok := hostSet[strings.ToLower(r.Host)]; !ok {
			http.Error(w, "invalid Host header", http.StatusMisdirectedRequest)
			return
		}
		if gw != nil {
			peerIP, _, _ := net.SplitHostPort(r.RemoteAddr)
			if gw.AllowedPeerIP != "" && peerIP != gw.AllowedPeerIP {
				http.Error(w, "forbidden peer", http.StatusForbidden)
				return
			}
			auth := r.Header.Get("Authorization")
			const prefix = "Bearer "
			if !strings.HasPrefix(auth, prefix) {
				http.Error(w, "missing bearer token", http.StatusUnauthorized)
				return
			}
			got := auth[len(prefix):]
			if subtle.ConstantTimeCompare([]byte(got), []byte(gw.Token)) != 1 {
				http.Error(w, "invalid bearer token", http.StatusUnauthorized)
				return
			}
		}
		next.ServeHTTP(w, r)
	})
}

// NewHTTPServer builds an h2c-enabled http.Server around handler.
func NewHTTPServer(handler http.Handler) *http.Server {
	h2s := &http2.Server{}
	return &http.Server{
		Handler: h2c.NewHandler(handler, h2s),
	}
}

type peerFilterListener struct {
	net.Listener
	allowedIP string
}

func (l *peerFilterListener) Accept() (net.Conn, error) {
	for {
		c, err := l.Listener.Accept()
		if err != nil {
			return nil, err
		}
		if l.allowedIP != "" {
			if tcpAddr, ok := c.RemoteAddr().(*net.TCPAddr); ok && tcpAddr.IP.String() != l.allowedIP {
				_ = c.Close()
				continue
			}
		}
		return c, nil
	}
}

// RunDaemon starts jettyd on loopback and supervises the gateway uplink listener.
func RunDaemon(ctx context.Context, srv *Server, port int) error {
	if port <= 0 {
		port = config.ListenPort()
	}
	token, err := config.EnsureGatewayToken()
	if err != nil {
		return err
	}

	_, connectHandler := jettyv1connect.NewJettyHandler(srv)

	loopAddr := fmt.Sprintf("127.0.0.1:%d", port)
	loopLn, err := net.Listen("tcp", loopAddr)
	if err != nil {
		return err
	}
	loopHTTP := NewHTTPServer(WrapHandler(connectHandler, []string{loopAddr}, nil))

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		if err := loopHTTP.Serve(loopLn); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Printf("loopback server error: %v", err)
		}
	}()

	_ = srv.Ports().StartPersistent()
	_ = srv.ReversePorts().StartPersistent()
	srv.SetDaemonState("running")
	log.Printf("jettyd listening on %s", loopAddr)

	// Supervise 10.201.0.1:7700 when jettyup0 appears.
	gwAddr := fmt.Sprintf("%s:%d", lxd.UpAddr, port)
	gwHTTP := NewHTTPServer(WrapHandler(connectHandler, []string{gwAddr}, &GatewayOptions{
		AllowedPeerIP: lxd.GatewayUpIP,
		Token:         token,
	}))

	wg.Add(1)
	go func() {
		defer wg.Done()
		var activeLn net.Listener
		var serveErr chan error
		closeActive := func() {
			if activeLn != nil {
				_ = gwHTTP.Shutdown(context.Background())
				activeLn = nil
				serveErr = nil
			}
		}
		defer closeActive()

		for {
			if activeLn == nil && config.HostHasIPv4(lxd.UpAddr) {
				ln, err := net.Listen("tcp", gwAddr)
				if err != nil {
					log.Printf("gateway listener bind %s failed: %v", gwAddr, err)
				} else {
					activeLn = &peerFilterListener{Listener: ln, allowedIP: lxd.GatewayUpIP}
					serveErr = make(chan error, 1)
					log.Printf("jettyd gateway listener bound on %s", gwAddr)
					go func(l net.Listener, ch chan error) {
						ch <- gwHTTP.Serve(l)
					}(activeLn, serveErr)
				}
			}
			select {
			case <-ctx.Done():
				return
			case err := <-serveErr:
				if err != nil && !errors.Is(err, http.ErrServerClosed) {
					log.Printf("gateway listener exited: %v", err)
				}
				activeLn = nil
				serveErr = nil
				// Re-instantiate server for next bind after Shutdown/Close
				gwHTTP = NewHTTPServer(WrapHandler(connectHandler, []string{gwAddr}, &GatewayOptions{
					AllowedPeerIP: lxd.GatewayUpIP,
					Token:         token,
				}))
			case <-time.After(2 * time.Second):
			}
		}
	}()

	<-ctx.Done()
	srv.SetDaemonState("stopping")
	srv.Ports().Shutdown()
	srv.ReversePorts().Shutdown()
	shutCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	_ = loopHTTP.Shutdown(shutCtx)
	cancel()
	wg.Wait()
	srv.SetDaemonState("stopped")
	return nil
}

// RunDaemonWithSignals runs RunDaemon until SIGINT or SIGTERM.
func RunDaemonWithSignals(srv *Server, port int) error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return RunDaemon(ctx, srv, port)
}
