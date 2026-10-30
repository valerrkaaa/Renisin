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
    QListWidget,
    QListWidgetItem,
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
    "vterminal": Design(
        preset="vterminal",
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
        border-right: 1px solid {design.border};
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
        letter-spacing: 0.08em;
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
    QFrame#request {{ border-left: 3px solid {design.accent}; }}
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
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    eyebrow = QLabel("ЗАПРОС КОМАНДЫ")
    eyebrow.setObjectName("eyebrow")
    layout.addWidget(eyebrow)
    intent = QLabel("Проверить свободное место на диске")
    intent.setWordWrap(True)
    layout.addWidget(intent)
    where = QLabel("prod-web  ·  MCP")
    where.setObjectName("muted")
    layout.addWidget(where)
    code = QLabel("df -h")
    code.setObjectName("code")
    layout.addWidget(code)
    row = QHBoxLayout()
    run = QPushButton("Выполнить")
    run.setObjectName("primary")
    run.clicked.connect(lambda: on_action("run"))
    rule = QPushButton("Добавить правило")
    rule.clicked.connect(lambda: on_action("rule"))
    reject = QPushButton("Отклонить")
    reject.setObjectName("danger")
    reject.clicked.connect(lambda: on_action("reject"))
    row.addWidget(run)
    row.addWidget(rule)
    row.addWidget(reject)
    row.addStretch(1)
    layout.addLayout(row)
    return card


class TemplateWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("vterminal — набросок оформления")
        self.resize(1180, 780)
        self._design = PRESETS["vterminal"]
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
        body.addWidget(self._activity())
        body.addWidget(self._sidebar())
        body.addWidget(self._stage(), 1)
        body.addWidget(self._inspector())
        outer.addLayout(body, 1)
        outer.addWidget(self._status())
        self.setCentralWidget(root)
        self._apply()

    def _activity(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("activity")
        bar.setFixedWidth(48)
        layout = QVBoxLayout(bar)
        layout.setContentsMargins(4, 8, 4, 8)
        layout.setSpacing(4)
        for glyph, checked in (("⌘", True), ("≡", False), ("⚙", False)):
            button = QPushButton(glyph)
            button.setObjectName("activityButton")
            button.setCheckable(True)
            button.setChecked(checked)
            button.setFixedHeight(36)
            layout.addWidget(button)
        layout.addStretch(1)
        return bar

    def _sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(240)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)
        title = QLabel("ПОДКЛЮЧЕНИЯ")
        title.setObjectName("eyebrow")
        layout.addWidget(title)
        sessions = QListWidget()
        sessions.addItems(("Общий чат", "prod-web", "jump → app", "локальная CMD"))
        sessions.setCurrentRow(1)
        layout.addWidget(sessions, 1)
        return side

    def _stage(self) -> QWidget:
        stage = QWidget()
        stage.setObjectName("canvas")
        layout = QVBoxLayout(stage)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(14)
        title = QLabel("Превью")
        title.setObjectName("title")
        layout.addWidget(title)
        lead = QLabel(
            "Слева список, по центру запрос команды и карточка вывода, "
            "справа параметры. Всплывающие окна используют тот же конфиг."
        )
        lead.setObjectName("muted")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        actions = QHBoxLayout()
        confirm = QPushButton("Подтверждение")
        confirm.clicked.connect(self._ask_confirm)
        form = QPushButton("Диалог")
        form.clicked.connect(self._open_form)
        approve = QPushButton("Окно одобрения")
        approve.setObjectName("primary")
        approve.clicked.connect(self._open_approval)
        actions.addWidget(confirm)
        actions.addWidget(form)
        actions.addWidget(approve)
        actions.addStretch(1)
        layout.addLayout(actions)

        self._note = QLabel("Выберите пресет или поменяйте цвет.")
        self._note.setObjectName("muted")
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        layout.addWidget(_request_card(self._on_decision))

        done = QFrame()
        done.setObjectName("card")
        card = QVBoxLayout(done)
        card.setContentsMargins(14, 12, 14, 12)
        card.setSpacing(6)
        header = QLabel("КОМАНДА")
        header.setObjectName("eyebrow")
        card.addWidget(header)
        command = QLabel("uname -srm")
        command.setObjectName("code")
        card.addWidget(command)
        output = QLabel("Linux 6.8.0-45-generic x86_64")
        output.setObjectName("muted")
        card.addWidget(output)
        layout.addWidget(done)

        rules_label = QLabel("СПИСОК ПРАВИЛ")
        rules_label.setObjectName("eyebrow")
        layout.addWidget(rules_label)
        rules = QListWidget()
        rules.addItems(
            (
                "Разрешено    ls, df, uname",
                "Спросить     systemctl, apt",
                "Запрещено    rm -rf, mkfs",
            )
        )
        rules.setFixedHeight(120)
        layout.addWidget(rules)
        layout.addStretch(1)
        return stage

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

    def _status(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("statusBar")
        bar.setFixedHeight(26)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        self._status_label = QLabel(self._design.preset)
        self._status_label.setStyleSheet("color:white; background:transparent;")
        layout.addWidget(self._status_label)
        layout.addStretch(1)
        return bar

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
        self._status_label.setText(self._design.preset)
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
        labels = {"run": "выполнить", "rule": "правило", "reject": "отклонить"}
        self._note.setText(f"Запрос команды: {labels.get(action, action)}.")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = TemplateWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
