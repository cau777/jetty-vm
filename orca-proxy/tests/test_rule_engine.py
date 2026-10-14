from orca_proxy.rule_engine import (
    ALLOW_CREDENTIAL,
    ALLOW_DEFAULT,
    ALLOW_RULE,
    BLOCK_RULE,
    canonicalize_incoming_hostname,
    evaluate_connection,
    evaluate_request,
)


def _allow(name, priority, hostname, vms="all"):
    selector = {"type": "all"} if vms == "all" else {"type": "only", "vms": vms}
    return {"name": name, "priority": priority, "hostname": hostname, "vm_selector": selector,
            "action": {"type": "allow"}}


def _block(name, priority, hostname, vms="all"):
    selector = {"type": "all"} if vms == "all" else {"type": "only", "vms": vms}
    return {"name": name, "priority": priority, "hostname": hostname, "vm_selector": selector,
            "action": {"type": "block"}}


def _awc(name, priority, hostname, credential, path_prefix, vms="all", injection=None):
    selector = {"type": "all"} if vms == "all" else {"type": "only", "vms": vms}
    return {
        "name": name,
        "priority": priority,
        "hostname": hostname,
        "vm_selector": selector,
        "action": {
            "type": "allow_with_credential",
            "credential": credential,
            "path_prefix": path_prefix,
            "injection": injection or {"type": "bearer"},
        },
    }


# --- canonicalize_incoming_hostname ---

def test_canonicalize_lowercases_and_strips_trailing_dot_and_port():
    assert canonicalize_incoming_hostname("GitHub.COM.:443") == "github.com"


def test_canonicalize_leaves_plain_hostname_alone():
    assert canonicalize_incoming_hostname("api.github.com") == "api.github.com"


# --- evaluate_connection ---

def test_no_rules_default_allow_not_intercepted():
    decision = evaluate_connection([], "skills-dev", "github.com")
    assert decision.intercepted is False
    assert decision.outcome == ALLOW_DEFAULT


def test_matching_allow_rule_not_intercepted():
    rules = [_allow("r1", 10, "github.com")]
    decision = evaluate_connection(rules, "skills-dev", "github.com")
    assert decision.intercepted is False
    assert decision.outcome == ALLOW_RULE
    assert decision.matched_rule == "r1"


def test_matching_block_rule_not_intercepted():
    rules = [_block("r1", 10, "evil.example")]
    decision = evaluate_connection(rules, "skills-dev", "evil.example")
    assert decision.intercepted is False
    assert decision.outcome == BLOCK_RULE
    assert decision.matched_rule == "r1"


def test_allow_with_credential_forces_interception():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    decision = evaluate_connection(rules, "skills-dev", "api.github.com")
    assert decision.intercepted is True
    assert decision.intercepted_by_rule == "r1"
    assert decision.outcome is None


def test_lowest_priority_wins():
    rules = [
        _block("later", 20, "github.com"),
        _allow("earlier", 10, "github.com"),
    ]
    decision = evaluate_connection(rules, "skills-dev", "github.com")
    assert decision.outcome == ALLOW_RULE
    assert decision.matched_rule == "earlier"


def test_vm_selector_only_excludes_other_vms():
    rules = [_allow("r1", 10, "github.com", vms=["other-vm"])]
    decision = evaluate_connection(rules, "skills-dev", "github.com")
    assert decision.intercepted is False
    assert decision.outcome == ALLOW_DEFAULT  # rule doesn't select this VM, so it's not a candidate


def test_hostname_must_match_exactly():
    rules = [_allow("r1", 10, "github.com")]
    decision = evaluate_connection(rules, "skills-dev", "notgithub.com")
    assert decision.outcome == ALLOW_DEFAULT


def test_incoming_hostname_canonicalized_before_matching():
    rules = [_allow("r1", 10, "github.com")]
    decision = evaluate_connection(rules, "skills-dev", "GitHub.COM.")
    assert decision.outcome == ALLOW_RULE


# --- evaluate_request ---

def test_matching_path_returns_allow_credential():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/cau777/issues")
    assert decision.outcome == ALLOW_CREDENTIAL
    assert decision.matched_rule == "r1"
    assert decision.credential == "gh"
    assert decision.injection == {"type": "bearer"}


def test_path_prefix_is_segment_boundary_aware():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    # "/repos/cau777x" shares the string prefix but not a path segment boundary
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/cau777x")
    assert decision.outcome == ALLOW_DEFAULT


def test_exact_path_match():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/cau777")
    assert decision.outcome == ALLOW_CREDENTIAL


