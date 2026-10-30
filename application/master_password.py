# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Пароль журнала: пароль последней машины либо мастер-пароль этого запуска."""
from __future__ import annotations

import hashlib
import hmac
import threading
from typing import Optional

import domain


def connection_journal_password(
    provider: domain.ProviderType,
    ssh: Optional[domain.SSHConfig],
    auth_secret: Optional[str],
    hop_secrets: tuple[str, ...] = (),
) -> Optional[str]:
    """Пароль машины, где выполняются команды.

    Для цепочки это последний узел. Вход по ключу или агенту пароля не даёт:
    passphrase ключа журналом не является. None значит, что нужен мастер-пароль.
    """
    if ssh is None:
        return None
    if provider == domain.ProviderType.SSH_OVER_SSH and ssh.hops:
        secret = hop_secrets[-1] if hop_secrets else ""
        return _account_password(ssh.hops[-1].auth_method, secret)
    if provider == domain.ProviderType.SSH:
        return _account_password(ssh.auth_method, auth_secret)
    return None


def _account_password(
    method: domain.SSHAuthMethod, secret: Optional[str]
) -> Optional[str]:
    if method != domain.SSHAuthMethod.PASSWORD:
        return None
    if not secret:
        return None
    return secret


def _digest(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


class MasterPassword:
    """Один пароль на запуск процесса. На диск не пишется.

    При первом вводе хеш пароля сравнивается с хешем подтверждения.
    Совпадение оставляет в памяти и хеш, и сам пароль: журнал открывается паролем.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._password: Optional[str] = None
        self._digest: Optional[bytes] = None
        self._proven = False

    def ready(self) -> bool:
        with self._lock:
            return self._password is not None

    def establish(self, password: str, confirmation: str) -> None:
        if not password:
            raise ValueError("Введите мастер-пароль")
        entered = _digest(password)
        repeated = _digest(confirmation)
        if not hmac.compare_digest(entered, repeated):
            raise ValueError("Пароли не совпадают")
        with self._lock:
            if self._digest is None:
                self._digest = entered
                self._password = password
                self._proven = False
                return
            if not hmac.compare_digest(self._digest, entered):
                raise ValueError("Мастер-пароль уже задан и не совпадает")

    def prove(self) -> None:
        """Журнал принял пароль: до конца запуска он больше не спрашивается."""
        with self._lock:
            if self._password is not None:
                self._proven = True

    def forget_unproven(self) -> None:
        """Журнал отверг первый ввод. Следующая попытка спросит пароль снова."""
        with self._lock:
            if self._proven:
                return
            self._password = None
            self._digest = None

    def reveal(self) -> str:
        with self._lock:
            password = self._password
            stored = self._digest
        if password is None or stored is None:
            raise ValueError("Мастер-пароль ещё не задан")
        if not hmac.compare_digest(_digest(password), stored):
            raise ValueError("Мастер-пароль в памяти не совпал со своим хешем")
        return password
