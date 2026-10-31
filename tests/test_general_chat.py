# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Вёрстка общего чата: вкладки, имя, модель и список MCP."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json

import domain
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCharFormat
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from application.terminal_mcp import call_record
from ui.command_card import CommandCard
from ui.fitted_text import sync_scroll_extent
from ui.terminal_view import TerminalView
from ui.general_chat import GENERAL_CHAT_ID, GENERAL_CHAT_TITLE, GeneralChatPage
from ui.main_window import MainWindow


class _Service:
    def __init__(self) -> None:
        self.foreground = "kept"
        self.sessions = [
            domain.SessionInfo(
                session_id="a",
                name="рабочий",
                provider=domain.ProviderType.CMD,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.READY,
                llm_profile_id="m-1",
                mcp_alias="консоль",
                mcp_description="основной терминал",
            ),
            domain.SessionInfo(
                session_id="c",
                name="офлайн",
                provider=domain.ProviderType.CMD,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.READY,
                llm_profile_id="m-1",
            ),
            domain.SessionInfo(
                session_id="b",
                name="без модели",
                provider=domain.ProviderType.CMD,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.READY,
            ),
        ]

    def subscribe(self, _callback) -> None:
        return None

    def list_sessions(self):
        return list(self.sessions)

    def session_state(self, session_id: str) -> str:
        if session_id == "c":
            return "offline"
        return "ready"

    def get_session(self, session_id: str):
        for info in self.sessions:
            if info.session_id == session_id:
                return info
        return None

    def get_llm_profile(self, profile_id: str):
        if profile_id == "m-1":
            return domain.LlmProfile("m-1", "локальная", "http://127.0.0.1:1234/v1", "demo")
        return None

    def list_llm_profiles(self):
        profile = self.get_llm_profile("m-1")
        return [profile] if profile is not None else []

    def mcp_endpoint(self, session_id=None) -> str:
        info = self.get_session(session_id) if session_id else None
        if info is None or not info.llm_profile_id:
            return ""
        return f"http://127.0.0.1:9/mcp/{session_id}"

    def set_foreground_session(self, session_id) -> None:
        self.foreground = session_id


class GeneralChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def test_page_switches_tabs_renames_and_locks_the_model(self) -> None:
        page = GeneralChatPage()
        page.show()
        QApplication.processEvents()
        from PySide6.QtWidgets import QLabel

        page.set_models([("m-1", "локальная")])
        sent = []
        stopped = []
        page.send_requested.connect(lambda: sent.append(page.input_text()))
        page.stop_requested.connect(lambda: stopped.append(page.current_tab_id()))
        self.assertFalse(page._send.isEnabled())
        page._model_combo.setCurrentIndex(1)
        page._pick_model(1)
        self.assertTrue(page._send.isEnabled())
        page._draft.setText("привет")
        page._send.click()
        self.assertEqual(sent, ["привет"])
        page.set_busy(page.current_tab_id(), True)
        self.assertEqual(page._send.text(), "Остановить")
        page._send.click()
        self.assertEqual(stopped, [page.current_tab_id()])
        page.set_busy(page.current_tab_id(), False)
        texts = [label.text() for label in page.findChildren(QLabel)]
        self.assertTrue(any("Сообщений пока нет" in text for text in texts))
        self.assertEqual(page.current_title(), "чат")
        page._add_tab()
        QApplication.processEvents()
        self.assertEqual(page.tab_titles(), ["чат", "чат 2"])
        self.assertEqual(page.current_title(), "чат 2")
        page._select_tab(0)
        page._begin_rename()
        page._title_edit.setText("черновик")
        page._commit_title()
        self.assertEqual(page.current_title(), "черновик")
        page._model_combo.setCurrentIndex(1)
        page._pick_model(1)
        self.assertEqual(page.current_model_label(), "локальная")
        self.assertFalse(page._model_combo.isVisible())
        page._add_tab()
        first = page._tabs[0].tab_id
        page.show_command_request(first, "id-1", "список", "ls", "консоль")
        page._select_tab(0)
        QApplication.processEvents()
        self.assertEqual(page.findChild(QLabel, "commandTarget").text(), "MCP: консоль")
        page._select_tab(1)
        QApplication.processEvents()
        self.assertIsNone(page._request_card)
        self.assertIsNone(page._tabs[1].request)
        page.set_mcp_entries([("консоль", "основной терминал")])
        page._mcp_list.itemDoubleClicked.emit(page._mcp_list.item(0))
        self.assertEqual(page.input_text(), "«MCP: консоль»")
        self.assertEqual(page._mcp_list.item(0).text(), "консоль")
        self.assertEqual(page._mcp_list.item(0).toolTip(), "основной терминал")
        page.set_mcp_entries(
            [
                ("консоль", "основной терминал", "http://127.0.0.1:9/mcp/a"),
                ('сервер "б"', "второй", "http://127.0.0.1:9/mcp/b"),
            ]
        )
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QPushButton

        copy = page.findChild(QPushButton, "copyMcpJson")
        assert copy is not None
        copy.click()
        self.assertEqual(
            QGuiApplication.clipboard().text(),
            '"консоль": {\n'
            '"url": "http://127.0.0.1:9/mcp/a"\n'
            "},\n"
            '"сервер \\"б\\"": {\n'
            '"url": "http://127.0.0.1:9/mcp/b"\n'
            "},",
        )
        self.assertIsNotNone(page.findChild(QWidget, "loungeTabBar"))
        self.assertIsNotNone(page.findChild(QWidget, "loungeHead"))
        page.set_messages(
            page.current_tab_id(),
            [("user", "запрос альфа"), ("assistant", "ответ бета")],
        )
        QApplication.processEvents()
        edits = [edit for edit in page.findChildren(QTextEdit) if edit.objectName() == "chatUser"]
        self.assertEqual(len(edits), 1)
        user = edits[0]
        self.assertLess(user.height(), 80)
        self.assertIn("запрос альфа", user.toPlainText())
        index = page._current
        page._add_tab()
        QApplication.processEvents()
        page._select_tab(index)
        QApplication.processEvents()
        edits = [edit for edit in page.findChildren(QTextEdit) if edit.objectName() == "chatUser" and edit.isVisible()]
        self.assertEqual(len(edits), 1)
        self.assertLess(edits[0].height(), 80)
        self.assertIn("запрос альфа", edits[0].toPlainText())
        user = edits[0]
        self.assertTrue(user.isReadOnly())
        cursor = user.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(6, cursor.MoveMode.KeepAnchor)
        user.setTextCursor(cursor)
        self.assertEqual(user.textCursor().selectedText(), "запрос")
        user.copy()
        self.assertEqual(QApplication.clipboard().text(), "запрос")
        frame = user.document().rootFrame().frameFormat()
        self.assertGreaterEqual(frame.leftMargin(), 14)
        self.assertLessEqual(frame.topMargin(), 8)
        draft = page._draft
        one_line = draft.height()
        draft.insertPlainText("раз\nдва\nтри\nчетыре")
        QApplication.processEvents()
        self.assertGreater(draft.height(), one_line)
        page.close()

    def test_command_history_stays_when_the_tab_changes(self) -> None:
        page = GeneralChatPage()
        page.show()
        QApplication.processEvents()
        first = page.current_tab_id()
        card = page.open_command_card(first, "ls", "консоль")
        self.assertIsNotNone(card)
        fmt = QTextCharFormat()
        card.append_output("a.txt\n", fmt)
        page.finish_command_card(first, 0, fmt, fmt)
        page.append_message(first, "command", card.record)
        page._add_tab()
        QApplication.processEvents()
        page._select_tab(0)
        QApplication.processEvents()
        self.assertFalse(any(item.isVisible() for item in page.findChildren(CommandCard)))
        label = page.findChild(QLabel, "toolCall")
        self.assertIsNotNone(label)
        self.assertEqual(label.text(), "консоль: ls")
        self.assertEqual(label.parent().findChild(QLabel, "noteMcp").text(), "консоль")
        body = label.parent().findChild(QTextEdit, "commandOutput").parent()
        self.assertTrue(body.isHidden())
        QTest.mouseClick(label, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        self.assertFalse(body.isHidden())
        self.assertIn("a.txt", label.parent().findChild(QTextEdit, "commandOutput").toPlainText())
        self.assertIn("консоль", card.record)
        page.close()

    def test_saved_command_renders_without_a_live_card(self) -> None:
        page = GeneralChatPage()
        page.show()
        QApplication.processEvents()
        payload = json.dumps(
            {"mcp": "консоль", "command": "ls", "output": "a.txt", "code": 0},
            ensure_ascii=False,
        )
        page.set_messages(
            page.current_tab_id(),
            [("user", "покажи"), ("command", payload), ("result", "выполнено: ls\nMCP: консоль")],
        )
        QApplication.processEvents()
        self.assertFalse(any(not item.isHidden() for item in page.findChildren(CommandCard)))
        label = page.findChild(QLabel, "toolCall")
        self.assertIsNotNone(label)
        self.assertEqual(label.text(), "консоль: ls")
        self.assertEqual(label.parent().findChild(QLabel, "noteMcp").text(), "консоль")
        body = label.parent().findChild(QTextEdit, "commandOutput").parent()
        self.assertTrue(body.isHidden())
        QTest.mouseClick(label, Qt.MouseButton.LeftButton)
        QApplication.processEvents()
        self.assertIn("a.txt", label.parent().findChild(QTextEdit, "commandOutput").toPlainText())
        page._add_tab()
        page._select_tab(0)
        QApplication.processEvents()
        again = page.findChild(QLabel, "toolCall")
        self.assertIsNotNone(again)
        self.assertEqual(again.text(), "консоль: ls")
        self.assertEqual(again.parent().findChild(QLabel, "noteMcp").text(), "консоль")
        self.assertTrue(again.parent().findChild(QTextEdit, "commandOutput").parent().isHidden())
        page.close()

    def test_window_opens_general_chat_and_lists_live_mcp(self) -> None:
        service = _Service()
        window = MainWindow(service)
        window.show()
        QApplication.processEvents()
        self.assertEqual(
            window.session_list.topLevelItem(0).data(0, Qt.ItemDataRole.UserRole),
            GENERAL_CHAT_ID,
        )
        self.assertEqual(window.session_list.topLevelItem(0).text(0), GENERAL_CHAT_TITLE)
        window._activate_session(GENERAL_CHAT_ID)
        QApplication.processEvents()
        self.assertIs(window._pages.currentWidget(), window._lounge)
        self.assertEqual(service.foreground, None)
        self.assertEqual(window._lounge._mcp_list.count(), 1)
        self.assertEqual(window._lounge._mcp_list.item(0).text(), "консоль")
        self.assertEqual(
            window._lounge._mcp_list.item(0).toolTip(),
            "основной терминал",
        )
        self.assertFalse(window.close_button.isEnabled())
        window._activate_session("b")
        QApplication.processEvents()
        self.assertIs(window._pages.currentWidget(), window._terminal_page)
        self.assertFalse(window._settings_button.isHidden())
        self.assertEqual(window.findChildren(QLabel, "mcpEndpoint"), [])
        from ui.session_settings import SessionSettingsDialog

        dialog = SessionSettingsDialog(service.get_session("a"), service.mcp_endpoint("a"))
        self.assertEqual(dialog.mcp_alias(), "консоль")
        self.assertEqual(dialog.mcp_description(), "основной терминал")
        self.assertIn("/mcp/a", dialog._url.text())
        dialog.close()
        window.close()

    def test_short_output_does_not_keep_empty_pty_rows(self) -> None:
        font = QFont("Cascadia Mono", 10)
        view = TerminalView(font)
        view.set_interactive(True)
        view.resize(640, 400)
        view.feed("hello\nworld\n")
        view.feed("\x1b[22;1H")
        cell = view.fontMetrics().height()
        self.assertLess(view.height(), cell * 8)
        view.feed("\x1b[?1049h")
        view.set_preferred_rows(20)
        self.assertGreaterEqual(view.height(), cell * 8)
        view.close()

        card = CommandCard("cat /proc/loadavg", font)
        card.resize(640, 40)
        fmt = QTextCharFormat()
        fmt.setFont(font)
        card.append_output("0.07 0.03 0.00\n" + ("\n" * 30), fmt)
        QApplication.processEvents()
        self.assertLess(card.output.height(), cell * 6)
        card.close()

    def test_scroll_range_follows_a_taller_terminal(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.resize(240, 120)
        host = QWidget()
        layout = QVBoxLayout(host)
        tall = QWidget()
        tall.setFixedHeight(480)
        layout.addWidget(tall)
        layout.addStretch(1)
        scroll.setWidget(host)
        scroll.show()
        QApplication.processEvents()
        sync_scroll_extent(scroll)
        QApplication.processEvents()
        self.assertGreaterEqual(scroll.verticalScrollBar().maximum(), 300)
        scroll.close()

    def test_user_and_assistant_text_is_visible(self) -> None:
        page = GeneralChatPage(QFont("Consolas"))
        page.resize(900, 700)
        page.show()
        QApplication.processEvents()
        page.set_messages(
            page.current_tab_id(),
            [
                ("user", "какой аптайм"),
                ("assistant", "Сейчас проверю."),
                ("result", call_record("выполнено", "uptime", "up 11:52", "local")),
            ],
        )
        QApplication.processEvents()
        user = page.findChild(QTextEdit, "chatUser")
        assistant = page.findChild(QTextEdit, "chatAssistant")
        note = page.findChild(QLabel, "toolCall")
        assert user is not None and assistant is not None and note is not None
        self.assertGreater(user.viewport().width(), 40)
        self.assertGreater(assistant.viewport().width(), 40)
        self.assertLess(user.height(), 80)
        self.assertLess(assistant.height(), 80)
        self.assertEqual(user.toPlainText(), "какой аптайм")
        self.assertEqual(assistant.toPlainText(), "Сейчас проверю.")
        self.assertEqual(note.text(), "local: uptime")
        self.assertEqual(note.parent().findChild(QLabel, "noteMcp").text(), "local")
        page.close()
