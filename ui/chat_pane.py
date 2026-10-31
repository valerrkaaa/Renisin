# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Правая колонка чата с моделью. Сеть отсюда не вызывается."""
from __future__ import annotations

import html
import math
import re
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextOption
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QApplication,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from application.terminal_mcp import parse_call
from ui.command_input import five_line_height, one_line_height
from ui.fitted_text import FittedText, clamp_scroll_host

_FENCE = re.compile(r"```([^\n`]*)\r?\n(.*?)```", re.DOTALL)
_SHELL_LANGS = frozenset(
    {"bash", "sh", "shell", "zsh", "console", "cmd", "powershell", "ps1", "terminal"}
)
_EDIT_LANGS = frozenset({"edit", "replace", "замена"})
_PATH_LINE = re.compile(r"^(?:path|file|файл|путь)\s*[:=]\s*(.*?)\s*$", re.IGNORECASE)
_OLD_LINE = re.compile(r"^(?:old|было|старое)\s*[:=]\s*\|?\s*(.*)$", re.IGNORECASE)
_NEW_LINE = re.compile(r"^(?:new|стало|новое)\s*[:=]\s*\|?\s*(.*)$", re.IGNORECASE)
_THINKING_FRAMES = ("Думает", "Думает.", "Думает..", "Думает...")
_SELECTABLE = ("chatUser", "chatAssistant", "chatPlain", "shellCode")
_PREVIEW_LIMIT = 240


class _SelectableText(QTextEdit):
    """Текст, который выделяется мышью и копируется как в обычном поле."""

    def __init__(
        self, text: str, name: str, margin: Optional[int] = None, left_margin: Optional[int] = None
    ) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setObjectName(name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.setPlainText(text)
        if margin is None:
            if name in ("chatUser", "chatAssistant"):
                margin = 12
            elif name == "shellCode":
                margin = 6
            elif name == "chatPlain":
                margin = 4
            else:
                margin = 8
        if left_margin is None:
            self.document().setDocumentMargin(margin)
        else:
            self.document().setDocumentMargin(0)
            frame = self.document().rootFrame().frameFormat()
            frame.setTopMargin(margin)
            frame.setRightMargin(margin)
            frame.setBottomMargin(margin)
            frame.setLeftMargin(left_margin)
            self.document().rootFrame().setFrameFormat(frame)
        self.setMinimumHeight(1)
        self._fitting = False
        self.viewport().installEventFilter(self)

    def text(self) -> str:
        return self.toPlainText()

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, self.fontMetrics().lineSpacing())

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, max(self.fontMetrics().lineSpacing(), self.height()))

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.viewport() and event.type() == QEvent.Type.Wheel:
            return _forward_wheel(self, event)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.fit_to_text()

    def fit_to_text(self) -> None:
        if self._fitting:
            return
        width = self.viewport().width()
        parent = self.parentWidget()
        if parent is not None and parent.width() > 40:
            margins = 0
            parent_layout = parent.layout()
            if parent_layout is not None:
                margins = (
                    parent_layout.contentsMargins().left()
                    + parent_layout.contentsMargins().right()
                )
            available = parent.width() - margins
            if available >= 30 and (width < 30 or available < width):
                width = available
        if width < 30:
            return
        self._fitting = True
        try:
            document = self.document()
            document.setTextWidth(width)
            chrome = max(0, self.height() - self.viewport().height())
            # size() учитывает перенос строк и поля документа.
            # Высота по последнему блоку обрезала перенесённую строку,
            # и она рисовалась поверх кнопок под блоком.
            height = max(1, math.ceil(document.size().height()) + chrome + 8)
            if self.height() != height:
                self.setFixedHeight(height)
            self.verticalScrollBar().setValue(0)
        finally:
            self._fitting = False


def _forward_wheel(widget: QWidget, event) -> bool:
    """Колесо крутит ленту чата, а не текст внутри пузыря."""
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        return False
    event.ignore()
    QApplication.sendEvent(parent.viewport(), event)
    return True


def split_shell_blocks(text: str) -> List[Tuple[str, str]]:
    """Разбить ответ на обычный текст и команды из блоков ```bash и похожих."""
    parts: List[Tuple[str, str]] = []
    cursor = 0
    for match in _FENCE.finditer(text):
        if match.start() > cursor:
            parts.append(("text", text[cursor:match.start()]))
        header = match.group(1).strip().lower()
        lang = header.split()[0] if header else ""
        body = match.group(2).replace("\r\n", "\n").replace("\r", "\n")
        if body.endswith("\n"):
            body = body[:-1]
        if lang in _SHELL_LANGS and body.strip():
            parts.append(("shell", body.strip("\n")))
        elif lang in _EDIT_LANGS:
            parts.append(("edit", body.strip("\n")))
        else:
            parts.append(("text", match.group(0)))
        cursor = match.end()
    if cursor < len(text):
        parts.append(("text", text[cursor:]))
    if not parts:
        parts.append(("text", text))
    return parts


