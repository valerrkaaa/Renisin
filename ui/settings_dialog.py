# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Настройки приложения: адрес MCP, модели и правила команд."""
from __future__ import annotations

import ipaddress
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from application import ApplicationService
from mcp_host import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORTS,
    parse_mcp_host,
    parse_mcp_ports,
)
from ui.models_dialog import ModelsDialog
from ui.rules_dialog import RulesDialog


class SettingsDialog(QDialog):
    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.resize(920, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        tabs = QTabWidget()
        tabs.addTab(self._mcp_page(), "MCP")
        tabs.addTab(ModelsDialog(service, self), "Модели")
        tabs.addTab(RulesDialog(service, self), "Правила")
        layout.addWidget(tabs, 1)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)
        self._fill_mcp()

    def _mcp_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(10)

        hint = QLabel(
            "Программа занимает один свободный порт из списка. "
            "Если он занят другим окном, берётся следующий. "
            "Все консоли ходят на этот один адрес, поэтому число портов "
            "не ограничивает число сессий."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;")
        layout.addWidget(hint)

        form = QFormLayout()
        self._host = QLineEdit()
        self._host.setObjectName("mcpHost")
        self._host.setPlaceholderText(DEFAULT_MCP_HOST)
        self._host.textChanged.connect(self._refresh_exposure)
        form.addRow("IP", self._host)
        self._ports = QLineEdit()
        self._ports.setObjectName("mcpPorts")
        self._ports.setPlaceholderText(DEFAULT_MCP_PORTS)
        form.addRow("Порты", self._ports)
        layout.addLayout(form)

        self._exposure = QLabel(
            "Этот адрес доступен в сети, не только на этом компьютере."
        )
        self._exposure.setWordWrap(True)
        self._exposure.setStyleSheet("color:#ffd166;")
        self._exposure.setVisible(False)
        layout.addWidget(self._exposure)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setObjectName("mcpStatus")
        self._status.setStyleSheet("color:#8b949e;")
        layout.addWidget(self._status)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setObjectName("mcpError")
        self._error.setStyleSheet("color:#fb7185;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        apply = QPushButton("Применить")
        apply.setObjectName("primaryButton")
        apply.clicked.connect(self._apply_mcp)
        layout.addWidget(apply)
        layout.addStretch(1)
        return page

    def _fill_mcp(self) -> None:
        reader = getattr(self._service, "mcp_listen", None)
        host, ports = (DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS)
        if callable(reader):
            loaded = reader()
            if loaded and len(loaded) == 2:
                host, ports = loaded
        self._host.setText(host)
        self._ports.setText(ports)
        endpoint = ""
        getter = getattr(self._service, "mcp_endpoint", None)
        if callable(getter):
            endpoint = getter() or ""
        self._status.setText(self._status_text(endpoint))
        self._refresh_exposure()

    def _apply_mcp(self) -> None:
        try:
            host = parse_mcp_host(self._host.text())
            ports_text = self._ports.text().strip()
            ports = parse_mcp_ports(ports_text)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        updater = getattr(self._service, "update_mcp_listen", None)
        if not callable(updater):
            self._show_error("MCP-сервер не подключён")
            return
        try:
            endpoint = updater(host, ports, ports_text)
        except Exception as exc:
            self._show_error(str(exc) or "Не удалось занять порт MCP")
            return
        self._host.setText(host)
        self._ports.setText(ports_text)
        self._show_error("")
        self._status.setText(self._status_text(endpoint))
        self._refresh_exposure()

    def _refresh_exposure(self) -> None:
        visible = False
        try:
            host = parse_mcp_host(self._host.text())
            visible = not ipaddress.ip_address(host).is_loopback
        except ValueError:
            visible = False
        self._exposure.setVisible(visible)

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(bool(message))

    @staticmethod
    def _status_text(endpoint: str) -> str:
        if not endpoint:
            return "Сервер MCP сейчас не слушает порт."
        return (
            f"Слушает {endpoint}. Адрес конкретной консоли — в её настройках. "
            "После смены порта обновите его в клиенте MCP."
        )
