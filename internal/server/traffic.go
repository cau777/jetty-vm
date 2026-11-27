package server

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/url"
	"strconv"
	"strings"

	"connectrpc.com/connect"
	jettyv1 "github.com/cau777/jetty-vm/gen/go/jetty/v1"
	"github.com/cau777/jetty-vm/internal/aws"
	"github.com/cau777/jetty-vm/internal/credentials"
	"github.com/cau777/jetty-vm/internal/db"
	jerr "github.com/cau777/jetty-vm/internal/errors"
	"github.com/cau777/jetty-vm/internal/redaction"
	"github.com/cau777/jetty-vm/internal/requestlog"
	"github.com/cau777/jetty-vm/internal/rules"
)

const (
	blockUnknownVMOutcome      = "block_unknown_vm"
	blockMessage               = "blocked by orca-proxy policy"
	plaintextCredentialMessage = "blocked by orca-proxy: credential rules only apply over TLS"
)

func (s *Server) Hello(_ context.Context, req *connect.Request[jettyv1.GatewayHello]) (*connect.Response[jettyv1.GatewayConfig], error) {
	if req.Msg.GetAgentVersion() != ExpectedAgentVersion {
		repush := s.RepushHook
		if repush == nil && s.lxd != nil {
			repush = s.lxd.RepushGatewayAgent
		}
		if repush != nil {
			go func() {
				_ = repush(context.Background())
			}()
		}
		return nil, jerr.ToConnect(jerr.FailedPrecondition(
			fmt.Sprintf("gateway agent version %q does not match expected %q; auto-upgrade triggered", req.Msg.GetAgentVersion(), ExpectedAgentVersion),
		))
	}
	return connect.NewResponse(&jettyv1.GatewayConfig{
		ExpectedVersion:       ExpectedAgentVersion,
		MaxInterceptBodyBytes: DefaultMaxBodyBytes,
	}), nil
}

func (s *Server) ResolveSource(_ context.Context, req *connect.Request[jettyv1.SourceInfo]) (*connect.Response[jettyv1.SourceDecision], error) {
	ip := req.Msg.GetSourceIp()
	vmName, err := db.VMNameForIP(s.stateDB, ip)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if vmName != "" {
		return connect.NewResponse(&jettyv1.SourceDecision{
			Allowed: true,
			VmName:  vmName,
		}), nil
	}

	label := ip
	if label == "" {
		label = "unknown"
	}
	destIP := req.Msg.GetDestIp()
	if destIP == "" {
		destIP = "0.0.0.0"
	}
	outcome := blockUnknownVMOutcome
	connID := s.logs.LogConnection(
		db.NowISO(), label, destIP, int(req.Msg.GetDestPort()),
		nil, false, false, nil, false, &outcome, nil, nil,
	)
	if connID > 0 {
		if detail, err := s.logs.GetConnection(connID); err == nil && detail != nil {
			s.events.PublishRequest(&jettyv1.RequestEvent{Kind: "connection", Connection: detail.Connection})
			s.events.PublishWatch("connection", strconv.FormatInt(connID, 10))
		}
	}
	return connect.NewResponse(&jettyv1.SourceDecision{
		Allowed:      false,
		ConnectionId: strconv.FormatInt(connID, 10),
		ErrorMessage: "blocked by orca-proxy: source is not a registered VM",
	}), nil
}

func ruleSnapshot(all []db.Rule, name, scope string) *requestlog.RuleSnapshot {
	if name == "" {
		return nil
	}
	for _, r := range all {
		if r.Name == name && r.Scope == scope {
			return &requestlog.RuleSnapshot{
				Name:       r.Name,
				Scope:      r.Scope,
				Priority:   r.Priority,
				ActionType: r.Action.Type,
			}
		}
	}
	return nil
}