def test_non_matching_path_falls_through_to_default():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/user")
    assert decision.outcome == ALLOW_DEFAULT
    assert decision.matched_rule is None


def test_unsafe_path_skips_injection_and_continues():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/repos/cau777")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/../secret")
    assert decision.outcome == ALLOW_DEFAULT
    assert decision.trace[0].result == "skipped_unsafe_path"


def test_intercepted_connection_can_still_be_blocked_by_lower_priority_rule():
    # The scenario ticket #14 (Q13) called out: interception is forced by the
    # first-encountered allow_with_credential candidate, but a later Block
    # rule for the same hostname can still decide an individual request.
    rules = [
        _awc("inject", 10, "api.github.com", "gh", "/repos/cau777"),
        _block("block-rest", 20, "api.github.com"),
    ]
    conn = evaluate_connection(rules, "skills-dev", "api.github.com")
    assert conn.intercepted is True
    assert conn.intercepted_by_rule == "inject"

    matching_request = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/cau777")
    assert matching_request.outcome == ALLOW_CREDENTIAL

    other_request = evaluate_request(rules, "skills-dev", "api.github.com", "/user")
    assert other_request.outcome == BLOCK_RULE
    assert other_request.matched_rule == "block-rest"


def test_trace_records_every_selected_rule_in_priority_order():
    rules = [
        _awc("first", 10, "api.github.com", "gh", "/repos/cau777"),
        _awc("second", 20, "api.github.com", "gh", "/repos/other"),
    ]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/other/issues")
    assert [t.rule_name for t in decision.trace] == ["first", "second"]
    assert decision.trace[0].result == "path_no_match_continue"
    assert decision.trace[1].result == "matched_terminal"


def test_root_path_prefix_matches_every_path():
    # "/" is the Web UI's own default for a new rule's path_prefix — it
    # must match every request path, not just a literal "GET /".
    rules = [_awc("r1", 10, "api.github.com", "gh", "/")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/repos/cau777/issues")
    assert decision.outcome == ALLOW_CREDENTIAL
    assert decision.matched_rule == "r1"


def test_root_path_prefix_matches_bare_root_too():
    rules = [_awc("r1", 10, "api.github.com", "gh", "/")]
    decision = evaluate_request(rules, "skills-dev", "api.github.com", "/")
    assert decision.outcome == ALLOW_CREDENTIAL


def test_no_matching_rules_defaults_allow():
    decision = evaluate_request([], "skills-dev", "github.com", "/anything")
    assert decision.outcome == ALLOW_DEFAULT
    assert decision.trace == []


def test_wildcard_intercepts_and_selects_credentials_for_subdomains():
    rules = [_awc('aws', 10, '*.us-east-1.amazonaws.com', 'aws', '/', vms=['secure-stack'])]
    for host in ('ecs.us-east-1.amazonaws.com', 'bucket.s3.us-east-1.amazonaws.com'):
        assert evaluate_connection(rules, 'secure-stack', host).intercepted
        assert evaluate_request(rules, 'secure-stack', host, '/').credential == 'aws'
    for host in ('us-east-1.amazonaws.com', 'ecs.us-east-1.amazonaws.com.evil.com', 'ecs.us-west-2.amazonaws.com'):
        assert not evaluate_connection(rules, 'secure-stack', host).intercepted
    assert not evaluate_connection(rules, 'other-vm', 'ecs.us-east-1.amazonaws.com').intercepted


def test_wildcard_and_exact_rules_follow_priority():
    rules = [_allow('exception', 1, 'ecs.us-east-1.amazonaws.com'),
             _awc('aws', 10, '*.us-east-1.amazonaws.com', 'aws', '/')]
    assert evaluate_connection(rules, 'vm', 'ecs.us-east-1.amazonaws.com').outcome == ALLOW_RULE


def test_aws_endpoint_selector_covers_partitions_and_dualstack_only_for_selected_vm():
    rules = [_awc('aws', 10, '@aws', 'aws', '/', vms=['secure-stack'])]
    for host in ('iam.amazonaws.com', 'ecs.us-east-1.amazonaws.com', 'ec2.us-west-2.api.aws', 'ec2.cn-north-1.amazonaws.com.cn'):
        assert evaluate_connection(rules, 'secure-stack', host).intercepted
        assert evaluate_request(rules, 'secure-stack', host, '/').credential == 'aws'
        assert not evaluate_connection(rules, 'other', host).intercepted
    for host in ('amazonaws.com', 'api.aws', 'iam.amazonaws.com.evil.com', 'evilamazonaws.com', 'github.com'):
        assert not evaluate_connection(rules, 'secure-stack', host).intercepted
