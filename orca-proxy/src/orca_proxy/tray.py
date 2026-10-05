"""Desktop tray process and embedded management window."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import urllib.error
import urllib.request

from . import config


def _status() -> dict:
    base = f"http://127.0.0.1:{config.management_api_port()}"
    request = urllib.request.Request(f"{base}/api/v1/status")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            result = json.loads(response.read())
        try:
            with urllib.request.urlopen(f"{base}/readyz", timeout=1.5) as response:
                readiness = json.loads(response.read())
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            readiness = {"ready": False}
        result["ready"] = bool(readiness.get("ready"))
        return result
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return {"state": "failed", "ready": False, "proxy_error": "Jetty daemon is unreachable."}


def main() -> int:
    restriction = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
    try:
        if restriction.read_text(encoding="ascii").strip() == "1":
            # QtWebEngine cannot use its namespace sandbox under this Ubuntu
            # setting; the embedded page is restricted to Jetty's loopback UI.
            os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    except OSError:
        pass
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
        from PySide6.QtWidgets import (
            QApplication,
            QDialog,
            QMainWindow,
            QMenu,
            QPlainTextEdit,
            QSystemTrayIcon,
            QVBoxLayout,
        )
        from PySide6.QtWebEngineCore import QWebEnginePage
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError as exc:
        print(f"Jetty's desktop dependencies are unavailable: {exc}", file=sys.stderr)
        return 1

    class JettyPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            del navigation_type
            if (
                url.scheme() == "http"
                and url.host() in {"127.0.0.1", "localhost"}
                and url.port() == config.management_api_port()
            ):
                return True
            QDesktopServices.openUrl(url)
            return False

    class JettyWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Jetty")
            self.resize(1200, 800)
            self.view = QWebEngineView(self)
            self.view.setPage(JettyPage(self.view))
            self.setCentralWidget(self.view)
            self.open_url("/")

        def open_url(self, path: str):
            self.view.setUrl(QUrl(f"http://127.0.0.1:{config.management_api_port()}{path}"))
            self.show()
            self.raise_()
            self.activateWindow()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Jetty")
    app.setQuitOnLastWindowClosed(False)
    window = JettyWindow()

    def make_icon(color: str) -> QIcon:
        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor("transparent"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#11253c"))
        painter.setBrush(QColor(color))
        painter.drawEllipse(4, 4, 24, 24)
        painter.setPen(QColor("white"))
        painter.drawEllipse(10, 10, 12, 12)
        painter.end()
        return QIcon(pixmap)

    tray = QSystemTrayIcon(make_icon("#97a6ba"), app)
    menu = QMenu()
    # Owned by the app, not the menu: refresh() calls menu.clear(), which
    # deletes any action the menu owns.
    open_action = QAction("Open Jetty", app)
    setup_action = QAction("Open setup", app)
    restart_action = QAction("Restart proxy", app)
    logs_action = QAction("Show logs", app)
    quit_action = QAction("Quit tray", app)
    open_action.triggered.connect(lambda: window.open_url("/"))
    setup_action.triggered.connect(lambda: window.open_url("/setup"))
    restart_action.triggered.connect(
        lambda: subprocess.Popen(
            ["systemctl", "--user", "restart", "jetty-daemon.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    )

    def show_logs():
        result = subprocess.run(
            ["journalctl", "--user", "-u", "jetty-daemon.service", "-n", "100", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
        dialog = QDialog(window)
        dialog.setWindowTitle("Jetty service logs")
        dialog.resize(900, 560)
        editor = QPlainTextEdit(dialog)
        editor.setReadOnly(True)
        editor.setPlainText(result.stdout or result.stderr or "No service logs are available.")
        dialog.setLayout(QVBoxLayout())
        dialog.layout().addWidget(editor)
        dialog.exec()

    logs_action.triggered.connect(show_logs)
    quit_action.triggered.connect(lambda: (tray.hide(), app.quit()))

    def refresh():
        current = _status()
        state = current.get("state")
        if state == "running" and current.get("ready"):
            icon = make_icon("#21b66f")
            tooltip = "Jetty proxy is running"
            menu.clear()
            menu.addAction(open_action)
            menu.addSeparator()
            menu.addAction(quit_action)
        elif state in {"not_configured", "waiting_for_network"}:
            icon = make_icon("#97a6ba")
            tooltip = "Jetty needs setup" if state == "not_configured" else "Jetty is waiting for its network"
            menu.clear()
            menu.addAction(setup_action)
            menu.addAction(open_action)
            menu.addSeparator()
            menu.addAction(quit_action)
        else:
            icon = make_icon("#df4c59")
            tooltip = current.get("proxy_error") or "Jetty proxy is unavailable"
            menu.clear()
            menu.addAction(restart_action)
            menu.addAction(logs_action)
            menu.addAction(open_action)
            menu.addSeparator()
            menu.addAction(quit_action)
        tray.setIcon(icon)
        tray.setToolTip(tooltip[:127])

    menu.addAction(open_action)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: window.open_url("/") if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
    refresh()
    timer = QTimer(app)
    timer.timeout.connect(refresh)
    timer.start(3000)

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        # A .desktop launcher remains available even on sessions with no SNI host.
        window.show()
    if os.environ.get("JETTY_OPEN_SETUP") == "1":
        window.open_url("/setup")
    return app.exec()
