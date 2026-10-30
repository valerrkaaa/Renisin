# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Ключ из настроек: проверка файла и запись публичной части на хост."""
from __future__ import annotations

import os
from typing import Optional

import paramiko


SETUP_REQUIRED = "Нужна первичная настройка: укажите ключ SSH в настройках"

_KEY_CLASSES = (
    paramiko.Ed25519Key,
    paramiko.RSAKey,
    paramiko.ECDSAKey,
)


def require_configured_key(path: str) -> str:
    """Путь из настроек. Пустой или отсутствующий файл — первичная настройка не сделана."""
    cleaned = (path or "").strip()
    if not cleaned or not os.path.isfile(cleaned):
        raise ValueError(SETUP_REQUIRED)
    return cleaned


def load_private_key(path: str, passphrase: Optional[str]):
    password = passphrase or None
    needs_phrase = False
    for cls in _KEY_CLASSES:
        try:
            return cls.from_private_key_file(path, password=password)
        except paramiko.PasswordRequiredException:
            needs_phrase = True
        except paramiko.SSHException as exc:
            if password:
                raise ValueError("Неверная passphrase ключа") from exc
        except Exception:
            continue
    if needs_phrase and not password:
        raise paramiko.PasswordRequiredException(path)
    raise ValueError("Файл не является ключом SSH")


def public_line(key) -> str:
    return f"{key.get_name()} {key.get_base64()} vterminal"


def authorized_keys_command(line: str) -> str:
    """Команда на удалённой машине: дописать ключ, если его ещё нет."""
    safe = line.replace("'", "'\"'\"'")
    return (
        "umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qxF '{safe}' ~/.ssh/authorized_keys || "
        f"printf '%s\\n' '{safe}' >> ~/.ssh/authorized_keys"
    )


def install_authorized_key(client, line: str) -> None:
    _stdin, stdout, stderr = client.exec_command(authorized_keys_command(line))
    code = stdout.channel.recv_exit_status()
    if code == 0:
        return
    detail = stderr.read().decode("utf-8", "replace").strip()
    raise ValueError(detail or "Не удалось записать ключ в authorized_keys")


def generate_ed25519(path: str, passphrase: str = "") -> None:
    """Новый ключ. Существующий файл не перезаписывается.

    Пустая passphrase оставляет файл без шифрования. Создание из настроек
    подставляет мастер-пароль раньше и сюда он приходит уже непустым.
    """
    if os.path.exists(path):
        raise ValueError("Файл ключа уже существует")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private = ed25519.Ed25519PrivateKey.generate()
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
        if passphrase
        else serialization.NoEncryption()
    )
    blob = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=encryption,
    )
    with open(path, "wb") as handle:
        handle.write(blob)
    key = load_private_key(path, passphrase or None)
    with open(path + ".pub", "w", encoding="utf-8") as handle:
        handle.write(public_line(key) + "\n")
