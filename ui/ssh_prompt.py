# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Пароль SSH с рабочего потока: диалог показывается в окне программы."""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit


class _Bridge(QObject):
    request = Signal(object)


class QtSshPasswordPrompt:
    def __init__(self) -> None:
        self._bridge = _Bridge()
        self._bridge.request.connect(
            self._deliver, Qt.ConnectionType.QueuedConnection
        )

    def __call__(self, prompt: str) -> str:
        app = QApplication.instance()
        if app is None or QThread.currentThread() is app.thread():
            return self._ask(prompt)
        holder: dict = {}
        done = threading.Event()
        self._bridge.request.emit((prompt, holder, done))
        done.wait()
        return str(holder.get("password") or "")

    def _deliver(self, payload: object) -> None:
        prompt, holder, done = payload  # type: ignore[misc]
        try:
            holder["password"] = self._ask(prompt)
        finally:
            done.set()

    @staticmethod
    def _ask(prompt: str) -> str:
        text, ok = QInputDialog.getText(
            None,
            "Пароль SSH",
            prompt,
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return ""
        return text
