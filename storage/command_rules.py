# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Один файл шаблонов команд. У каждой ОС свой список."""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Tuple

import domain
from application.command_rules import RuleSet, compile_rule, rule_set


class FileCommandRuleStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._sets: Dict[str, RuleSet] = {}

    def lists(self) -> Dict[str, Tuple[str, ...]]:
        with self._lock:
            stored = self._load()
        return {family: tuple(stored.get(family, ())) for family in _FAMILIES}

    def patterns(self, family: str) -> Tuple[str, ...]:
        key = domain.normalize_shell_family(family)
        with self._lock:
            return tuple(self._load().get(key, ()))

    def rule_set_for(self, family: str) -> RuleSet:
        key = domain.normalize_shell_family(family)
        with self._lock:
            cached = self._sets.get(key)
            if cached is None:
                cached = rule_set(self._load().get(key, ()))
                self._sets[key] = cached
            return cached

    def add(self, family: str, pattern: str) -> None:
        source = pattern.strip()
        key = domain.normalize_shell_family(family)
        if compile_rule(source) is None:
            raise ValueError("Регулярное выражение не разбирается")
        with self._lock:
            stored = self._load()
            current = list(stored.get(key, ()))
            if source not in current:
                current.append(source)
            stored[key] = current
            self._write(stored)
            self._drop_cache()

    def remove(self, family: str, pattern: str) -> None:
        source = pattern.strip()
        key = domain.normalize_shell_family(family)
        with self._lock:
            stored = self._load()
            stored[key] = [item for item in stored.get(key, ()) if item != source]
            self._write(stored)
            self._drop_cache()

    def _drop_cache(self) -> None:
        self._sets.clear()

    def _load(self) -> Dict[str, List[str]]:
        if not os.path.exists(self._path):
            return {family: [] for family in _FAMILIES}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {family: [] for family in _FAMILIES}
        if not isinstance(data, dict):
            return {family: [] for family in _FAMILIES}
        stored: Dict[str, List[str]] = {}
        for family in _FAMILIES:
            raw = data.get(family) or []
            kept: List[str] = []
            if isinstance(raw, list):
                for item in raw:
                    text = str(item).strip()
                    if text and text not in kept:
                        kept.append(text)
            stored[family] = kept
        return stored

    def _write(self, stored: Dict[str, List[str]]) -> None:
        payload = {family: list(stored.get(family, ())) for family in _FAMILIES}
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)


_FAMILIES = (domain.SHELL_UNIX, domain.SHELL_WINDOWS, domain.SHELL_OTHER)
