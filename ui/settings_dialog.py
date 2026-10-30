# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Настройки приложения: адрес MCP, ключ SSH, модели и правила команд."""
from __future__ import annotations

import ipaddress
import os
from typing import Optional

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from application import ApplicationService
from ui.buttons import russian_box
from mcp_host import (
    DEFAULT_MCP_HOST,
    DEFAULT_MCP_PORTS,
    parse_mcp_host,
    parse_mcp_ports,
)
from ui.models_dialog import ModelsDialog
from ui.rules_dialog import RulesDialog


class SettingsDialog(QDialog):
    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("Настройки")
        self.setModal(True)
        self.resize(920, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(12)

        tabs = QTabWidget()
        tabs.addTab(self._mcp_page(), "MCP")
        tabs.addTab(self._key_page(), "Ключ SSH")
        tabs.addTab(ModelsDialog(service, self), "Модели")
        tabs.addTab(RulesDialog(service, self), "Правила")
        layout.addWidget(tabs, 1)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        russian_box(close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)
        self._fill_mcp()
        self._fill_key()

    def _mcp_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(10)

        hint = QLabel(
            "Программа занимает один свободный порт из списка. "
            "Если он занят другим окном, берётся следующий. "
            "Все консоли ходят на этот один адрес, поэтому число портов "
            "не ограничивает число сессий."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(hint)

        form = QFormLayout()
        self._host = QLineEdit()
        self._host.setObjectName("mcpHost")
        self._host.setPlaceholderText(DEFAULT_MCP_HOST)
        self._host.textChanged.connect(self._refresh_exposure)
        form.addRow("IP", self._host)
        self._ports = QLineEdit()
        self._ports.setObjectName("mcpPorts")
        self._ports.setPlaceholderText(DEFAULT_MCP_PORTS)
        form.addRow("Порты", self._ports)
        layout.addLayout(form)

        self._exposure = QLabel(
            "Этот адрес доступен в сети, не только на этом компьютере."
        )
        self._exposure.setWordWrap(True)
        self._exposure.setStyleSheet("color:#ffd166;")
        self._exposure.setVisible(False)
        layout.addWidget(self._exposure)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setObjectName("mcpStatus")
        self._status.setStyleSheet("color:#8b8d97;")
        layout.addWidget(self._status)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setObjectName("mcpError")
        self._error.setStyleSheet("color:#fb7185;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        apply = QPushButton("Применить")
        apply.setObjectName("primaryButton")
        apply.clicked.connect(self._apply_mcp)
        layout.addWidget(apply)
        layout.addStretch(1)
        return page

    def _fill_mcp(self) -> None:
        reader = getattr(self._service, "mcp_listen", None)
        host, ports = (DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS)
        if callable(reader):
            loaded = reader()
            if loaded and len(loaded) == 2:
                host, ports = loaded
        self._host.setText(host)
        self._ports.setText(ports)
        endpoint = ""
        getter = getattr(self._service, "mcp_endpoint", None)
        if callable(getter):
            endpoint = getter() or ""
        self._status.setText(self._status_text(endpoint))
        self._refresh_exposure()

    def _apply_mcp(self) -> None:
        try:
            host = parse_mcp_host(self._host.text())
            ports_text = self._ports.text().strip()
            ports = parse_mcp_ports(ports_text)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        updater = getattr(self._service, "update_mcp_listen", None)
        if not callable(updater):
            self._show_error("MCP-сервер не подключён")
            return
        try:
            endpoint = updater(host, ports, ports_text)
        except Exception as exc:
            self._show_error(str(exc) or "Не удалось занять порт MCP")
            return
        self._host.setText(host)
        self._ports.setText(ports_text)
        self._show_error("")
        self._status.setText(self._status_text(endpoint))
        self._refresh_exposure()

    def _refresh_exposure(self) -> None:
        visible = False
        try:
            host = parse_mcp_host(self._host.text())
            visible = not ipaddress.ip_address(host).is_loopback
        except ValueError:
            visible = False
        self._exposure.setVisible(visible)

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(bool(message))

    @staticmethod
    def _status_text(endpoint: str) -> str:
        if not endpoint:
            return "Сервер MCP сейчас не слушает порт."
        return (
            f"Слушает {endpoint}. Адрес конкретной консоли — в её настройках. "
            "После смены порта обновите его в клиенте MCP."
        )

    def _key_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.setSpacing(10)

        master_title = QLabel("Мастер-пароль")
        master_title.setStyleSheet("font-weight:600;")
        layout.addWidget(master_title)
        self._master_hint = QLabel(
            "Без мастер-пароля нельзя добавить ключ SSH."
            "Он же открывает журналы локальной консоли и входа по ключу."
        )
        self._master_hint.setWordWrap(True)
        self._master_hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(self._master_hint)

        form_master = QFormLayout()
        self._master_form = form_master
        self._master_password = QLineEdit()
        self._master_password.setObjectName("masterPassword")
        self._master_password.setEchoMode(QLineEdit.EchoMode.Password)
        self._master_password.returnPressed.connect(self._set_master)
        form_master.addRow("Мастер-пароль", self._master_password)
        self._master_confirm = QLineEdit()
        self._master_confirm.setObjectName("masterConfirm")
        self._master_confirm.setEchoMode(QLineEdit.EchoMode.Password)
        self._master_confirm.returnPressed.connect(self._set_master)
        form_master.addRow("Подтверждение", self._master_confirm)
        layout.addLayout(form_master)

        self._master_status = QLabel("")
        self._master_status.setWordWrap(True)
        self._master_status.setObjectName("masterStatus")
        self._master_status.setStyleSheet("color:#8b8d97;")
        layout.addWidget(self._master_status)
        self._master_error = QLabel("")
        self._master_error.setWordWrap(True)
        self._master_error.setObjectName("masterError")
        self._master_error.setStyleSheet("color:#fb7185;")
        self._master_error.setVisible(False)
        layout.addWidget(self._master_error)

        master_buttons = QHBoxLayout()
        self._master_set = QPushButton("Задать")
        self._master_set.setObjectName("primaryButton")
        self._master_set.clicked.connect(self._set_master)
        self._master_reset = QPushButton("Сбросить")
        self._master_reset.setObjectName("secondaryButton")
        self._master_reset.clicked.connect(self._reset_master)
        master_buttons.addWidget(self._master_set)
        master_buttons.addWidget(self._master_reset)
        master_buttons.addStretch(1)
        layout.addLayout(master_buttons)

        key_title = QLabel("Ключ SSH")
        key_title.setStyleSheet("font-weight:600;")
        layout.addWidget(key_title)
        hint = QLabel(
            "Этот ключ записывается на хост, если при новом SSH-подключении "
            "включена галочка «Записать мой ключ». Пока ключ не указан, "
            "такое подключение сообщит, что нужна первичная настройка."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(hint)

        form = QFormLayout()
        self._key_path = QLineEdit()
        self._key_path.setObjectName("sshKeyPath")
        self._key_path.setPlaceholderText(r"C:\Users\you\.ssh\id_ed25519")
        browse = QPushButton("…")
        browse.setObjectName("sshKeyBrowse")
        browse.setFixedWidth(34)
        browse.clicked.connect(self._browse_key)
        self._key_browse = browse
        pair = QWidget()
        pair_layout = QHBoxLayout(pair)
        pair_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.setSpacing(6)
        pair_layout.addWidget(self._key_path, 1)
        pair_layout.addWidget(browse)
        form.addRow("Приватный ключ", pair)
        self._key_passphrase = QLineEdit()
        self._key_passphrase.setObjectName("sshKeyPassphrase")
        self._key_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_passphrase.setPlaceholderText("Пусто — мастер-пароль")
        form.addRow("Passphrase", self._key_passphrase)
        layout.addLayout(form)

        self._key_status = QLabel("")
        self._key_status.setWordWrap(True)
        self._key_status.setObjectName("sshKeyStatus")
        self._key_status.setStyleSheet("color:#8b8d97;")
        layout.addWidget(self._key_status)
        self._key_error = QLabel("")
        self._key_error.setWordWrap(True)
        self._key_error.setObjectName("sshKeyError")
        self._key_error.setStyleSheet("color:#fb7185;")
        self._key_error.setVisible(False)
        layout.addWidget(self._key_error)

        buttons = QHBoxLayout()
        create = QPushButton("Создать ключ")
        create.setObjectName("secondaryButton")
        create.clicked.connect(self._create_key)
        self._key_create = create
        apply = QPushButton("Применить")
        apply.setObjectName("primaryButton")
        apply.clicked.connect(self._apply_key)
        self._key_apply = apply
        buttons.addWidget(create)
        buttons.addWidget(apply)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return page

    def _master_ready(self) -> bool:
        ready = getattr(self._service, "has_master_password", None)
        if not callable(ready):
            return True
        return bool(ready())

    def _fill_key(self) -> None:
        reader = getattr(self._service, "ssh_key_path", None)
        path = reader() if callable(reader) else ""
        self._key_path.setText(path or "")
        self._refresh_master()

    def _refresh_master(self) -> None:
        ready = self._master_ready()
        self._master_password.setVisible(not ready)
        self._master_confirm.setVisible(not ready)
        self._master_set.setVisible(not ready)
        self._master_reset.setVisible(ready)
        for field in (self._master_password, self._master_confirm):
            label = self._master_form.labelForField(field)
            if label is not None:
                label.setVisible(not ready)
        if ready:
            self._master_status.setText(
                "Мастер-пароль задан на этот запуск. Сброс закроет доступ "
                "к подключениям, которые им открываются. Ключ SSH останется."
            )
        else:
            self._master_status.setText("Мастер-пароль не задан. Ключ SSH добавить нельзя.")
        path = self._key_path.text().strip()
        if path:
            self._key_status.setText("Ключ установлен.")
        else:
            self._key_status.setText("Ключ не задан.")
        for widget in (
            self._key_path,
            self._key_browse,
            self._key_passphrase,
            self._key_create,
            self._key_apply,
        ):
            widget.setEnabled(ready)

    def _set_master(self) -> None:
        setter = getattr(self._service, "set_master_password", None)
        if not callable(setter):
            self._show_master_error("Мастер-пароль не подключён")
            return
        try:
            setter(self._master_password.text(), self._master_confirm.text())
        except Exception as exc:
            self._show_master_error(str(exc) or "Не удалось задать мастер-пароль")
            return
        self._master_password.clear()
        self._master_confirm.clear()
        self._show_master_error("")
        self._refresh_master()

    def _reset_master(self) -> None:
        answer = QMessageBox.question(
            self,
            "Сбросить мастер-пароль",
            "Подключения, которые открываются мастер-паролем, перестанут работать, "
            "пока пароль не задан снова. Ключ SSH и его passphrase останутся.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        reset = getattr(self._service, "reset_master_password", None)
        if not callable(reset):
            self._show_master_error("Мастер-пароль не подключён")
            return
        try:
            reset()
        except Exception as exc:
            self._show_master_error(str(exc) or "Не удалось сбросить мастер-пароль")
            return
        self._show_master_error("")
        self._fill_key()

    def _show_master_error(self, message: str) -> None:
        self._master_error.setText(message)
        self._master_error.setVisible(bool(message))

    def _browse_key(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, "Приватный ключ SSH")
        if path:
            self._key_path.setText(path)

    def _create_key(self) -> None:
        path = self._key_path.text().strip()
        if not path:
            path = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
            self._key_path.setText(path)
        if not self._ensure_master():
            return
        maker = getattr(self._service, "generate_ssh_key", None)
        if not callable(maker):
            self._show_key_error("Хранилище ключа не подключено")
            return
        try:
            maker(path, self._key_passphrase.text())
        except TypeError:
            try:
                maker(path)
            except Exception as exc:
                self._show_key_error(str(exc) or "Не удалось создать ключ")
                return
        except Exception as exc:
            self._show_key_error(str(exc) or "Не удалось создать ключ")
            return
        self._key_passphrase.clear()
        self._show_key_error("")
        self._key_status.setText("Ключ создан и сохранён.")
        self._refresh_master()

    def _ensure_master(self) -> bool:
        ready = getattr(self._service, "has_master_password", None)
        if callable(ready) and ready():
            return True
        from ui.dialogs import MasterPasswordDialog

        dialog = MasterPasswordDialog(self._service, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._show_key_error("Сначала задайте мастер-пароль")
            return False
        self._show_master_error("")
        self._refresh_master()
        return True

    def _apply_key(self) -> None:
        saver = getattr(self._service, "set_ssh_key_path", None)
        if not callable(saver):
            self._show_key_error("Хранилище ключа не подключено")
            return
        if self._key_path.text().strip() and not self._ensure_master():
            return
        try:
            saver(self._key_path.text())
        except Exception as exc:
            self._show_key_error(str(exc) or "Не удалось сохранить ключ")
            return
        self._show_key_error("")
        self._fill_key()

    def _show_key_error(self, message: str) -> None:
        self._key_error.setText(message)
        self._key_error.setVisible(bool(message))
