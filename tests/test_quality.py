# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Проверки журнала, экрана и поля ввода."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime

import domain
from application.service import ApplicationService, SessionUnavailable
from screen_log import ScreenLog


class _Journal:
    def __init__(self) -> None:
        self.events = []
        self.deleted = []
        self.locked = []
        self.unlocked = set()
        self.stored = []
        self.fail = False
        self.next = 0

    def append(self, event: domain.TranscriptEvent) -> None:
        if event.sequence != self.next:
            raise domain.JournalCorruptedError(
                f"ждали {self.next}, пришло {event.sequence}"
            )
        if self.fail:
            self.fail = False
            raise OSError("disk")
        self.events.append(event)
        self.next += 1

    def next_sequence(self, session_id: str) -> int:
        return self.next

    def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)

    def lock(self, session_id: str) -> None:
        self.locked.append(session_id)
        self.unlocked.discard(session_id)

    def unlock(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def is_unlocked(self, session_id: str) -> bool:
        return session_id in self.unlocked

    def get_by_session(self, session_id: str):
        return list(self.stored)

    def read(self, session_id: str):
        return []


class _Catalog:
    def __init__(self) -> None:
        self.items = {}

    def list(self):
        return list(self.items.values())

    def get(self, session_id: str):
        return self.items.get(session_id)

    def save(self, info: domain.SessionInfo) -> None:
        self.items[info.session_id] = info

    def delete(self, session_id: str) -> None:
        self.items.pop(session_id, None)


def _event(session_id: str, payload: str) -> domain.OutputEvent:
    return domain.OutputEvent(
        session_id=session_id,
        event_type=domain.EventType.OUTPUT_CHUNK,
        timestamp=datetime.now(),
        payload=payload,
        persist=True,
    )


class JournalSequenceTests(unittest.TestCase):
    def test_failed_append_does_not_advance_sequence(self) -> None:
        journal = _Journal()
        catalog = _Catalog()
        service = ApplicationService(lambda provider: None, catalog, journal)
        session_id = "s-1"
        seen = []
        service.subscribe(seen.append)

        service._handle_driver_event(_event(session_id, "one"))
        journal.fail = True
        service._handle_driver_event(_event(session_id, "two"))
        service._handle_driver_event(_event(session_id, "three"))

        self.assertEqual([event.payload for event in journal.events], ["one", "three"])
        self.assertEqual([event.sequence for event in journal.events], [0, 1])
        errors = [
            event for event in seen if event.event_type == domain.EventType.ERROR
        ]
        self.assertEqual(len(errors), 1)
        self.assertFalse(errors[0].persist)
        self.assertEqual(service._sequences[session_id], 2)


class DeleteSessionTests(unittest.TestCase):
    def test_delete_removes_catalog_and_journal(self) -> None:
        journal = _Journal()
        catalog = _Catalog()
        service = ApplicationService(lambda provider: None, catalog, journal)
        info = domain.SessionInfo(
            session_id="s-del",
            name="demo",
            provider=domain.ProviderType.CMD,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.CLOSED,
        )
        catalog.save(info)
        service.delete_session("s-del")
        self.assertNotIn("s-del", catalog.items)
        self.assertEqual(journal.deleted, ["s-del"])


class _DownDriver:
    def __init__(self) -> None:
        self.closed = False

    def set_event_sink(self, sink) -> None:
        return None

    def open(self, config) -> None:
        raise OSError("Нет маршрута до хоста")

    def close(self) -> None:
        self.closed = True


class _UpDriver:
    def set_event_sink(self, sink) -> None:
        return None

    def open(self, config) -> None:
        return None

    def close(self) -> None:
        return None


class OfflineJournalTests(unittest.TestCase):
    def test_unavailable_ssh_keeps_journal_open(self) -> None:
        journal = _Journal()
        past = domain.TranscriptEvent(
            event_id="e-1",
            session_id="s-ssh",
            sequence=0,
            event_type=domain.EventType.COMMAND_SENT,
            timestamp=datetime.now(),
            payload="uptime\n",
        )
        journal.stored.append(past)
        journal.next = 1
        catalog = _Catalog()
        catalog.save(
            domain.SessionInfo(
                session_id="s-ssh",
                name="remote",
                provider=domain.ProviderType.SSH,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.CLOSED,
                ssh=domain.SSHConfig(host="10.0.0.8", username="root"),
            )
        )
        drivers = {"n": 0}

        def factory(_provider):
            drivers["n"] += 1
            return _DownDriver() if drivers["n"] == 1 else _UpDriver()

        service = ApplicationService(factory, catalog, journal)
        with self.assertRaises(SessionUnavailable) as caught:
            service.open_session("s-ssh", "journal-pw", auth_secret="secret")
        self.assertIn("маршрута", str(caught.exception))
        self.assertEqual(service.session_state("s-ssh"), "offline")
        self.assertEqual(service.get_history("s-ssh"), [past])
        self.assertEqual(journal.locked, [])

        info = service.reconnect_session("s-ssh", auth_secret="secret")
        self.assertEqual(info.status, domain.SessionStatus.READY)
        self.assertEqual(service.session_state("s-ssh"), "ready")
        self.assertEqual(service.session_problem("s-ssh"), "")


class RenameTests(unittest.TestCase):
    def test_rename_updates_catalog_and_open_session(self) -> None:
        journal = _Journal()
        catalog = _Catalog()
        service = ApplicationService(lambda _provider: None, catalog, journal)
        info = domain.SessionInfo(
            session_id="s-name",
            name="k8s",
            provider=domain.ProviderType.SSH,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.READY,
            ssh=domain.SSHConfig(host="localhost", port=10022, username="k8s"),
        )
        catalog.save(info)
        service._sessions[info.session_id] = info
        renamed = service.rename_session(info.session_id, "  кластер  ")
        self.assertEqual(renamed.name, "кластер")
        self.assertEqual(service.get_session(info.session_id).name, "кластер")
        self.assertEqual(catalog.get(info.session_id).name, "кластер")
        self.assertEqual(catalog.get(info.session_id).ssh.host, "localhost")
        updated = service.update_session_settings(
            info.session_id,
            "кластер",
            "  пульт  ",
            "  боевой стенд  ",
        )
        self.assertEqual(updated.mcp_alias, "пульт")
        self.assertEqual(updated.mcp_description, "боевой стенд")
        self.assertEqual(catalog.get(info.session_id).mcp_alias, "пульт")
        self.assertEqual(service.get_session(info.session_id).ssh.host, "localhost")
        with self.assertRaises(ValueError):
            service.rename_session(info.session_id, "   ")


class ScreenLogTests(unittest.TestCase):
    def test_alternate_screen_is_not_in_snapshot(self) -> None:
        screen = ScreenLog(20, 4)
        screen.feed("hello\r\nworld")
        screen.feed("\x1b[?1049h")
        screen.feed("nano screen")
        self.assertTrue(screen.in_alternate_screen)
        screen.feed("\x1b[?1049l")
        text = screen.primary_text()
        self.assertIn("hello", text)
        self.assertIn("world", text)
        self.assertNotIn("nano", text)


class PromptUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_history_drafts_and_paste(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from ui.command_card import CommandCard
        from ui.main_window import MainWindow
        from ui.styles import build_text_formats
        from ui.terminal_view import _function_key

        self.assertEqual(_function_key(Qt.Key.Key_F1), "\x1bOP")
        self.assertEqual(_function_key(Qt.Key.Key_F12), "\x1b[24~")

        class FakeService:
            def __init__(self) -> None:
                self.sessions = [
                    domain.SessionInfo(
                        session_id="a",
                        name="A",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                    ),
                    domain.SessionInfo(
                        session_id="b",
                        name="B",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                    ),
                ]
                self.sent = []

            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return list(self.sessions)

            def session_state(self, session_id: str) -> str:
                return "ready"

            def get_session(self, session_id: str):
                for info in self.sessions:
                    if info.session_id == session_id:
                        return info
                return None

            def get_history(self, session_id: str):
                return []

            def send_command(self, session_id: str, text: str) -> str:
                self.sent.append((session_id, text))
                return "c-1"

            def delete_session(self, session_id: str) -> None:
                return None

            def close_session(self, session_id: str) -> None:
                return None

            def complete(self, session_id: str, text: str):
                return []

            def write_input(self, session_id: str, data: str) -> None:
                return None

            def resize(self, session_id: str, cols: int, rows: int) -> None:
                return None

        service = FakeService()
        window = MainWindow(service)
        formats = build_text_formats()
        window._activate_session("a")
        window._add_history_widget(CommandCard("echo one", formats.mono))
        window._add_history_widget(CommandCard("echo two", formats.mono))
        window.input.setText("draft-a")
        window._navigate_history(-1)
        self.assertEqual(window.input.text(), "echo two")
        window._activate_session("b")
        window.input.setText("draft-b")
        window._activate_session("a")
        self.assertEqual(window.input.text(), "echo two")
        window._navigate_history(1)
        self.assertEqual(window.input.text(), "draft-a")

        cards = [
            window.history_layout.itemAt(i).widget()
            for i in range(window.history_layout.count())
        ]
        echo_two = next(card for card in cards if getattr(card, "command", "") == "echo two")
        window._on_card_remove(echo_two)
        self.assertEqual(window._command_history("a"), ["echo one"])

        window._activate_session("b")
        self.assertEqual(window.input.text(), "draft-b")

        QApplication.clipboard().setText("echo pasted")
        window.paste_button.click()
        self.assertIn("echo pasted", window.input.text())

        QApplication.clipboard().setText("echo run")
        window.paste_run_button.click()
        self.assertEqual(service.sent, [("b", "echo run")])

        tip = window.paste_button.toolTip()
        self.assertIn("\n", tip)
        self.assertIn("\n", window.paste_run_button.toolTip())
        self.assertEqual(window.paste_button.text(), "")
        self.assertEqual(window.paste_run_button.text(), "")
        row = window.paste_button.parentWidget().layout()
        widgets = [row.itemAt(i).widget() for i in range(row.count())]
        self.assertLess(widgets.index(window.paste_button), widgets.index(window.paste_run_button))
        self.assertLess(widgets.index(window.paste_run_button), widgets.index(window.input))
        self.assertFalse(window._settings_button.isHidden())
        self.assertEqual(window._settings_button.toolTip(), "Настройки сессии")
        window.close()

    def test_short_command_does_not_keep_empty_screen(self) -> None:
        from PySide6.QtGui import QFontMetrics

        from ui.styles import build_mono_font
        from ui.terminal_view import TerminalView

        font = build_mono_font()
        view = TerminalView(font)
        view.feed("Desktop Documents Downloads\r\n", columns=100, rows=40)
        cell = QFontMetrics(font).height()
        self.assertLess(view.height(), cell * 2)
        view.feed("\x1b[?1049h", columns=100, rows=24)
        self.assertGreater(view.height(), cell * 20)

    def test_offline_session_shows_journal_and_restore(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QLabel

        from ui.main_window import MainWindow

        class FakeService:
            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return [
                    domain.SessionInfo(
                        session_id="a",
                        name="remote",
                        provider=domain.ProviderType.SSH,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.ERROR,
                        ssh=domain.SSHConfig(host="10.0.0.8", username="root"),
                    )
                ]

            def session_state(self, session_id: str) -> str:
                return "offline"

            def session_problem(self, session_id: str) -> str:
                return "Нет маршрута до хоста"

            def get_session(self, session_id: str):
                return self.list_sessions()[0]

            def get_history(self, session_id: str):
                return [
                    domain.TranscriptEvent(
                        event_id="e-1",
                        session_id=session_id,
                        sequence=0,
                        event_type=domain.EventType.COMMAND_SENT,
                        timestamp=datetime.now(),
                        payload="uptime",
                    )
                ]

            def send_command(self, session_id: str, text: str) -> str:
                return "c-1"

            def delete_session(self, session_id: str) -> None:
                return None

            def close_session(self, session_id: str) -> None:
                return None

            def complete(self, session_id: str, text: str):
                return []

            def write_input(self, session_id: str, data: str) -> None:
                return None

            def resize(self, session_id: str, cols: int, rows: int) -> None:
                return None

        window = MainWindow(FakeService())
        window.show()
        window._activate_session("a")
        self.assertTrue(window._offline_banner.isVisible())
        self.assertIn("Нет маршрута до хоста", window._offline_detail.text())
        self.assertEqual(window._restore_button.text(), "Восстановить соединение")
        commands = [
            widget.command
            for i in range(window.history_layout.count())
            if (widget := window.history_layout.itemAt(i).widget()) is not None
            and hasattr(widget, "command")
        ]
        self.assertIn("uptime", commands)
        self.assertNotIn("Система", commands)
        texts = [
            label.text()
            for label in window.history_scroll.findChildren(QLabel)
        ]
        self.assertFalse(any("Сессия закрыта" in text for text in texts))
        session_row = next(
            window.session_list.topLevelItem(index).text(0)
            for index in range(window.session_list.topLevelItemCount())
            if window.session_list.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) == "a"
        )
        self.assertIn("нет соединения", session_row)
        window.close()


class MultilineAndSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_heredoc_stays_open_until_the_marker(self) -> None:
        from ui.command_input import command_needs_more

        open_command = "\n".join(
            [
                "cat <<EOF | sudo tee -a /etc/hosts",
                "192.168.1.10 control-plane",
                "192.168.1.11 worker-1",
            ]
        )
        self.assertTrue(command_needs_more(open_command))
        closed = open_command + "\n192.168.1.12 worker-2\nEOF"
        self.assertFalse(command_needs_more(closed))
        self.assertTrue(command_needs_more('echo "ещё'))
        self.assertFalse(command_needs_more('echo "готово"'))
        self.assertTrue(command_needs_more("echo one \\"))

    def test_enter_sends_a_finished_heredoc(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from ui.main_window import MainWindow

        class FakeService:
            def __init__(self) -> None:
                self.sessions = [
                    domain.SessionInfo(
                        session_id="a",
                        name="A",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                    )
                ]
                self.sent = []

            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return list(self.sessions)

            def session_state(self, session_id: str) -> str:
                return "ready"

            def get_session(self, session_id: str):
                return self.sessions[0]

            def get_history(self, session_id: str):
                return []

            def send_command(self, session_id: str, text: str) -> str:
                self.sent.append(text)
                return "c-1"

            def delete_session(self, session_id: str) -> None:
                return None

            def close_session(self, session_id: str) -> None:
                return None

            def complete(self, session_id: str, text: str):
                return []

            def write_input(self, session_id: str, data: str) -> None:
                return None

            def resize(self, session_id: str, cols: int, rows: int) -> None:
                return None

        service = FakeService()
        window = MainWindow(service)
        window._activate_session("a")
        window.input.setText("cat <<EOF\n192.168.1.10 control-plane")
        QTest.keyClick(window.input, Qt.Key.Key_Return)
        self.assertEqual(service.sent, [])
        self.assertTrue(window.input.text().endswith("\n"))
        finished = "cat <<EOF | sudo tee -a /etc/hosts\n192.168.1.10 control-plane\nEOF"
        window.input.setText(finished)
        QTest.keyClick(window.input, Qt.Key.Key_Return)
        self.assertEqual(service.sent, [finished])
        self.assertEqual(window.input.text(), "")
        window.close()

    def test_journal_output_shrinks_after_the_card_widens(self) -> None:
        from PySide6.QtGui import QFontMetrics, QTextCharFormat
        from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

        from ui.command_card import CommandCard
        from ui.styles import build_text_formats

        formats = build_text_formats()
        host = QWidget()
        layout = QVBoxLayout(host)
        card = CommandCard("ip a", formats.mono)
        layout.addWidget(card)
        line = "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN group default qlen 1000"
        card.append_output(line + "\n    link/loopback\n", QTextCharFormat())
        host.resize(160, 500)
        host.show()
        QApplication.processEvents()
        QApplication.processEvents()
        narrow = card.output.height()
        host.resize(760, 500)
        QApplication.processEvents()
        QApplication.processEvents()
        wide = card.output.height()
        self.assertLess(wide, narrow)
        line_h = QFontMetrics(formats.mono).height()
        self.assertLess(wide, line_h * 4)
        doc = card.output.document()
        block = doc.lastBlock()
        if block.text() == "" and block.previous().isValid():
            block = block.previous()
        bottom = doc.documentLayout().blockBoundingRect(block).bottom()
        self.assertGreaterEqual(card.output.viewport().height(), int(bottom))
        self.assertEqual(card.output.verticalScrollBar().value(), 0)
        host.close()

    def test_wheel_scrolls_the_feed_not_the_output(self) -> None:
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QTextCharFormat, QWheelEvent
        from PySide6.QtWidgets import QApplication, QScrollArea

        from ui.command_card import CommandCard
        from ui.styles import build_text_formats

        formats = build_text_formats()
        scroll = QScrollArea()
        card = CommandCard("ip a", formats.mono)
        scroll.setWidget(card)
        scroll.setWidgetResizable(True)
        text = "\n".join(f"строка {i} достаточно длинная" for i in range(12)) + "\n"
        card.append_output(text, QTextCharFormat())
        scroll.resize(420, 160)
        scroll.show()
        QApplication.processEvents()
        QApplication.processEvents()
        before = card.output.verticalScrollBar().value()
        feed = scroll.verticalScrollBar().value()
        event = QWheelEvent(
            QPoint(20, 20),
            card.output.viewport().mapToGlobal(QPoint(20, 20)),
            QPoint(0, 0),
            QPoint(0, -120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(card.output.viewport(), event)
        QApplication.processEvents()
        self.assertEqual(card.output.verticalScrollBar().value(), before)
        self.assertGreater(scroll.verticalScrollBar().value(), feed)
        scroll.close()

    def test_terminal_selection_copies_cells(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.styles import build_mono_font
        from ui.terminal_view import TerminalView

        view = TerminalView(build_mono_font())
        view.resize(420, 160)
        view.show()
        view.feed("\x1b[?1049h", columns=40, rows=8)
        view.feed("nano hosts\r\n192.168.1.10 control-plane")
        view._anchor = (1, 0)
        view._caret = (1, 12)
        self.assertEqual(view.selected_text(), "192.168.1.10")
        view._copy_selection()
        self.assertEqual(QApplication.clipboard().text(), "192.168.1.10")
        view.close()


if __name__ == "__main__":
    unittest.main()
