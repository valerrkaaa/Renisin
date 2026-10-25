# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Когда команда становится оболочкой, а не одноразовым процессом.

Пароль, подтверждение ключа и nano остаются вводом в карточке ответа.
ssh, su, sudo -i и похожие команды после приглашения оболочки отдают
следующие команды нижней строке.
"""
from __future__ import annotations

import os
import re
import shlex
import time


_SHELLS = {
    "bash", "sh", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "ash", "login",
}
_SSH_WITH_ARG = {
    "-p", "-l", "-i", "-o", "-F", "-J", "-b", "-c", "-D", "-E", "-e",
    "-I", "-L", "-R", "-S", "-W", "-w", "-O", "-B",
}
_SECRET = re.compile(
    r"(password|passphrase|passcode|пароль)\b.*:?\s*$",
    re.IGNORECASE,
)


class PromptWatch:
    """Приглашение оболочки считается готовым, когда вывод затих."""

    def __init__(self, idle_s: float = 0.45) -> None:
        self._idle_s = idle_s
        self._last_data = time.monotonic()
        self._saw_data = False

    def mark(self) -> None:
        self._last_data = time.monotonic()
        self._saw_data = True

    def ready(self, last_line: str, alternate: bool) -> bool:
        if not self._saw_data or alternate:
            return False
        if time.monotonic() - self._last_data < self._idle_s:
            return False
        return looks_like_shell_prompt(last_line)


def opens_shell(text: str) -> bool:
    """Команда запускает интерактивную оболочку, а не один процесс."""
    line = text.strip()
    if not line or "\n" in line or any(op in line for op in ("|", "&&", "||", ";")):
        return False
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        return False
    return _opens_shell(_skip_env(parts))


def is_exit_command(text: str) -> bool:
    line = text.strip()
    if not line or "\n" in line:
        return False
    parts = line.split()
    if parts[0] == "logout":
        return len(parts) == 1
    if parts[0] != "exit":
        return False
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[1].lstrip("-").isdigit()


def looks_like_secret_prompt(line: str) -> bool:
    return _SECRET.search(line.strip()) is not None


def looks_like_shell_prompt(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 220 or text[-1] not in "#$%":
        return False
    if looks_like_secret_prompt(text):
        return False
    if re.fullmatch(r"[#$%]", text):
        return True
    if "@" in text or "~" in text:
        return True
    return re.fullmatch(r"\[[^\]]+\]\s*[#$%]", text) is not None


def prompt_label(line: str) -> str:
    text = line.strip()
    found = re.search(r"([A-Za-z0-9._-]+@[A-Za-z0-9._-]+)", text)
    if found:
        return found.group(1)[:40]
    if text.endswith("#"):
        return "root"
    return "shell"


def wrap_command(text: str, tag: str) -> str:
    """Одна строка оболочки: команда, затем метка. Метка не попадает в nano и sudo."""
    body = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    return (
        f"{body}; __vibe_ec=$?; "
        f"printf '\\r\\n__VIBE_DONE__{tag}__%s__\\r\\n' \"$__vibe_ec\"\n"
    )


def silence_command() -> str:
    return "stty -echo 2>/dev/null || true\n"


def last_text_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _skip_env(parts: list) -> list:
    index = 0
    while index < len(parts):
        item = parts[index]
        if "=" not in item or item.startswith("=") or item.startswith("-"):
            break
        index += 1
    return parts[index:]


def _opens_shell(parts: list) -> bool:
    if not parts:
        return False
    command = os.path.basename(parts[0]).lower()
    rest = parts[1:]
    if command == "ssh":
        positionals = _ssh_positionals(rest)
        if any(flag in rest for flag in ("-N", "-f")):
            return False
        return len(positionals) <= 1
    if command == "su":
        return "-c" not in rest and "--command" not in rest
    if command in {"sudo", "doas"}:
        return _sudo_opens_shell(rest)
    if command in _SHELLS:
        if any(flag in rest for flag in ("-c", "--command")):
            return False
        positionals = [item for item in rest if not item.startswith("-")]
        return not positionals
    if command in {"docker", "podman"} and "exec" in rest:
        if "-c" in rest or "--command" in rest:
            return False
        blob = " ".join(rest)
        return "-it" in blob or "-ti" in blob or " -i " in f" {blob} "
    if command == "kubectl" and "exec" in rest:
        blob = " ".join(rest)
        return "-it" in blob or "-ti" in blob
    return False


def _sudo_opens_shell(rest: list) -> bool:
    positionals = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item in {"-i", "-s", "--login", "--shell"}:
            return True
        if item in {
            "-u", "-g", "-p", "-h", "-C", "-D", "-R", "-T",
            "--user", "--group", "--prompt", "--host",
        }:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        positionals.append(item)
        index += 1
    if not positionals:
        return False
    return _opens_shell(positionals)


def _ssh_positionals(rest: list) -> list:
    positionals = []
    index = 0
    while index < len(rest):
        item = rest[index]
        if item == "--":
            positionals.extend(rest[index + 1:])
            break
        if item.startswith("-") and item != "-":
            index += 2 if item in _SSH_WITH_ARG else 1
            continue
        positionals.append(item)
        index += 1
    return positionals
