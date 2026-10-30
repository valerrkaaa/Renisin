# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Русские подписи стандартных кнопок Qt."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QMessageBox, QWidget

_BOX = {
    QDialogButtonBox.StandardButton.Ok: "ОК",
    QDialogButtonBox.StandardButton.Cancel: "Отмена",
    QDialogButtonBox.StandardButton.Close: "Закрыть",
    QDialogButtonBox.StandardButton.Save: "Сохранить",
    QDialogButtonBox.StandardButton.Yes: "Да",
    QDialogButtonBox.StandardButton.No: "Нет",
    QDialogButtonBox.StandardButton.Apply: "Применить",
}

_MESSAGE = {
    QMessageBox.StandardButton.Ok: "ОК",
    QMessageBox.StandardButton.Cancel: "Отмена",
    QMessageBox.StandardButton.Close: "Закрыть",
    QMessageBox.StandardButton.Save: "Сохранить",
    QMessageBox.StandardButton.Yes: "Да",
    QMessageBox.StandardButton.No: "Нет",
    QMessageBox.StandardButton.Apply: "Применить",
}


def russian_box(box: QDialogButtonBox) -> QDialogButtonBox:
    for standard, text in _BOX.items():
        button = box.button(standard)
        if button is not None and button.text() != text:
            button.setText(text)
    return box


def russian_message(box: QMessageBox) -> None:
    for standard, text in _MESSAGE.items():
        button = box.button(standard)
        if button is not None and button.text() != text:
            button.setText(text)
    box.adjustSize()


class ButtonLocalizer(QObject):
    """Подменяет Yes/Close и остальные стандартные подписи перед показом окна."""

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.Show and isinstance(watched, QWidget):
            if isinstance(watched, QMessageBox):
                russian_message(watched)
            elif isinstance(watched, QDialogButtonBox):
                russian_box(watched)
            elif isinstance(watched, QDialog):
                for box in watched.findChildren(QDialogButtonBox):
                    russian_box(box)
        return False