class _CallNote(QWidget):
    """Свёрнутая строка вызова. По нажатию показывает команду и ответ."""

    def __init__(self, status: str, command: str, output: str, mcp: str = "") -> None:
        super().__init__()
        self._status = status.strip() or "выполнено"
        self._command = command
        self._open = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(6)
        self._summary = QLabel()
        self._summary.setObjectName("toolCall")
        self._summary.setWordWrap(True)
        self._summary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._summary.installEventFilter(self)
        layout.addWidget(self._summary)
        self._body = QWidget()
        self._body.setObjectName("toolCallBody")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(12, 0, 0, 0)
        body.setSpacing(4)
        self._command_view = _SelectableText(command, "commandText", margin=0)
        body.addWidget(self._command_view)
        self._answer_view = _SelectableText(output, "commandOutput", margin=0)
        body.addWidget(self._answer_view)
        self._mcp = " ".join(mcp.split())
        self._body.hide()
        layout.addWidget(self._body)
        self._show_summary()

    def _show_summary(self) -> None:
        text = self._line()
        self._summary.setText(text)
        lines = text.count("\n") + 1
        self._summary.setMinimumHeight(self._summary.fontMetrics().lineSpacing() * lines + 4)

    def _line(self) -> str:
        """Имя MCP на первой строке, стрелка и команда — на следующей."""
        mark = "v" if self._open else ">"
        head = f"{self._status}:"
        if self._mcp:
            head = f"{head} {self._mcp}"
        command = " ".join(self._command.split())
        if command:
            return f"{head}\n{mark} {command}"
        return f"{mark} {head}"

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if (
            obj is self._summary
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._open = not self._open
            self._body.setVisible(self._open)
            self._show_summary()
            if self._open:
                self._command_view.fit_to_text()
                self._answer_view.fit_to_text()
                QTimer.singleShot(0, self._refit_body)
            return True
        return super().eventFilter(obj, event)

    def _refit_body(self) -> None:
        try:
            self._command_view.fit_to_text()
            self._answer_view.fit_to_text()
        except RuntimeError:
            return


def call_note(text: str) -> Optional[QWidget]:
    data = parse_call(text)
    if data is None:
        return None
    return _CallNote(
        str(data.get("status") or ""),
        str(data.get("command") or ""),
        str(data.get("output") or ""),
        str(data.get("mcp") or ""),
    )


def parse_edit_block(body: str) -> tuple[str, str, str] | None:
    """Разобрать блок edit. Терпит лишние пробелы и подписи path/OLD/NEW."""
    path = ""
    old_lines: List[str] = []
    new_lines: List[str] = []
    bucket: List[str] | None = None
    for raw in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        path_match = _PATH_LINE.match(line)
        old_match = _OLD_LINE.match(line)
        new_match = _NEW_LINE.match(line)
        if path_match and bucket is None:
            path = path_match.group(1).strip().strip('"').strip("'")
            continue
        if old_match and bucket is None:
            bucket = old_lines
            if old_match.group(1).strip() not in ("", "|"):
                old_lines.append(old_match.group(1))
            continue
        if new_match and bucket is old_lines:
            bucket = new_lines
            if new_match.group(1).strip() not in ("", "|"):
                new_lines.append(new_match.group(1))
            continue
        if bucket is not None:
            bucket.append(raw)
    if not path or not old_lines or bucket is not new_lines:
        return None
    return path, _block_text(old_lines), _block_text(new_lines)


def _block_text(lines: List[str]) -> str:
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def command_caption(command: str, parts: Tuple[str, ...] = ()) -> str:
    """Несколько команд — каждая со своей строки. Одна команда остаётся в строке."""
    shown = [part.strip() for part in parts if part and part.strip()]
    if len(shown) <= 1:
        return f"Команда: {command}"
    return "Команда:\n" + "\n".join(shown)


def command_rich_text(
    command: str,
    parts: Tuple[str, ...] = (),
    spans: Sequence[Sequence[object]] = (),
) -> Optional[str]:
    """HTML, если хотя бы одна команда совпала с шаблоном. Иначе обычный текст."""
    if not spans or not any(
        bool(getattr(span, "matched", False)) for line in spans for span in line
    ):
        return None
    lines = []
    for line in spans:
        bits = []
        for span in line:
            escaped = html.escape(str(getattr(span, "text", "")))
            if getattr(span, "matched", False):
                bits.append(f'<font color="#86efac">{escaped}</font>')
            else:
                bits.append(escaped)
        lines.append("".join(bits))
    body = "<br>".join(lines)
    shown = [part.strip() for part in parts if part and part.strip()]
    if len(shown) <= 1 and len(lines) <= 1:
        return f"Команда: {body}"
    return "Команда:<br>" + body


class _CommandText(QTextEdit):
    """Текст команды. Совпавшая с шаблоном часть зелёная, операторы — обычные."""

    def __init__(
        self,
        mono: QFont,
        parts: Tuple[str, ...],
        spans: Sequence[Sequence[object]],
    ) -> None:
        super().__init__()
        self.setObjectName("shellCodeRich")
        self.setReadOnly(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(mono)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.document().setDocumentMargin(0)
        self.viewport().setAutoFillBackground(False)
        self._fitting = False
        self.viewport().installEventFilter(self)
        plain = QTextCharFormat()
        plain.setFont(mono)
        plain.setForeground(QColor("#e4e4e7"))
        hit = QTextCharFormat(plain)
        hit.setForeground(QColor("#86efac"))
        cursor = self.textCursor()
        cursor.beginEditBlock()
        shown = [part.strip() for part in parts if part and part.strip()]
        prefix = "Команда: " if len(shown) <= 1 and len(spans) <= 1 else "Команда:\n"
        cursor.insertText(prefix, plain)
        for index, line in enumerate(spans):
            if index:
                cursor.insertText("\n", plain)
            for span in line:
                cursor.insertText(
                    str(getattr(span, "text", "")),
                    hit if getattr(span, "matched", False) else plain,
                )
        cursor.endEditBlock()
        self._fit()

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.viewport() and event.type() == QEvent.Type.Wheel:
            if self.verticalScrollBar().maximum() > 0:
                return False
            return _forward_wheel(self, event)
        return super().eventFilter(obj, event)

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, self.fontMetrics().lineSpacing())

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, max(self.fontMetrics().lineSpacing(), self.height()))

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        if self._fitting:
            return
        self._fitting = True
        try:
            width = self.viewport().width()
            if width > 20:
                self.document().setTextWidth(width)
            height = int(self.document().size().height()) + 4
            capped = min(height, _PREVIEW_LIMIT)
            self.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded
                if height > _PREVIEW_LIMIT
                else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            if self.height() != capped:
                self.setFixedHeight(max(22, capped))
        finally:
            self._fitting = False


