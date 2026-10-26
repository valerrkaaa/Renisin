# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Шаблоны автоодобрения команд. Общие для всех подключений, отдельные по ОС."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import domain
from application.terminal_mcp import split_shell_commands


@dataclass(frozen=True)
class TextSpan:
    """Кусок строки подтверждения. command — это команда, а не оператор или пробел."""

    text: str
    matched: bool = False
    command: bool = False


@dataclass(frozen=True)
class CompiledRule:
    source: str
    # «grep *»: команда равна префиксу или начинается с «префикс ».
    # «grep*»: достаточно startswith, без границы слова.
    prefix: Optional[str]
    word: bool
    regex: Optional[re.Pattern]


@dataclass(frozen=True)
class RuleSet:
    prefixes: Tuple[Tuple[str, bool], ...] = ()
    regexes: Tuple[re.Pattern, ...] = ()

    def matches(self, command: str) -> bool:
        text = command.strip()
        for prefix, word in self.prefixes:
            if word:
                if text == prefix or text.startswith(prefix + " "):
                    return True
            elif text.startswith(prefix):
                return True
        if not text:
            return False
        for regex in self.regexes:
            if regex.fullmatch(text):
                return True
        return False


def expand_pattern(pattern: str) -> str:
    """«*» означает любой текст. Уже записанное «.*» и экранированное «\\*» не трогаем."""
    out: list = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            out.append(pattern[index : index + 2])
            index += 2
            continue
        if char == "*":
            if out and out[-1].endswith("."):
                out.append("*")
            else:
                out.append(".*")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def compile_rule(pattern: str) -> Optional[CompiledRule]:
    """None, если регулярное выражение не разбирается."""
    source = pattern.strip()
    if not source:
        return None
    if source.endswith(" *"):
        head = source[:-2]
        if head and _literal(head):
            return CompiledRule(source=source, prefix=head, word=True, regex=None)
    expanded = expand_pattern(source)
    if expanded.endswith(".*"):
        head = expanded[:-2]
        if _literal(head):
            return CompiledRule(source=source, prefix=head, word=False, regex=None)
    try:
        regex = re.compile(expanded, re.DOTALL)
    except re.error:
        return None
    return CompiledRule(source=source, prefix=None, word=False, regex=regex)


def rule_set(patterns: Sequence[str]) -> RuleSet:
    prefixes = []
    regexes = []
    for pattern in patterns:
        compiled = compile_rule(pattern)
        if compiled is None:
            continue
        if compiled.prefix is not None:
            prefixes.append((compiled.prefix, compiled.word))
        elif compiled.regex is not None:
            regexes.append(compiled.regex)
    return RuleSet(prefixes=tuple(prefixes), regexes=tuple(regexes))


def try_match(pattern: str, sample: str) -> str:
    """Результат проверки шаблона: invalid, match или miss."""
    if not pattern.strip():
        return "invalid"
    compiled = compile_rule(pattern)
    if compiled is None:
        return "invalid"
    rules = RuleSet(
        prefixes=((compiled.prefix, compiled.word),) if compiled.prefix is not None else (),
        regexes=(compiled.regex,) if compiled.regex is not None else (),
    )
    return "match" if rules.matches(sample) else "miss"


def command_spans(command: str, family: str, rules: RuleSet) -> Tuple[Tuple[TextSpan, ...], ...]:
    """Строки, как в подтверждении. Внутри «&&», «||» и «|» подсвечивается только команда."""
    kind = domain.normalize_shell_family(family)
    lines = split_shell_commands(command, kind)
    if not lines:
        return ()
    if kind == domain.SHELL_OTHER:
        return tuple((TextSpan(line, rules.matches(line), True),) for line in lines)
    windows = kind == domain.SHELL_WINDOWS
    return tuple(tuple(_spans_for_line(line, rules, windows)) for line in lines)


def fully_allowed(spans: Sequence[Sequence[TextSpan]]) -> bool:
    """Все команды строки совпали хотя бы с одним шаблоном."""
    saw = False
    for line in spans:
        for span in line:
            if not span.command:
                continue
            saw = True
            if not span.matched:
                return False
    return saw


def _literal(text: str) -> bool:
    return re.fullmatch(r"[^*+?^$\\.{}\[\]|()]*", text) is not None


def _spans_for_line(line: str, rules: RuleSet, windows: bool) -> Tuple[TextSpan, ...]:
    spans: list = []
    for text, operator in _pieces(line, windows):
        if operator:
            spans.append(TextSpan(text, False, False))
            continue
        core = text.strip()
        if not core:
            spans.append(TextSpan(text, False, False))
            continue
        lead = len(text) - len(text.lstrip())
        trail = len(text) - len(text.rstrip())
        if lead:
            spans.append(TextSpan(text[:lead], False, False))
        spans.append(TextSpan(core, rules.matches(core), True))
        if trail:
            spans.append(TextSpan(text[-trail:], False, False))
    return tuple(spans)


def _pieces(line: str, windows: bool) -> list:
    """Текст команды или оператор &&, ||, |."""
    out: list = []
    buf: list = []
    index = 0
    length = len(line)
    quote = ""

    def flush() -> None:
        if buf:
            out.append(("".join(buf), False))
            buf.clear()

    while index < length:
        char = line[index]
        if quote:
            buf.append(char)
            if windows:
                if char == '"':
                    quote = ""
                index += 1
                continue
            if char == "\\" and quote == '"' and index + 1 < length:
                buf.append(line[index + 1])
                index += 2
                continue
            if char == quote:
                quote = ""
            index += 1
            continue
        if not windows and char == "\\" and index + 1 < length:
            buf.append(char)
            buf.append(line[index + 1])
            index += 2
            continue
        if windows and char == "^" and index + 1 < length:
            buf.append(char)
            buf.append(line[index + 1])
            index += 2
            continue
        if char == '"' or (char == "'" and not windows):
            quote = char
            buf.append(char)
            index += 1
            continue
        if line.startswith("&&", index):
            flush()
            out.append(("&&", True))
            index += 2
            continue
        if line.startswith("||", index):
            flush()
            out.append(("||", True))
            index += 2
            continue
        if char == "|":
            flush()
            operator = "|"
            index += 1
            if index < length and line[index] == "&":
                operator = "|&"
                index += 1
            out.append((operator, True))
            continue
        buf.append(char)
        index += 1
    flush()
    return out
