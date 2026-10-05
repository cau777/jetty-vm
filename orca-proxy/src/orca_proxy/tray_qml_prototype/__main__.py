"""PROTOTYPE — throwaway, lives only on the prototype/qml-tray branch.

Question: does a native Qt Quick (QML) UI feel good enough to replace the
QtWebEngine window, and how big is the bundle without QtWebEngine?

Three structurally different variants of the VMs screen, switchable with the
bottom bar, the Left/Right keys, or --variant A|B|C:
  A  Console       sidebar + table + inspector, create in a side sheet
  B  Board         VM cards, create in a modal with a big progress view
  C  Tray popover  narrow list, everything expands inline

The VM list is read live (GET only) from the running daemon, falling back to
sample data. VM creation and agent handoff are stubs: creation is a timed
fake job (a name containing "fail" fails at cloud-init), handoff only reports
what it would do.

Run: uv run python -m orca_proxy.tray_qml_prototype [--variant B]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from PySide6.QtCore import Property, QMetaObject, QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

API = f"http://127.0.0.1:{os.environ.get('ORCA_PROXY_MANAGEMENT_PORT', '8080')}"
VARIANTS = ["A", "B", "C"]
AGENTS = [
    {"key": "claude", "label": "Claude Code"},
    {"key": "codex", "label": "Codex"},
    {"key": "pi", "label": "Pi"},
    {"key": "opencode", "label": "OpenCode"},
]
SAMPLE = [
    {"name": "jetty-agent-harness", "ip_address": "10.202.0.11", "status": "Running", "rules": 4},
    {"name": "jetty-sales-leads", "ip_address": "10.202.0.14", "status": "Stopped", "rules": 4},
    {"name": "orca-oauth-mock", "ip_address": "10.202.0.17", "status": "Running", "rules": 4},
]
# (label, seconds) — roughly the real create path, compressed.
STEPS = [
    ("Validating request", 0.4),
    ("Pulling ubuntu:24.04 image", 2.5),
    ("Creating instance", 1.0),
    ("Booting VM", 1.5),
    ("Waiting for the LXD guest agent", 1.5),
    ("Running cloud-init", 2.5),
    ("Registering with the proxy", 0.5),
]


class Backend(QObject):
    variantChanged = Signal()
    vmsChanged = Signal()
    jobChanged = Signal()

    def __init__(self, variant: str):
        super().__init__()
        self._variant = variant
        self._vms: list[dict] = []
        self._extra: list[dict] = []
        self._live = False
        self._proxy = "unknown"
        self._job: dict = {"state": "idle"}
        self._t = 0.0
        self._poll = QTimer(self, interval=3000, timeout=self.refresh)
        self._poll.start()
        self._tick = QTimer(self, interval=100, timeout=self._advance)
        self.refresh()

    # --- variant ---
    def _get_variant(self):
        return self._variant

    def _set_variant(self, value):
        if value in VARIANTS and value != self._variant:
            self._variant = value
            self.variantChanged.emit()

    variant = Property(str, _get_variant, _set_variant, notify=variantChanged)

    @Slot(int)
    def cycle(self, step):
        self._set_variant(VARIANTS[(VARIANTS.index(self._variant) + step) % len(VARIANTS)])

    # --- live, read-only data ---
    def _fetch(self, path):
        with urllib.request.urlopen(API + path, timeout=1) as response:
            return json.loads(response.read())

    @Slot()
    def refresh(self):
        try:
            vms = self._fetch("/api/v1/jetty/vms")["vms"]
            rules = self._fetch("/api/v1/rules")["rules"]
            self._proxy = self._fetch("/api/v1/status").get("state", "unknown")
            for vm in vms:
                vm["rules"] = sum(
                    1 for r in rules if r["vm_selector"]["type"] == "all" or vm["name"] in r["vm_selector"].get("vms", [])
                )
            self._live = True
        except Exception:
            vms, self._live, self._proxy = [dict(v) for v in SAMPLE], False, "unreachable"
        self._vms = self._extra + sorted(vms, key=lambda v: v["name"])
        self.vmsChanged.emit()

    vms = Property("QVariantList", lambda self: self._vms, notify=vmsChanged)
    live = Property(bool, lambda self: self._live, notify=vmsChanged)
    proxyState = Property(str, lambda self: self._proxy, notify=vmsChanged)
    agents = Property("QVariantList", lambda self: AGENTS, constant=True)
    homeDir = Property(str, lambda self: str(Path.home()), constant=True)

    # --- stubbed create job ---
    job = Property("QVariantMap", lambda self: self._job, notify=jobChanged)

    @Slot(str, int, str, str)
    def startCreate(self, name, cpus, memory, disk):
        self._t = 0.0
        self._job = {
            "state": "running", "name": name or "new-vm", "cpus": cpus, "memory": memory, "disk": disk,
            "step": 0, "progress": 0.0, "elapsed": 0.0, "error": "", "ip": "",
            "steps": [{"label": label, "state": "pending"} for label, _ in STEPS],
            "log": [f"POST /api/v1/jetty/vms {{name: {name}, cpus: {cpus}, memory: {memory}, disk: {disk}}}"],
        }
        self._job["steps"][0]["state"] = "running"
        self._tick.start()
        self.jobChanged.emit()

    def _advance(self):
        job = self._job
        self._t += 0.1
        total = sum(d for _, d in STEPS)
        bounds, acc = [], 0.0
        for _, duration in STEPS:
            acc += duration
            bounds.append(acc)
        index = next((i for i, b in enumerate(bounds) if self._t < b), len(STEPS))
        fail_at = 5 if "fail" in job["name"] else None
        if fail_at is not None and index >= fail_at:
            job["steps"][fail_at]["state"] = "failed"
            job["state"] = "failed"
            job["error"] = "cloud-init status --wait exited 2 (recoverable errors). The VM exists but setup did not finish."
            job["log"].append("ERROR " + job["error"])
            self._tick.stop()
        elif index >= len(STEPS):
            for s in job["steps"]:
                s["state"] = "done"
            job["state"] = "done"
            job["ip"] = f"10.202.0.{18 + len(self._extra)}"
            job["log"].append(f"201 Created {{name: {job['name']}, ip_address: {job['ip']}, status: Running}}")
            self._extra.insert(0, {"name": job["name"], "ip_address": job["ip"], "status": "Running", "rules": 0, "fresh": True})
            self._tick.stop()
            self.refresh()
        elif index != job["step"]:
            job["steps"][job["step"]]["state"] = "done"
            job["steps"][index]["state"] = "running"
            job["log"].append(f"[{self._t:4.1f}s] {STEPS[index][0]}")
            job["step"] = index
        job["progress"] = min(self._t / total, 1.0)
        job["elapsed"] = round(self._t, 1)
        self._job = dict(job)
        self.jobChanged.emit()

    @Slot()
    def dismissJob(self):
        self._tick.stop()
        self._job = {"state": "idle"}
        self.jobChanged.emit()

    # --- stubbed handoff ---
    @Slot(str, str, str, result=str)
    def handOff(self, vm, agent, folder):
        label = next(a["label"] for a in AGENTS if a["key"] == agent)
        return f"Would open ptyxis in {folder or Path.home()}, refresh the skill for {label}, then start it on {vm}."

    @Slot(QUrl, result=str)
    def localPath(self, url):
        return url.toLocalFile()


def _screenshots(engine, backend, out: Path):
    """Self-check mode: grab each variant idle, mid-create and done, then quit."""
    window = engine.rootObjects()[0]
    plan = []
    for v in os.environ.get("PROTO_SHOT_VARIANTS", "".join(VARIANTS)):
        plan += [
            (0, lambda v=v: backend._set_variant(v)),
            (900, lambda v=v: window.grabWindow().save(str(out / f"{v}-1-list.png"))),
            (0, lambda: QMetaObject.invokeMethod(window, "openCreate")),
            (500, lambda v=v: window.grabWindow().save(str(out / f"{v}-2-form.png"))),
            (0, lambda: backend.startCreate("demo-vm", 4, "8GiB", "40GiB")),
            (4500, lambda v=v: window.grabWindow().save(str(out / f"{v}-3-progress.png"))),
            (6500, lambda v=v: window.grabWindow().save(str(out / f"{v}-4-done.png"))),
            (0, lambda: (QMetaObject.invokeMethod(window, "closeCreate"), backend.dismissJob(), backend._extra.clear(), backend.refresh())),
        ]
    plan.append((300, QGuiApplication.quit))

    def run(i=0):
        if i < len(plan):
            wait, action = plan[i]
            QTimer.singleShot(wait, lambda: (action(), run(i + 1)))

    run()


def main() -> int:
    parser = argparse.ArgumentParser(description="PROTOTYPE: Jetty VMs screen in QML")
    parser.add_argument("--variant", choices=VARIANTS, default="A")
    args = parser.parse_args()
    QQuickStyle.setStyle("Material")
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("Jetty QML prototype")
    backend = Backend(args.variant)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(QUrl.fromLocalFile(str(Path(__file__).with_name("Main.qml"))))
    if not engine.rootObjects():
        return 1
    shots = os.environ.get("PROTO_SHOT_DIR")
    if shots:
        Path(shots).mkdir(parents=True, exist_ok=True)
        _screenshots(engine, backend, Path(shots))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
