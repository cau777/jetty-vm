// Package credentials implements single-flight bash credential execution and TTL caching.
package credentials

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/cau777/jetty-vm/internal/aws"
)

const (
	DefaultTimeout = 30 * time.Second
	MaxOutputBytes = 16 * 1024
)

// ExecutionError records a category ("exit" | "timeout" | "invalid_output") without leaking stdout.
type ExecutionError struct {
	Category string
	Detail   string
	ExitCode int
}

func (e *ExecutionError) Error() string { return e.Detail }

// Digest returns the SHA-256 hex digest of a Credential Value.
func Digest(value string) string {
	sum := sha256.Sum256([]byte(value))
	return hex.EncodeToString(sum[:])
}

// Status is the safe ephemeral metadata exposed via APIs.
type Status struct {
	Status          string
	ExpiresAt       *float64
	LastSuccessAt   *float64
	LastFailureAt   *float64
	FailureCategory string
}

type inflightCall struct {
	done  chan struct{}
	value string
	err   error
	gen   uint64
}

type state struct {
	status          string
	value           string
	hasValue        bool
	expiresAt       *time.Time
	lastSuccessAt   *time.Time
	lastFailureAt   *time.Time
	failureCategory string
	gen             uint64
	cancel          context.CancelFunc
	inflight        *inflightCall
}

// Cache provides single-flight execution, TTL caching, and invalidation for Credentials.
type Cache struct {
	mu      sync.Mutex
	timeout time.Duration
	states  map[string]*state
}

// NewCache creates a CredentialCache with timeout (or DefaultTimeout if <= 0).
func NewCache(timeout time.Duration) *Cache {
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	return &Cache{
		timeout: timeout,
		states:  make(map[string]*state),
	}
}

func (c *Cache) getStateLocked(name string) *state {
	st, ok := c.states[name]
	if !ok {
		st = &state{status: "empty"}
		c.states[name] = st
	}
	return st
}

// ValidateOutput validates raw command output for "header" or "aws_process" format.
func ValidateOutput(raw []byte, outputFormat string) (string, error) {
	if len(raw) > MaxOutputBytes {
		return "", &ExecutionError{Category: "invalid_output", Detail: "output larger than 16 KiB"}
	}
	if !utf8.Valid(raw) {
		return "", &ExecutionError{Category: "invalid_output", Detail: "output is not valid UTF-8"}
	}
	text := string(raw)
	if outputFormat == "aws_process" {
		creds, _, err := aws.ParseCredentials(text)
		if err != nil {
			return "", &ExecutionError{Category: "invalid_output", Detail: err.Error()}
		}
		compact, err := json.Marshal(creds)
		if err != nil {
			return "", &ExecutionError{Category: "invalid_output", Detail: err.Error()}
		}
		return string(compact), nil
	}
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return "", &ExecutionError{Category: "invalid_output", Detail: "output is empty"}
	}
	for i := 0; i < len(text); i++ {
		if text[i] < 0x20 || text[i] == 0x7F {
			return "", &ExecutionError{Category: "invalid_output", Detail: "output contains a control character"}
		}
	}
	return text, nil
}

