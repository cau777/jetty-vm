from types import SimpleNamespace

from orca_proxy.firewall import FirewallSync


# --- FirewallSync (aiohttp-side wrapper) ---


class FakeScriptRunner:
    """Fakes the `sudo -n <script> ...` subprocess call itself, not the
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
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    status = sync.reconcile(vm_count=1)
    assert status == {"vm-a": "in_sync"}
    assert sync.status == {"vm-a": "in_sync"}
    assert sync.is_synced is True


def test_firewall_sync_skips_script_when_vm_count_zero():
    runner = FakeScriptRunner()
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    status = sync.reconcile(vm_count=0)
    assert status == {}
    assert sync.is_synced is True
    assert runner.calls == []


def test_firewall_sync_marks_unsynced_on_script_failure():
    runner = FakeScriptRunner(returncode=1, stderr="permission denied")
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vm_count=1)
    assert sync.is_synced is False
    assert "__error__" in sync.status


def test_firewall_sync_marks_unsynced_on_malformed_output():
    runner = FakeScriptRunner(stdout="not json")
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vm_count=1)
    assert sync.is_synced is False


def test_firewall_sync_never_raises_even_if_runner_throws():
    def exploding_runner(*args, **kwargs):
        raise OSError("sudo not found")

    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=exploding_runner)
    status = sync.reconcile(vm_count=1)
    assert "__error__" in status
    assert sync.is_synced is False


def test_firewall_sync_flushes_on_delete_to_zero_after_having_had_vms():
    """Deleting the last registered VM must still run the script — skipping
    it leaves that VM's REDIRECT/DROP rules stale in the kernel, which can
    later match an unrelated VM that recycles the same IP.
    """
    runner = FakeScriptRunner(stdout="{}")
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vm_count=1)
    assert len(runner.calls) == 1
    sync.reconcile(vm_count=0)
    assert len(runner.calls) == 2  # script actually ran the second time too
    assert sync.status == {}


def test_firewall_sync_invokes_sudo_n_with_the_named_script_path():
    runner = FakeScriptRunner()
    sync = FirewallSync("/opt/orca-proxy/bin/orca-proxy-firewall-sync", "/data/state.sqlite", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vm_count=1)
    assert runner.calls[0][:3] == ["sudo", "-n", "/opt/orca-proxy/bin/orca-proxy-firewall-sync"]


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
    sync = FirewallSync("/path/to/script", "/path/to/db", "mpqemubr0", 8443, runner=runner)
    sync.reconcile(vm_count=1)
    runner.rules_present = False  # Multipass deletes the jump rules after startup.

    await sync.maintain(
        vm_count=lambda: 1,
        startup_attempts=1,
        startup_interval=0,
        steady_interval=0,
        stop_after=1,
    )

    assert runner.rules_present is True
    assert len(runner.calls) == 2
