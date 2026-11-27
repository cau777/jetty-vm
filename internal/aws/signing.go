package aws

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	awssdk "github.com/aws/aws-sdk-go-v2/aws"
	v4 "github.com/aws/aws-sdk-go-v2/aws/signer/v4"
)

// ErrUnsupportedRequest indicates a request cannot safely be re-signed.
type ErrUnsupportedRequest struct {
	Message string
}

func (e ErrUnsupportedRequest) Error() string { return e.Message }

// ErrUnknownEndpoint indicates an auto-scoped rule matched a non-SigV4 host.
type ErrUnknownEndpoint struct {
	Message string
}

func (e ErrUnknownEndpoint) Error() string { return e.Message }

// InjectionConfig describes an aws_sigv4 injection setting.
type InjectionConfig struct {
	Service string
	Region  string
}

// HeaderEdit is a header name/value pair.
type HeaderEdit struct {
	Name  string
	Value string
}

// SignInput carries the metadata from RequestInfo needed for SigV4 signing.
type SignInput struct {
	Scheme     string
	Method     string
	Hostname   string
	Port       uint32
	HostHeader string
	RawPath    string
	Headers    []HeaderEdit
	BodySHA256 []byte
	HasBody    bool
}

// SignResult returns header edits for the gateway agent.
type SignResult struct {
	RemoveHeaders []string
	SetHeaders    []HeaderEdit
}

// ResolveInjection resolves "auto" service/region against the AWS endpoint catalog.
func ResolveInjection(hostname string, cfg InjectionConfig) (InjectionConfig, error) {
	if cfg.Service != "auto" && cfg.Region != "auto" {
		return cfg, nil
	}
	scope, ok := SigningScope(hostname)
	if !ok {
		return cfg, ErrUnknownEndpoint{Message: "Automatic AWS signing requires a known AWS endpoint"}
	}
	if cfg.Region != "auto" && cfg.Region != scope.Region {
		return cfg, ErrUnsupportedRequest{Message: "AWS endpoint is outside the configured signing region"}
	}
	if cfg.Service != "auto" && cfg.Service != scope.Service {
		return cfg, ErrUnsupportedRequest{Message: "AWS endpoint does not match the configured signing service"}
	}
	return InjectionConfig{Service: scope.Service, Region: scope.Region}, nil
}

// ValidateRequest mirrors Python aws_signing.validate_request().
func ValidateRequest(in SignInput) error {
	if in.Scheme != "https" {
		return ErrUnsupportedRequest{Message: "AWS signing requires HTTPS to the rule's hostname"}
	}
	port := in.Port
	if port == 0 {
		port = 443
	}
	expected := in.Hostname
	if port != 443 {
		expected = fmt.Sprintf("%s:%d", in.Hostname, port)
	}
	if in.HostHeader != "" {
		if strings.TrimRight(strings.ToLower(in.HostHeader), ".") != strings.TrimRight(strings.ToLower(expected), ".") {
			return ErrUnsupportedRequest{Message: "AWS request authority does not match its TLS hostname"}
		}
	}
	if !in.HasBody {
		return ErrUnsupportedRequest{Message: "AWS signing requires a buffered request body"}
	}
	for _, h := range in.Headers {
		lower := strings.ToLower(h.Name)
		switch lower {
		case "x-amz-content-sha256":
			if strings.HasPrefix(h.Value, "STREAMING-") {
				return ErrUnsupportedRequest{Message: "AWS streaming uploads are not supported"}
			}
		case "content-encoding":
			if strings.Contains(strings.ToLower(h.Value), "aws-chunked") {
				return ErrUnsupportedRequest{Message: "AWS streaming uploads are not supported"}
			}
		case "x-amz-region-set":
			return ErrUnsupportedRequest{Message: "AWS SigV4a is not supported"}
		case "authorization":
			if strings.HasPrefix(h.Value, "AWS4-ECDSA") {
				return ErrUnsupportedRequest{Message: "AWS SigV4a is not supported"}
			}
		}
	}
	if _, rawQuery, ok := strings.Cut(in.RawPath, "?"); ok {
		if params, err := url.ParseQuery(rawQuery); err == nil {
			for k := range params {
				if strings.HasPrefix(strings.ToLower(k), "x-amz-") {
					return ErrUnsupportedRequest{Message: "AWS presigned requests are not supported"}
				}
			}
		}
	}
	return nil
}

