# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Выбор заголовков chat completions по виду API. Без Qt."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping

import domain
from drivers.llm import OpenAiChatClient


@dataclass(frozen=True)
class ChatDialect:
    extra_headers: Mapping[str, str]


def dialect_for(kind: domain.LlmApiKind) -> ChatDialect:
    if kind == domain.LlmApiKind.OPENROUTER:
        return ChatDialect(extra_headers={"X-Title": "Renisin"})
    return ChatDialect(extra_headers={})


class RoutingLlmClient:
    """Один HTTP-адаптер. Вид API профиля задаёт только заголовки."""

    def __init__(self, timeout: float = 20.0, complete_timeout: float = 300.0) -> None:
        self._client = OpenAiChatClient(timeout=timeout, complete_timeout=complete_timeout)

    def list_models(self, profile: domain.LlmProfile) -> List[str]:
        return self._client.list_models(profile, self._headers(profile))

    def complete(self, profile: domain.LlmProfile, messages: List[tuple]) -> str:
        return self._client.complete(profile, messages, self._headers(profile))

    def complete_turn(
        self,
        profile: domain.LlmProfile,
        messages: list,
        tools: list,
        allow_tools: bool = True,
    ) -> domain.LlmTurn:
        return self._client.complete_turn(
            profile,
            messages,
            tools,
            allow_tools,
            extra_headers=self._headers(profile),
        )

    def _headers(self, profile: domain.LlmProfile) -> Mapping[str, str]:
        return dialect_for(profile.api_kind).extra_headers
