# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Путь к приватному ключу SSH и признак, что его passphrase — мастер-пароль."""
from __future__ import annotations

import json
import os
import threading


class FileSshKeySettings:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> str:
        return self._read()[0]

    def passphrase_is_master(self) -> bool:
        return self._read()[1]

    def _read(self) -> tuple[str, bool]:
        with self._lock:
            if not os.path.exists(self._path):
                return "", False
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return "", False
        if not isinstance(data, dict):
            return "", False
        path = str(data.get("private_key_path") or "").strip()
        return path, bool(data.get("passphrase_is_master"))

    def save(
        self,
        private_key_path: str,
        passphrase_is_master: bool | None = None,
    ) -> None:
        cleaned = private_key_path.strip()
        if passphrase_is_master is None:
            current_path, current_flag = self._read()
            passphrase_is_master = bool(current_flag) and cleaned == current_path
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            {
                "private_key_path": cleaned,
                "passphrase_is_master": bool(passphrase_is_master) and bool(cleaned),
            },
            ensure_ascii=False,
            indent=2,
        )
        temporary = self._path + ".tmp"
        with self._lock:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
