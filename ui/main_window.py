# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Главное окно PySide6.

Работает только через ApplicationService и domain.
Не запускает процессы, не читает и не пишет файлы журналов,
не занимается шифрованием.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set
import os
import threading

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QDrag, QFontMetrics, QTextCharFormat
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import domain
from application import ApplicationService
from application.service import LlmUnavailable
from application.terminal_mcp import CommandAnswer, CommandAsk, lounge_tab_id
from shell_takeover import looks_like_secret_prompt
from ui.chat_pane import ChatPane
from ui.command_input import CommandInput, command_needs_more
from ui.command_card import CommandCard
from ui.dialogs import CreateSessionDialog, UnlockSessionDialog
from ui.general_chat import GENERAL_CHAT_ID, GENERAL_CHAT_TITLE, GeneralChatPage
from ui.edit_dialog import FileEditDialog
from ui.icons import gear_icon, paste_icon, paste_run_icon
from ui.session_settings import SessionSettingsDialog
from ui.models_dialog import ModelsDialog
from ui.styles import STYLESHEET, TextFormats, build_text_formats
from ui.hostkey import QtHostKeyVerifier


def _asks_for_secret(payload: str) -> bool:
    for line in payload.replace("\r", "").split("\n"):
        if line.strip() and looks_like_secret_prompt(line):
            return True
    return False


def _provider_label(provider: domain.ProviderType) -> str:
    if provider == domain.ProviderType.SSH_OVER_SSH:
        return "ssh→ssh"
    return provider.value


_NODE_MIME = "application/x-vibe-node"
_KIND_ROLE = Qt.ItemDataRole.UserRole + 1


class _SessionListDelegate(QStyledItemDelegate):
    """Текст пункта не рисуется: его показывает виджет строки.

    Иначе под прозрачной строкой просвечивает та же надпись
    и буквы накладываются друг на друга. Стрелка группы рисуется самим деревом.
    """

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        return