func (s *Server) DecideConnection(_ context.Context, req *connect.Request[jettyv1.ConnectionInfo]) (*connect.Response[jettyv1.ConnectionDecision], error) {
	vmName, err := db.VMNameForIP(s.stateDB, req.Msg.GetSourceIp())
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	if vmName == "" {
		return nil, jerr.ToConnect(jerr.FailedPrecondition("source is not a registered VM"))
	}
	allRules, err := db.ListRules(s.stateDB, nil)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	sni := req.Msg.GetSni()
	sniPresent := sni != ""
	matchHost := sni

	decision := rules.EvaluateConnection(allRules, vmName, matchHost)
	destIP := req.Msg.GetDestIp()
	if destIP == "" {
		destIP = "0.0.0.0"
	}
	var destHostPtr *string
	if sniPresent {
		destHostPtr = &sni
	}

	// Per docs/architechture.md, Block can't refuse in tls_clienthello, so we intercept
	// and DecideRequest returns 403 for every request on the connection.
	intercepted := decision.Intercepted || decision.Outcome == rules.OutcomeBlockRule
	var outcomePtr *string
	if decision.Outcome != "" {
		o := decision.Outcome
		outcomePtr = &o
	}
	matchedSnap := ruleSnapshot(allRules, decision.MatchedRule, decision.MatchedRuleScope)
	interSnap := ruleSnapshot(allRules, decision.InterceptedByRule, decision.InterceptedRuleScope)
	if decision.Outcome == rules.OutcomeBlockRule && interSnap == nil {
		interSnap = matchedSnap
	}

	connID := s.logs.LogConnection(
		db.NowISO(), vmName, destIP, int(req.Msg.GetDestPort()),
		destHostPtr, sniPresent, req.Msg.GetEchPresent(), nil,
		intercepted, outcomePtr, matchedSnap, interSnap,
	)
	if connID > 0 {
		s.mu.Lock()
		s.conns[connID] = connContext{vmName: vmName, sni: sni}
		s.mu.Unlock()
		if detail, err := s.logs.GetConnection(connID); err == nil && detail != nil {
			s.events.PublishRequest(&jettyv1.RequestEvent{Kind: "connection", Connection: detail.Connection})
			s.events.PublishWatch("connection", strconv.FormatInt(connID, 10))
		}
	}

	resp := &jettyv1.ConnectionDecision{
		ConnectionId: strconv.FormatInt(connID, 10),
	}
	if intercepted {
		resp.Action = &jettyv1.ConnectionDecision_Intercept{Intercept: &jettyv1.Intercept{}}
	} else {
		resp.Action = &jettyv1.ConnectionDecision_Passthrough{Passthrough: &jettyv1.Passthrough{}}
	}
	return connect.NewResponse(resp), nil
}

