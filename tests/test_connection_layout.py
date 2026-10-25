# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Группы подключений: порядок, вложенность и свёрнутость."""
from __future__ import annotations

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
            service.move_connection_node(info.session_id, nested.group_id, 0)
            again = FileSessionCatalog(path)
            layout = again.load_layout()
            self.assertEqual(layout.group(group.group_id).name, "работа")
            self.assertTrue(layout.group(group.group_id).expanded)
            self.assertFalse(layout.group(nested.group_id).expanded)
            self.assertEqual(layout.children_of(nested.group_id), (info.session_id,))
            catalog.save(service.get_session(info.session_id))
            self.assertEqual(
                FileSessionCatalog(path).load_layout().children_of(nested.group_id),
                (info.session_id,),
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
        service.move_connection_node(info.session_id, first.group_id, 0)
        service.delete_connection_group(first.group_id)
        root = service.connection_layout().children_of("")
        self.assertIn(info.session_id, root)
        self.assertIn(inner.group_id, root)
        self.assertIsNone(service.connection_layout().group(first.group_id))
        self.assertEqual(
            service.connection_layout().group(inner.group_id).parent_id,
            "",
        )


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
        service.move_connection_node(info.session_id, group.group_id, 0)
        window = MainWindow(service)
        window.show()
        QApplication.processEvents()
        tree = window.session_list
        self.assertEqual(tree.topLevelItem(0).text(0), "общий чат для ИИ")
        folder_item = tree.topLevelItem(1)
        self.assertEqual(folder_item.text(0), "папка")
        self.assertFalse(folder_item.isExpanded())
        fold = window.session_list.itemWidget(folder_item, 0).findChild(
            __import__("PySide6.QtWidgets", fromlist=["QPushButton"]).QPushButton,
            "sessionFold",
        )
        self.assertIsNotNone(fold)
        self.assertEqual(fold.text(), "▸")
        self.assertEqual(
            folder_item.child(0).data(0, Qt.ItemDataRole.UserRole),
            info.session_id,
        )
        window.close()
