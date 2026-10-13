async def _make_vm(client, name="skills-dev", ip="10.0.0.1"):
    await client.put(f"/api/v1/vms/{name}", json={"ip_address": ip})


async def _make_credential(client, name="github-host-login"):
    await client.put(f"/api/v1/credentials/{name}", json={"command": "gh auth token", "ttl_seconds": 300})


async def test_create_allow_rule(client):
    await _make_vm(client)
    resp = await client.put(
        "/api/v1/rules/allow-github",
        json={
            "priority": 10,
            "vm_selector": {"type": "only", "vms": ["skills-dev"]},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    body = await resp.json()
    assert resp.status == 201
    assert body["action"] == {"type": "allow"}
    assert body["vm_selector"] == {"type": "only", "vms": ["skills-dev"]}


async def test_hostname_normalized(client):
    await _make_vm(client)
    resp = await client.put(
        "/api/v1/rules/allow-github",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "GitHub.COM.",
            "action": {"type": "allow"},
        },
    )
    body = await resp.json()
    assert body["hostname"] == "github.com"


async def test_hostname_with_scheme_rejected(client):
    resp = await client.put(
        "/api/v1/rules/bad",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "https://github.com",
            "action": {"type": "allow"},
        },
    )
    assert resp.status == 422


async def test_vm_selector_only_unknown_vm_rejected(client):
    resp = await client.put(
        "/api/v1/rules/bad",
        json={
            "priority": 10,
            "vm_selector": {"type": "only", "vms": ["does-not-exist"]},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    body = await resp.json()
    assert resp.status == 422
    assert "vm_selector" in body["error"]["fields"]


async def test_vm_selector_only_empty_list_rejected(client):
    resp = await client.put(
        "/api/v1/rules/bad",
        json={
            "priority": 10,
            "vm_selector": {"type": "only", "vms": []},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    assert resp.status == 422


async def test_allow_with_credential_rule(client):
    await _make_vm(client)
    await _make_credential(client)
    resp = await client.put(
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
    body = await resp.json()
    assert resp.status == 201
    assert body["action"]["credential"] == "github-host-login"
    assert body["action"]["path_prefix"] == "/repos/cau777"


async def test_allow_with_credential_unknown_credential_rejected(client):
    resp = await client.put(
        "/api/v1/rules/inject-github",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "api.github.com",
            "action": {
                "type": "allow_with_credential",
                "credential": "does-not-exist",
                "path_prefix": "/repos/cau777",
                "injection": {"type": "bearer"},
            },
        },
    )
    assert resp.status == 422


async def test_allow_with_credential_basic_requires_username(client):
    await _make_credential(client)
    resp = await client.put(
        "/api/v1/rules/inject-github",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "github.com",
            "action": {
                "type": "allow_with_credential",
                "credential": "github-host-login",
                "path_prefix": "/cau777",
                "injection": {"type": "basic"},
            },
        },
    )
    assert resp.status == 422


async def test_path_prefix_dot_segment_rejected(client):
    await _make_credential(client)
    resp = await client.put(
        "/api/v1/rules/bad",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "api.github.com",
            "action": {
                "type": "allow_with_credential",
                "credential": "github-host-login",
                "path_prefix": "/repos/../secret",
                "injection": {"type": "bearer"},
            },
        },
    )
    assert resp.status == 422


async def test_path_prefix_percent_encoding_rejected(client):
    await _make_credential(client)
    resp = await client.put(
        "/api/v1/rules/bad",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "api.github.com",
            "action": {
                "type": "allow_with_credential",
                "credential": "github-host-login",
                "path_prefix": "/repos%2Fsecret",
                "injection": {"type": "bearer"},
            },
        },
    )
    assert resp.status == 422


async def test_duplicate_priority_rejected(client):
    await client.put(
        "/api/v1/rules/first",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    resp = await client.put(
        "/api/v1/rules/second",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "example.com",
            "action": {"type": "block"},
        },
    )
    body = await resp.json()
    assert resp.status == 409
    assert body["error"]["code"] == "conflict"


async def test_list_rules_sorted_by_priority(client):
    await client.put(
        "/api/v1/rules/low",
        json={
            "priority": 20,
            "vm_selector": {"type": "all"},
            "hostname": "b.com",
            "action": {"type": "allow"},
        },
    )
    await client.put(
        "/api/v1/rules/high",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "a.com",
            "action": {"type": "block"},
        },
    )
    resp = await client.get("/api/v1/rules")
    body = await resp.json()
    assert [r["name"] for r in body["rules"]] == ["high", "low"]


async def test_delete_rule(client):
    await client.put(
        "/api/v1/rules/allow-all",
        json={
            "priority": 10,
            "vm_selector": {"type": "all"},
            "hostname": "github.com",
            "action": {"type": "allow"},
        },
    )
    resp = await client.delete("/api/v1/rules/allow-all")
    assert resp.status == 204


async def test_aws_sigv4_rule_round_trips_service_and_region(client):
    await _make_credential(client)
    injection = {"type": "aws_sigv4", "service": "sts", "region": "us-east-1"}
    response = await client.put("/api/v1/rules/aws", json={
        "priority": 10, "vm_selector": {"type": "all"}, "hostname": "sts.us-east-1.amazonaws.com",
        "action": {"type": "allow_with_credential", "credential": "github-host-login", "path_prefix": "/", "injection": injection},
    })
    assert response.status == 201
    assert (await response.json())["action"]["injection"] == injection
    for invalid in ({"type": "aws_sigv4"}, {**injection, "region": "*"}, {**injection, "service": ""}, {**injection, "extra": "oops"}):
        response = await client.put("/api/v1/rules/aws", json={
            "priority": 10, "vm_selector": {"type": "all"}, "hostname": "sts.us-east-1.amazonaws.com",
            "action": {"type": "allow_with_credential", "credential": "github-host-login", "path_prefix": "/", "injection": invalid},
        })
        assert response.status == 422


async def test_wildcard_aws_rule_round_trips(client):
    await _make_credential(client)
    response = await client.put('/api/v1/rules/regional-aws', json={
        'priority': 10, 'vm_selector': {'type': 'all'},
        'hostname': '*.US-EAST-1.AMAZONAWS.COM.',
        'action': {'type': 'allow_with_credential', 'credential': 'github-host-login',
                   'path_prefix': '/', 'injection': {'type': 'aws_sigv4', 'service': 'auto', 'region': 'us-east-1'}},
    })
    assert response.status == 201
    rule = await response.json()
    assert rule['hostname'] == '*.us-east-1.amazonaws.com'
    assert rule['action']['injection']['service'] == 'auto'


async def test_only_leading_label_wildcard_is_accepted(client):
    for hostname in ('*', '*amazonaws.com', 'ecs.*.amazonaws.com', '*.*.amazonaws.com', '*.amazonaws.com:443', '*.'):
        response = await client.put('/api/v1/rules/invalid', json={
            'priority': 10, 'vm_selector': {'type': 'all'}, 'hostname': hostname,
            'action': {'type': 'allow'},
        })
        assert response.status == 422, hostname
