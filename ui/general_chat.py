# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Общий чат с моделью: вкладки, шапка и список MCP других терминалов.

Пока это кликабельная вёрстка. Сообщения не уходят в модель.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from uuid import uuid4

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
    QComboBox,
)

from ui.chat_pane import _CommandRequest, _SelectableText
from ui.command_card import CommandCard
from ui.fitted_text import FittedText, clamp_scroll_host
from ui.command_input import GrowingText

GENERAL_CHAT_ID = "general-ai-chat"
GENERAL_CHAT_TITLE = "общий чат для ИИ"


def mcp_json_fragment(entries: List[Tuple[str, str]]) -> str:
    """Фрагмент объекта mcp.json по уже поднятым серверам."""
    blocks = []
    for name, url in entries:
        blocks.append(
            f"{json.dumps(name, ensure_ascii=False)}: {{\n"
            f'"url": {json.dumps(url, ensure_ascii=False)}\n'
            "},"
        )
    return "\n".join(blocks)

_THINKING = ("Думает", "Думает.", "Думает..", "Думает...")


@dataclass
class _Tab:
    title: str
    messages: List[Tuple[str, str]] = field(default_factory=list)
    model_id: str = ""
    model_label: str = ""
    tab_id: str = field(default_factory=lambda: uuid4().hex)
    draft: str = ""
    problem: str = ""
    request: Optional[tuple] = None
    # Карточки команд: после скольких сообщений стоят и сам блок.
    cards: List[Tuple[int, CommandCard]] = field(default_factory=list)


