package rules

import (
	"testing"

	"github.com/cau777/jetty-vm/internal/db"
)

func allowRule(name string, prio int, host string, vms ...string) db.Rule {
	sel := db.VMSelector{Type: "all"}
	if len(vms) > 0 {
		sel = db.VMSelector{Type: "only", VMs: vms}
	}
	return db.Rule{Name: name, Priority: prio, Hostname: host, VMSelector: sel, Action: db.RuleAction{Type: "allow"}}
}

func blockRule(name string, prio int, host string, vms ...string) db.Rule {
	sel := db.VMSelector{Type: "all"}
	if len(vms) > 0 {
		sel = db.VMSelector{Type: "only", VMs: vms}
	}
	return db.Rule{Name: name, Priority: prio, Hostname: host, VMSelector: sel, Action: db.RuleAction{Type: "block"}}
}

func awcRule(name string, prio int, host, cred, prefix string, vms ...string) db.Rule {
	sel := db.VMSelector{Type: "all"}
	if len(vms) > 0 {
		sel = db.VMSelector{Type: "only", VMs: vms}
	}
	return db.Rule{
		Name:       name,
		Priority:   prio,
		Hostname:   host,
		VMSelector: sel,
		Action: db.RuleAction{
			Type:       "allow_with_credential",
			Credential: cred,
			PathPrefix: prefix,
			Injection:  &db.Injection{Type: "bearer"},
		},
	}
}

func TestCanonicalizeIncomingHostname(t *testing.T) {
	if got := CanonicalizeIncomingHostname("GitHub.COM.:443"); got != "github.com" {
		t.Fatalf("got %q", got)
	}
	if got := CanonicalizeIncomingHostname("api.github.com"); got != "api.github.com" {
		t.Fatalf("got %q", got)
	}
}

func TestEvaluateConnectionGolden(t *testing.T) {
	if d := EvaluateConnection(nil, "skills-dev", "github.com"); d.Intercepted || d.Outcome != OutcomeAllowDefault {
		t.Fatalf("expected allow_default, got %+v", d)
	}
	if d := EvaluateConnection([]db.Rule{allowRule("r1", 10, "github.com")}, "skills-dev", "GitHub.COM."); d.Intercepted || d.Outcome != OutcomeAllowRule || d.MatchedRule != "r1" {
		t.Fatalf("expected allow_rule r1, got %+v", d)
	}
	if d := EvaluateConnection([]db.Rule{blockRule("r1", 10, "evil.example")}, "skills-dev", "evil.example"); d.Intercepted || d.Outcome != OutcomeBlockRule {
		t.Fatalf("expected block_rule, got %+v", d)
	}
	if d := EvaluateConnection([]db.Rule{awcRule("r1", 10, "api.github.com", "gh", "/repos/cau777")}, "skills-dev", "api.github.com"); !d.Intercepted || d.InterceptedByRule != "r1" {
		t.Fatalf("expected intercepted, got %+v", d)
	}
	if d := EvaluateConnection([]db.Rule{blockRule("later", 20, "github.com"), allowRule("earlier", 10, "github.com")}, "skills-dev", "github.com"); d.Outcome != OutcomeAllowRule || d.MatchedRule != "earlier" {
		t.Fatalf("lowest priority should win, got %+v", d)
	}
}

