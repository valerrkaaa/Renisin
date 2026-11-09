# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Команды SSH на Windows не оборачиваются в синтаксис bash."""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from drivers.ssh import (
    SSHSessionDriver,
    _CwdMark,
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


class WindowsLaunchTests(unittest.TestCase):
    def _driver(self) -> SSHSessionDriver:
        driver = SSHSessionDriver(host_key_verifier=None, known_hosts_path="")
        driver._remote_shell = "cmd"
        driver._win_temp = r"C:\no\such\dir"
        return driver

    def test_marker_is_removed_from_the_output(self) -> None:
        mark = _CwdMark()
        visible = mark.feed("hello\r\n__VIBE_CWD__C:\\Windows\r\n")
        self.assertEqual(visible, "hello\r\n")
        self.assertEqual(mark.flush(), "")
        self.assertEqual(mark.cwd, r"C:\Windows")

    def test_launch_ignores_a_missing_temp_directory(self) -> None:
        driver = self._driver()
        driver._cwd = "/home/user"
        command = driver._windows_launch("echo hello-from-vibe", "tag1")
        self.assertNotIn('"', command)
        self.assertIn("-EncodedCommand", command)
        script = base64.b64decode(command.split()[-1]).decode("utf-16-le")
        bat = _hosted_bat(script)
        self.assertIn("echo hello-from-vibe", bat)
        self.assertNotIn(r"C:\no\such\dir", script)
        self.assertNotIn("/home/user", bat)

        space = Path(tempfile.mkdtemp(prefix="vibe space "))
        env = os.environ.copy()
        env["TEMP"] = str(space)
        env["TMP"] = str(space)
        try:
            completed = _run_windows(command, env)
            text = _output_text(completed)
            leftover = list(space.iterdir())
        finally:
            shutil.rmtree(space, ignore_errors=True)
        self.assertNotIn("The system cannot find the path specified", text)
        self.assertIn("hello-from-vibe", text)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("__VIBE_CWD__", text)
        self.assertEqual(leftover, [])

    def test_next_command_starts_in_the_reported_directory(self) -> None:
        driver = self._driver()
        first = driver._windows_launch("cd /d C:\\Windows", "tag2")
        completed = _run_windows(first, os.environ.copy())
        text = _output_text(completed)
        self.assertEqual(completed.returncode, 0, text)
        marker = text.rfind("__VIBE_CWD__")
        self.assertGreaterEqual(marker, 0, text)
        reported = text[marker + len("__VIBE_CWD__"):].splitlines()[0].strip()
        self.assertEqual(reported.upper(), r"C:\WINDOWS")
        driver._cwd = reported
        second = driver._windows_launch("cd", "tag3")
        script = base64.b64decode(second.split()[-1]).decode("utf-16-le")
        self.assertIn('@cd /d "' + reported + '"', _hosted_bat(script))
        again = _run_windows(second, os.environ.copy())
        body = _output_text(again).split("__VIBE_CWD__")[0]
        self.assertIn(r"C:\Windows", body)

    def test_failed_command_keeps_its_exit_code(self) -> None:
        driver = self._driver()
        command = driver._windows_launch("exit /b 7", "tag4")
        completed = _run_windows(command, os.environ.copy())
        text = _output_text(completed)
        self.assertNotIn("The system cannot find the path specified", text)
        self.assertEqual(completed.returncode, 7, text)


def _hosted_bat(script: str) -> str:
    marker = "FromBase64String('"
    start = script.index(marker) + len(marker)
    end = script.index("')", start)
    return base64.b64decode(script[start:end]).decode("utf-8")


def _run_windows(command: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        capture_output=True,
        env=env,
        timeout=60,
    )


def _output_text(completed: subprocess.CompletedProcess) -> str:
    return (
        completed.stdout.decode("utf-8", errors="replace")
        + completed.stderr.decode("utf-8", errors="replace")
    )


if __name__ == "__main__":
    unittest.main()
