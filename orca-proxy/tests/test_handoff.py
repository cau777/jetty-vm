import shutil
import subprocess

import pytest

from orca_proxy import handoff


@pytest.fixture
def launched(tmp_path, monkeypatch):
    """Fake a home folder, a single terminal and no systemd-run; record the spawn argv."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", "/bin/bash")
    real_which = shutil.which
    monkeypatch.setattr(
        handoff.shutil,
        "which",
        lambda name: "/usr/bin/ptyxis" if name == "ptyxis" else None if name in {"systemd-run"} or name in dict(handoff._TERMINALS) else real_which(name),
    )
    calls = []
    monkeypatch.setattr(handoff.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)))
    return home, calls


async def _register(client, name="proj-vm", ip="10.202.0.21"):
    resp = await client.put(f"/api/v1/vms/{name}", json={"ip_address": ip})
    assert resp.status == 201


async def test_handoff_opens_terminal_and_installs_missing_skill(client, launched):
    home, calls = launched
    await _register(client)
    resp = await client.post("/api/v1/jetty/vms/proj-vm/handoff", json={"agent": "claude"})
    body = await resp.json()
    assert resp.status == 202
    assert body["skill_install"] is True
    assert body["terminal"] == "ptyxis"
    assert body["project_dir"] == str(home)
    argv = calls[0][0]
    assert argv[:3] == ["/usr/bin/ptyxis", "--", "/bin/bash"]
    # project dir, install flag, skill source, skills agent id, agent command, prompt
    assert argv[6:9] == [str(home), "1", str(home / ".local/share/jetty/orca-ssh-setup")]
    assert argv[9:11] == ["claude-code", "claude"]
    assert "'proj-vm'" in argv[11] and "10.202.0.21" in argv[11]
    assert (home / ".local/share/jetty/orca-ssh-setup/SKILL.md").is_file()


async def test_handoff_skips_install_when_agent_has_current_skill(client, launched):
    home, calls = launched
    await _register(client)
    shutil.copytree(handoff._bundled_skill(), home / ".agents/skills/orca-ssh-setup")
    project = home / "code"
    project.mkdir()
    resp = await client.post(
        "/api/v1/jetty/vms/proj-vm/handoff", json={"agent": "opencode", "project_dir": str(project)}
    )
    assert resp.status == 202
    assert (await resp.json())["skill_install"] is False
    argv = calls[0][0]
    assert argv[6:8] == [str(project), "0"]
    assert argv[9:12] == ["opencode", "opencode", "--prompt"]


async def test_handoff_reinstalls_outdated_skill(client, launched):
    home, _calls = launched
    await _register(client)
    stale = home / ".claude/skills/orca-ssh-setup"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("old version\n")
    resp = await client.post("/api/v1/jetty/vms/proj-vm/handoff", json={"agent": "claude"})
    assert (await resp.json())["skill_install"] is True


@pytest.mark.parametrize(
    ("payload", "status", "field"),
    [
        ({"agent": "cursor"}, 422, "agent"),
        ({"agent": "pi", "project_dir": "relative/path"}, 422, "project_dir"),
        ({"agent": "pi", "project_dir": "/definitely/missing"}, 422, "project_dir"),
    ],
)
async def test_handoff_rejects_bad_input(client, launched, payload, status, field):
    await _register(client)
    resp = await client.post("/api/v1/jetty/vms/proj-vm/handoff", json=payload)
    body = await resp.json()
    assert resp.status == status
    assert field in body["error"]["fields"]
    assert launched[1] == []


async def test_handoff_unknown_vm_404(client, launched):
    resp = await client.post("/api/v1/jetty/vms/nope/handoff", json={"agent": "codex"})
    assert resp.status == 404


def test_script_runs_agent_with_prompt_in_project_dir(tmp_path):
    """Run the terminal script itself, with a stand-in agent that records its cwd and args."""
    agent = tmp_path / "fake-agent"
    agent.write_text('#!/bin/sh\npwd > "$OUT"\nprintf "%s\\n" "$@" >> "$OUT"\n')
    agent.chmod(0o755)
    out = tmp_path / "out.txt"
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["/bin/bash", "-c", handoff._SCRIPT, "jetty-handoff", str(project), "0", "unused", "unused",
         str(agent), "--prompt", "set up my VM"],
        check=True,
        env={"OUT": str(out), "PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert out.read_text().splitlines() == [str(project), "--prompt", "set up my VM"]
