# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Команды, которые пользователь разрешил выполнять без вопроса.

Хранится только текст команды. Намерение в файл не пишется.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List


def _command(command: str) -> str:
    return str(command).replace("\r\n", "\n").replace("\r", "\n").strip()


class FileCommandAllowStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    def allows(self, session_id: str, command: str) -> bool:
        text = _command(command)
        if not text:
            return False
        with self._lock:
            return text in self._load().get(session_id, [])

    def remember(self, session_id: str, command: str) -> None:
        text = _command(command)
        if not text:
            return
        with self._lock:
            sessions = self._load()
            commands = list(sessions.get(session_id, []))
            if text not in commands:
                commands.append(text)
            sessions[session_id] = commands
            self._write(sessions)

    def _load(self) -> Dict[str, List[str]]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("sessions") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        sessions: Dict[str, List[str]] = {}
        for session_id, commands in raw.items():
            if not isinstance(commands, list):
                continue
            kept = []
            for command in commands:
                text = _command(str(command))
                if text and text not in kept:
                    kept.append(text)
            if kept:
                sessions[str(session_id)] = kept
        return sessions

    def _write(self, sessions: Dict[str, List[str]]) -> None:
        payload = {"sessions": sessions}
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
