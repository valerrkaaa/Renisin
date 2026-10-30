# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Страница шаблонов, по которым команда выполняется без вопроса."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import domain
from application import ApplicationService
from application.command_rules import try_match


_FAMILIES = (
    (domain.SHELL_UNIX, "linux/macOS"),
    (domain.SHELL_WINDOWS, "windows"),
    (domain.SHELL_OTHER, "other"),
)


class PatternDialog(QDialog):
    """Шаблон и однострочная проверка: галочка, если текст совпал."""

    def __init__(self, os_label: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новый шаблон")
        self.setModal(True)
        self.resize(460, 240)
        self._pattern_text = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(10)

        if os_label:
            where = QLabel(f"ОС: {os_label}")
            where.setObjectName("sidebarTitle")
            layout.addWidget(where)

        hint = QLabel(
            "Регулярное выражение. Символ * означает любой текст, например grep *."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(hint)

        self._pattern = QLineEdit()
        self._pattern.setPlaceholderText("grep *")
        self._pattern.textChanged.connect(self._refresh)
        layout.addWidget(self._pattern)

        sample_row = QHBoxLayout()
        sample_row.setSpacing(8)
        self._sample = QLineEdit()
        self._sample.setPlaceholderText("Проверка на одной строке")
        self._sample.textChanged.connect(self._refresh)
        sample_row.addWidget(self._sample, 1)
        self._mark = QLabel("")
        self._mark.setObjectName("ruleMark")
        self._mark.setFixedWidth(24)
        self._mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sample_row.addWidget(self._mark)
        layout.addLayout(sample_row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#8b8d97;")
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save is not None:
            save.setText("Сохранить")
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setText("Отменить")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def pattern(self) -> str:
        return self._pattern_text

    def _refresh(self) -> None:
        pattern = self._pattern.text()
        sample = self._sample.text()
        if not pattern.strip():
            self._mark.setText("")
            self._status.setText("")
            return
        verdict = try_match(pattern, sample)
        if verdict == "invalid":
            self._mark.setText("✕")
            self._mark.setStyleSheet("color:#fb7185; font-weight:700;")
            self._status.setText("Регулярное выражение не разбирается")
            return
        if verdict == "match":
            self._mark.setText("✓")
            self._mark.setStyleSheet("color:#86efac; font-weight:700;")
            self._status.setText("Текст совпадает с шаблоном")
            return
        self._mark.setText("✕")
        self._mark.setStyleSheet("color:#fb7185; font-weight:700;")
        self._status.setText("Текст не совпадает с шаблоном")

    def _on_save(self) -> None:
        pattern = self._pattern.text().strip()
        if not pattern:
            self._status.setText("Введите шаблон")
            self._status.setStyleSheet("color:#fb7185;")
            return
        if try_match(pattern, self._sample.text()) == "invalid":
            self._status.setText("Регулярное выражение не разбирается")
            self._status.setStyleSheet("color:#fb7185;")
            return
        self._pattern_text = pattern
        self.accept()


class _RuleRow(QWidget):
    remove_requested = Signal(str)

    def __init__(self, pattern: str) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        label = QLabel(pattern)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(label, 1)
        remove = QPushButton("✕")
        remove.setObjectName("auxButton")
        remove.setFixedWidth(34)
        remove.setToolTip("Удалить шаблон")
        remove.clicked.connect(lambda: self.remove_requested.emit(pattern))
        layout.addWidget(remove)


class RulesDialog(QWidget):
    """Списки шаблонов. Живут на вкладке настроек."""

    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(12)

        hint = QLabel(
            "Шаблоны общие для всех подключений, списки разные у каждой ОС. "
            "Команда уходит в терминал целиком и без вопроса, только если "
            "каждая её часть совпала хотя бы с одним шаблоном."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.setSpacing(16)
        self._lists = {}
        for family, title in _FAMILIES:
            columns.addWidget(self._column(family, title), 1)
        layout.addLayout(columns, 1)
        self._reload()

    def _column(self, family: str, title: str) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("sidebarTitle")
        layout.addWidget(label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(6)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        self._lists[family] = body_layout

        add = QPushButton("+")
        add.setObjectName("auxButton")
        add.setToolTip("Добавить шаблон")
        add.clicked.connect(lambda: self._on_add(family))
        layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        return host

    def _reload(self) -> None:
        stored = self._service.command_rule_lists()
        for family, body in self._lists.items():
            while body.count() > 1:
                item = body.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()
            patterns = stored.get(family) or ()
            for pattern in patterns:
                row = _RuleRow(pattern)
                row.remove_requested.connect(
                    lambda text, key=family: self._on_remove(key, text)
                )
                body.insertWidget(body.count() - 1, row)

    def _on_add(self, family: str) -> None:
        dialog = PatternDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._service.add_command_rule(family, dialog.pattern())
        except Exception as exc:
            self._show_error(str(exc) or "Не удалось сохранить шаблон")
            return
        self._reload()

    def _on_remove(self, family: str, pattern: str) -> None:
        try:
            self._service.remove_command_rule(family, pattern)
        except Exception as exc:
            self._show_error(str(exc) or "Не удалось удалить шаблон")
            return
        self._reload()

    def _show_error(self, message: str) -> None:
        QMessageBox.warning(self, "Правила команд", message)
