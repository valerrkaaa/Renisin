# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Оболочка внутри консоли переключает строку ввода, пароль и nano — нет."""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.service import ApplicationService
from shell_takeover import (
    PromptWatch,
    is_exit_command,
    looks_like_shell_prompt,
    opens_shell,
    prompt_label,
    wrap_command,
)


class OpenShellTests(unittest.TestCase):
    def test_ssh_and_user_switch_take_the_input_line(self) -> None:
        self.assertTrue(opens_shell("ssh user@host"))
        self.assertTrue(opens_shell("ssh -p 2200 user@host"))
        self.assertTrue(opens_shell("su -"))
        self.assertTrue(opens_shell("sudo -i"))
        self.assertTrue(opens_shell("sudo su -"))
        self.assertTrue(opens_shell("sudo -u root bash"))
        self.assertTrue(opens_shell("bash"))
        self.assertTrue(opens_shell("docker exec -it box bash"))

    def test_one_shot_commands_stay_in_the_card(self) -> None:
        self.assertFalse(opens_shell("ssh host ls"))
        self.assertFalse(opens_shell("sudo apt update"))
        self.assertFalse(opens_shell("bash -c ls"))
        self.assertFalse(opens_shell("su -c id"))
        self.assertFalse(opens_shell("nano /etc/hosts"))
        self.assertFalse(opens_shell("ls"))

    def test_exit_returns_to_the_previous_shell(self) -> None:
        self.assertTrue(is_exit_command("exit"))
        self.assertTrue(is_exit_command("exit 1"))
        self.assertTrue(is_exit_command("logout"))
        self.assertFalse(is_exit_command("exited"))
        self.assertFalse(is_exit_command("exit now"))

    def test_prompt_is_not_a_password(self) -> None:
        self.assertTrue(looks_like_shell_prompt("user@host:~$"))
        self.assertTrue(looks_like_shell_prompt("root@host:~#"))
        self.assertTrue(looks_like_shell_prompt("#"))
        self.assertFalse(looks_like_shell_prompt("[sudo] password for user:"))
        self.assertFalse(looks_like_shell_prompt("New password:"))
        self.assertEqual(prompt_label("user@host:~/src$"), "user@host")
        self.assertEqual(prompt_label("#"), "root")

    def test_wrapper_keeps_the_marker_on_the_same_line(self) -> None:
        wrapped = wrap_command("nano file", "abc")
        self.assertTrue(wrapped.startswith("nano file;"))
        self.assertIn("__VIBE_DONE__abc__", wrapped)
        self.assertTrue(wrapped.endswith("\n"))

    def test_prompt_waits_until_output_is_idle(self) -> None:
        watch = PromptWatch(idle_s=0.05)
        self.assertFalse(watch.ready("user@host:~$", False))
        watch.mark()
        self.assertFalse(watch.ready("user@host:~$", False))
        time.sleep(0.06)
        self.assertTrue(watch.ready("user@host:~$", False))
        self.assertFalse(watch.ready("user@host:~$", True))
        self.assertFalse(watch.ready("Password:", False))


class _Journal:
    def __init__(self) -> None:
        self.next = 0

    def append(self, event) -> None:
        self.next += 1

    def next_sequence(self, session_id: str) -> int:
        return self.next

    def is_unlocked(self, session_id: str) -> bool:
        return True


class ShellLabelTests(unittest.TestCase):
    def test_context_survives_the_finished_command(self) -> None:
        service = ApplicationService(lambda provider: None, None, _Journal())
        service._handle_driver_event(domain.OutputEvent(
            session_id="s",
            event_type=domain.EventType.SHELL_CONTEXT,
            timestamp=datetime.now(),
            payload="user@host",
            persist=False,
        ))
        self.assertEqual(service.shell_label("s"), "user@host")
        service._handle_driver_event(domain.OutputEvent(
            session_id="s",
            event_type=domain.EventType.COMMAND_FINISHED,
            timestamp=datetime.now(),
            payload="",
            persist=False,
        ))
        self.assertEqual(service.shell_label("s"), "user@host")
        service._handle_driver_event(domain.OutputEvent(
            session_id="s",
            event_type=domain.EventType.SHELL_CONTEXT,
            timestamp=datetime.now(),
            payload="",
            persist=False,
        ))
        self.assertEqual(service.shell_label("s"), "")


if __name__ == "__main__":
    unittest.main()
