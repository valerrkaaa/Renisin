# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Общий клиент OpenAI chat completions. Без Qt.

OpenAI, LM Studio и OpenRouter отличаются адресом и заголовками.
Их подставляет RoutingLlmClient. temperature и response_format
не отправляются: часть серверов отвергает незнакомые поля.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import List, Mapping, Optional

import domain

# Прежние имена: тип хода живёт в домене, драйвер его только заполняет.
LlmToolCall = domain.LlmToolCall
LlmTurn = domain.LlmTurn


class OpenAiChatClient:
    def __init__(self, timeout: float = 20.0, complete_timeout: float = 300.0) -> None:
        self._timeout = timeout
        self._complete_timeout = complete_timeout

    def list_models(
        self,
        profile: domain.LlmProfile,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> List[str]:
        url = _endpoint(profile.base_url, "models")
        data = self._request(url, profile.api_key, None, self._timeout, extra_headers)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ConnectionError("Сервер не вернул список моделей")
        names: List[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                names.append(str(item["id"]))
        return names

    def complete(
        self,
        profile: domain.LlmProfile,
        messages: List[tuple],
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> str:
        url = _endpoint(profile.base_url, "chat/completions")
        body = {
            "model": profile.model_id,
            "messages": [
                {"role": role, "content": content} for role, content in messages
            ],
        }
        data = self._request(
            url, profile.api_key, body, self._complete_timeout, extra_headers
        )
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise ConnectionError("Модель не вернула ответ")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = ""
        if isinstance(message, dict):
            content = str(message.get("content") or "")
        if not content.strip():
            raise ConnectionError("Пустой ответ модели")
        return content

    def complete_turn(
        self,
        profile: domain.LlmProfile,
        messages: list,
        tools: list,
        allow_tools: bool = True,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> domain.LlmTurn:
        """Один ответ модели. Пустой текст допустим, если есть вызов инструмента.

        Ждёт весь JSON ответа. Пока сервер генерирует, клиент ход не закрывает.
        finish_reason=length значит, что сервер сам оборвал текст по лимиту.
        """
        url = _endpoint(profile.base_url, "chat/completions")
        body = {
            "model": profile.model_id,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto" if allow_tools else "none"
        data = self._request(
            url, profile.api_key, body, self._complete_timeout, extra_headers
        )
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ConnectionError("Модель не вернула ответ")
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ConnectionError("Модель не вернула ответ")
        content, calls = _read_message(message)
        finish = str(choice.get("finish_reason") or "stop")
        if not allow_tools:
            calls = []
        if not content.strip() and not calls and finish != "length":
            raise ConnectionError("Пустой ответ модели")
        return domain.LlmTurn(content, calls, finish)

    def _request(
        self,
        url: str,
        api_key: str,
        payload: object,
        timeout: float,
        extra_headers: Optional[Mapping[str, str]] = None,
    ):
        headers = {"Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        data = None
        method = "GET"
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
            method = "POST"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        stop = domain.current_chat_stop()
        if stop is not None and stop.cancelled():
            raise domain.ChatStopped()
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _open(request, timeout, stop) as response:
                raw = response.read()
        except domain.ChatStopped:
            raise
        except urllib.error.HTTPError as exc:
            if stop is not None and stop.cancelled():
                raise domain.ChatStopped() from exc
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ConnectionError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            if stop is not None and stop.cancelled():
                raise domain.ChatStopped() from exc
            raise ConnectionError(f"Нет соединения: {exc.reason}") from exc
        except TimeoutError as exc:
            if stop is not None and stop.cancelled():
                raise domain.ChatStopped() from exc
            raise ConnectionError("Превышено время ожидания") from exc
        except OSError as exc:
            if stop is not None and stop.cancelled():
                raise domain.ChatStopped() from exc
            raise ConnectionError(f"Нет соединения: {exc}") from exc
        finally:
            if stop is not None:
                stop.detach()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ConnectionError("Сервер вернул не JSON") from exc
        if not isinstance(parsed, dict):
            raise ConnectionError("Неожиданный ответ сервера")
        return parsed


class LmStudioClient(OpenAiChatClient):
    """Прежнее имя клиента без дополнительных заголовков."""


class _MemoryResponse:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def _open(request: urllib.request.Request, timeout: float, stop: Optional[domain.ChatStop]):
    """Ждёт ответ. Остановка проверяется часто: на Windows чужой поток не будит чтение."""
    if stop is None:
        return urllib.request.urlopen(request, timeout=timeout)
    if stop.cancelled():
        raise domain.ChatStopped()
    box: dict = {}
    done = threading.Event()

    def work() -> None:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                box["raw"] = response.read()
        except BaseException as exc:
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=work, name="llm-http", daemon=True).start()
    deadline = time.monotonic() + timeout
    while not done.wait(0.2):
        if stop.cancelled():
            raise domain.ChatStopped()
        if time.monotonic() >= deadline:
            raise TimeoutError()
    if stop.cancelled():
        raise domain.ChatStopped()
    error = box.get("error")
    if error is not None:
        raise error
    return _MemoryResponse(box.get("raw") or b"")


def _endpoint(base_url: str, path: str) -> str:
    return base_url.rstrip("/") + "/" + path.lstrip("/")


def _read_message(message: dict) -> tuple:
    calls: List[domain.LlmToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if isinstance(raw_calls, list):
        for item in raw_calls:
            call = _parse_tool_call(item)
            if call is None:
                calls.append(domain.LlmToolCall("", "", {}, broken=True))
            else:
                calls.append(call)
    legacy = message.get("function_call")
    if isinstance(legacy, dict) and not calls:
        call = _parse_tool_call({"id": "legacy", "function": legacy})
        if call is not None:
            calls.append(call)
    content = message.get("content")
    if isinstance(content, list):
        texts: List[str] = []
        for item in content:
            if isinstance(item, str):
                texts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind in ("tool_use", "tool_call", "function_call", "function"):
                call = _call_from_part(item)
                if call is not None:
                    calls.append(call)
                continue
            texts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in texts if part), calls
    return str(content or ""), calls


def _call_from_part(item: dict) -> domain.LlmToolCall | None:
    function = item.get("function") if isinstance(item.get("function"), dict) else {}
    name = str(item.get("name") or function.get("name") or "")
    if not name:
        return None
    raw = item.get("input", item.get("arguments", function.get("arguments", {})))
    broken = False
    if isinstance(raw, dict):
        arguments = raw
    else:
        try:
            arguments = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            arguments = {}
            broken = True
    if not isinstance(arguments, dict):
        arguments = {}
        broken = True
    return domain.LlmToolCall(
        id=str(item.get("id") or ""),
        name=name,
        arguments=arguments,
        broken=broken,
    )


def _parse_tool_call(item: object) -> domain.LlmToolCall | None:
    if not isinstance(item, dict):
        return None
    function = item.get("function")
    if not isinstance(function, dict):
        function = item
    name = str(function.get("name") or item.get("name") or "")
    if not name:
        return None
    raw = function.get("arguments")
    if raw is None:
        raw = function.get("parameters", function.get("input", item.get("arguments")))
    broken = False
    if isinstance(raw, dict):
        arguments = raw
    else:
        try:
            arguments = json.loads(str(raw or "{}"))
        except json.JSONDecodeError:
            arguments = {}
            broken = True
    if not isinstance(arguments, dict):
        arguments = {}
        broken = True
    return domain.LlmToolCall(
        id=str(item.get("id") or ""),
        name=name,
        arguments=arguments,
        broken=broken,
    )
