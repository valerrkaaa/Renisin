# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Composition root."""
from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from application import ApplicationService
from drivers import DriverFactory, RoutingLlmClient
from mcp_host import TerminalMcpServer, parse_mcp_ports
from storage import (
    EncryptedJournalRepository,
    FileCommandAllowStore,
    FileCommandRuleStore,
    FileLlmProfileStore,
    FileLoungeStore,
    FileMcpSettings,
    FileSessionCatalog,
)
from ui import MainWindow
from ui.hostkey import QtHostKeyVerifier
from ui.styles import VibeStyle


def _bundle_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def app_icon() -> QIcon:
    """Иконка окна, панели задач и трея из assets/icons."""
    root = os.path.join(_bundle_dir(), "assets", "icons")
    icon = QIcon(os.path.join(root, "vterminal.ico"))
    for name, side in (
        ("favicon-16x16.png", 16),
        ("favicon-32x32.png", 32),
        ("favicon-96x96.png", 96),
        ("favicon-192x192.png", 192),
        ("favicon-512x512.png", 512),
    ):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            icon.addFile(path, QSize(side, side))
    return icon


def _install_tray(app: QApplication, window, icon: QIcon) -> None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return
    tray = QSystemTrayIcon(icon, window)
    tray.setToolTip("vterminal")
    menu = QMenu()
    open_action = menu.addAction("Открыть")
    open_action.triggered.connect(lambda: _present(window))
    quit_action = menu.addAction("Выход")
    quit_action.triggered.connect(app.quit)
    tray.setContextMenu(menu)

    def on_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            _present(window)

    tray.activated.connect(on_activated)
    tray.show()
    window._tray = tray


def _present(window) -> None:
    window.showNormal()
    window.raise_()
    window.activateWindow()


def get_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~/.local/share")
    return os.path.join(base, "vibe_terminal")


def main() -> int:
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "vibe_terminal.vterminal"
        )
    app = QApplication(sys.argv)
    app.setApplicationName("vterminal")
    icon = app_icon()
    app.setWindowIcon(icon)
    app.setStyle(VibeStyle("Fusion"))
    ui_font = QFont("Segoe UI")
    ui_font.setPointSize(10)
    app.setFont(ui_font)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#101114"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f4f4f5"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#1b1c22"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#17181d"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#fafafa"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#23242a"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#fafafa"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#3f3a66"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#23242a"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#fafafa"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#71717a"))
    app.setPalette(palette)

    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    journals_dir = os.path.join(data_dir, "journals")
    os.makedirs(journals_dir, exist_ok=True)

    catalog = FileSessionCatalog(os.path.join(data_dir, "sessions.json"))
    journal = EncryptedJournalRepository(journals_dir)
    llm_profiles = FileLlmProfileStore(os.path.join(data_dir, "llm_profiles.json"))
    command_allow = FileCommandAllowStore(os.path.join(data_dir, "mcp_allow.json"))
    command_rules = FileCommandRuleStore(os.path.join(data_dir, "command_rules.json"))
    mcp_settings = FileMcpSettings(os.path.join(data_dir, "mcp.json"))
    listen_host, mcp_ports_text = mcp_settings.load()

    host_key_verifier = QtHostKeyVerifier()
    factory = DriverFactory(
        host_key_verifier=host_key_verifier,
        known_hosts_path=os.path.join(data_dir, "known_hosts"),
    )

    service = ApplicationService(
        driver_factory=factory,
        catalog=catalog,
        journal=journal,
        llm_profiles=llm_profiles,
        llm_client=RoutingLlmClient(),
        command_allow=command_allow,
        command_rules=command_rules,
        lounge_store=FileLoungeStore(os.path.join(data_dir, "lounge.json")),
        mcp_settings=mcp_settings,
    )
    try:
        service.attach_mcp(
            TerminalMcpServer(service, listen_host, parse_mcp_ports(mcp_ports_text))
        )
    except OSError:
        pass

    window = MainWindow(service)
    window.setWindowIcon(icon)
    _install_tray(app, window, icon)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())