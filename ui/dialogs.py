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
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

import domain
from application import ApplicationService, SessionUnavailable


class _HopFields(QWidget):
    """IP, логин и пароль одного узла цепочки SSH."""

    remove_requested = Signal(object)

    def __init__(self, removable: bool, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("hopCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        head = QHBoxLayout()
        self.title = QLabel("Узел")
        self.title.setObjectName("sidebarTitle")
        head.addWidget(self.title)
        head.addStretch(1)
        self.remove_button = QPushButton("−")
        self.remove_button.setObjectName("auxButton")
        self.remove_button.setFixedWidth(34)
        self.remove_button.setToolTip("Убрать этот узел из цепочки")
        self.remove_button.setVisible(removable)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self))
        head.addWidget(self.remove_button)
        layout.addLayout(head)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.host = QLineEdit()
        self.host.setPlaceholderText("10.0.0.5")
        form.addRow("IP", self.host)
        self.user = QLineEdit()
        self.user.setPlaceholderText("ubuntu")
        form.addRow("Логин", self.user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пароль SSH")
        form.addRow("Пароль", self.password)
        layout.addLayout(form)

    def set_index(self, index: int, removable: bool) -> None:
        self.title.setText(f"Узел {index}")
        self.remove_button.setVisible(removable)

    def clear_secret(self) -> None:
        self.password.clear()

    def values(self) -> tuple[str, str, str]:
        return self.host.text().strip(), self.user.text().strip(), self.password.text()


class _ChainPasswordRow(QWidget):
    """Строка повторного входа: логин@адрес и поле пароля."""

    def __init__(
        self,
        username: str,
        host: str,
        port: int = 22,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._host = host.strip()
        self._user = username.strip()
        self._port = port
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
        self.password = QLineEdit()
        self.password.setObjectName("chainPassword")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Пароль")
        layout.addWidget(self.password, 1)

    def values(self) -> tuple[str, str, str]:
        return self._host, self._user, self.password.text()

    def clear_secret(self) -> None:
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
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(10)
        form_host = QWidget()
        layout = QVBoxLayout(form_host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        info = QLabel(
            "Пароль журнала защищает историю сессии. Это не пароль Windows "
            "и не пароль SSH. Пароль нельзя восстановить."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#8b949e;")
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
        self._shell_hint.setStyleSheet("color:#8b949e;")
        basic_form.addRow("", self._shell_hint)

        layout.addLayout(basic_form)

        self._llm_container = QWidget()
        llm_form = QFormLayout(self._llm_container)
        llm_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.profile_combo = QComboBox()
        llm_form.addRow("Модель", self.profile_combo)
        self._llm_empty = QLabel(
            "Сначала добавьте модель кнопкой «Модели» в боковой панели."
        )
        self._llm_empty.setWordWrap(True)
        self._llm_empty.setStyleSheet("color:#8b949e;")
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
        layout.addWidget(self._ssh_container)

        self._chain_container = QWidget()
        chain_layout = QVBoxLayout(self._chain_container)
        chain_layout.setContentsMargins(0, 0, 0, 0)
        chain_layout.setSpacing(8)
        chain_hint = QLabel(
            "Сначала вход на первый узел. После него SSH идёт на следующий, "
            "и так по цепочке. Пароли не сохраняются."
        )
        chain_hint.setWordWrap(True)
        chain_hint.setStyleSheet("color:#8b949e;")
        chain_layout.addWidget(chain_hint)
        self._hop_host = QWidget()
        self._hop_layout = QVBoxLayout(self._hop_host)
        self._hop_layout.setContentsMargins(0, 0, 0, 0)
        self._hop_layout.setSpacing(8)
        self._hop_rows: list[_HopFields] = []
        hop_scroll = QScrollArea()
        hop_scroll.setWidgetResizable(True)
        hop_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        hop_scroll.setWidget(self._hop_host)
        hop_scroll.setMinimumHeight(120)
        hop_scroll.setMaximumHeight(280)
        chain_layout.addWidget(hop_scroll)
        self._add_hop = QPushButton("+")
        self._add_hop.setObjectName("auxButton")
        self._add_hop.setAutoDefault(False)
        self._add_hop.setDefault(False)
        self._add_hop.setToolTip("Добавить следующий узел цепочки")
        self._add_hop.clicked.connect(self._add_hop_row)
        chain_layout.addWidget(self._add_hop, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self._chain_container)
        self._add_hop_row()

        # --- Пароль журнала ----------------------------------------------

        pw_form = QFormLayout()
        pw_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_form.addRow("Пароль журнала", self.password_edit)
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.EchoMode.Password)
        pw_form.addRow("Подтверждение", self.confirm_edit)
        layout.addLayout(pw_form)

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setWidget(form_host)
        root.addWidget(form_scroll, 1)

        # Ошибка и кнопки всегда видны, даже если форма выше окна.
        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#ff8a80; font-weight:600;")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#8b949e;")
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
        if error is not None:
            self._created = None
            exc = error if isinstance(error, BaseException) else Exception(str(error))
            self._set_error(str(exc) or exc.__class__.__name__)
            return
        self._created = info  # type: ignore[assignment]
        self.password_edit.clear()
        self.confirm_edit.clear()
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
        pw = self.password_edit.text()
        confirm = self.confirm_edit.text()

        if not name:
            self._set_error("Имя сессии не может быть пустым")
            return
        if not pw:
            self._set_error("Пароль журнала не может быть пустым")
            return
        if pw != confirm:
            self._set_error("Пароли не совпадают")
            return

        provider_key = self.provider_combo.currentData()
        llm_profile_id = None
        if provider_key == "llm":
            llm_profile_id = self.profile_combo.currentData()
            if not llm_profile_id:
                self._set_error(
                    "Сначала добавьте модель кнопкой «Модели» в боковой панели."
                )
                return
            provider_key = self.terminal_combo.currentData()

        if provider_key == "cmd":
            request = NewSessionRequest(
                name=name,
                journal_password=pw,
                provider=domain.ProviderType.CMD,
                ssh=None,
                auth_secret=None,
                llm_profile_id=llm_profile_id,
                shell_family=self._shell_family(),
            )
        elif provider_key == "ssh_over_ssh":
            built = self._chain_request(name, pw, llm_profile_id)
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
                self._set_error("Введите пароль SSH")
                return
            if method == domain.SSHAuthMethod.PRIVATE_KEY and not key_path:
                self._set_error("Укажите файл приватного ключа")
                return

            request = NewSessionRequest(
                name=name,
                journal_password=pw,
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
            )

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
                )
            finally:
                request.auth_secret = None
                request.hop_secrets = ()

        self._run_off_ui(call)

    def _add_hop_row(self) -> None:
        row = _HopFields(removable=True)
        row.remove_requested.connect(self._remove_hop_row)
        self._hop_layout.addWidget(row)
        self._hop_rows.append(row)
        self._renumber_hops()

    def _remove_hop_row(self, row: object) -> None:
        if not isinstance(row, _HopFields) or len(self._hop_rows) <= 1:
            return
        self._hop_rows.remove(row)
        self._hop_layout.removeWidget(row)
        row.deleteLater()
        self._renumber_hops()

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
        first_host = ""
        first_user = ""
        first_secret = ""
        for index, row in enumerate(self._hop_rows, start=1):
            host = row.host.text().strip()
            user = row.user.text().strip()
            secret = row.password.text()
            if not host:
                self._set_error(f"Укажите IP узла {index}")
                return None
            if not user:
                self._set_error(f"Укажите логин узла {index}")
                return None
            if not secret:
                self._set_error(f"Введите пароль узла {index}")
                return None
            if index == 1:
                first_host, first_user, first_secret = host, user, secret
            else:
                hops.append(domain.SSHHop(host=host, username=user))
                secrets.append(secret)
        return NewSessionRequest(
            name=name,
            journal_password=journal_password,
            provider=domain.ProviderType.SSH_OVER_SSH,
            ssh=domain.SSHConfig(
                host=first_host,
                username=first_user,
                auth_method=domain.SSHAuthMethod.PASSWORD,
                hops=tuple(hops),
            ),
            auth_secret=first_secret,
            hop_secrets=tuple(secrets),
            llm_profile_id=llm_profile_id,
            shell_family=self._shell_family(),
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
        tall = session_info.provider in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        )
        if reconnect and session_info.provider == domain.ProviderType.SSH_OVER_SSH:
            self._fit_to_screen(520, 360)
        else:
            self._fit_to_screen(560, 680 if tall else 280)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
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
        header.setWordWrap(True)
        layout.addWidget(header)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.returnPressed.connect(self._on_ok)
        self._journal_hint = QLabel(
            "Если оставить пустым, сюда подставится пароль SSH."
        )
        self._journal_hint.setWordWrap(True)
        self._journal_hint.setStyleSheet("color:#a1a1aa;")
        journal_box = QWidget()
        journal_layout = QVBoxLayout(journal_box)
        journal_layout.setContentsMargins(0, 0, 0, 0)
        journal_layout.setSpacing(4)
        journal_layout.addWidget(self.password_edit)
        journal_layout.addWidget(self._journal_hint)
        self._journal_box = journal_box
        form.addRow("Пароль журнала", journal_box)
        if reconnect:
            journal_box.hide()
            password_label = form.labelForField(journal_box)
            if password_label is not None:
                password_label.hide()
        self._journal_hint.setVisible(False)

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

        self._chain_rows: list[_HopFields] = []
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
            hint = QLabel("Пароли узлов не хранятся. Введите их снова.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#8b949e;")
            chain_layout.addWidget(hint)
            self._chain_host = QWidget()
            self._chain_layout = QVBoxLayout(self._chain_host)
            self._chain_layout.setContentsMargins(0, 0, 0, 0)
            self._chain_layout.setSpacing(8)
            if reconnect:
                chain_layout.addWidget(self._chain_host)
                self._add_password_row(ssh.username, ssh.host, ssh.port)
                for hop in ssh.hops:
                    self._add_password_row(hop.username, hop.host, hop.port)
            else:
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                scroll.setWidget(self._chain_host)
                scroll.setMinimumHeight(120)
                scroll.setMaximumHeight(280)
                chain_layout.addWidget(scroll)
                self._add_chain_hop = QPushButton("+")
                self._add_chain_hop.setObjectName("auxButton")
                self._add_chain_hop.setAutoDefault(False)
                self._add_chain_hop.setDefault(False)
                self._add_chain_hop.setToolTip("Добавить следующий узел цепочки")
                self._add_chain_hop.clicked.connect(self._add_unlock_hop)
                chain_layout.addWidget(self._add_chain_hop, 0, Qt.AlignmentFlag.AlignLeft)
                self._add_unlock_hop(ssh.host, ssh.username)
                for hop in ssh.hops:
                    self._add_unlock_hop(hop.host, hop.username)
            layout.addWidget(self._chain_container)

        layout.addLayout(form)
        if self.ssh_auth is not None:
            self._on_auth_changed()

        form_scroll = QScrollArea()
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        form_scroll.setWidget(form_host)
        root.addWidget(form_scroll, 1)

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#ff8a80; font-weight:600;")
        self._error_label.setVisible(False)
        root.addWidget(self._error_label)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color:#8b949e;")
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
        show_journal_fill = is_password and not self._reconnect
        self._journal_hint.setVisible(show_journal_fill)
        self.password_edit.setPlaceholderText(
            "пароль SSH" if show_journal_fill else ""
        )
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
        if isinstance(error, SessionUnavailable):
            if self._reconnect:
                self._set_error(error.reason)
                return
            self._opened = error.info
            self.password_edit.clear()
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
        self.password_edit.clear()
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
        if self._chain_rows:
            self._chain_rows[0].password.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if self.ssh_secret is not None and self.ssh_secret.isVisible():
            self.ssh_secret.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        if self.password_edit.isVisible():
            self.password_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _resolved_journal_password(
        self,
        ssh_method: Optional[domain.SSHAuthMethod],
        ssh_secret: Optional[str],
    ) -> str:
        typed = self.password_edit.text()
        if typed or self._reconnect:
            return typed
        if ssh_method == domain.SSHAuthMethod.PASSWORD and ssh_secret:
            return ssh_secret
        return ""

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
        ssh_method: Optional[domain.SSHAuthMethod] = None

        if self._session_info.provider == domain.ProviderType.SSH_OVER_SSH:
            built = self._unlock_chain()
            if built is None:
                return
            ssh_override, ssh_secret, hop_secrets = built
            ssh_method = domain.SSHAuthMethod.PASSWORD
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
            ssh_method = method
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

        journal_pw = self._resolved_journal_password(ssh_method, ssh_secret)
        if not self._reconnect and not journal_pw:
            self._set_error("Введите пароль журнала")
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
            if isinstance(row, _ChainPasswordRow):
                widgets.append(row.password)
            elif isinstance(row, _HopFields):
                widgets.extend([row.host, row.user, row.password])
                if self._kept_in_tab_order(row.remove_button):
                    widgets.append(row.remove_button)
        for field in (self.ssh_user, self.ssh_auth, self.ssh_secret, self.ssh_key_path):
            if field is not None and self._kept_in_tab_order(field):
                widgets.append(field)
        add_hop = getattr(self, "_add_chain_hop", None)
        if add_hop is not None and self._kept_in_tab_order(add_hop):
            widgets.append(add_hop)
        if self._kept_in_tab_order(self.password_edit):
            self.password_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            widgets.append(self.password_edit)
        else:
            self.password_edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if self._ok_button is not None:
            widgets.append(self._ok_button)
        if self._cancel_button is not None:
            widgets.append(self._cancel_button)
        for earlier, later in zip(widgets, widgets[1:]):
            self.setTabOrder(earlier, later)

    def _add_password_row(self, username: str, host: str, port: int = 22) -> None:
        row = _ChainPasswordRow(username, host, port)
        row.password.returnPressed.connect(self._on_ok)
        self._chain_layout.addWidget(row)
        self._chain_rows.append(row)

    def _add_unlock_hop(self, host: str = "", username: str = "") -> None:
        row = _HopFields(removable=True)
        row.host.setText(host)
        row.user.setText(username)
        row.remove_requested.connect(self._remove_unlock_hop)
        self._chain_layout.addWidget(row)
        self._chain_rows.append(row)
        self._renumber_unlock_hops()

    def _remove_unlock_hop(self, row: object) -> None:
        if not isinstance(row, _HopFields) or len(self._chain_rows) <= 1:
            return
        self._chain_rows.remove(row)
        self._chain_layout.removeWidget(row)
        row.deleteLater()
        self._renumber_unlock_hops()

    def _renumber_unlock_hops(self) -> None:
        removable = len(self._chain_rows) > 1
        for index, row in enumerate(self._chain_rows, start=1):
            row.set_index(index, removable)

    def _unlock_chain(
        self,
    ) -> Optional[tuple[domain.SSHConfig, str, tuple[str, ...]]]:
        if len(self._chain_rows) < 2:
            self._set_error("Добавьте ещё один узел кнопкой +")
            return None
        hops: list[domain.SSHHop] = []
        secrets: list[str] = []
        first_host = ""
        first_user = ""
        first_secret = ""
        for index, row in enumerate(self._chain_rows, start=1):
            host, user, secret = row.values()
            if not host:
                self._set_error(f"Укажите IP узла {index}")
                return None
            if not user:
                self._set_error(f"Укажите логин узла {index}")
                return None
            if not secret:
                self._set_error(f"Введите пароль узла {index}")
                return None
            if hasattr(row, "_port"):
                port = row._port
            elif index == 1 and self._session_info.ssh is not None:
                port = self._session_info.ssh.port
            else:
                port = 22
            if index == 1:
                first_host, first_user, first_secret, first_port = host, user, secret, port
            else:
                hops.append(domain.SSHHop(host=host, username=user, port=port))
                secrets.append(secret)
        timeout = 15.0
        if self._session_info.ssh is not None:
            timeout = self._session_info.ssh.connect_timeout
        config = domain.SSHConfig(
            host=first_host,
            port=first_port,
            username=first_user,
            auth_method=domain.SSHAuthMethod.PASSWORD,
            connect_timeout=timeout,
            hops=tuple(hops),
        )
        return config, first_secret, tuple(secrets)


class RenameSessionDialog(QDialog):
    """Новое имя сессии. Пустое имя не принимается."""

    def __init__(self, current_name: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Переименовать сессию")
        self.setModal(True)
        self.resize(420, 180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
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
        self._error.setStyleSheet("color:#ff8a80; font-weight:600;")
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