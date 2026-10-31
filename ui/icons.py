# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Небольшие монохромные иконки для кнопок окна."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pixmap(draw, color: str = "#e4e4e7") -> QIcon:
    size = 32
    image = QPixmap(size, size)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color))
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
        painter.setBrush(QColor("#e4e4e7"))
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
        painter.save()
        painter.translate(16, 16)
        for angle in range(0, 360, 45):
            painter.save()
            painter.rotate(angle)
            painter.drawRoundedRect(-2, -14, 4, 5, 1, 1)
            painter.restore()
        painter.drawEllipse(-8, -8, 16, 16)
        painter.drawEllipse(-3, -3, 6, 6)
        painter.restore()

    return _pixmap(draw)


def plus_icon() -> QIcon:
    """Плюс: новая сессия."""

    def draw(painter: QPainter) -> None:
        painter.drawLine(16, 8, 16, 24)
        painter.drawLine(8, 16, 24, 16)

    return _pixmap(draw)


def terminal_icon() -> QIcon:
    """Терминал: строка подключения, не папка."""

    def draw(painter: QPainter) -> None:
        painter.drawRoundedRect(7, 8, 18, 16, 2, 2)
        painter.drawLine(11, 14, 14, 17)
        painter.drawLine(14, 17, 11, 20)
        painter.drawLine(16, 20, 21, 20)

    return _pixmap(draw)


def chevron_icon() -> QIcon:
    """Узкая стрелка вправо. Раскрытый узел поворачивает её в коде строки."""

    def draw(painter: QPainter) -> None:
        painter.drawLine(12, 7, 21, 16)
        painter.drawLine(21, 16, 12, 25)

    return _pixmap(draw, "#8b8d97")


def tree_folder_icon() -> QIcon:
    """Папка в списке иерархии."""

    def draw(painter: QPainter) -> None:
        painter.drawLine(8, 13, 14, 13)
        painter.drawLine(14, 13, 16, 10)
        painter.drawLine(16, 10, 24, 10)
        painter.drawRoundedRect(8, 13, 16, 11, 2, 2)

    return _pixmap(draw, "#8b8d97")


def connection_node_icon() -> QIcon:
    """Подключение: колба с контактами сверху."""

    def draw(painter: QPainter) -> None:
        scale = 32 / 24
        painter.drawLine(9 * scale, 2 * scale, 9 * scale, 7 * scale)
        painter.drawLine(15 * scale, 2 * scale, 15 * scale, 7 * scale)
        path = QPainterPath()
        path.moveTo(6.5 * scale, 7 * scale)
        path.lineTo(17.5 * scale, 7 * scale)
        path.lineTo(17.5 * scale, 9.5 * scale)
        path.arcTo(6.5 * scale, 4 * scale, 11 * scale, 11 * scale, 0, -180)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(12 * scale, 15 * scale, 12 * scale, 22 * scale)

    return _pixmap(draw, "#8b8d97")


def chat_icon() -> QIcon:
    """Чат внутри подключения."""

    def draw(painter: QPainter) -> None:
        painter.drawEllipse(5, 5, 20, 16)
        painter.drawLine(9, 20, 7, 26)
        painter.drawLine(7, 26, 14, 20)

    return _pixmap(draw, "#8b8d97")


def sparkles_icon() -> QIcon:
    """Общий чат."""

    def draw(painter: QPainter) -> None:
        def star(cx: float, cy: float, rx: float, ry: float) -> None:
            painter.drawLine(cx, cy - ry, cx + rx, cy)
            painter.drawLine(cx + rx, cy, cx, cy + ry)
            painter.drawLine(cx, cy + ry, cx - rx, cy)
            painter.drawLine(cx - rx, cy, cx, cy - ry)

        star(14, 13, 6, 8)
        star(23, 22, 3, 4)

    return _pixmap(draw, "#8b8d97")


def folder_icon() -> QIcon:
    """Папка: новая группа."""

    def draw(painter: QPainter) -> None:
        painter.drawRoundedRect(7, 12, 18, 12, 2, 2)
        painter.drawLine(7, 14, 13, 14)
        painter.drawLine(13, 14, 15, 11)
        painter.drawLine(15, 11, 24, 11)

    return _pixmap(draw)


def sidebar_icon(side: str) -> QIcon:
    """Рамка окна с полосой слева или справа, как переключатель панели."""

    def draw(painter: QPainter) -> None:
        painter.drawRoundedRect(6, 7, 20, 18, 2, 2)
        if side == "right":
            painter.drawLine(19, 8, 19, 24)
        else:
            painter.drawLine(13, 8, 13, 24)

    return _pixmap(draw)


def close_icon() -> QIcon:
    """Крестик: закрыть сессию."""

    def draw(painter: QPainter) -> None:
        painter.drawLine(11, 11, 21, 21)
        painter.drawLine(21, 11, 11, 21)

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