class GeneralChatPage(QWidget):
    command_decided = Signal(str, str, str)
    rule_requested = Signal(str)
    send_requested = Signal()
    stop_requested = Signal()
    add_tab_requested = Signal()
    close_tab_requested = Signal(str)
    rename_tab_requested = Signal(str, str)
    model_requested = Signal(str, str)

    def __init__(self, mono: QFont | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("generalChat")
        self._mono = mono or QFont("Consolas")
        self._request_card: Optional[_CommandRequest] = None
        self._remote = False
        self._busy: set[str] = set()
        self._thinking_phase = 0
        self._thinking_timer = QTimer(self)
        self._thinking_timer.setInterval(400)
        self._thinking_timer.timeout.connect(self._advance_thinking)
        self._thinking_label: Optional[QLabel] = None
        self._tabs: List[_Tab] = [_Tab("чат")]
        self._current = 0
        self._models: List[Tuple[str, str]] = []
        self._editing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_chat())
        split.addWidget(self._build_mcp())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([760, 220])
        root.addWidget(split, 1)
        self._render_tabs()
        self._render_header()
        self._render_messages()

    def set_models(self, options: List[Tuple[str, str]]) -> None:
        """Варианты для вкладок, где модель ещё не выбрана."""
        self._models = list(options)
        if self._tabs and not self._tabs[self._current].model_id:
            self._fill_model_combo()

    def set_mcp_entries(self, entries: List[Tuple[str, ...]]) -> None:
        """Имя для списка, описание при наведении и адрес для mcp.json."""
        self._mcp_list.clear()
        self._mcp_targets = []
        if not entries:
            item = QListWidgetItem("Нет поднятых MCP")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._mcp_list.addItem(item)
            return
        for row in entries:
            alias = row[0]
            description = row[1] if len(row) > 1 else ""
            url = row[2] if len(row) > 2 else ""
            item = QListWidgetItem(alias)
            item.setToolTip(description.strip())
            item.setData(Qt.ItemDataRole.UserRole, url)
            self._mcp_list.addItem(item)
            if str(url).strip():
                self._mcp_targets.append((alias, str(url)))

    def append_mcp_cite(self, name: str) -> None:
        """Короткая цитата MCP в конец черновика. В модель уйдёт имя и описание."""
        label = " ".join(name.split())
        if not label:
            return
        cite = f"«MCP: {label}»"
        current = self._draft.text()
        if current and not current[-1].isspace():
            current += " "
        self.set_input_text(current + cite)
        self._draft.setFocus()

    def _cite_mcp(self, item: QListWidgetItem) -> None:
        if not (item.flags() & Qt.ItemFlag.ItemIsEnabled):
            return
        self.append_mcp_cite(item.text())

    def tab_titles(self) -> List[str]:
        return [tab.title for tab in self._tabs]

    def current_title(self) -> str:
        return self._tabs[self._current].title

    def current_model_label(self) -> str:
        return self._tabs[self._current].model_label

    def current_tab_id(self) -> str:
        return self._tabs[self._current].tab_id

    def auto_run_on_match(self) -> bool:
        return self._auto_run.isChecked()

    def has_tab(self, tab_id: str) -> bool:
        return any(tab.tab_id == tab_id for tab in self._tabs)

    def focus_tab(self, tab_id: str) -> bool:
        for index, tab in enumerate(self._tabs):
            if tab.tab_id != tab_id:
                continue
            if index != self._current:
                self._select_tab(index)
            return True
        return False

    def show_command_request(
        self,
        tab_id: str,
        request_id: str,
        intent: str,
        command: str,
        target: str,
        parts: Tuple[str, ...] = (),
        spans: tuple = (),
    ) -> None:
        tab = self._find_tab(tab_id)
        if tab is None:
            return
        tab.request = (request_id, intent, command, target, tuple(parts), spans)
        if self._tabs[self._current].tab_id == tab_id:
            self._render_messages()

    def clear_command_request(self, tab_id: str = "") -> None:
        for tab in self._tabs:
            if not tab_id or tab.tab_id == tab_id:
                tab.request = None
        self._render_messages()

    def _build_chat(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 8, 10, 10)
        layout.setSpacing(12)

        self._tab_bar = QHBoxLayout()
        self._tab_bar.setSpacing(6)
        self._tab_host = QWidget()
        self._tab_host.setLayout(self._tab_bar)
        layout.addWidget(self._tab_host)
        layout.addWidget(self._rule())

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel()
        self._title.setObjectName("loungeTitle")
        header.addWidget(self._title)
        self._title_edit = QLineEdit()
        self._title_edit.setObjectName("loungeTitleEdit")
        self._title_edit.returnPressed.connect(self._commit_title)
        self._title_edit.editingFinished.connect(self._commit_title)
        self._title_edit.setVisible(False)
        header.addWidget(self._title_edit)
        self._rename = QPushButton()
        self._rename.setObjectName("iconButton")
        self._rename.setText("✎")
        self._rename.setFixedSize(28, 28)
        self._rename.setToolTip("Переименовать чат")
        self._rename.clicked.connect(self._begin_rename)
        header.addWidget(self._rename)
        header.addStretch(1)
        self._auto_run = QCheckBox("Авто")
        self._auto_run.setObjectName("autoRun")
        self._auto_run.setChecked(True)
        self._auto_run.setToolTip(
            "выполнять автоматически при полном совпадении с паттернами"
        )
        header.addWidget(self._auto_run)
        self._model_label = QLabel()
        self._model_label.setObjectName("loungeModel")
        header.addWidget(self._model_label)
        self._model_combo = QComboBox()
        self._model_combo.setObjectName("loungeModelCombo")
        self._model_combo.setMinimumWidth(180)
        self._model_combo.activated.connect(self._pick_model)
        header.addWidget(self._model_combo)
        layout.addLayout(header)
        layout.addWidget(self._rule())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._transcript_host = QWidget()
        self._transcript = QVBoxLayout(self._transcript_host)
        self._transcript.setContentsMargins(4, 4, 4, 4)
        self._transcript.setSpacing(10)
        self._transcript.addStretch(1)
        scroll.setWidget(self._transcript_host)
        scroll.viewport().installEventFilter(self)
        self._transcript_scroll = scroll
        layout.addWidget(scroll, 1)

        self._problem = QLabel()
        self._problem.setObjectName("loungeProblem")
        self._problem.setWordWrap(True)
        self._problem.setVisible(False)
        layout.addWidget(self._problem)

        box = QFrame()
        box.setObjectName("inputPanel")
        row = QHBoxLayout(box)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(8)
        self._draft = GrowingText()
        self._draft.setObjectName("loungeDraft")
        self._draft.setPlaceholderText("Сначала выберите модель")
        self._draft.submitted.connect(self._on_primary)
        row.addWidget(self._draft, 1)
        self._send = QPushButton("Отправить")
        self._send.setObjectName("sendButton")
        self._send.setMinimumWidth(120)
        self._send.clicked.connect(self._on_primary)
        row.addWidget(self._send, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(box)
        self._sync_composer()
        return page

    @staticmethod
    def _rule() -> QFrame:
        line = QFrame()
        line.setObjectName("loungeRule")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFixedHeight(1)
        return line

    def _build_mcp(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("mcpRail")
        rail.setMinimumWidth(180)
        rail.setMaximumWidth(280)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)
        title = QLabel("MCP")
        title.setObjectName("mcpRailTitle")
        layout.addWidget(title)
        hint = QLabel("Подняты в других терминалах")
        hint.setObjectName("mcpRailHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._mcp_list = QListWidget()
        self._mcp_list.setObjectName("mcpList")
        self._mcp_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._mcp_list.itemDoubleClicked.connect(self._cite_mcp)
        layout.addWidget(self._mcp_list, 1)
        self._mcp_targets: List[Tuple[str, str]] = []
        copy = QPushButton("скопировать для mcp.json")
        copy.setObjectName("copyMcpJson")
        copy.setToolTip("Скопировать адреса поднятых MCP для вставки в mcp.json")
        copy.clicked.connect(self._copy_mcp_json)
        layout.addWidget(copy)
        return rail

    def _copy_mcp_json(self) -> None:
        QGuiApplication.clipboard().setText(mcp_json_fragment(self._mcp_targets))

    def add_tool(self, widget: QWidget) -> None:
        """Кнопка слева от вкладок. Перерисовка вкладок её не удаляет."""
        widget.setProperty("pinned", "true")
        self._tab_bar.insertWidget(0, widget)

    def _render_tabs(self) -> None:
        pinned: List[QWidget] = []
        while self._tab_bar.count():
            item = self._tab_bar.takeAt(0)
            widget = item.widget()
            if widget is not None and widget.property("pinned") == "true":
                pinned.append(widget)
            elif widget is not None:
                widget.deleteLater()
        for widget in pinned:
            self._tab_bar.addWidget(widget)
        for index, tab in enumerate(self._tabs):
            button = QPushButton(tab.title)
            button.setObjectName("chatTab")
            button.setProperty("active", "true" if index == self._current else "false")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, number=index: self._select_tab(number))
            self._style(button)
            if len(self._tabs) > 1:
                close = QPushButton("×")
                close.setObjectName("chatTabClose")
                close.setFixedSize(18, 18)
                close.clicked.connect(lambda _checked=False, number=index: self._close_tab(number))
                wrap = QWidget()
                row = QHBoxLayout(wrap)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(2)
                row.addWidget(button)
                row.addWidget(close)
                self._tab_bar.addWidget(wrap)
            else:
                self._tab_bar.addWidget(button)
        plus = QPushButton("+")
        plus.setObjectName("chatTabAdd")
        plus.setFixedSize(32, 32)
        plus.setToolTip("Новая вкладка")
        plus.clicked.connect(self._add_tab)
        self._tab_bar.addWidget(plus)
        self._tab_bar.addStretch(1)

    def _render_header(self) -> None:
        tab = self._tabs[self._current]
        self._title.setText(tab.title)
        self._title.setVisible(not self._editing)
        self._title_edit.setVisible(self._editing)
        if tab.model_id:
            self._model_label.setText(tab.model_label)
            self._model_label.setVisible(True)
            self._model_combo.setVisible(False)
        else:
            self._model_label.setVisible(False)
            self._model_combo.setVisible(True)
            self._fill_model_combo()

    def open_command_card(
        self, tab_id: str, command: str, mcp: str = ""
    ) -> Optional[CommandCard]:
        """Блок команды в ленте, как в личном чате. Пока он живой, в него можно вводить текст."""
        tab = self._find_tab(tab_id)
        if tab is None:
            return None
        card = CommandCard(command, self._mono, target=mcp)
        card.resend_button.hide()
        card.delete_button.hide()
        card.attach_button.hide()
        card.begin_live()
        tab.cards.append((len(tab.messages), card))
        if self.current_tab_id() == tab_id:
            self._render_messages()
            self._scroll_bottom()
        return card

    def feed_command_card(
        self,
        tab_id: str,
        text: str,
        columns: Optional[int],
        rows: Optional[int],
        alternate: bool,
        persist: bool,
        stdout: QTextCharFormat,
        stderr: QTextCharFormat,
        is_error: bool,
    ) -> None:
        card = self._live_card(tab_id)
        if card is None:
            return
        if not persist:
            card.feed_live(text, columns, rows, alternate)
        elif card.is_live():
            card.remember_plain(text)
        else:
            card.append_output(text, stderr if is_error else stdout)
        if self.current_tab_id() == tab_id:
            self._scroll_bottom()

    def focus_command_card(self, tab_id: str) -> None:
        card = self._live_card(tab_id)
        if card is None:
            return
        card.focus_terminal()
        if self.current_tab_id() == tab_id:
            self._scroll_bottom()

    def finish_command_card(
        self,
        tab_id: str,
        return_code: Optional[int],
        stdout: QTextCharFormat,
        exit_fmt: QTextCharFormat,
    ) -> str:
        """Завершает живую карточку и возвращает запись для журнала вкладки."""
        card = self._live_card(tab_id)
        if card is None:
            return ""
        if card.is_live():
            plain = card.take_plain()
            kept = card.end_live()
            if kept and plain:
                card.remember_plain(plain)
            elif plain:
                card.append_output(plain, stdout)
        card.finalize(return_code, exit_fmt)
        card.record = _command_record(card)
        if self.current_tab_id() == tab_id:
            self._render_messages()
        return card.record

    def _live_card(self, tab_id: str) -> Optional[CommandCard]:
        tab = self._find_tab(tab_id)
        if tab is None:
            return None
        for _index, card in reversed(tab.cards):
            if card.is_live():
                return card
        return None

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        scroll = getattr(self, "_transcript_scroll", None)
        if (
            scroll is not None
            and obj is scroll.viewport()
            and event.type() == QEvent.Type.Resize
        ):
            clamp_scroll_host(scroll)
        return super().eventFilter(obj, event)

    def _render_messages(self) -> None:
        tab = self._tabs[self._current]
        self._drop_dead_cards(tab)
        current = {card for _index, card in tab.cards}
        owned = {card for item in self._tabs for _index, card in item.cards}
        while self._transcript.count() > 1:
            item = self._transcript.takeAt(0)
            widget = item.widget()
            if widget is None or widget in current:
                continue
            widget.hide()
            widget.setParent(None)
            if widget not in owned:
                widget.deleteLater()
        self._request_card = None
        if (
            not tab.messages
            and tab.request is None
            and not tab.cards
            and tab.tab_id not in self._busy
        ):
            empty = QLabel("Сообщений пока нет")
            empty.setObjectName("mcpRailHint")
            self._transcript.insertWidget(0, empty)
            return
        grouped: dict[int, List[CommandCard]] = {}
        for index, card in tab.cards:
            grouped.setdefault(index, []).append(card)

        def place_cards(index: int) -> None:
            for card in grouped.get(index, []):
                card.show()
                self._transcript.insertWidget(self._transcript.count() - 1, card)

        saved = [
            card.record
            for _index, card in tab.cards
            if getattr(card, "record", "")
        ]

        def already_shown(text: str) -> bool:
            if text not in saved:
                return False
            saved.remove(text)
            return True

        place_cards(0)
        for number, (role, text) in enumerate(tab.messages, start=1):
            if role == "command" and already_shown(text):
                place_cards(number)
                continue
            bubble = (
                self._stored_command(text)
                if role == "command"
                else self._bubble(role, text)
            )
            self._transcript.insertWidget(self._transcript.count() - 1, bubble)
            place_cards(number)
        self._thinking_label = None
        waiting = tab.request is not None
        live = any(card.is_live() for _index, card in tab.cards)
        if tab.tab_id in self._busy and not waiting and not live:
            thinking = QLabel(_THINKING[self._thinking_phase])
            thinking.setObjectName("loungeThinking")
            self._thinking_label = thinking
            self._transcript.insertWidget(self._transcript.count() - 1, thinking)
            self._thinking_timer.start()
        else:
            self._thinking_timer.stop()
        if waiting:
            request_id, intent, command, target = tab.request[:4]
            parts = tab.request[4] if len(tab.request) > 4 else ()
            spans = tab.request[5] if len(tab.request) > 5 else ()
            card = _CommandRequest(
                request_id, intent, command, self._mono, target, parts, spans
            )
            card.decided.connect(self.command_decided.emit)
            card.rule_requested.connect(self.rule_requested.emit)
            self._request_card = card
            self._transcript.insertWidget(self._transcript.count() - 1, card)
        self._scroll_bottom()

    def update_command_preview(
        self,
        request_id: str,
        parts: tuple,
        spans: tuple,
    ) -> None:
        for tab in self._tabs:
            request = tab.request
            if request is None or request[0] != request_id:
                continue
            tab.request = (
                request[0],
                request[1],
                request[2],
                request[3] if len(request) > 3 else "",
                tuple(parts),
                spans,
            )
        card = self._request_card
        if card is not None and card.request_id() == request_id:
            card.set_preview(parts, spans)

    def _scroll_bottom(self) -> None:
        """В самый низ. Повтор после раскладки: высота блока появляется не сразу."""

        def go(repeat: bool = True) -> None:
            try:
                self._transcript.activate()
                bar = self._transcript_scroll.verticalScrollBar()
                bar.setValue(bar.maximum())
            except RuntimeError:
                return
            if repeat:
                QTimer.singleShot(0, lambda: go(False))

        QTimer.singleShot(0, go)

    def _drop_dead_cards(self, tab: _Tab) -> None:
        alive = []
        for index, card in tab.cards:
            try:
                card.isVisible()
            except RuntimeError:
                continue
            alive.append((index, card))
        tab.cards = alive

    def _stored_command(self, text: str) -> QWidget:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            return self._bubble("result", text)
        card = CommandCard(
            str(data.get("command") or ""),
            self._mono,
            target=str(data.get("mcp") or ""),
        )
        card.resend_button.hide()
        card.delete_button.hide()
        card.attach_button.hide()
        output = str(data.get("output") or "")
        if output:
            fmt = QTextCharFormat()
            fmt.setFont(self._mono)
            card.append_output(output, fmt)
        return card

    def _bubble(self, role: str, text: str) -> QWidget:
        if role == "result":
            label = FittedText(text)
            label.setObjectName("toolOutcome")
            return label
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        label = _SelectableText(
            text,
            "chatUser" if role == "user" else "chatAssistant",
            margin=6,
            left_margin=14,
        )
        label.setMaximumWidth(520)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        if role == "user":
            layout.addStretch(1)
            layout.addWidget(label)
        else:
            layout.addWidget(label)
            layout.addStretch(1)
        return row

    def _fill_model_combo(self) -> None:
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        self._model_combo.addItem("Выберите модель", "")
        for model_id, label in self._models:
            self._model_combo.addItem(label, model_id)
        self._model_combo.setCurrentIndex(0)
        self._model_combo.blockSignals(False)

    def _select_tab(self, index: int) -> None:
        if index == self._current or not 0 <= index < len(self._tabs):
            return
        self._remember_draft()
        self._editing = False
        self._current = index
        self._show_draft()
        self._render_tabs()
        self._render_header()
        self._render_messages()
        self._sync_composer()

    def _add_tab(self) -> None:
        if self._remote:
            self.add_tab_requested.emit()
            return
        self._remember_draft()
        self._tabs.append(_Tab(title=f"чат {len(self._tabs) + 1}"))
        self._current = len(self._tabs) - 1
        self._editing = False
        self._show_draft()
        self._render_tabs()
        self._render_header()
        self._render_messages()
        self._sync_composer()

    def _find_tab(self, tab_id: str) -> Optional[_Tab]:
        for tab in self._tabs:
            if tab.tab_id == tab_id:
                return tab
        return None

    def _close_tab(self, index: int) -> None:
        if len(self._tabs) < 2 or not 0 <= index < len(self._tabs):
            return
        request = self._tabs[index].request
        tab_id = self._tabs[index].tab_id
        if self._remote:
            if request is not None:
                self.command_decided.emit(request[0], "reject_reason", "вкладка закрыта")
            self.close_tab_requested.emit(tab_id)
            return
        del self._tabs[index]
        if self._current >= len(self._tabs):
            self._current = len(self._tabs) - 1
        elif self._current > index:
            self._current -= 1
        self._editing = False
        self._show_draft()
        self._render_tabs()
        self._render_header()
        self._render_messages()
        self._sync_composer()
        if request is not None:
            self.command_decided.emit(request[0], "reject_reason", "вкладка закрыта")

    def _begin_rename(self) -> None:
        self._editing = True
        self._title_edit.setText(self._tabs[self._current].title)
        self._render_header()
        self._title_edit.setFocus()
        self._title_edit.selectAll()

    def _commit_title(self) -> None:
        if not self._editing:
            return
        text = self._title_edit.text().strip()
        tab_id = self._tabs[self._current].tab_id
        if self._remote:
            self._editing = False
            self._render_header()
            if text:
                self.rename_tab_requested.emit(tab_id, text)
            return
        if text:
            self._tabs[self._current].title = text
        self._editing = False
        self._render_tabs()
        self._render_header()

    def _pick_model(self, index: int) -> None:
        model_id = str(self._model_combo.itemData(index) or "")
        if not model_id:
            return
        tab = self._tabs[self._current]
        if self._remote:
            self.model_requested.emit(tab.tab_id, model_id)
            return
        tab.model_id = model_id
        tab.model_label = self._model_combo.itemText(index)
        self._render_header()
        self._sync_composer()

    def use_service_tabs(self) -> None:
        self._remote = True

    def input_text(self) -> str:
        return self._draft.text()

    def clear_input(self) -> None:
        self._draft.clear()
        self._tabs[self._current].draft = ""

    def set_input_text(self, text: str) -> None:
        self._draft.setText(text)
        self._tabs[self._current].draft = text

    def set_busy(self, tab_id: str, busy: bool) -> None:
        if busy:
            self._busy.add(tab_id)
        else:
            self._busy.discard(tab_id)
        if not self._busy:
            self._thinking_timer.stop()
        self._sync_composer()
        if self.current_tab_id() == tab_id:
            self._render_messages()

    def set_problem(self, tab_id: str, text: str) -> None:
        tab = self._find_tab(tab_id)
        if tab is None:
            return
        tab.problem = text
        if self.current_tab_id() == tab_id:
            self._problem.setText(text)
            self._problem.setVisible(bool(text))

    def append_message(self, tab_id: str, role: str, text: str) -> None:
        tab = self._find_tab(tab_id)
        if tab is None:
            return
        tab.messages.append((role, text))
        if self.current_tab_id() == tab_id:
            self._render_messages()

    def set_messages(self, tab_id: str, messages: List[Tuple[str, str]]) -> None:
        tab = self._find_tab(tab_id)
        if tab is None:
            return
        tab.messages = list(messages)
        if self.current_tab_id() == tab_id:
            self._render_messages()

    def set_tabs(self, tabs: List[Tuple[str, str, str, str, List[Tuple[str, str]], str]]) -> None:
        """id, заголовок, id модели, подпись модели, сообщения, текст ошибки."""
        self._remember_draft()
        current = self.current_tab_id() if self._tabs else ""
        drafts = {tab.tab_id: tab.draft for tab in self._tabs}
        requests = {tab.tab_id: tab.request for tab in self._tabs}
        cards = {tab.tab_id: list(tab.cards) for tab in self._tabs}
        self._tabs = []
        for tab_id, title, model_id, model_label, messages, problem in tabs:
            self._tabs.append(
                _Tab(
                    title=title,
                    messages=list(messages),
                    model_id=model_id,
                    model_label=model_label,
                    tab_id=tab_id,
                    draft=drafts.get(tab_id, ""),
                    problem=problem,
                    request=requests.get(tab_id),
                    cards=cards.get(tab_id, []),
                )
            )
        if not self._tabs:
            self._tabs = [_Tab("чат")]
        self._current = 0
        for index, tab in enumerate(self._tabs):
            if tab.tab_id == current:
                self._current = index
                break
        self._editing = False
        self._show_draft()
        self._render_tabs()
        self._render_header()
        self._render_messages()
        self._sync_composer()

    def _remember_draft(self) -> None:
        if self._tabs:
            self._tabs[self._current].draft = self._draft.text()

    def _show_draft(self) -> None:
        self._draft.setText(self._tabs[self._current].draft)

    def _sync_composer(self) -> None:
        tab = self._tabs[self._current]
        busy = tab.tab_id in self._busy
        self._draft.setReadOnly(busy or not tab.model_id)
        self._draft.setPlaceholderText(
            "Сообщение модели. Enter — отправить, Ctrl+Enter или Shift+Enter — новая строка."
            if tab.model_id
            else "Сначала выберите модель"
        )
        self._send.setEnabled(busy or bool(tab.model_id))
        if busy:
            self._send.setText("Остановить")
            self._send.setObjectName("stopButton")
        else:
            self._send.setText("Отправить")
            self._send.setObjectName("sendButton")
        self._style(self._send)
        self._problem.setText(tab.problem)
        self._problem.setVisible(bool(tab.problem))

    def _on_primary(self) -> None:
        if self.current_tab_id() in self._busy:
            self.stop_requested.emit()
            return
        if not self._tabs[self._current].model_id:
            return
        if not self._draft.text().strip():
            return
        self.send_requested.emit()

    def _advance_thinking(self) -> None:
        self._thinking_phase = (self._thinking_phase + 1) % len(_THINKING)
        label = self._thinking_label
        if label is not None:
            label.setText(_THINKING[self._thinking_phase])

    def _style(self, widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _command_record(card: CommandCard) -> str:
    payload = {
        "mcp": card.target,
        "command": card.command,
        "output": card.output_text(),
    }
    if card.return_code is not None:
        payload["code"] = card.return_code
    return json.dumps(payload, ensure_ascii=False)
