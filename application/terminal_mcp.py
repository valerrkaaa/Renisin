# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Команда терминала через MCP: схема инструмента и тексты ответов модели."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


TOOL_NAME = "run_command"

# Длинный лог не должен целиком уходить в контекст модели.
OUTPUT_CHAR_LIMIT = 4000

TOOL_DESCRIPTION = (
    "Самому выполнить одну команду в терминале этой сессии "
    "и получить её вывод. Вызывай снова, пока не достигнет нужного результата. "
    "Пользователь подтверждает команду в чате. "
    "Пример для человека этим инструментом не показывай: пример — это блок кода в тексте. "
    "Нельзя вызывать интерактивные команды: nano, vim, vi, less, more, top, htop, man "
    "и любые программы, которые открывают интерактивный экран и ждут ввод. "
    "Такая команда будет прервана, а в ответ придёт отказ: "
    "MCP не поддерживает интерактивные сессии. "
    "Длинный вывод обрезается: в конце будет пометка, что ответ урезан "
    "из-за большого числа символов."
)

INTERACTIVE_REASON = "MCP не поддерживает интерактивные сессии"


# Источник общего чата: «lounge-tab:» и id вкладки. Пустая строка — чат терминала.
LOUNGE_ORIGIN_PREFIX = "lounge-tab:"


def lounge_origin(tab_id: str) -> str:
    return f"{LOUNGE_ORIGIN_PREFIX}{tab_id}"


def lounge_tab_id(origin_id: str) -> str:
    """Id вкладки общего чата или пустая строка, если источник — чат консоли."""
    if not origin_id.startswith(LOUNGE_ORIGIN_PREFIX):
        return ""
    tab_id = origin_id[len(LOUNGE_ORIGIN_PREFIX) :]
    if not tab_id or "/" in tab_id:
        return ""
    return tab_id


@dataclass(frozen=True)
class CommandAsk:
    request_id: str
    session_id: str
    intent: str
    command: str
    origin_id: str = ""


@dataclass(frozen=True)
class CommandAnswer:
    """action: run, always, reject, reject_reason."""

    action: str
    reason: str = ""


def normalize_command(command: str) -> str:
    return command.replace("\r\n", "\n").replace("\r", "\n").strip()


def command_text(arguments: object) -> str:
    """Текст команды из аргументов инструмента. Модели называют поле по-разному."""
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "shell"):
        text = normalize_command(str(arguments.get(key) or ""))
        if text:
            return text
    raw = arguments.get("input")
    if isinstance(raw, str):
        return normalize_command(raw)
    return ""


def refusal() -> str:
    return "отказ"


def refusal_reason(reason: str) -> str:
    text = " ".join(reason.split())
    return f"отказ, причина: {text}"


def clip_output(output: str, limit: int = OUTPUT_CHAR_LIMIT) -> str:
    """Оставляет начало вывода. Хвост в ответ модели не входит."""
    text = output.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if len(text) <= limit:
        return text
    note = (
        "\n… обрезано из-за большого числа символов "
        f"(показано {limit} из {len(text)})"
    )
    return text[:limit].rstrip() + note


def finished_text(code: Optional[int], output: str) -> str:
    shown = clip_output(output)
    code_text = "нет" if code is None else str(code)
    if shown:
        return f"выполнено, код {code_text}\n{shown}"
    return f"выполнено, код {code_text}"


def input_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "Намерение: что этой командой нужно сделать",
            },
            "command": {
                "type": "string",
                "description": "Текст команды для терминала",
            },
        },
        "required": ["intent", "command"],
    }


def os_command_note(os_name: str) -> str:
    """Фраза для модели: для какой ОС писать команду."""
    cleaned = os_name.strip()
    if cleaned:
        return (
            f" ОС: {cleaned}. Команду пиши только для этой ОС "
            "и не подменяй её другой системой."
        )
    return (
        " ОС не определена. Не угадывай Windows, Linux или другую систему."
    )


def openai_tools(os_name: str = "") -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": TOOL_DESCRIPTION + os_command_note(os_name),
                "parameters": input_schema(),
            },
        }
    ]


def lounge_tools(names: List[str], os_by_name: Optional[dict] = None) -> list:
    """Тот же run_command, но с именем MCP и его ОС."""
    shown = ", ".join(names) if names else "нет подключённых MCP"
    schema = input_schema()
    properties = dict(schema["properties"])
    properties["target"] = {
        "type": "string",
        "description": f"Точное имя MCP из списка: {shown}",
    }
    os_text = ""
    if names:
        notes = []
        known = os_by_name or {}
        for name in names:
            system = str(known.get(name) or "").strip() or "ОС не определена"
            notes.append(f"{name} — {system}")
        os_text = (
            " У каждого MCP своя ОС: "
            + "; ".join(notes)
            + ". Команду пиши для ОС выбранного target. "
            "Если ОС не определена, не угадывай систему."
        )
    return [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": (
                    TOOL_DESCRIPTION
                    + " Поле target — точное имя MCP, в чей терминал уйдёт команда. "
                    + f"Сейчас доступны: {shown}."
                    + os_text
                ),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["intent", "command", "target"],
                },
            },
        }
    ]


