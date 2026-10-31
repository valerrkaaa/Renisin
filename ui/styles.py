# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Стили и форматы текста для UI."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QTextCharFormat
from PySide6.QtWidgets import QProxyStyle, QPushButton, QStyle, QStyleOptionButton

# Выбранный конфиг оформления. Плотность compact, радиус 5, шрифт 12px.
CANVAS = "#1f1f1f"
SIDEBAR = "#181818"
ACTIVITY = "#101113"
SURFACE = "#262626"
INPUT = "#1f1f1f"
BORDER = "#2e2f36"
TEXT = "#e4e4e7"
MUTED = "#8b8d97"
FAINT = "#6b6d78"
ACCENT = "#2f318f"
ACCENT_HOVER = "#5d5ea8"
ACCENT_PRESS = "#272875"
FOCUS = "#b6b7d8"
SELECTION = "#1f2440"
DANGER = "#fb7185"
OK = "#86efac"
RADIUS = 5
FONT_SIZE = 12
FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Cascadia Mono"


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
        primary = widget.objectName() in {"primaryButton", "sendButton", "composerSend"} or widget.isDefault()
        if primary:
            painter.restore()
            return
        pen = QPen(QColor(FOCUS))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = widget.rect().adjusted(1, 1, -2, -2)
        painter.drawRoundedRect(rect, RADIUS, RADIUS)
        painter.restore()


def build_mono_font() -> QFont:
    chosen = "Consolas"
    try:
        from PySide6.QtGui import QFontDatabase

        families = set(QFontDatabase.families())
        for name in (MONO_FAMILY, "Cascadia Code", "Consolas"):
            if name in families:
                chosen = name
                break
    except Exception:
        pass
    mono = QFont(chosen)
    mono.setStyleHint(QFont.StyleHint.Monospace)
    mono.setPixelSize(FONT_SIZE)
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
        stdout=make(MUTED),
        stderr=make(DANGER),
        system=make(MUTED, italic=True),
        error=make("#fdba74", italic=True),
        exit=make(FAINT, italic=True),
    )


