# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Список профилей моделей. Проверка соединения идёт через сервис, не из виджета."""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import domain
from application import ApplicationService

def _known_base_urls() -> set:
    return {spec.base_url.rstrip("/") for spec in domain.LLM_API_SPECS}


class ProfileFormDialog(QDialog):
    """Форма одного профиля. Сохранение только после успешного GET /v1/models."""

    _call_done = Signal(object, object)

    def __init__(
        self,
        service: ApplicationService,
        profile: Optional[domain.LlmProfile] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._profile = profile
        self._busy = False
        self._saved: Optional[domain.LlmProfile] = None
        self._call_done.connect(self._finish)

        self.setWindowTitle("Модель" if profile is None else "Изменить модель")
        self.setModal(True)
        self.resize(480, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)

        hint = QLabel(
            "Перед записью программа спросит у сервера список моделей. "
            "Профиль сохранится, только если указанный id есть в ответе."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.name_edit = QLineEdit(profile.name if profile else "")
        self.name_edit.setPlaceholderText("Например: рабочая")
        form.addRow("Имя", self.name_edit)
        self.kind_combo = QComboBox()
        for spec in domain.LLM_API_SPECS:
            self.kind_combo.addItem(spec.label, spec.kind.value)
        form.addRow("API", self.kind_combo)
        initial = profile.api_kind if profile else domain.LlmApiKind.OPENAI
        index = self.kind_combo.findData(initial.value)
        self.kind_combo.setCurrentIndex(index if index >= 0 else 0)
        self.url_edit = QLineEdit(
            profile.base_url if profile else domain.llm_api_spec(initial).base_url
        )
        form.addRow("Адрес", self.url_edit)
        self.model_edit = QLineEdit(profile.model_id if profile else "")
        form.addRow("Модель", self.model_edit)
        self.key_edit = QLineEdit(profile.api_key if profile else "")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API-ключ", self.key_edit)
        self._sync_kind_hints()
        self.kind_combo.currentIndexChanged.connect(self._on_kind)
        layout.addLayout(form)

        self._error = QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color:#ff8a80; font-weight:600;")
        self._error.setVisible(False)
        layout.addWidget(self._error)

        self._status = QLabel("")
        self._status.setStyleSheet("color:#8b949e;")
        self._status.setVisible(False)
        layout.addWidget(self._status)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def saved_profile(self) -> Optional[domain.LlmProfile]:
        return self._saved

    def reject(self) -> None:  # type: ignore[override]
        if self._busy:
            return
        super().reject()

    def _on_ok(self) -> None:
        self._error.setVisible(False)
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        model_id = self.model_edit.text().strip()
        if not name or not url or not model_id:
            self._show_error("Заполните имя, адрес и id модели")
            return
        profile = domain.LlmProfile(
            profile_id=self._profile.profile_id if self._profile else "",
            name=name,
            base_url=url,
            model_id=model_id,
            api_key=self.key_edit.text(),
            context_limit=(
                self._profile.context_limit
                if self._profile
                else domain.DEFAULT_CONTEXT_LIMIT
            ),
            api_kind=self._selected_kind(),
        )
        self._busy = True
        self._ok.setEnabled(False)
        self._status.setText("Проверка подключения…")
        self._status.setVisible(True)

        def work() -> None:
            saved = None
            error = None
            try:
                saved = self._service.save_llm_profile(profile)
            except Exception as exc:
                error = exc
            self._call_done.emit(saved, error)

        threading.Thread(target=work, daemon=True).start()

    def _finish(self, saved: object, error: object) -> None:
        self._busy = False
        self._ok.setEnabled(True)
        self._status.setVisible(False)
        if error is not None:
            exc = error if isinstance(error, BaseException) else Exception(str(error))
            self._show_error(str(exc) or exc.__class__.__name__)
            return
        self._saved = saved  # type: ignore[assignment]
        self.accept()

    def _selected_kind(self) -> domain.LlmApiKind:
        return domain.LlmApiKind(str(self.kind_combo.currentData()))

    def _sync_kind_hints(self) -> None:
        spec = domain.llm_api_spec(self._selected_kind())
        self.model_edit.setPlaceholderText(spec.model_hint)
        self.key_edit.setPlaceholderText(
            "Обязательно" if spec.key_required else "Необязательно"
        )

    def _on_kind(self) -> None:
        spec = domain.llm_api_spec(self._selected_kind())
        current = self.url_edit.text().strip().rstrip("/")
        if not current or current in _known_base_urls():
            self.url_edit.setText(spec.base_url)
        self._sync_kind_hints()

    def _show_error(self, message: str) -> None:
        self._error.setText(message)
        self._error.setVisible(True)


class ModelsDialog(QDialog):
    def __init__(
        self,
        service: ApplicationService,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self.setWindowTitle("Модели")
        self.setModal(True)
        self.resize(460, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        hint = QLabel(
            "Каждая сессия типа LLM использует ровно один сохранённый профиль."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#8b949e;")
        layout.addWidget(hint)

        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        self._empty = QLabel("Пока нет моделей. Добавьте подключение.")
        self._empty.setWordWrap(True)
        self._empty.setStyleSheet("color:#8b949e;")
        layout.addWidget(self._empty)

        row = QHBoxLayout()
        add = QPushButton("Добавить")
        add.setObjectName("primaryButton")
        add.clicked.connect(self._on_add)
        edit = QPushButton("Изменить")
        edit.setObjectName("secondaryButton")
        edit.clicked.connect(self._on_edit)
        delete = QPushButton("Удалить")
        delete.setObjectName("secondaryButton")
        delete.clicked.connect(self._on_delete)
        row.addWidget(add)
        row.addWidget(edit)
        row.addWidget(delete)
        layout.addLayout(row)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        close.accepted.connect(self.accept)
        layout.addWidget(close)
        self._reload()

    def _reload(self) -> None:
        self._list.clear()
        profiles = self._service.list_llm_profiles()
        for profile in profiles:
            label = domain.llm_api_spec(profile.api_kind).label
            item = QListWidgetItem(f"{profile.name}  ·  {label}  ·  {profile.model_id}")
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            self._list.addItem(item)
        self._empty.setVisible(not profiles)

    def _selected_id(self) -> Optional[str]:
        item = self._list.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_add(self) -> None:
        dialog = ProfileFormDialog(self._service, None, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    def _on_edit(self) -> None:
        profile_id = self._selected_id()
        if profile_id is None:
            return
        profile = self._service.get_llm_profile(profile_id)
        if profile is None:
            self._reload()
            return
        dialog = ProfileFormDialog(self._service, profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._reload()

    def _on_delete(self) -> None:
        profile_id = self._selected_id()
        if profile_id is None:
            return
        profile = self._service.get_llm_profile(profile_id)
        name = profile.name if profile is not None else profile_id
        answer = QMessageBox.question(
            self,
            "Удалить модель",
            f"Удалить профиль «{name}»?\n"
            "Сессии, которые на него ссылаются, останутся, но чат сообщит, "
            "что модель не найдена.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._service.delete_llm_profile(profile_id)
        self._reload()
