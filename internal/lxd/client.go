// Package lxd implements Jetty's Unix-socket LXD client and lifecycle manager.
package lxd

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// ExecResult captures noninteractive LXD command output.
type ExecResult struct {
	ExitCode int
	Stdout   []byte
	Stderr   []byte
}

// Error represents a local LXD API failure.
type Error struct {
	Message  string
	NotFound bool
}

func (e *Error) Error() string { return e.Message }

// SocketPath resolves LXD's Unix socket using the standard search order.
func SocketPath() string {
	if s := os.Getenv("LXD_SOCKET"); s != "" {
		return s
	}
	if d := os.Getenv("LXD_DIR"); d != "" {
		return filepath.Join(d, "unix.socket")
	}
	snap := "/var/snap/lxd/common/lxd/unix.socket"
	if info, err := os.Stat(snap); err == nil && (info.Mode()&os.ModeSocket) != 0 {
		if f, err := os.OpenFile(snap, os.O_RDWR, 0); err == nil {
			f.Close()
			return snap
		}
	}
	return "/var/lib/lxd/unix.socket"
}

// SocketAccessible checks whether the LXD Unix socket exists and is R/W accessible.
func SocketAccessible(path string) bool {
	if path == "" {
		path = SocketPath()
	}
	info, err := os.Stat(path)
	if err != nil || (info.Mode()&os.ModeSocket) == 0 {
		return false
	}
	conn, err := net.DialTimeout("unix", path, time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// Client talks to LXD over its local Unix socket.
type Client interface {
	Request(ctx context.Context, method, path string, body any, project *string, wait bool) (any, error)
	GetFile(ctx context.Context, instance, guestPath string) ([]byte, error)
	PutFile(ctx context.Context, instance, guestPath string, content []byte, mode int, uid, gid int) error
	DeleteFile(ctx context.Context, instance, guestPath string) error
	Execute(ctx context.Context, instance string, command []string, opts ExecOpts) (ExecResult, error)
}

// ExecOpts configures command execution inside an LXD instance.
type ExecOpts struct {
	User        int
	Group       int
	Cwd         string
	Environment map[string]string
}

type unixClient struct {
	project    string
	socketPath string
	http       *http.Client
}

// NewUnixClient opens a Client targeting LXD's local Unix socket.
func NewUnixClient(project, socketPath string) (Client, error) {
	if socketPath == "" {
		socketPath = SocketPath()
	}
	if !SocketAccessible(socketPath) {
		return nil, &Error{
			Message: fmt.Sprintf("LXD socket is not accessible at %s. Install LXD and sign in again after adding your account to the lxd group.", socketPath),
		}
	}
	if project == "" {
		project = "jetty"
	}
	tr := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 15 * time.Second}).DialContext(ctx, "unix", socketPath)
		},
	}
	return &unixClient{
		project:    project,
		socketPath: socketPath,
		http:       &http.Client{Transport: tr, Timeout: 15 * time.Minute},
	}, nil
}

func projectPath(path string, project *string) string {
	if project == nil || *project == "" {
		return path
	}
	sep := "?"
	if strings.Contains(path, "?") {
		sep = "&"
	}
	return path + sep + "project=" + url.QueryEscape(*project)
}

type lxdEnvelope struct {
	Type       string          `json:"type"`
	StatusCode int             `json:"status_code"`
	Operation  string          `json:"operation"`
	Error      string          `json:"error"`
	Metadata   json.RawMessage `json:"metadata"`
}

