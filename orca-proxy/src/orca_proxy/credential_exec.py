"""Credential command execution and in-memory TTL caching (design ticket #10).

Each Credential is a trusted bash command whose stdout becomes a validated
Credential Value, TTL-cached only in memory. This module owns execution,
output validation, single-flight concurrency, and cache invalidation — never
persistence (the command string itself lives in SQLite, per repo/credentials.py)
and never HTTP exposure of the value (per #10: "Do not add refresh, test, or
cache-clear Management API operations in v1").
"""

import asyncio
import hashlib
import os
import signal
import time
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_OUTPUT_BYTES = 16 * 1024


class CredentialExecutionError(Exception):
    """Raised when a Credential command fails, times out, or produces invalid output.

    `category` is one of "exit" | "timeout" | "invalid_output" — the only
    values #10 permits exposing via status. `detail` is for logs only, never
    returned to a caller or persisted.
    """

    def __init__(self, category: str, detail: str, exit_code: int | None = None):
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.exit_code = exit_code


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class _CredentialState:
    status: str = "empty"  # empty | refreshing | valid | error
    value: str | None = None
    expires_at: float | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    failure_category: str | None = None
    process: "asyncio.subprocess.Process | None" = field(default=None, repr=False)
    inflight: "asyncio.Future | None" = field(default=None, repr=False)


def _validate_output(raw: bytes) -> str:
    if len(raw) > MAX_OUTPUT_BYTES:
        raise CredentialExecutionError("invalid_output", "output larger than 16 KiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CredentialExecutionError("invalid_output", "output is not valid UTF-8") from exc
    if text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise CredentialExecutionError("invalid_output", "output is empty")
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise CredentialExecutionError("invalid_output", "output contains a control character")
    return text


class CredentialCache:
    """In-memory, per-Credential single-flight execution and TTL cache."""

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._timeout_seconds = timeout_seconds
        self._states: dict[str, _CredentialState] = {}

    def _state(self, name: str) -> _CredentialState:
        return self._states.setdefault(name, _CredentialState())

    async def get_value(self, name: str, command: str, ttl_seconds: int) -> str:
        """Return a validated Credential Value, executing (and caching) as needed.

        Raises CredentialExecutionError on failure — callers translate this to
        a fail-closed 502 `credential_unavailable` response; failures are
        never cached, so the next new request retries.
        """
        state = self._state(name)
        now = time.monotonic()

        if state.value is not None and (state.expires_at is None or now < state.expires_at):
            return state.value

        if state.inflight is not None:
            return await asyncio.shield(state.inflight)

        state.status = "refreshing"
        future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        state.inflight = future
        try:
            value = await self._execute(state, command)
        except CredentialExecutionError as exc:
            state.status = "error"
            state.last_failure_at = time.time()
            state.failure_category = exc.category
            state.inflight = None
            if not future.done():
                future.set_exception(exc)
                future.exception()  # mark retrieved so an unawaited future doesn't warn
            raise
        except BaseException as exc:
            # Anything else — most realistically asyncio.CancelledError from
            # a cancelled caller (e.g. a disconnected HTTP client), but also
            # FileNotFoundError/OSError from a broken subprocess launch —
            # must still clear state.inflight and resolve the future.
            # Without this, every concurrent/future single-flight caller for
            # this Credential (`await asyncio.shield(state.inflight)` above)
            # hangs forever on a future nothing will ever resolve.
            state.status = "error"
            state.last_failure_at = time.time()
            state.failure_category = "exit"
            state.inflight = None
            if not future.done():
                wrapped = CredentialExecutionError("exit", f"execution failed: {exc!r}")
                future.set_exception(wrapped)
                future.exception()  # mark retrieved so an unawaited future doesn't warn
            raise
        else:
            # ttl_seconds == 0 disables caching entirely (#10) — leave
            # state.value unset so the next call always re-executes, rather
            # than treating "no expiry" as "cache forever" (the opposite).
            if ttl_seconds > 0:
                state.value = value
                state.expires_at = now + ttl_seconds
            state.status = "valid"
            state.last_success_at = time.time()
            state.failure_category = None
            state.inflight = None
            if not future.done():
                future.set_result(value)
            return value

    async def _execute(self, state: _CredentialState, command: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/bash",
            "-lc",
            command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # own process group, so a kill/timeout reaches descendants too
        )
        state.process = proc
        try:
            try:
                stdout_data, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=self._timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                self._killpg(proc)
                await proc.wait()
                raise CredentialExecutionError(
                    "timeout", f"command exceeded {self._timeout_seconds}s"
                ) from exc

            if proc.returncode != 0:
                raise CredentialExecutionError(
                    "exit", f"exit status {proc.returncode}", exit_code=proc.returncode
                )

            return _validate_output(stdout_data)
        finally:
            state.process = None

    @staticmethod
    def _killpg(proc: "asyncio.subprocess.Process") -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def invalidate(self, name: str) -> None:
        """Clear cached state; kill and discard any in-flight execution.

        Called on Credential PUT/DELETE (#10: "clears cached value, invalidates
        any older in-flight generation, and preferably terminates the old
        process group ... Always discard an old result if it later completes.
        Do not execute the new command during PUT"). Killing the process turns
        an in-flight execution's eventual outcome into a failure (nonzero
        exit) rather than a stale success, so already-waiting callers don't
        silently receive a pre-invalidation value.
        """
        old = self._states.get(name)
        if old is not None and old.process is not None:
            self._killpg(old.process)
        self._states[name] = _CredentialState()

    def reject(self, name: str, value_digest: str) -> bool:
        """Forget the cached value if it is still the one upstream just rejected.

        A refresh elsewhere on the host (the provider's own CLI, or another
        proxy sharing the same login) can revoke a cached access token before
        its TTL. Compared by digest so a burst of 401s for the same stale
        token clears it once, and a newer value or an in-flight refresh is
        left alone.
        """
        state = self._states.get(name)
        if state is None or state.value is None or value_digest != digest(state.value):
            return False
        state.value = None
        state.expires_at = None
        state.status = "empty"
        return True

    def drop(self, name: str) -> None:
        """Remove all state for a deleted Credential (same effect as invalidate, no replacement)."""
        old = self._states.pop(name, None)
        if old is not None and old.process is not None:
            self._killpg(old.process)

    def get_status(self, name: str) -> dict:
        """Safe, ephemeral status only — never the Credential Value or output."""
        state = self._states.get(name)
        if state is None:
            return {
                "status": "empty",
                "expires_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "failure_category": None,
            }
        return {
            "status": state.status,
            "expires_at": state.expires_at,
            "last_success_at": state.last_success_at,
            "last_failure_at": state.last_failure_at,
            "failure_category": state.failure_category,
        }
