"""Tests for deploy/orca-proxy-firewall-sync -- the single, dependency-free
script (design ticket #12) that is the entire privileged attack surface in
orca-proxy. It's not part of the `orca_proxy` package (deliberately -- see
its own module docstring for why), so it's loaded here directly from its
real path via importlib, the same source install.sh compiles (Nuitka) and
installs, `setcap`-granted, to /usr/local/sbin/orca-proxy-firewall-sync.
This is what's actually under test (pre-compile, running as plain Python),
not a hand-maintained copy of it.
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER_PATH = Path(__file__).parent.parent / "deploy" / "orca-proxy-firewall-sync"


def _load_helper():
    # No .py suffix (it's installed verbatim as an executable, not imported
    # as a package member) -- spec_from_file_location can't infer a loader
    # from the extension, so one is supplied explicitly.
    loader = importlib.machinery.SourceFileLoader("orca_proxy_firewall_sync_helper", str(HELPER_PATH))
    spec = importlib.util.spec_from_file_location("orca_proxy_firewall_sync_helper", HELPER_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper = _load_helper()


def _vm_args(vms: list[tuple[str, str]]) -> list[str]:
    args = []
    for name, ip in vms:
        args += ["--vm", f"{name}={ip}"]
    return args


class FakeRunner:
    def __init__(self, fail_on: set[int] | None = None):
        self.calls: list[list[str]] = []
        self._fail_on = fail_on or set()

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        returncode = 1 if len(self.calls) - 1 in self._fail_on else 0
        return SimpleNamespace(returncode=returncode, stdout="", stderr="boom" if returncode else "")


class FirewallStateRunner:
    """Small stateful iptables model for check-before-rebuild tests."""

    def __init__(self, rules_present: bool):
        self.rules_present = rules_present
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if "-C" in command:
            return SimpleNamespace(
                returncode=0 if self.rules_present else 1, stdout="", stderr=""
            )
        if "-S" in command:
            chain = command[-1]
            if not self.rules_present:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            table = "nat" if "nat" in command else "filter"
            target = "REDIRECT --to-port 8443" if table == "nat" else "DROP"
            stdout = (
                f"-N {chain}\n"
                f"-A {chain} -s 10.0.0.1/32 -p tcp -m tcp --dport 80 -j {target}\n"
                f"-A {chain} -s 10.0.0.1/32 -p tcp -m tcp --dport 443 -j {target}\n"
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        # Any mutation represents a successful full rebuild.
        self.rules_present = True
        return SimpleNamespace(returncode=0, stdout="", stderr="")


# --- build_commands (pure) ---


def test_build_commands_creates_and_flushes_both_chains():
    commands = helper.build_commands([], "mpqemubr0", 8443)
    joined = [" ".join(c) if c[0] != "sh" else c[-1] for c in commands]
    assert any(f"-t nat -N {helper.NAT_CHAIN}" in s for s in joined)
    assert any(f"-N {helper.FILTER_CHAIN}" in s and "-t nat" not in s for s in joined)
    assert ["iptables", "-t", "nat", "-F", helper.NAT_CHAIN] in commands
    assert ["iptables", "-F", helper.FILTER_CHAIN] in commands


def test_build_commands_chain_create_tolerates_already_exists():
    """iptables -N exits 1 if the chain already exists — true on every
    reconcile after the first VM registration. reconcile() treats any
    non-zero exit as fatal, so the -N commands must never fail on their
    own; a real bug had this abort the whole rebuild before the -F flush.
    """
    commands = helper.build_commands([], "mpqemubr0", 8443)
    create_commands = [c for c in commands if c[0] == "sh" and "-N" in c[-1]]
    assert len(create_commands) == 2
    for c in create_commands:
        assert "|| true" in c[-1]


def test_build_commands_rejects_unsafe_bridge_name():
    with pytest.raises(ValueError):
        helper.build_commands([], "eth0; id > /tmp/pwned; #", 8443)


def test_build_commands_one_vm_gets_redirect_and_drop_for_both_ports():
    commands = helper.build_commands([("skills-dev", "10.14.105.22")], "mpqemubr0", 8443)
    nat_rules = [c for c in commands if c[:4] == ["iptables", "-t", "nat", "-A"]]
    filter_rules = [c for c in commands if c[:3] == ["iptables", "-A", helper.FILTER_CHAIN]]
    assert len(nat_rules) == 2  # 80 and 443
    assert len(filter_rules) == 2
    assert all("-s" in c and "10.14.105.22" in c for c in nat_rules)
    assert all("REDIRECT" in c and "8443" in c for c in nat_rules)
    assert all("DROP" in c for c in filter_rules)


def test_build_commands_multiple_vms_each_get_their_own_rules():
    commands = helper.build_commands([("vm-a", "10.0.0.1"), ("vm-b", "10.0.0.2")], "mpqemubr0", 8443)
    nat_rules = [c for c in commands if c[:4] == ["iptables", "-t", "nat", "-A"]]
    assert len(nat_rules) == 4  # 2 VMs x 2 ports


def test_build_commands_hooks_chains_via_idempotent_check_then_add():
    commands = helper.build_commands([], "mpqemubr0", 8443)
    joined = [" ".join(c) if isinstance(c, list) and c[0] != "sh" else c[-1] for c in commands]
    assert any("PREROUTING" in s and helper.NAT_CHAIN in s for s in joined)
    assert any("FORWARD" in s and helper.FILTER_CHAIN in s and "-I FORWARD 1" in s for s in joined)


# --- reconcile (execution, fake runner) ---


def test_reconcile_reports_in_sync_when_every_command_succeeds():
    runner = FakeRunner()
    status = helper.reconcile([("vm-a", "10.0.0.1")], "mpqemubr0", 8443, runner=runner)
    assert status == {"vm-a": "in_sync"}
    assert len(runner.calls) > 0


def test_reconcile_reports_error_for_every_vm_when_any_command_fails():
    runner = FakeRunner(fail_on={0})  # fail the very first command
    status = helper.reconcile([("vm-a", "10.0.0.1"), ("vm-b", "10.0.0.2")], "mpqemubr0", 8443, runner=runner)
    assert status == {"vm-a": "error", "vm-b": "error"}


def test_reconcile_stops_running_commands_after_first_failure():
    runner = FakeRunner(fail_on={0})
    helper.reconcile([("vm-a", "10.0.0.1")], "mpqemubr0", 8443, runner=runner)
    total_commands = len(helper.build_commands([("vm-a", "10.0.0.1")], "mpqemubr0", 8443))
    assert len(runner.calls) == 1 < total_commands


def test_ensure_reconciled_does_not_mutate_an_intact_firewall():
    runner = FirewallStateRunner(rules_present=True)

    status = helper.ensure_reconciled(
        [("vm-a", "10.0.0.1")], "mpqemubr0", 8443, runner=runner
    )

    assert status == {"vm-a": "in_sync"}
    assert not any("-F" in command or "-A" in command for command in runner.calls)


def test_ensure_reconciled_rebuilds_after_external_hook_deletion():
    runner = FirewallStateRunner(rules_present=False)

    status = helper.ensure_reconciled(
        [("vm-a", "10.0.0.1")], "mpqemubr0", 8443, runner=runner
    )

    assert status == {"vm-a": "in_sync"}
    assert runner.rules_present is True
    assert any("-F" in command for command in runner.calls)


# --- _parse_vm / main (argv integration) ---


def test_parse_vm_splits_on_first_equals():
    assert helper._parse_vm("skills-dev=10.14.105.22") == ("skills-dev", "10.14.105.22")


def test_parse_vm_rejects_missing_equals():
    with pytest.raises(Exception):
        helper._parse_vm("skills-dev")


def _ok_runner(command, **kwargs):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_main_prints_json_status_and_returns_zero(capsys):
    argv = ["--bridge", "mpqemubr0", "--proxy-port", "8443", *_vm_args([("skills-dev", "10.14.105.22")])]
    exit_code = helper.main(argv, runner=_ok_runner)
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"skills-dev": "in_sync"}


def test_main_returns_nonzero_on_failure(capsys):
    def failing_runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="permission denied")

    argv = ["--bridge", "mpqemubr0", "--proxy-port", "8443", *_vm_args([("skills-dev", "10.14.105.22")])]
    exit_code = helper.main(argv, runner=failing_runner)
    assert exit_code == 1


def test_main_with_no_vms_still_prints_valid_json(capsys):
    exit_code = helper.main(["--bridge", "mpqemubr0", "--proxy-port", "8443"], runner=_ok_runner)
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {}


# --- _raise_ambient_capabilities ---


def test_raise_ambient_capabilities_does_not_raise_when_unprivileged(capsys):
    """Running from a plain `pytest` process (no CAP_NET_ADMIN in the
    permitted/inheritable sets prctl(PR_CAP_AMBIENT_RAISE) requires) must
    not blow up main() -- it's best-effort, see the function's own
    docstring. The real signal that this failed is the subsequent
    `iptables` calls returning non-zero, which reconcile() already turns
    into an "error" status; that path is covered by
    test_main_returns_nonzero_on_failure and friends.
    """
    helper._raise_ambient_capabilities()  # must not raise
    assert "orca-proxy-firewall-sync" in capsys.readouterr().err
