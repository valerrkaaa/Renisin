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

from application.terminal_mcp import format_command_results, parse_call
from screen_log import plain_terminal_text
from ui.command_input import five_line_height, one_line_height
from ui.fitted_text import FittedText, clamp_scroll_host, fit_limited_height, scroll_or_chain
from ui.terminal_view import TerminalView

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
_COMMAND_MAX_LINES = 20
_OUTPUT_MAX_LINES = 20
_CODE_MARGIN = 6
_CHANGED_MARK = " (изменённое)"


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
        cap = self.maximumWidth()
        if 30 <= cap < 10_000 and cap < width:
            width = cap
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
            content = math.ceil(document.size().height())
            height = max(1, content + chrome + 8)
            if self.height() != height:
                self.setFixedHeight(height)
            # setFixedHeight не обновляет диапазон, если ширина текста не
            # изменилась: полоса остаётся от прежней высоты в одну строку.
            visible = max(1, self.viewport().height())
            bar = self.verticalScrollBar()
            bar.setPageStep(visible)
            bar.setRange(0, max(0, content - visible))
            bar.setValue(0)
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
    """Карточка вызова. По нажатию показывает вывод команды."""

    def __init__(
        self,
        status: str,
        command: str,
        output: str,
        mcp: str = "",
        variant: str = "session",
    ) -> None:
        super().__init__()
        self._summary = None
        self._status = status.strip() or "выполнено"
        self._command = command
        self._open = False
        self._variant = variant
        self.setObjectName("callNote")
        self.setProperty("variant", variant)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(12, 8, 12, 8)
        head.setSpacing(8)
        self._chevron = QLabel("> ")
        self._chevron.setObjectName("noteChevron")
        self._mcp = " ".join(mcp.split())
        self._mcp_label = QLabel(self._mcp)
        self._mcp_label.setObjectName("noteMcp")
        self._mcp_label.setWordWrap(True)
        self._mcp_label.hide()
        collapsed = " ".join(command.split()) or self._status
        if variant == "lounge" and self._mcp:
            collapsed = f"{self._mcp}: {collapsed}" if collapsed else self._mcp
        self._summary = QLabel(collapsed)
        self._summary.setObjectName("toolCall")
        self._summary.setWordWrap(True)
        head.addWidget(self._chevron, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        column.addWidget(self._mcp_label)
        column.addWidget(self._summary)
        head.addLayout(column, 1)
        layout.addLayout(head)
        self._summary.installEventFilter(self)
        self._chevron.installEventFilter(self)
        self._body = QWidget()
        self._body.setObjectName("toolCallBody")
        body = QVBoxLayout(self._body)
        body.setContentsMargins(28, 0, 12, 12)
        body.setSpacing(4)
        self._command_view = _SelectableText(command, "commandText", margin=0)
        body.addWidget(self._command_view)
        self._answer_view = _SelectableText(output, "commandOutput", margin=0)
        body.addWidget(self._answer_view)
        self._body.hide()
        layout.addWidget(self._body)
        self._show_summary()

    def _show_summary(self) -> None:
        self._chevron.setText("v " if self._open else "> ")
        self._summary.setVisible(not self._open)
        self._mcp_label.setVisible(self._open and bool(self._mcp))

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if (
            obj in (self._summary, self._chevron)
            and self._summary is not None
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


def call_note(text: str, variant: str = "session") -> Optional[QWidget]:
    data = parse_call(text)
    if data is None:
        return None
    return _CallNote(
        str(data.get("status") or ""),
        str(data.get("command") or ""),
        str(data.get("output") or ""),
        str(data.get("mcp") or ""),
        variant=variant,
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


class _CaptureEdit(QPlainTextEdit):
    """Поле вывода. Колесо крутит его, пока текст не упёрся в край жеста.

    Жест вверх, начатый уже у верхнего края, крутит ленту чата.
    Жест, который только доехал до края, чат не двигает.
    """

    def __init__(self) -> None:
        super().__init__()
        self._wheel_at = 0.0
        self._gesture_chains = False
        self._fitting = False
        self._max_lines = 0
        self.viewport().installEventFilter(self)
        self.viewport().setAutoFillBackground(False)

    def enable_line_fit(self, max_lines: int = _COMMAND_MAX_LINES) -> None:
        """Расти по тексту до max_lines, дальше прокрутка внутри блока."""
        self._max_lines = max(1, int(max_lines))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(_CODE_MARGIN)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.textChanged.connect(self._fit)
        self._fit()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._max_lines:
            self._fit()

    def _fit(self) -> None:
        if self._max_lines:
            fit_limited_height(self, self._max_lines)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        self._scroll_wheel(event)

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.viewport() and event.type() == QEvent.Type.Wheel:
            self._scroll_wheel(event)
            return True
        return super().eventFilter(obj, event)

    def _scroll_wheel(self, event) -> None:
        scroll_or_chain(self, event)


class _CommandText(QTextEdit):
    """Текст команды. Совпавшая с шаблоном часть зелёная, операторы — обычные."""

    def __init__(
        self,
        mono: QFont,
        parts: Tuple[str, ...],
        spans: Sequence[Sequence[object]],
        banner: bool = True,
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
        self.document().setDocumentMargin(_CODE_MARGIN)
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
        if banner:
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
                scroll_or_chain(self, event)
                return True
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
        fit_limited_height(self, _COMMAND_MAX_LINES)


def _count_phrase(n: int) -> str:
    last = n % 10
    last2 = n % 100
    if last == 1 and last2 != 11:
        word = "команда"
    elif last in {2, 3, 4} and last2 not in {12, 13, 14}:
        word = "команды"
    else:
        word = "команд"
    return f"{n} {word}"


def _step_specs(
    intent: str,
    command: str,
    parts: Tuple[str, ...],
    steps: Sequence[object],
) -> Tuple[list, bool]:
    """Пары описание/команда и признак: выполнять по отдельности."""
    real = tuple(steps)
    if real:
        items = []
        for step in real:
            text = str(getattr(step, "command", "") or "")
            description = str(getattr(step, "description", "") or "")
            if isinstance(step, tuple):
                description = str(step[0] if len(step) > 1 else "")
                text = str(step[-1])
            items.append((description, text))
        return items, True
    shown = [part.strip() for part in parts if part and str(part).strip()]
    if len(shown) > 1:
        return [(intent, part) for part in shown], False
    return [(intent, command)], False


class _CommandRequest(QFrame):
    """Подтверждение команды, которую модель хочет выполнить через MCP."""

    decided = Signal(str, str, str)
    rule_requested = Signal(str)
    output_decided = Signal(str, str, bool, str, object)
    stdin_submitted = Signal(str)
    input_generated = Signal(str)
    interrupt_requested = Signal(str)
    resized = Signal(int, int)

    def __init__(
        self,
        request_id: str,
        intent: str,
        command: str,
        mono: QFont,
        target: str = "",
        parts: Tuple[str, ...] = (),
        spans: Sequence[Sequence[object]] = (),
        steps: Sequence[object] = (),
    ) -> None:
        super().__init__()
        self._request_id = request_id
        self._command = command
        self._parts = parts
        self.setObjectName("commandRequest")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        self._mono = mono

        title = QLabel("Запрос на выполнение команд")
        title.setObjectName("requestTitle")
        title.setWordWrap(True)
        self._title = title
        specs, separate = _step_specs(intent, command, parts, steps)
        self._joined = not separate
        self._multi = separate and len(specs) > 1
        meta = QLabel(f"{_count_phrase(len(specs))} · ожидает подтверждения")
        meta.setObjectName("commandMeta")
        meta.setWordWrap(True)
        self._meta = meta
        add_rule = QPushButton("Добавить правило")
        add_rule.setObjectName("auxButton")
        add_rule.setToolTip(
            "Открыть шаблон для ОС этого подключения.\n"
            "Команда при этом не выполняется."
        )
        add_rule.clicked.connect(lambda: self.rule_requested.emit(self._request_id))
        head_main = QVBoxLayout()
        head_main.setContentsMargins(0, 0, 0, 0)
        head_main.setSpacing(3)
        head_main.addWidget(title)
        if target:
            where = QLabel(f"MCP: {target}")
            where.setObjectName("commandTarget")
            where.setWordWrap(True)
            head_main.addWidget(where)
        head_main.addWidget(meta)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addLayout(head_main, 1)
        head.addWidget(add_rule, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self._step_rows: list = []
        self._active_output = 0
        self._command_edit: QPlainTextEdit | None = None
        steps_host = QWidget()
        steps_host.setObjectName("commandSteps")
        steps_layout = QVBoxLayout(steps_host)
        steps_layout.setContentsMargins(0, 0, 0, 0)
        steps_layout.setSpacing(8)
        for index, (description, text) in enumerate(specs):
            self._add_step(steps_layout, index, description, text)
        layout.addWidget(steps_host)
        self._code = self._step_rows[0]["editor"]
        self._command_edit = self._code
        self._output = self._step_rows[0]["output"]
        self._outputs = [row["output"] for row in self._step_rows]

        self._stdin = QLineEdit()
        self._stdin.setObjectName("commandStdin")
        self._stdin.setFont(mono)
        self._stdin.setPlaceholderText("Ввод в программу, затем Enter")
        self._stdin.setToolTip(
            "Сюда попадает то, что ждёт программа.\n"
            "q и Enter закрывают more. Кнопка «Прервать» останавливает её."
        )
        self._stdin.returnPressed.connect(self._submit_stdin)
        self._stop = QPushButton("Прервать")
        self._stop.setObjectName("auxButton")
        self._stop.setToolTip("Остановить программу, которая ждёт ввод")
        self._stop.clicked.connect(self._interrupt)
        typing = QHBoxLayout()
        typing.setContentsMargins(0, 0, 0, 0)
        typing.setSpacing(8)
        typing.addWidget(self._stdin, 1)
        typing.addWidget(self._stop)
        self._screen = TerminalView(mono)
        self._screen.setObjectName("commandScreen")
        self._screen.setVisible(False)
        self._screen.input_generated.connect(self.input_generated.emit)
        self._screen.resized.connect(self.resized.emit)
        self._live_host = QWidget()
        self._live_host.setObjectName("commandLive")
        live_layout = QVBoxLayout(self._live_host)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(6)
        live_layout.addLayout(typing)
        live_layout.addWidget(self._screen)
        self._live_host.setParent(self)
        self._live_host.setVisible(False)

        run = QPushButton("Выполнить")
        run.setObjectName("primaryButton")
        run.setToolTip("Выполнить команду один раз")
        run.clicked.connect(lambda: self._choose("run", ""))
        self._run = run
        explain = QPushButton("Отклонить по причине")
        explain.setObjectName("auxButton")
        explain.setToolTip("Отказать и передать модели причину")
        explain.clicked.connect(self._show_reason)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(run)
        actions.addWidget(explain)
        actions.addStretch(1)
        self._approval_row = QWidget()
        self._approval_row.setLayout(actions)
        layout.addWidget(self._approval_row)

        self._secret = False
        self._captured = ""
        self._model_text = ""
        self._exit_code: object = None
        self._editable = False
        self._settled = False
        self._prompted = False
        self._raw = ""
        self._live = ""

        send = QPushButton("Отправить")
        send.setObjectName("primaryButton")
        send.setToolTip("Передать модели вывод команды")
        send.clicked.connect(self._send_output)
        self._send_review = send
        for editor in self._outputs:
            editor.textChanged.connect(self._sync_send_label)
        review_reject = QPushButton("Отклонить по причине")
        review_reject.setObjectName("auxButton")
        review_reject.setToolTip("Отказать и передать модели причину")
        review_reject.clicked.connect(self._show_reason)
        review = QHBoxLayout()
        review.setContentsMargins(0, 0, 0, 0)
        review.setSpacing(8)
        review.addWidget(send)
        review.addWidget(review_reject)
        review.addStretch(1)
        self._review_row = QWidget()
        self._review_row.setLayout(review)
        self._review_row.setVisible(False)
        layout.addWidget(self._review_row)
        hint = QLabel(
            "Вывод можно исправить перед отправкой — модель получит уже правленый текст."
        )
        hint.setObjectName("commandHint")
        hint.setWordWrap(True)
        hint.setVisible(False)
        self._hint = hint
        layout.addWidget(hint)

        self._reason_row = QWidget()
        reason_layout = QHBoxLayout(self._reason_row)
        reason_layout.setContentsMargins(0, 0, 0, 0)
        reason_layout.setSpacing(8)
        self._reason = QLineEdit()
        self._reason.setObjectName("commandReason")
        self._reason.setPlaceholderText("Почему команда отклонена")
        self._reason.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._reason.setMinimumHeight(self._reason.fontMetrics().height() + 12)
        self._reason.returnPressed.connect(self._submit_reason)
        submit = QPushButton("Отправить отказ")
        submit.setObjectName("auxButton")
        submit.clicked.connect(self._submit_reason)
        reason_layout.addWidget(self._reason, 1)
        reason_layout.addWidget(submit)
        self._reason_row.setVisible(False)
        layout.addWidget(self._reason_row)
        self._add_rule = add_rule
        self._approval = (run, explain)
        self._review_buttons = (send, review_reject, submit)
        self._buttons = self._approval + (submit,)
        self._sync_run_label()

    def _add_step(
        self,
        layout: QVBoxLayout,
        index: int,
        description: str,
        command: str,
    ) -> None:
        frame = QFrame()
        frame.setObjectName("commandStep")
        body = QVBoxLayout(frame)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(6)
        desc_row = QHBoxLayout()
        desc_row.setSpacing(6)
        number = QLabel(str(index + 1))
        number.setObjectName("commandStepIndex")
        number.setFont(self._mono)
        desc_row.addWidget(number, 0, Qt.AlignmentFlag.AlignTop)
        label = QLabel(description or "Команда")
        label.setObjectName("commandIntent")
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        desc_row.addWidget(label, 1)
        body.addLayout(desc_row)

        prompt = QLabel("$")
        prompt.setObjectName("commandPrompt")
        prompt.setFont(self._mono)
        editor = self._make_command_editor(command)
        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(6)
        cmd_row.addWidget(prompt, 0, Qt.AlignmentFlag.AlignTop)
        cmd_row.addWidget(editor, 1)
        cmd_wrap = QWidget()
        cmd_wrap.setObjectName("commandCmd")
        cmd_inner = QHBoxLayout(cmd_wrap)
        cmd_inner.setContentsMargins(6, 6, 6, 6)
        cmd_inner.setSpacing(6)
        cmd_inner.addLayout(cmd_row)
        body.addWidget(cmd_wrap)

        output = self._make_output()
        rail = QFrame()
        rail.setObjectName("commandOutRail")
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(10, 6, 0, 0)
        rail_layout.setSpacing(6)
        rail_layout.addWidget(output)
        rail.setVisible(False)
        body.addWidget(rail)
        layout.addWidget(frame)
        self._step_rows.append(
            {
                "original": command,
                "command": command,
                "description": description,
                "editor": editor,
                "preview": editor,
                "output": output,
                "rail": rail,
                "frame": frame,
                "captured": "",
                "model_text": "",
                "code": None,
            }
        )

    def _make_output(self) -> _CaptureEdit:
        output = _CaptureEdit()
        output.setObjectName("commandCapture")
        output.setFont(self._mono)
        output.setReadOnly(True)
        output.setPlaceholderText("Вывод команды")
        output.enable_line_fit(_OUTPUT_MAX_LINES)
        output.setVisible(False)
        return output

    def _make_command_editor(self, text: str) -> _CaptureEdit:
        editor = _CaptureEdit()
        editor.setObjectName("commandEdit")
        editor.setFont(self._mono)
        editor.setPlainText(text)
        editor.setReadOnly(False)
        editor.enable_line_fit(_COMMAND_MAX_LINES)
        editor.textChanged.connect(self._sync_run_label)
        return editor

    def _place_live(self, index: int) -> None:
        row = self._step_rows[index]
        rail = row["rail"]
        layout = rail.layout()
        if self._live_host.parent() is not rail:
            layout.addWidget(self._live_host)
        rail.setVisible(True)
        self._live_host.setVisible(True)

    def _reveal_output(self, editor: QWidget) -> None:
        editor.setVisible(True)
        for row in self._step_rows:
            if row["output"] is editor:
                row["rail"].setVisible(True)
                break

    def request_id(self) -> str:
        return self._request_id

    def command_to_run(self) -> str | None:
        edited = self.edited_commands()
        if edited is None:
            return None
        return edited[0] if len(edited) == 1 else "\n".join(edited)

    def edited_commands(self) -> Tuple[str, ...] | None:
        """Тексты команд, если пользователь что-то менял. Иначе None."""
        texts = [row["editor"].toPlainText() for row in self._step_rows]
        originals = [row["original"] for row in self._step_rows]
        if texts == originals:
            return None
        if any(not item.strip() for item in texts):
            return tuple(texts)
        if self._joined:
            return ("\n".join(texts),)
        return tuple(texts)

    def set_preview(self, parts: Tuple[str, ...], spans: Sequence[Sequence[object]]) -> None:
        """Текст команд после нового шаблона. Уже правленые поля не трогаем."""
        shown = [part.strip() for part in parts if part and str(part).strip()]
        if len(shown) != len(self._step_rows):
            return
        self._parts = parts
        for index, row in enumerate(self._step_rows):
            if row["editor"].toPlainText() == row["original"]:
                row["editor"].blockSignals(True)
                row["editor"].setPlainText(shown[index])
                row["editor"].blockSignals(False)
                row["original"] = shown[index]
                row["command"] = shown[index]
        self._sync_run_label()

    def begin_step(self, index: int) -> None:
        """Показать вывод той команды, которая сейчас выполняется."""
        if not self._step_rows:
            return
        self._active_output = max(0, min(index, len(self._step_rows) - 1))
        self._raw = ""
        self._live = ""
        self._secret = False
        self._prompted = False
        row = self._step_rows[self._active_output]
        editor = row["output"]
        editor.setPlainText("")
        editor.setReadOnly(True)
        editor.setPlaceholderText("Команда выполняется…")
        self._reveal_output(editor)
        self._place_live(self._active_output)
        self._stdin.setVisible(True)
        self._stdin.clear()
        self._stdin.setReadOnly(False)
        self._stdin.setEchoMode(QLineEdit.EchoMode.Normal)
        self._stdin.setPlaceholderText("Ввод в программу, затем Enter")
        self._stop.setVisible(True)
        self._screen.setVisible(False)
        self._meta.setText(
            f"{_count_phrase(len(self._step_rows))} · выполняется "
            f"{self._active_output + 1}/{len(self._step_rows)}"
        )
        QTimer.singleShot(0, self._focus_stdin)
        self._reveal_in_chat()

    def active_step_widget(self) -> QWidget | None:
        if not self._step_rows:
            return None
        return self._step_rows[self._active_output]["frame"]

    def _show_reason(self) -> None:
        self._reason.setReadOnly(False)
        self._reason_row.setVisible(True)
        self._reason_row.raise_()
        QTimer.singleShot(0, self._focus_reason)

    def _focus_reason(self) -> None:
        try:
            visible = self._reason_row.isVisible()
        except RuntimeError:
            return
        if not visible:
            return
        self._reason.setFocus(Qt.FocusReason.OtherFocusReason)
        self._reveal_in_chat()

    def _submit_reason(self) -> None:
        text = self._reason.text().strip()
        if not text:
            self._reason.setFocus()
            return
        self._choose("reject_reason", text)

    def show_running(self) -> None:
        """Команда пошла в терминал. Вывод копится в этом блоке."""
        self._approval_row.hide()
        for row in self._step_rows:
            row["editor"].setReadOnly(True)
        self._review_row.setVisible(False)
        self._hint.setVisible(False)
        self._settled = False
        self._editable = False
        self.begin_step(0)

    def note_output(self, text: str, replace: bool) -> None:
        if self._editable or self._settled:
            return
        if replace:
            self._raw = text
        else:
            self._raw += text
        self._live = plain_terminal_text(self._raw)
        editor = self._step_rows[self._active_output]["output"]
        editor.setPlainText(self._live)
        cursor = editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        editor.setTextCursor(cursor)
        if not self._prompted and _pager_waiting(self._live):
            self._prompted = True
            self._stdin.setPlaceholderText("q и Enter закрывают more")
            self._focus_stdin()

    def offer_secret(self) -> None:
        """Пароль sudo и похожие запросы. Символы не попадают в текст для модели."""
        if self._settled or self._secret:
            return
        self._secret = True
        self._place_live(self._active_output)
        self._stdin.setVisible(True)
        self._stdin.setEchoMode(QLineEdit.EchoMode.Password)
        self._stdin.setPlaceholderText("Пароль")
        self._stdin.setFocus()
        self._reveal_in_chat()

    def offer_terminal(
        self,
        data: str,
        columns: Optional[int],
        rows: Optional[int],
        alternate: bool,
    ) -> None:
        """Полноэкранный ввод: клавиши уходят в ту же сессию."""
        if self._settled:
            return
        self._place_live(self._active_output)
        if not self._screen.isVisible():
            self._screen.setVisible(True)
            self._screen.set_interactive(True)
            self._stdin.setVisible(False)
            self._reveal_in_chat()
        self._screen.feed(data, columns, rows, alternate)
        if not self._screen.hasFocus():
            self._screen.setFocus()

    def _reveal_in_chat(self) -> None:
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, ChatPane):
            parent = parent.parentWidget()
        if isinstance(parent, ChatPane):
            parent.reveal_command_request()

    def _focus_stdin(self) -> None:
        try:
            visible = self._stdin.isVisible() and self._live_host.isVisible()
        except RuntimeError:
            return
        if visible and not self._secret:
            self._stdin.setFocus(Qt.FocusReason.OtherFocusReason)

    def _interrupt(self) -> None:
        self.interrupt_requested.emit(self._request_id)

    def _submit_stdin(self) -> None:
        text = self._stdin.text()
        self._stdin.clear()
        self.stdin_submitted.emit(text + "\n")

    def _enter_review(self) -> None:
        self._settled = True
        self._editable = True
        self._title.setText("Проверка вывода перед отправкой")
        self._meta.setText(f"{_count_phrase(len(self._step_rows))} · вывод готов")
        self.setProperty("review", "true")
        self.style().unpolish(self)
        self.style().polish(self)
        self._live_host.setVisible(False)
        self._stdin.setVisible(False)
        self._stop.setVisible(False)
        self._screen.set_interactive(False)
        self._screen.setVisible(False)
        self._approval_row.hide()
        self._review_row.setVisible(True)
        self._hint.setVisible(True)
        for button in self._review_buttons:
            button.setEnabled(True)
            button.show()
        self._sync_send_label()

    def show_result(self, output: str, model_text: str, code: object) -> None:
        shown = plain_terminal_text(output if output.strip() else model_text)
        self._captured = shown
        self._raw = shown
        self._model_text = model_text
        self._exit_code = code
        row = self._step_rows[0]
        row["captured"] = shown
        row["model_text"] = model_text
        row["code"] = code
        row["output"].setPlainText(shown)
        row["output"].setReadOnly(False)
        self._reveal_output(row["output"])
        self._enter_review()

    def show_step_results(self, rows: Sequence[Sequence[object]]) -> None:
        """Пары «команда — вывод». Каждый вывод можно править."""
        for index, row in enumerate(rows):
            output = str(row[1])
            model_text = str(row[2])
            code = row[3] if len(row) > 3 else None
            stored = self._step_rows[index]
            shown = plain_terminal_text(output if output.strip() else model_text)
            stored["captured"] = shown
            stored["model_text"] = model_text
            stored["code"] = code
            editor = stored["output"]
            editor.setPlainText(shown)
            editor.setReadOnly(False)
            self._reveal_output(editor)
        if self._step_rows:
            last = self._step_rows[-1]
            self._captured = last["captured"]
            self._model_text = last["model_text"]
            self._exit_code = last["code"]
        self._enter_review()

    def _commands_changed(self) -> bool:
        return any(
            row["editor"].toPlainText() != row["original"] for row in self._step_rows
        )

    def _outputs_changed(self) -> bool:
        return any(
            row["output"].isVisible() and row["output"].toPlainText() != row["captured"]
            for row in self._step_rows
        )

    def _sync_run_label(self) -> None:
        if getattr(self, "_run", None) is None:
            return
        self._run.setText("Выполнить" + (_CHANGED_MARK if self._commands_changed() else ""))

    def _sync_send_label(self) -> None:
        if getattr(self, "_send_review", None) is None:
            return
        self._send_review.setText(
            "Отправить" + (_CHANGED_MARK if self._outputs_changed() else "")
        )

    def _send_output(self) -> None:
        pairs = []
        original = []
        edited = False
        for row in self._step_rows:
            current = row["output"].toPlainText()
            source = row["original"]
            model_text = row["model_text"] or current
            if current != row["captured"]:
                edited = True
                pairs.append((source, current))
            else:
                pairs.append((source, model_text))
            original.append((source, model_text))
        text = format_command_results(pairs)
        model_text = format_command_results(original)
        code = self._step_rows[-1]["code"] if self._step_rows else None
        for button in self._review_buttons:
            button.setEnabled(False)
        for row in self._step_rows:
            row["output"].setReadOnly(True)
            row["editor"].setReadOnly(True)
        self.output_decided.emit(
            self._request_id, text, edited, model_text, code
        )

    def _choose(self, action: str, reason: str) -> None:
        if action == "run":
            for row in self._step_rows:
                if not row["editor"].toPlainText().strip():
                    row["editor"].setFocus()
                    return
        for button in self._buttons + self._review_buttons:
            button.setEnabled(False)
        for row in self._step_rows:
            row["output"].setReadOnly(True)
            row["editor"].setReadOnly(True)
        self._reason.setReadOnly(True)
        self.decided.emit(self._request_id, action, reason)


def _pager_waiting(text: str) -> bool:
    folded = text.replace("\r", "").lower()
    return "-- more --" in folded or "--more--" in folded


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
    command_input = Signal(str, str)
    command_interrupt = Signal(str)
    command_resized = Signal(int, int)
    rule_requested = Signal(str)
    limit_changed = Signal(int)
    model_requested = Signal(str)

    def __init__(self, mono: QFont, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._width_floor = 0
        self.setObjectName("chatPane")
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(12)

        head_frame = QFrame()
        head_frame.setObjectName("chatHead")
        head = QVBoxLayout(head_frame)
        head.setContentsMargins(14, 10, 12, 10)
        head.setSpacing(8)
        title = QLabel("Чат")
        title.setObjectName("chatTitle")
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("chatModelCombo")
        self._model_combo.setMinimumWidth(0)
        self._model_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._model_combo.setMinimumContentsLength(8)
        self._model_combo.activated.connect(self._on_header_model)
        self._usage_tokens = 0
        self._usage_messages = 0
        self._usage = QPushButton("лимит")
        self._usage.setObjectName("usageButton")
        self._usage.setCursor(Qt.CursorShape.PointingHandCursor)
        self._usage.setSizePolicy(
            QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed
        )
        self._usage.setMinimumHeight(self._usage.fontMetrics().height() + 12)
        self._usage.setToolTip(
            "Сколько токенов из лимита уйдёт в модель.\n"
            "Нажмите, чтобы задать лимит или сбросить контекст."
        )
        self._usage.clicked.connect(self._open_limit)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addStretch(1)
        top.addWidget(self._model_combo, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addWidget(self._usage, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addLayout(top)
        self._auto_run = QCheckBox("Автовыполнение")
        self._auto_run.setObjectName("autoRun")
        self._auto_run.setChecked(True)
        self._auto_run.setToolTip(
            "выполнять автоматически при полном совпадении с паттернами"
        )
        self._review_output = QCheckBox("Предпросмотр вывода")
        self._review_output.setObjectName("reviewOutput")
        self._review_output.setChecked(True)
        self._review_output.setToolTip(
            "Показывать вывод команды перед отправкой модели.\n"
            "Если выключено, ответ уходит сразу."
        )
        switches = QHBoxLayout()
        switches.setSpacing(8)
        switches.addStretch(1)
        switches.addWidget(self._auto_run, 0, Qt.AlignmentFlag.AlignVCenter)
        switches.addWidget(self._review_output, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addLayout(switches)
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
        root.addWidget(head_frame)

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
        self._messages.setSpacing(10)
        self._messages.setAlignment(Qt.AlignmentFlag.AlignBottom)
        # Пустое место над сообщениями: короткая лента прижата к полю ввода.
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

        foot = QHBoxLayout()
        foot.setContentsMargins(4, 0, 4, 0)
        self._model_name = QLabel("")
        self._model_name.setObjectName("chatModelName")
        self._model_name.setMinimumWidth(0)
        self._model_name.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        foot.addWidget(self._model_name, 1)
        hint = QLabel("Shift+Enter — новая строка")
        hint.setObjectName("chatHint")
        hint.setMinimumWidth(0)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(hint, 1)
        root.addLayout(foot)

        box = QFrame()
        box.setObjectName("inputPanel")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(12, 8, 10, 8)
        box_layout.setSpacing(8)
        self._input = QTextEdit()
        self._input.setObjectName("chatInput")
        self._input.setFont(mono)
        self._mono = mono
        self._input.setPlaceholderText("Промпт…")
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

    def set_width_floor(self, width: int) -> None:
        """Ниже этой ширины колонку чата не сжать."""
        self._width_floor = max(0, int(width))
        self.setMinimumWidth(self._width_floor)
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        hint = super().minimumSizeHint()
        if self._width_floor:
            return QSize(self._width_floor, hint.height())
        return hint

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
            self._messages.insertWidget(self._tail_index(), card)
        def settle() -> None:
            self._fit_bubbles()
            self._scroll_bottom()

        QTimer.singleShot(0, settle)

    def append_message(self, role: str, text: str) -> None:
        self._append(role, text)

        def settle(repeat: bool = True) -> None:
            try:
                self._messages_host.setMinimumHeight(0)
                self._fit_bubbles()
                if self._request_card is not None:
                    self._pin_command_request()
                else:
                    self._scroll_bottom()
            except RuntimeError:
                return
            if repeat:
                QTimer.singleShot(0, lambda: settle(False))

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
        steps: Sequence[object] = (),
    ) -> None:
        self.clear_command_request()
        card = _CommandRequest(
            request_id,
            intent,
            command,
            self._mono,
            parts=parts,
            spans=spans,
            steps=steps,
        )
        card.decided.connect(self.command_decided.emit)
        card.output_decided.connect(self.output_decided.emit)
        card.rule_requested.connect(self.rule_requested.emit)
        card.stdin_submitted.connect(
            lambda data, rid=request_id: self.command_input.emit(rid, data)
        )
        card.interrupt_requested.connect(self.command_interrupt.emit)
        card.input_generated.connect(
            lambda data, rid=request_id: self.command_input.emit(rid, data)
        )
        card.resized.connect(self.command_resized.emit)
        self._request_card = card
        self._messages.insertWidget(self._tail_index(), card)
        self.set_thinking(False)
        self.reveal_command_request()

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
        self.reveal_command_request()

    def offer_command_secret(self, request_id: str) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.offer_secret()

    def feed_command_screen(
        self,
        request_id: str,
        data: str,
        columns: Optional[int],
        rows: Optional[int],
        alternate: bool,
    ) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.offer_terminal(data, columns, rows, alternate)

    def note_command_output(self, request_id: str, text: str, replace: bool) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.note_output(text, replace)

    def show_command_result(
        self, request_id: str, output: str, model_text: str, code: object
    ) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.show_result(output, model_text, code)
        self.reveal_command_request()

    def begin_command_step(self, request_id: str, index: int) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.begin_step(index)
        self.reveal_command_request()

    def show_command_steps(
        self, request_id: str, rows: Sequence[Sequence[object]]
    ) -> None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return
        card.show_step_results(rows)
        self.reveal_command_request()

    def edited_commands(self, request_id: str) -> Tuple[str, ...] | None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return None
        return card.edited_commands()

    def edited_command(self, request_id: str) -> str | None:
        card = self._request_card
        if card is None or card.request_id() != request_id:
            return None
        return card.command_to_run()

    def command_request(self):
        return self._request_card

    def take_command_request(self):
        """Снять карточку с ленты, не уничтожая её. Чат можно переключить."""
        card = self._request_card
        if card is None:
            return None
        self._request_card = None
        self._messages.removeWidget(card)
        card.hide()
        card.setParent(None)
        return card

    def attach_command_request(self, card) -> None:
        current = self._request_card
        if current is card:
            card.show()
            self.reveal_command_request()
            return
        if current is not None:
            self.clear_command_request()
        self._request_card = card
        self._messages.insertWidget(self._tail_index(), card)
        card.show()
        self.reveal_command_request()

    def clear_command_request(self) -> None:
        self._request_card = None
        layout = self._messages
        for index in range(layout.count() - 1, -1, -1):
            item = layout.itemAt(index)
            card = item.widget() if item is not None else None
            if card is None or card.objectName() != "commandRequest":
                continue
            layout.removeWidget(card)
            card.hide()
            card.setParent(None)
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
            self._messages.insertWidget(self._tail_index(self._request_card), self._thinking)
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
                widget.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
                )
            else:
                widget.setSizePolicy(
                    QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
                )
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
            widget = self._user_row(shown)
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._insert_message(widget)
        return widget

    def _assistant_message(self, text: str) -> QWidget:
        parts = split_shell_blocks(text)
        if len(parts) == 1 and parts[0][0] == "text":
            return self._assistant_row(parts[0][1])
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

    def _assistant_row(self, text: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(10)
        mark = QLabel("✦")
        mark.setObjectName("assistantMark")
        mark.setFixedSize(26, 26)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._bubble(text, "chatAssistant"), 1)
        return row

    def _user_row(self, text: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        layout.addWidget(self._bubble(text, "chatUser"), 1)
        return row

    def _bubble(self, text: str, name: str) -> _SelectableText:
        if name == "chatUser":
            bubble = _SelectableText(text, name, margin=8, left_margin=14)
            bubble.setMaximumWidth(440)
            bubble.setMinimumWidth(48)
            bubble.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            return bubble
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

    def _tail_index(self, *stay_last: QWidget) -> int:
        """Куда ставить следующий блок. Растяжка сверху не сдвигается."""
        index = self._messages.count()
        for widget in (self._thinking, self._request_card, *stay_last):
            if widget is None:
                continue
            found = self._messages.indexOf(widget)
            if found > 0:
                index = min(index, found)
        return index

    def _insert_message(self, widget: QWidget) -> None:
        # Запрос на команду остаётся последним. Иначе вывод следующей
        # команды уезжает под него, а прокрутка вниз прячет одобрение.
        self._messages.insertWidget(self._tail_index(), widget)
        QTimer.singleShot(0, self._fit_bubbles)

    def reveal_command_request(self) -> None:
        """Держит блок запроса у нижнего края. Верх — только если он выше экрана."""

        def go(repeat: bool = True) -> None:
            self._pin_command_request()
            if repeat:
                QTimer.singleShot(0, lambda: go(False))

        QTimer.singleShot(0, go)

    def _pin_command_request(self) -> None:
        card = self._request_card
        try:
            if card is None or card.isHidden():
                return
            self._messages_host.setMinimumHeight(0)
            self._messages.activate()
            bar = self._scroll.verticalScrollBar()
            view = max(1, self._scroll.viewport().height())
            step = card.active_step_widget()
            if step is not None and card.height() >= view - 8:
                top = step.mapTo(self._messages_host, QPoint(0, 0)).y()
                bottom = top + step.height()
                if bottom > bar.value() + view - 8:
                    bar.setValue(min(bar.maximum(), max(0, bottom - view + 12)))
                elif top < bar.value():
                    bar.setValue(max(0, top - 4))
                return
            if card.height() >= view - 8:
                top = max(0, card.y() - 4)
                bar.setValue(min(bar.maximum(), top))
                return
            bar.setValue(bar.maximum())
        except RuntimeError:
            return

    def _scroll_bottom(self) -> None:
        """В самый низ. Второй проход ловит высоту, которая появляется после раскладки."""

        def go(repeat: bool = True) -> None:
            try:
                self._messages_host.setMinimumHeight(0)
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
