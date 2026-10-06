import asyncio

import pytest

from orca_proxy.handlers import jetty as jetty_handlers


class FakeJetty:
    async def create_vm(self, name, **kwargs):
        progress = kwargs["progress"]
        progress("validate")
        await asyncio.sleep(0.01)
        progress("instance")
        progress("ready")
        return {"name": name, "ip_address": "10.202.0.21", "status": "Running"}

    async def setup(self, ssh_key=None, *, progress):
        progress("validate")
        await asyncio.sleep(0.01)
        progress("project")
        raise RuntimeError("fake gateway failure")


class SlowFakeJetty(FakeJetty):
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def create_vm(self, name, **kwargs):
        self.started.set()
        await self.release.wait()
        return {"name": name, "ip_address": "10.202.0.21", "status": "Running"}


async def _until_terminal(client, job_id):
    for _ in range(30):
        response = await client.get(f"/api/v1/jetty/jobs/{job_id}")
        assert response.status == 200
        job = await response.json()
        if job["state"] in {"done", "failed", "cancelled"}:
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


async def test_create_vm_returns_job_and_tracks_real_stage_updates(client, monkeypatch):
    monkeypatch.setattr(jetty_handlers, "_service", lambda _request: FakeJetty())
    response = await client.post(
        "/api/v1/jetty/vms",
        json={"name": "agent-vm", "cpus": 4, "memory": "8GiB", "disk": "40GiB"},
    )
    assert response.status == 202
    accepted = await response.json()
    assert accepted["state"] in {"queued", "running"}
    assert accepted["kind"] == "vm_create"

    job = await _until_terminal(client, accepted["job_id"])
    assert job["state"] == "done"
    assert job["result"] == {"name": "agent-vm", "ip_address": "10.202.0.21", "status": "Running"}
    assert [step["state"] for step in job["steps"]] == ["done"] * len(job["steps"])


async def test_gateway_setup_failure_is_available_in_job(client, monkeypatch):
    monkeypatch.setattr(jetty_handlers, "_service", lambda _request: FakeJetty())
    response = await client.post("/api/v1/jetty/setup", json={})
    assert response.status == 202
    accepted = await response.json()

    job = await _until_terminal(client, accepted["job_id"])
    assert job["state"] == "failed"
    assert job["error"]["message"] == "fake gateway failure"
    assert job["steps"][1]["state"] == "failed"


async def test_unknown_job_returns_not_found(client):
    response = await client.get("/api/v1/jetty/jobs/missing")
    body = await response.json()
    assert response.status == 404
    assert body["error"]["code"] == "not_found"


async def test_provisioning_rejects_overlapping_jobs(client, monkeypatch):
    service = SlowFakeJetty()
    monkeypatch.setattr(jetty_handlers, "_service", lambda _request: service)
    first = await client.post("/api/v1/jetty/vms", json={"name": "agent-vm"})
    assert first.status == 202
    accepted = await first.json()
    await service.started.wait()

    second = await client.post("/api/v1/jetty/vms", json={"name": "other-vm"})
    body = await second.json()
    assert second.status == 409
    assert body["error"]["message"] == "Another Jetty provisioning job is already in progress"

    service.release.set()
    assert (await _until_terminal(client, accepted["job_id"]))["state"] == "done"
