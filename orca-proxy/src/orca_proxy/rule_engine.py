"""Rule matching / evaluation (design ticket #5).

Pure decision logic — no I/O, no mitmproxy or aiohttp dependency, so it's
directly unit-testable and reusable from both the future proxy addon and the
request-logging layer. Callers pass in the Rule rows (already the shape
produced by handlers/rules.py's _serialize / repo/rules.py) plus the live
connection facts (VM name, hostname) or per-request facts (+ path).

Two entry points, matching the two-level connection/request-row split from
design ticket #11's logging schema, and the finding from ticket #14 that
outcome is fundamentally per-HTTP-request, not per-connection, once TLS is
intercepted:

- evaluate_connection(): a one-time, pre-TLS decision — is this (VM, hostname)
  pair terminal (Allow/Block) or does the first-encountered candidate force
  interception (Allow-with-credential)? Mirrors #5 steps 1-4.
- evaluate_request(): re-walks the same candidate list per HTTP request
  (path varies per request even on one intercepted connection), implementing
  #5 steps 4-8 including the path-eligibility short-circuit from step 7.
"""

from dataclasses import dataclass, field

from .validation import is_safe_absolute_path, normalize_path

ALLOW_DEFAULT = "allow_default"
ALLOW_RULE = "allow_rule"
BLOCK_RULE = "block_rule"
ALLOW_CREDENTIAL = "allow_credential"


def canonicalize_incoming_hostname(raw: str) -> str:
    """Normalize a live SNI/Host value the same way a stored Rule's hostname
    is normalized at write time (validation.validate_hostname), but without
    rejecting anything — an unmatchable hostname should fall through to
    default-Allow, not crash the connection.
    """
    value = raw.strip().lower()
    host, sep, port = value.partition(":")
    if not (sep and port.isdigit()):
        host = value
    if host.endswith("."):
        host = host[:-1]
    return host


def _selects_vm(vm_selector: dict, vm_name: str) -> bool:
    if vm_selector["type"] == "all":
        return True
    return vm_name in vm_selector["vms"]


def _matches_hostname(pattern: str, hostname: str) -> bool:
    if pattern.startswith("*."):
        # The dot boundary excludes the apex and lookalike suffixes.
        return hostname.endswith(pattern[1:]) and len(hostname) > len(pattern[1:])
    return pattern == hostname


def _candidates(rules: list[dict], vm_name: str, hostname: str) -> list[dict]:
    """Rules whose VM selector and hostname match, in ascending priority order.

    Priority is the sole tiebreaker (#5 step 3) — duplicate priorities are
    rejected at write time (repo/rules.py + handlers/rules.py), so this is
    unambiguous.
    """
    matched = [
        rule
        for rule in rules
        if _matches_hostname(rule["hostname"], hostname) and _selects_vm(rule["vm_selector"], vm_name)
    ]
    return sorted(matched, key=lambda r: r["priority"])


@dataclass
class ConnectionDecision:
    intercepted: bool
    outcome: str | None = None  # set only when not intercepted
    matched_rule: str | None = None  # rule name behind `outcome`
    intercepted_by_rule: str | None = None  # rule name that forced interception


def evaluate_connection(rules: list[dict], vm_name: str, hostname: str) -> ConnectionDecision:
    hostname = canonicalize_incoming_hostname(hostname)
    candidates = _candidates(rules, vm_name, hostname)
    if not candidates:
        return ConnectionDecision(intercepted=False, outcome=ALLOW_DEFAULT)

    first = candidates[0]
    if first["action"]["type"] == "allow":
        return ConnectionDecision(intercepted=False, outcome=ALLOW_RULE, matched_rule=first["name"])
    if first["action"]["type"] == "block":
        return ConnectionDecision(intercepted=False, outcome=BLOCK_RULE, matched_rule=first["name"])
    # allow_with_credential: interception is forced by this rule's mere
    # presence — the per-path outcome is resolved later, per HTTP request.
    return ConnectionDecision(intercepted=True, intercepted_by_rule=first["name"])


@dataclass
class TraceEntry:
    rule_name: str
    priority: int
    action_type: str
    result: str  # matched_terminal | path_no_match_continue | skipped_unsafe_path


@dataclass
class RequestDecision:
    outcome: str
    matched_rule: str | None = None
    credential: str | None = None
    injection: dict | None = None
    trace: list[TraceEntry] = field(default_factory=list)


def evaluate_request(rules: list[dict], vm_name: str, hostname: str, path: str) -> RequestDecision:
    hostname = canonicalize_incoming_hostname(hostname)
    candidates = _candidates(rules, vm_name, hostname)
    trace: list[TraceEntry] = []

    for rule in candidates:
        action = rule["action"]

        if action["type"] == "allow":
            trace.append(TraceEntry(rule["name"], rule["priority"], "allow", "matched_terminal"))
            return RequestDecision(outcome=ALLOW_RULE, matched_rule=rule["name"], trace=trace)

        if action["type"] == "block":
            trace.append(TraceEntry(rule["name"], rule["priority"], "block", "matched_terminal"))
            return RequestDecision(outcome=BLOCK_RULE, matched_rule=rule["name"], trace=trace)

        # allow_with_credential
        if not is_safe_absolute_path(path):
            trace.append(
                TraceEntry(
                    rule["name"], rule["priority"], "allow_with_credential", "skipped_unsafe_path"
                )
            )
            continue

        normalized_path = normalize_path(path)
        prefix = action["path_prefix"]
        # "/" is a segment boundary by definition — every absolute path is
        # a descendant of the root — so it must match everything, not just
        # a literal "/". Without this special case, `prefix + "/"` becomes
        # "//", which no real request path starts with, so a "/" prefix
        # (the Web UI's own default for new rules) would silently match
        # nothing but the bare root.
        matches = prefix == "/" or normalized_path == prefix or normalized_path.startswith(prefix + "/")
        if matches:
            trace.append(
                TraceEntry(
                    rule["name"], rule["priority"], "allow_with_credential", "matched_terminal"
                )
            )
            return RequestDecision(
                outcome=ALLOW_CREDENTIAL,
                matched_rule=rule["name"],
                credential=action["credential"],
                injection=action["injection"],
                trace=trace,
            )

        trace.append(
            TraceEntry(
                rule["name"], rule["priority"], "allow_with_credential", "path_no_match_continue"
            )
        )

    return RequestDecision(outcome=ALLOW_DEFAULT, trace=trace)
