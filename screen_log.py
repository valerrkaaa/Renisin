# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Экран терминала на pyte с отдельным основным буфером.

Лист без Qt и без процессов. Его используют драйверы (снимок для журнала)
и UI (живой кадр). Кадры alternate screen не попадают в основной текст.
"""
from __future__ import annotations

import copy
import threading
from typing import List, Optional, Tuple

import pyte
from pyte.screens import Char, Cursor, Margins, Screen

# Режимы DEC alternate screen: CSI ? 47 / 1047 / 1049 h.
_ALT_MODES = {47, 1047, 1049}

Line = dict
PaintRow = Tuple[List[Char], int, int, bool]


class PrimaryScreen(Screen):
    """Экран, который помнит прокрутку основного буфера и прячет alternate screen."""

    def __init__(self, columns: int, lines: int, history: int = 4000) -> None:
        super().__init__(columns, lines)
        self._history_limit = history
        self._log: List[Line] = []
        self._alt = False
        self._saved_buffer = None
        self._saved_cursor: Optional[Tuple[int, int]] = None

    @property
    def in_alternate_screen(self) -> bool:
        return self._alt

    def set_mode(self, *modes: int, **kwargs) -> None:  # type: ignore[override]
        if kwargs.get("private") and any(mode in _ALT_MODES for mode in modes):
            self._enter_alt()
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs) -> None:  # type: ignore[override]
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and any(mode in _ALT_MODES for mode in modes):
            self._leave_alt()

    def index(self) -> None:  # type: ignore[override]
        top, bottom = self.margins or Margins(0, self.lines - 1)
        if not self._alt and self.cursor.y == bottom:
            self._log.append(copy.deepcopy(self.buffer[top]))
            if len(self._log) > self._history_limit:
                del self._log[: len(self._log) - self._history_limit]
        super().index()

    def _enter_alt(self) -> None:
        if self._alt:
            return
        self._saved_buffer = copy.deepcopy(self.buffer)
        self._saved_cursor = (self.cursor.x, self.cursor.y)
        self._alt = True
        self.erase_in_display(2)
        self.cursor_position()

    def _leave_alt(self) -> None:
        if not self._alt:
            return
        if self._saved_buffer is not None:
            self.buffer = self._saved_buffer
        if self._saved_cursor is not None:
            self.cursor.x, self.cursor.y = self._saved_cursor
        self._saved_buffer = None
        self._saved_cursor = None
        self._alt = False
        self.dirty.update(range(self.lines))

    def primary_lines(self) -> List[str]:
        """Текст основного экрана. Alternate screen в него не входит."""
        if self._alt and self._saved_buffer is not None:
            saved = self._saved_buffer
            visible = [self._render_line(saved[y]) for y in range(self.lines)]
        else:
            visible = [self._render_line(self.buffer[y]) for y in range(self.lines)]
        lines = [self._render_line(line) for line in self._log]
        lines.extend(visible)
        while lines and not lines[-1].strip():
            lines.pop()
        return lines

    def paint_rows(self) -> Tuple[List[Line], Cursor, bool]:
        """Строки для отрисовки: в alternate screen только его буфер."""
        if self._alt:
            rows = [self.buffer[y] for y in range(self.lines)]
            return rows, self.cursor, True
        rows = list(self._log) + [self.buffer[y] for y in range(self.lines)]
        cursor = Cursor(self.cursor.x, self.cursor.y + len(self._log))
        cursor.hidden = self.cursor.hidden
        return rows, cursor, False

    def _render_line(self, line: Line) -> str:
        parts: List[str] = []
        skip = False
        for x in range(self.columns):
            if skip:
                skip = False
                continue
            data = line[x].data
            parts.append(data)
            if data and _is_wide(data[0]):
                skip = True
        return "".join(parts).rstrip()


class ScreenLog:
    """Потокобезопасная обёртка: feed / resize / снимок."""

    def __init__(self, columns: int = 80, lines: int = 24) -> None:
        self._screen = PrimaryScreen(columns, lines)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()

    def feed(self, data: str) -> None:
        if not data:
            return
        with self._lock:
            self._stream.feed(data)

    def resize(self, columns: int, lines: int) -> None:
        columns = max(2, columns)
        lines = max(2, lines)
        with self._lock:
            self._screen.resize(lines=lines, columns=columns)

    def primary_text(self) -> str:
        with self._lock:
            lines = self._screen.primary_lines()
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    def application_cursor(self) -> bool:
        """Стрелки в режиме приложения (DECCKM): ESC O A вместо ESC [ A."""
        with self._lock:
            return (1 << 5) in self._screen.mode

    @property
    def in_alternate_screen(self) -> bool:
        with self._lock:
            return self._screen.in_alternate_screen

    def paint_rows(self) -> Tuple[List[Line], Cursor, bool]:
        with self._lock:
            return self._screen.paint_rows()

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def lines(self) -> int:
        return self._screen.lines


def _is_wide(ch: str) -> bool:
    try:
        from wcwidth import wcwidth
    except ImportError:
        return False
    return wcwidth(ch) == 2
