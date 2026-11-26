async def test_create_vm(client):
    resp = await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.14.105.22"})
    body = await resp.json()
    assert resp.status == 201
    assert body["name"] == "skills-dev"
    assert body["ip_address"] == "10.14.105.22"


async def test_replace_vm_returns_200(client):
    await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.14.105.22"})
    resp = await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.14.105.23"})
    body = await resp.json()
    assert resp.status == 200
    assert body["ip_address"] == "10.14.105.23"


async def test_get_missing_vm_404(client):
    resp = await client.get("/api/v1/vms/nope")
    body = await resp.json()
    assert resp.status == 404
    assert body["error"]["code"] == "not_found"


async def test_list_vms_sorted_by_name(client):
    await client.put("/api/v1/vms/zeta", json={"ip_address": "10.0.0.1"})
    await client.put("/api/v1/vms/alpha", json={"ip_address": "10.0.0.2"})
    resp = await client.get("/api/v1/vms")
    body = await resp.json()
    assert [v["name"] for v in body["vms"]] == ["alpha", "zeta"]


async def test_invalid_name_rejected(client):
    resp = await client.put("/api/v1/vms/-bad-name", json={"ip_address": "10.0.0.1"})
    assert resp.status == 422


async def test_invalid_ip_rejected(client):
    resp = await client.put("/api/v1/vms/skills-dev", json={"ip_address": "not-an-ip"})
    body = await resp.json()
    assert resp.status == 422
    assert body["error"]["code"] == "validation_failed"
    assert "ip_address" in body["error"]["fields"]


async def test_duplicate_ip_rejected(client):
    await client.put("/api/v1/vms/vm-a", json={"ip_address": "10.0.0.1"})
    resp = await client.put("/api/v1/vms/vm-b", json={"ip_address": "10.0.0.1"})
    body = await resp.json()
    assert resp.status == 409
    assert body["error"]["code"] == "conflict"


async def test_replacing_own_ip_not_treated_as_duplicate(client):
    await client.put("/api/v1/vms/vm-a", json={"ip_address": "10.0.0.1"})
    resp = await client.put("/api/v1/vms/vm-a", json={"ip_address": "10.0.0.1"})
    assert resp.status == 200


async def test_unknown_field_rejected(client):
    resp = await client.put(
        "/api/v1/vms/skills-dev", json={"ip_address": "10.0.0.1", "extra": "nope"}
    )
    assert resp.status == 422


async def test_malformed_json_rejected(client):
    resp = await client.put(
        "/api/v1/vms/skills-dev", data="{not json", headers={"Content-Type": "application/json"}
    )
    body = await resp.json()
    assert resp.status == 400
    assert body["error"]["code"] == "invalid_json"


async def test_delete_vm(client):
    await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.0.0.1"})
    resp = await client.delete("/api/v1/vms/skills-dev")
    assert resp.status == 204
    resp = await client.get("/api/v1/vms/skills-dev")
    assert resp.status == 404


async def test_delete_missing_vm_404(client):
    resp = await client.delete("/api/v1/vms/nope")
    assert resp.status == 404


async def test_delete_vm_removes_only_its_rules(client):
    await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.0.0.1"})
    await client.put("/api/v1/vms/other-vm", json={"ip_address": "10.0.0.2"})
    for path, priority in [
        ("/api/v1/vms/skills-dev/rules/allow-github", 10),
        ("/api/v1/vms/skills-dev/rules/allow-github-api", 20),
        ("/api/v1/vms/other-vm/rules/allow-github", 10),
        ("/api/v1/rules/allow-github?scope=global", 10),
    ]:
        response = await client.put(
            path, json={"priority": priority, "hostname": "github.com", "action": {"type": "allow"}}
        )
        assert response.status == 201

    assert (await client.delete("/api/v1/vms/skills-dev")).status == 204
    assert (await client.get("/api/v1/vms/skills-dev")).status == 404
    response = await client.get("/api/v1/rules")
    assert [(rule["scope"], rule["name"]) for rule in (await response.json())["rules"]] == [
        ("", "allow-github"),
        ("other-vm", "allow-github"),
    ]


async def test_delete_vm_only_referenced_via_all_selector_not_blocked(client):
    await client.put("/api/v1/vms/skills-dev", json={"ip_address": "10.0.0.1"})
    await client.put(
        "/api/v1/rules/allow-everyone",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    resp = await client.delete("/api/v1/vms/skills-dev")
    assert resp.status == 204