STYLESHEET = f"""
QMainWindow, QDialog, QMessageBox {{
    background: {CANVAS};
    color: {TEXT};
}}
QWidget#workArea, QWidget#chatPane {{
    background: {CANVAS};
    color: {TEXT};
}}
QWidget#sideBar {{
    background: {SIDEBAR};
    color: {TEXT};
    border-right: 1px solid {BORDER};
}}
QWidget#generalChat {{
    background: {CANVAS};
    color: {TEXT};
}}
QWidget#mcpRail {{
    background: {SIDEBAR};
    border-left: 1px solid {BORDER};
}}
QLabel#loungeTitle {{
    font-size: 15px;
    font-weight: 600;
}}
QFrame#loungeRule {{
    background: {BORDER};
    border: none;
    max-height: 1px;
}}
QWidget#generalChat QLabel#chatUser,
QWidget#generalChat QLabel#chatAssistant {{
    padding: 2px 4px;
}}
QLabel#toolCall {{
    background: transparent;
    color: {MUTED};
    border: none;
    padding: 2px 2px;
}}
QLabel#toolOutcome, QTextEdit#toolOutcome {{
    background: transparent;
    color: {MUTED};
    border: none;
    padding: 2px 2px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLabel#loungeTitle {{
    font-size: 16px;
    font-weight: 600;
}}
QLabel#loungeModelCaption {{
    color: {MUTED};
}}
QLabel#loungeModel {{
    color: {TEXT};
}}
QLabel#mcpRailTitle {{
    color: {FAINT};
    font-size: 11px;
    font-weight: 700;
}}
QLabel#mcpRailHint {{
    color: {MUTED};
}}
QLabel#loungeProblem {{
    color: {DANGER};
}}
QLabel#loungeThinking {{
    color: {MUTED};
}}
QLineEdit#loungeTitleEdit, QLineEdit#loungeDraft, QComboBox#loungeModelCombo {{
    background: {INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px 8px;
}}
QComboBox#chatModelCombo, QComboBox#loungeModelCombo {{
    font-size: 11px;
    padding: 4px 8px;
}}
QPlainTextEdit#loungeDraft {{
    background: transparent;
    color: {TEXT};
    border: none;
    border-radius: 0;
    padding: 2px 2px;
}}
QWidget#loungeTabBar {{
    border-bottom: 1px solid {BORDER};
    min-height: 46px;
}}
QWidget#loungeHead {{
    border-bottom: 1px solid {BORDER};
}}
QPushButton#chatTab, QPushButton#chatTabAdd {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 12px 8px;
    font-size: 13px;
}}
QPushButton#chatTab[active="true"] {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
    border-bottom: 2px solid {ACCENT_HOVER};
}}
QPushButton#chatTabAdd {{
    border-bottom: none;
    border-radius: {RADIUS}px;
    padding: 0;
    color: {FAINT};
    font-size: 18px;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
}}
QPushButton#chatTabAdd:hover {{
    background: #222328;
    color: {TEXT};
}}
QPushButton#chatTabClose {{
    background: transparent;
    color: {FAINT};
    border: none;
    padding: 0;
}}
QTabWidget::pane {{
    background: {CANVAS};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    top: -1px;
}}
QTabBar::tab {{
    background: {SIDEBAR};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS}px;
    border-top-right-radius: {RADIUS}px;
    padding: 4px 10px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{
    background: {CANVAS};
    color: {TEXT};
}}
QTabBar::tab:hover {{
    color: {TEXT};
}}
QListWidget#mcpList {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget#mcpList::item {{
    color: {TEXT};
    padding: 8px 10px;
    border-bottom: none;
    border-radius: {RADIUS}px;
}}
QListWidget#mcpList::item:hover {{
    background: #222328;
}}
QLabel {{
    background: transparent;
    color: {TEXT};
}}
QLabel#appTitle {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 600;
}}
QLabel#sessionLabel, QLabel#chatTitle {{
    color: {TEXT};
    font-size: 13px;
    font-weight: 600;
    padding-left: 2px;
}}
QLabel#requestTitle {{
    color: {TEXT};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#statusBadge {{
    color: {OK};
    background: transparent;
    border: none;
    padding: 0;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#statusBadge[running="true"] {{
    color: #fcd34d;
}}
QLabel#statusBadge[closed="true"] {{
    color: {MUTED};
}}
QLabel#statusBadge[error="true"] {{
    color: {DANGER};
}}
QLabel#sidebarTitle {{
    color: {FAINT};
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.06em;
}}
QPushButton#headerAction {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 0;
}}
QPushButton#headerAction:hover {{
    background: #222328;
}}
QListWidget#sessionList, QTreeWidget#sessionList {{
    background: transparent;
    border: none;
    padding: 0;
    outline: none;
}}
QListWidget#sessionList::item, QTreeWidget#sessionList::item {{
    background: transparent;
    padding: 1px 0;
    margin: 1px 0;
    border: none;
    color: {TEXT};
}}
QListWidget#sessionList::item:hover,
QListWidget#sessionList::item:selected,
QTreeWidget#sessionList::item:hover,
QTreeWidget#sessionList::item:selected {{
    background: transparent;
    color: {TEXT};
}}
QTreeWidget#sessionList::branch {{
    background: transparent;
    border-image: none;
    image: none;
    width: 0px;
}}
QWidget#sessionRow {{
    background: transparent;
    border: none;
}}
QWidget#sessionRow QLabel {{
    font-size: 14px;
    background: transparent;
}}
QLabel#pinnedLabel {{
    color: {TEXT};
    font-size: 14px;
    font-weight: 600;
}}
QFrame#treeDivider {{
    background: {BORDER};
    border: none;
    max-height: 1px;
}}
QLabel#folderTitle {{
    color: {MUTED};
    font-weight: 600;
}}
QLabel#connectionTitle {{
    color: {TEXT};
    font-weight: 600;
}}
QLabel#sessionName, QLabel#sessionMuted {{
    background: transparent;
    color: {TEXT};
}}
QLabel#sessionMuted {{
    color: {FAINT};
}}
QLabel#sessionOffline {{
    color: {DANGER};
}}
QLabel#sessionRunning {{
    color: {OK};
}}
QPushButton#sessionDelete {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-radius: 3px;
    padding: 0;
    font-size: 14px;
}}
QPushButton#sessionDelete:hover {{
    background: #3a1d22;
    color: {DANGER};
}}
QPushButton#sessionFold {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-radius: {RADIUS}px;
    font-size: 12px;
    padding: 0;
}}
QPushButton#sessionFold:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
QPushButton#sessionAdd {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-radius: {RADIUS}px;
    font-size: 14px;
    font-weight: 600;
    padding: 0;
}}
QPushButton#sessionAdd:hover {{
    background: {SURFACE};
    color: {TEXT};
}}
QScrollArea#historyScroll, QWidget#historyHost {{
    background: transparent;
    border: none;
}}
QFrame#hopCard, QWidget#hopCard {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#hopIndex {{
    color: {MUTED};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#chainRoute {{
    color: {TEXT};
    font-weight: 600;
}}
QPlainTextEdit#commandCapture {{
    background: {ACTIVITY};
    color: {MUTED};
    border: none;
    border-radius: {RADIUS}px;
    padding: 6px 8px;
    font-size: 11px;
}}
QPlainTextEdit#commandCapture:disabled {{
    background: {ACTIVITY};
    color: {MUTED};
}}
QFrame#commandCard {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#commandIcon, QLabel#inputPrompt {{
    color: {MUTED};
    font-weight: 600;
    padding: 0;
}}
QLabel#inputPrompt {{
    padding-top: 4px;
}}
QLabel#commandText, QTextEdit#commandText, QTextEdit#commandTarget {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
    border: none;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QFrame#commandDivider {{
    background: transparent;
    border: none;
    min-height: 0;
    max-height: 0;
    margin: 0;
}}
QFrame#cardSeparator {{
    color: {BORDER};
    background: {BORDER};
    max-height: 1px;
    border: none;
}}
QTextEdit#commandOutput {{
    background: transparent;
    color: {MUTED};
    border: none;
    padding: 0 0 0 16px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QFrame#inputPanel {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QWidget#generalChat QFrame#inputPanel QPlainTextEdit,
QWidget#chatPane QFrame#inputPanel QTextEdit {{
    background: transparent;
    border: none;
}}
QLineEdit#commandInput, QPlainTextEdit#commandInput {{
    background: transparent;
    color: {TEXT};
    border: none;
    selection-background-color: {SELECTION};
    selection-color: #ffffff;
    padding: 6px 2px;
}}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTextEdit {{
    background: {INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px 8px;
    selection-background-color: {SELECTION};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {FOCUS};
}}
QComboBox::drop-down {{
    border: none;
    width: 18px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {SELECTION};
    outline: none;
    padding: 2px;
}}
QCheckBox {{
    spacing: 6px;
    color: {TEXT};
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border-radius: 3px;
    border: 1px solid {BORDER};
    background: {INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QPushButton {{
    border: none;
    border-radius: {RADIUS}px;
    padding: 4px 10px;
    color: {TEXT};
}}
QPushButton#sendButton, QPushButton#composerSend, QPushButton#primaryButton,
QDialogButtonBox QPushButton:default {{
    background: {ACCENT};
    color: white;
    font-weight: 600;
    border: none;
    padding: 6px 10px;
}}
QPushButton#sendButton:hover, QPushButton#composerSend:hover, QPushButton#primaryButton:hover,
QDialogButtonBox QPushButton:default:hover {{
    background: {ACCENT_HOVER};
}}
QPushButton#sendButton:focus, QPushButton#composerSend:focus, QPushButton#primaryButton:focus,
QDialogButtonBox QPushButton:default:focus {{
    background: {ACCENT_HOVER};
    outline: none;
}}
QMessageBox QPushButton, QDialogButtonBox QPushButton {{
    padding: 4px 14px;
    min-width: 92px;
}}
QPushButton#sendButton:disabled, QPushButton#composerSend:disabled, QPushButton#primaryButton:disabled {{
    background: {SURFACE};
    color: {FAINT};
}}
QPushButton#stopButton {{
    background: {SURFACE};
    color: {FAINT};
    font-weight: 600;
    padding: 4px 12px;
}}
QPushButton#stopButton:hover {{
    background: {INPUT};
    color: {MUTED};
}}
QPushButton#secondaryButton, QDialogButtonBox QPushButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
}}
QPushButton#secondaryButton:hover, QDialogButtonBox QPushButton:hover {{
    background: {INPUT};
}}
QPushButton#secondaryButton:disabled {{
    color: {FAINT};
    border-color: {BORDER};
}}
QPushButton#auxButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    font-weight: 600;
    padding: 6px 10px;
}}
QPushButton#auxButton:hover {{
    background: {SELECTION};
    color: {TEXT};
    border-color: {ACCENT};
}}
QPushButton#resendButton,
QPushButton#attachButton, QPushButton#deleteButton,
QPushButton#toolButton {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-radius: 4px;
    padding: 0;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
}}
QPushButton#resendButton:hover,
QPushButton#attachButton:hover, QPushButton#toolButton:hover {{
    background: {SELECTION};
    color: {TEXT};
}}
QPushButton#toolButton:checked {{
    background: {SELECTION};
    color: {TEXT};
}}
QPushButton#toolButton:disabled {{
    color: {FAINT};
    background: transparent;
}}
QPushButton#usageButton {{
    background: {INPUT};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 6px 10px;
    font-size: 11px;
}}
QPushButton#usageButton:hover {{
    background: {SELECTION};
    color: {TEXT};
}}
QPushButton#usageButton[over="true"] {{
    color: {DANGER};
    border-color: #6b2430;
}}
QFrame#limitPopup {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QPushButton#auxButton:disabled {{
    color: {FAINT};
    border-color: {BORDER};
}}
QPushButton#deleteButton:hover {{
    background: #3a1d22;
    color: {DANGER};
}}
QPushButton#iconButton {{
    background: transparent;
    color: {TEXT};
    border: 1px solid transparent;
    border-radius: {RADIUS}px;
    padding: 2px;
}}
QPushButton#iconButton:hover {{
    background: {SURFACE};
    border-color: {BORDER};
}}
QPushButton#iconButton:disabled {{
    background: transparent;
    border-color: transparent;
}}
QFrame#offlineBanner {{
    background: #2a230f;
    border: 1px solid #5c4a16;
    border-radius: {RADIUS}px;
}}
QLabel#offlineTitle {{
    color: #fcd34d;
    font-weight: 600;
}}
QLabel#offlineDetail {{
    color: {TEXT};
}}
QToolTip {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px 6px;
}}
QLabel#completionHint {{
    color: {MUTED};
    padding: 0 4px 2px 8px;
}}
QListWidget#completionList {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 2px;
    outline: none;
}}
QListWidget#completionList::item {{
    padding: 4px 8px;
    border-radius: 3px;
}}
QListWidget#completionList::item:selected {{
    background: {SELECTION};
    color: {TEXT};
}}
QLabel#chatUser, QTextEdit#chatUser {{
    background: {SELECTION};
    color: {TEXT};
    border: 1px solid {ACCENT};
    border-radius: 14px;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QWidget#generalChat QLabel#chatUser, QWidget#generalChat QTextEdit#chatUser {{
    background: {ACCENT};
    color: #ffffff;
    border: none;
    border-radius: 14px;
}}
QLabel#chatAssistant, QTextEdit#chatAssistant, QWidget#chatAssistant {{
    background: transparent;
    color: {TEXT};
    border: none;
    border-radius: 0;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QWidget#generalChat QLabel#chatAssistant, QWidget#generalChat QTextEdit#chatAssistant {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QLabel#chatPlain, QTextEdit#chatPlain {{
    color: {TEXT};
    background: transparent;
    border: none;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QFrame#shellBlock {{
    background: {ACTIVITY};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QTextEdit#shellCodeRich {{
    background: transparent;
    border: none;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QFrame#commandRequest QLabel#shellCode,
QFrame#commandRequest QTextEdit#shellCode {{
    color: {TEXT};
    background: {ACTIVITY};
    border: none;
    border-radius: {RADIUS}px;
    padding: 6px 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLabel#shellCode, QTextEdit#shellCode {{
    color: {TEXT};
    background: transparent;
    border: none;
    padding: 0;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QFrame#commandRequest {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#commandRequest[review="true"] {{
    border: 1px solid {ACCENT};
}}
QFrame#chatHead {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel#chatTitle {{
    color: {TEXT};
    background: transparent;
    font-size: 14px;
    font-weight: 600;
}}
QLabel#assistantMark {{
    background: {ACCENT};
    color: #ffffff;
    border-radius: 9px;
    font-size: 12px;
}}
QLabel#chatHint, QLabel#chatModelName {{
    color: {MUTED};
    font-size: 12px;
    background: transparent;
}}
QFrame#chatHead QComboBox#chatModelCombo,
QFrame#chatHead QPushButton#auxButton,
QFrame#chatHead QPushButton#usageButton {{
    background: {INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 2px 12px;
    min-height: 26px;
}}
QFrame#chatHead QPushButton#usageButton {{
    color: {MUTED};
    font-size: 12px;
}}
QFrame#chatHead QPushButton#auxButton:hover,
QFrame#chatHead QPushButton#usageButton:hover {{
    background: #222328;
    border-color: {ACCENT_HOVER};
}}
QCheckBox#autoRun::indicator, QCheckBox#reviewOutput::indicator {{
    width: 30px;
    height: 17px;
    border-radius: 8px;
    background: {SURFACE};
    border: 1px solid {BORDER};
}}
QCheckBox#autoRun::indicator:checked, QCheckBox#reviewOutput::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}
QFrame#chatHead QCheckBox#autoRun, QFrame#chatHead QCheckBox#reviewOutput {{
    color: {MUTED};
    background: {INPUT};
    border: 1px solid {BORDER};
    border-radius: 11px;
    padding: 2px 10px 2px 8px;
    min-height: 26px;
}}
QFrame#chatHead QCheckBox#autoRun:checked, QFrame#chatHead QCheckBox#reviewOutput:checked {{
    color: {TEXT};
    background: {SELECTION};
    border-color: {ACCENT};
}}
QWidget#callNote {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QWidget#callNote[variant="lounge"] {{
    background: {SIDEBAR};
}}
QLabel#noteMcp, QLabel#noteChevron {{
    color: {FAINT};
    background: transparent;
}}
QLabel#notePrompt {{
    color: {ACCENT_HOVER};
    background: transparent;
}}
QWidget#callNote QLabel#toolCall {{
    color: {TEXT};
    background: transparent;
}}
QLabel#noteBadge {{
    background: {SELECTION};
    color: {TEXT};
    border: 1px solid {ACCENT};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#noteBadge[kind="bad"] {{
    background: {CANVAS};
    color: {MUTED};
    border: 1px solid {BORDER};
}}
QWidget#toolCallBody {{
    background: transparent;
}}
QLabel#commandIntent {{
    color: {TEXT};
}}
QLabel#commandTarget {{
    color: {MUTED};
}}
QLineEdit#commandReason {{
    background: {INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 4px 8px;
    selection-background-color: {SELECTION};
    selection-color: #ffffff;
}}
QLabel#mcpEndpoint, QLabel#chatContext, QLabel#chatReset, QLabel#chatThinking, QTextEdit#chatReset {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#chatContext[over="true"] {{
    color: {DANGER};
}}
QSpinBox#chatLimit {{
    background: {INPUT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    padding: 2px 6px;
    min-width: 84px;
    max-width: 120px;
}}
QLabel#chatReset, QLabel#chatThinking, QTextEdit#chatReset {{
    font-style: italic;
    padding: 2px 4px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QTextEdit#chatInput {{
    background: transparent;
    color: {TEXT};
    border: none;
    border-radius: 0;
    padding: 6px 2px;
    selection-background-color: {SELECTION};
    selection-color: #ffffff;
}}
QLabel#chatModelName {{
    color: {TEXT};
    font-weight: 600;
    padding: 2px 2px 0 2px;
}}
QLabel#chatModelHint, QLabel#chatModelEmpty {{
    color: {MUTED};
}}
QLabel#chatPlaceholder {{
    color: {MUTED};
    background: transparent;
    padding: 0;
}}
QMenu {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 4px 16px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background: {SELECTION};
}}
QSplitter {{
    background: {CANVAS};
}}
QSplitter::handle {{
    background: {BORDER};
}}
QSplitter::handle:horizontal {{
    width: 1px;
}}
QSplitter::handle:vertical {{
    height: 1px;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px 2px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: #3a3b44;
    border-radius: 4px;
    min-height: 24px;
    min-width: 24px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: #4e5060;
}}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
    border: none;
    height: 0;
    width: 0;
}}
"""
