# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Стили и форматы текста для UI."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import QProxyStyle, QPushButton, QStyle, QStyleOptionButton


class VibeStyle(QProxyStyle):
    """Fusion без квадратной рамки фокуса на кнопках.

    Нативный прямоугольник фокуса рисуется поверх скруглённой кнопки.
    Для QPushButton он отключается, а фокус показывает своя рамка.
    """

    def drawPrimitive(self, element, option, painter, widget=None):  # type: ignore[override]
        if (
            element == QStyle.PrimitiveElement.PE_FrameFocusRect
            and isinstance(widget, QPushButton)
        ):
            return
        super().drawPrimitive(element, option, painter, widget)

    def drawControl(self, element, option, painter, widget=None):  # type: ignore[override]
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)
        if element == QStyle.ControlElement.CE_PushButton and focused and widget is not None:
            plain = QStyleOptionButton(option)
            plain.state = plain.state & ~QStyle.StateFlag.State_HasFocus
            super().drawControl(element, plain, painter, widget)
            self._draw_button_focus(painter, widget)
            return
        super().drawControl(element, option, painter, widget)

    @staticmethod
    def _draw_button_focus(painter: QPainter, widget: QPushButton) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        primary = widget.objectName() in {"primaryButton", "sendButton"} or widget.isDefault()
        pen = QPen(QColor("#efeaff" if primary else "#c4b5fd"))
        pen.setWidthF(2.0 if primary else 1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = widget.rect().adjusted(2, 2, -3, -3)
        painter.drawRoundedRect(rect, 8, 8)
        painter.restore()


def build_mono_font() -> QFont:
    chosen = "Consolas"
    try:
        from PySide6.QtGui import QFontDatabase

        families = set(QFontDatabase.families())
        for name in ("Cascadia Mono", "Cascadia Code", "Consolas"):
            if name in families:
                chosen = name
                break
    except Exception:
        pass
    mono = QFont(chosen)
    mono.setStyleHint(QFont.StyleHint.Monospace)
    mono.setPointSize(10)
    return mono


@dataclass(frozen=True)
class TextFormats:
    mono: QFont
    stdout: QTextCharFormat
    stderr: QTextCharFormat
    system: QTextCharFormat
    error: QTextCharFormat
    exit: QTextCharFormat


def build_text_formats() -> TextFormats:
    mono = build_mono_font()

    def make(
        color: str, *, bold: bool = False, italic: bool = False
    ) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setFont(mono)
        fmt.setForeground(QColor(color))
        if bold:
            fmt.setFontWeight(QFont.Weight.Bold)
        if italic:
            fmt.setFontItalic(True)
        return fmt

    return TextFormats(
        mono=mono,
        stdout=make("#ececf1"),
        stderr=make("#fb7185"),
        system=make("#a1a1aa", italic=True),
        error=make("#fdba74", italic=True),
        exit=make("#71717a", italic=True),
    )


STYLESHEET = """
QMainWindow, QDialog, QMessageBox {
    background: #101114;
    color: #f4f4f5;
}
QWidget#workArea, QWidget#chatPane {
    background: #101114;
    color: #f4f4f5;
}
QWidget#sideBar {
    background: #17181d;
    color: #f4f4f5;
    border-right: 1px solid #2a2b31;
}
QWidget#generalChat {
    background: #101114;
    color: #f4f4f5;
}
QWidget#mcpRail {
    background: #17181d;
    border-left: 1px solid #2a2b31;
}
QLabel#loungeTitle {
    font-size: 18px;
    font-weight: 600;
}
QFrame#loungeRule {
    background: #2a2b31;
    border: none;
    max-height: 1px;
}
QWidget#generalChat QLabel#chatUser,
QWidget#generalChat QLabel#chatAssistant {
    padding: 4px 6px;
}
QLabel#toolOutcome {
    color: #a1a1aa;
    padding: 2px 2px;
}
QLabel#loungeModel, QLabel#mcpRailTitle {
    color: #e4e4e7;
    font-weight: 600;
}
QLabel#mcpRailHint {
    color: #8b949e;
}
QLabel#loungeProblem {
    color: #f0b4b4;
}
QLabel#loungeThinking {
    color: #a1a1aa;
}
QLineEdit#loungeTitleEdit, QLineEdit#loungeDraft, QComboBox#loungeModelCombo {
    background: #1b1c22;
    color: #f4f4f5;
    border: 1px solid #34353d;
    border-radius: 8px;
    padding: 6px 8px;
}
QPlainTextEdit#loungeDraft {
    background: #1b1c22;
    color: #f4f4f5;
    border: 1px solid #34353d;
    border-radius: 8px;
    padding: 6px 8px 6px 14px;
}
QPushButton#chatTab, QPushButton#chatTabAdd {
    background: #1b1c22;
    color: #a1a1aa;
    border: 1px solid #2e2f36;
    border-bottom: none;
    border-top-left-radius: 10px;
    border-top-right-radius: 10px;
    padding: 6px 12px;
}
QPushButton#chatTab[active="true"] {
    background: #2a2740;
    color: #fafafa;
}
QPushButton#chatTabAdd {
    border-bottom: 1px solid #2e2f36;
    border-radius: 10px;
    padding: 0;
    font-size: 16px;
}
QPushButton#chatTabClose {
    background: transparent;
    color: #71717a;
    border: none;
    padding: 0;
}
QListWidget#mcpList {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget#mcpList::item {
    color: #e4e4e7;
    padding: 8px 4px;
    border-bottom: 1px solid #2a2b31;
}
QLabel {
    background: transparent;
    color: #f4f4f5;
}
QLabel#appTitle {
    color: #fafafa;
    font-size: 15px;
    font-weight: 600;
}
QLabel#sessionLabel {
    color: #a1a1aa;
    padding-left: 2px;
}
QLabel#statusBadge {
    color: #86efac;
    background: #14291c;
    border: 1px solid #1f4d32;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 11px;
    font-weight: 600;
}
QLabel#statusBadge[running="true"] {
    color: #fcd34d;
    background: #2a230f;
    border: 1px solid #5c4a16;
}
QLabel#statusBadge[closed="true"] {
    color: #a1a1aa;
    background: #23242a;
    border: 1px solid #3f3f46;
}
QLabel#statusBadge[error="true"] {
    color: #fda4af;
    background: #2a1518;
    border: 1px solid #6b2430;
}
QLabel#sidebarTitle {
    color: #a1a1aa;
    font-size: 13px;
    font-weight: 600;
}
QListWidget#sessionList, QTreeWidget#sessionList {
    background: transparent;
    border: none;
    padding: 0;
    outline: none;
}
QListWidget#sessionList::item, QTreeWidget#sessionList::item {
    background: transparent;
    padding: 1px 0;
    margin: 2px 0;
    border: none;
    color: #e4e4e7;
}
QListWidget#sessionList::item:hover,
QListWidget#sessionList::item:selected,
QTreeWidget#sessionList::item:hover,
QTreeWidget#sessionList::item:selected {
    background: transparent;
    color: #e4e4e7;
}
QTreeWidget#sessionList::branch {
    background: transparent;
    border-image: none;
    image: none;
    width: 0px;
}
QWidget#sessionRow {
    background: #17181d;
    border-radius: 10px;
}
QWidget#sessionRow[hover="true"] {
    background: #23242b;
}
QWidget#sessionRow[active="true"] {
    background: #2a2740;
}
QLabel#sessionName, QLabel#sessionMuted {
    background: transparent;
    color: #e4e4e7;
}
QLabel#sessionMuted {
    color: #8b949e;
}
QPushButton#sessionDelete {
    background: #3a1d22;
    color: #fda4af;
    border: 1px solid #6b2430;
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 12px;
}
QPushButton#sessionFold {
    background: transparent;
    color: #a1a1aa;
    border: none;
    border-radius: 6px;
    font-size: 14px;
    padding: 0;
}
QPushButton#sessionFold:hover {
    background: #2a2b31;
    color: #fafafa;
}
QScrollArea#historyScroll, QWidget#historyHost {
    background: transparent;
    border: none;
}
QFrame#hopCard, QWidget#hopCard {
    background: #1b1c22;
    border: 1px solid #2e2f36;
    border-radius: 12px;
}
QFrame#commandCard {
    background: transparent;
    border: none;
    border-radius: 0;
}
QLabel#commandIcon, QLabel#inputPrompt {
    color: #c4b5fd;
    font-weight: 700;
}
QLabel#commandText {
    color: #ddd6fe;
}
QFrame#cardSeparator {
    color: #2e2f36;
    background: #2e2f36;
    max-height: 1px;
    border: none;
}
QTextEdit#commandOutput {
    background: transparent;
    color: #ececf1;
    border: none;
    padding: 0;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QFrame#inputPanel {
    background: #1b1c22;
    border: 1px solid #34353d;
    border-radius: 14px;
}
QLineEdit#commandInput, QPlainTextEdit#commandInput {
    background: transparent;
    color: #fafafa;
    border: none;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
    padding: 4px 2px;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {
    background: #1b1c22;
    color: #fafafa;
    border: 1px solid #34353d;
    border-radius: 10px;
    padding: 7px 10px;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #8b7cf7;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #1b1c22;
    color: #fafafa;
    border: 1px solid #34353d;
    selection-background-color: #2a2740;
    outline: none;
    padding: 4px;
}
QCheckBox {
    spacing: 8px;
    color: #e4e4e7;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 5px;
    border: 1px solid #52525b;
    background: #1b1c22;
}
QCheckBox::indicator:checked {
    background: #7c6af7;
    border-color: #7c6af7;
}
QPushButton {
    border: none;
    border-radius: 10px;
    padding: 8px 12px;
    color: #fafafa;
}
QPushButton#sendButton, QPushButton#primaryButton,
QDialogButtonBox QPushButton:default {
    background: #7c6af7;
    color: white;
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#sendButton:hover, QPushButton#primaryButton:hover,
QDialogButtonBox QPushButton:default:hover {
    background: #8d7cf8;
}
QPushButton#sendButton:focus, QPushButton#primaryButton:focus,
QDialogButtonBox QPushButton:default:focus {
    background: #6d5ce8;
    border: 2px solid #efeaff;
    outline: none;
}
QDialogButtonBox QPushButton:focus {
    border: 1px solid #c4b5fd;
    outline: none;
}
QPushButton#sendButton:disabled, QPushButton#primaryButton:disabled {
    background: #2a2b31;
    color: #71717a;
}
QPushButton#stopButton {
    background: #2a2b31;
    color: #71717a;
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#stopButton:hover {
    background: #32333a;
    color: #a1a1aa;
}
QPushButton#secondaryButton, QDialogButtonBox QPushButton {
    background: #23242a;
    color: #e4e4e7;
    border: 1px solid #3f3f46;
}
QPushButton#secondaryButton:hover, QDialogButtonBox QPushButton:hover {
    background: #2c2d34;
}
QPushButton#secondaryButton:disabled {
    color: #71717a;
    border-color: #2a2b31;
}
QPushButton#copyButton, QPushButton#resendButton, QPushButton#auxButton,
QPushButton#attachButton, QPushButton#deleteButton {
    background: transparent;
    color: #d4d4d8;
    border: 1px solid #3f3f46;
    border-radius: 9px;
    padding: 5px 9px;
}
QPushButton#copyButton:hover, QPushButton#resendButton:hover, QPushButton#auxButton:hover,
QPushButton#attachButton:hover {
    background: #2a2740;
    color: #fafafa;
    border-color: #4c4670;
}
QPushButton#copyButton:disabled {
    color: #86efac;
    border-color: #1f4d32;
}
QPushButton#auxButton:disabled {
    color: #71717a;
    border-color: #2a2b31;
}
QPushButton#deleteButton {
    min-width: 28px;
    max-width: 28px;
    padding: 5px 6px;
}
QPushButton#attachButton {
    min-width: 28px;
    max-width: 28px;
    padding: 5px 6px;
}
QPushButton#deleteButton:hover {
    background: #3f1d24;
    color: #fda4af;
    border-color: #7f1d2c;
}
QPushButton#iconButton {
    background: transparent;
    color: #e4e4e7;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 4px;
}
QPushButton#iconButton:hover {
    background: #2c2d34;
    border-color: #3f3f46;
}
QPushButton#iconButton:disabled {
    background: transparent;
    border-color: transparent;
}
QFrame#offlineBanner {
    background: #2a230f;
    border: 1px solid #5c4a16;
    border-radius: 14px;
}
QLabel#offlineTitle {
    color: #fcd34d;
    font-weight: 600;
}
QLabel#offlineDetail {
    color: #f4f4f5;
}
QToolTip {
    background: #23242a;
    color: #fafafa;
    border: 1px solid #3f3f46;
    border-radius: 8px;
    padding: 6px 8px;
}
QLabel#completionHint {
    color: #a1a1aa;
    padding: 0 4px 2px 10px;
}
QListWidget#completionList {
    background: #1b1c22;
    color: #f4f4f5;
    border: 1px solid #34353d;
    border-radius: 12px;
    padding: 4px;
    outline: none;
}
QListWidget#completionList::item {
    padding: 6px 8px;
    border-radius: 8px;
}
QListWidget#completionList::item:selected {
    background: #2a2740;
    color: #fafafa;
}
QLabel#chatUser, QTextEdit#chatUser {
    background: #2a2740;
    color: #fafafa;
    border: none;
    border-radius: 14px;
    padding: 0;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QLabel#chatAssistant, QTextEdit#chatAssistant, QWidget#chatAssistant {
    background: #1b1c22;
    color: #e4e4e7;
    border: 1px solid #2e2f36;
    border-radius: 14px;
    padding: 0;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QLabel#chatPlain, QTextEdit#chatPlain {
    color: #e4e4e7;
    background: transparent;
    border: none;
    padding: 0;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QFrame#shellBlock {
    background: #121318;
    border: 1px solid #2e2f36;
    border-radius: 12px;
}
QLabel#shellCode, QTextEdit#shellCode {
    color: #ececf1;
    background: transparent;
    border: none;
    padding: 0;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QFrame#commandRequest {
    background: #1b1c22;
    border: 1px solid #3f3a66;
    border-radius: 12px;
}
QLabel#commandIntent {
    color: #e4e4e7;
}
QLineEdit#commandReason {
    background: #121318;
    color: #fafafa;
    border: 1px solid #34353d;
    border-radius: 8px;
    padding: 6px 8px;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QLabel#mcpEndpoint, QLabel#chatContext, QLabel#chatReset, QLabel#chatThinking {
    color: #a1a1aa;
    font-size: 12px;
}
QLabel#chatContext[over="true"] {
    color: #fda4af;
}
QSpinBox#chatLimit {
    background: #1b1c22;
    color: #e4e4e7;
    border: 1px solid #34353d;
    border-radius: 8px;
    padding: 2px 6px;
    min-width: 84px;
    max-width: 120px;
}
QLabel#chatReset, QLabel#chatThinking {
    font-style: italic;
    padding: 2px 4px;
}
QTextEdit#chatInput {
    background: transparent;
    color: #fafafa;
    border: none;
    border-radius: 0;
    padding: 2px;
    selection-background-color: #3f3a66;
    selection-color: #ffffff;
}
QSplitter {
    background: #101114;
}
QSplitter::handle {
    background: #2a2b31;
}
QSplitter::handle:horizontal {
    width: 1px;
}
QSplitter::handle:vertical {
    height: 1px;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 4px 2px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px 4px;
}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
    background: #3f3f46;
    border-radius: 4px;
    min-height: 24px;
    min-width: 24px;
}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
    background: #52525b;
}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
    border: none;
    height: 0;
    width: 0;
}
"""