# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Карточка команды и связанного с ней вывода."""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.fitted_text import FittedText
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
        target: str = "",
    ) -> None:
        super().__init__(parent)
        self.command = command
        self.target = target
        self.record = ""
        self.return_code: Optional[int] = None
        self._finalized = False
        self._has_output = False
        self._height_scheduled = False
        self._live = False
        self._plain: Optional[str] = None

        self.setObjectName("commandCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(2)
        header.setAlignment(Qt.AlignmentFlag.AlignTop)

        icon = QLabel("❯")
        icon.setObjectName("commandIcon")
        icon.setFont(mono)
        icon.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        icon.setFixedHeight(QFontMetrics(mono).height())
        icon.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        arrow = QWidget()
        arrow.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        arrow_column = QVBoxLayout(arrow)
        arrow_column.setContentsMargins(0, 0, 0, 0)
        arrow_column.setSpacing(0)
        arrow_column.addWidget(icon)
        arrow_column.addStretch(1)
        header.addWidget(arrow, 0, Qt.AlignmentFlag.AlignTop)

        if target:
            where = QLabel(f"MCP: {target}")
            where.setObjectName("commandTarget")
            where.setMinimumWidth(0)
            where.setMaximumWidth(180)
            header.addWidget(where, 0, Qt.AlignmentFlag.AlignTop)

        command_font = QFont(mono)
        command_font.setWeight(QFont.Weight.DemiBold)
        self.command_label = FittedText(command, command_font)
        self.command_label.setObjectName("commandText")
        header.addWidget(self.command_label, 1, Qt.AlignmentFlag.AlignTop)

        self.attach_button = QPushButton(">")
        self.attach_button.setFixedSize(22, 22)
        self.attach_button.setObjectName("attachButton")
        self.attach_button.setToolTip(
            "Добавить команду и её вывод\nв конец поля чата. Сообщение не отправляется."
        )
        self.attach_button.setVisible(False)
        self.attach_button.clicked.connect(lambda: self.attach_requested.emit(self))
        header.addWidget(self.attach_button, 0, Qt.AlignmentFlag.AlignTop)

        self.resend_button = QPushButton("↻")
        self.resend_button.setObjectName("resendButton")
        self.resend_button.setFixedSize(22, 22)
        self.resend_button.setToolTip("Вставить команду в поле ввода и выполнить снова")
        self.resend_button.clicked.connect(
            lambda: self.resend_requested.emit(self)
        )
        header.addWidget(self.resend_button, 0, Qt.AlignmentFlag.AlignTop)

        self.delete_button = QPushButton("×")
        self.delete_button.setObjectName("deleteButton")
        self.delete_button.setFixedSize(22, 22)
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
        self.output.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
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
        self.output.setMinimumWidth(0)
        self.output.setMinimumHeight(0)
        self.output.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.output.document().setDocumentMargin(0)
        self.output.setVisible(False)
        self.output.installEventFilter(self)
        self.output.viewport().installEventFilter(self)
        self.command_label.installEventFilter(self)
        self.command_label.viewport().installEventFilter(self)
        layout.addWidget(self.output)

        self.terminal = TerminalView(mono)
        self.terminal.setObjectName("liveTerminal")
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

    def end_live(self) -> str:
        """Выключить экран и вернуть его текст. Дальше блок выглядит как история."""
        self._live = False
        self.terminal.set_interactive(False)
        self.reject_button.setVisible(False)
        self._reason_row.setVisible(False)
        text = self.terminal.plain_text() if self.terminal.has_content() else ""
        self.terminal.setVisible(False)
        self.terminal.setFixedHeight(0)
        return text.strip("\n")

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
        self.return_code = return_code

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

    def refit(self) -> None:
        """Подогнать высоту вывода под текст, без пустого хвоста от буфера PTY."""
        if self._has_output:
            self._adjust_height()

    def _adjust_height(self) -> None:
        if not self._has_output:
            return
        width = self.output.viewport().width()
        if width < 30:
            width = 480
        doc = self.output.document()
        doc.setDocumentMargin(0)
        doc.setTextWidth(width)
        block = doc.lastBlock()
        while block.text().strip() == "" and block.previous().isValid():
            block = block.previous()
        bottom = doc.documentLayout().blockBoundingRect(block).bottom()
        chrome = max(0, self.output.height() - self.output.viewport().height())
        new_h = max(1, math.ceil(bottom) + chrome + 2)
        if self.output.height() != new_h:
            self.output.setFixedHeight(new_h)
        self.output.verticalScrollBar().setValue(0)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if event.type() == QEvent.Type.ContextMenu and obj in (
            self.output,
            self.output.viewport(),
            self.command_label,
            self.command_label.viewport(),
        ):
            self._copy_menu(event.globalPos())
            return True
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
        editor = self.output.toPlainText()
        if not self.terminal.isVisible():
            return editor
        screen = self._plain if self._plain else self.terminal.plain_text()
        body = (screen or "").strip("\n")
        if not body:
            return editor
        extra = editor.strip("\n")
        if extra and extra not in body:
            return body + "\n" + extra
        return screen or body

    def attachment_text(self) -> str:
        output = self.output_text().rstrip()
        body = f"$ {self.command}"
        if output:
            body += "\n" + output
        return wrap_block("terminal", body)

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        self._copy_menu(event.globalPos())
        event.accept()

    def copy_command(self) -> None:
        QApplication.clipboard().setText(self.command)

    def copy_output(self) -> None:
        QApplication.clipboard().setText(self.output_text())

    def copy_both(self) -> None:
        output_text = self.output_text()
        text = self.command
        if output_text:
            text += "\n" + output_text
        QApplication.clipboard().setText(text)

    def _copy_menu(self, global_pos) -> None:
        menu = QMenu(self)
        menu.addAction("Скопировать команду", self.copy_command)
        menu.addAction("Скопировать ответ", self.copy_output)
        menu.addAction("Скопировать оба", self.copy_both)
        menu.exec(global_pos)


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