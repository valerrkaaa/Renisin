# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Набросок оформления. Запускается отдельно от программы.

    python template.py

Справа выбираются пресет и параметры. «Скопировать конфиг» кладёт JSON в буфер:
его можно вставить в чат, чтобы перенести оформление в приложение.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)


@dataclass
class Design:
    preset: str
    canvas: str
    sidebar: str
    activity: str
    surface: str
    input: str
    border: str
    text: str
    muted: str
    accent: str
    selection: str
    danger: str
    ok: str
    radius: int
    density: str
    font: str
    mono: str
    font_size: int

    def pad(self) -> int:
        return 6 if self.density == "compact" else 10


PRESETS: dict[str, Design] = {
    "Renisin": Design(
        preset="Renisin",
        canvas="#1f1f1f",
        sidebar="#181818",
        activity="#101113",
        surface="#262626",
        input="#1f1f1f",
        border="#2e2f36",
        text="#e4e4e7",
        muted="#8b8d97",
        accent="#2f318f",
        selection="#1f2440",
        danger="#fb7185",
        ok="#86efac",
        radius=5,
        density="compact",
        font="Segoe UI",
        mono="Cascadia Mono",
        font_size=12,
    ),
    "VS Code Dark": Design(
        preset="VS Code Dark",
        canvas="#1e1e1e",
        sidebar="#252526",
        activity="#333333",
        surface="#252526",
        input="#3c3c3c",
        border="#3c3c3c",
        text="#cccccc",
        muted="#858585",
        accent="#0078d4",
        selection="#04395e",
        danger="#f14c4c",
        ok="#89d185",
        radius=0,
        density="compact",
        font="Segoe UI",
        mono="Cascadia Mono",
        font_size=13,
    ),
    "VS Code Dark Modern": Design(
        preset="VS Code Dark Modern",
        canvas="#1f1f1f",
        sidebar="#181818",
        activity="#141414",
        surface="#262626",
        input="#313131",
        border="#2b2b2b",
        text="#cccccc",
        muted="#9d9d9d",
        accent="#0078d4",
        selection="#04395e",
        danger="#f14c4c",
        ok="#89d185",
        radius=6,
        density="regular",
        font="Segoe UI",
        mono="Cascadia Mono",
        font_size=13,
    ),
    "VS Code Light": Design(
        preset="VS Code Light",
        canvas="#ffffff",
        sidebar="#f3f3f3",
        activity="#f3f3f3",
        surface="#ffffff",
        input="#ffffff",
        border="#e5e5e5",
        text="#1f1f1f",
        muted="#616161",
        accent="#005fb8",
        selection="#cce5ff",
        danger="#e51400",
        ok="#107c10",
        radius=6,
        density="regular",
        font="Segoe UI",
        mono="Cascadia Mono",
        font_size=13,
    ),
    "Quiet": Design(
        preset="Quiet",
        canvas="#1a1b1e",
        sidebar="#141517",
        activity="#101113",
        surface="#222327",
        input="#2a2b31",
        border="#2e2f36",
        text="#e4e4e7",
        muted="#8b8d97",
        accent="#7c6af7",
        selection="#2a2740",
        danger="#fb7185",
        ok="#86efac",
        radius=10,
        density="regular",
        font="Segoe UI",
        mono="Cascadia Mono",
        font_size=13,
    ),
}