func (s *Server) DecideRequest(ctx context.Context, req *connect.Request[jettyv1.RequestInfo]) (*connect.Response[jettyv1.RequestDecision], error) {
	connID, _ := strconv.ParseInt(req.Msg.GetConnectionId(), 10, 64)
	var vmName, sni string
	if connID > 0 {
		s.mu.Lock()
		cc, ok := s.conns[connID]
		s.mu.Unlock()
		if ok {
			vmName = cc.vmName
			sni = cc.sni
		}
	}
	if vmName == "" {
		var err error
		vmName, err = db.VMNameForIP(s.stateDB, req.Msg.GetSourceIp())
		if err != nil {
			return nil, jerr.ToConnect(err)
		}
	}
	if vmName == "" {
		return connect.NewResponse(&jettyv1.RequestDecision{
			Action: &jettyv1.RequestDecision_Respond{
				Respond: &jettyv1.Synthesize{
					StatusCode: 403,
					Body:       []byte("blocked by orca-proxy: source is not a registered VM"),
					Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "text/plain"}},
				},
			},
		}), nil
	}

	plaintext := connID <= 0 || req.Msg.GetScheme() == "http"
	hostname := sni
	if hostname == "" {
		hostname = req.Msg.GetHost()
	}
	rawPath := req.Msg.GetRawPath()
	if rawPath == "" {
		rawPath = req.Msg.GetPath()
	}
	reqPath := req.Msg.GetPath()
	var queryKeys []string
	if parsed, err := url.ParseRequestURI(rawPath); err == nil {
		if reqPath == "" {
			reqPath = parsed.Path
		}
		for k := range parsed.Query() {
			queryKeys = append(queryKeys, k)
		}
	}
	if reqPath == "" {
		reqPath = "/"
	}

	allRules, err := db.ListRules(s.stateDB, nil)
	if err != nil {
		return nil, jerr.ToConnect(err)
	}
	decision := rules.EvaluateRequest(allRules, vmName, hostname, reqPath)

	rawPairs := make([]redaction.HeaderPair, 0, len(req.Msg.GetHeaders()))
	awsHeaders := make([]aws.HeaderEdit, 0, len(req.Msg.GetHeaders()))
	for _, h := range req.Msg.GetHeaders() {
		rawPairs = append(rawPairs, redaction.HeaderPair{Name: h.GetName(), Value: h.GetValue()})
		awsHeaders = append(awsHeaders, aws.HeaderEdit{Name: h.GetName(), Value: h.GetValue()})
	}

	logSync := func(statusCode *int32, statusOrigin *string, outcome string, injectedHdr, credName string) int64 {
		if connID <= 0 {
			return 0
		}
		var credPtr *string
		if credName != "" {
			credPtr = &credName
		}
		redacted := redaction.RedactHeaders(rawPairs, injectedHdr, credName)
		snap := ruleSnapshot(allRules, decision.MatchedRule, decision.MatchedRuleScope)
		id := s.logs.LogHTTPRequest(
			connID, db.NowISO(), req.Msg.GetMethod(), reqPath, queryKeys,
			statusCode, statusOrigin, nil, outcome, snap, credPtr, decision.Trace, redacted,
		)
		if id > 0 {
			if row, err := s.logs.GetHTTPRequest(id); err == nil && row != nil {
				s.events.PublishRequest(&jettyv1.RequestEvent{Kind: "http_request", HttpRequest: row})
				s.events.PublishWatch("http_request", strconv.FormatInt(id, 10))
			}
		}
		return id
	}

	if decision.Outcome == rules.OutcomeBlockRule {
		st := int32(403)
		orig := "proxy"
		id := logSync(&st, &orig, decision.Outcome, "", "")
		return connect.NewResponse(&jettyv1.RequestDecision{
			RequestId: strconv.FormatInt(id, 10),
			Action: &jettyv1.RequestDecision_Respond{
				Respond: &jettyv1.Synthesize{
					StatusCode: 403,
					Body:       []byte(blockMessage),
					Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "text/plain"}},
				},
			},
		}), nil
	}

	if decision.Outcome == rules.OutcomeAllowCredential && plaintext {
		return connect.NewResponse(&jettyv1.RequestDecision{
			Action: &jettyv1.RequestDecision_Respond{
				Respond: &jettyv1.Synthesize{
					StatusCode: 403,
					Body:       []byte(plaintextCredentialMessage),
					Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "text/plain"}},
				},
			},
		}), nil
	}

	if decision.Outcome == rules.OutcomeAllowCredential && decision.Injection != nil {
		inj := *decision.Injection
		if inj.Type == "aws_sigv4" {
			resolved, err := aws.ResolveInjection(hostname, aws.InjectionConfig{Service: inj.Service, Region: inj.Region})
			if err != nil {
				var unknownErr aws.ErrUnknownEndpoint
				if errors.As(err, &unknownErr) {
					decision.Outcome = rules.OutcomeAllowRule
					id := logSync(nil, nil, decision.Outcome, "", "")
					return connect.NewResponse(&jettyv1.RequestDecision{
						RequestId: strconv.FormatInt(id, 10),
						Action:    &jettyv1.RequestDecision_Forward{Forward: &jettyv1.Forward{}},
					}), nil
				}
				st := int32(400)
				orig := "proxy"
				id := logSync(&st, &orig, decision.Outcome, "", "")
				return connect.NewResponse(&jettyv1.RequestDecision{
					RequestId: strconv.FormatInt(id, 10),
					Action: &jettyv1.RequestDecision_Respond{
						Respond: &jettyv1.Synthesize{
							StatusCode: 400,
							Body:       []byte(err.Error()),
							Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "text/plain"}},
						},
					},
				}), nil
			}
			inj.Service = resolved.Service
			inj.Region = resolved.Region
			if err := aws.ValidateRequest(aws.SignInput{
				Scheme:     req.Msg.GetScheme(),
				Method:     req.Msg.GetMethod(),
				Hostname:   hostname,
				Port:       req.Msg.GetDestPort(),
				HostHeader: req.Msg.GetHostHeader(),
				RawPath:    rawPath,
				Headers:    awsHeaders,
				BodySHA256: req.Msg.GetBodySha256(),
				HasBody:    req.Msg.GetHasBody(),
			}); err != nil {
				st := int32(400)
				orig := "proxy"
				id := logSync(&st, &orig, decision.Outcome, "", "")
				return connect.NewResponse(&jettyv1.RequestDecision{
					RequestId: strconv.FormatInt(id, 10),
					Action: &jettyv1.RequestDecision_Respond{
						Respond: &jettyv1.Synthesize{
							StatusCode: 400,
							Body:       []byte(err.Error()),
							Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "text/plain"}},
						},
					},
				}), nil
			}
		}

		credRow, err := db.GetCredential(s.stateDB, decision.Credential)
		if err != nil || credRow == nil {
			st := int32(502)
			orig := "proxy"
			id := logSync(&st, &orig, decision.Outcome, "", "")
			return connect.NewResponse(synthesize502(id)), nil
		}

		outFormat := "header"
		if inj.Type == "aws_sigv4" {
			outFormat = "aws_process"
		}
		val, err := s.creds.GetValue(ctx, credRow.Name, credRow.Command, credRow.TTLSeconds, outFormat)
		if err != nil {
			st := int32(502)
			orig := "proxy"
			id := logSync(&st, &orig, decision.Outcome, "", "")
			return connect.NewResponse(synthesize502(id)), nil
		}

		var forward jettyv1.Forward
		injectedHeader := "Authorization"
		switch inj.Type {
		case "bearer":
			forward.SetHeaders = []*jettyv1.Header{{Name: "Authorization", Value: "Bearer " + val}}
		case "basic":
			enc := base64.StdEncoding.EncodeToString([]byte(inj.Username + ":" + val))
			forward.SetHeaders = []*jettyv1.Header{{Name: "Authorization", Value: "Basic " + enc}}
		case "header":
			injectedHeader = inj.Name
			forward.SetHeaders = []*jettyv1.Header{{Name: inj.Name, Value: val}}
		case "aws_sigv4":
			signRes, err := aws.SignRequest(aws.SignInput{
				Scheme:     req.Msg.GetScheme(),
				Method:     req.Msg.GetMethod(),
				Hostname:   hostname,
				Port:       req.Msg.GetDestPort(),
				HostHeader: req.Msg.GetHostHeader(),
				RawPath:    rawPath,
				Headers:    awsHeaders,
				BodySHA256: req.Msg.GetBodySha256(),
				HasBody:    req.Msg.GetHasBody(),
			}, aws.InjectionConfig{Service: inj.Service, Region: inj.Region}, val, s.Now())
			if err != nil {
				s.creds.Reject(credRow.Name, credentials.Digest(val))
				st := int32(502)
				orig := "proxy"
				id := logSync(&st, &orig, decision.Outcome, "", "")
				return connect.NewResponse(synthesize502(id)), nil
			}
			forward.RemoveHeaders = signRes.RemoveHeaders
			for _, sh := range signRes.SetHeaders {
				forward.SetHeaders = append(forward.SetHeaders, &jettyv1.Header{Name: sh.Name, Value: sh.Value})
			}
		}

		id := logSync(nil, nil, decision.Outcome, injectedHeader, credRow.Name)
		if id > 0 {
			s.mu.Lock()
			s.reqCreds[id] = reqCredentialMeta{credName: credRow.Name, digest: credentials.Digest(val)}
			s.mu.Unlock()
		}
		return connect.NewResponse(&jettyv1.RequestDecision{
			RequestId: strconv.FormatInt(id, 10),
			Action:    &jettyv1.RequestDecision_Forward{Forward: &forward},
		}), nil
	}

	id := logSync(nil, nil, decision.Outcome, "", "")
	return connect.NewResponse(&jettyv1.RequestDecision{
		RequestId: strconv.FormatInt(id, 10),
		Action:    &jettyv1.RequestDecision_Forward{Forward: &jettyv1.Forward{}},
	}), nil
}

