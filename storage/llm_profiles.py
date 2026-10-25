# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Профили моделей. Ключ хранится здесь, не в каталоге сессий."""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

import domain


class FileLlmProfileStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    def list(self) -> List[domain.LlmProfile]:
        with self._lock:
            profiles = list(self._load().values())
        profiles.sort(key=lambda item: item.name.lower())
        return profiles

    def get(self, profile_id: domain.ProfileId) -> Optional[domain.LlmProfile]:
        with self._lock:
            return self._load().get(profile_id)

    def save(self, profile: domain.LlmProfile) -> None:
        with self._lock:
            profiles = self._load()
            profiles[profile.profile_id] = profile
            self._write(profiles)

    def delete(self, profile_id: domain.ProfileId) -> None:
        with self._lock:
            profiles = self._load()
            if profile_id not in profiles:
                return
            del profiles[profile_id]
            self._write(profiles)

    def _load(self) -> Dict[str, domain.LlmProfile]:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("profiles")
        if not isinstance(raw, list):
            return {}
        profiles: Dict[str, domain.LlmProfile] = {}
        for item in raw:
            if not isinstance(item, dict) or not item.get("profile_id"):
                continue
            try:
                context_limit = int(item.get("context_limit") or domain.DEFAULT_CONTEXT_LIMIT)
            except (TypeError, ValueError):
                context_limit = domain.DEFAULT_CONTEXT_LIMIT
            if context_limit < 256:
                context_limit = domain.DEFAULT_CONTEXT_LIMIT
            raw_kind = item.get("api_kind")
            if raw_kind in (None, ""):
                api_kind = domain.LlmApiKind.LM_STUDIO
            else:
                try:
                    api_kind = domain.LlmApiKind(str(raw_kind))
                except ValueError:
                    continue
            profile = domain.LlmProfile(
                profile_id=str(item["profile_id"]),
                name=str(item.get("name") or ""),
                base_url=str(item.get("base_url") or ""),
                model_id=str(item.get("model_id") or ""),
                api_key=str(item.get("api_key") or ""),
                context_limit=context_limit,
                api_kind=api_kind,
            )
            profiles[profile.profile_id] = profile
        return profiles

    def _write(self, profiles: Dict[str, domain.LlmProfile]) -> None:
        payload = {
            "profiles": [
                {
                    "profile_id": item.profile_id,
                    "name": item.name,
                    "base_url": item.base_url,
                    "model_id": item.model_id,
                    "api_key": item.api_key,
                    "context_limit": item.context_limit,
                    "api_kind": item.api_kind.value,
                }
                for item in profiles.values()
            ]
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._path)