func (c *unixClient) do(ctx context.Context, method, path string, jsonBody any, rawBody []byte, headers map[string]string) (int, *lxdEnvelope, []byte, error) {
	var bodyReader io.Reader
	if jsonBody != nil {
		b, err := json.Marshal(jsonBody)
		if err != nil {
			return 0, nil, nil, err
		}
		bodyReader = bytes.NewReader(b)
	} else if rawBody != nil {
		bodyReader = bytes.NewReader(rawBody)
	}
	req, err := http.NewRequestWithContext(ctx, method, "http://unix.socket"+path, bodyReader)
	if err != nil {
		return 0, nil, nil, &Error{Message: err.Error()}
	}
	if jsonBody != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, nil, &Error{Message: fmt.Sprintf("LXD %s %s: %v", method, path, err)}
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp.StatusCode, nil, nil, &Error{Message: fmt.Sprintf("LXD %s %s: %v", method, path, err)}
	}

	var env *lxdEnvelope
	if strings.Contains(resp.Header.Get("Content-Type"), "json") {
		var parsed lxdEnvelope
		if json.Unmarshal(raw, &parsed) == nil {
			env = &parsed
		}
	}
	if resp.StatusCode >= 400 || (env != nil && env.Type == "error") {
		msg := resp.Status
		if env != nil && env.Error != "" {
			msg = env.Error
		} else if len(raw) > 0 {
			msg = strings.TrimSpace(string(raw))
		}
		return resp.StatusCode, env, raw, &Error{
			Message:  fmt.Sprintf("LXD %s %s: %s", method, path, msg),
			NotFound: resp.StatusCode == http.StatusNotFound,
		}
	}
	return resp.StatusCode, env, raw, nil
}

func (c *unixClient) Request(ctx context.Context, method, path string, body any, project *string, wait bool) (any, error) {
	effProj := &c.project
	if project != nil {
		if *project == "" {
			effProj = nil
		} else {
			effProj = project
		}
	}
	fullPath := projectPath(path, effProj)
	_, env, _, err := c.do(ctx, method, fullPath, body, nil, nil)
	if err != nil {
		return nil, err
	}
	if env == nil {
		return nil, &Error{Message: fmt.Sprintf("LXD %s %s returned an invalid response", method, fullPath)}
	}
	if env.Type == "async" {
		if !wait {
			return env, nil
		}
		op := env.Operation
		if op == "" {
			var meta map[string]any
			_ = json.Unmarshal(env.Metadata, &meta)
			if id, ok := meta["id"].(string); ok {
				op = id
			}
		}
		if op == "" {
			return nil, &Error{Message: fmt.Sprintf("LXD %s %s did not return an operation URL", method, fullPath)}
		}
		return c.waitOperation(ctx, op)
	}
	if env.Type != "sync" {
		return nil, &Error{Message: fmt.Sprintf("LXD %s %s returned an unknown response", method, fullPath)}
	}
	var out any
	if len(env.Metadata) > 0 {
		_ = json.Unmarshal(env.Metadata, &out)
	}
	return out, nil
}

type opMetadata struct {
	StatusCode int            `json:"status_code"`
	Err        string         `json:"err"`
	Metadata   map[string]any `json:"metadata"`
}

func (c *unixClient) waitOperation(ctx context.Context, operation string) (map[string]any, error) {
	if !strings.HasPrefix(operation, "/1.0/") {
		operation = "/1.0/operations/" + url.PathEscape(operation)
	}
	deadline := time.Now().Add(15 * time.Minute)
	for {
		sep := "?"
		if strings.Contains(operation, "?") {
			sep = "&"
		}
		_, env, _, err := c.do(ctx, http.MethodGet, operation+sep+"timeout=30", nil, nil, nil)
		if err != nil {
			return nil, err
		}
		if env == nil || env.Type == "error" {
			return nil, &Error{Message: fmt.Sprintf("LXD operation %s returned an invalid response", operation)}
		}
		var meta opMetadata
		if err := json.Unmarshal(env.Metadata, &meta); err != nil {
			return nil, &Error{Message: err.Error()}
		}
		switch meta.StatusCode {
		case 200:
			return meta.Metadata, nil
		case 112, 400, 401:
			msg := meta.Err
			if msg == "" {
				msg = fmt.Sprintf("LXD operation failed (%d)", meta.StatusCode)
			}
			return nil, &Error{Message: msg}
		}
		if time.Now().After(deadline) {
			return nil, &Error{Message: fmt.Sprintf("LXD operation timed out: %s", operation)}
		}
	}
}

