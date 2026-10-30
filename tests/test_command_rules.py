# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Шаблоны автоодобрения: хранилище, быстрый префикс и подсветка части строки."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.command_rules import (
    command_spans,
    fully_allowed,
    rule_set,
    try_match,
)
from application.terminal_mcp import CommandAnswer
from storage.command_rules import FileCommandRuleStore
from tests.test_mcp_command import _Gate, _session, _world
from ui.chat_pane import command_rich_text


class PatternTests(unittest.TestCase):
    def test_star_is_a_prefix_and_does_not_match_a_neighbor(self) -> None:
        rules = rule_set(["grep *", "^ls$"])
        self.assertEqual(rules.prefixes, (("grep", True),))
        self.assertTrue(rules.matches("grep"))
        self.assertTrue(rules.matches("grep -n file"))
        self.assertFalse(rules.matches("grepfoo"))
        self.assertFalse(rules.matches("egrep -n file"))
        self.assertTrue(rules.matches("ls"))
        self.assertFalse(rules.matches("ls -l"))
        self.assertEqual(try_match("grep *", "grep -n file"), "match")
        self.assertEqual(try_match("grep *", "egrep"), "miss")
        self.assertEqual(try_match("(", "grep"), "invalid")

    def test_highlight_stops_at_operators(self) -> None:
        rules = rule_set(["cmd1", "cmd4*"])
        spans = command_spans(
            'echo "a && b"; cmd1 && cmd2 || cmd3; cmd4 | grep x',
            "unix",
            rules,
        )
        commands = [
            (span.text, span.matched)
            for line in spans
            for span in line
            if span.command
        ]
        self.assertEqual(
            commands,
            [
                ('echo "a && b"', False),
                ("cmd1", True),
                ("cmd2", False),
                ("cmd3", False),
                ("cmd4", True),
                ("grep x", False),
            ],
        )
        self.assertFalse(fully_allowed(spans))
        only = command_spans("cmd1 && cmd1", "unix", rules)
        self.assertTrue(fully_allowed(only))
        whole = command_spans("cmd1 && cmd2", "other", rules)
        self.assertEqual(
            [(span.text, span.matched, span.command) for span in whole[0]],
            [("cmd1 && cmd2", False, True)],
        )

    def test_rich_text_colors_only_the_matching_command(self) -> None:
        rules = rule_set(["cmd1"])
        spans = command_spans("cmd1 && cmd2 | cmd3", "unix", rules)
        html = command_rich_text("cmd1 && cmd2 | cmd3", ("cmd1 && cmd2 | cmd3",), spans)
        assert html is not None
        self.assertIn('<font color="#86efac">cmd1</font>', html)
        self.assertNotIn('<font color="#86efac">cmd2</font>', html)
        self.assertIn("&amp;&amp;", html)
        self.assertIsNone(command_rich_text("ls", ("ls",), ()))

    def test_store_keeps_lists_apart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = FileCommandRuleStore(os.path.join(folder, "command_rules.json"))
            store.add("unix", "grep *")
            store.add(domain.SHELL_WINDOWS, "dir *")
            again = FileCommandRuleStore(os.path.join(folder, "command_rules.json"))
            self.assertEqual(again.patterns("unix"), ("grep *",))
            self.assertEqual(again.patterns("windows"), ("dir *",))
            self.assertEqual(again.patterns("other"), ())
            self.assertTrue(again.rule_set_for("unix").matches("grep -n a"))
            self.assertFalse(again.rule_set_for("windows").matches("grep -n a"))
            with self.assertRaises(ValueError):
                store.add("unix", "(")
            store.remove("unix", "grep *")
            self.assertEqual(store.patterns("unix"), ())


class AutoApproveTests(unittest.TestCase):
    def test_full_match_runs_and_partial_match_is_marked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            rules = FileCommandRuleStore(os.path.join(folder, "command_rules.json"))
            rules.add("unix", "grep *")
            rules.add("unix", "ls")
            service, driver, profiles = _world()
            service._command_rules = rules
            info = _session(service, profiles)
            gate = _Gate(CommandAnswer("run"))
            service.set_command_approver(gate.ask)

            service.run_mcp_command(info.session_id, "найти", "grep -n foo")
            self.assertEqual(len(gate.asks), 1)
            self.assertTrue(fully_allowed(gate.asks[0].spans))
            self.assertEqual(driver.sent[-1], "grep -n foo")

            service.run_mcp_command(
                info.session_id, "смешать", "grep -n foo && rm x"
            )
            self.assertEqual(len(gate.asks), 2)
            self.assertEqual(gate.asks[1].command, "grep -n foo && rm x")
            marked = [
                (span.text, span.matched)
                for line in gate.asks[1].spans
                for span in line
                if span.command
            ]
            self.assertEqual(marked, [("grep -n foo", True), ("rm x", False)])
            self.assertEqual(driver.sent[-1], "grep -n foo && rm x")
