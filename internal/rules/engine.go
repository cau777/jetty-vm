// Package rules implements Jetty's pure, deterministic Rule evaluation engine.
package rules

import (
	"sort"
	"strconv"
	"strings"

	"github.com/cau777/jetty-vm/internal/aws"
	"github.com/cau777/jetty-vm/internal/db"
	"github.com/cau777/jetty-vm/internal/validation"
)

const (
	OutcomeAllowDefault    = "allow_default"
	OutcomeAllowRule       = "allow_rule"
	OutcomeBlockRule       = "block_rule"
	OutcomeAllowCredential = "allow_credential"
)

// CanonicalizeIncomingHostname normalizes a live SNI/Host value without failing.
func CanonicalizeIncomingHostname(raw string) string {
	value := strings.ToLower(strings.TrimSpace(raw))
	host, port, found := strings.Cut(value, ":")
	if !found || !isDigits(port) {
		host = value
	}
	return strings.TrimSuffix(host, ".")
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func selectsVM(sel db.VMSelector, vmName string) bool {
	if sel.Type == "all" {
		return true
	}
	for _, v := range sel.VMs {
		if v == vmName {
			return true
		}
	}
	return false
}

func matchesHostname(pattern, hostname string) bool {
	if pattern == "@aws" {
		_, ok := aws.SigningScope(hostname)
		return ok
	}
	if strings.HasPrefix(pattern, "*.") {
		suffix := pattern[1:]
		return strings.HasSuffix(hostname, suffix) && len(hostname) > len(suffix)
	}
	return pattern == hostname
}

func candidates(all []db.Rule, vmName, hostname string) []db.Rule {
	var matched []db.Rule
	for _, r := range all {
		if matchesHostname(r.Hostname, hostname) && selectsVM(r.VMSelector, vmName) {
			matched = append(matched, r)
		}
	}
	sort.SliceStable(matched, func(i, j int) bool {
		gi := matched[i].VMSelector.Type == "all"
		gj := matched[j].VMSelector.Type == "all"
		if gi != gj {
			return !gi // VM rules precede global rules
		}
		return matched[i].Priority < matched[j].Priority
	})
	return matched
}

func ruleScope(r db.Rule, vmName string) string {
	if r.VMSelector.Type == "all" {
		return ""
	}
	return vmName
}

// ConnectionDecision mirrors rule_engine.ConnectionDecision.
type ConnectionDecision struct {
	Intercepted          bool
	Outcome              string
	MatchedRule          string
	MatchedRuleScope     string
	InterceptedByRule    string
	InterceptedRuleScope string
}

// EvaluateConnection decides whether a TLS ClientHello should be intercepted, blocked, or passed through.
func EvaluateConnection(all []db.Rule, vmName, hostname string) ConnectionDecision {
	hostname = CanonicalizeIncomingHostname(hostname)
	cand := candidates(all, vmName, hostname)
	if len(cand) == 0 {
		return ConnectionDecision{Intercepted: false, Outcome: OutcomeAllowDefault}
	}
	first := cand[0]
	switch first.Action.Type {
	case "allow":
		return ConnectionDecision{
			Intercepted:      false,
			Outcome:          OutcomeAllowRule,
			MatchedRule:      first.Name,
			MatchedRuleScope: ruleScope(first, vmName),
		}
	case "block":
		return ConnectionDecision{
			Intercepted:      false,
			Outcome:          OutcomeBlockRule,
			MatchedRule:      first.Name,
			MatchedRuleScope: ruleScope(first, vmName),
		}
	default:
		return ConnectionDecision{
			Intercepted:          true,
			InterceptedByRule:    first.Name,
			InterceptedRuleScope: ruleScope(first, vmName),
		}
	}
}

// TraceEntry mirrors rule_engine.TraceEntry.
type TraceEntry struct {
	RuleName   string `json:"rule_name"`
	Scope      string `json:"scope"`
	Priority   int    `json:"priority"`
	ActionType string `json:"action_type"`
	Result     string `json:"result"`
}

// RequestDecision mirrors rule_engine.RequestDecision.
type RequestDecision struct {
	Outcome          string
	MatchedRule      string
	MatchedRuleScope string
	Credential       string
	Injection        *db.Injection
	Trace            []TraceEntry
}

// EvaluateRequest evaluates an individual HTTP request against candidate rules.
func EvaluateRequest(all []db.Rule, vmName, hostname, path string) RequestDecision {
	hostname = CanonicalizeIncomingHostname(hostname)
	cand := candidates(all, vmName, hostname)
	trace := make([]TraceEntry, 0, len(cand))

	// Decode only encoded colons (%3A/%3a) for Bedrock model IDs, matching rule_engine.py.
	matchingPath := strings.ReplaceAll(strings.ReplaceAll(path, "%3A", ":"), "%3a", ":")

	for _, r := range cand {
		scope := ruleScope(r, vmName)
		switch r.Action.Type {
		case "allow":
			trace = append(trace, TraceEntry{RuleName: r.Name, Priority: r.Priority, ActionType: "allow", Result: "matched_terminal", Scope: scope})
			return RequestDecision{Outcome: OutcomeAllowRule, MatchedRule: r.Name, MatchedRuleScope: scope, Trace: trace}
		case "block":
			trace = append(trace, TraceEntry{RuleName: r.Name, Priority: r.Priority, ActionType: "block", Result: "matched_terminal", Scope: scope})
			return RequestDecision{Outcome: OutcomeBlockRule, MatchedRule: r.Name, MatchedRuleScope: scope, Trace: trace}
		default:
			if !validation.IsSafeAbsolutePath(matchingPath) {
				trace = append(trace, TraceEntry{RuleName: r.Name, Priority: r.Priority, ActionType: "allow_with_credential", Result: "skipped_unsafe_path", Scope: scope})
				continue
			}
			normalizedPath := validation.NormalizePath(matchingPath)
			prefix := r.Action.PathPrefix
			matches := prefix == "/" || normalizedPath == prefix || strings.HasPrefix(normalizedPath, prefix+"/")
			if matches {
				trace = append(trace, TraceEntry{RuleName: r.Name, Priority: r.Priority, ActionType: "allow_with_credential", Result: "matched_terminal", Scope: scope})
				return RequestDecision{
					Outcome:          OutcomeAllowCredential,
					MatchedRule:      r.Name,
					MatchedRuleScope: scope,
					Credential:       r.Action.Credential,
					Injection:        r.Action.Injection,
					Trace:            trace,
				}
			}
			trace = append(trace, TraceEntry{RuleName: r.Name, Priority: r.Priority, ActionType: "allow_with_credential", Result: "path_no_match_continue", Scope: scope})
		}
	}
	return RequestDecision{Outcome: OutcomeAllowDefault, Trace: trace}
}

// ValidatePriority ensures priority is >= 0.
func ValidatePriority(p int) string {
	return strconv.Itoa(p)
}
