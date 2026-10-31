# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Группы подключений: порядок, вложенность и свёрнутость."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import domain
from application.service import ApplicationService
from storage.catalog import FileSessionCatalog


class _Journal:
    def __init__(self) -> None:
        self.unlocked = set()
        self.next = 0

    def next_sequence(self, session_id: str) -> int:
        return self.next

    def create(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def delete(self, session_id: str) -> None:
        return None

    def lock(self, session_id: str) -> None:
        self.unlocked.discard(session_id)

    def unlock(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def is_unlocked(self, session_id: str) -> bool:
        return session_id in self.unlocked

    def get_by_session(self, session_id: str):
        return []


class _Driver:
    def set_event_sink(self, sink) -> None:
        self.sink = sink

    def open(self, config):
        return config.session_id

    def send(self, request) -> None:
        self.sink(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_FINISHED,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload="",
            return_code=0,
        ))

    def close(self) -> None:
        return None


def _service(catalog):
    return ApplicationService(lambda _provider: _Driver(), catalog, _Journal())


class LayoutStoreTests(unittest.TestCase):
    def test_layout_survives_a_session_save(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sessions.json")
            catalog = FileSessionCatalog(path)
            service = _service(catalog)
            group = service.create_connection_group("работа")
            nested = service.create_connection_group("внутренняя", group.group_id)
            service.set_connection_group_expanded(group.group_id, True)
            info = service.create_session("консоль", "pw")
            service.move_connection_node(info.connection_id, nested.group_id, 0)
            again = FileSessionCatalog(path)
            layout = again.load_layout()
            self.assertEqual(layout.group(group.group_id).name, "работа")
            self.assertTrue(layout.group(group.group_id).expanded)
            self.assertFalse(layout.group(nested.group_id).expanded)
            self.assertEqual(layout.children_of(nested.group_id), (info.connection_id,))
            self.assertEqual(layout.children_of(info.connection_id), (info.session_id,))
            catalog.save(service.get_session(info.session_id))
            self.assertEqual(
                FileSessionCatalog(path).load_layout().children_of(nested.group_id),
                (info.connection_id,),
            )

    def test_move_reorder_and_refuse_a_cycle(self) -> None:
        catalog = FileSessionCatalog(os.path.join(tempfile.mkdtemp(), "sessions.json"))
        service = _service(catalog)
        first = service.create_connection_group("первая")
        second = service.create_connection_group("вторая")
        inner = service.create_connection_group("внутри", first.group_id)
        service.move_connection_node(second.group_id, "", 0)
        self.assertEqual(
            service.connection_layout().children_of("")[:2],
            (second.group_id, first.group_id),
        )
        with self.assertRaises(ValueError):
            service.move_connection_node(first.group_id, inner.group_id, 0)
        self.assertEqual(
            service.connection_layout().group(first.group_id).parent_id,
            "",
        )
        info = service.create_session("консоль", "pw")
        service.move_connection_node(info.connection_id, first.group_id, 0)
        with self.assertRaises(ValueError):
            service.move_connection_node(info.session_id, first.group_id, 0)
        service.delete_connection_group(first.group_id)
        root = service.connection_layout().children_of("")
        self.assertIn(info.connection_id, root)
        self.assertEqual(
            service.connection_layout().children_of(info.connection_id),
            (info.session_id,),
        )
        self.assertIn(inner.group_id, root)
        self.assertIsNone(service.connection_layout().group(first.group_id))
        self.assertEqual(
            service.connection_layout().group(inner.group_id).parent_id,
            "",
        )


    def test_old_session_record_becomes_a_connection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "sessions.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "sessions": {
                            "s-old": {
                                "session_id": "s-old",
                                "name": "консоль",
                                "provider": "cmd",
                                "identity": "current_user",
                                "status": "closed",
                                "llm_profile_id": "m-1",
                                "shell_family": "windows",
                            }
                        },
                        "layout": {"groups": {}, "order": {"": ["s-old"]}},
                    },
                    handle,
                )
            catalog = FileSessionCatalog(path)
            loaded = catalog.get("s-old")
            assert loaded is not None
            self.assertEqual(loaded.connection_id, "k-old")
            self.assertEqual(loaded.llm_profile_id, "m-1")
            self.assertEqual(loaded.shell_family, domain.SHELL_WINDOWS)
            connection = catalog.get_connection("k-old")
            assert connection is not None
            self.assertEqual(connection.name, "консоль")
            self.assertEqual(connection.provider, domain.ProviderType.CMD)
            self.assertEqual(catalog.load_layout().children_of(""), ("k-old",))
            self.assertEqual(catalog.load_layout().children_of("k-old"), ("s-old",))

    def test_second_session_uses_the_same_connection(self) -> None:
        catalog = FileSessionCatalog(os.path.join(tempfile.mkdtemp(), "sessions.json"))
        service = _service(catalog)
        first = service.create_session("консоль", "pw", shell_family=domain.SHELL_WINDOWS)
        second = service.add_session(first.connection_id, "вторая")
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(second.connection_id, first.connection_id)
        self.assertEqual(second.shell_family, domain.SHELL_WINDOWS)
        self.assertEqual(second.provider, domain.ProviderType.CMD)
        self.assertIsNone(second.llm_profile_id)
        children = service.connection_layout().children_of(first.connection_id)
        self.assertEqual(children, (first.session_id, second.session_id))
        self.assertEqual(service.mcp_label(second), "консоль / вторая")
        service.delete_connection(first.connection_id)
        self.assertIsNone(service.get_session(first.session_id))
        self.assertIsNone(service.get_session(second.session_id))
        self.assertIsNone(service.get_connection(first.connection_id))


class TreeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_new_group_is_collapsed_and_holds_a_session(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from ui.main_window import MainWindow

        folder = tempfile.mkdtemp()
        service = _service(FileSessionCatalog(os.path.join(folder, "sessions.json")))
        info = service.create_session("консоль", "pw")
        group = service.create_connection_group("папка")
        service.move_connection_node(info.connection_id, group.group_id, 0)
        window = MainWindow(service)
        window.show()
        QApplication.processEvents()
        tree = window.session_list
        self.assertEqual(tree.topLevelItem(0).text(0), "общий чат для ИИ")
        folder_item = tree.topLevelItem(2)
        self.assertEqual(folder_item.text(0), "папка")
        self.assertFalse(folder_item.isExpanded())
        fold = window.session_list.itemWidget(folder_item, 0).findChild(
            __import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton,
            "sessionFold",
        )
        self.assertIsNotNone(fold)
        self.assertFalse(fold.icon().isNull())
        connection_item = folder_item.child(0)
        self.assertFalse(connection_item.isExpanded())
        connection_row = window.session_list.itemWidget(connection_item, 0)
        self.assertIsNotNone(connection_row.findChild(
            __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
            "connectionMark",
        ))
        self.assertIsNotNone(window.session_list.itemWidget(folder_item, 0).findChild(
            __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
            "folderMark",
        ))
        self.assertEqual(
            connection_item.data(0, Qt.ItemDataRole.UserRole),
            info.connection_id,
        )
        session_item = connection_item.child(0)
        self.assertEqual(
            session_item.data(0, Qt.ItemDataRole.UserRole),
            info.session_id,
        )
        session_row = window.session_list.itemWidget(session_item, 0)
        self.assertGreaterEqual(
            session_row.layout().contentsMargins().left(),
            connection_row.layout().contentsMargins().left() + 19,
        )
        window.close()

    def test_journals_are_collapsed_on_startup(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from ui.main_window import MainWindow

        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "sessions.json")
        service = _service(FileSessionCatalog(path))
        info = service.create_session("консоль", "pw")
        group = service.create_connection_group("серверы")
        service.set_connection_group_expanded(group.group_id, True)
        service.set_connection_expanded(info.connection_id, True)
        restarted = _service(FileSessionCatalog(path))
        self.assertFalse(restarted.get_connection(info.connection_id).expanded)
        saved_group = restarted.connection_layout().group(group.group_id)
        assert saved_group is not None
        self.assertFalse(saved_group.expanded)
        window = MainWindow(restarted)
        window.show()
        QApplication.processEvents()
        connection_item = None
        group_item = None
        for index in range(window.session_list.topLevelItemCount()):
            item = window.session_list.topLevelItem(index)
            node = item.data(0, Qt.ItemDataRole.UserRole)
            if node == info.connection_id:
                connection_item = item
            elif node == group.group_id:
                group_item = item
        self.assertIsNotNone(connection_item)
        self.assertFalse(connection_item.isExpanded())
        self.assertIsNotNone(group_item)
        self.assertFalse(group_item.isExpanded())
        window.close()

    def test_delete_is_a_context_menu_item(self) -> None:
        from PySide6.QtWidgets import QPushButton

        from ui.main_window import _SessionRow

        session = _SessionRow(
            "s",
            "длинное имя подключения в списке сессий",
            active=False,
            muted=False,
        )
        self.assertEqual(session.findChildren(QPushButton, "sessionDelete"), [])
        self.assertIn("Удалить", [title for title, _callback in session._menu_entries()])
        folder = _SessionRow("g", "папка", active=False, muted=False, kind="group", deletable=False)
        self.assertIsNotNone(folder.findChild(
            __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
            "folderMark",
        ))
        self.assertIn("Удалить", [title for title, _callback in folder._menu_entries()])
        connection = _SessionRow(
            "k", "хост", active=False, muted=False, kind="connection"
        )
        self.assertIsNotNone(connection.findChild(
            __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel,
            "connectionMark",
        ))
        self.assertIn("Удалить", [title for title, _callback in connection._menu_entries()])
