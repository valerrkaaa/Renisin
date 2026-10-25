# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Точечная замена в файле. Остальные байты не перекодируются и не переписываются."""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass


class FileEditError(Exception):
    """Файл не изменён."""


@dataclass(frozen=True)
class FileEditView:
    ok: bool
    message: str
    context: str = ""


MAX_BYTES = 2_000_000


def prepare_edit(data: bytes, old: str, new: str) -> tuple[FileEditView, bytes | None]:
    """Вернуть новый файл только если old встречается ровно один раз."""
    if len(data) > MAX_BYTES:
        return FileEditView(False, "Файл слишком большой. Он не изменён."), None
    text = old.replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return FileEditView(False, "Пустой фрагмент. Файл не изменён."), None

    pattern, count = _locate(data, text)
    if count == 0 or not pattern:
        return (
            FileEditView(False, "Такой фрагмент в файле не найден. Файл не изменён."),
            None,
        )
    if count != 1:
        return (
            FileEditView(
                False,
                f"Фрагмент встречается {count} раз. Файл не изменён. "
                "Нужен более длинный кусок, который есть только в одном месте.",
            ),
            None,
        )

    start = data.find(pattern)
    end = start + len(pattern)
    new_bytes = _encode_new(new, pattern)
    updated = data[:start] + new_bytes + data[end:]
    if updated[:start] != data[:start] or updated[start + len(new_bytes) :] != data[end:]:
        return FileEditView(False, "Проверка замены не прошла. Файл не изменён."), None
    if len(updated) != len(data) - len(pattern) + len(new_bytes):
        return FileEditView(False, "Проверка длины не прошла. Файл не изменён."), None
    context = _context(data, start, end, new_bytes)
    return (
        FileEditView(True, "Фрагмент найден один раз. Остальной файл не меняется.", context),
        updated,
    )


def write_bytes_atomic(path: str, data: bytes) -> None:
    """Заменить файл через временный в том же каталоге. При ошибке исходный остаётся."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".vibe-", dir=directory)
    try:
        os.write(fd, data)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        os.remove(tmp)
        raise
    os.close(fd)
    try:
        os.chmod(tmp, os.stat(path).st_mode)
    except OSError:
        pass
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _locate(data: bytes, text: str) -> tuple[bytes, int]:
    lf = text.encode("utf-8")
    crlf = text.replace("\n", "\r\n").encode("utf-8")
    if b"\r\n" in data and crlf != lf:
        ordered = (crlf, lf)
    elif crlf != lf:
        ordered = (lf, crlf)
    else:
        ordered = (lf,)
    counts = [(pattern, data.count(pattern)) for pattern in ordered if pattern]
    unique = [item for item in counts if item[1] == 1]
    if unique:
        return unique[0]
    many = [item for item in counts if item[1] > 1]
    if many:
        return max(many, key=lambda item: item[1])
    return b"", 0


def _encode_new(new: str, pattern: bytes) -> bytes:
    text = new.replace("\r\n", "\n").replace("\r", "\n")
    if b"\r\n" in pattern:
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def _context(data: bytes, start: int, end: int, new_bytes: bytes) -> str:
    before = data[max(0, start - 160) : start]
    after = data[end : end + 160]

    def show(raw: bytes) -> str:
        return raw.decode("utf-8", errors="replace")

    return (
        show(before).lstrip("\n")
        + "\n--- было ---\n"
        + show(data[start:end])
        + "\n--- стало ---\n"
        + show(new_bytes)
        + "\n--- дальше ---\n"
        + show(after).rstrip("\n")
    )
