import pytest

from orca_proxy import cli


def test_cli_waits_until_operation_job_returns_result(monkeypatch):
    responses = iter(
        [
            {"job_id": "abc", "state": "running", "current_step": "boot", "steps": [{"key": "boot", "label": "Starting VM"}]},
            {"job_id": "abc", "state": "done", "result": {"name": "agent-vm", "status": "Running"}},
        ]
    )
    requests = []
    monkeypatch.setattr(cli, "_api", lambda method, path: (requests.append((method, path)), next(responses))[1])
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    result = cli._wait_for_job({"job_id": "abc", "state": "queued", "steps": []})

    assert result == {"name": "agent-vm", "status": "Running"}
    assert requests == [("GET", "/api/v1/jetty/jobs/abc"), ("GET", "/api/v1/jetty/jobs/abc")]


def test_cli_reports_job_failure(monkeypatch):
    monkeypatch.setattr(cli, "_api", lambda _method, _path: {"job_id": "abc", "state": "failed", "error": {"message": "cloud-init failed"}})
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="cloud-init failed"):
        cli._wait_for_job({"job_id": "abc", "state": "running", "steps": []})
