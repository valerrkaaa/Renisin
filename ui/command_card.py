# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Карточка команды и связанного с ней вывода."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.terminal_view import TerminalView


class CommandCard(QFrame):
    """Карточка команды и связанного с ней вывода."""

    remove_requested = Signal(object)
    resend_requested = Signal(object)
    attach_requested = Signal(object)
    input_generated = Signal(str)
    resized = Signal(int, int)
    reject_requested = Signal(str)

    def __init__(
        self,
        command: str,
        mono: QFont,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.command = command
        self._finalized = False
        self._has_output = False
        self._height_scheduled = False
        self._live = False
        self._plain: Optional[str] = None

        self.setObjectName("commandCard")
        self.setFrameShape(QFrame.Shape.NoFrame)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(8)

        icon = QLabel("❯")
        icon.setObjectName("commandIcon")
        icon.setFont(mono)
        header.addWidget(icon)

        self.command_label = QLabel(command)
        self.command_label.setObjectName("commandText")
        self.command_label.setFont(mono)
        self.command_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.command_label.setWordWrap(True)
        header.addWidget(self.command_label, 1)

        self.attach_button = QPushButton(">")
        self.attach_button.setObjectName("attachButton")
        self.attach_button.setToolTip(
            "Добавить команду и её вывод\nв конец поля чата. Сообщение не отправляется."
        )
        self.attach_button.setVisible(False)
        self.attach_button.clicked.connect(lambda: self.attach_requested.emit(self))
        header.addWidget(self.attach_button, 0, Qt.AlignmentFlag.AlignTop)

        self.resend_button = QPushButton("↻  Повторить")
        self.resend_button.setObjectName("resendButton")
        self.resend_button.setToolTip("Вставить команду в поле ввода и выполнить снова")
        self.resend_button.clicked.connect(
            lambda: self.resend_requested.emit(self)
        )
        header.addWidget(self.resend_button, 0, Qt.AlignmentFlag.AlignTop)

        self.copy_button = QPushButton("⧉  Скопировать")
        self.copy_button.setObjectName("copyButton")
        self.copy_button.setToolTip("Скопировать команду и весь вывод")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        header.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)

        self.delete_button = QPushButton("✕")
        self.delete_button.setObjectName("deleteButton")
        self.delete_button.setToolTip("Удалить этот блок из истории")
        self.delete_button.clicked.connect(
            lambda: self.remove_requested.emit(self)
        )
        header.addWidget(self.delete_button, 0, Qt.AlignmentFlag.AlignTop)

        layout.addLayout(header)

        self.separator = QFrame()
        self.separator.setObjectName("cardSeparator")
        self.separator.setFrameShape(QFrame.Shape.HLine)
        self.separator.setFrameShadow(QFrame.Shadow.Plain)
        self.separator.setVisible(False)
        layout.addWidget(self.separator)

        self.output = QTextEdit()
        self.output.setObjectName("commandOutput")
        self.output.setReadOnly(True)
        self.output.setAcceptRichText(False)
        self.output.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.output.setFont(mono)
        self.output.setFrameShape(QFrame.Shape.NoFrame)
        self.output.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.output.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.output.setMinimumHeight(0)
        self.output.document().setDocumentMargin(0)
        self.output.setVisible(False)
        self.output.installEventFilter(self)
        self.output.viewport().installEventFilter(self)
        layout.addWidget(self.output)

        self.terminal = TerminalView(mono)
        self.terminal.setVisible(False)
        self.terminal.input_generated.connect(self.input_generated)
        self.terminal.resized.connect(self.resized)
        layout.addWidget(self.terminal)

        self.reject_button = QPushButton("Отклонить по причине")
        self.reject_button.setObjectName("auxButton")
        self.reject_button.setToolTip("Прервать ввод через Ctrl+C и передать модели причину")
        self.reject_button.setVisible(False)
        self.reject_button.clicked.connect(self._show_reject)
        layout.addWidget(self.reject_button, 0, Qt.AlignmentFlag.AlignLeft)

        self._reason_row = QWidget()
        reason_layout = QHBoxLayout(self._reason_row)
        reason_layout.setContentsMargins(0, 0, 0, 0)
        reason_layout.setSpacing(8)
        self._reason = QLineEdit()
        self._reason.setObjectName("commandReason")
        self._reason.setPlaceholderText("Почему отклонить")
        self._reason.returnPressed.connect(self._submit_reject)
        submit = QPushButton("Отправить отказ")
        submit.setObjectName("auxButton")
        submit.clicked.connect(self._submit_reject)
        reason_layout.addWidget(self._reason, 1)
        reason_layout.addWidget(submit)
        self._reason_row.setVisible(False)
        layout.addWidget(self._reason_row)

    # --- вывод ------------------------------------------------------------

    def append_output(self, text: str, fmt: QTextCharFormat) -> None:
        if not text:
            return

        if not self._has_output:
            self._has_output = True
            self.separator.setVisible(True)
            self.output.setVisible(True)

        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text, fmt)
        self.output.setTextCursor(cursor)

        self._adjust_height()
        self._schedule_adjust_height()

    def is_live(self) -> bool:
        return self._live

    def begin_live(self) -> None:
        self._live = True
        self._plain = None
        self.separator.setVisible(True)
        self.output.setVisible(False)
        self.terminal.setVisible(True)
        self.terminal.set_interactive(True)
        self.reject_button.setVisible(True)
        self.reject_button.setEnabled(True)
        self._reason.clear()
        self._reason_row.setVisible(False)

    def feed_live(
        self,
        data: str,
        columns: Optional[int] = None,
        rows: Optional[int] = None,
        alternate: bool = False,
    ) -> None:
        if not self._live and not data and columns is None:
            return
        if not self._live:
            self.begin_live()
        self.terminal.feed(data, columns, rows, alternate)

    def is_fullscreen(self) -> bool:
        return self.terminal.is_fullscreen()

    def set_pty_rows(self, rows: int) -> None:
        self.terminal.set_preferred_rows(rows)

    def focus_terminal(self) -> None:
        if self._live:
            self.terminal.setFocus()

    def remember_plain(self, text: str) -> None:
        self._plain = text

    def take_plain(self) -> Optional[str]:
        text = self._plain
        self._plain = None
        return text

    def end_live(self) -> bool:
        """Выключить клавиатуру. Экран с текстом остаётся на месте."""
        self._live = False
        self.terminal.set_interactive(False)
        self.reject_button.setVisible(False)
        self._reason_row.setVisible(False)
        if self.terminal.has_content():
            return True
        self.terminal.setVisible(False)
        return False

    def finalize(
        self,
        return_code: Optional[int],
        exit_fmt: QTextCharFormat,
    ) -> None:
        """Отметить карточку завершённой и показать код возврата.

        Если вывода не было, поле вывода и разделитель остаются скрытыми —
        карточка выглядит компактно.
        """
        if self._finalized:
            return
        self._finalized = True

        if return_code is None:
            self._append_exit_line("[завершено без кода возврата]", exit_fmt)
        elif return_code != 0:
            self._append_exit_line(f"[код возврата: {return_code}]", exit_fmt)

    def _show_reject(self) -> None:
        self._reason_row.setVisible(True)
        self._reason.setFocus()

    def _submit_reject(self) -> None:
        reason = self._reason.text().strip()
        if not reason:
            self._reason.setFocus()
            return
        self.reject_button.setEnabled(False)
        self._reason.setReadOnly(True)
        self.reject_requested.emit(reason)

    def _append_exit_line(self, text: str, fmt: QTextCharFormat) -> None:
        prefix = "\n" if self._has_output else ""
        self.append_output(prefix + text, fmt)

    # --- геометрия --------------------------------------------------------

    def _schedule_adjust_height(self) -> None:
        if self._height_scheduled:
            return
        self._height_scheduled = True
        QTimer.singleShot(0, self._do_adjust_height)

    def _do_adjust_height(self) -> None:
        self._height_scheduled = False
        self._adjust_height()

    def _adjust_height(self) -> None:
        if not self._has_output:
            return
        width = self.output.viewport().width()
        if width < 30:
            return
        doc = self.output.document()
        doc.setDocumentMargin(0)
        doc.setTextWidth(width)
        block = doc.lastBlock()
        if block.text() == "" and block.previous().isValid():
            block = block.previous()
        bottom = doc.documentLayout().blockBoundingRect(block).bottom()
        chrome = max(0, self.output.height() - self.output.viewport().height())
        new_h = max(1, math.ceil(bottom) + chrome + 2)
        if self.output.height() != new_h:
            self.output.setFixedHeight(new_h)
        self.output.verticalScrollBar().setValue(0)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self.output.viewport() and event.type() == QEvent.Type.Wheel:
            return _forward_wheel(self.output, event)
        if obj is self.output and event.type() == QEvent.Type.Resize:
            # Ширина viewport обновляется после этого события.
            # Пересчёт сразу берёт старую узкую ширину и оставляет пустоту снизу.
            self._schedule_adjust_height()
        return super().eventFilter(obj, event)

    # --- действия ---------------------------------------------------------

    def set_attach_visible(self, visible: bool) -> None:
        self.attach_button.setVisible(visible)

    def output_text(self) -> str:
        if self.terminal.isVisible() and self._plain:
            return self._plain
        return self.output.toPlainText()

    def attachment_text(self) -> str:
        output = self.output_text().rstrip()
        body = f"$ {self.command}"
        if output:
            body += "\n" + output
        return wrap_block("terminal", body)

    def copy_to_clipboard(self) -> None:
        output_text = self.output_text()
        text = self.command
        if output_text:
            text += "\n" + output_text
        QApplication.clipboard().setText(text)

        self.copy_button.setText("✓  Скопировано")
        self.copy_button.setEnabled(False)
        QTimer.singleShot(1200, self._restore_copy_button)

    def _restore_copy_button(self) -> None:
        self.copy_button.setText("⧉  Скопировать")
        self.copy_button.setEnabled(True)


def _forward_wheel(widget: QWidget, event) -> bool:
    """Колесо крутит ленту, а не текст внутри сообщения."""
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        return False
    event.ignore()
    QApplication.sendEvent(parent.viewport(), event)
    return True


def wrap_block(kind: str, body: str) -> str:
    """Обернуть текст в ограждение, которого нет внутри самого текста."""
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}{kind}\n{body}\n{fence}"