// SignRequest computes SigV4 headers using body_sha256 without buffering the body in jettyd.
func SignRequest(in SignInput, cfg InjectionConfig, credsJSON string, signingTime time.Time) (SignResult, error) {
	if err := ValidateRequest(in); err != nil {
		return SignResult{}, err
	}
	resolved, err := ResolveInjection(in.Hostname, cfg)
	if err != nil {
		return SignResult{}, err
	}
	creds, _, err := ParseCredentials(credsJSON)
	if err != nil {
		return SignResult{}, err
	}

	port := in.Port
	if port == 0 {
		port = 443
	}
	authority := in.Hostname
	if port != 443 {
		authority = fmt.Sprintf("%s:%d", in.Hostname, port)
	}

	hasContentSHAHeader := false
	for _, h := range in.Headers {
		if strings.ToLower(h.Name) == "x-amz-content-sha256" {
			hasContentSHAHeader = true
			break
		}
	}
	includePayloadHash := hasContentSHAHeader || resolved.Service == "s3"

	payloadHashHex := hex.EncodeToString(in.BodySHA256)
	if len(in.BodySHA256) == 0 {
		empty := sha256.Sum256(nil)
		payloadHashHex = hex.EncodeToString(empty[:])
	}

	rawPath := in.RawPath
	if rawPath == "" {
		rawPath = "/"
	}
	pathPart, rawQuery, hasQuery := strings.Cut(rawPath, "?")
	reqURL := &url.URL{
		Scheme:   "https",
		Host:     authority,
		Path:     pathPart,
		RawPath:  pathPart,
		RawQuery: rawQuery,
	}
	if !hasQuery {
		reqURL.RawQuery = ""
	}
	// Preserve exact path bytes (including //, .., %20) without Go cleaning them.
	req, err := http.NewRequest(in.Method, reqURL.String(), nil)
	if err != nil {
		return SignResult{}, err
	}
	req.URL = reqURL
	req.Host = authority

	stripSet := map[string]struct{}{
		"authorization":        {},
		"x-amz-security-token": {},
		"x-amz-date":           {},
		"date":                 {},
		"x-amz-content-sha256": {},
		"host":                 {},
	}
	for _, h := range in.Headers {
		if _, drop := stripSet[strings.ToLower(h.Name)]; drop {
			continue
		}
		req.Header.Add(h.Name, h.Value)
	}
	if includePayloadHash {
		req.Header.Set("X-Amz-Content-Sha256", payloadHashHex)
	}

	sessionToken := ""
	if creds.SessionToken != nil {
		sessionToken = *creds.SessionToken
	}
	awsCreds := awssdk.Credentials{
		AccessKeyID:     creds.AccessKeyID,
		SecretAccessKey: creds.SecretAccessKey,
		SessionToken:    sessionToken,
	}
	signer := v4.NewSigner(func(o *v4.SignerOptions) {
		if resolved.Service == "s3" {
			o.DisableURIPathEscaping = true
		}
	})
	if err := signer.SignHTTP(context.Background(), awsCreds, req, payloadHashHex, resolved.Service, resolved.Region, signingTime.UTC()); err != nil {
		return SignResult{}, errors.New("AWS signing failed")
	}

	remove := []string{"Authorization", "X-Amz-Security-Token", "X-Amz-Date", "Date", "X-Amz-Content-SHA256"}
	set := []HeaderEdit{
		{Name: "Host", Value: authority},
	}
	for _, hdrName := range []string{"Authorization", "X-Amz-Date", "X-Amz-Security-Token"} {
		if val := req.Header.Get(hdrName); val != "" {
			set = append(set, HeaderEdit{Name: hdrName, Value: val})
		}
	}
	if includePayloadHash {
		set = append(set, HeaderEdit{Name: "X-Amz-Content-SHA256", Value: payloadHashHex})
	}
	return SignResult{RemoveHeaders: remove, SetHeaders: set}, nil
}
