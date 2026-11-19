# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Текст, который переносится по ширине колонки и не раздвигает её."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QFont, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QWidget,
)


class FittedText(QTextEdit):
    """Только чтение. Длинная строка переносится, ширина колонки не растёт."""

    def __init__(self, text: str = "", font: QFont | None = None) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.document().setDocumentMargin(0)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        if font is not None:
            self.setFont(font)
        self.setPlainText(text)
        self._fitting = False
        self._scroll_limit = 0
        self._scroll_lines = 0
        self._chain_at_edge = False
        self.viewport().installEventFilter(self)
        self.viewport().setAutoFillBackground(False)
        self.setFixedHeight(self.fontMetrics().lineSpacing() + 4)

    def text(self) -> str:
        return self.toPlainText()

    def minimumSizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, self.fontMetrics().lineSpacing())

    def sizeHint(self) -> QSize:  # type: ignore[override]
        return QSize(0, max(self.fontMetrics().lineSpacing(), self.height()))

    def eventFilter(self, obj, event) -> bool:  # type: ignore[override]
        if obj is self.viewport() and event.type() == QEvent.Type.Wheel:
            if self._chain_at_edge and self.verticalScrollBar().maximum() > 0:
                scroll_or_chain(self, event)
                return True
            if self.verticalScrollBar().maximum() > 0:
                return False
            return _forward_wheel(self, event)
        return super().eventFilter(obj, event)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.fit_to_width()

    def fit_to_width(self) -> None:
        if self._fitting:
            return
        if self._scroll_lines:
            fit_limited_height(self, self._scroll_lines)
            return
        width = self.viewport().width()
        if width < 20:
            return
        self._fitting = True
        try:
            document = self.document()
            document.setTextWidth(width)
            chrome = max(0, self.height() - self.viewport().height())
            height = max(
                self.fontMetrics().lineSpacing(),
                math.ceil(document.size().height()) + chrome + 2,
            )
            limit = self._scroll_limit
            if limit and height > limit:
                height = limit
                self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            else:
                self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            if self.height() != height:
                self.setFixedHeight(int(height))
        finally:
            self._fitting = False

    def chain_scroll_at_edge(self) -> None:
        """Длинный текст крутится внутри. Жест вверх с верхнего края — у родителя."""
        self._chain_at_edge = True

    def set_scroll_limit(self, limit: int) -> None:
        """Выше этого предела текст крутится внутри блока, а не растягивает ленту."""
        self._scroll_limit = max(0, int(limit))
        self.fit_to_width()

    def set_scroll_lines(self, lines: int) -> None:
        """Выше этого числа строк появляется прокрутка."""
        self._scroll_lines = max(0, int(lines))
        self.fit_to_width()


def clamp_scroll_host(scroll: QScrollArea) -> None:
    """Колонка чата не шире видимой области, даже если внутри длинная строка."""
    host = scroll.widget()
    if host is None:
        return
    width = scroll.viewport().width()
    if width > 0:
        host.setMinimumWidth(0)
        host.setMaximumWidth(width)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


def sync_scroll_extent(scroll: QScrollArea) -> None:
    """Полоса прокрутки достаёт до низа содержимого, а не до старой высоты окна."""
    host = scroll.widget()
    if host is None:
        return
    layout = host.layout()
    if layout is None:
        return
    layout.invalidate()
    hint = max(layout.sizeHint().height(), scroll.viewport().height())
    if host.minimumHeight() != hint:
        host.setMinimumHeight(hint)
        host.updateGeometry()


def _block_content_height(widget: QWidget) -> int:
    """Высота текста по блокам. document.size() у QPlainTextEdit врёт и даёт пустой скролл."""
    document = widget.document()
    margin = int(document.documentMargin()) if hasattr(document, "documentMargin") else 0
    metrics = widget.fontMetrics()
    line = max(1, metrics.lineSpacing())
    if isinstance(widget, QPlainTextEdit):
        height = 0.0
        block = document.firstBlock()
        while block.isValid():
            rect = widget.blockBoundingRect(block)
            height += rect.height() if rect.height() > 0 else line
            block = block.next()
        if height <= 0:
            height = line
        return int(math.ceil(height)) + margin * 2
    width = widget.viewport().width()
    if width > 20:
        document.setTextWidth(width)
    raw = int(math.ceil(float(document.size().height())))
    return max(line + margin * 2, raw)


def fit_limited_height(widget: QWidget, max_lines: int) -> int:
    """Высота по тексту до max_lines строк. Дальше появляется прокрутка."""
    if getattr(widget, "_fitting", False):
        return widget.height()
    widget._fitting = True
    try:
        metrics = widget.fontMetrics()
        line = max(1, metrics.lineSpacing())
        document = widget.document()
        margin = int(document.documentMargin()) if hasattr(document, "documentMargin") else 0
        chrome = widget.contentsMargins().top() + widget.contentsMargins().bottom()
        chrome += widget.frameWidth() * 2
        needed = _block_content_height(widget) + chrome
        limit = line * max(1, int(max_lines)) + margin * 2 + chrome
        needed = max(line + margin * 2 + chrome, needed)
        overflow = needed > limit + 1
        height = limit if overflow else needed
        widget._fits_without_scroll = not overflow
        widget.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if overflow
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        if widget.height() != height:
            widget.setFixedHeight(int(height))
        if not overflow and widget.verticalScrollBar().maximum() > 0:
            # Пара пикселей округления, без раздувания блока под пустое место.
            extra = min(int(widget.verticalScrollBar().maximum()), line)
            grown = min(int(height + extra), int(limit))
            if grown != height:
                widget.setFixedHeight(grown)
                height = grown
            if widget.verticalScrollBar().maximum() > 0:
                widget.verticalScrollBar().setValue(0)
        return int(height)
    finally:
        widget._fitting = False


_WHEEL_GESTURE_GAP = 0.35


def scroll_or_chain(widget: QWidget, event) -> None:
    """Крутит сам блок. Жест вверх, начатый у верхнего края, уходит родителю.

    Жест, который только доехал до края, родителя не двигает.
    """
    bar = widget.verticalScrollBar()
    if getattr(widget, "_fits_without_scroll", False):
        _forward_wheel(widget, event)
        return
    delta = event.angleDelta().y()
    pixels = event.pixelDelta().y()
    upward = delta > 0 or (delta == 0 and pixels > 0)
    at_top = bar.value() <= bar.minimum()
    if _new_wheel_gesture(widget, event):
        widget._gesture_chains = upward and at_top
    if getattr(widget, "_gesture_chains", False) and upward:
        _forward_wheel(widget, event)
        return
    if bar.maximum() > bar.minimum():
        if delta:
            steps = delta / 120.0
            bar.setValue(int(bar.value() - steps * max(bar.singleStep(), 20) * 3))
        elif pixels:
            bar.setValue(bar.value() - pixels)
    event.accept()


def _new_wheel_gesture(widget: QWidget, event) -> bool:
    phase = event.phase()
    now = time.monotonic()
    previous = getattr(widget, "_wheel_at", 0.0)
    if phase == Qt.ScrollPhase.ScrollBegin:
        fresh = True
    elif phase in (Qt.ScrollPhase.ScrollUpdate, Qt.ScrollPhase.ScrollEnd):
        fresh = False
    else:
        fresh = now - previous > _WHEEL_GESTURE_GAP
    widget._wheel_at = now
    return fresh


def _forward_wheel(widget: QWidget, event) -> bool:
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        return False
    event.ignore()
    QApplication.sendEvent(parent.viewport(), event)
    return True
