"""Native Qt Quick desktop window and system tray process."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

def main() -> int:
    try:
        from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuickControls2 import QQuickStyle
        from PySide6.QtWidgets import QApplication, QDialog, QMenu, QPlainTextEdit, QSystemTrayIcon, QVBoxLayout
        from .desktop import DesktopBackend
    except ImportError as exc:
        print(f"Jetty's desktop dependencies are unavailable: {exc}", file=sys.stderr)
        return 1

    QQuickStyle.setStyle("Material")
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Jetty")
    app.setOrganizationName("Jetty")
    app.setQuitOnLastWindowClosed(False)

    backend = DesktopBackend(force_setup=os.environ.get("JETTY_OPEN_SETUP") == "1")
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("backend", backend)
    qml_path = Path(__file__).parent / "qml" / "Main.qml"
    engine.load(str(qml_path))
    if not engine.rootObjects():
        print(f"Could not load Jetty's native UI from {qml_path}.", file=sys.stderr)
        return 1
    window = engine.rootObjects()[0]

    def show_window():
        window.show()
        window.raise_()
        window.requestActivate()

    def show_setup():
        backend.showSetup()
        show_window()

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
    # Keep actions owned by QApplication: menu.clear() deletes menu-owned actions.
    open_action = QAction("Open Jetty", app)
    setup_action = QAction("Open setup", app)
    restart_action = QAction("Restart proxy", app)
    logs_action = QAction("Show logs", app)
    quit_action = QAction("Quit tray", app)
    open_action.triggered.connect(show_window)
    setup_action.triggered.connect(show_setup)
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

    def refresh_menu():
        if backend.systemState == "running" and backend.ready:
            color = "#21b66f"
            tooltip = "Jetty proxy is running"
            actions = [open_action, None, quit_action]
        elif backend.needsSetup:
            color = "#97a6ba"
            tooltip = "Jetty needs setup" if backend.systemState == "not_configured" else "Jetty is waiting for its network"
            actions = [setup_action, open_action, None, quit_action]
        else:
            color = "#df4c59"
            tooltip = backend.statusMessage or "Jetty proxy is unavailable"
            actions = [restart_action, logs_action, open_action, None, quit_action]
        menu.clear()
        for action in actions:
            if action is None:
                menu.addSeparator()
            else:
                menu.addAction(action)
        tray.setIcon(make_icon(color))
        tray.setToolTip(tooltip[:127])

    backend.statusChanged.connect(refresh_menu)
    menu.addAction(open_action)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: show_window()
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )
    refresh_menu()

    if QSystemTrayIcon.isSystemTrayAvailable():
        tray.show()
    else:
        # Keep the management window accessible when the desktop has no SNI host.
        show_window()
    if os.environ.get("JETTY_OPEN_SETUP") == "1":
        show_setup()
    return app.exec()
