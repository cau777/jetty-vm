"""Unprivileged side of per-VM DNAT reconciliation (design ticket #12).

The actual iptables logic (`build_commands`/`reconcile`) lives entirely in
`deploy/orca-proxy-firewall-sync` now, not here — that script is compiled
(Nuitka) to a standalone binary and carries `CAP_NET_ADMIN`/`CAP_NET_RAW`
via `setcap`, installed root-owned so nothing in its execution path is
writable by the unprivileged service account. This module is what the
Management API (never root, never capability-bearing) actually calls: run
that capability-carrying binary directly and parse its JSON stdout — no
`sudo`, no sudoers entry, the kernel grants the capability from the file's
own extended attributes regardless of who invokes it.
"""

import asyncio
import json
import subprocess
from collections.abc import Callable
from functools import partial
from pathlib import Path


def run_reconcile_script(
    script_path: Path, db_path: Path, bridge: str, proxy_port: int, runner=subprocess.run
) -> dict[str, str]:
    """What the aiohttp process actually calls: invoke the capability-carrying
    helper binary as a subprocess and parse its JSON stdout. Kept separate
    from the helper itself so the Management API side never holds
    `CAP_NET_ADMIN` itself — only the one auditable, setcap'd binary does.
    """
    result = runner(
        [
            str(script_path),
            "--db", str(db_path),
            "--bridge", bridge,
            "--proxy-port", str(proxy_port),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"__error__": result.stderr.strip() or "firewall-sync script failed"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"__error__": "firewall-sync script returned malformed output"}


class FirewallSync:
    """aiohttp-side handle: triggers the capability-carrying binary and remembers
    the last-known per-VM status for `/readyz` (#12's Q7 — no separate
    diagnostics endpoint, everything folds into `/readyz`'s JSON body).
    """

    def __init__(self, script_path: Path, db_path: Path, bridge: str, proxy_port: int, runner=subprocess.run):
        self._script_path = script_path
        self._db_path = db_path
        self._bridge = bridge
        self._proxy_port = proxy_port
        self._runner = runner
        self._status: dict[str, str] = {}
        self._async_lock = asyncio.Lock()
        # Tracks whether this process has ever asked the script to populate
        # the chains — NOT whether any VM is registered right now. Needed
        # to distinguish "fresh install, chains never touched, nothing to
        # flush" (safe to skip — avoids invoking the helper binary
        # before the first VM is ever registered) from "we just deleted the
        # last VM" (the chains still hold that VM's REDIRECT/DROP rules
        # from the last real reconcile and MUST be flushed, or they persist
        # indefinitely and can later match an unrelated VM that's recycled
        # the same DHCP-leased IP).
        self._ever_reconciled_nonzero = False

    def reconcile(self, vm_count: int | None = None) -> dict[str, str]:
        """`vm_count`, when given, skips invoking the privileged binary only
        on a fresh install with zero VMs ever registered this process —
        there's nothing to reconcile, and no reason to shell out for a
        rebuild of empty chains. Once any reconcile has populated the
        chains, a later zero-VM call (e.g. deleting the last registered VM)
        always runs the script, since skipping it would leave that VM's
        rules stale in the kernel. Callers that don't have a cheap count
        handy can omit it; the script itself is idempotent either way.
        """
        if vm_count == 0 and not self._ever_reconciled_nonzero:
            self._status = {}
            return self._status
        if vm_count:
            self._ever_reconciled_nonzero = True
        try:
            self._status = run_reconcile_script(
                self._script_path, self._db_path, self._bridge, self._proxy_port, runner=self._runner
            )
        except Exception as exc:  # never let a firewall-sync failure break a VM CRUD response
            self._status = {"__error__": str(exc)}
        return self._status

    async def reconcile_async(self, vm_count: int | None = None) -> dict[str, str]:
        """Same as reconcile(), but off the calling event loop.

        `reconcile()` shells out via a blocking `subprocess.run(...)` call
        — fine for app.py's one startup call (runs before the loop is
        serving anything), but fatal for the Management API's per-VM
        create/delete handlers, which run embedded on mitmdump's own
        asyncio loop (#4's "same process" decision): a blocking call there
        freezes every in-flight TLS handshake and proxied request on the
        data plane for the duration of the iptables round-trip. Offload
        to a thread via run_in_executor so the loop stays responsive.
        """
        # VM CRUD and the maintenance loop can request reconciliation at the
        # same time. Serialize them so two privileged helpers never flush and
        # repopulate the same chains concurrently.
        async with self._async_lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, partial(self.reconcile, vm_count))

    async def maintain(
        self,
        vm_count: Callable[[], int],
        *,
        startup_attempts: int = 10,
        startup_interval: float = 1.0,
        steady_interval: float = 30.0,
        stop_after: int | None = None,
    ) -> None:
        """Continuously repair firewall state after external rewrites.

        Multipass starts in parallel with this user service and rebuilds its
        bridge firewall asynchronously, after systemd has already considered
        the Multipass service started. Check rapidly during that boot window,
        then settle into a slower integrity-check cadence. The privileged
        helper is check-before-rebuild, so intact rules are never flushed.

        ``stop_after`` bounds the loop for deterministic tests; production
        callers leave it unset.
        """
        completed = 0
        for _attempt in range(startup_attempts):
            await self.reconcile_async(vm_count=vm_count())
            completed += 1
            if stop_after is not None and completed >= stop_after:
                return
            await asyncio.sleep(startup_interval)

        while True:
            await self.reconcile_async(vm_count=vm_count())
            completed += 1
            if stop_after is not None and completed >= stop_after:
                return
            await asyncio.sleep(steady_interval)

    @property
    def status(self) -> dict[str, str]:
        return self._status

    @property
    def is_synced(self) -> bool:
        """Vacuously true with no registered VMs — nothing to reconcile yet,
        so a fresh install doesn't report unready before any VM exists.
        """
        return "__error__" not in self._status and all(v == "in_sync" for v in self._status.values())