func synthesize502(id int64) *jettyv1.RequestDecision {
	return &jettyv1.RequestDecision{
		RequestId: strconv.FormatInt(id, 10),
		Action: &jettyv1.RequestDecision_Respond{
			Respond: &jettyv1.Synthesize{
				StatusCode: 502,
				Body:       []byte(`{"error":{"code":"credential_unavailable","message":"credential command failed"}}`),
				Headers:    []*jettyv1.Header{{Name: "Content-Type", Value: "application/json"}},
			},
		},
	}
}

func (s *Server) Report(_ context.Context, stream *connect.ClientStream[jettyv1.GatewayEvent]) (*connect.Response[jettyv1.Empty], error) {
	for stream.Receive() {
		ev := stream.Msg()
		switch item := ev.Event.(type) {
		case *jettyv1.GatewayEvent_RequestCompleted:
			rc := item.RequestCompleted
			reqID, _ := strconv.ParseInt(rc.GetRequestId(), 10, 64)
			var stPtr *int32
			if rc.GetHasStatus() {
				st := rc.GetStatusCode()
				stPtr = &st
				if st == 401 && reqID > 0 {
					s.mu.Lock()
					meta, ok := s.reqCreds[reqID]
					if ok {
						delete(s.reqCreds, reqID)
					}
					s.mu.Unlock()
					if ok {
						s.creds.Reject(meta.credName, meta.digest)
					}
				}
			}
			var origPtr *string
			if rc.GetStatusOrigin() != "" {
				orig := rc.GetStatusOrigin()
				origPtr = &orig
			}
			var latPtr *int64
			if rc.GetHasLatencyMs() {
				lat := rc.GetLatencyMs()
				latPtr = &lat
			}
			if reqID > 0 {
				s.logs.UpdateHTTPRequestResult(reqID, stPtr, origPtr, latPtr)
				if row, err := s.logs.GetHTTPRequest(reqID); err == nil && row != nil {
					s.events.PublishRequest(&jettyv1.RequestEvent{Kind: "http_request", HttpRequest: row})
					s.events.PublishWatch("http_request", strconv.FormatInt(reqID, 10))
				}
			}
		case *jettyv1.GatewayEvent_ConnectionClosed:
			cc := item.ConnectionClosed
			connID, _ := strconv.ParseInt(cc.GetConnectionId(), 10, 64)
			if connID > 0 {
				s.logs.UpdateConnectionDuration(connID, cc.GetDurationMs())
				s.mu.Lock()
				delete(s.conns, connID)
				s.mu.Unlock()
			}
		}
	}
	if err := stream.Err(); err != nil && !errors.Is(err, io.EOF) && !strings.Contains(err.Error(), "canceled") {
		return nil, err
	}
	return connect.NewResponse(&jettyv1.Empty{}), nil
}
