# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Многострочное поле команды.

Enter отправляет законченную команду. Пока открыты heredoc, кавычки
или перенос через \\, Enter добавляет строку. Сам процесс не запускает.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QPlainTextEdit, QSizePolicy

_Delimiter = Tuple[str, bool]


def command_needs_more(text: str) -> bool:
    """Команда ещё не собрана: незакрытый heredoc, кавычка или \\ в конце."""
    if not text:
        return False
    lines = text.split("\n")
    pending: List[_Delimiter] = []
    quote: Optional[str] = None
    for index, line in enumerate(lines):
        if pending:
            token, strip_tabs = pending[0]
            probe = line.lstrip("\t") if strip_tabs else line
            if probe.rstrip("\r") == token:
                pending.pop(0)
            continue
        quote, opened = _scan_line(line, quote)
        pending.extend(opened)
        if index == len(lines) - 1 and not pending and quote is None and _continued(line):
            return True
    return bool(pending) or quote is not None


def _continued(line: str) -> bool:
    count = 0
    index = len(line) - 1
    while index >= 0 and line[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _scan_line(line: str, quote: Optional[str]) -> Tuple[Optional[str], List[_Delimiter]]:
    opened: List[_Delimiter] = []
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\" and quote == '"' and index + 1 < len(line):
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "\\" and index + 1 < len(line):
            index += 2
            continue
        if line.startswith("<<", index):
            token, strip_tabs, next_index = _read_heredoc(line, index)
            if token:
                opened.append((token, strip_tabs))
                index = next_index
                continue
        index += 1
    return quote, opened


def _read_heredoc(line: str, start: int) -> Tuple[str, bool, int]:
    index = start + 2
    strip_tabs = False
    if index < len(line) and line[index] == "-":
        strip_tabs = True
        index += 1
    while index < len(line) and line[index] in " \t":
        index += 1
    if index >= len(line):
        return "", False, start + 2
    if line[index] in "'\"":
        mark = line[index]
        end = line.find(mark, index + 1)
        if end == -1:
            return "", False, start + 2
        return line[index + 1 : end], strip_tabs, end + 1
    end = index
    while end < len(line) and (line[end].isalnum() or line[end] == "_"):
        end += 1
    if end == index:
        return "", False, start + 2
    return line[index:end], strip_tabs, end


class CommandInput(QPlainTextEdit):
    """Поле команды с теми же методами, что были у однострочного поля."""

    def __init__(self, font: QFont) -> None:
        super().__init__()
        self.setObjectName("commandInput")
        self.setFont(font)
        self.setPlaceholderText("Выберите сессию слева")
        self.setTabChangesFocus(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        self._min_height = 34
        self._max_height = 180
        self.setFixedHeight(self._min_height)
        self.textChanged.connect(self._fit_height)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)
        self.setCursorPosition(len(value))

    def insert(self, value: str) -> None:
        self.insertPlainText(value)

    def setCursorPosition(self, position: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(int(position), len(self.text()))))
        self.setTextCursor(cursor)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & (
            Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
        ):
            self.insertPlainText("\n")
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        document = self.document()
        document.setTextWidth(max(1, self.viewport().width()))
        height = int(document.size().height()) + 8
        height = max(self._min_height, min(self._max_height, height))
        if self.height() != height:
            self.setFixedHeight(height)


class GrowingText(QPlainTextEdit):
    """Поле промпта. Enter отправляет, Ctrl+Enter и Shift+Enter переносят строку."""

    submitted = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._min_height = 36
        self._max_height = 180
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.document().setDocumentMargin(2)
        self.setFixedHeight(self._min_height)
        self.textChanged.connect(self._fit_height)

    def text(self) -> str:
        return self.toPlainText()

    def setText(self, value: str) -> None:
        self.setPlainText(value)
        self.setCursorPosition(len(value))

    def setCursorPosition(self, position: int) -> None:
        cursor = self.textCursor()
        cursor.setPosition(max(0, min(int(position), len(self.text()))))
        self.setTextCursor(cursor)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                self.insertPlainText("\n")
                return
            if not mods & Qt.KeyboardModifier.AltModifier:
                self.submitted.emit()
                return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        # QPlainTextDocumentLayout отдаёт высоту в строках. setTextWidth
        # на этом документе схлопывает её почти в ноль, и поле не растёт.
        lines = self.document().documentLayout().documentSize().height()
        if lines < 1:
            lines = 1
        content = math.ceil(lines * self.fontMetrics().lineSpacing())
        margins = self.contentsMargins()
        height = int(content + margins.top() + margins.bottom() + self.frameWidth() * 2)
        height = max(self._min_height, min(self._max_height, height))
        if self.height() != height:
            self.setFixedHeight(height)
