async def test_readyz_ready_after_startup(client):
    resp = await client.get("/readyz")
    body = await resp.json()
    assert resp.status == 200
    assert body["ready"] is True
    assert body["checks"] == {
        "migrations": True,
        "ca_materialized": True,
        # No VMs registered yet — vacuously synced (#12's Q7), no real
        # capability-carrying iptables call needed to reach "ready" on a
        # fresh install.
        "firewall_synced": True,
    }
    assert body["firewall_status"] == {}


async def test_readyz_unsynced_when_firewall_sync_fails(client, app):
    app["firewall_sync"]._status = {"vm-a": "error"}
    resp = await client.get("/readyz")
    body = await resp.json()
    assert resp.status == 503
    assert body["ready"] is False
    assert body["checks"]["firewall_synced"] is False