func (c *unixClient) GetFile(ctx context.Context, instance, guestPath string) ([]byte, error) {
	p := fmt.Sprintf("/1.0/instances/%s/files?path=%s", url.PathEscape(instance), url.QueryEscape(guestPath))
	_, _, raw, err := c.do(ctx, http.MethodGet, projectPath(p, &c.project), nil, nil, nil)
	return raw, err
}

func (c *unixClient) PutFile(ctx context.Context, instance, guestPath string, content []byte, mode int, uid, gid int) error {
	p := fmt.Sprintf("/1.0/instances/%s/files?path=%s", url.PathEscape(instance), url.QueryEscape(guestPath))
	_, _, _, err := c.do(ctx, http.MethodPost, projectPath(p, &c.project), nil, content, map[string]string{
		"Content-Type": "application/octet-stream",
		"X-LXD-uid":    fmt.Sprintf("%d", uid),
		"X-LXD-gid":    fmt.Sprintf("%d", gid),
		"X-LXD-mode":   fmt.Sprintf("%o", mode),
	})
	return err
}

func (c *unixClient) DeleteFile(ctx context.Context, instance, guestPath string) error {
	p := fmt.Sprintf("/1.0/instances/%s/files?path=%s", url.PathEscape(instance), url.QueryEscape(guestPath))
	_, _, _, err := c.do(ctx, http.MethodDelete, projectPath(p, &c.project), nil, nil, nil)
	return err
}

func (c *unixClient) Execute(ctx context.Context, instance string, command []string, opts ExecOpts) (ExecResult, error) {
	if opts.Cwd == "" {
		opts.Cwd = "/root"
	}
	payload := map[string]any{
		"command":       command,
		"interactive":   false,
		"record-output": true,
		"user":          opts.User,
		"group":         opts.Group,
		"cwd":           opts.Cwd,
	}
	if len(opts.Environment) > 0 {
		payload["environment"] = opts.Environment
	}
	p := fmt.Sprintf("/1.0/instances/%s/exec", url.PathEscape(instance))
	rawEnv, err := c.Request(ctx, http.MethodPost, p, payload, nil, false)
	if err != nil {
		return ExecResult{}, err
	}
	env, ok := rawEnv.(*lxdEnvelope)
	if !ok || env == nil {
		return ExecResult{}, &Error{Message: fmt.Sprintf("LXD exec for %s returned invalid envelope", instance)}
	}
	op := env.Operation
	if op == "" {
		var m map[string]any
		_ = json.Unmarshal(env.Metadata, &m)
		if id, ok := m["id"].(string); ok {
			op = id
		}
	}
	if op == "" {
		return ExecResult{}, &Error{Message: fmt.Sprintf("LXD exec for %s did not return an operation URL", instance)}
	}
	resMeta, err := c.waitOperation(ctx, op)
	if err != nil {
		return ExecResult{}, err
	}
	outputFiles, _ := resMeta["output"].(map[string]any)
	var stdout, stderr []byte
	for _, pair := range []struct {
		id  string
		dst *[]byte
	}{
		{"1", &stdout},
		{"2", &stderr},
	} {
		outPath, _ := outputFiles[pair.id].(string)
		if outPath == "" {
			continue
		}
		parts := strings.Split(outPath, "/")
		filename := parts[len(parts)-1]
		endpoint := fmt.Sprintf("/1.0/instances/%s/logs/exec-output/%s", url.PathEscape(instance), url.PathEscape(filename))
		_, _, data, err := c.do(ctx, http.MethodGet, projectPath(endpoint, &c.project), nil, nil, nil)
		if err == nil {
			*pair.dst = data
			_, _, _, _ = c.do(ctx, http.MethodDelete, projectPath(endpoint, &c.project), nil, nil, nil)
		}
	}
	exitCode := 0
	if rc, ok := resMeta["return"].(float64); ok {
		exitCode = int(rc)
	}
	return ExecResult{ExitCode: exitCode, Stdout: stdout, Stderr: stderr}, nil
}

// IsNotFound checks whether an error represents LXD HTTP 404.
func IsNotFound(err error) bool {
	var le *Error
	return errors.As(err, &le) && le.NotFound
}
