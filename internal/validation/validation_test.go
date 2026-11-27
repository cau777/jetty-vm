package validation

import "testing"

func TestValidateHostnameAndPath(t *testing.T) {
	if h, err := ValidateHostname("API.GitHub.COM.", "hostname"); err != nil || h != "api.github.com" {
		t.Fatalf("got %q, %v", h, err)
	}
	if h, err := ValidateHostname("@aws", "hostname"); err != nil || h != "@aws" {
		t.Fatalf("got %q, %v", h, err)
	}
	if _, err := ValidateHostname("https://github.com", "hostname"); err == nil {
		t.Fatal("expected scheme error")
	}
	if _, err := ValidateHostname("github.com:443", "hostname"); err == nil {
		t.Fatal("expected port error")
	}
	if p, err := ValidatePathPrefix("/repos/cau777/", "path_prefix"); err != nil || p != "/repos/cau777" {
		t.Fatalf("got %q, %v", p, err)
	}
	if _, err := ValidatePathPrefix("/repos/../x", "path_prefix"); err == nil {
		t.Fatal("expected traversal rejection")
	}
}
