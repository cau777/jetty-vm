async def test_readyz_ready_after_startup(client):
    resp = await client.get("/readyz")
    body = await resp.json()
    assert resp.status == 200
    assert body["ready"] is True
    assert body["checks"] == {"migrations": True, "ca_materialized": True, "tunnel_keys": True}


async def test_readyz_unready_when_tunnel_keys_are_readable_by_others(client, tmp_path):
    (tmp_path / "wireguard.json").chmod(0o644)
    resp = await client.get("/readyz")
    body = await resp.json()
    assert resp.status == 503
    assert body["checks"]["tunnel_keys"] is False
