package redaction

import "testing"

func TestRedactHeadersAllowlistAndInjected(t *testing.T) {
	out := RedactHeaders([]HeaderPair{
		{Name: "Host", Value: "api.github.com"},
		{Name: "Authorization", Value: "Bearer secret-val"},
		{Name: "X-Custom-Secret", Value: "nope"},
	}, "Authorization", "github-host-login")

	if len(out) != 3 {
		t.Fatalf("got %d", len(out))
	}
	if out[0].Redacted || out[0].Value != "api.github.com" {
		t.Fatalf("allowlisted host redacted: %+v", out[0])
	}
	if !out[1].Redacted || *out[1].RedactionReason != "injected" || out[1].Value != "[REDACTED · injected by github-host-login]" {
		t.Fatalf("injected header mismatch: %+v", out[1])
	}
	if !out[2].Redacted || *out[2].RedactionReason != "not_allowlisted" || out[2].Value != "[REDACTED]" {
		t.Fatalf("unknown header mismatch: %+v", out[2])
	}
}