// GetValue returns a cached or freshly executed Credential Value.
func (c *Cache) GetValue(ctx context.Context, name, command string, ttlSeconds int, outputFormat string) (string, error) {
	if outputFormat == "" {
		outputFormat = "header"
	}
	c.mu.Lock()
	st := c.getStateLocked(name)

	if outputFormat == "aws_process" && st.hasValue {
		if _, _, err := aws.ParseCredentials(st.value); err != nil {
			st.hasValue = false
			st.value = ""
			st.expiresAt = nil
		}
	}

	now := time.Now()
	if st.hasValue && (st.expiresAt == nil || now.Before(*st.expiresAt)) {
		val := st.value
		c.mu.Unlock()
		return ValidateOutput([]byte(val), outputFormat)
	}

	if st.inflight != nil {
		call := st.inflight
		c.mu.Unlock()
		select {
		case <-call.done:
			if call.err != nil {
				return "", call.err
			}
			return ValidateOutput([]byte(call.value), outputFormat)
		case <-ctx.Done():
			return "", &ExecutionError{Category: "exit", Detail: ctx.Err().Error()}
		}
	}

	st.status = "refreshing"
	gen := st.gen
	execCtx, cancel := context.WithTimeout(context.Background(), c.timeout)
	st.cancel = cancel
	call := &inflightCall{done: make(chan struct{}), gen: gen}
	st.inflight = call
	c.mu.Unlock()

	val, execErr := executeCommand(execCtx, command, outputFormat)
	cancel()

	var awsDeadline *time.Time
	if execErr == nil && outputFormat == "aws_process" {
		_, awsDeadline, execErr = aws.ParseCredentials(val)
		if execErr != nil {
			execErr = &ExecutionError{Category: "invalid_output", Detail: execErr.Error()}
		}
	}

	c.mu.Lock()
	cur := c.getStateLocked(name)
	// Discard result if Invalidate/Drop incremented generation while running.
	if cur.gen != gen {
		if execErr == nil {
			execErr = &ExecutionError{Category: "exit", Detail: "credential invalidated during execution"}
		}
		call.err = execErr
		close(call.done)
		c.mu.Unlock()
		return "", execErr
	}

	cur.inflight = nil
	cur.cancel = nil
	if execErr != nil {
		nowFail := time.Now()
		cur.status = "error"
		cur.lastFailureAt = &nowFail
		var ee *ExecutionError
		if errors.As(execErr, &ee) {
			cur.failureCategory = ee.Category
		} else {
			cur.failureCategory = "exit"
		}
		call.err = execErr
		close(call.done)
		c.mu.Unlock()
		return "", execErr
	}

	if ttlSeconds > 0 {
		cur.value = val
		cur.hasValue = true
		exp := time.Now().Add(time.Duration(ttlSeconds) * time.Second)
		if awsDeadline != nil && awsDeadline.Before(exp) {
			exp = *awsDeadline
		}
		cur.expiresAt = &exp
	}
	nowOK := time.Now()
	cur.status = "valid"
	cur.lastSuccessAt = &nowOK
	cur.failureCategory = ""
	call.value = val
	close(call.done)
	c.mu.Unlock()

	return val, nil
}

func executeCommand(ctx context.Context, command, outputFormat string) (string, error) {
	cmd := exec.Command("/usr/bin/bash", "-lc", command)
	cmd.Stdin = nil
	var stdout bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = nil
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	if err := cmd.Start(); err != nil {
		return "", &ExecutionError{Category: "exit", Detail: err.Error()}
	}
	done := make(chan error, 1)
	go func() {
		done <- cmd.Wait()
	}()

	select {
	case err := <-done:
		if err != nil {
			var exitErr *exec.ExitError
			code := 1
			if errors.As(err, &exitErr) {
				code = exitErr.ExitCode()
			}
			return "", &ExecutionError{Category: "exit", Detail: fmt.Sprintf("exit status %d", code), ExitCode: code}
		}
		return ValidateOutput(stdout.Bytes(), outputFormat)
	case <-ctx.Done():
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		<-done
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return "", &ExecutionError{Category: "timeout", Detail: "command timed out"}
		}
		return "", &ExecutionError{Category: "exit", Detail: ctx.Err().Error()}
	}
}

// Invalidate clears cached state and kills any in-flight command execution.
func (c *Cache) Invalidate(name string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if st, ok := c.states[name]; ok {
		st.gen++
		if st.cancel != nil {
			st.cancel()
		}
		c.states[name] = &state{status: "empty", gen: st.gen}
		return
	}
	c.states[name] = &state{status: "empty"}
}

// Reject forgets a cached value if its digest matches the rejected token.
func (c *Cache) Reject(name, valueDigest string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.states[name]
	if !ok || !st.hasValue || Digest(st.value) != valueDigest {
		return false
	}
	st.value = ""
	st.hasValue = false
	st.expiresAt = nil
	st.status = "empty"
	return true
}

// Drop removes all cached state for a deleted Credential.
func (c *Cache) Drop(name string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if st, ok := c.states[name]; ok {
		st.gen++
		if st.cancel != nil {
			st.cancel()
		}
		delete(c.states, name)
	}
}

// GetStatus returns safe ephemeral status without secrets.
func (c *Cache) GetStatus(name string) Status {
	c.mu.Lock()
	defer c.mu.Unlock()
	st, ok := c.states[name]
	if !ok {
		return Status{Status: "empty"}
	}
	out := Status{
		Status:          st.status,
		FailureCategory: st.failureCategory,
	}
	if st.expiresAt != nil {
		// Approximate unix timestamp (consistent with Python time.monotonic/wall usage)
		v := float64(st.expiresAt.UnixNano()) / 1e9
		out.ExpiresAt = &v
	}
	if st.lastSuccessAt != nil {
		v := float64(st.lastSuccessAt.UnixNano()) / 1e9
		out.LastSuccessAt = &v
	}
	if st.lastFailureAt != nil {
		v := float64(st.lastFailureAt.UnixNano()) / 1e9
		out.LastFailureAt = &v
	}
	return out
}
