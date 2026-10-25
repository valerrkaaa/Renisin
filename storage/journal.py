# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Зашифрованный журнал: отдельный файл на сессию, дозапись в конец.

Формат файла:
  Header (32 байта):
    magic       8  = b"VIBEJRL1"
    version     4  uint32 LE
    iterations  4  uint32 LE
    salt        16
  Записи (подряд до конца файла):
    length      4  uint32 LE  — длина nonce + ciphertext
    nonce       12
    ciphertext  length-12      — AES-GCM output (ciphertext + tag)

AAD каждой записи: header + uint64 LE sequence.
Ключ выводится из пароля и salt через PBKDF2-HMAC-SHA256.
"""
from __future__ import annotations

import json
import os
import struct
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import domain


@dataclass
class _UnlockedJournal:
    key: bytes
    salt: bytes
    iterations: int
    next_sequence: int
    file_path: str


class EncryptedJournalRepository:
    """Реализация JournalRepository."""

    MAGIC = b"VIBEJRL1"
    VERSION = 1
    KDF_ITERATIONS = 200_000
    HEADER_SIZE = 8 + 4 + 4 + 16
    NONCE_SIZE = 12
    MAX_RECORD_SIZE = 8 * 1024 * 1024

    def __init__(self, dir_path: str) -> None:
        self._dir = dir_path
        os.makedirs(dir_path, exist_ok=True)
        self._lock = threading.RLock()
        self._unlocked: Dict[domain.SessionId, _UnlockedJournal] = {}

    # --- внутреннее -------------------------------------------------------

    def _path(self, session_id: domain.SessionId) -> str:
        return os.path.join(self._dir, f"{session_id}.jrn")

    @staticmethod
    def _derive_key(password: str, salt: bytes, iterations: int) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        )
        return kdf.derive(password.encode("utf-8"))

    @staticmethod
    def _header(iterations: int, salt: bytes) -> bytes:
        return (
            EncryptedJournalRepository.MAGIC
            + struct.pack("<II", EncryptedJournalRepository.VERSION, iterations)
            + salt
        )

    @staticmethod
    def _aad(header: bytes, sequence: int) -> bytes:
        return header + struct.pack("<Q", sequence)

    @staticmethod
    def _to_dict(event: domain.TranscriptEvent) -> dict:
        return {
            "event_id": event.event_id,
            "session_id": event.session_id,
            "sequence": event.sequence,
            "event_type": event.event_type.value,
            "timestamp": event.timestamp.isoformat(),
            "command_id": event.command_id,
            "stream_type": event.stream_type.value if event.stream_type else None,
            "payload": event.payload,
            "return_code": event.return_code,
            "status": event.status.value if event.status else None,
        }

    @staticmethod
    def _from_dict(d: dict) -> domain.TranscriptEvent:
        return domain.TranscriptEvent(
            event_id=d["event_id"],
            session_id=d["session_id"],
            sequence=d["sequence"],
            event_type=domain.EventType(d["event_type"]),
            timestamp=datetime.fromisoformat(d["timestamp"]),
            command_id=d.get("command_id"),
            stream_type=(
                domain.StreamType(d["stream_type"]) if d.get("stream_type") else None
            ),
            payload=d.get("payload", ""),
            return_code=d.get("return_code"),
            status=(
                domain.SessionStatus(d["status"]) if d.get("status") else None
            ),
        )

    # --- JournalRepository ------------------------------------------------

    def create(self, session_id: domain.SessionId, password: str) -> None:
        if not password:
            raise ValueError("Пароль не может быть пустым")
        with self._lock:
            path = self._path(session_id)
            if os.path.exists(path):
                raise domain.JournalAlreadyExistsError(
                    f"Журнал для сессии {session_id} уже существует"
                )
            salt = os.urandom(16)
            iterations = self.KDF_ITERATIONS
            key = self._derive_key(password, salt, iterations)
            header = self._header(iterations, salt)
            try:
                with open(path, "wb") as f:
                    f.write(header)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as exc:
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise domain.JournalError(
                    f"Не удалось создать журнал: {exc}"
                ) from exc
            self._unlocked[session_id] = _UnlockedJournal(
                key=key,
                salt=salt,
                iterations=iterations,
                next_sequence=0,
                file_path=path,
            )

    def unlock(self, session_id: domain.SessionId, password: str) -> None:
        with self._lock:
            path = self._path(session_id)
            if not os.path.exists(path):
                raise domain.JournalNotFoundError(
                    f"Файл журнала для сессии {session_id} не найден"
                )
            try:
                with open(path, "rb") as f:
                    header = f.read(self.HEADER_SIZE)
                    if len(header) < self.HEADER_SIZE or header[:8] != self.MAGIC:
                        raise domain.JournalCorruptedError("Неверный заголовок журнала")
                    version, iterations = struct.unpack("<II", header[8:16])
                    salt = header[16:32]
                    if version != self.VERSION:
                        raise domain.JournalCorruptedError(
                            f"Неподдерживаемая версия журнала: {version}"
                        )
                    if iterations <= 0 or iterations > 10_000_000:
                        raise domain.JournalCorruptedError(
                            "Некорректное число итераций KDF"
                        )
                    key = self._derive_key(password, salt, iterations)
                    expected_header = self._header(iterations, salt)

                    seq = 0
                    while True:
                        len_bytes = f.read(4)
                        if not len_bytes:
                            break
                        if len(len_bytes) < 4:
                            raise domain.JournalCorruptedError("Обрезанная запись")
                        (length,) = struct.unpack("<I", len_bytes)
                        if length < self.NONCE_SIZE or length > self.MAX_RECORD_SIZE:
                            raise domain.JournalCorruptedError(
                                "Некорректная длина записи"
                            )
                        nonce = f.read(self.NONCE_SIZE)
                        if len(nonce) < self.NONCE_SIZE:
                            raise domain.JournalCorruptedError("Обрезанный nonce")
                        ct = f.read(length - self.NONCE_SIZE)
                        if len(ct) < length - self.NONCE_SIZE:
                            raise domain.JournalCorruptedError("Обрезанная запись")

                        aad = self._aad(expected_header, seq)
                        try:
                            plaintext = AESGCM(key).decrypt(nonce, ct, aad)
                        except Exception as exc:
                            raise domain.JournalAuthError(
                                "Неверный пароль или повреждение журнала"
                            ) from exc
                        try:
                            json.loads(plaintext.decode("utf-8"))
                        except Exception as exc:
                            raise domain.JournalCorruptedError(
                                "Повреждённая запись журнала"
                            ) from exc
                        seq += 1
            except domain.JournalError:
                raise
            except OSError as exc:
                raise domain.JournalError(
                    f"Ошибка чтения журнала: {exc}"
                ) from exc

            self._unlocked[session_id] = _UnlockedJournal(
                key=key,
                salt=salt,
                iterations=iterations,
                next_sequence=seq,
                file_path=path,
            )

    def lock(self, session_id: domain.SessionId) -> None:
        with self._lock:
            self._unlocked.pop(session_id, None)

    def is_unlocked(self, session_id: domain.SessionId) -> bool:
        with self._lock:
            return session_id in self._unlocked

    def next_sequence(self, session_id: domain.SessionId) -> int:
        with self._lock:
            state = self._unlocked.get(session_id)
            if state is None:
                raise domain.JournalLockedError("Журнал не разблокирован")
            return state.next_sequence

    def append(self, event: domain.TranscriptEvent) -> None:
        with self._lock:
            state = self._unlocked.get(event.session_id)
            if state is None:
                raise domain.JournalLockedError("Журнал не разблокирован")
            if event.sequence != state.next_sequence:
                raise domain.JournalCorruptedError(
                    f"Нарушен порядок событий: ожидалось {state.next_sequence}, "
                    f"получено {event.sequence}"
                )

            payload = json.dumps(
                self._to_dict(event), ensure_ascii=False
            ).encode("utf-8")
            nonce = os.urandom(self.NONCE_SIZE)
            header = self._header(state.iterations, state.salt)
            aad = self._aad(header, event.sequence)
            ct = AESGCM(state.key).encrypt(nonce, payload, aad)
            length = self.NONCE_SIZE + len(ct)

            try:
                with open(state.file_path, "ab") as f:
                    f.write(struct.pack("<I", length))
                    f.write(nonce)
                    f.write(ct)
                    f.flush()
                    if event.event_type == domain.EventType.COMMAND_FINISHED:
                        os.fsync(f.fileno())
            except OSError as exc:
                raise domain.JournalError(
                    f"Не удалось записать событие в журнал: {exc}"
                ) from exc

            state.next_sequence += 1

    def get_by_session(
        self, session_id: domain.SessionId
    ) -> List[domain.TranscriptEvent]:
        with self._lock:
            state = self._unlocked.get(session_id)
            if state is None:
                raise domain.JournalLockedError("Журнал не разблокирован")
            return self._read_all(state)

    def delete(self, session_id: domain.SessionId) -> None:
        with self._lock:
            self._unlocked.pop(session_id, None)
            path = self._path(session_id)
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    # --- чтение всех записей ---------------------------------------------

    def _read_all(
        self, state: _UnlockedJournal
    ) -> List[domain.TranscriptEvent]:
        header = self._header(state.iterations, state.salt)
        events: List[domain.TranscriptEvent] = []
        try:
            with open(state.file_path, "rb") as f:
                f.read(self.HEADER_SIZE)
                seq = 0
                while True:
                    len_bytes = f.read(4)
                    if not len_bytes:
                        break
                    if len(len_bytes) < 4:
                        raise domain.JournalCorruptedError("Обрезанная запись")
                    (length,) = struct.unpack("<I", len_bytes)
                    if length < self.NONCE_SIZE or length > self.MAX_RECORD_SIZE:
                        raise domain.JournalCorruptedError(
                            "Некорректная длина записи"
                        )
                    nonce = f.read(self.NONCE_SIZE)
                    ct = f.read(length - self.NONCE_SIZE)
                    if (
                        len(nonce) < self.NONCE_SIZE
                        or len(ct) < length - self.NONCE_SIZE
                    ):
                        raise domain.JournalCorruptedError("Обрезанная запись")
                    aad = self._aad(header, seq)
                    try:
                        plaintext = AESGCM(state.key).decrypt(nonce, ct, aad)
                    except Exception as exc:
                        raise domain.JournalAuthError(
                            "Неверный пароль или повреждение журнала"
                        ) from exc
                    d = json.loads(plaintext.decode("utf-8"))
                    events.append(self._from_dict(d))
                    seq += 1
        except domain.JournalError:
            raise
        except OSError as exc:
            raise domain.JournalError(
                f"Ошибка чтения журнала: {exc}"
            ) from exc
        return events