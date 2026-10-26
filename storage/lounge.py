# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Вкладки общего чата: имя, модель и переписка."""
from __future__ import annotations

import json
import os
import threading
from typing import List

import domain


class FileLoungeStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    def load(self) -> List[domain.LoungeTab]:
        with self._lock:
            if not os.path.exists(self._path):
                return []
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return []
        tabs = []
        for item in payload.get("tabs") or []:
            if not isinstance(item, dict) or not item.get("tab_id"):
                continue
            messages = []
            for message in item.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                if role not in ("user", "assistant", "result", "command"):
                    continue
                messages.append((role, str(message.get("text") or "")))
            tabs.append(
                domain.LoungeTab(
                    tab_id=str(item["tab_id"]),
                    title=str(item.get("title") or "чат").strip() or "чат",
                    profile_id=str(item.get("profile_id") or ""),
                    messages=tuple(messages),
                )
            )
        return tabs

    def save(self, tabs: List[domain.LoungeTab]) -> None:
        payload = {
            "tabs": [
                {
                    "tab_id": tab.tab_id,
                    "title": tab.title,
                    "profile_id": tab.profile_id,
                    "messages": [
                        {"role": role, "text": text} for role, text in tab.messages
                    ],
                }
                for tab in tabs
            ]
        }
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self._path + ".tmp"
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
