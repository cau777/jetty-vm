// Package requestlog persists connection and HTTP request records in requests.sqlite.
package requestlog

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"

	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/internal/redaction"
	"github.com/cau777/jetty-vm/internal/rules"
)

const (
	DefaultRetentionRows = 100_000
	DefaultPageSize      = 50
	MaxPageSize          = 200
)

// RuleSnapshot summarizes the rule stored in JSON log columns.
type RuleSnapshot struct {
	Name       string `json:"name"`
	Scope      string `json:"scope"`
	Priority   int    `json:"priority"`
	ActionType string `json:"action_type"`
}

// Store manages fail-open writes and query reads against requests.sqlite.
type Store struct {
	conn          *sql.DB
	retentionRows int
}

// NewStore creates a Store with DefaultRetentionRows when retention <= 0.
func NewStore(conn *sql.DB, retention int) *Store {
	if retention <= 0 {
		retention = DefaultRetentionRows
	}
	return &Store{conn: conn, retentionRows: retention}
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func marshalNullable(v any) any {
	if v == nil {
		return nil
	}
	b, err := json.Marshal(v)
	if err != nil {
		return nil
	}
	return string(b)
}

// LogConnection inserts a connection row (fail-open: returns 0 on error).
func (s *Store) LogConnection(
	startedAt, vmName, destIP string,
	destPort int,
	destHost *string,
	sniPresent, echPresent bool,
	durationMS *int64,
	intercepted bool,
	outcome *string,
	matchedRule, interceptedByRule *RuleSnapshot,
) int64 {
	var hostArg, durArg, outArg any
	if destHost != nil {
		hostArg = *destHost
	}
	if durationMS != nil {
		durArg = *durationMS
	}
	if outcome != nil {
		outArg = *outcome
	}
	var matchedArg, interArg any
	if matchedRule != nil {
		matchedArg = marshalNullable(matchedRule)
	}
	if interceptedByRule != nil {
		interArg = marshalNullable(interceptedByRule)
	}

	res, err := s.conn.Exec(
		`INSERT INTO connections (
			started_at, vm_name, destination_ip, destination_port,
			destination_hostname, sni_present, ech_present, duration_ms,
			intercepted, outcome, matched_rule_json, intercepted_by_rule_json
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		startedAt, vmName, destIP, destPort, hostArg,
		boolToInt(sniPresent), boolToInt(echPresent), durArg,
		boolToInt(intercepted), outArg, matchedArg, interArg,
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "log write failed: %v\n", err)
		return 0
	}
	id, _ := res.LastInsertId()
	if _, err := s.conn.Exec(
		`DELETE FROM connections WHERE id <= (SELECT COALESCE(MAX(id), 0) FROM connections) - ?`,
		s.retentionRows,
	); err != nil {
		fmt.Fprintf(os.Stderr, "log prune failed: %v\n", err)
	}
	return id
}

// UpdateConnectionDuration updates duration_ms for a closed connection (fail-open).
func (s *Store) UpdateConnectionDuration(connectionID, durationMS int64) {
	if connectionID <= 0 {
		return
	}
	if _, err := s.conn.Exec(`UPDATE connections SET duration_ms = ? WHERE id = ?`, durationMS, connectionID); err != nil {
		fmt.Fprintf(os.Stderr, "log duration update failed: %v\n", err)
	}
}

// LogHTTPRequest inserts an http_requests row (fail-open: returns 0 on error).
func (s *Store) LogHTTPRequest(
	connectionID int64,
	occurredAt, method, path string,
	queryKeys []string,
	status *int32,
	statusOrigin *string,
	latencyMS *int64,
	outcome string,
	matchedRule *RuleSnapshot,
	matchedCredential *string,
	trace []rules.TraceEntry,
	headers []redaction.LoggedHeader,
) int64 {
	if queryKeys == nil {
		queryKeys = []string{}
	}
	if trace == nil {
		trace = []rules.TraceEntry{}
	}
	if headers == nil {
		headers = []redaction.LoggedHeader{}
	}
	var stArg, origArg, latArg, matchedArg, credArg any
	if status != nil {
		stArg = *status
	}
	if statusOrigin != nil {
		origArg = *statusOrigin
	}
	if latencyMS != nil {
		latArg = *latencyMS
	}
	if matchedRule != nil {
		matchedArg = marshalNullable(matchedRule)
	}
	if matchedCredential != nil {
		credArg = *matchedCredential
	}
	qkBytes, _ := json.Marshal(queryKeys)
	trBytes, _ := json.Marshal(trace)
	hdBytes, _ := json.Marshal(headers)

	res, err := s.conn.Exec(
		`INSERT INTO http_requests (
			connection_id, occurred_at, method, path, query_keys_json,
			status, status_origin, latency_ms, outcome, matched_rule_json,
			matched_credential, trace_json, headers_json
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		connectionID, occurredAt, method, path, string(qkBytes),
		stArg, origArg, latArg, outcome, matchedArg, credArg, string(trBytes), string(hdBytes),
	)
	if err != nil {
		fmt.Fprintf(os.Stderr, "log write failed: %v\n", err)
		return 0
	}
	id, _ := res.LastInsertId()
	return id
}

// UpdateHTTPRequestResult records upstream status and latency after completion.
func (s *Store) UpdateHTTPRequestResult(reqID int64, status *int32, statusOrigin *string, latencyMS *int64) {
	if reqID <= 0 {
		return
	}
	var stArg, origArg, latArg any
	if status != nil {
		stArg = *status
	}
	if statusOrigin != nil {
		origArg = *statusOrigin
	}
	if latencyMS != nil {
		latArg = *latencyMS
	}
	if _, err := s.conn.Exec(
		`UPDATE http_requests SET status = ?, status_origin = ?, latency_ms = ? WHERE id = ?`,
		stArg, origArg, latArg, reqID,
	); err != nil {
		fmt.Fprintf(os.Stderr, "log request update failed: %v\n", err)
	}
}

// ListConnections queries connections matching the supplied filters.
func (s *Store) ListConnections(req *jettyv1.ListRequestsRequest) ([]*jettyv1.ConnectionLog, error) {
	limit := int(req.GetLimit())
	if limit <= 0 {
		limit = DefaultPageSize
	}
	if limit > MaxPageSize {
		limit = MaxPageSize
	}
	var clauses []string
	var params []any
	if req.GetBeforeId() > 0 {
		clauses = append(clauses, "c.id < ?")
		params = append(params, req.GetBeforeId())
	}
	if req.GetAfterId() > 0 {
		clauses = append(clauses, "c.id > ?")
		params = append(params, req.GetAfterId())
	}
	if req.GetVmName() != "" {
		clauses = append(clauses, "c.vm_name = ?")
		params = append(params, req.GetVmName())
	}
	if req.GetHost() != "" {
		clauses = append(clauses, "c.destination_hostname = ?")
		params = append(params, req.GetHost())
	}
	if req.GetSince() != "" {
		clauses = append(clauses, "c.started_at >= ?")
		params = append(params, req.GetSince())
	}
	if req.GetUntil() != "" {
		clauses = append(clauses, "c.started_at <= ?")
		params = append(params, req.GetUntil())
	}
	if req.GetDecision() != "" {
		clauses = append(clauses, "(c.outcome = ? OR EXISTS (SELECT 1 FROM http_requests r WHERE r.connection_id = c.id AND r.outcome = ?))")
		params = append(params, req.GetDecision(), req.GetDecision())
	}
	if req.GetStatus() > 0 {
		clauses = append(clauses, "EXISTS (SELECT 1 FROM http_requests r WHERE r.connection_id = c.id AND r.status = ?)")
		params = append(params, req.GetStatus())
	}
	where := ""
	if len(clauses) > 0 {
		where = "WHERE " + strings.Join(clauses, " AND ")
	}
	params = append(params, limit)
	rows, err := s.conn.Query(
		`SELECT id, started_at, vm_name, destination_ip, destination_port, destination_hostname,
		        sni_present, ech_present, duration_ms, intercepted, outcome, matched_rule_json, intercepted_by_rule_json
		 FROM connections c `+where+` ORDER BY c.id DESC LIMIT ?`,
		params...,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []*jettyv1.ConnectionLog
	for rows.Next() {
		c, err := scanConnection(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}

// GetConnection returns a connection and all its http_requests, or nil if not found.
func (s *Store) GetConnection(id int64) (*jettyv1.RequestDetail, error) {
	row := s.conn.QueryRow(
		`SELECT id, started_at, vm_name, destination_ip, destination_port, destination_hostname,
		        sni_present, ech_present, duration_ms, intercepted, outcome, matched_rule_json, intercepted_by_rule_json
		 FROM connections WHERE id = ?`, id,
	)
	connLog, err := scanConnection(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	reqRows, err := s.conn.Query(
		`SELECT id, connection_id, occurred_at, method, path, query_keys_json,
		        status, status_origin, latency_ms, outcome, matched_rule_json,
		        matched_credential, trace_json, headers_json
		 FROM http_requests WHERE connection_id = ? ORDER BY id ASC`, id,
	)
	if err != nil {
		return nil, err
	}
	defer reqRows.Close()
	var reqs []*jettyv1.HttpRequestLog
	for reqRows.Next() {
		r, err := scanHTTPRequest(reqRows)
		if err != nil {
			return nil, err
		}
		reqs = append(reqs, r)
	}
	if err := reqRows.Err(); err != nil {
		return nil, err
	}
	return &jettyv1.RequestDetail{Connection: connLog, HttpRequests: reqs}, nil
}

// GetHTTPRequest returns a single http_request row by ID.
func (s *Store) GetHTTPRequest(id int64) (*jettyv1.HttpRequestLog, error) {
	row := s.conn.QueryRow(
		`SELECT id, connection_id, occurred_at, method, path, query_keys_json,
		        status, status_origin, latency_ms, outcome, matched_rule_json,
		        matched_credential, trace_json, headers_json
		 FROM http_requests WHERE id = ?`, id,
	)
	r, err := scanHTTPRequest(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

func scanConnection(scanner interface{ Scan(dest ...any) error }) (*jettyv1.ConnectionLog, error) {
	var c jettyv1.ConnectionLog
	var destHost, outcome, matchedJSON, interJSON sql.NullString
	var durMS sql.NullInt64
	var sniPres, echPres, inter int
	var destPort int32
	if err := scanner.Scan(
		&c.Id, &c.StartedAt, &c.VmName, &c.DestinationIp, &destPort,
		&destHost, &sniPres, &echPres, &durMS, &inter, &outcome, &matchedJSON, &interJSON,
	); err != nil {
		return nil, err
	}
	c.DestinationPort = destPort
	c.DestinationHostname = destHost.String
	c.SniPresent = sniPres != 0
	c.EchPresent = echPres != 0
	if durMS.Valid {
		c.DurationMs = durMS.Int64
		c.HasDurationMs = true
	}
	c.Intercepted = inter != 0
	c.Outcome = outcome.String
	c.MatchedRule = parseRuleSnapshot(matchedJSON)
	c.InterceptedByRule = parseRuleSnapshot(interJSON)
	return &c, nil
}

func scanHTTPRequest(scanner interface{ Scan(dest ...any) error }) (*jettyv1.HttpRequestLog, error) {
	var r jettyv1.HttpRequestLog
	var qkJSON, traceJSON, headersJSON string
	var status sql.NullInt32
	var statusOrig, matchedJSON, matchedCred sql.NullString
	var latencyMS sql.NullInt64
	if err := scanner.Scan(
		&r.Id, &r.ConnectionId, &r.OccurredAt, &r.Method, &r.Path, &qkJSON,
		&status, &statusOrig, &latencyMS, &r.Outcome, &matchedJSON, &matchedCred,
		&traceJSON, &headersJSON,
	); err != nil {
		return nil, err
	}
	_ = json.Unmarshal([]byte(qkJSON), &r.QueryKeys)
	if status.Valid {
		r.Status = status.Int32
		r.HasStatus = true
	}
	r.StatusOrigin = statusOrig.String
	if latencyMS.Valid {
		r.LatencyMs = latencyMS.Int64
		r.HasLatencyMs = true
	}
	r.MatchedRule = parseRuleSnapshot(matchedJSON)
	r.MatchedCredential = matchedCred.String

	var traceEntries []rules.TraceEntry
	if err := json.Unmarshal([]byte(traceJSON), &traceEntries); err == nil {
		for _, t := range traceEntries {
			r.Trace = append(r.Trace, &jettyv1.TraceEntry{
				RuleName:   t.RuleName,
				Scope:      t.Scope,
				Priority:   int32(t.Priority),
				ActionType: t.ActionType,
				Result:     t.Result,
			})
		}
	}

	var loggedHeaders []redaction.LoggedHeader
	if err := json.Unmarshal([]byte(headersJSON), &loggedHeaders); err == nil {
		for _, h := range loggedHeaders {
			reason := ""
			if h.RedactionReason != nil {
				reason = *h.RedactionReason
			}
			r.Headers = append(r.Headers, &jettyv1.LoggedHeader{
				Name:            h.Name,
				Value:           h.Value,
				Redacted:        h.Redacted,
				RedactionReason: reason,
			})
		}
	}
	return &r, nil
}

func parseRuleSnapshot(ns sql.NullString) *jettyv1.RuleSnapshot {
	if !ns.Valid || ns.String == "" {
		return nil
	}
	var snap RuleSnapshot
	if err := json.Unmarshal([]byte(ns.String), &snap); err != nil {
		return nil
	}
	return &jettyv1.RuleSnapshot{
		Name:       snap.Name,
		Scope:      snap.Scope,
		Priority:   int32(snap.Priority),
		ActionType: snap.ActionType,
	}
}
