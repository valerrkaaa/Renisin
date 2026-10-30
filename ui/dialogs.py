# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Диалоги создания и разблокировки сессий.

Диалоги сами вызывают сценарии ApplicationService, чтобы при ошибке
подключения (например, «connection refused») оставаться открытыми,
показывать сообщение и давать поправить поля и повторить попытку.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import domain
from application import ApplicationService, SessionUnavailable
from application.master_password import connection_journal_password


class _HopFields(QWidget):
    """Одна строка узла: адрес, порт, логин, тип входа и секрет."""

    remove_requested = Signal(object)
    changed = Signal()

    def __init__(self, removable: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("hopCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel("Узел")
        self.title.setObjectName("hopIndex")
        self.title.setFixedWidth(52)
        head.addWidget(self.title)
        self.host = QLineEdit()
        self.host.setPlaceholderText("хост или IP")
        head.addWidget(self.host, 2)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.port.setFixedWidth(78)
        self.port.setToolTip("Порт SSH")
        head.addWidget(self.port)
        self.user = QLineEdit()
        self.user.setPlaceholderText("логин")
        self.user.setFixedWidth(110)
        head.addWidget(self.user)
        self.auth = QComboBox()
        self.auth.addItem("Пароль", domain.SSHAuthMethod.PASSWORD.value)
        self.auth.addItem("Ключ", domain.SSHAuthMethod.PRIVATE_KEY.value)
        self.auth.addItem("Агент", domain.SSHAuthMethod.AGENT.value)
        self.auth.setFixedWidth(108)
        self.auth.setToolTip("Способ входа на этот узел")
        head.addWidget(self.auth)
        self.remove_button = QPushButton("−")
        self.remove_button.setObjectName("auxButton")
        self.remove_button.setFixedWidth(34)
        self.remove_button.setAutoDefault(False)
        self.remove_button.setToolTip("Убрать этот узел из цепочки")
        self.remove_button.setVisible(removable)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        head.addWidget(self.remove_button)
        layout.addLayout(head)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пароль SSH")
        self.key_path = QLineEdit()
        self.key_path.setPlaceholderText("Файл приватного ключа")
        browse = QPushButton("…")
        browse.setFixedWidth(34)
        browse.setAutoDefault(False)
        browse.clicked.connect(self._browse_key)
        self._key_row = QWidget()
        key_layout = QHBoxLayout(self._key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.key_path, 1)
        key_layout.addWidget(browse)
        self.passphrase = QLineEdit()
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("Passphrase")
        self.passphrase.setFixedWidth(140)
        self._secret_host = QWidget()
        secret = QHBoxLayout(self._secret_host)
        secret.setContentsMargins(58, 0, 0, 0)
        secret.setSpacing(6)
        secret.addWidget(self.password, 1)
        secret.addWidget(self._key_row, 1)
        secret.addWidget(self.passphrase)
        layout.addWidget(self._secret_host)
        self.install_key = QCheckBox("Записать мой ключ")
        self.install_key.setToolTip(
            "Если хост уже пускает ключом из настроек, пароль не нужен. "
            "Иначе ключ будет добавлен в authorized_keys."
        )
        layout.addWidget(self.install_key)

        self.host.textChanged.connect(lambda _text: self.changed.emit())
        self.user.textChanged.connect(lambda _text: self.changed.emit())
        self.port.valueChanged.connect(lambda _value: self.changed.emit())
        self.auth.currentIndexChanged.connect(self._on_auth_changed)
        self._on_auth_changed()

    def set_index(self, index: int, removable: bool) -> None:
        self.title.setText(f"Узел {index}")
        self.remove_button.setVisible(removable)

    def endpoint(self) -> str:
        host = self.host.text().strip() or "…"
        user = self.user.text().strip()
        port = int(self.port.value())
        text = f"{user}@{host}" if user else host
        if port != 22:
            text = f"{text}:{port}"
        return text

    def auth_method(self) -> domain.SSHAuthMethod:
        return domain.SSHAuthMethod(self.auth.currentData())

    def secret(self) -> str:
        method = self.auth_method()
        if method == domain.SSHAuthMethod.PASSWORD:
            return self.password.text()
        if method == domain.SSHAuthMethod.PRIVATE_KEY:
            return self.passphrase.text()
        return ""

    def wants_install(self) -> bool:
        return (
            self.auth_method() == domain.SSHAuthMethod.PASSWORD
            and self.install_key.isChecked()
        )

    def clear_secret(self) -> None:
        self.password.clear()
        self.passphrase.clear()

    def _on_auth_changed(self) -> None:
        method = self.auth.currentData()
        is_password = method == domain.SSHAuthMethod.PASSWORD.value
        is_key = method == domain.SSHAuthMethod.PRIVATE_KEY.value
        self.password.setVisible(is_password)
        self._key_row.setVisible(is_key)
        self.passphrase.setVisible(is_key)
        self._secret_host.setVisible(is_password or is_key)
        self.install_key.setVisible(is_password)
        if not is_password:
            self.install_key.setChecked(False)
        self.changed.emit()

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите файл приватного ключа")
        if path:
            self.key_path.setText(path)


class _ChainPasswordRow(QWidget):
    """Строка входа в уже известный узел: логин@адрес и пароль либо подпись о ключе."""

    def __init__(
        self,
        username: str,
        host: str,
        port: int = 22,
        auth_method: domain.SSHAuthMethod = domain.SSHAuthMethod.PASSWORD,
        private_key_path: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._host = host.strip()
        self._user = username.strip()
        self._port = port
        self._auth = auth_method
        self._key_path = (private_key_path or "").strip() or None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        shown = f"{self._user}@{self._host}"
        if port and port != 22:
            shown = f"{shown}:{port}"
        self.endpoint = QLabel(shown)
        self.endpoint.setObjectName("chainEndpoint")
        self.endpoint.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.endpoint)
        self.password: Optional[QLineEdit] = None
        self.note: Optional[QLabel] = None
        if auth_method == domain.SSHAuthMethod.PASSWORD:
            self.password = QLineEdit()
            self.password.setObjectName("chainPassword")
            self.password.setEchoMode(QLineEdit.EchoMode.Password)
            self.password.setPlaceholderText("Пароль")
            layout.addWidget(self.password, 1)
        else:
            text = (
                "Используется ключ"
                if auth_method == domain.SSHAuthMethod.PRIVATE_KEY
                else "Используется SSH-агент"
            )
            self.note = QLabel(text)
            self.note.setObjectName("chainAuthNote")
            self.note.setStyleSheet("color:#8b8d97;")
            layout.addWidget(self.note, 1)

    def auth_method(self) -> domain.SSHAuthMethod:
        return self._auth

    def key_path(self) -> Optional[str]:
        return self._key_path

    def secret(self) -> str:
        if self.password is None:
            return ""
        return self.password.text()

    def values(self) -> tuple[str, str, str]:
        return self._host, self._user, self.secret()

    def clear_secret(self) -> None:
        if self.password is not None:
            self.password.clear()


@dataclass
class NewSessionRequest:
    name: str
    journal_password: str
    provider: domain.ProviderType
    ssh: Optional[domain.SSHConfig]
    auth_secret: Optional[str]
    hop_secrets: tuple[str, ...] = ()
    llm_profile_id: Optional[str] = None
    shell_family: str = domain.SHELL_UNIX
    install_key: tuple[bool, ...] = ()


class MasterPasswordDialog(QDialog):
    """Первый мастер-пароль за запуск. Остаётся только в памяти."""

    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("Мастер-пароль")
        self.setModal(True)
        self.resize(460, 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(10)
        hint = QLabel(
            "Нужен для журналов без пароля подключения: локальная консоль "
            "и SSH по ключу. Вводится один раз за запуск и остаётся только в памяти."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b8d97;")
        layout.addWidget(hint)

        form = QFormLayout()
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.returnPressed.connect(self._on_ok)
        form.addRow("Мастер-пароль", self._password)
        layout.addLayout(form)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color:#fb7185; font-weight:600;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def password(self) -> str:
        return self._service.master_password()

    def _on_ok(self) -> None:
        password = self._password.text()
        try:
            self._service.set_master_password(password, password)
        except ValueError as exc:
            self._error.setText(str(exc))
            self._error.setVisible(True)
            return
        self._password.clear()
        self.accept()


def ask_journal_password(
    service: ApplicationService,
    provider: domain.ProviderType,
    ssh: Optional[domain.SSHConfig],
    auth_secret: Optional[str],
    hop_secrets: tuple[str, ...],
    parent: Optional[QWidget] = None,
    install_key: tuple[bool, ...] = (),
) -> Optional[str]:
    """Пароль журнала: пароль последней машины или мастер-пароль этого запуска."""
    bound = connection_journal_password(
        provider, ssh, auth_secret, hop_secrets, install_key
    )
    if bound:
        return bound
    if service.has_master_password():
        return service.master_password()
    dialog = MasterPasswordDialog(service, parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.password()


def _prove_master_password(service: ApplicationService) -> None:
    prove = getattr(service, "prove_master_password", None)
    if callable(prove):
        prove()


def _forget_unproven_master_password(service: ApplicationService) -> None:
    forget = getattr(service, "forget_unproven_master_password", None)
    if callable(forget):
        forget()


class _SessionCallDialog(QDialog):
    """Диалог, который ждёт create/open вне GUI-потока."""

    _call_done = Signal(object, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._busy = False
        self._call_done.connect(self._finish_call)

    def _finish_call(self, info: object, error: object) -> None:
        raise NotImplementedError

    def reject(self) -> None:  # type: ignore[override]
        if self._busy:
            return
        super().reject()

    def _fit_to_screen(self, width: int, height: int) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(width, height)
            return
        area = screen.availableGeometry()
        self.setMaximumHeight(max(420, area.height() - 48))
        self.setMaximumWidth(max(480, area.width() - 48))
        self.resize(min(width, self.maximumWidth()), min(height, self.maximumHeight()))

    def _run_off_ui(self, fn: Callable[[], object]) -> None:
        self._set_busy(True, "Подключение…")

        def work() -> None:
            info = None
            error = None
            try:
                info = fn()
            except Exception as exc:
                error = exc
            self._call_done.emit(info, error)

        threading.Thread(target=work, daemon=True).start()


class CreateSessionDialog(_SessionCallDialog):
    """Форма создания сессии для CMD и SSH.

    При ошибке подключения диалог не закрывается: показывает сообщение
    в нижней части окна и даёт изменить параметры и повторить.
    """

    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._created: Optional[domain.SessionInfo] = None

        self.setWindowTitle("Новая сессия")
        self.setModal(True)
        self._fit_to_screen(560, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)
        form_host = QWidget()
        layout = QVBoxLayout(form_host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        info = QLabel(
            "История шифруется паролем последней машины. "
            "Локальная консоль и вход по ключу используют мастер-пароль: "
            "он спрашивается один раз за запуск и остаётся только в памяти."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#8b8d97;")
        layout.addWidget(info)

        # --- Общие поля ---------------------------------------------------

        basic_form = QFormLayout()
        basic_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.provider_combo = QComboBox()
        self.provider_combo.addItem("CMD (локальная)", "cmd")
        self.provider_combo.addItem("SSH", "ssh")
        self.provider_combo.addItem("SSH через SSH", "ssh_over_ssh")
        self.provider_combo.addItem("LLM", "llm")
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        basic_form.addRow("Тип", self.provider_combo)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Например: Рабочая CMD")
        basic_form.addRow("Имя сессии", self.name_edit)

        self.shell_combo = QComboBox()
        self.shell_combo.addItem("linux/macOS", domain.SHELL_UNIX)
        self.shell_combo.addItem("windows", domain.SHELL_WINDOWS)
        self.shell_combo.addItem("other", domain.SHELL_OTHER)
        self.shell_combo.setToolTip(
            "Как делить команду в запросе. Для SSH через SSH — ОС последнего узла."
        )
        basic_form.addRow("ОС", self.shell_combo)
        self._shell_hint = QLabel("Для цепочки это ОС последнего узла, где выполняются команды.")
        self._shell_hint.setWordWrap(True)
        self._shell_hint.setStyleSheet("color:#8b8d97;")
        basic_form.addRow("", self._shell_hint)

        layout.addLayout(basic_form)

        self._llm_container = QWidget()
        llm_form = QFormLayout(self._llm_container)
        llm_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.profile_combo = QComboBox()
        llm_form.addRow("Модель", self.profile_combo)
        self._llm_empty = QLabel(
            "Сначала добавьте модель в настройках."
        )
        self._llm_empty.setWordWrap(True)
        self._llm_empty.setStyleSheet("color:#8b8d97;")
        llm_form.addRow("", self._llm_empty)
        self.terminal_combo = QComboBox()
        self.terminal_combo.addItem("CMD (локальная)", "cmd")
        self.terminal_combo.addItem("SSH", "ssh")
        self.terminal_combo.addItem("SSH через SSH", "ssh_over_ssh")
        self.terminal_combo.currentIndexChanged.connect(self._on_provider_changed)
        llm_form.addRow("Терминал", self.terminal_combo)
        layout.addWidget(self._llm_container)
        self._fill_profiles()

        # --- SSH поля -----------------------------------------------------

        self.ssh_host = QLineEdit()
        self.ssh_host.setPlaceholderText("example.com или 10.0.0.5")

        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)

        self.ssh_user = QLineEdit()
        self.ssh_user.setPlaceholderText("ubuntu")

        self.ssh_auth = QComboBox()
        self.ssh_auth.addItem("Пароль", domain.SSHAuthMethod.PASSWORD.value)
        self.ssh_auth.addItem(
            "Приватный ключ", domain.SSHAuthMethod.PRIVATE_KEY.value
        )
        self.ssh_auth.addItem(
            "SSH-агент", domain.SSHAuthMethod.AGENT.value
        )
        self.ssh_auth.currentIndexChanged.connect(self._on_auth_changed)

        self.ssh_password = QLineEdit()
        self.ssh_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_password.setPlaceholderText("Пароль SSH")

        self.ssh_key_path = QLineEdit()
        self.ssh_key_browse = QPushButton("…")
        self.ssh_key_browse.setFixedWidth(34)
        self.ssh_key_browse.clicked.connect(self._browse_key)

        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.setSpacing(6)
        key_layout.addWidget(self.ssh_key_path, 1)
        key_layout.addWidget(self.ssh_key_browse)

        self.ssh_passphrase = QLineEdit()
        self.ssh_passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.ssh_passphrase.setPlaceholderText("Необязательно")

        self._ssh_container = QWidget()
        self._ssh_form = QFormLayout(self._ssh_container)
        self._ssh_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._ssh_form.addRow("Хост", self.ssh_host)
        self._ssh_form.addRow("Порт", self.ssh_port)
        self._ssh_form.addRow("Пользователь", self.ssh_user)
        self._ssh_form.addRow("Аутентификация", self.ssh_auth)
        self._ssh_form.addRow("Пароль SSH", self.ssh_password)
        self._ssh_form.addRow("Ключ", key_row)
        self._ssh_form.addRow("Passphrase", self.ssh_passphrase)
        self.ssh_install = QCheckBox("Записать мой ключ")
        self.ssh_install.setToolTip(
            "Если хост уже пускает ключом из настроек, пароль не нужен. "
            "Иначе ключ будет добавлен в authorized_keys."
        )
        self._ssh_form.addRow("", self.ssh_install)
        layout.addWidget(self._ssh_container)

        self._chain_container = QWidget()
        chain_layout = QVBoxLayout(self._chain_container)
        chain_layout.setContentsMargins(0, 0, 0, 0)
        chain_layout.setSpacing(8)
        self._chain_route = QLabel("…")
        self._chain_route.setObjectName("chainRoute")
        self._chain_route.setWordWrap(True)
        self._chain_route.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        chain_layout.addWidget(self._chain_route)
        chain_hint = QLabel(
            "Сверху вниз — порядок входа. Пароли не сохраняются."
        )
        chain_hint.setWordWrap(True)
        chain_hint.setStyleSheet("color:#8b8d97;")
        chain_layout.addWidget(chain_hint)
        self._hop_host = QWidget()
        self._hop_layout = QVBoxLayout(self._hop_host)
        self._hop_layout.setContentsMargins(0, 0, 0, 0)
        self._hop_layout.setSpacing(6)
        self._hop_rows: list[_HopFields] = []
        chain_layout.addWidget(self._hop_host)
        self._add_hop = QPushButton("+")
        self._add_hop.setObjectName("auxButton")
        self._add_hop.setAutoDefault(False)
        self._add_hop.setDefault(False)
        self._add_hop.setToolTip("Добавить следующий узел цепочки")
        self._add_hop.clicked.connect(self._add_hop_row)
        chain_layout.addWidget(self._add_hop, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._chain_container)
        self._add_hop_row()

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setWidget(form_host)
        root.addWidget(form_scroll, 1)

        # Ошибка и кнопки всегда видны, даже если форма выше окна.
        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#fb7185; font-weight:600;")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#8b8d97;")
        self._status_label.setVisible(False)
        root.addWidget(self._status_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._cancel_button = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self._ok_text = self._ok_button.text() if self._ok_button is not None else "OK"
        if self._ok_button is not None:
            self._ok_button.setDefault(True)
            self._ok_button.setAutoDefault(True)
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        llm = self.provider_combo.findData("llm")
        if llm >= 0:
            self.provider_combo.setCurrentIndex(llm)
        self._on_provider_changed()
        self._on_auth_changed()

    # --- публичное --------------------------------------------------------

    def created_session(self) -> Optional[domain.SessionInfo]:
        return self._created

    # --- вспомогательное --------------------------------------------------

    def _fill_profiles(self) -> None:
        self.profile_combo.clear()
        for profile in self._service.list_llm_profiles():
            self.profile_combo.addItem(
                f"{profile.name}  ·  {profile.model_id}", profile.profile_id
            )
        empty = self.profile_combo.count() == 0
        self._llm_empty.setVisible(empty)
        self.profile_combo.setVisible(not empty)

    def _terminal_kind(self) -> str:
        if self.provider_combo.currentData() == "llm":
            return str(self.terminal_combo.currentData())
        return str(self.provider_combo.currentData())

    def _on_provider_changed(self) -> None:
        is_llm = self.provider_combo.currentData() == "llm"
        kind = self._terminal_kind()
        is_ssh = kind == "ssh"
        is_chain = kind == "ssh_over_ssh"
        self._llm_container.setVisible(is_llm)
        self._ssh_container.setVisible(is_ssh)
        self._chain_container.setVisible(is_chain)
        self._shell_hint.setVisible(is_chain)
        self.name_edit.setPlaceholderText(
            "Например: prod-web" if is_ssh or is_chain else "Например: Рабочая CMD"
        )
        self._clear_error()
        if not self._busy and self._ok_button is not None:
            blocked = is_llm and self.profile_combo.count() == 0
            self._ok_button.setEnabled(not blocked)

    def _shell_family(self) -> str:
        return str(self.shell_combo.currentData() or domain.SHELL_UNIX)

    def _on_auth_changed(self) -> None:
        method = self.ssh_auth.currentData()
        is_password = method == domain.SSHAuthMethod.PASSWORD.value
        is_key = method == domain.SSHAuthMethod.PRIVATE_KEY.value

        self._set_row_visible(self.ssh_password, is_password)
        self._set_row_visible(self.ssh_install, is_password)
        if not is_password:
            self.ssh_install.setChecked(False)
        self._set_row_visible(self.ssh_key_path, is_key)
        self.ssh_key_browse.setVisible(is_key)
        self._set_row_visible(self.ssh_passphrase, is_key)

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        for form in (self._ssh_form,):
            label = form.labelForField(field)
            if label is not None:
                label.setVisible(visible)
                return

    def _browse_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите файл приватного ключа"
        )
        if path:
            self.ssh_key_path.setText(path)

    def _set_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))

    def _clear_error(self) -> None:
        self._error_label.clear()
        self._error_label.setVisible(False)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        self._status_label.setText(status)
        self._status_label.setVisible(busy and bool(status))
        self._add_hop.setEnabled(not busy)
        if self._ok_button is not None:
            self._ok_button.setText("Подключение…" if busy else self._ok_text)
        if self._cancel_button is not None:
            self._cancel_button.setEnabled(not busy)
        if busy:
            if self._ok_button is not None:
                self._ok_button.setEnabled(False)
            return
        self._on_provider_changed()

    def _finish_call(self, info: object, error: object) -> None:
        self._set_busy(False)
        _prove_master_password(self._service)
        if error is not None:
            self._created = None
            exc = error if isinstance(error, BaseException) else Exception(str(error))
            self._set_error(str(exc) or exc.__class__.__name__)
            return
        self._created = info  # type: ignore[assignment]
        self.ssh_password.clear()
        self.ssh_passphrase.clear()
        for row in self._hop_rows:
            row.clear_secret()
        self.accept()

    # --- OK ---------------------------------------------------------------

    def _on_ok(self) -> None:
        if self._busy:
            return
        try:
            self._submit()
        except Exception as exc:
            self._set_busy(False)
            self._set_error(str(exc) or exc.__class__.__name__)

    def _submit(self) -> None:
        self._clear_error()

        name = self.name_edit.text().strip()

        if not name:
            self._set_error("Имя сессии не может быть пустым")
            return

        provider_key = self.provider_combo.currentData()
        llm_profile_id = None
        if provider_key == "llm":
            llm_profile_id = self.profile_combo.currentData()
            if not llm_profile_id:
                self._set_error(
                    "Сначала добавьте модель в настройках."
                )
                return
            provider_key = self.terminal_combo.currentData()

        if provider_key == "cmd":
            request = NewSessionRequest(
                name=name,
                journal_password="",
                provider=domain.ProviderType.CMD,
                ssh=None,
                auth_secret=None,
                llm_profile_id=llm_profile_id,
                shell_family=self._shell_family(),
            )
        elif provider_key == "ssh_over_ssh":
            built = self._chain_request(name, "", llm_profile_id)
            if built is None:
                return
            request = built
        else:
            host = self.ssh_host.text().strip()
            user = self.ssh_user.text().strip()
            method = domain.SSHAuthMethod(self.ssh_auth.currentData())
            key_path = self.ssh_key_path.text().strip() or None
            if method == domain.SSHAuthMethod.PASSWORD:
                secret = self.ssh_password.text()
            elif method == domain.SSHAuthMethod.PRIVATE_KEY:
                secret = self.ssh_passphrase.text() or None
            else:
                secret = None

            if not host:
                self._set_error("Укажите адрес хоста")
                return
            if not user:
                self._set_error("Укажите пользователя SSH")
                return
            if method == domain.SSHAuthMethod.PASSWORD and not secret:
                if not self.ssh_install.isChecked():
                    self._set_error("Введите пароль SSH")
                    return
            if method == domain.SSHAuthMethod.PRIVATE_KEY and not key_path:
                self._set_error("Укажите файл приватного ключа")
                return

            request = NewSessionRequest(
                name=name,
                journal_password="",
                provider=domain.ProviderType.SSH,
                ssh=domain.SSHConfig(
                    host=host,
                    port=int(self.ssh_port.value()),
                    username=user,
                    auth_method=method,
                    private_key_path=key_path,
                ),
                auth_secret=secret,
                llm_profile_id=llm_profile_id,
                shell_family=self._shell_family(),
                install_key=(True,) if self.ssh_install.isChecked() else (),
            )

        journal_password = ask_journal_password(
            self._service,
            request.provider,
            request.ssh,
            request.auth_secret,
            request.hop_secrets,
            self,
            request.install_key,
        )
        if journal_password is None:
            return
        if any(request.install_key):
            uses = getattr(self._service, "ssh_key_uses_master_passphrase", None)
            has = getattr(self._service, "has_master_password", None)
            if callable(uses) and uses() and callable(has) and not has():
                prompted = MasterPasswordDialog(self._service, self)
                if prompted.exec() != QDialog.DialogCode.Accepted:
                    return
        request.journal_password = journal_password
        secret = request.auth_secret

        def call() -> object:
            try:
                return self._service.create_session(
                    name=request.name,
                    password=request.journal_password,
                    provider=request.provider,
                    ssh=request.ssh,
                    auth_secret=secret,
                    hop_secrets=request.hop_secrets,
                    llm_profile_id=request.llm_profile_id,
                    shell_family=request.shell_family,
                    install_key=request.install_key,
                )
            finally:
                request.auth_secret = None
                request.hop_secrets = ()

        self._run_off_ui(call)

    def _add_hop_row(self) -> None:
        row = _HopFields(removable=True)
        row.remove_requested.connect(self._remove_hop_row)
        row.changed.connect(self._refresh_chain_route)
        self._hop_layout.addWidget(row)
        self._hop_rows.append(row)
        self._renumber_hops()
        self._refresh_chain_route()

    def _remove_hop_row(self, row: object) -> None:
        if not isinstance(row, _HopFields) or len(self._hop_rows) <= 1:
            return
        self._hop_rows.remove(row)
        self._hop_layout.removeWidget(row)
        row.deleteLater()
        self._renumber_hops()
        self._refresh_chain_route()

    def _refresh_chain_route(self) -> None:
        parts = [row.endpoint() for row in self._hop_rows]
        self._chain_route.setText(" → ".join(parts) if parts else "…")

    def _renumber_hops(self) -> None:
        removable = len(self._hop_rows) > 1
        for index, row in enumerate(self._hop_rows, start=1):
            row.set_index(index, removable)

    def _chain_request(
        self,
        name: str,
        journal_password: str,
        llm_profile_id: Optional[str],
    ) -> Optional[NewSessionRequest]:
        if len(self._hop_rows) < 2:
            self._set_error("Добавьте ещё один узел кнопкой +")
            return None
        hops: list[domain.SSHHop] = []
        secrets: list[str] = []
        install: list[bool] = []
        first_host = ""
        first_user = ""
        first_port = 22
        first_method = domain.SSHAuthMethod.PASSWORD
        first_key: Optional[str] = None
        first_secret: Optional[str] = None
        for index, row in enumerate(self._hop_rows, start=1):
            host = row.host.text().strip()
            user = row.user.text().strip()
            method = row.auth_method()
            key_path = row.key_path.text().strip() or None
            if not host:
                self._set_error(f"Укажите IP узла {index}")
                return None
            if not user:
                self._set_error(f"Укажите логин узла {index}")
                return None
            if (
                method == domain.SSHAuthMethod.PASSWORD
                and not row.password.text()
                and not row.wants_install()
            ):
                self._set_error(f"Введите пароль узла {index}")
                return None
            if method == domain.SSHAuthMethod.PRIVATE_KEY and not key_path:
                self._set_error(f"Укажите файл ключа узла {index}")
                return None
            secret = row.secret()
            install.append(row.wants_install())
            if index == 1:
                first_host = host
                first_user = user
                first_port = int(row.port.value())
                first_method = method
                first_key = key_path
                first_secret = secret or None
            else:
                hops.append(domain.SSHHop(
                    host=host,
                    port=int(row.port.value()),
                    username=user,
                    auth_method=method,
                    private_key_path=key_path,
                ))
                secrets.append(secret)
        return NewSessionRequest(
            name=name,
            journal_password=journal_password,
            provider=domain.ProviderType.SSH_OVER_SSH,
            ssh=domain.SSHConfig(
                host=first_host,
                port=first_port,
                username=first_user,
                auth_method=first_method,
                private_key_path=first_key,
                hops=tuple(hops),
            ),
            auth_secret=first_secret,
            hop_secrets=tuple(secrets),
            llm_profile_id=llm_profile_id,
            shell_family=self._shell_family(),
            install_key=tuple(install),
        )


class UnlockSessionDialog(_SessionCallDialog):
    """Разблокировка журнала и, для SSH, вход под логином и паролем."""

    def __init__(
        self,
        service: ApplicationService,
        session_info: domain.SessionInfo,
        parent: Optional[QWidget] = None,
        reconnect: bool = False,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._session_info = session_info
        self._opened: Optional[domain.SessionInfo] = None
        self._reconnect = reconnect

        self.setWindowTitle(
            "Восстановление соединения" if reconnect else "Открытие сессии"
        )
        self.setModal(True)
        if session_info.provider == domain.ProviderType.SSH_OVER_SSH:
            self._fit_to_screen(520, 360 if reconnect else 440)
        elif session_info.provider == domain.ProviderType.SSH:
            self._fit_to_screen(520, 460)
        else:
            self._fit_to_screen(480, 280)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)
        form_host = QWidget()
        layout = QVBoxLayout(form_host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        if reconnect:
            header_text = (
                f"Журнал «{session_info.name}» уже открыт. "
                "Введите данные входа, чтобы снова подключить сессию."
            )
        else:
            header_text = (
                f"Сессия «{session_info.name}» "
                f"({session_info.provider.value})."
            )
        header = QLabel(header_text)
        header.setObjectName("sessionIntro")
        header.setWordWrap(True)
        header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        header.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        layout.addWidget(header)

        if (
            not reconnect
            and session_info.provider == domain.ProviderType.CMD
        ):
            note = QLabel("Журнал откроется мастер-паролем этого запуска.")
            note.setWordWrap(True)
            note.setStyleSheet("color:#8b8d97;")
            layout.addWidget(note)

        # --- SSH: логин, метод, секрет -----------------------------------

        self._ssh_container: Optional[QWidget] = None
        self.ssh_user: Optional[QLineEdit] = None
        self.ssh_auth: Optional[QComboBox] = None
        self.ssh_secret: Optional[QLineEdit] = None
        self.ssh_key_path: Optional[QLineEdit] = None

        if (
            session_info.provider == domain.ProviderType.SSH
            and session_info.ssh is not None
        ):
            ssh = session_info.ssh
            self._ssh_container = QWidget()
            ssh_form = QFormLayout(self._ssh_container)
            ssh_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

            self.ssh_user = QLineEdit(ssh.username)
            ssh_form.addRow("Логин", self.ssh_user)

            self.ssh_auth = QComboBox()
            self.ssh_auth.addItem(
                "Пароль", domain.SSHAuthMethod.PASSWORD.value
            )
            self.ssh_auth.addItem(
                "Приватный ключ", domain.SSHAuthMethod.PRIVATE_KEY.value
            )
            self.ssh_auth.addItem(
                "SSH-агент", domain.SSHAuthMethod.AGENT.value
            )
            idx = self.ssh_auth.findData(ssh.auth_method.value)
            if idx >= 0:
                self.ssh_auth.setCurrentIndex(idx)
            self.ssh_auth.currentIndexChanged.connect(self._on_auth_changed)
            ssh_form.addRow("Аутентификация", self.ssh_auth)

            self.ssh_secret = QLineEdit()
            self.ssh_secret.setEchoMode(QLineEdit.EchoMode.Password)
            self.ssh_secret.setPlaceholderText("Пароль или passphrase")
            self.ssh_secret.returnPressed.connect(self._on_ok)
            ssh_form.addRow("Секрет", self.ssh_secret)

            self.ssh_key_path = QLineEdit(ssh.private_key_path or "")
            ssh_form.addRow("Ключ", self.ssh_key_path)

            self._ssh_form = ssh_form
            layout.addWidget(self._ssh_container)

        self._chain_rows: list[_ChainPasswordRow] = []
        self._chain_container: Optional[QWidget] = None
        if (
            session_info.provider == domain.ProviderType.SSH_OVER_SSH
            and session_info.ssh is not None
        ):
            ssh = session_info.ssh
            self._chain_container = QWidget()
            chain_layout = QVBoxLayout(self._chain_container)
            chain_layout.setContentsMargins(0, 0, 0, 0)
            chain_layout.setSpacing(8)
            route = QLabel(domain.ssh_location(ssh))
            route.setObjectName("chainRoute")
            route.setWordWrap(True)
            route.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            chain_layout.addWidget(route)
            hint = QLabel("Пароль нужен только узлам, которые входят по паролю.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#8b8d97;")
            chain_layout.addWidget(hint)
            self._chain_host = QWidget()
            self._chain_layout = QVBoxLayout(self._chain_host)
            self._chain_layout.setContentsMargins(0, 0, 0, 0)
            self._chain_layout.setSpacing(8)
            chain_layout.addWidget(self._chain_host)
            self._add_password_row(
                ssh.username, ssh.host, ssh.port, ssh.auth_method, ssh.private_key_path
            )
            for hop in ssh.hops:
                self._add_password_row(
                    hop.username,
                    hop.host,
                    hop.port,
                    hop.auth_method,
                    hop.private_key_path,
                )
            layout.addWidget(self._chain_container)

        layout.addStretch(1)
        if self.ssh_auth is not None:
            self._on_auth_changed()

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setWidget(form_host)
        root.addWidget(form_scroll, 1)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#fb7185; font-weight:600;")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#8b8d97;")
        self._status_label.setVisible(False)
        root.addWidget(self._status_label)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._cancel_button = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self._ok_text = self._ok_button.text() if self._ok_button is not None else "OK"
        if self._ok_button is not None:
            self._ok_button.setObjectName("primaryButton")
            self._ok_button.setDefault(True)
            self._ok_button.setAutoDefault(True)
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        self._apply_tab_order()

    # --- публичное --------------------------------------------------------

    def opened_session(self) -> Optional[domain.SessionInfo]:
        return self._opened

    # --- вспомогательное --------------------------------------------------

    def _on_auth_changed(self) -> None:
        if self.ssh_auth is None or self.ssh_secret is None or self.ssh_key_path is None:
            return
        method = self.ssh_auth.currentData()
        is_password = method == domain.SSHAuthMethod.PASSWORD.value
        is_key = method == domain.SSHAuthMethod.PRIVATE_KEY.value

        # Подпись у поля секрета: пароль или passphrase.
        if self._ssh_container is not None:
            label = self._ssh_form.labelForField(self.ssh_secret)
            if label is not None:
                label.setText("Пароль SSH" if is_password else "Passphrase")
                label.setVisible(is_password or is_key)
        self.ssh_secret.setVisible(is_password or is_key)
        self.ssh_key_path.setVisible(is_key)
        if self._ssh_container is not None:
            key_label = self._ssh_form.labelForField(self.ssh_key_path)
            if key_label is not None:
                key_label.setVisible(is_key)
        if getattr(self, "_ok_button", None) is not None:
            self._apply_tab_order()

    def _set_error(self, message: str) -> None:
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))

    def _clear_error(self) -> None:
        self._error_label.clear()
        self._error_label.setVisible(False)

    def _set_busy(self, busy: bool, status: str = "") -> None:
        self._busy = busy
        self._status_label.setText(status)
        self._status_label.setVisible(busy and bool(status))
        if getattr(self, "_add_chain_hop", None) is not None:
            self._add_chain_hop.setEnabled(not busy)
        if self._ok_button is not None:
            self._ok_button.setText("Подключение…" if busy else self._ok_text)
            self._ok_button.setEnabled(not busy)
        if self._cancel_button is not None:
            self._cancel_button.setEnabled(not busy)

    def _finish_call(self, info: object, error: object) -> None:
        self._set_busy(False)
        if error is None or isinstance(error, SessionUnavailable):
            _prove_master_password(self._service)
        else:
            _forget_unproven_master_password(self._service)
        if isinstance(error, SessionUnavailable):
            if self._reconnect:
                self._set_error(error.reason)
                return
            self._opened = error.info
            if self.ssh_secret is not None:
                self.ssh_secret.clear()
            for row in self._chain_rows:
                row.clear_secret()
            self.accept()
            return
        if error is not None:
            self._opened = None
            exc = error if isinstance(error, BaseException) else Exception(str(error))
            self._set_error(str(exc) or exc.__class__.__name__)
            return
        self._opened = info  # type: ignore[assignment]
        if self.ssh_secret is not None:
            self.ssh_secret.clear()
        for row in self._chain_rows:
            row.clear_secret()
        self.accept()

    # --- OK ---------------------------------------------------------------

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(0, self._focus_password)

    def _focus_password(self) -> None:
        for row in self._chain_rows:
            if row.password is not None:
                row.password.setFocus(Qt.FocusReason.OtherFocusReason)
                return
        if self.ssh_secret is not None and self.ssh_secret.isVisible():
            self.ssh_secret.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if self._ok_button is not None:
            self._ok_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_ok(self) -> None:
        if self._busy:
            return
        try:
            self._submit()
        except Exception as exc:
            self._set_busy(False)
            self._set_error(str(exc) or exc.__class__.__name__)

    def _submit(self) -> None:
        self._clear_error()

        ssh_override: Optional[domain.SSHConfig] = None
        ssh_secret: Optional[str] = None
        hop_secrets: tuple[str, ...] = ()

        if self._session_info.provider == domain.ProviderType.SSH_OVER_SSH:
            built = self._unlock_chain()
            if built is None:
                return
            ssh_override, ssh_secret, hop_secrets = built
        elif (
            self._session_info.provider == domain.ProviderType.SSH
            and self.ssh_user is not None
            and self.ssh_auth is not None
            and self.ssh_secret is not None
            and self.ssh_key_path is not None
        ):
            user = self.ssh_user.text().strip()
            if not user:
                self._set_error("Введите логин SSH")
                return

            method = domain.SSHAuthMethod(self.ssh_auth.currentData())
            key_path = self.ssh_key_path.text().strip() or None

            if method == domain.SSHAuthMethod.PASSWORD:
                ssh_secret = self.ssh_secret.text()
                if not ssh_secret:
                    self._set_error("Введите пароль SSH")
                    return
            elif method == domain.SSHAuthMethod.PRIVATE_KEY:
                if not key_path:
                    self._set_error("Укажите файл приватного ключа")
                    return
                ssh_secret = self.ssh_secret.text() or None

            ssh_override = domain.SSHConfig(
                host=self._session_info.ssh.host if self._session_info.ssh else "",
                port=self._session_info.ssh.port if self._session_info.ssh else 22,
                username=user,
                auth_method=method,
                private_key_path=key_path,
                connect_timeout=(
                    self._session_info.ssh.connect_timeout
                    if self._session_info.ssh else 15.0
                ),
            )

        journal_pw = ""
        if not self._reconnect:
            journal_pw = ask_journal_password(
                self._service,
                self._session_info.provider,
                ssh_override or self._session_info.ssh,
                ssh_secret,
                hop_secrets,
                self,
            )
            if not journal_pw:
                return

        session_id = self._session_info.session_id
        if self._reconnect:
            self._run_off_ui(
                lambda: self._service.reconnect_session(
                    session_id,
                    auth_secret=ssh_secret,
                    hop_secrets=hop_secrets,
                    ssh_override=ssh_override,
                )
            )
            return
        self._run_off_ui(
            lambda: self._service.open_session(
                session_id,
                journal_pw,
                auth_secret=ssh_secret,
                hop_secrets=hop_secrets,
                ssh_override=ssh_override,
            )
        )

    def _kept_in_tab_order(self, widget: QWidget) -> bool:
        """isVisible() до show() всегда ложен. Смотрим локальный флаг и родителей."""
        current: Optional[QWidget] = widget
        while current is not None and current is not self:
            if current.isHidden():
                return False
            current = current.parentWidget()
        return True

    def _apply_tab_order(self) -> None:
        """Tab идёт по полям сверху вниз. Скрытый пароль журнала пропускается."""
        widgets: list[QWidget] = []
        for row in self._chain_rows:
            if row.password is not None:
                widgets.append(row.password)
        for field in (self.ssh_user, self.ssh_auth, self.ssh_secret, self.ssh_key_path):
            if field is not None and self._kept_in_tab_order(field):
                widgets.append(field)
        add_hop = getattr(self, "_add_chain_hop", None)
        if add_hop is not None and self._kept_in_tab_order(add_hop):
            widgets.append(add_hop)
        if self._ok_button is not None:
            widgets.append(self._ok_button)
        if self._cancel_button is not None:
            widgets.append(self._cancel_button)
        for earlier, later in zip(widgets, widgets[1:]):
            self.setTabOrder(earlier, later)

    def _add_password_row(
        self,
        username: str,
        host: str,
        port: int = 22,
        auth_method: domain.SSHAuthMethod = domain.SSHAuthMethod.PASSWORD,
        private_key_path: Optional[str] = None,
    ) -> None:
        row = _ChainPasswordRow(username, host, port, auth_method, private_key_path)
        if row.password is not None:
            row.password.returnPressed.connect(self._on_ok)
        self._chain_layout.addWidget(row)
        self._chain_rows.append(row)

    def _unlock_chain(
        self,
    ) -> Optional[tuple[domain.SSHConfig, Optional[str], tuple[str, ...]]]:
        if len(self._chain_rows) < 2:
            self._set_error("В цепочке SSH нужно хотя бы два узла")
            return None
        hops: list[domain.SSHHop] = []
        secrets: list[str] = []
        first_host = ""
        first_user = ""
        first_port = 22
        first_method = domain.SSHAuthMethod.PASSWORD
        first_key: Optional[str] = None
        first_secret = ""
        for index, row in enumerate(self._chain_rows, start=1):
            host, user, secret = row.values()
            if row.auth_method() == domain.SSHAuthMethod.PASSWORD and not secret:
                self._set_error(f"Введите пароль узла {index}")
                return None
            if index == 1:
                first_host, first_user, first_secret = host, user, secret
                first_port = row._port
                first_method = row.auth_method()
                first_key = row.key_path()
            else:
                hops.append(domain.SSHHop(
                    host=host,
                    username=user,
                    port=row._port,
                    auth_method=row.auth_method(),
                    private_key_path=row.key_path(),
                ))
                secrets.append(secret)
        timeout = 15.0
        if self._session_info.ssh is not None:
            timeout = self._session_info.ssh.connect_timeout
        config = domain.SSHConfig(
            host=first_host,
            port=first_port,
            username=first_user,
            auth_method=first_method,
            private_key_path=first_key,
            connect_timeout=timeout,
            hops=tuple(hops),
        )
        return config, first_secret or None, tuple(secrets)


class RenameSessionDialog(QDialog):
    """Новое имя сессии. Пустое имя не принимается."""

    def __init__(self, current_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Переименовать сессию")
        self.setModal(True)
        self.resize(420, 180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(12)

        hint = QLabel("Это имя видно в списке сессий и в шапке окна.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.name_edit = QLineEdit(current_name)
        self.name_edit.selectAll()
        self.name_edit.returnPressed.connect(self._on_ok)
        layout.addWidget(self.name_edit)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color:#fb7185; font-weight:600;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name_edit.setFocus()

    def name(self) -> str:
        return self.name_edit.text().strip()

    def _on_ok(self) -> None:
        if not self.name():
            self._error.setText("Имя сессии не может быть пустым")
            self._error.setVisible(True)
            return
        self.accept()