// Package redaction implements allowlist-based header redaction for request logs.
package redaction

import (
	"fmt"
	"strings"
)

var allowlistedHeaders = map[string]struct{}{
	"user-agent":      {},
	"accept":          {},
	"accept-encoding": {},
	"accept-language": {},
	"content-type":    {},
	"content-length":  {},
	"host":            {},
}

// HeaderPair represents a raw HTTP header name/value pair.
type HeaderPair struct {
	Name  string
	Value string
}

// LoggedHeader represents a redacted header entry stored in requests.sqlite.
type LoggedHeader struct {
	Name            string  `json:"name"`
	Value           string  `json:"value"`
	Redacted        bool    `json:"redacted"`
	RedactionReason *string `json:"redaction_reason"`
}

// RedactHeaders renders raw header pairs into the redacted log representation.
func RedactHeaders(headers []HeaderPair, injectedHeaderName, credentialName string) []LoggedHeader {
	result := make([]LoggedHeader, 0, len(headers))
	for _, h := range headers {
		lower := strings.ToLower(h.Name)
		if injectedHeaderName != "" && lower == strings.ToLower(injectedHeaderName) {
			reason := "injected"
			result = append(result, LoggedHeader{
				Name:            h.Name,
				Value:           fmt.Sprintf("[REDACTED · injected by %s]", credentialName),
				Redacted:        true,
				RedactionReason: &reason,
			})
			continue
		}
		if _, ok := allowlistedHeaders[lower]; ok {
			result = append(result, LoggedHeader{
				Name:            h.Name,
				Value:           h.Value,
				Redacted:        false,
				RedactionReason: nil,
			})
			continue
		}
		reason := "not_allowlisted"
		result = append(result, LoggedHeader{
			Name:            h.Name,
			Value:           "[REDACTED]",
			Redacted:        true,
			RedactionReason: &reason,
		})
	}
	return result
}
