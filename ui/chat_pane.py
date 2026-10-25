# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Правая колонка чата с моделью. Сеть отсюда не вызывается."""
from __future__ import annotations

import math
import re
from typing import List, Optional, Tuple

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QApplication,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

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


class _SelectableText(QTextEdit):
    """Текст, который выделяется мышью и копируется как в обычном поле."""

    def __init__(
        self, text: str, name: str, margin: Optional[int] = None, left_margin: Optional[int] = None
    ) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setObjectName(name)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
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
            if 30 <= available < width:
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


class _CommandRequest(QFrame):
    """Подтверждение команды, которую модель хочет выполнить через MCP."""

    decided = Signal(str, str, str)

    def __init__(
        self,
        request_id: str,
        intent: str,
        command: str,
        mono: QFont,
        target: str = "",
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self.setObjectName("commandRequest")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
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
        why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(why)
        code = QLabel(f"Команда: {command}")
        code.setObjectName("shellCode")
        code.setFont(mono)
        code.setWordWrap(True)
        code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(code)

        run = QPushButton("Выполнить")
        run.setObjectName("auxButton")
        run.setToolTip("Выполнить команду один раз")
        run.clicked.connect(lambda: self._choose("run", ""))
        always = QPushButton("Всегда выполнять")
        always.setObjectName("auxButton")
        always.setToolTip(
            "Выполнить и больше не спрашивать эту команду.\n"
            "Сохраняется только текст команды, без намерения."
        )
        always.clicked.connect(lambda: self._choose("always", ""))
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
        row.addWidget(always)
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
        self._buttons = (run, always, reject, explain, submit)

    def _show_reason(self) -> None:
        self._reason_row.setVisible(True)
        self._reason.setFocus()

    def _submit_reason(self) -> None:
        text = self._reason.text().strip()
        if not text:
            self._reason.setFocus()
            return
        self._choose("reject_reason", text)

    def _choose(self, action: str, reason: str) -> None:
        for button in self._buttons:
            button.setEnabled(False)
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
    command_decided = Signal(str, str, str)
    limit_changed = Signal(int)

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
        self._context = QLabel("")
        self._context.setObjectName("chatContext")
        head.addWidget(self._context)
        self._slash = QLabel("/")
        self._slash.setObjectName("chatContext")
        self._slash.setVisible(False)
        head.addWidget(self._slash)
        self._limit = QSpinBox()
        self._limit.setObjectName("chatLimit")
        self._limit.setRange(256, 1_000_000)
        self._limit.setSingleStep(1024)
        self._limit.setValue(8192)
        self._limit.setVisible(False)
        self._limit.setToolTip(
            "Лимит контекста. Слева — оценка того, сколько уже уйдёт в модель."
        )
        self._limit.valueChanged.connect(self._on_limit_changed)
        head.addWidget(self._limit)
        self._usage_messages = 0
        self._reset = QPushButton("Сбросить")
        self._reset.setObjectName("auxButton")
        self._reset.setToolTip(
            "Модель забудет переписку выше этой черты.\n"
            "Сообщения останутся в чате, в запрос они больше не попадут."
        )
        self._reset.clicked.connect(self.reset_requested.emit)
        head.addWidget(self._reset)
        root.addLayout(head)

        self._banner = QFrame()
        self._banner.setObjectName("offlineBanner")
        banner_layout = QVBoxLayout(self._banner)
        banner_layout.setContentsMargins(14, 12, 14, 12)
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
        self._fitting = False
        self._fit_again = False

        box = QFrame()
        box.setObjectName("inputPanel")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(8, 6, 8, 6)
        box_layout.setSpacing(0)
        self._input = QTextEdit()
        self._input.setObjectName("chatInput")
        self._input.setFont(mono)
        self._mono = mono
        self._input.setPlaceholderText(
            "Сообщение модели. Enter — отправить, Ctrl+Enter или Shift+Enter — новая строка."
        )
        self._input.document().setDocumentMargin(2)
        self._input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._input.setFixedHeight(self._input_min_height())
        self._input.textChanged.connect(self._fit_input)
        self._input.installEventFilter(self)
        self._send = QPushButton("Отправить")
        self._send.setObjectName("sendButton")
        self._send.clicked.connect(self._on_primary_click)
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._input, 1)
        row.addWidget(self._send, 0, Qt.AlignmentFlag.AlignBottom)
        box_layout.addLayout(row)
        root.addWidget(box)

        self._thinking = QLabel(_THINKING_FRAMES[0])
        self._thinking.setObjectName("chatThinking")
        self._thinking_phase = 0
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(420)
        self._thinking_timer.timeout.connect(self._advance_thinking)
        self._request_card: QWidget | None = None

    def eventFilter(self, obj, event):  # type: ignore[override]
        scroll = getattr(self, "_scroll", None)
        if scroll is not None and obj is scroll.viewport() and event.type() == QEvent.Type.Resize:
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

    def set_context(self, text: str) -> None:
        self._context.setText(text)
        self._context.setVisible(bool(text))
        self._context.setProperty("over", "false")
        self._context.style().unpolish(self._context)
        self._context.style().polish(self._context)
        self._slash.setVisible(False)
        self._limit.setVisible(False)

    def set_usage(self, tokens: int, messages: int, limit: int) -> None:
        """Счётчик занятого контекста: потрачено / лимит."""
        self._usage_messages = messages
        if limit <= 0:
            if messages:
                self.set_context(f"~{tokens} ток. · {messages} сообщ.")
            else:
                self.set_context("")
            return
        self._limit.blockSignals(True)
        self._limit.setValue(limit)
        self._limit.blockSignals(False)
        self._limit.setVisible(True)
        self._slash.setVisible(True)
        self._context.setText(str(tokens))
        self._context.setVisible(True)
        self._context.setToolTip(
            f"{messages} сообщ. в запросе к модели. Поле справа — лимит."
        )
        self._context.setProperty("over", "true" if tokens > limit else "false")
        self._context.style().unpolish(self._context)
        self._context.style().polish(self._context)

    def _on_limit_changed(self, value: int) -> None:
        tokens = 0
        try:
            tokens = int(self._context.text())
        except ValueError:
            tokens = 0
        self._context.setProperty("over", "true" if tokens > value else "false")
        self._context.style().unpolish(self._context)
        self._context.style().polish(self._context)
        self.limit_changed.emit(value)

    def show_command_request(self, request_id: str, intent: str, command: str) -> None:
        self.clear_command_request()
        card = _CommandRequest(request_id, intent, command, self._mono)
        card.decided.connect(self.command_decided.emit)
        self._request_card = card
        self._messages.insertWidget(self._messages.count() - 1, card)
        self.set_thinking(False)
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
        self._banner.setVisible(bool(text))

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
        return 36

    def _input_max_height(self) -> int:
        return 180

    def _fit_input(self) -> None:
        field = getattr(self, "_input", None)
        if field is None:
            return
        width = field.viewport().width()
        if width < 20:
            return
        document = field.document()
        document.setTextWidth(width)
        height = int(document.size().height()) + 8
        height = max(self._input_min_height(), min(self._input_max_height(), height))
        if field.height() != height:
            field.setFixedHeight(height)

    def _append(self, role: str, text: str) -> QWidget:
        shown = text.strip("\n")
        if role == "result":
            widget = QLabel(shown)
            widget.setObjectName("toolOutcome")
            widget.setWordWrap(True)
            widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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
