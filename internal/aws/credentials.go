package aws

import (
	"encoding/json"
	"errors"
	"time"
)

// ProcessCredentials holds validated AWS process-format credentials.
type ProcessCredentials struct {
	Version         int     `json:"Version"`
	AccessKeyID     string  `json:"AccessKeyId"`
	SecretAccessKey string  `json:"SecretAccessKey"`
	SessionToken    *string `json:"SessionToken,omitempty"`
	Expiration      *string `json:"Expiration,omitempty"`
}

func isValidCredentialField(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < 0x21 || s[i] == 0x7F {
			return false
		}
	}
	return true
}

// ParseCredentials validates AWS process-format JSON and returns its refresh deadline (30s early).
func ParseCredentials(value string) (ProcessCredentials, *time.Time, error) {
	var raw map[string]any
	if err := json.Unmarshal([]byte(value), &raw); err != nil || raw == nil {
		return ProcessCredentials{}, nil, errors.New("AWS credentials must be a JSON object")
	}
	verVal, ok := raw["Version"].(float64)
	if !ok || verVal != 1 {
		return ProcessCredentials{}, nil, errors.New("AWS credentials require Version 1")
	}
	ak, _ := raw["AccessKeyId"].(string)
	sk, _ := raw["SecretAccessKey"].(string)
	if !isValidCredentialField(ak) || !isValidCredentialField(sk) {
		return ProcessCredentials{}, nil, errors.New("AWS credentials contain an invalid credential field")
	}
	var sessionPtr *string
	if stVal, exists := raw["SessionToken"]; exists && stVal != nil {
		st, ok := stVal.(string)
		if !ok || !isValidCredentialField(st) {
			return ProcessCredentials{}, nil, errors.New("AWS credentials contain an invalid credential field")
		}
		sessionPtr = &st
	}

	var deadlinePtr *time.Time
	var expPtr *string
	if expVal, exists := raw["Expiration"]; exists {
		expStr, ok := expVal.(string)
		if !ok {
			return ProcessCredentials{}, nil, errors.New("AWS credentials contain an invalid expiration")
		}
		expTime, err := parseISOWithTZ(expStr)
		if err != nil {
			return ProcessCredentials{}, nil, errors.New("AWS credentials contain an invalid expiration")
		}
		deadline := expTime.Add(-30 * time.Second)
		if !deadline.After(time.Now()) {
			return ProcessCredentials{}, nil, errors.New("AWS credentials have expired or are about to expire")
		}
		deadlinePtr = &deadline
		expPtr = &expStr
	}

	return ProcessCredentials{
		Version:         1,
		AccessKeyID:     ak,
		SecretAccessKey: sk,
		SessionToken:    sessionPtr,
		Expiration:      expPtr,
	}, deadlinePtr, nil
}

func parseISOWithTZ(s string) (time.Time, error) {
	for _, layout := range []string{
		time.RFC3339Nano,
		time.RFC3339,
		"2006-01-02T15:04:05.999999999-07:00",
		"2006-01-02T15:04:05-07:00",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, errors.New("invalid timestamp")
}
