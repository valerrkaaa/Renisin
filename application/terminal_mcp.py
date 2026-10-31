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
    # Части для показа. В терминал уходит command целиком.
    parts: tuple = ()
    # Строки подтверждения, разрезанные на команды и операторы.
    spans: tuple = ()


@dataclass(frozen=True)
class CommandAnswer:
    """action: run, always, reject, reject_reason, deliver.

    deliver — команда уже выполнена, модели уходит body.
    """

    action: str
    reason: str = ""
    body: str = ""


def normalize_command(command: str) -> str:
    return command.replace("\r\n", "\n").replace("\r", "\n").strip()


def split_shell_commands(command: str, family: str) -> tuple:
    """Разбить текст на команды для показа. Исходная строка не меняется.

    linux/macOS: «;» и перевод строки. windows: «&» и перевод строки.
    «&&», «||» и конвейер «|» остаются одной командой.
    other — вся строка одним элементом.
    """
    text = normalize_command(command)
    if not text:
        return ()
    kind = (family or "").strip().lower()
    if kind in {"other", "другая"}:
        return (text,)
    separator = "&" if kind in {"windows", "win", "cmd"} else ";"
    parts = [part.strip() for part in _split_shell(text, separator) if part.strip()]
    return tuple(parts) if parts else (text,)


def _split_shell(text: str, separator: str) -> list:
    windows = separator == "&"
    parts: list = []
    buf: list = []
    index = 0
    length = len(text)
    quote = ""
    backtick = False
    paren = 0
    heredocs: list = []
    at_line_start = True

    def flush() -> None:
        parts.append("".join(buf))
        buf.clear()

    while index < length:
        if heredocs and at_line_start:
            end = text.find("\n", index)
            if end < 0:
                end = length
            line = text[index:end]
            token, strip_tabs = heredocs[0]
            probe = line.lstrip("\t") if strip_tabs else line
            buf.append(line)
            if probe.rstrip("\r") == token:
                heredocs.pop(0)
            index = end
            at_line_start = False
            continue

        char = text[index]
        if char == "\n":
            buf.append(char)
            index += 1
            at_line_start = True
            if quote or backtick or paren or heredocs or _escaped_break(buf, windows):
                continue
            if _ends_with_joiner(buf):
                continue
            buf.pop()
            flush()
            continue

        at_line_start = False
        if quote:
            buf.append(char)
            if windows:
                if char == '"':
                    quote = ""
                index += 1
                continue
            if char == "\\" and quote == '"' and index + 1 < length:
                buf.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue

        if backtick:
            buf.append(char)
            if char == "\\" and index + 1 < length:
                buf.append(text[index + 1])
                index += 2
                continue
            if char == "`":
                backtick = False
            index += 1
            continue

        if not windows and char == "\\" and index + 1 < length:
            buf.append(char)
            buf.append(text[index + 1])
            index += 2
            continue
        if windows and char == "^" and index + 1 < length:
            buf.append(char)
            buf.append(text[index + 1])
            index += 2
            continue

        if char == '"' or (char == "'" and not windows):
            quote = char
            buf.append(char)
            index += 1
            continue
        if not windows and char == "`":
            backtick = True
            buf.append(char)
            index += 1
            continue
        if char == "(":
            paren += 1
            buf.append(char)
            index += 1
            continue
        if char == ")" and paren:
            paren -= 1
            buf.append(char)
            index += 1
            continue

        if not windows and text.startswith("<<", index):
            opened = _read_heredoc_opener(text, index)
            if opened is not None:
                token, strip_tabs, next_index = opened
                buf.append(text[index:next_index])
                if token:
                    heredocs.append((token, strip_tabs))
                index = next_index
                continue

        # Тело heredoc идёт следующими строками, поэтому «;» на строке
        # открытия не отрывает его от команды.
        if heredocs:
            buf.append(char)
            index += 1
            continue

        if paren == 0 and separator == ";" and char == ";":
            if index + 1 < length and text[index + 1] == ";":
                buf.append(";;")
                index += 2
                continue
            if index > 0 and text[index - 1] == ";":
                buf.append(char)
                index += 1
                continue
            flush()
            index += 1
            continue
        if paren == 0 and separator == "&" and char == "&":
            nxt = text[index + 1] if index + 1 < length else ""
            prev = text[index - 1] if index else ""
            if nxt == "&":
                buf.append("&&")
                index += 2
                continue
            if prev in "<>":
                buf.append(char)
                index += 1
                continue
            flush()
            index += 1
            continue

        buf.append(char)
        index += 1

    flush()
    return parts


def _escaped_break(buf: list, windows: bool) -> bool:
    """Перевод строки после \\ или ^ — продолжение, а не новая команда."""
    body = "".join(buf[:-1])
    if windows:
        return body.endswith("^")
    count = 0
    index = len(body) - 1
    while index >= 0 and body[index] == "\\":
        count += 1
        index -= 1
    return count % 2 == 1


def _ends_with_joiner(buf: list) -> bool:
    body = "".join(buf[:-1]).rstrip()
    return body.endswith("&&") or body.endswith("|")


def _read_heredoc_opener(text: str, start: int):
    """<<WORD на этой строке. None, если это here-string <<<."""
    index = start + 2
    strip_tabs = False
    if index < len(text) and text[index] == "-":
        strip_tabs = True
        index += 1
    if index < len(text) and text[index] == "<":
        return None
    while index < len(text) and text[index] in " \t":
        index += 1
    if index >= len(text) or text[index] in "\n;|&":
        return None
    if text[index] in "'\"":
        quote = text[index]
        index += 1
        token_start = index
        while index < len(text) and text[index] != quote:
            index += 1
        token = text[token_start:index]
        if index < len(text):
            index += 1
        return token, strip_tabs, index
    token_start = index
    while index < len(text) and text[index] not in " \t\n;|&<>":
        index += 1
    token = text[token_start:index]
    if not token:
        return None
    return token, strip_tabs, index


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


def call_record(status: str, command: str, output: str, mcp: str = "") -> str:
    """Запись вызова в истории чата. Модели эта строка не отправляется."""
    payload = {
        "kind": "call",
        "status": status,
        "command": command,
        "output": output,
    }
    label = mcp.strip()
    if label:
        payload["mcp"] = label
    return json.dumps(payload, ensure_ascii=False)


def parse_call(text: str) -> Optional[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("kind") != "call":
        return None
    return data


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
