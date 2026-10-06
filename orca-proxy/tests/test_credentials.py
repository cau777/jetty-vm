async def test_create_credential(client):
    resp = await client.put(
        "/api/v1/credentials/github-host-login",
        json={"command": "gh auth token", "ttl_seconds": 300},
    )
    body = await resp.json()
    assert resp.status == 201
    assert body["command"] == "gh auth token"
    assert body["ttl_seconds"] == 300
    assert body["status"] == "empty"


async def test_credential_value_never_exposed(client):
    resp = await client.put(
        "/api/v1/credentials/github-host-login",
        json={"command": "gh auth token", "ttl_seconds": 300},
    )
    body = await resp.json()
    assert "value" not in body
    assert "output" not in body
    assert "stdout" not in body


async def test_empty_command_rejected(client):
    resp = await client.put(
        "/api/v1/credentials/github-host-login", json={"command": "", "ttl_seconds": 300}
    )
    assert resp.status == 422


async def test_negative_ttl_rejected(client):
    resp = await client.put(
        "/api/v1/credentials/github-host-login",
        json={"command": "gh auth token", "ttl_seconds": -1},
    )
    assert resp.status == 422


async def test_zero_ttl_allowed(client):
    resp = await client.put(
        "/api/v1/credentials/github-host-login",
        json={"command": "gh auth token", "ttl_seconds": 0},
    )
    assert resp.status == 201


async def test_delete_credential_referenced_by_rule_409(client):
    await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.0.0.1"})
    await client.put(
        "/api/v1/credentials/github-host-login",
        json={"command": "gh auth token", "ttl_seconds": 300},
    )
    await client.put(
        "/api/v1/rules/inject-github",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "api.github.com",
            "action": {
                "type": "allow_with_credential",
                "credential": "github-host-login",
                "path_prefix": "/repos/cau777",
                "injection": {"type": "bearer"},
            },
        },
    )
    resp = await client.delete("/api/v1/credentials/github-host-login")
    assert resp.status == 409


async def test_list_credentials_sorted_by_name(client):
    await client.put("/api/v1/credentials/zeta", json={"command": "echo z", "ttl_seconds": 0})
    await client.put("/api/v1/credentials/alpha", json={"command": "echo a", "ttl_seconds": 0})
    resp = await client.get("/api/v1/credentials")
    body = await resp.json()
    assert [c["name"] for c in body["credentials"]] == ["alpha", "zeta"]


async def test_refresh_credential_forgets_cached_value_without_running_command(app, client, tmp_path):
    marker = tmp_path / "runs"
    command = f"echo run >> {marker}; echo token-$(wc -l < {marker})"
    await client.put("/api/v1/credentials/github-host-login", json={"command": command, "ttl_seconds": 300})
    cache = app["credential_cache"]
    assert await cache.get_value("github-host-login", command, 300) == "token-1"

    resp = await client.post("/api/v1/credentials/github-host-login/refresh")
    body = await resp.json()
    assert resp.status == 200
    assert body["status"] == "empty"
    assert "value" not in body
    assert marker.read_text().count("run") == 1

    assert await cache.get_value("github-host-login", command, 300) == "token-2"


async def test_refresh_unknown_credential_returns_not_found(client):
    resp = await client.post("/api/v1/credentials/missing/refresh")
    assert resp.status == 404