def stylesheet(design: Design) -> str:
    radius = design.radius
    pad = design.pad()
    button_radius = min(radius, 8)
    return f"""
    QMainWindow, QDialog, QMessageBox {{
        background: {design.canvas};
        color: {design.text};
        font-family: "{design.font}";
        font-size: {design.font_size}px;
    }}
    QWidget#canvas {{ background: {design.canvas}; color: {design.text}; }}
    QWidget#sidebar {{
        background: {design.sidebar};
    }}
    QWidget#activity {{
        background: {design.activity};
        border-right: 1px solid {design.border};
    }}
    QWidget#inspector {{
        background: {design.sidebar};
        border-left: 1px solid {design.border};
    }}
    QWidget#statusBar {{
        background: {design.accent};
        color: white;
    }}
    QLabel {{
        background: transparent;
        color: {design.text};
        font-family: "{design.font}";
        font-size: {design.font_size}px;
    }}
    QLabel#eyebrow {{
        color: {design.muted};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.04em;
    }}
    QLabel#muted {{ color: {design.muted}; }}
    QLabel#title {{ font-size: 18px; font-weight: 600; }}
    QLabel#code {{
        color: {design.text};
        font-family: "{design.mono}";
        font-size: {design.font_size}px;
    }}
    QFrame#card, QFrame#request {{
        background: {design.surface};
        border: 1px solid {design.border};
        border-radius: {radius}px;
    }}
    QWidget#prompt, QWidget#composer {{
        background: {design.input};
        border: 1px solid {design.border};
        border-radius: {radius}px;
    }}
    QLabel#status, QLabel#ready {{
        color: {design.ok};
        font-weight: 600;
    }}
    QLabel#ready {{ font-size: 10px; }}
    QLabel#status {{ font-size: 11px; }}
    QLabel#sessionTitle, QLabel#chatTitle {{
        font-size: 13px;
        font-weight: 600;
    }}
    QLabel#arrow, QLabel#output, QLabel#note, QLabel#placeholder {{
        color: {design.muted};
    }}
    QLabel#arrow, QLabel#code, QLabel#output, QLabel#preview, QLabel#capture {{
        font-family: "{design.mono}";
    }}
    QLabel#code {{ font-weight: 600; }}
    QLabel#preview, QLabel#capture {{
        background: {design.activity};
        border-radius: {radius}px;
        padding: 6px 8px;
    }}
    QLabel#capture {{ color: {design.muted}; font-size: 11px; }}
    QLabel#user {{
        background: {design.selection};
        border-radius: {radius}px;
        padding: 8px 10px 8px 14px;
    }}
    QLabel#model, QLabel#usage {{
        background: {design.input};
        border: 1px solid {design.border};
        border-radius: {radius}px;
        padding: 4px 8px;
    }}
    QLabel#usage {{ color: {design.muted}; font-size: 11px; padding: 6px 10px; }}
    QFrame#rule {{
        background: {design.border};
        max-height: 1px;
        border: none;
    }}
    QWidget#sessionRow {{
        background: {design.sidebar};
        border-radius: {min(radius, 6)}px;
    }}
    QWidget#sessionRow[active="true"] {{ background: {design.selection}; }}
    QLabel#sessionMuted {{ color: {design.muted}; }}
    QPushButton#tool {{
        background: transparent;
        border: none;
        color: {design.muted};
        padding: 0;
        min-width: 22px;
        max-width: 22px;
        min-height: 22px;
        max-height: 22px;
    }}
    QListWidget {{
        background: transparent;
        border: none;
        outline: none;
        color: {design.text};
        font-family: "{design.font}";
        font-size: {design.font_size}px;
    }}
    QListWidget::item {{
        padding: {pad - 2}px {pad}px;
        border-radius: {min(radius, 6)}px;
    }}
    QListWidget::item:selected, QListWidget::item:hover {{
        background: {design.selection};
        color: {design.text};
    }}
    QLineEdit, QPlainTextEdit, QComboBox {{
        background: {design.input};
        color: {design.text};
        border: 1px solid {design.border};
        border-radius: {min(radius, 6)}px;
        padding: {max(4, pad - 4)}px {pad}px;
        selection-background-color: {design.selection};
    }}
    QPushButton {{
        background: {design.input};
        color: {design.text};
        border: 1px solid {design.border};
        border-radius: {button_radius}px;
        padding: {max(4, pad - 4)}px {pad + 4}px;
        font-family: "{design.font}";
    }}
    QPushButton:hover {{ border-color: {design.accent}; }}
    QPushButton#primary {{
        background: {design.accent};
        color: white;
        border: none;
        font-weight: 600;
    }}
    QPushButton#danger {{
        background: transparent;
        color: {design.danger};
        border: 1px solid {design.danger};
    }}
    QPushButton#activityButton {{
        background: transparent;
        border: none;
        color: {design.muted};
        border-radius: {button_radius}px;
        padding: 8px 0;
        font-size: 16px;
    }}
    QPushButton#activityButton:hover, QPushButton#activityButton:checked {{
        color: {design.text};
        background: {design.selection};
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {design.border};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        width: 14px;
        margin: -6px 0;
        background: {design.accent};
        border-radius: 7px;
    }}
    QDialogButtonBox QPushButton {{
        min-width: 72px;
    }}
    """


