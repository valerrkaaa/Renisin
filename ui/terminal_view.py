# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Живой экран терминала внутри карточки команды.

Рисует буфер из screen_log и переводит клавиши Qt в байты терминала.
Процессы не запускает.
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget

_SELECT = QColor("#1f2440")

from screen_log import ScreenLog

PTY_ROWS = 24

_FG = QColor("#e4e4e7")
_BG = QColor("#101113")
_CURSOR = QColor("#b6b7d8")
_FRAME = QColor("#2e2f36")

_COLORS: Dict[str, str] = {
    "black": "#484f58",
    "red": "#ff7b72",
    "green": "#7ee787",
    "brown": "#d29922",
    "yellow": "#d29922",
    "blue": "#79c0ff",
    "magenta": "#d2a8ff",
    "cyan": "#56d4dd",
    "white": "#e6edf3",
    "brightblack": "#6e7681",
    "brightred": "#ffa198",
    "brightgreen": "#7ee787",
    "brightbrown": "#e3b341",
    "brightyellow": "#e3b341",
    "brightblue": "#a5d6ff",
    "brightmagenta": "#d2a8ff",
    "brightcyan": "#56d4dd",
    "brightwhite": "#ffffff",
}


def _qcolor(name: str, fallback: QColor) -> QColor:
    if not name or name == "default":
        return fallback
    if len(name) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in name):
        return QColor("#" + name[:6])
    hex_color = _COLORS.get(name)
    if hex_color:
        return QColor(hex_color)
    return fallback


