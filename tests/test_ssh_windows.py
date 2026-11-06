# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Команды SSH на Windows не оборачиваются в синтаксис bash."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.ssh import (
    SSHSessionDriver,
    _present_completion,
    _windows_temp_dir,
    assemble_windows_completions,
    classify_remote_shell,
    windows_completion_target,
)


class WindowsRemoteScriptTests(unittest.TestCase):
    def _driver(self) -> SSHSessionDriver:
        return SSHSessionDriver(host_key_verifier=None, known_hosts_path="")

    def test_unix_wrapper_still_exports_term(self) -> None:
        script = self._driver()._build_remote_script("ls", "/tmp/cwd")
        self.assertIn("export TERM=${TERM:-xterm-256color}", script)
        self.assertIn("ls", script)

    def test_cmd_wrapper_has_no_unix_syntax(self) -> None:
        driver = self._driver()
        driver._remote_shell = "cmd"
        driver._cwd = r"C:\Work"
        script = driver._build_remote_script("dir", r"C:\Temp\cwd.txt")
        self.assertNotIn("export", script)
        self.assertNotIn("pwd", script)
        self.assertNotIn("$?", script)
        self.assertIn("\r\ndir\r\n", script)
        self.assertNotIn("\ufeff", script)
        self.assertNotIn("я╗┐", script)
        self.assertIn('@cd /d "C:\\Work"', script)
        self.assertIn("@set __vibe_ec=%ERRORLEVEL%", script)
        self.assertIn("@exit /b %__vibe_ec%", script)
        self.assertIn('@cd > "C:\\Temp\\cwd.txt"', script)
        self.assertTrue(script.startswith("@echo off\r\n"))

    def test_powershell_wrapper_has_no_export(self) -> None:
        driver = self._driver()
        driver._remote_shell = "powershell"
        driver._cwd = r"C:\Work"
        script = driver._build_powershell_script("Get-ChildItem", r"C:\Temp\cwd.txt")
        self.assertNotIn("export", script)
        self.assertIn("Get-ChildItem", script)
        self.assertIn("Set-Location -LiteralPath 'C:\\Work'", script)
        self.assertIn("exit $__vibe_ec", script)

    def test_shell_is_classified_from_the_probe(self) -> None:
        self.assertEqual(classify_remote_shell("Windows_NT\r\n"), "cmd")
        self.assertEqual(classify_remote_shell("%OS%\n", "Windows_NT"), "powershell")
        self.assertEqual(classify_remote_shell("%OS%\n", "$env:OS\n"), "unix")

    def test_temp_directory_is_the_last_windows_path(self) -> None:
        self.assertEqual(
            _windows_temp_dir("noise\r\nC:\\Users\\a\\AppData\\Local\\Temp\r\n"),
            r"C:\Users\a\AppData\Local\Temp",
        )
        self.assertEqual(_windows_temp_dir("%TEMP%"), r"C:\Windows\Temp")

    def test_tab_target_is_a_command_or_a_path(self) -> None:
        self.assertEqual(
            windows_completion_target("di"),
            ("command", "di", "", "di", "\\"),
        )
        self.assertEqual(
            windows_completion_target("dir C:\\Win"),
            ("path", "C:\\Win", "C:\\", "Win", "\\"),
        )
        self.assertEqual(
            windows_completion_target("echo hi&ip"),
            ("command", "ip", "", "ip", "\\"),
        )
        self.assertEqual(
            windows_completion_target('type .\\Re'),
            ("path", ".\\Re", ".\\", "Re", "\\"),
        )

    def test_remote_lines_become_commands_and_paths(self) -> None:
        commands = assemble_windows_completions(
            "command", "", "\\", "ipconfig\r\ndir\r\nipconfig\r\n"
        )
        self.assertEqual(commands, ["dir", "ipconfig"])
        paths = assemble_windows_completions(
            "path", "C:\\", "\\", "D\tWindows\r\nF\twin.ini\r\nD\t.\r\n"
        )
        self.assertEqual(paths, ["C:\\win.ini", "C:\\Windows\\"])

    def test_windows_completion_quotes_spaces_for_cmd(self) -> None:
        self.assertEqual(
            _present_completion("cmd", "C:\\Program Files\\", ""),
            '"C:\\Program Files\\"',
        )
        self.assertEqual(_present_completion("cmd", "C:\\Windows\\", ""), "C:\\Windows\\")

    def test_windows_driver_does_not_ask_bash(self) -> None:
        driver = self._driver()
        driver._remote_shell = "cmd"
        driver._session_id = "s"
        driver._client = object()
        seen = {}

        def read_exec(_client, command, timeout=5):
            seen["command"] = command
            seen["timeout"] = timeout
            return "dir\r\nipconfig\r\n"

        driver._read_exec = read_exec
        found = driver.complete("s", "di")
        self.assertEqual(found, ["dir", "ipconfig"])
        self.assertIn("powershell.exe", seen["command"])
        self.assertNotIn("bash -c", seen["command"])


if __name__ == "__main__":
    unittest.main()
