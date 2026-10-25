# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""sudo на SSH: пароль не сохраняется на клиенте, конец команды прячется."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.ssh import SSHSessionDriver, SudoFence, _SUDO_BROKER


class SudoTicketTests(unittest.TestCase):
    def test_password_is_not_written_into_the_command(self) -> None:
        driver = SSHSessionDriver(host_key_verifier=None, known_hosts_path="")
        script = driver._build_remote_script(
            "sudo tee -a /etc/hosts", "/tmp/vibe-cwd"
        )
        self.assertIn("sudo tee -a /etc/hosts", script)
        self.assertNotIn("SUDO_ASKPASS", script)
        self.assertNotIn("mktemp", script)
        self.assertNotIn("password", script.lower())
        self.assertNotIn("ASKPASS", _SUDO_BROKER)
        self.assertNotIn("mktemp", _SUDO_BROKER)

    def test_marker_is_hidden_and_exit_code_is_kept(self) -> None:
        fence = SudoFence("abc")
        self.assertEqual(fence.feed("пароль принят"), "пароль принят")
        visible = fence.feed("\r\n__VIBE_DONE__abc__0__\r\n")
        self.assertEqual(visible, "")
        self.assertEqual(fence.exit_code, 0)
        self.assertEqual(fence.feed("ещё"), "")

    def test_prompt_is_shown_before_the_next_key(self) -> None:
        fence = SudoFence("abc")
        prompt = "New password: "
        self.assertEqual(fence.feed(prompt), prompt)
        self.assertEqual(fence.feed("a"), "a")

    def test_partial_marker_stays_hidden(self) -> None:
        fence = SudoFence("abc")
        self.assertEqual(fence.feed("ok\r\n__VIBE"), "ok\r\n")
        self.assertEqual(fence.feed("_DONE__abc__0__\r\n"), "")
        self.assertEqual(fence.exit_code, 0)

    def test_split_marker_waits_for_the_code(self) -> None:
        fence = SudoFence("abc")
        self.assertEqual(fence.feed("ok\r\n__VIBE_DONE__abc__"), "ok")
        visible = fence.feed("15__\r\n")
        self.assertEqual(visible, "")
        self.assertEqual(fence.exit_code, 15)


if __name__ == "__main__":
    unittest.main()