class _CommandRequest(QFrame):
    """Подтверждение команды, которую модель хочет выполнить через MCP."""

    decided = Signal(str, str, str)
    rule_requested = Signal(str)
    output_decided = Signal(str, str, bool, str, object)

    def __init__(
        self,
        request_id: str,
        intent: str,
        command: str,
        mono: QFont,
        target: str = "",
        parts: Tuple[str, ...] = (),
        spans: Sequence[Sequence[object]] = (),
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self._command = command
        self._parts = parts
        self.setObjectName("commandRequest")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        title = QLabel("Запрос на выполнение команды")
        title.setObjectName("sidebarTitle")
        layout.addWidget(title)
        if target:
            where = QLabel(f"MCP: {target}")
            where.setObjectName("commandTarget")
            where.setWordWrap(True)
            layout.addWidget(where)
        why = QLabel(f"{intent}")
        why.setObjectName("commandIntent")
        why.setWordWrap(True)
        why.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        layout.addWidget(why)
        self._mono = mono
        self._code = self._make_preview(parts, spans)
        layout.addWidget(self._code)

        run = QPushButton("Выполнить")
        run.setObjectName("auxButton")
        run.setToolTip("Выполнить команду один раз")
        run.clicked.connect(lambda: self._choose("run", ""))
        add_rule = QPushButton("Добавить правило")
        add_rule.setObjectName("auxButton")
        add_rule.setToolTip(
            "Открыть шаблон для ОС этого подключения.\n"
            "Команда при этом не выполняется."
        )
        add_rule.clicked.connect(lambda: self.rule_requested.emit(self._request_id))
        reject = QPushButton("Отклонить")
        reject.setObjectName("auxButton")
        reject.setToolTip("Отказать без пояснения")
        reject.clicked.connect(lambda: self._choose("reject", ""))
        explain = QPushButton("Отклонить по причине")
        explain.setObjectName("auxButton")
        explain.setToolTip("Отказать и передать модели причину")
        explain.clicked.connect(self._show_reason)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(run)
        row.addWidget(add_rule)
        row.addStretch(1)
        layout.addLayout(row)
        second = QHBoxLayout()
        second.setSpacing(8)
        second.addWidget(reject)
        second.addWidget(explain)
        second.addStretch(1)
        layout.addLayout(second)

        self._reason_row = QWidget()
        reason_layout = QHBoxLayout(self._reason_row)
        reason_layout.setContentsMargins(0, 0, 0, 0)
        reason_layout.setSpacing(8)
        self._reason = QLineEdit()
        self._reason.setObjectName("commandReason")
        self._reason.setPlaceholderText("Почему команда отклонена")
        self._reason.returnPressed.connect(self._submit_reason)
        submit = QPushButton("Отправить отказ")
        submit.setObjectName("auxButton")
        submit.clicked.connect(self._submit_reason)
        reason_layout.addWidget(self._reason, 1)
        reason_layout.addWidget(submit)
        self._reason_row.setVisible(False)
        layout.addWidget(self._reason_row)

        self._output = QPlainTextEdit()
        self._output.setObjectName("commandCapture")
        self._output.setFont(mono)
        self._output.setReadOnly(True)
        self._output.setPlaceholderText("Вывод команды")
        self._output.setMinimumHeight(96)
        self._output.setMaximumHeight(280)
        self._output.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._output.setVisible(False)
        layout.addWidget(self._output)
        self._captured = ""
        self._model_text = ""
        self._exit_code: object = None
        self._editable = False
        self._settled = False
        self._live = ""

        review = QHBoxLayout()
        review.setSpacing(8)
        send = QPushButton("Отправить")
        send.setObjectName("auxButton")
        send.setToolTip("Передать модели вывод команды")
        send.clicked.connect(self._send_output)
        redact = QPushButton("Обезличить")
        redact.setObjectName("auxButton")
        redact.setToolTip("Разрешить правку вывода перед отправкой")
        redact.clicked.connect(self._redact_output)
        review_reject = QPushButton("Отклонить по причине")
        review_reject.setObjectName("auxButton")
        review_reject.setToolTip("Отказать и передать модели причину")
        review_reject.clicked.connect(self._show_reason)
        review.addWidget(send)
        review.addWidget(redact)
        review.addWidget(review_reject)
        review.addStretch(1)
        self._review_row = QWidget()
        self._review_row.setLayout(review)
        self._review_row.setVisible(False)
        layout.addWidget(self._review_row)
        self._approval = (run, add_rule, reject, explain)
        self._review_buttons = (send, redact, review_reject, submit)
        self._buttons = self._approval + (submit,)

    def request_id(self) -> str:
        return self._request_id

    def set_preview(self, parts: Tuple[str, ...], spans: Sequence[Sequence[object]]) -> None:
        """Перерисовать команды после нового шаблона."""
        self._parts = parts
        layout = self.layout()
        index = layout.indexOf(self._code)
        layout.removeWidget(self._code)
        self._code.deleteLater()
        self._code = self._make_preview(parts, spans)
        if index < 0:
            layout.addWidget(self._code)
        else:
            layout.insertWidget(index, self._code)

    def _make_preview(self, parts: Tuple[str, ...], spans: Sequence[Sequence[object]]) -> QWidget:
        rich = command_rich_text(self._command, parts, spans)
        if rich is None:
            code = FittedText(command_caption(self._command, parts), self._mono)
            code.setObjectName("shellCode")
            code.set_scroll_limit(_PREVIEW_LIMIT)
            return code
        return _CommandText(self._mono, parts, spans)

    def _show_reason(self) -> None:
        self._reason_row.setVisible(True)
        self._reason.setFocus()

    def _submit_reason(self) -> None:
        text = self._reason.text().strip()
        if not text:
            self._reason.setFocus()
            return
        self._choose("reject_reason", text)

    def show_running(self) -> None:
        """Команда пошла в терминал. Вывод копится в этом блоке."""
        for button in self._approval:
            button.hide()
        self._output.setVisible(True)
        self._output.setReadOnly(True)
        self._output.setPlainText("")
        self._review_row.setVisible(False)
        self._editable = False
        self._settled = False
        self._live = ""

    def note_output(self, text: str, replace: bool) -> None:
        if self._editable or self._settled:
            return
        if replace:
            self._live = text
        else:
            self._live += text
        self._output.setPlainText(self._live)
        cursor = self._output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._output.setTextCursor(cursor)

    def show_result(self, output: str, model_text: str, code: object) -> None:
        self._settled = True
        self._model_text = model_text
        self._exit_code = code
        shown = output if output.strip() else model_text
        self._captured = shown
        if not self._editable:
            self._output.setPlainText(shown)
        self._output.setVisible(True)
        if not self._editable:
            self._output.setReadOnly(True)
        self._review_row.setVisible(True)
        for button in self._review_buttons:
            button.setEnabled(True)
            button.show()

    def _redact_output(self) -> None:
        self._editable = True
        self._output.setReadOnly(False)
        self._output.setFocus()
        self._review_buttons[1].hide()

    def _send_output(self) -> None:
        text = self._output.toPlainText()
        edited = text != self._captured
        for button in self._review_buttons:
            button.setEnabled(False)
        self._output.setReadOnly(True)
        self.output_decided.emit(
            self._request_id, text, edited, self._model_text, self._exit_code
        )

    def _choose(self, action: str, reason: str) -> None:
        for button in self._buttons + self._review_buttons:
            button.setEnabled(False)
        self._output.setReadOnly(True)
        self._reason.setReadOnly(True)
        self.decided.emit(self._request_id, action, reason)


class ChatPane(QWidget):
    send_requested = Signal()
    stop_requested = Signal()
    retry_requested = Signal()
    run_command = Signal(str)
    insert_command = Signal(str)
    apply_edit = Signal(str, str, str)
    reset_requested = Signal()
    clear_requested = Signal()
    command_decided = Signal(str, str, str)
    output_decided = Signal(str, str, bool, str, object)
    rule_requested = Signal(str)
    limit_changed = Signal(int)
    model_requested = Signal(str)

    def __init__(self, mono: QFont, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPane")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Чат")
        title.setObjectName("sidebarTitle")
        head.addWidget(title)
        head.addStretch(1)
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("chatModelCombo")
        self._model_combo.setMinimumWidth(180)
        self._model_combo.activated.connect(self._on_header_model)
        head.addWidget(self._model_combo)
        self._auto_run = QCheckBox("Авто")
        self._auto_run.setObjectName("autoRun")
        self._auto_run.setChecked(True)
        self._auto_run.setToolTip(
            "выполнять автоматически при полном совпадении с паттернами"
        )
        head.addWidget(self._auto_run)
        self._review_output = QCheckBox("Просмотр")
        self._review_output.setObjectName("reviewOutput")
        self._review_output.setChecked(True)
        self._review_output.setToolTip(
            "Показывать вывод команды перед отправкой модели.\n"
            "Если выключено, ответ уходит сразу."
        )
        head.addWidget(self._review_output)
        self._clear = QPushButton("Очистить")
        self._clear.setObjectName("auxButton")
        self._clear.setToolTip(
            "Остановить ответ, сбросить контекст модели\n"
            "и убрать сообщения из этого чата."
        )
        self._clear.clicked.connect(self.clear_requested.emit)
        head.addWidget(self._clear)
        self._usage_tokens = 0
        self._usage_messages = 0
        self._usage = QPushButton("лимит")
        self._usage.setObjectName("usageButton")
        self._usage.setCursor(Qt.CursorShape.PointingHandCursor)
        self._usage.setToolTip(
            "Сколько токенов из лимита уйдёт в модель.\n"
            "Нажмите, чтобы задать лимит или сбросить контекст."
        )
        self._usage.clicked.connect(self._open_limit)
        head.addWidget(self._usage)
        self._limit = QSpinBox()
        self._limit.setObjectName("chatLimit")
        self._limit.setRange(256, 1_000_000)
        self._limit.setSingleStep(1024)
        self._limit.setValue(8192)
        self._limit.setToolTip("Лимит контекста в токенах.")
        self._limit.valueChanged.connect(self._on_limit_changed)
        self._reset = QPushButton("Сбросить")
        self._reset.setObjectName("auxButton")
        self._reset.setToolTip(
            "Модель забудет переписку выше этой черты.\n"
            "Сообщения останутся в чате, в запрос они больше не попадут."
        )
        self._reset.clicked.connect(self.reset_requested.emit)
        self._reset.clicked.connect(self._hide_limit)
        self._limit_popup = QFrame(self, Qt.WindowType.Popup)
        self._limit_popup.setObjectName("limitPopup")
        popup_layout = QVBoxLayout(self._limit_popup)
        popup_layout.setContentsMargins(10, 10, 10, 10)
        popup_layout.setSpacing(8)
        caption = QLabel("Лимит токенов")
        caption.setObjectName("chatContext")
        popup_layout.addWidget(caption)
        popup_layout.addWidget(self._limit)
        popup_layout.addWidget(self._reset)
        root.addLayout(head)

        self._banner = QFrame()
        self._banner.setObjectName("offlineBanner")
        banner_layout = QVBoxLayout(self._banner)
        banner_layout.setContentsMargins(10, 8, 10, 8)
        banner_layout.setSpacing(8)
        self._banner_title = QLabel("Модель недоступна")
        self._banner_title.setObjectName("offlineTitle")
        banner_layout.addWidget(self._banner_title)
        self._problem = QLabel("")
        self._problem.setObjectName("offlineDetail")
        self._problem.setWordWrap(True)
        banner_layout.addWidget(self._problem)
        self._retry = QPushButton("Проверить снова")
        self._retry.setObjectName("auxButton")
        self._retry.setToolTip(
            "Ещё раз спросить у сервера список моделей.\n"
            "Терминал при этом не закрывается."
        )
        self._retry.clicked.connect(self.retry_requested.emit)
        banner_layout.addWidget(self._retry, 0, Qt.AlignmentFlag.AlignLeft)
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        scroll = QScrollArea()
        scroll.setObjectName("historyScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._messages_host = QWidget()
        self._messages_host.setObjectName("historyHost")
        self._messages = QVBoxLayout(self._messages_host)
        self._messages.setContentsMargins(4, 4, 4, 8)
        self._messages.setSpacing(8)
        self._messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._messages.addStretch(1)
        scroll.setWidget(self._messages_host)
        scroll.viewport().setAutoFillBackground(False)
        self._scroll = scroll
        scroll.viewport().installEventFilter(self)
        root.addWidget(scroll, 1)
        self._picker = self._build_picker()
        root.addWidget(self._picker, 1)
        self._picker.setVisible(False)
        self._bound = True
        self._current_model = ""
        self._fitting = False
        self._fit_again = False

        box = QFrame()
        box.setObjectName("inputPanel")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(8, 4, 8, 4)
        box_layout.setSpacing(4)
        self._model_name = QLabel("")
        self._model_name.setObjectName("chatModelName")
        box_layout.addWidget(self._model_name)
        self._input = QTextEdit()
        self._input.setObjectName("chatInput")
        self._input.setFont(mono)
        self._mono = mono
        self._input.setPlaceholderText("Промпт... Enter - отправить, Ctrl+Enter или Shift+Enter- новая строка")
        self._input.document().setDocumentMargin(2)
        self._input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._input.setFixedHeight(self._input_min_height())
        self._input.textChanged.connect(self._fit_input)
        self._input.installEventFilter(self)
        self._send = QPushButton("Отправить")
        self._send.setObjectName("sendButton")
        self._send.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._send.clicked.connect(self._on_primary_click)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._input, 1)
        row.addWidget(self._send, 0)
        box_layout.addLayout(row)
        self._input_box = box
        root.addWidget(box)

        self._thinking = QLabel(_THINKING_FRAMES[0])
        self._thinking.setObjectName("chatThinking")
        self._thinking_phase = 0
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(420)
        self._thinking_timer.timeout.connect(self._advance_thinking)
        self._request_card: QWidget | None = None

    def _build_picker(self) -> QWidget:
        host = QWidget()
        host.setObjectName("chatModelPicker")
        outer = QVBoxLayout(host)
        outer.setContentsMargins(24, 24, 24, 24)
        column = QWidget()
        column.setMaximumWidth(360)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        hint = QLabel("Выберите модель для этого чата")
        hint.setObjectName("chatModelHint")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)
        self._picker_combo = QComboBox()
        self._picker_combo.setObjectName("chatModelPickerCombo")
        self._picker_combo.activated.connect(self._on_picker_model)
        layout.addWidget(self._picker_combo)
        self._picker_empty = QLabel("Сначала добавьте модель в настройках.")
        self._picker_empty.setObjectName("chatModelEmpty")
        self._picker_empty.setWordWrap(True)
        self._picker_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._picker_empty.setVisible(False)
        layout.addWidget(self._picker_empty)
        outer.addStretch(1)
        outer.addWidget(column, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        return host

    def set_models(self, items: List[Tuple[str, str, str]], current_id: str) -> None:
        """Список сохранённых моделей. Пустой current_id — модель ещё не выбрана."""
        self._current_model = current_id
        self._fill_model_combo(self._model_combo, items, current_id, placeholder=False)
        self._fill_model_combo(self._picker_combo, items, "", placeholder=True)
        names = {profile_id: name for profile_id, name, _model in items}
        bound = bool(current_id)
        self._bound = bound
        self._picker.setVisible(not bound)
        self._scroll.setVisible(bound)
        self._input_box.setVisible(bound)
        for widget in (
            self._model_combo,
            self._auto_run,
            self._review_output,
            self._clear,
            self._usage,
        ):
            widget.setVisible(bound)
        has_models = bool(items)
        self._picker_combo.setVisible(not bound and has_models)
        self._picker_empty.setVisible(not bound and not has_models)
        if bound:
            self._model_name.setText(names.get(current_id) or "Модель не найдена")
        else:
            self._banner.setVisible(False)
            self._model_name.clear()

    def popup_unbound_model(self) -> None:
        """Раскрыть список, если модель ещё не выбрана и есть из чего выбирать."""
        combo = self._picker_combo
        if self._bound or not combo.isVisible() or combo.count() <= 1:
            return

        def open() -> None:
            if not self._bound and combo.isVisible():
                combo.showPopup()

        QTimer.singleShot(0, open)

    def _fill_model_combo(
        self,
        combo: QComboBox,
        items: List[Tuple[str, str, str]],
        current_id: str,
        placeholder: bool,
    ) -> None:
        combo.blockSignals(True)
        combo.clear()
        if placeholder:
            combo.addItem("Выберите модель", "")
        for profile_id, name, model_id in items:
            label = name
            if model_id and model_id != name:
                label = f"{name}  ·  {model_id}"
            combo.addItem(label, profile_id)
        if current_id:
            combo.setCurrentIndex(combo.findData(current_id))
        combo.blockSignals(False)

    def _on_picker_model(self, index: int) -> None:
        profile_id = str(self._picker_combo.itemData(index) or "")
        if profile_id:
            self.model_requested.emit(profile_id)

    def _on_header_model(self, index: int) -> None:
        profile_id = str(self._model_combo.itemData(index) or "")
        if profile_id and profile_id != self._current_model:
            self.model_requested.emit(profile_id)

    def eventFilter(self, obj, event):  # type: ignore[override]
        scroll = getattr(self, "_scroll", None)
        if scroll is not None and obj is scroll.viewport() and event.type() == QEvent.Type.Resize:
            clamp_scroll_host(scroll)
            self._fit_bubbles()
            return False
        field = getattr(self, "_input", None)
        if obj is field and event.type() == QEvent.Type.Resize:
            self._fit_input()
            return False
        if obj is field and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                mods = event.modifiers()
                if mods & (
                    Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
                ):
                    field.insertPlainText("\n")
                    return True
                if not mods & Qt.KeyboardModifier.AltModifier:
                    self.send_requested.emit()
                    return True
        return super().eventFilter(obj, event)

    def set_messages(self, messages: List[Tuple[str, str]]) -> None:
        self._messages_host.setMinimumHeight(0)
        self._clear_messages()
        for role, text in messages:
            self._append(role, text)
        card = self._request_card
        if card is not None:
            self._messages.removeWidget(card)
            self._messages.insertWidget(self._messages.count() - 1, card)
        def settle() -> None:
            self._fit_bubbles()
            self._scroll_bottom()

        QTimer.singleShot(0, settle)

    def append_message(self, role: str, text: str) -> None:
        self._append(role, text)

        def settle() -> None:
            self._messages_host.setMinimumHeight(0)
            self._fit_bubbles()
            self._scroll_bottom()

        QTimer.singleShot(0, settle)

    def input_text(self) -> str:
        return self._input.toPlainText()

    def set_input_text(self, text: str) -> None:
        self._input.setPlainText(text)
        cursor = self._input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._input.setTextCursor(cursor)

    def clear_input(self) -> None:
        self._input.clear()
        self._fit_input()

    def focus_input(self) -> None:
        self._input.setFocus()

    def auto_run_on_match(self) -> bool:
        return self._auto_run.isChecked()

    def review_before_send(self) -> bool:
        return self._review_output.isChecked()

    def set_context(self, text: str) -> None:
        self._usage_tokens = 0
        self._show_usage(text, over=False)

    def set_usage(self, tokens: int, messages: int, limit: int) -> None:
        """Счётчик занятого контекста: потрачено / лимит."""
        self._usage_messages = messages
        self._usage_tokens = tokens
        if limit > 0:
            self._limit.blockSignals(True)
            self._limit.setValue(limit)
            self._limit.blockSignals(False)
            self._show_usage(f"{tokens} / {limit}", tokens > limit)
            self._usage.setToolTip(
                f"{messages} сообщ. в запросе к модели.\n"
                "Нажмите, чтобы задать лимит или сбросить контекст."
            )
            return
        if messages:
            self._show_usage(f"~{tokens}", over=False)
        else:
            self._show_usage("лимит", over=False)

    def _show_usage(self, text: str, over: bool) -> None:
        self._usage.setText(text or "лимит")
        self._usage.setVisible(True)
        self._usage.setProperty("over", "true" if over else "false")
        self._usage.style().unpolish(self._usage)
        self._usage.style().polish(self._usage)

    def _open_limit(self) -> None:
        popup = self._limit_popup
        popup.adjustSize()
        origin = self._usage.mapToGlobal(QPoint(0, self._usage.height() + 4))
        popup.move(origin)
        popup.show()
        self._limit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._limit.selectAll()

    def _hide_limit(self) -> None:
        self._limit_popup.hide()

    def _on_limit_changed(self, value: int) -> None:
        self._show_usage(
            f"{self._usage_tokens} / {value}",
            self._usage_tokens > value,
        )
        self.limit_changed.emit(value)

    def show_command_request(
        self,
        request_id: str,
        intent: str,
        command: str,
        parts: Tuple[str, ...] = (),
        spans: Sequence[Sequence[object]] = (),
    ) -> None:
        self.clear_command_request()
        card = _CommandRequest(
            request_id, intent, command, self._mono, parts=parts, spans=spans
        )
        card.decided.connect(self.command_decided.emit)
        card.output_decided.connect(self.output_decided.emit)
        card.rule_requested.connect(self.rule_requested.emit)
        self._request_card = card
        self._messages.insertWidget(self._messages.count() - 1, card)
        self.set_thinking(False)
        self._scroll_bottom()

    def update_command_preview(
        self,
        request_id: str,
        parts: Tuple[str, ...],
        spans: Sequence[Sequence[object]],
    ) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.set_preview(parts, spans)

    def show_command_running(self, request_id: str) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.show_running()
        self._scroll_bottom()

    def note_command_output(self, request_id: str, text: str, replace: bool) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.note_output(text, replace)
        self._scroll_bottom()

    def show_command_result(
        self, request_id: str, output: str, model_text: str, code: object
    ) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.show_result(output, model_text, code)
        self._scroll_bottom()

    def clear_command_request(self) -> None:
        card = self._request_card
        self._request_card = None
        if card is None:
            return
        self._messages.removeWidget(card)
        card.deleteLater()

    def set_problem(self, text: str, title: str = "") -> None:
        self._problem.setText(text)
        self._banner_title.setText(title or "Модель недоступна")
        tool_failure = title == "Ответ не завершён"
        self._retry.setVisible(not tool_failure)
        self._banner.setVisible(bool(text) and self._bound)

    def is_busy(self) -> bool:
        return self._input.isReadOnly()

    def _on_primary_click(self) -> None:
        if self.is_busy():
            self.stop_requested.emit()
            return
        self.send_requested.emit()

    def set_busy(self, busy: bool) -> None:
        self._retry.setEnabled(not busy)
        self._input.setReadOnly(busy)
        self._send.setEnabled(True)
        if busy:
            self._send.setText("Остановить")
            self._send.setObjectName("stopButton")
        else:
            self._send.setText("Отправить")
            self._send.setObjectName("sendButton")
        self._send.style().unpolish(self._send)
        self._send.style().polish(self._send)

    def set_thinking(self, thinking: bool) -> None:
        if self._request_card is not None:
            thinking = False
        if thinking:
            already = (
                self._thinking.isVisible()
                and self._messages.indexOf(self._thinking) >= 0
            )
            self._messages.removeWidget(self._thinking)
            self._messages.insertWidget(self._messages.count() - 1, self._thinking)
            self._thinking.setVisible(True)
            if not already:
                self._thinking_phase = 0
                self._thinking.setText(_THINKING_FRAMES[0])
                self._thinking_timer.start()
            self._scroll_bottom()
            return
        self._thinking_timer.stop()
        self._thinking.setVisible(False)
        self._messages.removeWidget(self._thinking)

    def _advance_thinking(self) -> None:
        self._thinking_phase = (self._thinking_phase + 1) % len(_THINKING_FRAMES)
        self._thinking.setText(_THINKING_FRAMES[self._thinking_phase])

    def _clear_messages(self) -> None:
        index = 0
        while index < self._messages.count():
            widget = self._messages.itemAt(index).widget()
            if widget is None or widget is self._thinking or widget is self._request_card:
                index += 1
                continue
            self._messages.takeAt(index)
            widget.deleteLater()

    def _input_min_height(self) -> int:
        field = getattr(self, "_input", None)
        if field is None:
            return 28
        return one_line_height(field)

    def _input_max_height(self) -> int:
        field = getattr(self, "_input", None)
        if field is None:
            return 36 + 16 * 4
        return five_line_height(field)

    def _fit_input(self) -> None:
        field = getattr(self, "_input", None)
        if field is None:
            return
        width = field.viewport().width()
        if width < 20:
            return
        base = one_line_height(field)
        if not field.toPlainText():
            height = base
        else:
            document = field.document()
            document.setTextWidth(width)
            margins = field.contentsMargins()
            height = int(
                document.size().height()
                + margins.top()
                + margins.bottom()
                + field.frameWidth() * 2
            )
            height = max(base, min(self._input_max_height(), height))
        if field.height() != height:
            field.setFixedHeight(height)
        button = getattr(self, "_send", None)
        if button is not None and button.height() != height:
            button.setFixedHeight(height)

    def _append(self, role: str, text: str) -> QWidget:
        shown = text.strip("\n")
        if role == "result":
            widget = call_note(shown)
            if widget is None:
                widget = FittedText(shown)
                widget.setObjectName("toolOutcome")
            widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            self._insert_message(widget)
            return widget
        if role == "os":
            self._append("user", "Какая операционная система в этой сессии?")
            answer = text.strip() or "Не удалось определить."
            return self._append("assistant", answer)
        if role == "reset":
            widget = self._bubble(
                "Контекст сброшен. Модель не видит сообщения выше.",
                "chatReset",
            )
        elif role == "assistant":
            widget = self._assistant_message(shown)
        else:
            widget = self._bubble(shown, "chatUser")
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._insert_message(widget)
        return widget

    def _scroll_widget_top(self, widget: QWidget) -> None:
        try:
            self._fit_bubbles()
            self._messages.activate()
            top = max(0, widget.y() - 4)
            viewport_h = max(1, self._scroll.viewport().height())
            self._messages_host.setMinimumHeight(top + viewport_h)
            self._messages.activate()
            bar = self._scroll.verticalScrollBar()
            bar.setValue(min(bar.maximum(), top))
        except RuntimeError:
            return

    def _assistant_message(self, text: str) -> QWidget:
        parts = split_shell_blocks(text)
        if len(parts) == 1 and parts[0][0] == "text":
            return self._bubble(parts[0][1], "chatAssistant")
        box = QWidget()
        box.setObjectName("chatAssistant")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)
        for kind, payload in parts:
            if kind == "shell":
                layout.addWidget(self._shell_block(payload))
            elif kind == "edit":
                layout.addWidget(self._edit_block(payload))
            elif payload.strip():
                layout.addWidget(self._bubble(payload.strip("\n"), "chatPlain"))
        return box

    def _shell_block(self, code: str) -> QWidget:
        frame = QFrame()
        frame.setObjectName("shellBlock")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        code_view = _SelectableText(code, "shellCode")
        code_view.setFont(self._mono)
        layout.addWidget(code_view)
        row = QHBoxLayout()
        run = QPushButton("Выполнить")
        run.setObjectName("auxButton")
        run.setToolTip("Сразу выполнить эту команду в терминале сессии")
        run.clicked.connect(lambda _checked=False, command=code: self.run_command.emit(command))
        insert = QPushButton("Вставить")
        insert.setObjectName("auxButton")
        insert.setToolTip("Вставить команду в поле ввода терминала")
        insert.clicked.connect(
            lambda _checked=False, command=code: self.insert_command.emit(command)
        )
        row.addWidget(run)
        row.addWidget(insert)
        row.addStretch(1)
        layout.addLayout(row)
        return frame

    def _edit_block(self, body: str) -> QWidget:
        frame = QFrame()
        frame.setObjectName("shellBlock")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        parsed = parse_edit_block(body)
        if parsed is None:
            layout.addWidget(
                self._bubble(
                    "Блок замены не разобран. Нужны строки path:, OLD: и NEW:.\n" + body,
                    "shellCode",
                )
            )
            return frame
        path, old, new = parsed
        layout.addWidget(self._bubble(path, "chatPlain"))
        old_label = self._bubble(old, "shellCode")
        old_label.setFont(self._mono)
        layout.addWidget(old_label)
        layout.addWidget(self._bubble("→", "chatPlain"))
        new_label = self._bubble(new, "shellCode")
        new_label.setFont(self._mono)
        layout.addWidget(new_label)
        button = QPushButton("Заменить")
        button.setObjectName("auxButton")
        button.setToolTip(
            "Заменить этот фрагмент в файле.\n"
            "Файл изменится только если фрагмент найден ровно один раз."
        )
        button.clicked.connect(
            lambda _checked=False, item=parsed: self.apply_edit.emit(item[0], item[1], item[2])
        )
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        return frame

    def _bubble(self, text: str, name: str) -> _SelectableText:
        return _SelectableText(text, name)

    def _fit_bubbles(self) -> None:
        """Высота пузыря равна тексту. Иначе длинный ответ раздувается и текст уезжает вниз."""
        if self._fitting:
            self._fit_again = True
            return
        self._fitting = True
        try:
            for view in self._messages_host.findChildren(_SelectableText):
                if view.objectName() in _SELECTABLE:
                    view.fit_to_text()
        finally:
            self._fitting = False
        if self._fit_again:
            self._fit_again = False
            self._fit_bubbles()

    def _insert_message(self, widget: QWidget) -> None:
        index = self._messages.count() - 1
        thinking_index = self._messages.indexOf(self._thinking)
        if thinking_index >= 0:
            index = thinking_index
        self._messages.insertWidget(index, widget)
        QTimer.singleShot(0, self._fit_bubbles)

    def _scroll_bottom(self) -> None:
        """В самый низ. Второй проход ловит высоту, которая появляется после раскладки."""

        def go(repeat: bool = True) -> None:
            try:
                self._messages.activate()
                bar = self._scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return
            if repeat:
                QTimer.singleShot(0, lambda: go(False))

        QTimer.singleShot(0, go)

    def _scroll_down(self) -> None:
        try:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            return