func TestEvaluateRequestGolden(t *testing.T) {
	rules := []db.Rule{awcRule("r1", 10, "api.github.com", "gh", "/repos/cau777")}
	if d := EvaluateRequest(rules, "skills-dev", "api.github.com", "/repos/cau777/issues"); d.Outcome != OutcomeAllowCredential || d.Credential != "gh" {
		t.Fatalf("expected allow_credential, got %+v", d)
	}
	if d := EvaluateRequest(rules, "skills-dev", "api.github.com", "/repos/cau777x"); d.Outcome != OutcomeAllowDefault {
		t.Fatalf("expected segment-boundary miss -> allow_default, got %+v", d)
	}
	if d := EvaluateRequest(rules, "skills-dev", "api.github.com", "/repos/../secret"); d.Outcome != OutcomeAllowDefault || d.Trace[0].Result != "skipped_unsafe_path" {
		t.Fatalf("expected unsafe path skip, got %+v", d)
	}

	// Bedrock encoded colons
	bedrock := []db.Rule{awcRule("bedrock", 10, "bedrock-runtime.us-east-1.amazonaws.com", "bedrock", "/model/openai.gpt-oss-120b-1:0")}
	for _, ver := range []string{":0", "%3A0", "%3a0"} {
		for _, sfx := range []string{"", "/converse"} {
			d := EvaluateRequest(bedrock, "skills-dev", bedrock[0].Hostname, "/model/openai.gpt-oss-120b-1"+ver+sfx)
			if d.Outcome != OutcomeAllowCredential {
				t.Fatalf("expected match for %s%s, got %+v", ver, sfx, d)
			}
		}
	}
	for _, unsafe := range []string{
		"/model/a%3A0/../secret", "/model/a%3A0//converse",
		"/model/a%3A0%2Fconverse", "/model/%2e%2e/secret",
		"/model/a%253A0/converse", "/model/a%3A0%00/converse",
	} {
		d := EvaluateRequest([]db.Rule{awcRule("b", 10, "bedrock-runtime.us-east-1.amazonaws.com", "b", "/")}, "vm", "bedrock-runtime.us-east-1.amazonaws.com", unsafe)
		if d.Outcome != OutcomeAllowDefault || d.Trace[0].Result != "skipped_unsafe_path" {
			t.Fatalf("unsafe path %q was not skipped: %+v", unsafe, d)
		}
	}

	// VM rules always precede global rules
	scoped := []db.Rule{
		blockRule("fallback", 0, "api.github.com"),
		awcRule("local", 100, "api.github.com", "token", "/private", "alpha"),
	}
	if !EvaluateConnection(scoped, "alpha", "api.github.com").Intercepted {
		t.Fatal("expected alpha intercepted")
	}
	d := EvaluateRequest(scoped, "alpha", "api.github.com", "/public")
	if d.Outcome != OutcomeBlockRule || len(d.Trace) != 2 || d.Trace[0].Scope != "alpha" || d.Trace[1].Scope != "" {
		t.Fatalf("expected fallthrough to global block, got %+v", d)
	}
}

func TestAWSEndpointSelectorAndWildcards(t *testing.T) {
	awsRules := []db.Rule{awcRule("aws", 10, "@aws", "aws", "/", "secure-stack")}
	for _, h := range []string{"iam.amazonaws.com", "ecs.us-east-1.amazonaws.com", "ec2.us-west-2.api.aws", "ec2.cn-north-1.amazonaws.com.cn", "bucket.s3.us-east-1.amazonaws.com"} {
		if !EvaluateConnection(awsRules, "secure-stack", h).Intercepted {
			t.Fatalf("expected %s intercepted", h)
		}
		if EvaluateConnection(awsRules, "other", h).Intercepted {
			t.Fatalf("expected other VM not intercepted for %s", h)
		}
	}
	for _, hosted := range []string{
		"app-alb-ed2ac38-1850627321.us-east-1.elb.amazonaws.com",
		"example.us-east-1.compute.amazonaws.com",
		"example.amazonaws.com",
		"iam.amazonaws.com.evil.com",
	} {
		if EvaluateConnection(awsRules, "secure-stack", hosted).Intercepted {
			t.Fatalf("hosted/non-API host %s must not be intercepted", hosted)
		}
	}

	wc := []db.Rule{awcRule("wc", 10, "*.us-east-1.amazonaws.com", "aws", "/")}
	if !EvaluateConnection(wc, "vm", "ecs.us-east-1.amazonaws.com").Intercepted {
		t.Fatal("wildcard should match subdomain")
	}
	if EvaluateConnection(wc, "vm", "us-east-1.amazonaws.com").Intercepted {
		t.Fatal("wildcard must not match apex")
	}
}