class _SessionTree(QTreeWidget):
    """Дерево подключений. Перенос считает место сам и отдаёт его окну."""

    moved = Signal(str, str, int)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sessionList")
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(0)
        self.setRootIsDecorated(False)
        self.setAnimated(False)
        self.setExpandsOnDoubleClick(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setAcceptDrops(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(_SessionListDelegate(self))

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(_NODE_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasFormat(_NODE_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # type: ignore[override]
        node_id = bytes(event.mimeData().data(_NODE_MIME)).decode("utf-8", errors="replace")
        place = self._drop_place(event.position().toPoint(), node_id)
        if place is None:
            event.ignore()
            return
        parent_id, index = place
        event.acceptProposedAction()
        self.moved.emit(node_id, parent_id, index)

    def _drop_place(self, pos, node_id: str):
        if not node_id or node_id == GENERAL_CHAT_ID:
            return None
        item = self.itemAt(pos)
        if item is None:
            return "", self._root_layout_index(self.topLevelItemCount())
        kind = item.data(0, _KIND_ROLE)
        if kind == "lounge":
            return None
        rect = self.visualItemRect(item)
        offset = pos.y() - rect.top()
        height = max(1, rect.height())
        if kind == "group" and height * 0.25 < offset < height * 0.75:
            if self._contains(item, node_id):
                return None
            return str(item.data(0, Qt.ItemDataRole.UserRole) or ""), item.childCount()
        parent = item.parent()
        if parent is None:
            row = self.indexOfTopLevelItem(item)
            if offset >= height / 2:
                row += 1
            return "", self._root_layout_index(row)
        if self._contains(parent, node_id):
            return None
        row = parent.indexOfChild(item)
        if offset >= height / 2:
            row += 1
        return str(parent.data(0, Qt.ItemDataRole.UserRole) or ""), row

    def _root_layout_index(self, row: int) -> int:
        count = 0
        last = min(row, self.topLevelItemCount())
        for index in range(last):
            item = self.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) != GENERAL_CHAT_ID:
                count += 1
        return count

    def _contains(self, item: QTreeWidgetItem, node_id: str) -> bool:
        current: Optional[QTreeWidgetItem] = item
        while current is not None:
            if current.data(0, Qt.ItemDataRole.UserRole) == node_id:
                return True
            current = current.parent()
        return False


class _SessionRow(QWidget):
    """Строка дерева. Удаление сессии видно только при наведении."""

    delete_clicked = Signal(str)
    open_requested = Signal(str)
    toggle_requested = Signal(str)
    group_action = Signal(str, str)

    def __init__(
        self,
        session_id: str,
        text: str,
        active: bool,
        muted: bool,
        deletable: bool = True,
        kind: str = "session",
        depth: int = 0,
    ) -> None:
        super().__init__()
        self._session_id = session_id
        self._kind = kind
        self._press = None
        self._full = text
        self.setObjectName("sessionRow")
        self.setProperty("active", "true" if active else "false")
        self.setProperty("hover", "false")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10 + depth * 16, 8, 8, 8)
        layout.setSpacing(8)
        self._label = QLabel(text)
        self._label.setObjectName("sessionMuted" if muted else "sessionName")
        if active:
            font = self._label.font()
            font.setBold(True)
            self._label.setFont(font)
        self._label.setMinimumWidth(0)
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._delete = QPushButton("Удалить")
        self._delete.setObjectName("sessionDelete")
        self._delete.setCursor(Qt.CursorShape.PointingHandCursor)
        self._delete.clicked.connect(lambda: self.delete_clicked.emit(session_id))
        self._delete.setVisible(False)
        self._delete.setMaximumWidth(0)
        self._deletable = deletable
        self._fold = None
        if kind == "group":
            self._fold = QPushButton("▸")
            self._fold.setObjectName("sessionFold")
            self._fold.setFixedSize(22, 22)
            self._fold.setCursor(Qt.CursorShape.PointingHandCursor)
            self._fold.setToolTip("Свернуть или развернуть группу")
            self._fold.clicked.connect(lambda: self.toggle_requested.emit(session_id))
            layout.addWidget(self._fold)
        layout.addWidget(self._label, 1)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._set_delete_visible(True)
        self._set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        # Переход на кнопку-ребёнка тоже шлёт leave. Прячем кнопку,
        # только когда курсор действительно вышел из строки.
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._set_delete_visible(False)
            self._set_hover(False)
        super().leaveEvent(event)

    def _set_delete_visible(self, visible: bool) -> None:
        layout = self.layout()
        if layout is None or not self._deletable:
            return
        if visible:
            self._delete.setMaximumWidth(16777215)
            if layout.indexOf(self._delete) < 0:
                layout.addWidget(self._delete)
            self._delete.show()
        else:
            layout.removeWidget(self._delete)
            self._delete.hide()
            self._delete.setMaximumWidth(0)
        layout.invalidate()
        layout.activate()
        self._label.setText(self._full)
        self._elide()
        QTimer.singleShot(0, self._elide)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if (
            self._kind != "lounge"
            and self._press is not None
            and not self._over_button(event.position().toPoint())
            and (event.position().toPoint() - self._press).manhattanLength()
            >= QApplication.startDragDistance()
        ):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData(_NODE_MIME, self._session_id.encode("utf-8"))
            drag.setMimeData(mime)
            self._press = None
            drag.exec(Qt.DropAction.MoveAction)
            return
        super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        super().mouseDoubleClickEvent(event)
        kind = self._kind
        sid = self._session_id
        signal = self.toggle_requested if kind == "group" else self.open_requested
        # Список пересобирается в ответ на сигнал и удаляет эту строку.
        # Откладываем, чтобы не звать метод уже уничтоженного виджета.
        QTimer.singleShot(0, lambda: signal.emit(sid))

    def contextMenuEvent(self, event) -> None:  # type: ignore[override]
        if self._kind == "lounge":
            super().contextMenuEvent(event)
            return
        sid = self._session_id
        menu = QMenu()
        if self._kind == "group":
            menu.addAction("Переименовать", lambda: self.group_action.emit("rename", sid))
            menu.addAction("Создать подгруппу", lambda: self.group_action.emit("subgroup", sid))
            menu.addAction("Удалить группу", lambda: self.group_action.emit("delete", sid))
        else:
            menu.addAction(
                "Переименовать",
                lambda: self.group_action.emit("rename-session", sid),
            )
        menu.exec(event.globalPos())

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._elide()

    def _set_hover(self, hover: bool) -> None:
        self.setProperty("hover", "true" if hover else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def _elide(self) -> None:
        try:
            width = self._label.width()
        except RuntimeError:
            return
        if width < 24:
            self._label.setText(self._full)
            return
        self._label.setText(
            self._label.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, width)
        )

    def set_expanded(self, expanded: bool) -> None:
        if self._fold is None:
            return
        self._fold.setText("▾" if expanded else "▸")

    def _over_button(self, pos) -> bool:
        if self._delete.geometry().contains(pos):
            return True
        return self._fold is not None and self._fold.geometry().contains(pos)


@dataclass
class _PromptState:
    """Текст поля, несохранённый черновик и позиция в истории команд сессии."""

    text: str = ""
    draft: str = ""
    index: Optional[int] = None


def _variants_phrase(count: int) -> str:
    n100 = count % 100
    n10 = count % 10
    if 11 <= n100 <= 14:
        word = "вариантов"
    elif n10 == 1:
        word = "вариант"
    elif 2 <= n10 <= 4:
        word = "варианта"
    else:
        word = "вариантов"
    return f"{count} {word} — Tab ещё раз"


class MainWindow(QMainWindow):
    """Список сессий слева, чат и поле ввода справа.

    Поддерживает несколько открытых сессий. Активная сессия отображается
    в правой области; события остальных сессий сохраняются в их журналах
    и становятся видны при переключении.
    """

    _event_received = Signal(object)
    _completion_ready = Signal(int, str, object)
    _chat_done = Signal(object, object)
    _lounge_done = Signal(str, object)
    _mcp_ask = Signal(object)

    def __init__(self, service: ApplicationService) -> None:
        super().__init__()
        self._service = service
        self._active_session_id: Optional[domain.SessionId] = None
        self._prompts: Dict[domain.SessionId, _PromptState] = {}
        self._chat_drafts: Dict[domain.SessionId, str] = {}
        self._chat_wait: Set[domain.SessionId] = set()
        self._lounge_wait: Set[str] = set()
        self._lounge_runs: Dict[domain.SessionId, str] = {}
        self._mcp_pending: Dict[domain.SessionId, tuple] = {}
        self._panels: Dict[domain.SessionId, QScrollArea] = {}
        self._layouts: Dict[domain.SessionId, QVBoxLayout] = {}
        self._panel_ready: Set[domain.SessionId] = set()
        self._live_cards: Dict[domain.SessionId, CommandCard] = {}
        self._dismissed_commands: Set[domain.SessionId] = set()
        self._tab_text: Optional[str] = None
        self._tab_candidates: List[str] = []
        self._tab_list_armed = False
        self._completion_generation = 0
        self._completion_pending = False

        self._formats: TextFormats = build_text_formats()
        self._setup_ui()

        self._event_received.connect(self._on_event_ui)
        self._completion_ready.connect(self._on_completion_ready)
        self._chat_done.connect(self._on_chat_done)
        self._mcp_ask.connect(self._present_mcp)
        self.chat_pane.command_decided.connect(self._on_command_decided)
        self._lounge.command_decided.connect(self._on_command_decided)
        self._lounge_done.connect(self._on_lounge_done)
        self._bind_lounge()
        hook = getattr(self._service, "set_command_approver", None)
        if hook is not None:
            hook(self._approve_mcp)
        self._service.subscribe(self._on_event_from_driver)

        self._reload_sessions()
        self._update_header()
        self._update_input_state()

    # --- UI ---------------------------------------------------------------

    def _setup_ui(self) -> None:
        self.setWindowTitle("vterminal")
        self.resize(1180, 760)
        self.setMinimumSize(860, 520)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_pages())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 900])
        self.setCentralWidget(splitter)

        self.setStyleSheet(STYLESHEET)

    def _build_sidebar(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sideBar")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 18, 16, 16)
        layout.setSpacing(10)

        title = QLabel("Сессии")
        title.setObjectName("sidebarTitle")
        layout.addWidget(title)

        self.new_button = QPushButton("＋  Новая сессия")
        self.new_button.setObjectName("primaryButton")
        self.new_button.clicked.connect(self._on_create_session)
        layout.addWidget(self.new_button)

        self.models_button = QPushButton("Модели")
        self.models_button.setObjectName("secondaryButton")
        self.models_button.setToolTip(
            "Сохранённые подключения к моделям.\n"
            "Перед записью программа проверяет, что сервер знает эту модель."
        )
        self.models_button.clicked.connect(self._on_models)
        layout.addWidget(self.models_button)

        self.group_button = QPushButton("Создать группу")
        self.group_button.setObjectName("secondaryButton")
        self.group_button.setToolTip("Новая папка в корне списка подключений")
        self.group_button.clicked.connect(self._on_create_group)
        layout.addWidget(self.group_button)

        self.session_list = _SessionTree()
        self.session_list.moved.connect(self._on_node_moved)
        self.session_list.itemExpanded.connect(self._on_group_expanded)
        self.session_list.itemCollapsed.connect(self._on_group_collapsed)
        layout.addWidget(self.session_list, 1)

        self.close_button = QPushButton("Закрыть сессию")
        self.close_button.setObjectName("secondaryButton")
        self.close_button.setEnabled(False)
        self.close_button.clicked.connect(self._on_close_session)
        layout.addWidget(self.close_button)

        return panel

    def _build_pages(self) -> QWidget:
        self._pages = QStackedWidget()
        self._terminal_page = self._build_main_area()
        self._lounge = GeneralChatPage(self._formats.mono)
        self._pages.addWidget(self._terminal_page)
        self._pages.addWidget(self._lounge)
        return self._pages

    def _build_main_area(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("workArea")
        root = QVBoxLayout(panel)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        header = QHBoxLayout()
        header.setSpacing(10)

        title = QLabel("vterminal")
        title.setObjectName("appTitle")
        header.addWidget(title)

        self._header_label = QLabel("Сессия не выбрана")
        self._header_label.setObjectName("sessionLabel")
        header.addWidget(self._header_label)

        self._settings_button = QPushButton()
        self._settings_button.setObjectName("iconButton")
        self._settings_button.setIcon(gear_icon())
        self._settings_button.setIconSize(QSize(16, 16))
        self._settings_button.setFixedSize(28, 28)
        self._settings_button.setToolTip("Настройки сессии")
        self._settings_button.setVisible(False)
        self._settings_button.clicked.connect(self._on_session_settings)
        header.addWidget(self._settings_button)

        header.addStretch(1)

        self._status_label = QLabel("—")
        self._status_label.setObjectName("statusBadge")
        self._status_label.setProperty("running", "false")
        self._status_label.setProperty("closed", "false")
        header.addWidget(self._status_label)
        root.addLayout(header)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(14)

        self._offline_banner = QFrame()
        self._offline_banner.setObjectName("offlineBanner")
        banner_layout = QVBoxLayout(self._offline_banner)
        banner_layout.setContentsMargins(14, 12, 14, 12)
        banner_layout.setSpacing(8)
        self._offline_title = QLabel("Сессия недоступна")
        self._offline_title.setObjectName("offlineTitle")
        banner_layout.addWidget(self._offline_title)
        self._offline_detail = QLabel(
            "Журнал открыт. Команды сейчас отправить нельзя."
        )
        self._offline_detail.setObjectName("offlineDetail")
        self._offline_detail.setWordWrap(True)
        banner_layout.addWidget(self._offline_detail)
        self._restore_button = QPushButton("Восстановить соединение")
        self._restore_button.setObjectName("auxButton")
        self._restore_button.setToolTip(
            "Попробовать снова подключиться к сессии.\n"
            "Журнал при этом остаётся открытым."
        )
        self._restore_button.clicked.connect(self._on_restore_connection)
        banner_layout.addWidget(self._restore_button, 0, Qt.AlignmentFlag.AlignLeft)
        self._offline_banner.setVisible(False)
        left_layout.addWidget(self._offline_banner)

        self.history_stack = QStackedWidget()
        self._idle_scroll, self._idle_layout = self._make_history_scroll()
        self.history_scroll = self._idle_scroll
        self.history_layout = self._idle_layout
        self.history_stack.addWidget(self._idle_scroll)
        left_layout.addWidget(self.history_stack, 1)

        self._completion_hint = QLabel()
        self._completion_hint.setObjectName("completionHint")
        self._completion_hint.setVisible(False)
        left_layout.addWidget(self._completion_hint)

        self._completion_list = QListWidget()
        self._completion_list.setObjectName("completionList")
        self._completion_list.setFont(self._formats.mono)
        self._completion_list.setVisible(False)
        self._completion_list.setMaximumHeight(240)
        self._completion_list.itemClicked.connect(self._accept_completion_item)
        left_layout.addWidget(self._completion_list)

        input_panel = QFrame()
        input_panel.setObjectName("inputPanel")
        input_layout = QHBoxLayout(input_panel)
        input_layout.setContentsMargins(12, 8, 10, 8)
        input_layout.setSpacing(10)
        input_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.paste_button = QPushButton()
        self.paste_button.setObjectName("iconButton")
        self.paste_button.setIcon(paste_icon())
        self.paste_button.setIconSize(QSize(16, 16))
        self.paste_button.setFixedSize(32, 32)
        self.paste_button.setToolTip(
            "Взять текст из буфера обмена\nи вставить его в поле команды."
        )
        self.paste_button.setEnabled(False)
        self.paste_button.clicked.connect(self._on_paste)
        input_layout.addWidget(self.paste_button)

        self.paste_run_button = QPushButton()
        self.paste_run_button.setObjectName("iconButton")
        self.paste_run_button.setIcon(paste_run_icon())
        self.paste_run_button.setIconSize(QSize(16, 16))
        self.paste_run_button.setFixedSize(32, 32)
        self.paste_run_button.setToolTip(
            "Взять команду из буфера обмена\n"
            "и сразу выполнить её в текущей сессии.\n"
            "Несколько строк уходят как одна команда."
        )
        self.paste_run_button.setEnabled(False)
        self.paste_run_button.clicked.connect(self._on_paste_run)
        input_layout.addWidget(self.paste_run_button)

        self._prompt_label = QLabel("❯")
        self._prompt_label.setObjectName("inputPrompt")
        self._prompt_label.setFont(self._formats.mono)
        input_layout.addWidget(self._prompt_label)

        self.input = CommandInput(self._formats.mono)
        self.input.setPlaceholderText("Выберите сессию слева")
        self.input.textChanged.connect(lambda: self._on_input_changed(self.input.text()))
        self.input.installEventFilter(self)
        self.input.setEnabled(False)
        input_layout.addWidget(self.input, 1)

        self.send_button = QPushButton("Отправить  ↵")
        self.send_button.setObjectName("sendButton")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self._on_submit)
        input_layout.addWidget(self.send_button)

        left_layout.addWidget(input_panel)

        self.chat_pane = ChatPane(self._formats.mono)
        self.chat_pane.setVisible(False)
        self.chat_pane.send_requested.connect(self._on_chat_send)
        self.chat_pane.stop_requested.connect(self._on_chat_stop)
        self.chat_pane.retry_requested.connect(self._on_chat_retry)
        self.chat_pane.run_command.connect(self._on_chat_run_command)
        self.chat_pane.insert_command.connect(self._on_chat_insert_command)
        self.chat_pane.apply_edit.connect(self._on_chat_apply_edit)
        self.chat_pane.reset_requested.connect(self._on_chat_reset)
        self.chat_pane.limit_changed.connect(self._on_chat_limit)
        self._work_split = QSplitter(Qt.Orientation.Horizontal)
        self._work_split.setChildrenCollapsible(False)
        self._work_split.addWidget(left)
        self._work_split.addWidget(self.chat_pane)
        self._work_split.setStretchFactor(0, 1)
        self._work_split.setStretchFactor(1, 1)
        root.addWidget(self._work_split, 1)
        return panel

    def _make_history_scroll(self) -> tuple:
        scroll = QScrollArea()
        scroll.setObjectName("historyScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        container.setObjectName("historyHost")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 14)
        layout.setSpacing(12)
        layout.addStretch(1)
        scroll.setWidget(container)
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setProperty("vibeHistory", True)
        scroll.viewport().installEventFilter(self)
        return scroll, layout

    # --- список сессий ----------------------------------------------------

    def _reload_sessions(self) -> None:
        tree = self.session_list
        tree.blockSignals(True)
        tree.clear()
        self._add_general_chat_row()
        sessions = {info.session_id: info for info in self._service.list_sessions()}
        placed: set = set()
        self._fill_branch(None, "", self._read_layout(), sessions, placed, 0)
        for info in self._service.list_sessions():
            if info.session_id not in placed:
                self._add_session_item(None, info)
        tree.blockSignals(False)
        self._update_session_buttons()
        if self._active_session_id == GENERAL_CHAT_ID:
            self._refresh_lounge()

    def _read_layout(self) -> domain.ConnectionLayout:
        getter = getattr(self._service, "connection_layout", None)
        if not callable(getter):
            return domain.ConnectionLayout()
        try:
            layout = getter()
        except Exception:
            return domain.ConnectionLayout()
        return layout if isinstance(layout, domain.ConnectionLayout) else domain.ConnectionLayout()

    def _fill_branch(
        self,
        parent: Optional[QTreeWidgetItem],
        parent_id: str,
        layout: domain.ConnectionLayout,
        sessions: dict,
        placed: set,
        depth: int,
    ) -> None:
        for child_id in layout.children_of(parent_id):
            group = layout.group(child_id)
            if group is not None:
                item = self._add_group_item(parent, group, depth)
                self._fill_branch(item, group.group_id, layout, sessions, placed, depth + 1)
                item.setExpanded(group.expanded)
                self._sync_fold_icon(item, group.expanded)
                continue
            info = sessions.get(child_id)
            if info is None:
                continue
            placed.add(info.session_id)
            self._add_session_item(parent, info, depth)

    def _add_general_chat_row(self) -> None:
        active = self._active_session_id == GENERAL_CHAT_ID
        self._add_tree_row(
            None,
            GENERAL_CHAT_ID,
            "lounge",
            GENERAL_CHAT_TITLE,
            active=active,
            muted=False,
            deletable=False,
            depth=0,
        )

    def _add_group_item(
        self,
        parent: Optional[QTreeWidgetItem],
        group: domain.ConnectionGroup,
        depth: int,
    ) -> QTreeWidgetItem:
        return self._add_tree_row(
            parent,
            group.group_id,
            "group",
            group.name,
            active=False,
            muted=False,
            deletable=False,
            depth=depth,
        )

    def _add_session_item(
        self,
        parent: Optional[QTreeWidgetItem],
        info: domain.SessionInfo,
        depth: int = 0,
    ) -> QTreeWidgetItem:
        is_current = info.session_id == self._active_session_id
        state = self._service.session_state(info.session_id)
        prefix = "○ " if state == "closed" else "● "
        if state == "closed":
            availability = "не активна"
        elif state in ("error", "offline"):
            availability = "активна · нет соединения"
        elif state == "running":
            availability = "активна · выполняется"
        else:
            availability = "активна"
        text = (
            prefix
            + info.name
            + self._model_mark(info)
            + f"  ·  {_provider_label(info.provider)}  ·  {availability}"
        )
        return self._add_tree_row(
            parent,
            info.session_id,
            "session",
            text,
            active=is_current,
            muted=state == "closed",
            deletable=True,
            depth=depth,
        )

    def _add_tree_row(
        self,
        parent: Optional[QTreeWidgetItem],
        node_id: str,
        kind: str,
        text: str,
        active: bool,
        muted: bool,
        deletable: bool,
        depth: int = 0,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem()
        item.setData(0, Qt.ItemDataRole.UserRole, node_id)
        item.setData(0, _KIND_ROLE, kind)
        item.setText(0, text)
        if muted:
            item.setForeground(0, QColor("#8b949e"))
        if parent is None:
            self.session_list.addTopLevelItem(item)
        else:
            parent.addChild(item)
        row = _SessionRow(
            node_id,
            text,
            active=active,
            muted=muted,
            deletable=deletable,
            kind=kind,
            depth=depth,
        )
        row.delete_clicked.connect(self._on_delete_session)
        row.open_requested.connect(self._open_session_from_row)
        row.toggle_requested.connect(self._on_toggle_group)
        row.group_action.connect(self._on_group_action)
        self.session_list.setItemWidget(item, 0, row)
        item.setSizeHint(0, QSize(0, max(40, row.sizeHint().height())))
        return item

    def _on_create_group(self) -> None:
        self._ask_new_group("", "Новая группа")

    def _ask_new_group(self, parent_id: str, title: str) -> None:
        name, ok = QInputDialog.getText(self, title, "Имя группы")
        if not ok:
            return
        creator = getattr(self._service, "create_connection_group", None)
        if not callable(creator):
            return
        try:
            creator(name, parent_id)
        except Exception as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        QTimer.singleShot(0, self._reload_sessions)

    def _on_group_action(self, action: str, group_id: str) -> None:
        if action == "subgroup":
            self._ask_new_group(group_id, "Подгруппа")
            return
        if action == "rename-session":
            self._rename_session_from_list(group_id)
            return
        if action == "rename":
            name, ok = QInputDialog.getText(self, "Переименовать группу", "Имя группы")
            if not ok:
                return
            rename = getattr(self._service, "rename_connection_group", None)
            if not callable(rename):
                return
            try:
                rename(group_id, name)
            except Exception as exc:
                QMessageBox.warning(self, "Переименовать группу", str(exc))
                return
            QTimer.singleShot(0, self._reload_sessions)
            return
        if action != "delete":
            return
        deleter = getattr(self._service, "delete_connection_group", None)
        if not callable(deleter):
            return
        try:
            deleter(group_id)
        except Exception as exc:
            QMessageBox.warning(self, "Удалить группу", str(exc))
            return
        QTimer.singleShot(0, self._reload_sessions)

    def _rename_session_from_list(self, session_id: str) -> None:
        info = self._service.get_session(session_id)
        current = info.name if info is not None else ""
        name, ok = QInputDialog.getText(
            self, "Переименовать сессию", "Имя сессии", text=current
        )
        if not ok:
            return
        try:
            self._service.rename_session(session_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "Переименовать сессию", str(exc))
            return
        QTimer.singleShot(0, self._reload_sessions)
        if self._active_session_id == session_id:
            self._update_header()

    def _on_toggle_group(self, group_id: str) -> None:
        item = self._find_tree_item(group_id)
        if item is None:
            return
        item.setExpanded(not item.isExpanded())

    def _on_group_expanded(self, item: QTreeWidgetItem) -> None:
        self._remember_group_expanded(item, True)
        self._sync_fold_icon(item, True)

    def _on_group_collapsed(self, item: QTreeWidgetItem) -> None:
        self._remember_group_expanded(item, False)
        self._sync_fold_icon(item, False)

    def _sync_fold_icon(self, item: QTreeWidgetItem, expanded: bool) -> None:
        row = self.session_list.itemWidget(item, 0)
        if isinstance(row, _SessionRow):
            row.set_expanded(expanded)

    def _remember_group_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if item.data(0, _KIND_ROLE) != "group":
            return
        setter = getattr(self._service, "set_connection_group_expanded", None)
        if not callable(setter):
            return
        setter(str(item.data(0, Qt.ItemDataRole.UserRole) or ""), expanded)

    def _on_node_moved(self, node_id: str, parent_id: str, index: int) -> None:
        mover = getattr(self._service, "move_connection_node", None)
        if not callable(mover):
            return
        try:
            mover(node_id, parent_id, index)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось перенести", str(exc))
        self._reload_sessions()

    def _find_tree_item(self, node_id: str) -> Optional[QTreeWidgetItem]:
        def walk(item: QTreeWidgetItem) -> Optional[QTreeWidgetItem]:
            if item.data(0, Qt.ItemDataRole.UserRole) == node_id:
                return item
            for index in range(item.childCount()):
                found = walk(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.session_list.topLevelItemCount()):
            found = walk(self.session_list.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _update_session_buttons(self) -> None:
        sid = self._active_session_id
        if sid == GENERAL_CHAT_ID:
            self.close_button.setEnabled(False)
            return
        state = self._service.session_state(sid) if sid is not None else "closed"
        self.close_button.setEnabled(sid is not None and state != "closed")

    def _open_session_from_row(self, session_id: str) -> None:
        item = self._find_tree_item(session_id)
        if item is not None:
            self._on_open_session_item(item)

    def _model_mark(self, info: domain.SessionInfo) -> str:
        if not info.llm_profile_id:
            return ""
        getter = getattr(self._service, "get_llm_profile", None)
        profile = getter(info.llm_profile_id) if getter is not None else None
        if profile is None:
            return "  ·  модель"
        return f"  ·  {profile.name}"

    def _on_models(self) -> None:
        dialog = ModelsDialog(self._service, self)
        dialog.exec()
        self._reload_sessions()
        self._update_header()
        self._sync_chat(self._active_session_id)

    # --- действия с сессиями ---------------------------------------------
    def _on_create_session(self) -> None:
        dialog = CreateSessionDialog(self._service, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        info = dialog.created_session()
        if info is not None:
            self._activate_session(info.session_id)

    def _on_open_session_item(self, item: QTreeWidgetItem) -> None:
        session_id = item.data(0, Qt.ItemDataRole.UserRole)
        if session_id == GENERAL_CHAT_ID:
            self._activate_session(GENERAL_CHAT_ID)
            return
        if session_id is None:
            return

        state = self._service.session_state(session_id)
        if session_id == self._active_session_id and state != "closed":
            return
        if state != "closed":
            self._activate_session(session_id)
            return

        stored = self._service.get_session(session_id)
        if stored is None:
            return

        dialog = UnlockSessionDialog(self._service, stored, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        info = dialog.opened_session()
        if info is not None:
            self._activate_session(session_id)

    def _on_close_session(self) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        state = self._service.session_state(sid)
        if state == "closed":
            return
        if state == "running":
            QMessageBox.information(
                self,
                "Команда выполняется",
                "Дождитесь завершения команды перед закрытием сессии.",
            )
            return

        self._service.close_session(sid)
        self._drop_panel(sid)

        next_sid: Optional[domain.SessionId] = None
        for info in self._service.list_sessions():
            if info.session_id != sid and self._service.session_state(info.session_id) != "closed":
                next_sid = info.session_id
                break

        self._activate_session(next_sid if next_sid is not None else sid)

    def _on_delete_session(self, session_id: Optional[str] = None) -> None:
        sid = session_id or self._active_session_id
        if sid is None:
            return
        info = self._service.get_session(sid)
        name = info.name if info is not None else sid
        if self._service.session_state(sid) == "running":
            QMessageBox.information(
                self,
                "Команда выполняется",
                "Дождитесь завершения команды перед удалением сессии.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Удалить сессию",
            f"Удалить сессию «{name}»?\n"
            "Журнал команд будет удалён без возможности восстановления.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.delete_session(sid)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось удалить сессию", str(exc))
            return

        self._prompts.pop(sid, None)
        self._chat_drafts.pop(sid, None)
        self._drop_panel(sid)
        if self._active_session_id != sid:
            self._reload_sessions()
            return

        next_sid: Optional[domain.SessionId] = None
        for item_info in self._service.list_sessions():
            if self._service.session_state(item_info.session_id) != "closed":
                next_sid = item_info.session_id
                break
        if next_sid is None:
            remaining = self._service.list_sessions()
            next_sid = remaining[0].session_id if remaining else None
        self._active_session_id = None
        self.input.clear()
        self._activate_session(next_sid)

    # --- активация сессии -------------------------------------------------

    def _activate_session(self, session_id: Optional[domain.SessionId]) -> None:
        previous = self._active_session_id
        if session_id == GENERAL_CHAT_ID:
            self._show_general_chat(previous)
            return
        if previous == GENERAL_CHAT_ID:
            self._pages.setCurrentWidget(self._terminal_page)
        if previous != session_id:
            self._save_prompt(previous)
            self._save_chat_draft(previous)
        self._active_session_id = session_id
        mark = getattr(self._service, "set_foreground_session", None)
        if mark is not None:
            mark(session_id)
        self._disarm_tab()

        state = (
            self._service.session_state(session_id) if session_id is not None else "closed"
        )
        if session_id is None or state == "closed":
            self.history_scroll = self._idle_scroll
            self.history_layout = self._idle_layout
            self._clear_layout(self._idle_layout)
            if session_id is None:
                self._append_system_message("Сессия не выбрана.")
            else:
                self._append_system_message(
                    "Сессия закрыта. Дважды кликните по ней в списке слева, "
                    "чтобы открыть журнал и продолжить работу."
                )
            self.history_stack.setCurrentWidget(self._idle_scroll)
        else:
            scroll = self._ensure_panel(session_id)
            self.history_stack.setCurrentWidget(scroll)

        self._update_header()
        self._update_input_state()
        self._restore_prompt(session_id)
        self._sync_chat(session_id)
        self._reload_sessions()
        card = self._live_cards.get(session_id) if session_id is not None else None
        if card is not None and card.is_live():
            card.focus_terminal()

    def _ensure_panel(self, session_id: domain.SessionId) -> QScrollArea:
        if session_id not in self._panels:
            scroll, layout = self._make_history_scroll()
            self._panels[session_id] = scroll
            self._layouts[session_id] = layout
            self.history_stack.addWidget(scroll)
        if session_id not in self._panel_ready:
            self.history_layout = self._layouts[session_id]
            self.history_scroll = self._panels[session_id]
            self._render_session(session_id)
            self._panel_ready.add(session_id)
        self.history_layout = self._layouts[session_id]
        self.history_scroll = self._panels[session_id]
        return self._panels[session_id]

    def _drop_panel(self, session_id: domain.SessionId) -> None:
        scroll = self._panels.pop(session_id, None)
        self._layouts.pop(session_id, None)
        self._panel_ready.discard(session_id)
        self._live_cards.pop(session_id, None)
        if scroll is not None:
            self.history_stack.removeWidget(scroll)
            scroll.deleteLater()

    def _render_session(self, session_id: domain.SessionId) -> None:
        if self._service.session_state(session_id) == "closed":
            self._append_system_message(
                "Сессия закрыта. Дважды кликните по ней в списке слева, "
                "чтобы открыть журнал и продолжить работу."
            )
            return
        try:
            history = self._service.get_history(session_id)
        except Exception as exc:
            self._append_system_message(
                f"[ошибка] Не удалось прочитать журнал: {exc}"
            )
            return
        if history:
            self._render_history(history)
            return
        if self._service.session_state(session_id) in ("offline", "error"):
            return
        self._append_system_message(
            "Новая сессия. История будет записана в зашифрованный журнал."
        )

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        while layout.count() > 1:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _render_history(self, events: List[domain.TranscriptEvent]) -> None:
        current_card: Optional[CommandCard] = None
        for ev in events:
            if ev.event_type == domain.EventType.COMMAND_SENT:
                current_card = CommandCard(
                    ev.payload.rstrip("\n"), self._formats.mono
                )
                self._add_history_widget(current_card)
            elif ev.event_type == domain.EventType.OUTPUT_CHUNK:
                if current_card is None:
                    current_card = CommandCard("Команда", self._formats.mono)
                    self._add_history_widget(current_card)
                fmt = (
                    self._formats.stderr
                    if ev.stream_type == domain.StreamType.STDERR
                    else self._formats.stdout
                )
                current_card.append_output(ev.payload, fmt)
            elif ev.event_type == domain.EventType.ERROR:
                if current_card is None:
                    current_card = CommandCard("Команда", self._formats.mono)
                    self._add_history_widget(current_card)
                current_card.append_output(
                    f"[ошибка] {ev.payload}\n", self._formats.error
                )
            elif ev.event_type == domain.EventType.COMMAND_FINISHED:
                if current_card is not None:
                    current_card.finalize(ev.return_code, self._formats.exit)
                current_card = None
            elif ev.event_type == domain.EventType.SESSION_STATUS:
                if ev.status == domain.SessionStatus.CLOSED:
                    current_card = None
        QTimer.singleShot(0, self._scroll_to_bottom)

    # --- шапка и статус ---------------------------------------------------

    def _show_general_chat(self, previous: Optional[domain.SessionId]) -> None:
        if previous != GENERAL_CHAT_ID:
            self._save_prompt(previous)
            self._save_chat_draft(previous)
        self._active_session_id = GENERAL_CHAT_ID
        mark = getattr(self._service, "set_foreground_session", None)
        if mark is not None:
            mark(None)
        self._disarm_tab()
        self._pages.setCurrentWidget(self._lounge)
        self._refresh_lounge()
        self._update_session_buttons()
        self._reload_sessions()

    def _refresh_lounge(self) -> None:
        options = []
        listing = getattr(self._service, "list_llm_profiles", None)
        if listing is not None:
            for profile in listing():
                options.append((profile.profile_id, profile.name))
        self._lounge.set_models(options)
        listed = getattr(self._service, "lounge_mcp_entries", None)
        if listed is not None:
            self._lounge.set_mcp_entries(listed())
            return
        entries = []
        endpoint = getattr(self._service, "mcp_endpoint", None)
        for info in self._service.list_sessions():
            if self._service.session_state(info.session_id) not in ("ready", "running"):
                continue
            if endpoint is None or not endpoint(info.session_id):
                continue
            entries.append((info.mcp_alias.strip() or info.name, info.mcp_description))
        self._lounge.set_mcp_entries(entries)

    def _update_header(self) -> None:
        sid = self._active_session_id
        if sid is None:
            self._header_label.setText("Сессия не выбрана")
            self._settings_button.setVisible(False)
            return
        info = self._service.get_session(sid)
        if info is None:
            self._header_label.setText("Сессия не выбрана")
            self._settings_button.setVisible(False)
            return
        self._settings_button.setVisible(True)
        model = self._model_mark(info)
        if info.ssh is not None and info.provider in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            location = domain.ssh_location(info.ssh)
        else:
            location = info.identity.value
        self._header_label.setText(
            f"{info.name}{model}  ·  {_provider_label(info.provider)}  ·  {location}"
        )

    def _on_session_settings(self) -> None:
        sid = self._active_session_id
        if sid is None or sid == GENERAL_CHAT_ID:
            return
        info = self._service.get_session(sid)
        if info is None:
            return
        endpoint = ""
        getter = getattr(self._service, "mcp_endpoint", None)
        if getter is not None:
            endpoint = getter(sid) or ""
        dialog = SessionSettingsDialog(info, endpoint, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self._service.update_session_settings(
                sid,
                dialog.session_name(),
                dialog.mcp_alias(),
                dialog.mcp_description(),
                dialog.os_name(),
                dialog.username(),
                dialog.ip_addresses(),
            )
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось сохранить настройки", str(exc))
            return
        self._update_header()
        self._reload_sessions()

    def _update_input_state(self) -> None:
        sid = self._active_session_id
        state = self._service.session_state(sid) if sid is not None else "closed"

        self._update_session_buttons()
        self._show_shell_prompt(sid, state)

        if sid is None:
            self._set_prompt_enabled(False)
            self.input.setPlaceholderText("Выберите сессию слева")
            self._status_label.setText("—")
            self._status_label.setProperty("running", "false")
            self._status_label.setProperty("closed", "false")
            self._status_label.setProperty("error", "false")
        elif state == "closed":
            self._set_prompt_enabled(False)
            self.input.setPlaceholderText(
                "Сессия закрыта — откройте её двойным кликом"
            )
            self._status_label.setText("CLOSED")
            self._status_label.setProperty("running", "false")
            self._status_label.setProperty("closed", "true")
            self._status_label.setProperty("error", "false")
        elif state in ("error", "offline"):
            self._set_prompt_enabled(False)
            self.input.setPlaceholderText(
                "Журнал открыт — соединение недоступно"
            )
            self._status_label.setText("OFFLINE")
            self._status_label.setProperty("running", "false")
            self._status_label.setProperty("closed", "false")
            self._status_label.setProperty("error", "true")
        elif state == "running":
            self._set_prompt_enabled(False)
            self.input.setPlaceholderText("Ввод в блоке команды")
            self._status_label.setText("RUNNING")
            self._status_label.setProperty("running", "true")
            self._status_label.setProperty("closed", "false")
            self._status_label.setProperty("error", "false")
        else:
            self._set_prompt_enabled(True)
            if sid is not None and self._shell_label(sid):
                self.input.setPlaceholderText(
                    "Команда в этой оболочке…  Enter отправит, exit — назад"
                )
            else:
                self.input.setPlaceholderText(
                    "Команда…  Enter отправит, Ctrl+Enter или Shift+Enter — новая строка"
                )
            self._status_label.setText("READY")
            self._status_label.setProperty("running", "false")
            self._status_label.setProperty("closed", "false")
            self._status_label.setProperty("error", "false")
            self.input.setFocus()

        self._update_offline_banner(state if sid is not None else "closed")
        self._refresh_status_style()

    def _show_shell_prompt(self, sid: Optional[domain.SessionId], state: str) -> None:
        label = self._shell_label(sid) if state in ("ready", "running") else ""
        self._prompt_label.setText(f"{label} ❯" if label else "❯")

    def _shell_label(self, sid: Optional[domain.SessionId]) -> str:
        if sid is None:
            return ""
        getter = getattr(self._service, "shell_label", None)
        if getter is None:
            return ""
        return getter(sid) or ""

    def _update_offline_banner(self, state: str) -> None:
        visible = state in ("offline", "error")
        self._offline_banner.setVisible(visible)
        if not visible or self._active_session_id is None:
            return
        reason = self._service.session_problem(self._active_session_id).strip()
        if reason:
            detail = (
                f"{reason}\n"
                "Журнал открыт, прошлые команды на месте. "
                "Отправить новую команду можно после восстановления соединения."
            )
        else:
            detail = (
                "Соединение не установлено.\n"
                "Журнал открыт, прошлые команды на месте. "
                "Отправить новую команду можно после восстановления соединения."
            )
        self._offline_detail.setText(detail)

    def _on_restore_connection(self) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        stored = self._service.get_session(sid)
        if stored is None:
            return
        dialog = UnlockSessionDialog(self._service, stored, self, reconnect=True)
        dialog.exec()
        self._update_header()
        self._update_input_state()
        self._reload_sessions()
        if self._service.session_state(sid) == "ready":
            self.input.setFocus()

    def _refresh_status_style(self) -> None:
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    def _set_prompt_enabled(self, enabled: bool) -> None:
        self.input.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.paste_button.setEnabled(enabled)
        self.paste_run_button.setEnabled(enabled)

    def _viewport_rows(self, scroll: QScrollArea) -> int:
        height = scroll.viewport().height()
        cell = max(1, QFontMetrics(self._formats.mono).height())
        # Шапка карточки и отступы, чтобы nano целиком помещался в ленту.
        usable = max(cell * 8, height - cell * 3 - 24)
        return max(8, min(60, usable // cell))

    def _viewport_cols(self, scroll: QScrollArea) -> int:
        width = scroll.viewport().width() - 56
        cell = max(1, QFontMetrics(self._formats.mono).horizontalAdvance("M"))
        return max(20, min(400, width // cell))

    def _fit_live_terminal(self, card: CommandCard, scroll: QScrollArea) -> None:
        if not card.is_live():
            return
        card.terminal.use_geometry(
            self._viewport_cols(scroll), self._viewport_rows(scroll)
        )

    def _sync_live_rows(self) -> None:
        scroll = getattr(self, "history_scroll", None)
        if scroll is None:
            return
        for card in self._live_cards.values():
            if card.is_live():
                self._fit_live_terminal(card, scroll)

    def _show_card_top(self, card: CommandCard, scroll: QScrollArea) -> None:
        def go() -> None:
            try:
                bar = scroll.verticalScrollBar()
                bar.setValue(max(0, card.y() - 6))
            except RuntimeError:
                return
        QTimer.singleShot(0, go)

    # --- ввод команды -----------------------------------------------------

    def eventFilter(self, obj, event):  # type: ignore[override]
        if (
            event.type() == QEvent.Type.Resize
            and obj.property("vibeHistory")
        ):
            self._sync_live_rows()
            return False
        if obj is getattr(self, "input", None) and event.type() == QEvent.Type.KeyPress:
            if self._completion_list.isVisible():
                key = event.key()
                if key == Qt.Key.Key_Up:
                    self._move_completion(-1)
                    return True
                if key == Qt.Key.Key_Down:
                    self._move_completion(1)
                    return True
                if key == Qt.Key.Key_Escape:
                    self._disarm_tab()
                    return True
                if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    if event.modifiers() & (
                        Qt.KeyboardModifier.ShiftModifier
                        | Qt.KeyboardModifier.ControlModifier
                    ):
                        self.input.insertPlainText("\n")
                        return True
                    self._accept_current_completion()
                    return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & (
                Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier
            ):
                self.input.insertPlainText("\n")
                return True
            if event.key() == Qt.Key.Key_Up and self._history_edge(-1):
                self._navigate_history(-1)
                return True
            if event.key() == Qt.Key.Key_Down and self._history_edge(1):
                self._navigate_history(1)
                return True
            if event.key() == Qt.Key.Key_Tab:
                self._on_tab_complete()
                return True
            modifiers = event.modifiers()
            plain_enter = not (
                modifiers
                & (
                    Qt.KeyboardModifier.ShiftModifier
                    | Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.AltModifier
                )
            )
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and plain_enter:
                if command_needs_more(self.input.text()):
                    return False
                self._on_submit()
                return True
        return super().eventFilter(obj, event)

    def _history_edge(self, direction: int) -> bool:
        if self.input.blockCount() <= 1:
            return True
        cursor = self.input.textCursor()
        if cursor.hasSelection():
            return False
        if direction < 0:
            return cursor.blockNumber() == 0
        return cursor.blockNumber() == self.input.blockCount() - 1

    def _navigate_history(self, direction: int) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        history = self._command_history(sid)
        count = len(history)
        if count == 0:
            return

        state = self._prompts.setdefault(sid, _PromptState())
        current = count if state.index is None else max(0, min(count, state.index))
        new_index = max(0, min(count, current + direction))
        if new_index == current:
            return
        if current == count:
            state.draft = self.input.text()

        state.index = None if new_index == count else new_index
        if new_index == count:
            self.input.setText(state.draft)
        else:
            self.input.setText(history[new_index])
        state.text = self.input.text()
        self.input.setCursorPosition(len(self.input.text()))

    def _command_history(self, session_id: domain.SessionId) -> List[str]:
        layout = self._layouts.get(session_id)
        if layout is None:
            return self._journal_commands(session_id)
        commands: List[str] = []
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if not isinstance(widget, CommandCard):
                continue
            text = widget.command.strip()
            if not text or text in ("Система", "Команда"):
                continue
            commands.append(widget.command.rstrip("\n"))
        return commands

    def _journal_commands(self, session_id: domain.SessionId) -> List[str]:
        try:
            events = self._service.get_history(session_id)
        except Exception:
            return []
        commands: List[str] = []
        for event in events:
            if event.event_type != domain.EventType.COMMAND_SENT:
                continue
            text = event.payload.rstrip("\n")
            if text.strip():
                commands.append(text)
        return commands

    def _save_prompt(self, session_id: Optional[domain.SessionId]) -> None:
        if session_id is None:
            return
        state = self._prompts.setdefault(session_id, _PromptState())
        state.text = self.input.text()
        if state.index is None:
            state.draft = state.text

    def _save_chat_draft(self, session_id: Optional[domain.SessionId]) -> None:
        if session_id is None or self.chat_pane.isHidden():
            return
        self._chat_drafts[session_id] = self.chat_pane.input_text()

    def _sync_chat(self, session_id: Optional[domain.SessionId]) -> None:
        info = self._service.get_session(session_id) if session_id else None
        if info is None or not info.llm_profile_id:
            self.chat_pane.setVisible(False)
            return
        messages = []
        try:
            history = self._service.get_history(session_id)
        except Exception:
            history = []
        for event in history:
            if event.event_type == domain.EventType.CHAT_OS:
                messages.append(("os", event.payload))
            elif event.event_type == domain.EventType.CHAT_USER:
                messages.append(("user", event.payload))
            elif event.event_type == domain.EventType.CHAT_ASSISTANT:
                messages.append(("assistant", event.payload))
            elif event.event_type == domain.EventType.CHAT_RESET:
                messages.append(("reset", ""))
            elif event.event_type == domain.EventType.CHAT_TOOL:
                messages.append(("result", event.payload))
        self.chat_pane.setVisible(True)
        self.chat_pane.set_messages(messages)
        self._show_chat_context(session_id)
        waiting = session_id in self._chat_wait
        self.chat_pane.set_thinking(waiting)
        self.chat_pane.set_busy(waiting)
        self.chat_pane.set_input_text(self._chat_drafts.get(session_id, ""))
        self.chat_pane.set_problem(self._chat_problem(session_id, info))
        self._show_pending_mcp(session_id)
        self._work_split.setSizes([500, 420])

    def _chat_problem(
        self, session_id: domain.SessionId, info: domain.SessionInfo
    ) -> str:
        getter = getattr(self._service, "get_llm_profile", None)
        if getter is not None and info.llm_profile_id:
            if getter(info.llm_profile_id) is None:
                return "Сохранённая модель не найдена"
        problem = getattr(self._service, "llm_problem", None)
        if problem is None:
            return ""
        return problem(session_id)

    def _session_of_layout(self, layout: QVBoxLayout) -> Optional[domain.SessionId]:
        for session_id, candidate in self._layouts.items():
            if candidate is layout:
                return session_id
        return self._active_session_id

    def _on_attach_command(self, card: CommandCard) -> None:
        sid = self._active_session_id
        if sid is None or self.chat_pane.isHidden():
            return
        current = self.chat_pane.input_text()
        extra = card.attachment_text()
        if current.strip():
            if not current.endswith("\n"):
                current += "\n"
            current += "\n"
        self.chat_pane.set_input_text(current + extra)
        self._chat_drafts[sid] = self.chat_pane.input_text()
        self.chat_pane.focus_input()

    def _bind_lounge(self) -> None:
        if not hasattr(self._service, "list_lounge_tabs"):
            return
        self._lounge.use_service_tabs()
        self._lounge.add_tab_requested.connect(self._on_lounge_add)
        self._lounge.close_tab_requested.connect(self._on_lounge_close)
        self._lounge.rename_tab_requested.connect(self._on_lounge_rename)
        self._lounge.model_requested.connect(self._on_lounge_model)
        self._lounge.send_requested.connect(self._on_lounge_send)
        self._lounge.stop_requested.connect(self._on_lounge_stop)
        self._reload_lounge_tabs()

    def _reload_lounge_tabs(self) -> None:
        listing = getattr(self._service, "list_lounge_tabs", None)
        if listing is None:
            return
        rows = []
        for tab in listing():
            label = ""
            if tab.profile_id:
                profile = self._service.get_llm_profile(tab.profile_id)
                label = profile.name if profile is not None else tab.profile_id
            problem = ""
            getter = getattr(self._service, "lounge_problem", None)
            if getter is not None:
                problem = getter(tab.tab_id)
            rows.append(
                (tab.tab_id, tab.title, tab.profile_id, label, list(tab.messages), problem)
            )
        self._lounge.set_tabs(rows)
        for tab_id in self._lounge_wait:
            self._lounge.set_busy(tab_id, True)

    def _on_lounge_add(self) -> None:
        try:
            tab = self._service.open_lounge_tab()
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось открыть чат", str(exc))
            return
        self._reload_lounge_tabs()
        self._lounge.focus_tab(tab.tab_id)

    def _on_lounge_close(self, tab_id: str) -> None:
        try:
            self._service.close_lounge_tab(tab_id)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось закрыть чат", str(exc))
            return
        self._lounge_wait.discard(tab_id)
        self._reload_lounge_tabs()

    def _on_lounge_rename(self, tab_id: str, title: str) -> None:
        try:
            self._service.rename_lounge_tab(tab_id, title)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось переименовать чат", str(exc))
            return
        self._reload_lounge_tabs()

    def _on_lounge_model(self, tab_id: str, profile_id: str) -> None:
        try:
            self._service.choose_lounge_model(tab_id, profile_id)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось выбрать модель", str(exc))
            return
        self._reload_lounge_tabs()

    def _on_lounge_send(self) -> None:
        tab_id = self._lounge.current_tab_id()
        if tab_id in self._lounge_wait:
            return
        text = self._lounge.input_text()
        if not text.strip():
            return
        self._lounge.clear_input()
        self._lounge.set_problem(tab_id, "")
        self._lounge_wait.add(tab_id)
        self._lounge.set_busy(tab_id, True)

        def work() -> None:
            error = None
            restore = None
            try:
                self._service.send_lounge_chat(tab_id, text)
            except LlmUnavailable as exc:
                error = exc
            except Exception as exc:
                error = exc
                restore = text
            self._lounge_done.emit(tab_id, (error, restore))

        threading.Thread(target=work, daemon=True).start()

    def _on_lounge_stop(self) -> None:
        tab_id = self._lounge.current_tab_id()
        if tab_id not in self._lounge_wait:
            return
        cancel = getattr(self._service, "cancel_lounge_chat", None)
        if cancel is not None:
            cancel(tab_id)
        self._drop_mcp_pending("остановлено")

    def _on_lounge_done(self, tab_id: str, payload: object) -> None:
        error, restore = payload  # type: ignore[misc]
        self._lounge_wait.discard(tab_id)
        self._lounge.set_busy(tab_id, False)
        if restore and self._lounge.current_tab_id() == tab_id and not self._lounge.input_text():
            self._lounge.set_input_text(str(restore))
        self._reload_lounge_tabs()
        problem = ""
        getter = getattr(self._service, "lounge_problem", None)
        if getter is not None:
            problem = getter(tab_id)
        if error is not None and not problem:
            problem = str(error)
        self._lounge.set_problem(tab_id, problem)

    def _on_chat_send(self) -> None:
        sid = self._active_session_id
        if sid is None or self.chat_pane.isHidden() or self.chat_pane.is_busy():
            return
        text = self.chat_pane.input_text()
        if not text.strip():
            return
        self.chat_pane.clear_input()
        self.chat_pane.set_problem("")
        self._chat_drafts[sid] = ""
        self._chat_wait.add(sid)
        self.chat_pane.set_busy(True)
        self.chat_pane.set_thinking(True)

        def work() -> None:
            error = None
            restore = None
            try:
                self._service.send_chat(sid, text)
            except LlmUnavailable as exc:
                error = exc
            except Exception as exc:
                error = exc
                restore = text
            self._chat_done.emit(sid, (error, restore))

        threading.Thread(target=work, daemon=True).start()

    def _on_chat_stop(self) -> None:
        sid = self._active_session_id
        if sid is None or sid not in self._chat_wait:
            return
        cancel = getattr(self._service, "cancel_chat", None)
        if cancel is not None:
            cancel(sid)
        self._drop_mcp_pending("остановлено")

    def _on_chat_retry(self) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        self.chat_pane.set_busy(True)

        def work() -> None:
            error = None
            try:
                self._service.check_llm(sid)
            except Exception as exc:
                error = exc
            self._chat_done.emit(sid, (error, None))

        threading.Thread(target=work, daemon=True).start()

    def _on_chat_run_command(self, text: str) -> None:
        command = text.replace("\r\n", "\n").strip("\n")
        if command.strip():
            self._submit_text(command)

    def _show_chat_context(self, session_id: Optional[domain.SessionId]) -> None:
        measure = getattr(self._service, "chat_context", None)
        if session_id is None or measure is None:
            self.chat_pane.set_context("")
            return
        try:
            context = measure(session_id)
        except Exception:
            self.chat_pane.set_context("")
            return
        limit = 0
        measure_limit = getattr(self._service, "chat_limit", None)
        if measure_limit is not None:
            try:
                limit = int(measure_limit(session_id))
            except Exception:
                limit = 0
        if limit > 0 and hasattr(self.chat_pane, "set_usage"):
            self.chat_pane.set_usage(context.tokens, context.messages, limit)
            return
        self.chat_pane.set_context(
            f"~{context.tokens} ток. · {context.messages} сообщ."
        )

    def _on_chat_limit(self, value: int) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        setter = getattr(self._service, "set_chat_limit", None)
        if setter is None:
            return
        setter(sid, int(value))
        self._show_chat_context(sid)

    def _on_chat_reset(self) -> None:
        sid = self._active_session_id
        if sid is None or self.chat_pane.isHidden():
            return
        answer = QMessageBox.question(
            self,
            "Сбросить контекст",
            "Модель забудет переписку выше.\n"
            "Сообщения останутся на экране, но в следующие запросы не войдут.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._service.reset_chat_context(sid)
        except Exception as exc:
            QMessageBox.warning(self, "Не удалось сбросить контекст", str(exc))

    def _on_chat_apply_edit(self, path: str, old: str, new: str) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        FileEditDialog(self._service, sid, path, old, new, self).exec()

    def _on_chat_insert_command(self, text: str) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        command = text.replace("\r\n", "\n").strip("\n")
        self.input.setText(command)
        state = self._prompts.setdefault(sid, _PromptState())
        state.text = command
        state.draft = command
        state.index = None
        self.input.setCursorPosition(len(command))
        self.input.setFocus()

    @Slot(object, object)
    def _on_chat_done(self, session_id: object, payload: object) -> None:
        sid = session_id if isinstance(session_id, str) else None
        if sid is not None:
            self._chat_wait.discard(sid)
        if sid == self._active_session_id and not self.chat_pane.isHidden():
            self.chat_pane.set_thinking(False)
            self.chat_pane.set_busy(False)
        error = payload
        restore = None
        if isinstance(payload, tuple) and len(payload) == 2:
            error, restore = payload
        if restore and sid is not None:
            if sid == self._active_session_id and not self.chat_pane.isHidden():
                self.chat_pane.set_input_text(str(restore))
            else:
                self._chat_drafts[sid] = str(restore)
        if sid is None or sid != self._active_session_id or self.chat_pane.isHidden():
            return
        info = self._service.get_session(sid)
        if info is None:
            return
        problem = str(error) if error is not None else self._chat_problem(sid, info)
        title = ""
        if "инструмент" in problem or "подтвержден" in problem:
            title = "Ответ не завершён"
        self.chat_pane.set_problem(problem, title)

    def _restore_prompt(self, session_id: Optional[domain.SessionId]) -> None:
        if session_id is None:
            self.input.clear()
            return
        state = self._prompts.get(session_id, _PromptState())
        self.input.setText(state.text)
        self.input.setCursorPosition(len(state.text))

    def _on_paste(self) -> None:
        raw = QApplication.clipboard().text()
        if not raw:
            return
        flat = raw.replace("\r\n", "\n").replace("\r", "\n")
        self.input.insert(flat)
        self.input.setFocus()

    def _on_paste_run(self) -> None:
        raw = (
            QApplication.clipboard()
            .text()
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .strip()
        )
        if not raw:
            return
        self.input.setText(raw)
        self._submit_text(raw)

    def _on_submit(self) -> None:
        self._submit_text(self.input.text())

    def _submit_text(self, text: str) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        if self._service.session_state(sid) != "ready":
            return

        if not text.strip():
            return

        self._disarm_tab()
        state = self._prompts.setdefault(sid, _PromptState())
        state.text = ""
        state.draft = ""
        state.index = None
        self.input.clear()

        try:
            self._service.send_command(sid, text)
        except Exception as exc:
            self._append_system_message(
                f"[ошибка] Не удалось отправить команду: {exc}",
                self._formats.error,
            )
            return

        self._update_input_state()
        self._reload_sessions()

    # --- приём событий ----------------------------------------------------

    def _on_event_from_driver(self, event: domain.TranscriptEvent) -> None:
        self._event_received.emit(event)

    def _mirror_lounge_command(self, event: domain.TranscriptEvent) -> None:
        """Команда из общего чата видна в его ленте и принимает ввод, пока идёт."""
        tab_id = self._lounge_runs.get(event.session_id)
        if not tab_id:
            return
        kind = event.event_type
        if kind == domain.EventType.COMMAND_SENT:
            card = self._lounge.open_command_card(
                tab_id,
                event.payload.rstrip("\n"),
                self._mcp_label(event.session_id),
            )
            if card is None:
                return
            session_id = event.session_id
            card.input_generated.connect(
                lambda data, s=session_id: self._service.write_input(s, data)
            )
            card.resized.connect(
                lambda cols, rows, s=session_id: self._service.resize(s, cols, rows)
            )
            card.reject_requested.connect(
                lambda reason, s=session_id, command=card.command: self._reject_interactive(
                    s, reason, command
                )
            )
            return
        if kind == domain.EventType.OUTPUT_CHUNK:
            self._lounge.feed_command_card(
                tab_id,
                event.payload,
                event.columns,
                event.rows,
                event.alternate,
                event.persist,
                self._formats.stdout,
                self._formats.stderr,
                event.stream_type == domain.StreamType.STDERR,
            )
            if _asks_for_secret(event.payload):
                self._lounge.focus_command_card(tab_id)
            return
        if kind == domain.EventType.ERROR and event.command_id is not None:
            self._lounge.feed_command_card(
                tab_id,
                f"[ошибка] {event.payload}\n",
                None,
                None,
                False,
                True,
                self._formats.stdout,
                self._formats.error,
                True,
            )
            return
        if kind == domain.EventType.COMMAND_FINISHED:
            self._finish_lounge_card(tab_id, event.return_code)
            self._lounge_runs.pop(event.session_id, None)

    @Slot(object)
    def _on_event_ui(self, event: domain.TranscriptEvent) -> None:
        sid = event.session_id
        active = self._active_session_id
        lounge_tab = lounge_tab_id(sid)
        if lounge_tab:
            if event.event_type == domain.EventType.CHAT_USER:
                self._lounge.append_message(lounge_tab, "user", event.payload)
            elif event.event_type == domain.EventType.CHAT_ASSISTANT:
                self._lounge.append_message(lounge_tab, "assistant", event.payload)
            elif event.event_type == domain.EventType.CHAT_TOOL:
                self._lounge.append_message(lounge_tab, "result", event.payload)
            elif event.event_type == domain.EventType.CHAT_COMMAND:
                self._lounge.append_message(lounge_tab, "command", event.payload)
            return
        self._mirror_lounge_command(event)
        if event.event_type in (
            domain.EventType.CHAT_USER,
            domain.EventType.CHAT_ASSISTANT,
            domain.EventType.CHAT_RESET,
            domain.EventType.CHAT_OS,
            domain.EventType.CHAT_TOOL,
        ):
            if active == sid and not self.chat_pane.isHidden():
                if event.event_type == domain.EventType.CHAT_RESET:
                    role = "reset"
                elif event.event_type == domain.EventType.CHAT_OS:
                    role = "os"
                elif event.event_type == domain.EventType.CHAT_TOOL:
                    role = "result"
                elif event.event_type == domain.EventType.CHAT_USER:
                    role = "user"
                else:
                    role = "assistant"
                self.chat_pane.append_message(role, event.payload)
                if role == "assistant" and sid in self._chat_wait:
                    self.chat_pane.set_thinking(True)
                self._show_chat_context(sid)
            return
        if event.event_type == domain.EventType.SHELL_CONTEXT:
            if active == sid:
                self._update_input_state()
            return
        if sid not in self._layouts:
            if event.event_type in (
                domain.EventType.COMMAND_SENT,
                domain.EventType.COMMAND_FINISHED,
                domain.EventType.SESSION_STATUS,
            ):
                if active == sid:
                    self._update_input_state()
                self._reload_sessions()
            return

        et = event.event_type
        layout = self._layouts[sid]
        scroll = self._panels[sid]

        if et == domain.EventType.SESSION_STATUS:
            if active == sid:
                self._update_input_state()
            self._reload_sessions()
            return

        if et == domain.EventType.COMMAND_SENT:
            card = CommandCard(event.payload.rstrip("\n"), self._formats.mono)
            card.input_generated.connect(
                lambda data, s=sid: self._service.write_input(s, data)
            )
            card.resized.connect(
                lambda cols, rows, s=sid: self._service.resize(s, cols, rows)
            )
            card.reject_requested.connect(
                lambda reason, s=sid, command=card.command: self._reject_interactive(
                    s, reason, command
                )
            )
            self._live_cards[sid] = card
            self._add_history_widget(card, layout, scroll)
            card.begin_live()
            self._fit_live_terminal(card, scroll)
            QTimer.singleShot(
                0, lambda c=card, s=scroll: self._fit_live_terminal(c, s)
            )
            if active == sid:
                self._update_input_state()
                self._reload_sessions()
                card.focus_terminal()
            return

        if et == domain.EventType.OUTPUT_CHUNK:
            if sid in self._dismissed_commands:
                return
            card = self._live_cards.get(sid)
            if card is None:
                card = CommandCard("Команда", self._formats.mono)
                self._live_cards[sid] = card
                self._add_history_widget(card, layout, scroll)
            if not event.persist:
                card.feed_live(
                    event.payload, event.columns, event.rows, event.alternate
                )
            elif card.is_live():
                card.remember_plain(event.payload)
            else:
                fmt = (
                    self._formats.stderr
                    if event.stream_type == domain.StreamType.STDERR
                    else self._formats.stdout
                )
                card.append_output(event.payload, fmt)
            if active == sid:
                if card.is_fullscreen():
                    self._show_card_top(card, scroll)
                else:
                    self._scroll_to_bottom(scroll)
            return

        if et == domain.EventType.ERROR:
            if sid in self._dismissed_commands and event.command_id is not None:
                return
            if event.command_id is None:
                if active == sid:
                    self._update_input_state()
                self._reload_sessions()
                return
            card = self._live_cards.get(sid)
            if card is None:
                card = CommandCard("Команда", self._formats.mono)
                self._add_history_widget(card, layout, scroll)
            card.append_output(
                f"[ошибка] {event.payload}\n", self._formats.error
            )
            if active == sid:
                self._scroll_to_bottom(scroll)
            return

        if et == domain.EventType.COMMAND_FINISHED:
            self._dismissed_commands.discard(sid)
            card = self._live_cards.pop(sid, None)
            if card is not None:
                if card.is_live():
                    plain = card.take_plain()
                    kept = card.end_live()
                    if kept:
                        if plain:
                            card.remember_plain(plain)
                    elif plain:
                        card.append_output(plain, self._formats.stdout)
                card.finalize(event.return_code, self._formats.exit)
            if active == sid:
                self._update_input_state()
            self._reload_sessions()
            if active == sid:
                self._scroll_to_bottom(scroll)

    # --- вспомогательное --------------------------------------------------

    def _append_system_message(
        self,
        text: str,
        fmt: Optional[QTextCharFormat] = None,
    ) -> None:
        card = CommandCard("Система", self._formats.mono)
        card.copy_button.hide()
        card.resend_button.hide()
        card.delete_button.hide()
        card.command_label.setText("Система")
        card.append_output(
            text.rstrip("\n") + "\n", fmt or self._formats.system
        )
        self._add_history_widget(card)

    def _add_history_widget(
        self,
        widget: QWidget,
        layout: Optional[QVBoxLayout] = None,
        scroll: Optional[QScrollArea] = None,
    ) -> None:
        target = layout or self.history_layout
        if isinstance(widget, CommandCard):
            widget.remove_requested.connect(self._on_card_remove)
            widget.resend_requested.connect(self._on_card_resend)
            sid = self._session_of_layout(target)
            info = self._service.get_session(sid) if sid is not None else None
            if (
                info is not None
                and info.llm_profile_id
                and widget.command not in ("Система", "Команда")
            ):
                widget.set_attach_visible(True)
                widget.attach_requested.connect(self._on_attach_command)
        target.insertWidget(target.count() - 1, widget)
        self._scroll_to_bottom(scroll or self.history_scroll)

    def _scroll_to_bottom(self, scroll: Optional[QScrollArea] = None) -> None:
        target = scroll or self.history_scroll

        def go() -> None:
            try:
                bar = target.verticalScrollBar()
            except RuntimeError:
                return
            bar.setValue(bar.maximum())

        QTimer.singleShot(0, go)

    def _on_card_remove(self, card: CommandCard) -> None:
        parent = card.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None:
            layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        for session_id, live in list(self._live_cards.items()):
            if live is not card:
                continue
            self._live_cards.pop(session_id, None)
            self._dismissed_commands.add(session_id)
            cancel = getattr(self._service, "cancel_command", None)
            if cancel is not None:
                try:
                    cancel(session_id)
                except Exception:
                    pass
            if session_id == self._active_session_id:
                self._update_input_state()

    def _on_card_resend(self, card: CommandCard) -> None:
        sid = self._active_session_id
        if sid is None:
            return

        self.input.setText(card.command)
        self.input.setCursorPosition(len(card.command))
        self.input.setFocus()

        if self._service.session_state(sid) == "ready":
            self._on_submit()
    
    # --- закрытие ---------------------------------------------------------

    def closeEvent(self, event):  # type: ignore[override]
        self._drop_mcp_pending("окно закрыто")
        try:
            self._service.unsubscribe(self._on_event_from_driver)
            self._service.close_all()
        except Exception:
            pass
        super().closeEvent(event)

    def _approve_mcp(self, prompt: CommandAsk) -> CommandAnswer:
        event = threading.Event()
        box: Dict[str, CommandAnswer] = {}

        def finish(answer: CommandAnswer) -> None:
            box["answer"] = answer
            event.set()

        self._mcp_ask.emit((prompt, finish))
        event.wait()
        return box.get("answer") or CommandAnswer("reject", "")

    def _present_mcp(self, payload: object) -> None:
        if not isinstance(payload, tuple) or len(payload) != 2:
            return
        prompt, finish = payload
        if not isinstance(prompt, CommandAsk):
            finish(CommandAnswer("reject_reason", "запрос команды повреждён"))
            return
        info = self._service.get_session(prompt.session_id)
        if info is None:
            finish(CommandAnswer("reject_reason", "сессия не найдена"))
            return
        tab_id = lounge_tab_id(prompt.origin_id)
        if tab_id:
            if not self._lounge.has_tab(tab_id):
                finish(CommandAnswer("reject_reason", "вкладка общего чата не найдена"))
                return
            self._mcp_pending[prompt.origin_id] = (prompt, finish)
            if self._active_session_id != GENERAL_CHAT_ID:
                self._show_general_chat(self._active_session_id)
            self._lounge.focus_tab(tab_id)
            self._lounge.show_command_request(
                tab_id,
                prompt.request_id,
                prompt.intent,
                prompt.command,
                info.mcp_alias.strip() or info.name,
            )
            return
        if not info.llm_profile_id:
            finish(CommandAnswer("reject_reason", "у активной сессии нет чата для подтверждения"))
            return
        self._mcp_pending[prompt.session_id] = (prompt, finish)
        if self._active_session_id != prompt.session_id:
            state = self._service.session_state(prompt.session_id)
            if state != "closed":
                self._activate_session(prompt.session_id)
                return
        self._show_pending_mcp(prompt.session_id)

    def _show_pending_mcp(self, session_id: Optional[domain.SessionId]) -> None:
        if session_id is None or self.chat_pane.isHidden():
            return
        item = self._mcp_pending.get(session_id)
        if item is None:
            self.chat_pane.clear_command_request()
            return
        prompt, _finish = item
        self.chat_pane.show_command_request(prompt.request_id, prompt.intent, prompt.command)

    def _on_command_decided(self, request_id: str, action: str, reason: str) -> None:
        found = None
        for session_id, item in list(self._mcp_pending.items()):
            if item[0].request_id == request_id:
                found = self._mcp_pending.pop(session_id)
                break
        self.chat_pane.clear_command_request()
        self._lounge.clear_command_request()
        if found is None:
            return
        prompt = found[0]
        self._record_tool_outcome(prompt, action, reason)
        tab_id = lounge_tab_id(prompt.origin_id)
        if tab_id and action in ("run", "always"):
            self._lounge_runs[prompt.session_id] = tab_id
        found[1](CommandAnswer(action, reason))
        if (
            prompt.session_id in self._chat_wait
            and prompt.session_id == self._active_session_id
        ):
            self.chat_pane.set_thinking(True)

    def _reject_interactive(self, session_id: str, reason: str, command: str) -> None:
        reject = getattr(self._service, "reject_running_command", None)
        if callable(reject):
            try:
                reject(session_id, reason)
            except Exception:
                self._service.write_input(session_id, "\x03")
        else:
            self._service.write_input(session_id, "\x03")
        tab = self._lounge_runs.get(session_id, "")
        note = getattr(self._service, "record_tool_outcome", None)
        if callable(note):
            try:
                note(
                    session_id,
                    f"отклонено с комментарием {reason.strip()}\n{command.strip()}",
                    tab,
                )
            except Exception:
                pass
        if tab:
            self._finish_lounge_card(tab, None)
            self._lounge_runs.pop(session_id, None)

    def _record_tool_outcome(self, prompt: CommandAsk, action: str, reason: str) -> None:
        command = prompt.command.strip()
        if action in ("run", "always"):
            text = f"выполнено: {command}"
        elif reason.strip():
            text = f"отклонено с комментарием {reason.strip()}\n{command}"
        else:
            text = f"отклонено\n{command}"
        note = getattr(self._service, "record_tool_outcome", None)
        if not callable(note):
            return
        tab = lounge_tab_id(prompt.origin_id)
        if tab:
            label = self._mcp_label(prompt.session_id)
            if label:
                text = f"{text}\nMCP: {label}"
        try:
            note(prompt.session_id, text, tab)
        except Exception:
            return

    def _mcp_label(self, session_id: str) -> str:
        getter = getattr(self._service, "get_session", None)
        if not callable(getter):
            return ""
        try:
            info = getter(session_id)
        except Exception:
            return ""
        if info is None:
            return ""
        return (getattr(info, "mcp_alias", "") or "").strip() or getattr(info, "name", "")

    def _finish_lounge_card(self, tab_id: str, return_code: Optional[int]) -> None:
        record = self._lounge.finish_command_card(
            tab_id,
            return_code,
            self._formats.stdout,
            self._formats.exit,
        )
        if not record:
            return
        remember = getattr(self._service, "remember_lounge_command", None)
        if not callable(remember):
            return
        try:
            remember(tab_id, record)
        except Exception:
            return

    def _drop_mcp_pending(self, reason: str) -> None:
        pending = list(self._mcp_pending.values())
        self._mcp_pending.clear()
        self.chat_pane.clear_command_request()
        self._lounge.clear_command_request()
        for prompt, finish in pending:
            self._record_tool_outcome(prompt, "reject_reason", reason)
            finish(CommandAnswer("reject_reason", reason))
        
    def _on_tab_complete(self) -> None:
        sid = self._active_session_id
        if sid is None:
            return
        if self._service.session_state(sid) != "ready":
            return

        text = self.input.text()
        if (
            self._tab_list_armed
            and text == self._tab_text
            and len(self._tab_candidates) > 1
        ):
            self._show_completion_list(self._tab_candidates)
            return
        if self._completion_pending and text == self._tab_text:
            self._tab_list_armed = True
            return
        if not text or text[-1].isspace():
            return

        self._completion_generation += 1
        generation = self._completion_generation
        self._completion_pending = True
        self._tab_text = text

        def work() -> None:
            try:
                candidates = self._service.complete(sid, text)
            except Exception:
                candidates = []
            self._completion_ready.emit(generation, text, candidates)

        threading.Thread(target=work, daemon=True).start()

    @Slot(int, str, object)
    def _on_completion_ready(self, generation: int, text: str, candidates: object) -> None:
        if generation != self._completion_generation:
            return
        self._completion_pending = False
        if self.input.text() != text:
            return
        found = list(candidates) if isinstance(candidates, list) else []
        if not found:
            self._disarm_tab()
            return
        if len(found) == 1 and not self._tab_list_armed:
            self._apply_completion(found[0])
            self._disarm_tab()
            return

        common = os.path.commonprefix(found)
        idx = max(text.rfind(" "), text.rfind("\t"))
        current_token = text[idx + 1 :] if idx >= 0 else text
        if len(common) > len(current_token) and not self._tab_list_armed:
            self._apply_completion(common)

        self._tab_candidates = found
        self._tab_text = self.input.text()
        if self._tab_list_armed and len(found) > 1:
            self._show_completion_list(found)
            return
        self._tab_list_armed = True
        self._show_completion_hint(len(found))

    def _on_input_changed(self, text: str) -> None:
        if text != self._tab_text:
            self._disarm_tab()

    def _disarm_tab(self) -> None:
        self._completion_generation += 1
        self._completion_pending = False
        self._tab_list_armed = False
        self._tab_text = None
        self._tab_candidates = []
        self._completion_hint.setVisible(False)
        self._completion_list.setVisible(False)
        self._completion_list.clear()

    def _show_completion_hint(self, count: int) -> None:
        self._completion_list.setVisible(False)
        self._completion_hint.setText(_variants_phrase(count))
        self._completion_hint.setVisible(True)

    def _show_completion_list(self, candidates: List[str]) -> None:
        self._completion_hint.setVisible(False)
        self._completion_list.clear()
        for item in candidates[:300]:
            self._completion_list.addItem(item)
        if self._completion_list.count():
            self._completion_list.setCurrentRow(0)
        self._completion_list.setVisible(True)

    def _move_completion(self, delta: int) -> None:
        count = self._completion_list.count()
        if count == 0:
            return
        row = self._completion_list.currentRow()
        self._completion_list.setCurrentRow(max(0, min(count - 1, row + delta)))

    def _accept_current_completion(self) -> None:
        item = self._completion_list.currentItem()
        if item is not None:
            self._apply_completion(item.text())
        self._disarm_tab()
        self.input.setFocus()

    def _accept_completion_item(self, item: QListWidgetItem) -> None:
        self._apply_completion(item.text())
        self._disarm_tab()
        self.input.setFocus()

    def _apply_completion(self, completion: str) -> None:
        text = self.input.text()
        idx = max(text.rfind(" "), text.rfind("\t"))
        if idx >= 0:
            new_text = text[: idx + 1] + completion
        else:
            new_text = completion
        self.input.setText(new_text)
        self.input.setCursorPosition(len(new_text))