# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Диалог проверки ключа хоста и реализация HostKeyVerifier."""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

import domain


class HostKeyDialog(QDialog):
    def __init__(
        self,
        prompt: domain.HostKeyPrompt,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Проверка ключа хоста")
        self.setModal(True)
        self.resize(560, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        if prompt.is_changed:
            text = (
                "ВНИМАНИЕ: ключ хоста ИЗМЕНИЛСЯ по сравнению с сохранённым. "
                "Это может указывать на атаку «человек посередине» либо на "
                "переустановку сервера. Продолжайте только если уверены."
            )
            color = "#ff8a80"
        elif prompt.is_known:
            text = (
                "Хост известен, но предлагает новый тип ключа. "
                "Убедитесь, что это ожидаемо."
            )
            color = "#ffd166"
        else:
            text = (
                "Первое подключение к этому хосту. Убедитесь, что отпечаток "
                "совпадает с ожидаемым."
            )
            color = "#ffd166"

        warn = QLabel(text)
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{color}; font-weight:600;")
        layout.addWidget(warn)

        form = QFormLayout()
        form.addRow("Хост", QLabel(f"{prompt.host}:{prompt.port}"))
        form.addRow("Тип ключа", QLabel(prompt.key_type))
        form.addRow("Отпечаток", QLabel(prompt.fingerprint))
        if prompt.old_fingerprint:
            form.addRow("Было", QLabel(prompt.old_fingerprint))
        layout.addLayout(form)

        layout.addStretch(1)

        self._remember = QCheckBox("Запомнить этот ключ")
        self._remember.setChecked(True)
        layout.addWidget(self._remember)

        buttons = QDialogButtonBox()
        accept = buttons.addButton("Принять", QDialogButtonBox.ButtonRole.AcceptRole)
        reject = buttons.addButton("Отмена", QDialogButtonBox.ButtonRole.RejectRole)
        accept.clicked.connect(self.accept)
        reject.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def remember(self) -> bool:
        return self._remember.isChecked()


class _HostKeyBridge(QObject):
    request = Signal(object)


class QtHostKeyVerifier:
    """Диалог ключа хоста. Вызов с рабочего потока переносится в GUI-поток."""

    def __init__(self) -> None:
        self._bridge = _HostKeyBridge()
        self._bridge.request.connect(
            self._deliver, Qt.ConnectionType.QueuedConnection
        )

    def __call__(
        self, prompt: domain.HostKeyPrompt
    ) -> domain.HostKeyDecision:
        app = QApplication.instance()
        if app is None or QThread.currentThread() is app.thread():
            return self._ask(prompt)
        holder: dict = {}
        done = threading.Event()
        self._bridge.request.emit((prompt, holder, done))
        done.wait()
        decision = holder.get("decision")
        if isinstance(decision, domain.HostKeyDecision):
            return decision
        return domain.HostKeyDecision(accept=False)

    def _deliver(self, payload: object) -> None:
        prompt, holder, done = payload  # type: ignore[misc]
        try:
            holder["decision"] = self._ask(prompt)
        finally:
            done.set()

    def _ask(self, prompt: domain.HostKeyPrompt) -> domain.HostKeyDecision:
        dialog = HostKeyDialog(prompt)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return domain.HostKeyDecision(
                accept=True, remember=dialog.remember()
            )
        return domain.HostKeyDecision(accept=False)