import copy

import pytest

from orca_proxy.jetty import FREE_PAGE_REPORTING, JettyLxd
from orca_proxy.lxd import LxdError


class _ProfileClient:
    def __init__(self, profile, *, put_error=None, save_on_error=True):
        self.profile = profile
        self.puts = []
        self.put_error = put_error
        self.save_on_error = save_on_error

    async def request(self, method, path, body=None, **_kwargs):
        assert path == "/1.0/profiles/default"
        if method == "PUT":
            self.puts.append(body)
            if self.put_error is None or self.save_on_error:
                self.profile = body
            if self.put_error is not None:
                raise self.put_error
        return copy.deepcopy(self.profile)


async def test_profile_enables_free_page_reporting_once():
    client = _ProfileClient({"config": {}, "devices": {"root": {"type": "disk", "pool": "default", "path": "/"}}})
    service = JettyLxd(None)
    await service._ensure_profile(client)
    await service._ensure_profile(client)

    assert len(client.puts) == 1
    assert client.profile["config"]["raw.qemu.conf"] == FREE_PAGE_REPORTING


async def test_profile_keeps_an_existing_qemu_override():
    custom = '[device "qemu_balloon"]\nfree-page-reporting = "off"\n'
    client = _ProfileClient({"config": {"raw.qemu.conf": custom}, "devices": {}})
    await JettyLxd(None)._ensure_profile(client)

    assert client.profile["config"]["raw.qemu.conf"] == custom
    assert "root" in client.profile["devices"]


async def test_profile_tolerates_running_vms_rejecting_the_live_update():
    error = LxdError('Instance: jetty-gw: Key "raw.qemu.conf" cannot be updated when VM is running')
    client = _ProfileClient({"config": {}, "devices": {"root": {}}}, put_error=error)
    await JettyLxd(None)._ensure_profile(client)

    assert client.profile["config"]["raw.qemu.conf"] == FREE_PAGE_REPORTING


async def test_profile_raises_when_lxd_did_not_save_it():
    client = _ProfileClient({"config": {}, "devices": {"root": {}}}, put_error=LxdError("denied"), save_on_error=False)

    with pytest.raises(LxdError):
        await JettyLxd(None)._ensure_profile(client)