@dataclass(frozen=True)
class ParsedCall:
    """Вызов, который модель написала текстом, а не полем tool_calls."""

    id: str
    name: str
    arguments: dict


_TOOL_TAG = re.compile(
    r"<(?:tool_call|function_call)>\s*(.*?)\s*</(?:tool_call|function_call)>",
    re.DOTALL | re.IGNORECASE,
)
_PY_CALL = re.compile(r"run_command\s*\((.*?)\)", re.DOTALL | re.IGNORECASE)
_KWARG = re.compile(
    r"(?i)\b(intent|command|target)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
)
_QWEN = re.compile(
    r"<function=run_command>\s*(.*?)\s*</function>",
    re.DOTALL | re.IGNORECASE,
)
_PARAM = re.compile(
    r"<parameter=([A-Za-z0-9_]+)>\s*(.*?)\s*</parameter>",
    re.DOTALL | re.IGNORECASE,
)


def promote_shell_actions(text: str) -> Tuple[str, List[ParsedCall]]:
    """Забрать текстовую запись run_command. Блок кода остаётся текстом для человека."""
    cleaned, calls = _markup_calls(text)
    return _tidy(cleaned), calls


def _markup_calls(text: str) -> Tuple[str, List[ParsedCall]]:
    calls: List[ParsedCall] = []

    def take_tag(match: re.Match) -> str:
        call = _call_from_blob(match.group(1), len(calls) + 1)
        if call is None:
            return match.group(0)
        calls.append(call)
        return ""

    def take_qwen(match: re.Match) -> str:
        arguments = {
            key: value.strip()
            for key, value in _PARAM.findall(match.group(1))
        }
        call = _call_from_mapping(arguments, len(calls) + 1)
        if call is None:
            return match.group(0)
        calls.append(call)
        return ""

    def take_python(match: re.Match) -> str:
        call = _call_from_mapping(_kwargs(match.group(1)), len(calls) + 1)
        if call is None:
            return match.group(0)
        calls.append(call)
        return ""

    cleaned = _TOOL_TAG.sub(take_tag, text)
    cleaned = _QWEN.sub(take_qwen, cleaned)
    cleaned = _outside_fences(cleaned, lambda part: _PY_CALL.sub(take_python, part))
    return cleaned, calls


def _call_from_blob(blob: str, index: int) -> Optional[ParsedCall]:
    raw = blob.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return _call_from_mapping(data, index)
    arguments = {key: value.strip() for key, value in _PARAM.findall(raw)}
    if not arguments:
        arguments = _loose_fields(raw)
    if arguments:
        return _call_from_mapping(arguments, index)
    return None


def _call_from_mapping(data: dict, index: int) -> Optional[ParsedCall]:
    name = str(data.get("name") or "")
    arguments = data.get("arguments", data.get("parameters", data.get("input")))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = None
    if not isinstance(arguments, dict):
        arguments = data
    if name and name != TOOL_NAME:
        return None
    command = str(arguments.get("command") or "")
    if not normalize_command(command):
        return None
    intent = str(arguments.get("intent") or "").strip() or "выполнить команду"
    payload = {"intent": intent, "command": command}
    target = str(arguments.get("target") or "").strip()
    if target:
        payload["target"] = target
    return ParsedCall(
        id=f"text-{index}",
        name=TOOL_NAME,
        arguments=payload,
    )


def _kwargs(blob: str) -> dict:
    found = {}
    for match in _KWARG.finditer(blob):
        value = match.group(2) if match.group(2) is not None else match.group(3)
        found[match.group(1).lower()] = value
    return found


def _loose_fields(blob: str) -> dict:
    found = {}
    for key in ("name", "command", "intent", "target"):
        match = re.search(
            rf"(?im)^\s*{key}\s*[:=]\s*[\"']?(.*?)[\"']?\s*$",
            blob,
        )
        if match and match.group(1).strip():
            found[key] = match.group(1).strip()
    return found


def _outside_fences(text: str, rewrite) -> str:
    parts = re.split(r"(```.*?```)", text, flags=re.DOTALL)
    return "".join(part if part.startswith("```") else rewrite(part) for part in parts)


def _tidy(text: str) -> str:
    compact = re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    return compact.strip()
