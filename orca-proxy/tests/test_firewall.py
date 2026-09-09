from types import SimpleNamespace

from orca_proxy.firewall import FirewallSync

VM_A = [("vm-a", "10.0.0.1")]


# --- FirewallSync (aiohttp-side wrapper) ---


class FakeScriptRunner:
    """Fakes the direct `<script> ...` subprocess call itself, not the
    inner iptables commands — this is what FirewallSync actually invokes.
    The inner commands (build_commands/reconcile) now live entirely in
    deploy/orca-proxy-firewall-sync — see test_firewall_sync_helper.py.
    """

    def __init__(self, stdout='{"vm-a": "in_sync"}', returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def test_firewall_sync_reconcile_updates_status():
    runner = FakeScriptRunner()
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    status = sync.reconcile(vms=VM_A)
    assert status == {"vm-a": "in_sync"}
    assert sync.status == {"vm-a": "in_sync"}
    assert sync.is_synced is True


def test_firewall_sync_skips_script_when_vms_empty():
    runner = FakeScriptRunner()
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    status = sync.reconcile(vms=[])
    assert status == {}
    assert sync.is_synced is True
    assert runner.calls == []


def test_firewall_sync_marks_unsynced_on_script_failure():
    runner = FakeScriptRunner(returncode=1, stderr="permission denied")
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vms=VM_A)
    assert sync.is_synced is False
    assert "__error__" in sync.status


def test_firewall_sync_marks_unsynced_on_malformed_output():
    runner = FakeScriptRunner(stdout="not json")
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vms=VM_A)
    assert sync.is_synced is False


def test_firewall_sync_never_raises_even_if_runner_throws():
    def exploding_runner(*args, **kwargs):
        raise OSError("orca-proxy-firewall-sync: command not found")

    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=exploding_runner)
    status = sync.reconcile(vms=VM_A)
    assert "__error__" in status
    assert sync.is_synced is False


def test_firewall_sync_flushes_on_delete_to_zero_after_having_had_vms():
    """Deleting the last registered VM must still run the script — skipping
    it leaves that VM's REDIRECT/DROP rules stale in the kernel, which can
    later match an unrelated VM that recycles the same IP.
    """
    runner = FakeScriptRunner(stdout="{}")
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vms=VM_A)
    assert len(runner.calls) == 1
    sync.reconcile(vms=[])
    assert len(runner.calls) == 2  # script actually ran the second time too
    assert sync.status == {}


def test_firewall_sync_invokes_the_named_script_path_directly_with_vm_args():
    runner = FakeScriptRunner()
    sync = FirewallSync("/opt/orca-proxy/bin/orca-proxy-firewall-sync", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vms=[("vm-a", "10.0.0.1"), ("vm-b", "10.0.0.2")])
    command = runner.calls[0]
    assert command[0] == "/opt/orca-proxy/bin/orca-proxy-firewall-sync"
    assert "--db" not in command
    assert command.count("--vm") == 2
    assert "vm-a=10.0.0.1" in command
    assert "vm-b=10.0.0.2" in command


async def test_maintenance_restores_rules_deleted_after_initial_reconcile():
    class StatefulRunner(FakeScriptRunner):
        def __init__(self):
            super().__init__()
            self.rules_present = False

        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            self.rules_present = True
            return result

    runner = StatefulRunner()
    sync = FirewallSync("/path/to/script", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vms=VM_A)
    runner.rules_present = False  # Multipass deletes the jump rules after startup.

    await sync.maintain(
        vms=lambda: VM_A,
        startup_attempts=1,
        startup_interval=0,
        steady_interval=0,
        stop_after=1,
    )

    assert runner.rules_present is True
    assert len(runner.calls) == 2