class TerminalView(QWidget):
    """Сетка терминала. В alternate screen высота фиксирована, иначе растёт со scrollback."""

    input_generated = Signal(str)
    resized = Signal(int, int)

    def __init__(self, font: QFont, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._font = font
        self._screen = ScreenLog(80, PTY_ROWS)
        self._interactive = False
        self._cols = 80
        self._preferred_rows = PTY_ROWS
        self._announced_cols: Optional[int] = None
        self._announced_rows: Optional[int] = None
        self._full_screen = False
        self._row_origin = 0
        self.setFont(font)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setMouseTracking(False)
        self._anchor: Optional[tuple] = None
        self._caret: Optional[tuple] = None
        self._dragging = False
        self._apply_height()

    def feed(
        self,
        data: str,
        columns: Optional[int] = None,
        rows: Optional[int] = None,
        alternate: bool = False,
    ) -> None:
        if alternate or self._screen.in_alternate_screen:
            self._full_screen = True
        elif data or columns:
            self._full_screen = False
        if columns and rows:
            columns = max(2, int(columns))
            rows = max(2, int(rows))
            if columns != self._screen.columns or rows != self._screen.lines:
                self._screen.resize(columns, rows)
                self._cols = columns
            if rows != self._preferred_rows or columns != self._announced_cols:
                self._announce_size()
        if data:
            if not self._dragging:
                self._anchor = None
                self._caret = None
            self._screen.feed(data)
            if self._screen.in_alternate_screen:
                self._full_screen = True
            elif not alternate:
                self._full_screen = False
        self._apply_height()
        self.update()

    def use_geometry(self, cols: int, rows: int) -> None:
        """Задать размер PTY по видимой области, даже если виджет ещё узкий."""
        cols = max(20, min(int(cols), 400))
        rows = max(8, min(int(rows), 80))
        self._cols = cols
        self._preferred_rows = rows
        if cols == self._announced_cols and rows == self._announced_rows:
            self._apply_height()
            return
        self._announced_cols = cols
        self._announced_rows = rows
        self.resized.emit(cols, rows)
        self._apply_height()

    def set_preferred_rows(self, rows: int) -> None:
        rows = max(8, min(int(rows), 80))
        if rows == self._preferred_rows:
            return
        self._preferred_rows = rows
        self._announce_size()

    def is_fullscreen(self) -> bool:
        return self._full_screen or self._screen.in_alternate_screen

    def plain_text(self) -> str:
        return self._screen.primary_text()

    def has_content(self) -> bool:
        rows, _cursor, _alt = self._screen.paint_rows()
        return any(_line_has_text(line) for line in rows)

    def set_interactive(self, enabled: bool) -> None:
        self._interactive = enabled
        self.setFocusPolicy(
            Qt.FocusPolicy.StrongFocus if enabled else Qt.FocusPolicy.NoFocus
        )
        self._apply_height()
        self.update()

    def event(self, ev) -> bool:  # type: ignore[override]
        if (
            self._interactive
            and ev.type() == QEvent.Type.KeyPress
            and ev.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
        ):
            self.keyPressEvent(ev)
            return True
        return super().event(ev)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._sync_columns()

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._sync_columns()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setFont(self._font)
        metrics = painter.fontMetrics()
        cell_w = self._cell_width(metrics)
        cell_h = metrics.height()
        painter.fillRect(self.rect(), _BG)

        rows, cursor, _alt = self._screen.paint_rows()
        origin = self._row_origin
        clip = event.rect()
        columns = self._screen.columns
        for y, line in enumerate(rows):
            if y < origin:
                continue
            top = (y - origin) * cell_h
            if top + cell_h < clip.top() or top > clip.bottom():
                continue
            skip = False
            for x in range(columns):
                if skip:
                    skip = False
                    continue
                char = line[x]
                data = char.data or " "
                wide = _is_wide(data)
                span = 2 if wide else 1
                fg = _qcolor(char.fg, _FG)
                bg = _qcolor(char.bg, _BG)
                if char.reverse:
                    fg, bg = bg, fg
                if self._cell_selected(y, x):
                    bg = _SELECT
                    fg = QColor("#ffffff")
                if bg != _BG:
                    painter.fillRect(x * cell_w, top, cell_w * span, cell_h, bg)
                painter.setPen(fg)
                if char.bold:
                    bold = QFont(self._font)
                    bold.setBold(True)
                    painter.setFont(bold)
                else:
                    painter.setFont(self._font)
                painter.drawText(x * cell_w, top + metrics.ascent(), data)
                if char.underscore:
                    painter.drawLine(
                        x * cell_w,
                        top + cell_h - 1,
                        (x + span) * cell_w,
                        top + cell_h - 1,
                    )
                if wide:
                    skip = True

        if self._interactive and not cursor.hidden and cursor.y >= origin:
            cx, cy = cursor.x, cursor.y - origin
            painter.fillRect(
                cx * cell_w,
                cy * cell_h,
                cell_w,
                cell_h,
                QColor(_CURSOR.red(), _CURSOR.green(), _CURSOR.blue(), 90),
            )
            painter.setPen(QPen(_CURSOR))
            painter.drawRect(cx * cell_w, cy * cell_h, cell_w - 1, cell_h - 1)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(_FRAME))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.end()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        cell = self._cell_at(event.position())
        self._anchor = cell
        self._caret = cell
        self.update()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        self._caret = self._cell_at(event.position())
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self._caret = self._cell_at(event.position())
        self._copy_selection()
        self.update()

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if self._copy_shortcut(event):
            self._copy_selection()
            event.accept()
            return
        if not self._interactive:
            super().keyPressEvent(event)
            return
        data = self._translate_key(event)
        if data:
            self.input_generated.emit(data)
            event.accept()
            return
        super().keyPressEvent(event)

    def _translate_key(self, event) -> str:
        modifiers = event.modifiers()
        key = event.key()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        alt = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)

        if ctrl and shift and key == Qt.Key.Key_V:
            return self._clipboard_text()
        if shift and key == Qt.Key.Key_Insert:
            return self._clipboard_text()

        if ctrl and not alt and Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
            return chr(key - Qt.Key.Key_A + 1)

        if key == Qt.Key.Key_Tab:
            return "\x1b[Z" if shift else "\t"
        if key == Qt.Key.Key_Backtab:
            return "\x1b[Z"
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return "\r"
        if key == Qt.Key.Key_Backspace:
            return "\x7f"
        if key == Qt.Key.Key_Escape:
            return "\x1b"

        app_cursor = self._screen.application_cursor()
        arrows = {
            Qt.Key.Key_Up: ("\x1bOA", "\x1b[A"),
            Qt.Key.Key_Down: ("\x1bOB", "\x1b[B"),
            Qt.Key.Key_Right: ("\x1bOC", "\x1b[C"),
            Qt.Key.Key_Left: ("\x1bOD", "\x1b[D"),
            Qt.Key.Key_Home: ("\x1bOH", "\x1b[H"),
            Qt.Key.Key_End: ("\x1bOF", "\x1b[F"),
        }
        if key in arrows:
            return arrows[key][0 if app_cursor else 1]
        if key == Qt.Key.Key_Delete:
            return "\x1b[3~"
        if key == Qt.Key.Key_PageUp:
            return "\x1b[5~"
        if key == Qt.Key.Key_PageDown:
            return "\x1b[6~"
        fn = _function_key(key)
        if fn:
            return fn

        text = event.text()
        if not text or not text.isprintable():
            return ""
        if alt:
            return "\x1b" + text
        return text

    def selected_text(self) -> str:
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        (row_start, col_start), (row_end, col_end) = bounds
        rows, _cursor, _alt = self._screen.paint_rows()
        lines = []
        for row in range(row_start, row_end + 1):
            if row < 0 or row >= len(rows):
                continue
            line = rows[row]
            start = col_start if row == row_start else 0
            end = col_end if row == row_end else self._screen.columns - 1
            parts = []
            column = start
            while column <= end:
                data = line[column].data or " "
                parts.append(data)
                column += 2 if _is_wide(data) else 1
            lines.append("".join(parts).rstrip())
        return "\n".join(lines).strip("\n")

    def _copy_shortcut(self, event) -> bool:
        modifiers = event.modifiers()
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if not ctrl or event.key() != Qt.Key.Key_C or not self.selected_text():
            return False
        return shift or not self._interactive

    def _copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def _cell_at(self, point) -> tuple:
        metrics = QFontMetrics(self._font)
        cell_w = self._cell_width(metrics)
        cell_h = max(1, metrics.height())
        column = max(0, int(point.x()) // cell_w)
        row = self._row_origin + max(0, int(point.y()) // cell_h)
        rows, _cursor, _alt = self._screen.paint_rows()
        if rows:
            row = min(row, len(rows) - 1)
        column = min(column, max(0, self._screen.columns - 1))
        return row, column

    def _selection_bounds(self):
        if self._anchor is None or self._caret is None or self._anchor == self._caret:
            return None
        start, end = self._anchor, self._caret
        if start > end:
            start, end = end, start
        return start, end

    def _cell_selected(self, row: int, column: int) -> bool:
        bounds = self._selection_bounds()
        if bounds is None:
            return False
        (row_start, col_start), (row_end, col_end) = bounds
        if row < row_start or row > row_end:
            return False
        if row_start == row_end:
            return col_start <= column <= col_end
        if row == row_start:
            return column >= col_start
        if row == row_end:
            return column <= col_end
        return True

    def _clipboard_text(self) -> str:
        text = QApplication.clipboard().text()
        return text.replace("\r\n", "\r").replace("\n", "\r")

    def _sync_columns(self) -> None:
        self._announce_size()

    def _announce_size(self) -> None:
        metrics = QFontMetrics(self._font)
        cell_w = self._cell_width(metrics)
        if self.width() >= 80:
            cols = max(20, self.width() // cell_w)
        else:
            cols = self._announced_cols or self._cols
        rows = self._preferred_rows
        if cols == self._announced_cols and rows == self._announced_rows:
            self._apply_height()
            return
        self._announced_cols = cols
        self._announced_rows = rows
        self._cols = cols
        self.resized.emit(cols, rows)
        self._apply_height()

    def _visible_span(self) -> tuple:
        """(первая строка, число строк).

        Полноэкранный режим (nano, less) занимает заданную высоту PTY.
        Обычный вывод — только строки с текстом, без пустого хвоста.
        """
        rows, cursor, alt = self._screen.paint_rows()
        full = alt or self._full_screen
        if full:
            self._row_origin = 0
            return max(8, self._preferred_rows)
        first = None
        last = None
        for index, line in enumerate(rows):
            if _line_has_text(line):
                if first is None:
                    first = index
                last = index
        if first is None:
            self._row_origin = 0
            return 1
        if self._interactive and cursor.y > last:
            last = cursor.y
        self._row_origin = first
        return max(1, last - first + 1)

    def _apply_height(self) -> None:
        metrics = QFontMetrics(self._font)
        count = self._visible_span()
        height = metrics.height() * count + 4
        if self.height() != height:
            self.setFixedHeight(height)

    @staticmethod
    def _cell_width(metrics: QFontMetrics) -> int:
        return max(1, metrics.horizontalAdvance("M"))


def _line_has_text(line) -> bool:
    cells = line.values() if isinstance(line, dict) else line
    return any(
        getattr(cell, "data", "") and cell.data.strip() for cell in cells
    )


def _function_key(key: int) -> str:
    """Последовательности xterm для F1–F12."""
    table = {
        Qt.Key.Key_F1: "\x1bOP",
        Qt.Key.Key_F2: "\x1bOQ",
        Qt.Key.Key_F3: "\x1bOR",
        Qt.Key.Key_F4: "\x1bOS",
        Qt.Key.Key_F5: "\x1b[15~",
        Qt.Key.Key_F6: "\x1b[17~",
        Qt.Key.Key_F7: "\x1b[18~",
        Qt.Key.Key_F8: "\x1b[19~",
        Qt.Key.Key_F9: "\x1b[20~",
        Qt.Key.Key_F10: "\x1b[21~",
        Qt.Key.Key_F11: "\x1b[23~",
        Qt.Key.Key_F12: "\x1b[24~",
    }
    return table.get(key, "")


def _is_wide(data: str) -> bool:
    if not data:
        return False
    try:
        from wcwidth import wcwidth
    except ImportError:
        return False
    return wcwidth(data[0]) == 2
