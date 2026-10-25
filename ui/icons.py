# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Небольшие монохромные иконки для кнопок окна."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def _pixmap(draw) -> QIcon:
    size = 32
    image = QPixmap(size, size)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#d5deea"))
    pen.setWidthF(1.8)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter)
    painter.end()
    return QIcon(image)


def paste_icon() -> QIcon:
    """Буфер обмена."""

    def draw(painter: QPainter) -> None:
        painter.drawRoundedRect(9, 11, 14, 14, 2, 2)
        painter.drawLine(12, 8, 20, 8)
        painter.drawLine(12, 8, 12, 12)
        painter.drawLine(20, 8, 20, 12)

    return _pixmap(draw)


def paste_run_icon() -> QIcon:
    """Буфер обмена и немедленный запуск."""

    def draw(painter: QPainter) -> None:
        painter.drawRoundedRect(6, 11, 12, 13, 2, 2)
        painter.drawLine(9, 8, 15, 8)
        painter.drawLine(9, 8, 9, 12)
        painter.drawLine(15, 8, 15, 12)
        painter.setBrush(QColor("#d5deea"))
        painter.drawPolygon(
            [
                QPoint(20, 12),
                QPoint(20, 24),
                QPoint(28, 18),
            ]
        )

    return _pixmap(draw)


def gear_icon() -> QIcon:
    """Шестерёнка настроек."""

    def draw(painter: QPainter) -> None:
        painter.drawEllipse(11, 11, 10, 10)
        painter.drawEllipse(14, 14, 4, 4)
        for angle in range(0, 360, 45):
            painter.save()
            painter.translate(16, 16)
            painter.rotate(angle)
            painter.drawLine(0, -9, 0, -13)
            painter.restore()

    return _pixmap(draw)


def pencil_icon() -> QIcon:
    """Карандаш."""

    def draw(painter: QPainter) -> None:
        painter.drawLine(10, 22, 20, 10)
        painter.drawLine(20, 10, 23, 13)
        painter.drawLine(23, 13, 13, 25)
        painter.drawLine(13, 25, 10, 22)
        painter.drawLine(18, 12, 21, 15)
        painter.drawLine(11, 23, 9, 26)

    return _pixmap(draw)
