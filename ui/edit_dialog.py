# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Подтверждение замены в файле. Чтение файла идёт через сервис, не из виджета."""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from application import ApplicationService


class FileEditDialog(QDialog):
    _preview_done = Signal(object, object)
    _apply_done = Signal(object)

    def __init__(
        self,
        service: ApplicationService,
        session_id: str,
        path: str,
        old: str,
        new: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._session_id = session_id
        self._path = path
        self._old = old
        self._new = new
        self._busy = False
        self._can_apply = False
        self._preview_done.connect(self._show_preview)
        self._apply_done.connect(self._show_apply)

        self.setWindowTitle("Замена в файле")
        self.setModal(True)
        self.resize(520, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)
        self._status = QLabel("Проверяю, что фрагмент встречается в файле один раз…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._context = QTextEdit()
        self._context.setReadOnly(True)
        self._context.setVisible(False)
        layout.addWidget(self._context, 1)
        buttons = QDialogButtonBox()
        self._apply = QPushButton("Заменить")
        self._apply.setEnabled(False)
        self._apply.clicked.connect(self._on_apply)
        buttons.addButton(self._apply, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons

        self._busy = True
        threading.Thread(target=self._load, daemon=True).start()

    def reject(self) -> None:  # type: ignore[override]
        if self._busy:
            return
        super().reject()

    def _load(self) -> None:
        view = None
        error = None
        try:
            view = self._service.preview_file_edit(
                self._session_id, self._path, self._old, self._new
            )
        except Exception as exc:
            error = exc
        self._preview_done.emit(view, error)

    def _show_preview(self, view: object, error: object) -> None:
        self._busy = False
        if error is not None:
            self._status.setText(str(error) or "Файл не изменён.")
            return
        message = str(getattr(view, "message", ""))
        context = str(getattr(view, "context", ""))
        self._status.setText(message)
        self._can_apply = bool(getattr(view, "ok", False))
        self._apply.setEnabled(self._can_apply)
        if context:
            self._context.setPlainText(context)
            self._context.setVisible(True)

    def _on_apply(self) -> None:
        if not self._can_apply or self._busy:
            return
        self._busy = True
        self._apply.setEnabled(False)
        self._status.setText("Записываю замену…")

        def work() -> None:
            error = None
            try:
                self._service.apply_file_edit(
                    self._session_id, self._path, self._old, self._new
                )
            except Exception as exc:
                error = exc
            self._apply_done.emit(error)

        threading.Thread(target=work, daemon=True).start()

    def _show_apply(self, error: object) -> None:
        self._busy = False
        if error is not None:
            self._status.setText(str(error) or "Файл не изменён.")
            self._apply.setEnabled(False)
            return
        self._status.setText("Готово. Заменён только этот фрагмент.")
        self._apply.setEnabled(False)
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setText("Закрыть")