class ColorField(QWidget):
    def __init__(self, label: str, value: str, on_change) -> None:
        super().__init__()
        self._on_change = on_change
        self._value = value
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self._name = QLabel(label)
        self._name.setObjectName("muted")
        self._chip = QPushButton(value)
        self._chip.clicked.connect(self._pick)
        row.addWidget(self._name, 1)
        row.addWidget(self._chip)
        self._paint()

    def set_value(self, value: str) -> None:
        self._value = value
        self._chip.setText(value)
        self._paint()

    def _paint(self) -> None:
        color = QColor(self._value)
        ink = "#111111" if color.lightness() > 150 else "#ffffff"
        self._chip.setStyleSheet(
            f"background:{self._value}; color:{ink}; border:1px solid rgba(127,127,127,0.45);"
            "padding:4px 8px; min-width:88px;"
        )

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._value), self, self._name.text())
        if not chosen.isValid():
            return
        self.set_value(chosen.name())
        self._on_change(self._value)


class NoticeDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Новое подключение")
        self.resize(440, 220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        hint = QLabel("Модальное окно: поля и кнопки берут цвета из выбранного конфига.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        name = QLineEdit()
        name.setPlaceholderText("prod-web")
        form.addRow("Имя", name)
        secret = QLineEdit()
        secret.setEchoMode(QLineEdit.EchoMode.Password)
        secret.setPlaceholderText("Пароль")
        form.addRow("Пароль", secret)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setObjectName("primary")
            ok.setText("Создать")
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ApprovalDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Одобрение команды")
        self.resize(560, 240)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.addWidget(_request_card(self._finish))

    def _finish(self, _action: str) -> None:
        self.accept()


def _request_card(on_action) -> QFrame:
    card = QFrame()
    card.setObjectName("request")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(8)
    title = QLabel("Запрос на выполнение команды")
    title.setObjectName("chatTitle")
    layout.addWidget(title)
    where = QLabel("MCP: k8s")
    where.setObjectName("muted")
    layout.addWidget(where)
    intent = QLabel("Снять нагрузку процессора")
    intent.setWordWrap(True)
    layout.addWidget(intent)
    preview = QLabel("Команда: mpstat 1 2 | tail -n 5")
    preview.setObjectName("preview")
    layout.addWidget(preview)
    capture = QLabel("Linux 6.8.0-k8s (k8s-control-plane)\n%usr  %sys  %idle\n 4.2   1.1  94.0")
    capture.setObjectName("capture")
    layout.addWidget(capture)
    row = QHBoxLayout()
    row.setSpacing(8)
    send = QPushButton("Отправить")
    send.setObjectName("primary")
    send.clicked.connect(lambda: on_action("send"))
    redact = QPushButton("Обезличить")
    redact.clicked.connect(lambda: on_action("redact"))
    reject = QPushButton("Отклонить по причине")
    reject.clicked.connect(lambda: on_action("reject"))
    row.addWidget(send)
    row.addWidget(redact)
    row.addWidget(reject)
    row.addStretch(1)
    layout.addLayout(row)
    return card


class TemplateWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Renisin — набросок оформления")
        self.resize(1180, 780)
        self._design = PRESETS["Renisin"]
        self._colors: dict[str, ColorField] = {}
        self._lock = False

        root = QWidget()
        root.setObjectName("canvas")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        body.addWidget(self._stage(), 1)
        body.addWidget(self._inspector())
        outer.addLayout(body, 1)
        self.setCentralWidget(root)
        self._apply()

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(200)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(8, 8, 6, 8)
        layout.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(2)
        title = QLabel("СЕССИИ")
        title.setObjectName("eyebrow")
        head.addWidget(title, 1)
        for glyph in ("+", "▣", "⚙"):
            button = QPushButton(glyph)
            button.setObjectName("tool")
            head.addWidget(button)
        layout.addLayout(head)
        for text, kind in (
            ("общий чат для ИИ", "plain"),
            ("▾  кластер", "plain"),
            ("k8s-control-plane", "active"),
            ("demo", "muted"),
            ("local", "dot"),
        ):
            layout.addWidget(self._session(text, kind))
        layout.addStretch(1)
        return side

    def _session(self, text: str, kind: str) -> QWidget:
        row = QWidget()
        row.setObjectName("sessionRow")
        if kind == "active":
            row.setProperty("active", "true")
        nested = kind in ("active", "muted")
        box = QHBoxLayout(row)
        box.setContentsMargins(20 if nested else 8, 6, 8, 6)
        box.setSpacing(6)
        if kind in ("active", "dot"):
            dot = QLabel("●")
            dot.setObjectName("ready" if kind == "active" else "status")
            box.addWidget(dot)
        name = QLabel(text)
        name.setObjectName("sessionMuted" if kind == "muted" else "sessionName")
        box.addWidget(name, 1)
        if kind == "active":
            badge = QLabel("READY")
            badge.setObjectName("ready")
            box.addWidget(badge)
        return row

    def _stage(self) -> QWidget:
        stage = QWidget()
        stage.setObjectName("canvas")
        layout = QHBoxLayout(stage)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        layout.addWidget(self._terminal(), 1)
        layout.addWidget(self._chat())
        return stage

    def _terminal(self) -> QWidget:
        column = QWidget()
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        status = QLabel("READY")
        status.setObjectName("status")
        name = QLabel("k8s-control-plane")
        name.setObjectName("sessionTitle")
        gear = QPushButton("⚙")
        gear.setObjectName("tool")
        head.addWidget(status)
        head.addWidget(name)
        head.addWidget(gear)
        head.addStretch(1)
        layout.addLayout(head)
        layout.addWidget(self._command("hostname", "k8s-control-plane"))
        layout.addWidget(
            self._command(
                "cat /proc/loadavg && uptime",
                "0.07 0.03 0.00 1/564 7909\n"
                "15:18:15 up 11:52,  4 users,  load average: 0.07, 0.03, 0.00",
            )
        )
        layout.addStretch(1)
        layout.addWidget(self._field("команда…", mono=True))
        return column

    def _command(self, command: str, output: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        arrow = QLabel("❯")
        arrow.setObjectName("arrow")
        text = QLabel(command)
        text.setObjectName("code")
        head.addWidget(arrow)
        head.addWidget(text, 1)
        layout.addLayout(head)
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFixedHeight(1)
        layout.addWidget(rule)
        body = QLabel(output)
        body.setObjectName("output")
        body.setWordWrap(True)
        layout.addWidget(body)
        return card

    def _field(self, placeholder: str, mono: bool) -> QWidget:
        box = QWidget()
        box.setObjectName("prompt" if mono else "composer")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        hint = QLabel(placeholder)
        hint.setObjectName("placeholder")
        if mono:
            hint.setFont(QFont(self._design.mono, 10))
        layout.addWidget(hint, 1)
        send = QPushButton("Отправить")
        send.setObjectName("primary")
        layout.addWidget(send)
        return box

    def _chat(self) -> QWidget:
        column = QWidget()
        column.setFixedWidth(420)
        layout = QVBoxLayout(column)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Чат")
        title.setObjectName("chatTitle")
        model = QLabel("local · qwen")
        model.setObjectName("model")
        auto = QLabel("✓  Авто")
        review = QLabel("✓  Просмотр")
        usage = QLabel("1.2k / 8192")
        usage.setObjectName("usage")
        head.addWidget(title)
        head.addWidget(model)
        head.addWidget(auto)
        head.addWidget(review)
        head.addWidget(usage)
        head.addStretch(1)
        layout.addLayout(head)
        user_row = QHBoxLayout()
        user_row.addStretch(1)
        user = QLabel("Покажи аптайм и нагрузку на control-plane")
        user.setObjectName("user")
        user.setWordWrap(True)
        user.setMaximumWidth(320)
        user_row.addWidget(user)
        layout.addLayout(user_row)
        note = QLabel("выполнено: k8s\n> uptime")
        note.setObjectName("note")
        layout.addWidget(note)
        layout.addWidget(_request_card(self._on_decision))
        layout.addStretch(1)
        layout.addWidget(self._field("Сообщение модели…", mono=False))
        return column

    def _inspector(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("inspector")
        panel.setFixedWidth(320)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        title = QLabel("Параметры")
        title.setObjectName("title")
        layout.addWidget(title)
        self._note = QLabel("Выберите пресет или поменяйте цвет.")
        self._note.setObjectName("muted")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        tools = QHBoxLayout()
        confirm = QPushButton("Подтверждение")
        confirm.clicked.connect(self._ask_confirm)
        form = QPushButton("Диалог")
        form.clicked.connect(self._open_form)
        tools.addWidget(confirm)
        tools.addWidget(form)
        layout.addLayout(tools)

        self._preset = QComboBox()
        self._preset.addItems(list(PRESETS))
        self._preset.setCurrentText(self._design.preset)
        self._preset.currentTextChanged.connect(self._load_preset)
        layout.addWidget(self._preset)

        for key, label in (
            ("canvas", "Холст"),
            ("sidebar", "Боковая панель"),
            ("surface", "Карточки"),
            ("input", "Поля"),
            ("text", "Текст"),
            ("muted", "Вторичный текст"),
            ("accent", "Акцент"),
            ("selection", "Выделение"),
            ("danger", "Отказ"),
        ):
            field = ColorField(label, getattr(self._design, key), lambda value, name=key: self._set_color(name, value))
            self._colors[key] = field
            layout.addWidget(field)

        layout.addWidget(self._labeled("Скругление", self._radius_slider()))
        layout.addWidget(self._labeled("Плотность", self._density_box()))
        layout.addWidget(self._labeled("Шрифт", self._font_box()))
        layout.addWidget(self._labeled("Размер", self._size_slider()))

        copy = QPushButton("Скопировать конфиг")
        copy.setObjectName("primary")
        copy.clicked.connect(self._copy)
        layout.addWidget(copy)
        self._config = QPlainTextEdit()
        self._config.setReadOnly(True)
        self._config.setFixedHeight(160)
        layout.addWidget(self._config)
        layout.addStretch(1)
        return panel

    def _labeled(self, caption: str, widget: QWidget) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        label = QLabel(caption)
        label.setObjectName("muted")
        layout.addWidget(label)
        layout.addWidget(widget)
        return host

    def _radius_slider(self) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, 12)
        slider.setValue(self._design.radius)
        slider.valueChanged.connect(lambda value: self._patch(radius=value))
        self._radius = slider
        return slider

    def _size_slider(self) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(11, 16)
        slider.setValue(self._design.font_size)
        slider.valueChanged.connect(lambda value: self._patch(font_size=value))
        self._size = slider
        return slider

    def _density_box(self) -> QComboBox:
        box = QComboBox()
        box.addItem("Компактно", "compact")
        box.addItem("Обычно", "regular")
        box.setCurrentIndex(0 if self._design.density == "compact" else 1)
        box.currentIndexChanged.connect(
            lambda _index: self._patch(density=str(box.currentData()))
        )
        self._density = box
        return box

    def _font_box(self) -> QComboBox:
        box = QComboBox()
        box.addItems(("Segoe UI", "Inter", "system-ui"))
        box.setCurrentText(self._design.font if self._design.font in ("Segoe UI", "Inter", "system-ui") else "Segoe UI")
        box.currentTextChanged.connect(lambda name: self._patch(font=name))
        self._font = box
        return box

    def _load_preset(self, name: str) -> None:
        if self._lock or name not in PRESETS:
            return
        self._design = PRESETS[name]
        self._lock = True
        for key, field in self._colors.items():
            field.set_value(getattr(self._design, key))
        self._radius.setValue(self._design.radius)
        self._size.setValue(self._design.font_size)
        self._density.setCurrentIndex(0 if self._design.density == "compact" else 1)
        self._font.setCurrentText(self._design.font)
        self._lock = False
        self._apply()

    def _set_color(self, name: str, value: str) -> None:
        self._patch(**{name: value})

    def _patch(self, **changes) -> None:
        if self._lock:
            return
        data = asdict(self._design)
        data.update(changes)
        data["preset"] = "свой"
        self._design = Design(**data)
        self._lock = True
        if self._preset.findText("свой") < 0:
            self._preset.addItem("свой")
        self._preset.setCurrentText("свой")
        self._lock = False
        self._apply()

    def _apply(self) -> None:
        sheet = stylesheet(self._design)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(sheet)
        self.setStyleSheet(sheet)
        self._config.setPlainText(self._payload())
        self.setWindowTitle(f"Renisin — {self._design.preset}")
        self._note.setText(f"Сейчас: {self._design.preset}. Конфиг справа обновляется сразу.")

    def _payload(self) -> str:
        return json.dumps(asdict(self._design), ensure_ascii=False, indent=2)

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._payload())
        self._note.setText("Конфиг скопирован. Вставьте его в чат.")

    def _ask_confirm(self) -> None:
        answer = QMessageBox.question(
            self,
            "Сбросить мастер-пароль",
            "Подключения, которые открываются мастер-паролем, перестанут работать, "
            "пока пароль не задан снова.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        chosen = "да" if answer == QMessageBox.StandardButton.Yes else "нет"
        self._note.setText(f"Подтверждение: {chosen}.")

    def _open_form(self) -> None:
        NoticeDialog(self).exec()

    def _open_approval(self) -> None:
        ApprovalDialog(self).exec()

    def _on_decision(self, action: str) -> None:
        labels = {"send": "отправить", "redact": "обезличить", "reject": "отклонить"}
        self._note.setText(f"Запрос команды: {labels.get(action, action)}.")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = TemplateWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
