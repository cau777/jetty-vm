// Package validation mirrors Python validation.py for VMs, Rules, Credentials, and paths.
package validation

import (
	"fmt"
	"net/url"
	"regexp"
	"strconv"
	"strings"

	jerr "github.com/cau777/jetty-vm/internal/errors"
)

var (
	nameRe       = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	ruleNameRe   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)
	hostnameRe   = regexp.MustCompile(`^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$`)
	ipv4Re       = regexp.MustCompile(`^(\d{1,3}\.){3}\d{1,3}$`)
	awsIdentRe   = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)
	headerNameRe = regexp.MustCompile(`^[!#$%&'*+.^_` + "`" + `|~0-9A-Za-z-]+$`)
	sshKeyRe     = regexp.MustCompile(`^(ssh-|ecdsa-|sk-)[A-Za-z0-9@._+-]+\s+[A-Za-z0-9+/=]+(?:\s+.*)?$`)
	sizeRe       = regexp.MustCompile(`^[1-9][0-9]*(?:[KMGT]i?B)?$`)
	imageAliasRe = regexp.MustCompile(`^[A-Za-z0-9_.:/-]{1,120}$`)
)

// ValidateName validates a VM or Credential slug (max 63 chars).
func ValidateName(value, field string) (string, error) {
	if !nameRe.MatchString(value) {
		return "", jerr.Validation(
			fmt.Sprintf("'%s' must be a lowercase slug (letters, digits, hyphens; starting with a letter or digit; max 63 characters)", field),
			map[string]string{field: "invalid slug"},
		)
	}
	return value, nil
}

// ValidateRuleName validates a Rule slug without length cap.
func ValidateRuleName(value string) (string, error) {
	if !ruleNameRe.MatchString(value) {
		return "", jerr.Validation(
			"'name' must be a lowercase slug (letters, digits, hyphens; starting with a letter or digit)",
			map[string]string{"name": "invalid slug"},
		)
	}
	return value, nil
}

// ValidateIPv4 validates an IPv4 address string.
func ValidateIPv4(value, field string) (string, error) {
	if !ipv4Re.MatchString(value) {
		return "", jerr.Validation(fmt.Sprintf("'%s' must be a valid IPv4 address", field), map[string]string{field: "invalid IPv4 address"})
	}
	for _, p := range strings.Split(value, ".") {
		n, err := strconv.Atoi(p)
		if err != nil || n < 0 || n > 255 {
			return "", jerr.Validation(fmt.Sprintf("'%s' must be a valid IPv4 address", field), map[string]string{field: "invalid IPv4 address"})
		}
	}
	return value, nil
}

// ValidateHostname normalizes and validates exact, *.wildcard, or @aws hostnames.
func ValidateHostname(value, field string) (string, error) {
	if value == "@aws" {
		return value, nil
	}
	if value == "" {
		return "", jerr.Validation(fmt.Sprintf("'%s' is required", field), map[string]string{field: "required"})
	}
	if strings.Contains(value, "://") || strings.Contains(value, "/") {
		return "", jerr.Validation(fmt.Sprintf("'%s' must be a hostname (no scheme or path)", field), map[string]string{field: "must be a bare hostname"})
	}
	normalized := strings.ToLower(value)
	normalized = strings.TrimSuffix(normalized, ".")
	host, port, hasColon := strings.Cut(normalized, ":")
	if hasColon && port != "" {
		return "", jerr.Validation(fmt.Sprintf("'%s' must not include a port", field), map[string]string{field: "must not include a port"})
	}
	if hasColon {
		return "", jerr.Validation(fmt.Sprintf("'%s' is not a valid hostname", field), map[string]string{field: "not a valid hostname"})
	}
	bareHost := strings.TrimPrefix(host, "*.")
	if !hostnameRe.MatchString(bareHost) {
		return "", jerr.Validation(fmt.Sprintf("'%s' is not a valid hostname", field), map[string]string{field: "not a valid hostname"})
	}
	return host, nil
}

// IsSafeAbsolutePath rejects dot-segments, percent-encoding, and duplicate slashes.
func IsSafeAbsolutePath(value string) bool {
	if !strings.HasPrefix(value, "/") {
		return false
	}
	if strings.Contains(value, "%") {
		return false
	}
	decoded, err := url.PathUnescape(value)
	if err != nil || decoded != value {
		return false
	}
	segments := strings.Split(value, "/")
	for _, seg := range segments {
		if seg == "." || seg == ".." {
			return false
		}
	}
	if len(segments) > 2 {
		for _, seg := range segments[1 : len(segments)-1] {
			if seg == "" {
				return false
			}
		}
	}
	return true
}

// NormalizePath strips a trailing slash except on "/".
func NormalizePath(value string) string {
	if value != "/" && strings.HasSuffix(value, "/") {
		return strings.TrimSuffix(value, "/")
	}
	return value
}

// ValidatePathPrefix validates and normalizes a Rule path_prefix.
func ValidatePathPrefix(value, field string) (string, error) {
	if !IsSafeAbsolutePath(value) {
		return "", jerr.Validation(
			fmt.Sprintf("'%s' must be a safe absolute path (no '.'/'..' segments, no percent-encoding, no empty segments)", field),
			map[string]string{field: "must be a safe absolute path"},
		)
	}
	return NormalizePath(value), nil
}

// ValidateSSHKey validates a single-line SSH public key.
func ValidateSSHKey(value string) (string, error) {
	key := strings.TrimSpace(value)
	if strings.ContainsAny(key, "\r\n") || !sshKeyRe.MatchString(key) {
		return "", jerr.Validation("Provide one valid SSH public key on a single line", map[string]string{"ssh_public_key": "invalid SSH key"})
	}
	return key, nil
}

// ValidateSize validates memory/disk strings like 8GiB.
func ValidateSize(value, field string) (string, error) {
	if !sizeRe.MatchString(value) {
		return "", jerr.Validation(fmt.Sprintf("%s must be a positive size such as 8GiB", field), map[string]string{field: "invalid size"})
	}
	return value, nil
}

// ValidateImage validates ubuntu:<release> image strings.
func ValidateImage(value string) (string, error) {
	remote, alias, found := strings.Cut(value, ":")
	if remote != "ubuntu" || !found || alias == "" || !imageAliasRe.MatchString(alias) {
		return "", jerr.Validation("image must use the ubuntu:<release> form, such as ubuntu:24.04", map[string]string{"image": "unsupported image alias"})
	}
	return alias, nil
}

// ValidateAWSIdentifier validates service/region identifiers in AWS SigV4 injection.
func ValidateAWSIdentifier(value, key string) error {
	if !awsIdentRe.MatchString(value) {
		return jerr.Validation(
			fmt.Sprintf("'action.injection.%s' must be a lowercase AWS signing identifier", key),
			map[string]string{"action": fmt.Sprintf("invalid AWS signing %s", key)},
		)
	}
	return nil
}

// ValidateHeaderName validates a custom HTTP header name.
func ValidateHeaderName(name string) error {
	if name == "" || !headerNameRe.MatchString(name) {
		return jerr.Validation("Custom header injection requires a valid HTTP header name", map[string]string{"action": "invalid custom header name or fields"})
	}
	return nil
}

// ValidatePort validates a TCP port within [minPort, 65535].
func ValidatePort(port int, field string, minPort int) error {
	if port < minPort || port > 65535 {
		return jerr.Validation(fmt.Sprintf("'%s' must be a port between %d and 65535", field, minPort), map[string]string{field: "invalid port"})
	}
	return nil
}
