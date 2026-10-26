# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Настройки обычной сессии: имя, имя MCP, описание и адрес."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

import domain


class SessionSettingsDialog(QDialog):
    def __init__(
        self,
        info: domain.SessionInfo,
        mcp_url: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Настройки сессии")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit(info.name)
        form.addRow("Имя", self._name)
        self._alias = QLineEdit(info.mcp_alias.strip() or info.name)
        self._alias.setPlaceholderText(info.name)
        form.addRow("Имя MCP", self._alias)
        self._description = QPlainTextEdit(info.mcp_description)
        self._description.setPlaceholderText("Что это за терминал")
        self._description.setFixedHeight(72)
        form.addRow("Описание", self._description)
        self._shell = QComboBox()
        self._shell.addItem("linux/macOS", domain.SHELL_UNIX)
        self._shell.addItem("windows", domain.SHELL_WINDOWS)
        self._shell.addItem("other", domain.SHELL_OTHER)
        selected = self._shell.findData(domain.normalize_shell_family(info.shell_family))
        self._shell.setCurrentIndex(selected if selected >= 0 else 0)
        self._shell.setToolTip(
            "Как делить команду в запросе. В терминал уходит исходный текст."
        )
        form.addRow("ОС команд", self._shell)
        self._os = QLineEdit(info.os_name)
        self._os.setPlaceholderText("Определится при запуске терминала")
        form.addRow("ОС", self._os)
        self._ips = QLineEdit(info.ip_addresses)
        self._ips.setPlaceholderText("Определятся при запуске терминала")
        form.addRow("IP", self._ips)
        self._username = QLineEdit(info.username)
        self._username.setPlaceholderText("Определится при запуске терминала")
        form.addRow("Username", self._username)
        self._url = QLineEdit(mcp_url or "Появится после выбора модели")
        self._url.setReadOnly(True)
        form.addRow("Адрес MCP", self._url)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def session_name(self) -> str:
        return self._name.text()

    def mcp_alias(self) -> str:
        return self._alias.text()

    def mcp_description(self) -> str:
        return self._description.toPlainText()

    def shell_family(self) -> str:
        return str(self._shell.currentData() or domain.SHELL_UNIX)

    def os_name(self) -> str:
        return self._os.text()

    def ip_addresses(self) -> str:
        return self._ips.text()

    def username(self) -> str:
        return self._username.text()
