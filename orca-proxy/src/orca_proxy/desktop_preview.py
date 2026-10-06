"""Capture offline screenshots of the native desktop screens and job states."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from .desktop import DesktopBackend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture offline screenshots of Jetty's native UI")
    parser.add_argument("--output", type=Path, default=Path("/tmp/jetty-qml-screenshots"))
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)

    QQuickStyle.setStyle("Material")
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("Jetty UI self-check")
    backend = DesktopBackend(demo=True)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    engine.load(str(Path(__file__).parent / "qml" / "Main.qml"))
    if not engine.rootObjects():
        return 1
    window = engine.rootObjects()[0]
    window.show()
    captured: list[str] = []

    def capture(name: str) -> None:
        path = args.output / f"{name}.png"
        if not window.grabWindow().save(str(path)):
            raise RuntimeError(f"Could not save screenshot {path}")
        captured.append(str(path))

    def schedule(delay_ms: int, callback) -> None:
        QTimer.singleShot(delay_ms, callback)

    plan = [
        (400, lambda: capture("01-vms")),
        (250, lambda: (backend.setView("rules"), backend.selectItem("rules", "github"))),
        (350, lambda: capture("02-rules")),
        (250, lambda: (backend.setView("credentials"), backend.selectItem("credentials", "claude-code-subscription"))),
        (350, lambda: capture("03-credentials")),
        (250, lambda: (backend.setView("logs"), backend.selectItem("logs", "42"))),
        (400, lambda: capture("04-logs")),
        (250, lambda: (backend.setView("vms"), backend.openEditor("vms", ""))),
        (400, lambda: capture("05-create-form")),
        (100, lambda: backend.startCreate({"name": "demo-vm", "cpus": 4, "memory": "8GiB", "disk": "40GiB", "image": "ubuntu:24.04"})),
        (850, lambda: capture("06-create-progress")),
        (2100, lambda: capture("07-create-success")),
        (100, backend.dismissJob),
        (100, lambda: backend.startCreate({"name": "demo-fail", "cpus": 4, "memory": "8GiB", "disk": "40GiB", "image": "ubuntu:24.04"})),
        (2400, lambda: capture("08-create-failure")),
        (100, backend.closeEditor),
        (100, backend.showSetup),
        (350, lambda: capture("09-first-run-setup")),
        (200, app.quit),
    ]

    def run(index: int = 0) -> None:
        if index >= len(plan):
            return
        delay, callback = plan[index]

        def step():
            callback()
            run(index + 1)

        schedule(delay, step)

    run()
    result = app.exec()
    print("\n".join(captured))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
