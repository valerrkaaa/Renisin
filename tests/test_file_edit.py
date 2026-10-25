# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Точечная замена: остальные байты файла остаются на месте."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import file_edit
from ui.chat_pane import parse_edit_block


class PrepareEditTests(unittest.TestCase):
    def test_replaces_only_the_unique_span(self) -> None:
        data = b"\x00\xffalpha\r\nkeep me\r\nomega\x01"
        view, updated = file_edit.prepare_edit(data, "keep me", "kept")
        self.assertTrue(view.ok)
        self.assertEqual(updated, b"\x00\xffalpha\r\nkept\r\nomega\x01")

    def test_refuses_when_fragment_is_missing_or_repeated(self) -> None:
        missing, updated = file_edit.prepare_edit(b"abc", "zzz", "q")
        self.assertFalse(missing.ok)
        self.assertIsNone(updated)
        repeated, updated = file_edit.prepare_edit(b"keep keep", "keep", "kept")
        self.assertFalse(repeated.ok)
        self.assertIsNone(updated)
        self.assertIn("2", repeated.message)

    def test_atomic_write_keeps_original_on_success_and_surroundings(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "note.txt")
            original = "шапка\r\ntimeout = 5\r\nхвост\r\n".encode("utf-8")
            Path(path).write_bytes(original)
            _view, updated = file_edit.prepare_edit(original, "timeout = 5", "timeout = 15")
            assert updated is not None
            file_edit.write_bytes_atomic(path, updated)
            self.assertEqual(Path(path).read_bytes(), updated)
            self.assertTrue(updated.startswith("шапка\r\n".encode("utf-8")))
            self.assertTrue(updated.endswith("\r\nхвост\r\n".encode("utf-8")))


class ParserTests(unittest.TestCase):
    def test_accepts_a_loose_block_from_a_small_model(self) -> None:
        parsed = parse_edit_block(
            "\n".join(
                [
                    "path: \"src/app.py\"",
                    "old: |",
                    "    timeout = 5",
                    "NEW:",
                    "    timeout = 15",
                ]
            )
        )
        self.assertEqual(
            parsed,
            ("src/app.py", "    timeout = 5", "    timeout = 15"),
        )

    def test_rejects_a_block_without_the_new_text(self) -> None:
        self.assertIsNone(parse_edit_block("path: a.txt\nOLD:\nhello\n"))


class LocalDriverTests(unittest.TestCase):
    def test_cmd_driver_rewrites_one_span(self) -> None:
        from drivers.cmd import CmdSessionDriver

        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "note.txt")
            Path(path).write_bytes(b"head\nVALUE\ntail")
            driver = CmdSessionDriver()
            driver._session_id = "s"
            driver._cwd = folder
            driver._closed = False
            view = driver.preview_edit("s", "note.txt", "VALUE", "NEW")
            self.assertTrue(view.ok)
            message = driver.apply_edit("s", "note.txt", "VALUE", "NEW")
            self.assertIn("Заменено", message)
            self.assertEqual(Path(path).read_bytes(), b"head\nNEW\ntail")
            refused = driver.preview_edit("s", "note.txt", "nope", "x")
            self.assertFalse(refused.ok)
            self.assertEqual(Path(path).read_bytes(), b"head\nNEW\ntail")
