import asyncio

import pytest

from orca_proxy.credential_exec import CredentialCache, CredentialExecutionError, digest


async def test_successful_execution_returns_trimmed_output():
    cache = CredentialCache()
    value = await cache.get_value("c", "echo hello", ttl_seconds=60)
    assert value == "hello"


async def test_preserves_internal_spaces():
    cache = CredentialCache()
    value = await cache.get_value("c", "printf 'a b  c'", ttl_seconds=60)
    assert value == "a b  c"


async def test_nonzero_exit_raises_exit_category():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError) as exc_info:
        await cache.get_value("c", "exit 1", ttl_seconds=60)
    assert exc_info.value.category == "exit"
    assert exc_info.value.exit_code == 1


async def test_timeout_raises_timeout_category():
    cache = CredentialCache(timeout_seconds=0.2)
    with pytest.raises(CredentialExecutionError) as exc_info:
        await cache.get_value("c", "sleep 5", ttl_seconds=60)
    assert exc_info.value.category == "timeout"


async def test_empty_output_rejected():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError) as exc_info:
        await cache.get_value("c", "true", ttl_seconds=60)
    assert exc_info.value.category == "invalid_output"


async def test_output_too_large_rejected():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError) as exc_info:
        await cache.get_value("c", "yes | head -c 20000", ttl_seconds=60)
    assert exc_info.value.category == "invalid_output"


async def test_embedded_control_character_rejected():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError) as exc_info:
        await cache.get_value("c", "printf 'a\\tb'", ttl_seconds=60)
    assert exc_info.value.category == "invalid_output"


async def test_value_cached_within_ttl():
    cache = CredentialCache()
    marker_file_cmd = "echo $$"  # PID differs per execution if re-run
    first = await cache.get_value("c", marker_file_cmd, ttl_seconds=60)
    second = await cache.get_value("c", marker_file_cmd, ttl_seconds=60)
    assert first == second  # second call served from cache, command not re-run


async def test_ttl_zero_disables_caching():
    cache = CredentialCache()
    first = await cache.get_value("c", "echo $$", ttl_seconds=0)
    second = await cache.get_value("c", "echo $$", ttl_seconds=0)
    assert first != second  # re-executed every time


async def test_expiry_triggers_re_execution(monkeypatch):
    cache = CredentialCache()
    first = await cache.get_value("c", "echo $$", ttl_seconds=60)
    # Force the cached entry to look expired without sleeping in the test.
    state = cache._states["c"]
    state.expires_at = 0.0
    second = await cache.get_value("c", "echo $$", ttl_seconds=60)
    assert first != second


async def test_single_flight_concurrent_callers_share_one_execution():
    cache = CredentialCache()
    results = await asyncio.gather(
        cache.get_value("c", "sleep 0.1 && echo $$", ttl_seconds=60),
        cache.get_value("c", "sleep 0.1 && echo $$", ttl_seconds=60),
        cache.get_value("c", "sleep 0.1 && echo $$", ttl_seconds=60),
    )
    assert len(set(results)) == 1  # only one subprocess ran


async def test_different_credentials_execute_concurrently():
    cache = CredentialCache()
    start = asyncio.get_event_loop().time()
    await asyncio.gather(
        cache.get_value("a", "sleep 0.2 && echo a", ttl_seconds=60),
        cache.get_value("b", "sleep 0.2 && echo b", ttl_seconds=60),
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.35  # ran concurrently, not serially (would be ~0.4s+)


async def test_failure_not_cached_next_call_retries():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError):
        await cache.get_value("c", "exit 1", ttl_seconds=60)
    # A different command for the same name proves the failure wasn't cached.
    value = await cache.get_value("c", "echo ok", ttl_seconds=60)
    assert value == "ok"


async def test_status_reflects_success():
    cache = CredentialCache()
    assert cache.get_status("c")["status"] == "empty"
    await cache.get_value("c", "echo hello", ttl_seconds=60)
    status = cache.get_status("c")
    assert status["status"] == "valid"
    assert status["last_success_at"] is not None
    assert status["failure_category"] is None


async def test_status_reflects_failure_category():
    cache = CredentialCache()
    with pytest.raises(CredentialExecutionError):
        await cache.get_value("c", "exit 3", ttl_seconds=60)
    status = cache.get_status("c")
    assert status["status"] == "error"
    assert status["failure_category"] == "exit"
    assert status["last_failure_at"] is not None


async def test_status_never_exposes_value():
    cache = CredentialCache()
    await cache.get_value("c", "echo super-secret-value", ttl_seconds=60)
    status = cache.get_status("c")
    assert "value" not in status
    assert "super-secret-value" not in str(status)


async def test_invalidate_clears_cached_value():
    cache = CredentialCache()
    await cache.get_value("c", "echo first", ttl_seconds=60)
    cache.invalidate("c")
    assert cache.get_status("c")["status"] == "empty"
    value = await cache.get_value("c", "echo second", ttl_seconds=60)
    assert value == "second"


async def test_invalidate_kills_inflight_and_discards_its_result():
    cache = CredentialCache()
    task = asyncio.create_task(cache.get_value("c", "sleep 2 && echo late", ttl_seconds=60))
    await asyncio.sleep(0.05)  # let the subprocess actually start
    cache.invalidate("c")
    with pytest.raises(CredentialExecutionError):
        await task
    # The killed execution's result must not have landed in the fresh state.
    assert cache.get_status("c")["status"] == "empty"


async def test_cancelling_the_owning_caller_does_not_deadlock_followers():
    """A non-CredentialExecutionError raised out of _execute (most
    realistically asyncio.CancelledError from a client disconnect) must
    still clear state.inflight and resolve the shared future — otherwise
    every other caller single-flighted onto the same in-flight execution
    (`await asyncio.shield(state.inflight)`) hangs forever.
    """
    cache = CredentialCache()
    owner = asyncio.create_task(cache.get_value("c", "sleep 1 && echo x", ttl_seconds=60))
    await asyncio.sleep(0.05)
    follower = asyncio.create_task(cache.get_value("c", "sleep 1 && echo x", ttl_seconds=60))
    await asyncio.sleep(0.05)

    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner

    # The follower must fail fast with a real error, not hang indefinitely.
    with pytest.raises(CredentialExecutionError):
        await asyncio.wait_for(follower, timeout=1)


async def test_drop_removes_state_entirely():
    cache = CredentialCache()
    await cache.get_value("c", "echo hello", ttl_seconds=60)
    cache.drop("c")
    assert cache.get_status("c") == {
        "status": "empty",
        "expires_at": None,
        "last_success_at": None,
        "last_failure_at": None,
        "failure_category": None,
    }


async def test_reject_forgets_the_rejected_value_so_next_call_refreshes(tmp_path):
    counter = tmp_path / "n"
    command = f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; echo token-$n"
    cache = CredentialCache()
    first = await cache.get_value("c", command, ttl_seconds=3600)
    assert cache.reject("c", digest(first))
    assert await cache.get_value("c", command, ttl_seconds=3600) == "token-2"


async def test_reject_ignores_a_stale_digest():
    cache = CredentialCache()
    await cache.get_value("c", "echo new", ttl_seconds=3600)
    assert not cache.reject("c", digest("old"))
    assert cache.get_status("c")["status"] == "valid"
