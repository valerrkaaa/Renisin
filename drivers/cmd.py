# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Драйвер локального CMD.

Запускает каждую команду в ConPTY через `cmd.exe /c <временный .bat>`
от имени текущего пользователя. stdin остаётся открытым на время команды,
рабочая директория сохраняется между командами.
"""
from __future__ import annotations

import codecs
import os
import queue
import tempfile
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, List, Optional

from winpty import PtyProcess

import domain
import file_edit
import shell_takeover
from drivers.ssh import SudoFence
from screen_log import ScreenLog


def _pick_console_encoding() -> str:
    """Подобрать кодировку вывода CMD, не падая на незнакомых локалях."""
    for name in ("oem", "cp866", "cp1251", "mbcs"):
        try:
            codecs.lookup(name)
            return name
        except LookupError:
            continue
    return "utf-8"


class CmdSessionDriver:
    """Реализация SessionDriver для локального CMD."""

    def __init__(self) -> None:
        self._sink: Optional[Callable[[domain.OutputEvent], None]] = None
        self._session_id: Optional[domain.SessionId] = None
        self._running = False
        self._closed = False
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._encoding = _pick_console_encoding()

        self._cwd: str = os.getcwd()
        self._cwd_file: Optional[str] = None
        self._bat_file: Optional[str] = None
        self._cols = 80
        self._rows = 24
        self._pty: Optional[PtyProcess] = None
        self._screen: Optional[ScreenLog] = None
        self._live_request: Optional[domain.CommandRequest] = None
        self._pending_input: List[str] = []
        self._cancel = False
        self._shell_labels: List[str] = []
        self._attached_pty: Optional[PtyProcess] = None
        self._attached_queue: Optional[queue.Queue] = None
        self._reader: Optional[threading.Thread] = None
        self._reader_pty: Optional[PtyProcess] = None

    # --- SessionDriver ----------------------------------------------------

    def set_event_sink(self, sink: Callable[[domain.OutputEvent], None]) -> None:
        self._sink = sink

    def open(self, config: domain.SessionConfig) -> domain.SessionId:
        if config.provider != domain.ProviderType.CMD:
            raise ValueError(f"Неподдерживаемый провайдер: {config.provider!r}")
        if config.identity != domain.ExecutionIdentityType.CURRENT_USER:
            raise ValueError("Этап 2.1 поддерживает только текущего пользователя")

        with self._lock:
            self._session_id = config.session_id
            self._closed = False
            self._cwd = os.getcwd()
            self._cwd_file = self._create_temp(suffix=".txt")
            self._bat_file = self._create_temp(suffix=".bat")
            session_id = self._session_id

        self._emit(domain.OutputEvent(
            session_id=session_id,
            event_type=domain.EventType.SESSION_STATUS,
            timestamp=datetime.now(),
            stream_type=domain.StreamType.SYSTEM,
            status=domain.SessionStatus.READY,
            payload="ready",
        ))
        return session_id
    
    def complete(
        self, session_id: domain.SessionId, text: str
    ) -> List[str]:
        if session_id != self._session_id:
            return []
        with self._lock:
            if self._closed:
                return []
            cwd = self._cwd

        # Незавершённый последний токен: если ввод заканчивается пробелом —
        # пользователь начал новое слово, дополнять нечего.
        if not text or text[-1].isspace():
            return []

        # Разбить на "префикс + последний токен".
        idx = max(text.rfind(" "), text.rfind("\t"))
        if idx >= 0:
            head = text[: idx + 1]
            token = text[idx + 1 :]
        else:
            head = ""
            token = text

        # Учесть возможные кавычки вокруг токена.
        opening_quote = ""
        if token.startswith('"'):
            opening_quote = '"'
            token = token[1:]

        # Разделить токен на dir_part и name_part по последнему слэшу.
        sep_idx = max(token.rfind("\\"), token.rfind("/"))
        if sep_idx >= 0:
            dir_part = token[: sep_idx + 1]
            name_part = token[sep_idx + 1 :]
        else:
            dir_part = ""
            name_part = token

        target_dir = (
            os.path.join(cwd, dir_part) if dir_part else cwd
        )
        try:
            if not os.path.isdir(target_dir):
                return []
            entries = os.listdir(target_dir)
        except OSError:
            return []

        name_lower = name_part.lower()
        matches: List[str] = []
        for entry in entries:
            if not entry.lower().startswith(name_lower):
                continue
            full_path = os.path.join(target_dir, entry)
            is_dir = os.path.isdir(full_path)
            completion = dir_part + entry + (os.sep if is_dir else "")

            # Если в пути есть пробел — обернуть в кавычки CMD.
            if " " in completion:
                if opening_quote:
                    completion = f'"{completion}"'
                else:
                    completion = f'"{completion}"'
            elif opening_quote:
                completion = f'"{completion}"'

            matches.append(completion)

        matches.sort(key=str.lower)
        return matches

    def write_input(self, session_id: domain.SessionId, data: str) -> None:
        if not data:
            return
        with self._lock:
            if session_id != self._session_id or self._closed:
                return
            pty = self._pty
            if pty is None:
                self._pending_input.append(data)
                return
        try:
            pty.write(data)
        except Exception:
            pass

    def resize(self, session_id: domain.SessionId, cols: int, rows: int) -> None:
        cols = max(2, min(int(cols), 500))
        rows = max(2, min(int(rows), 200))
        with self._lock:
            if session_id != self._session_id or self._closed:
                return
            self._cols = cols
            self._rows = rows
            pty = self._pty or self._attached_pty
            screen = self._screen
        if screen is not None:
            with self._io_lock:
                screen.resize(cols, rows)
        if pty is not None:
            try:
                pty.setwinsize(rows, cols)
            except Exception:
                pass
        if screen is not None:
            self._emit_size(cols, rows)

    def send(self, request: domain.CommandRequest) -> None:
        with self._lock:
            if self._closed or self._session_id is None:
                raise RuntimeError("Сессия закрыта")
            if self._running:
                raise RuntimeError("Другая команда уже выполняется")
            self._running = True
            nested = bool(self._shell_labels) and self._attached_pty is not None

        self._emit(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_SENT,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload=request.text,
        ))

        target = self._run_attached if nested else self._run_command
        threading.Thread(target=target, args=(request,), daemon=True).start()

    def cancel(self, session_id: domain.SessionId) -> None:
        with self._lock:
            if session_id != self._session_id or not self._running:
                return
            self._cancel = True
            pty = self._pty
        if pty is not None:
            try:
                if pty.isalive():
                    pty.terminate(force=True)
            except Exception:
                pass

    def preview_edit(self, session_id: domain.SessionId, path: str, old: str, new: str):
        data, _resolved = self._read_local(session_id, path)
        view, _updated = file_edit.prepare_edit(data, old, new)
        return view

    def apply_edit(self, session_id: domain.SessionId, path: str, old: str, new: str) -> str:
        data, resolved = self._read_local(session_id, path)
        view, updated = file_edit.prepare_edit(data, old, new)
        if not view.ok or updated is None:
            raise file_edit.FileEditError(view.message)
        file_edit.write_bytes_atomic(resolved, updated)
        return f"Заменено в {resolved}. Остальные байты файла не менялись."

    def _read_local(self, session_id: domain.SessionId, path: str) -> tuple:
        with self._lock:
            if session_id != self._session_id or self._closed:
                raise file_edit.FileEditError("Сессия недоступна")
            cwd = self._cwd
        cleaned = path.strip().strip('"').strip("'")
        if not cleaned or "\n" in cleaned:
            raise file_edit.FileEditError("Не указан путь к файлу")
        resolved = cleaned if os.path.isabs(cleaned) else os.path.join(cwd, cleaned)
        resolved = os.path.normpath(resolved)
        if not os.path.isfile(resolved):
            raise file_edit.FileEditError("Файл не найден. Новый файл не создаётся.")
        try:
            with open(resolved, "rb") as handle:
                data = handle.read(file_edit.MAX_BYTES + 1)
        except OSError as exc:
            raise file_edit.FileEditError(f"Не удалось прочитать файл: {exc}") from exc
        return data, resolved

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._pty
            self._pty = None
            session_id = self._session_id
            cwd_file = self._cwd_file
            bat_file = self._bat_file
            self._cwd_file = None
            self._bat_file = None

        if process is not None:
            try:
                if process.isalive():
                    process.terminate(force=True)
            except Exception:
                pass

        for path in (cwd_file, bat_file):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

        if session_id is not None:
            self._emit(domain.OutputEvent(
                session_id=session_id,
                event_type=domain.EventType.SESSION_STATUS,
                timestamp=datetime.now(),
                stream_type=domain.StreamType.SYSTEM,
                status=domain.SessionStatus.CLOSED,
                payload="closed",
            ))

    # --- внутреннее -------------------------------------------------------

    @staticmethod
    def _create_temp(suffix: str) -> str:
        fd, path = tempfile.mkstemp(prefix="vibe_", suffix=suffix)
        os.close(fd)
        return path

    def _write_batch(self, text: str) -> None:
        assert self._bat_file is not None
        assert self._cwd_file is not None
        enc = self._encoding
        with open(self._bat_file, "wb") as f:
            def w(s: str) -> None:
                f.write(s.encode(enc, errors="replace"))
                f.write(b"\r\n")
            w("@echo off")
            w(text)
            w(f'cd > "{self._cwd_file}"')

    def _read_new_cwd(self) -> None:
        path = self._cwd_file
        if not path:
            return
        try:
            with open(path, "r", encoding=self._encoding, errors="replace") as f:
                line = f.read().strip()
        except OSError:
            return
        line = line.strip().strip('"')
        if line and os.path.isdir(line):
            self._cwd = line

    def _run_command(self, request: domain.CommandRequest) -> None:
        try:
            self._write_batch(request.text)
            assert self._bat_file is not None
            with self._lock:
                cols, rows = self._cols, self._rows
            pty = PtyProcess.spawn(
                ["cmd.exe", "/c", self._bat_file],
                cwd=self._cwd,
                dimensions=(rows, cols),
            )
        except Exception as exc:
            self._emit_error(request, f"Не удалось запустить процесс: {exc}")
            self._finish(request, return_code=None)
            return

        screen = ScreenLog(cols, rows)
        with self._lock:
            if self._closed or self._cancel:
                self._cancel = False
                try:
                    pty.terminate(force=True)
                except Exception:
                    pass
                self._finish(request, return_code=None)
                return
            self._pty = pty
            self._screen = screen
            self._live_request = request
            pending = "".join(self._pending_input)
            self._pending_input.clear()
        if pending:
            try:
                pty.write(pending)
            except Exception:
                pass

        handed = False
        try:
            if shell_takeover.opens_shell(request.text):
                self._start_reader(pty)
                handed = self._pump_shell_launch(request, pty, screen)
            else:
                while True:
                    try:
                        chunk = pty.read(4096)
                    except EOFError:
                        break
                    if not chunk:
                        break
                    self._show_live(request, screen, chunk)
        finally:
            with self._lock:
                if self._pty is pty:
                    self._pty = None

        if handed:
            return

        return_code = self._wait_exit(pty)
        with self._lock:
            self._screen = None
            self._live_request = None
        self._read_new_cwd()
        snapshot = screen.primary_text()
        if snapshot:
            self._emit_chunk(request, snapshot, persist=True)
        self._finish(request, return_code=return_code)

    def _show_live(
        self, request: domain.CommandRequest, screen: ScreenLog, text: str
    ) -> None:
        if not text:
            return
        with self._io_lock:
            screen.feed(text)
            fed_cols, fed_rows = screen.columns, screen.lines
            fed_alt = screen.in_alternate_screen
        self._emit_chunk(
            request,
            text,
            persist=False,
            columns=fed_cols,
            rows=fed_rows,
            alternate=fed_alt,
        )

    def _prompt_ready(self, watch: shell_takeover.PromptWatch, screen: ScreenLog) -> bool:
        with self._io_lock:
            alternate = screen.in_alternate_screen
            line = shell_takeover.last_text_line(screen.primary_text())
        return watch.ready(line, alternate)

    def _start_reader(self, pty: PtyProcess) -> None:
        with self._lock:
            if (
                self._reader_pty is pty
                and self._reader is not None
                and self._reader.is_alive()
                and self._attached_queue is not None
            ):
                return
            chunks: queue.Queue = queue.Queue()
            self._attached_queue = chunks
            self._reader_pty = pty

        def loop() -> None:
            while True:
                try:
                    chunk = pty.read(4096)
                except Exception:
                    chunks.put(None)
                    return
                if not chunk:
                    chunks.put(None)
                    return
                chunks.put(chunk)

        thread = threading.Thread(target=loop, daemon=True)
        with self._lock:
            self._reader = thread
        thread.start()

    def _next_chunk(self, timeout: float):
        chunks = self._attached_queue
        if chunks is None:
            return "eof", ""
        try:
            item = chunks.get(timeout=timeout)
        except queue.Empty:
            return "timeout", ""
        if item is None:
            return "eof", ""
        return "data", item

    def _pump_shell_launch(
        self,
        request: domain.CommandRequest,
        pty: PtyProcess,
        screen: ScreenLog,
    ) -> bool:
        watch = shell_takeover.PromptWatch()
        while True:
            kind, chunk = self._next_chunk(0.3)
            if kind == "timeout":
                if self._prompt_ready(watch, screen):
                    self._handoff_shell(request, pty, screen)
                    return True
                continue
            if kind != "data":
                return False
            watch.mark()
            self._show_live(request, screen, chunk)

    def _handoff_shell(
        self,
        request: domain.CommandRequest,
        pty: PtyProcess,
        screen: ScreenLog,
    ) -> None:
        with self._io_lock:
            line = shell_takeover.last_text_line(screen.primary_text())
            snapshot = screen.primary_text()
        self._silence_echo(pty)
        with self._lock:
            self._shell_labels.append(shell_takeover.prompt_label(line))
            self._attached_pty = pty
        self._emit_shell_context()
        if snapshot:
            self._emit_chunk(request, snapshot, persist=True)
        self._finish(request, 0)

    def _silence_echo(self, pty: PtyProcess) -> None:
        try:
            pty.write(shell_takeover.silence_command())
        except Exception:
            return
        watch = shell_takeover.PromptWatch(idle_s=0.3)
        screen = ScreenLog(self._cols, self._rows)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            kind, chunk = self._next_chunk(0.3)
            if kind == "timeout":
                if self._prompt_ready(watch, screen):
                    return
                continue
            if kind != "data":
                return
            watch.mark()
            screen.feed(chunk)

    def _emit_shell_context(self) -> None:
        with self._lock:
            session_id = self._session_id
            label = self._shell_labels[-1] if self._shell_labels else ""
        if session_id is None:
            return
        self._emit(domain.OutputEvent(
            session_id=session_id,
            event_type=domain.EventType.SHELL_CONTEXT,
            timestamp=datetime.now(),
            stream_type=domain.StreamType.SYSTEM,
            payload=label,
            persist=False,
        ))

    def _clear_shell(self) -> None:
        with self._lock:
            had = bool(self._shell_labels) or self._attached_pty is not None
            self._shell_labels.clear()
            self._attached_pty = None
        if had:
            self._emit_shell_context()

    def _pop_shell(self) -> None:
        with self._lock:
            if self._shell_labels:
                self._shell_labels.pop()
            if not self._shell_labels:
                self._attached_pty = None
        self._emit_shell_context()

    def _run_attached(self, request: domain.CommandRequest) -> None:
        with self._lock:
            pty = self._attached_pty
            cols, rows = self._cols, self._rows
        if pty is None:
            self._clear_shell()
            self._emit_error(request, "Оболочка уже закрыта")
            self._finish(request, None)
            return
        screen = ScreenLog(cols, rows)
        with self._lock:
            if self._closed or self._cancel:
                self._cancel = False
                self._finish(request, None)
                return
            self._pty = pty
            self._screen = screen
            self._live_request = request
            pending = "".join(self._pending_input)
            self._pending_input.clear()
        try:
            if shell_takeover.is_exit_command(request.text):
                pty.write("exit\n")
                self._read_shell_transition(request, screen, mode="exit")
                return
            if shell_takeover.opens_shell(request.text):
                line = request.text.replace("\r\n", "\n").replace("\r", "\n")
                pty.write(line.rstrip("\n") + "\n")
                if pending:
                    pty.write(pending)
                self._read_shell_transition(request, screen, mode="enter")
                return
            tag = uuid.uuid4().hex
            pty.write(shell_takeover.wrap_command(request.text, tag))
            if pending:
                pty.write(pending)
            self._read_marked_command(request, screen, tag)
        except Exception as exc:
            self._emit_error(request, f"Не удалось выполнить команду: {exc}")
            self._finish(request, None)
        finally:
            with self._lock:
                if self._pty is pty:
                    self._pty = None
                if self._screen is screen:
                    self._screen = None
                if self._live_request is request:
                    self._live_request = None

    def _read_shell_transition(
        self,
        request: domain.CommandRequest,
        screen: ScreenLog,
        *,
        mode: str,
    ) -> None:
        watch = shell_takeover.PromptWatch()
        while True:
            kind, chunk = self._next_chunk(0.3)
            if kind == "timeout":
                if not self._prompt_ready(watch, screen):
                    continue
                if mode == "exit":
                    self._pop_shell()
                    self._finish_screen(request, screen, 0)
                else:
                    with self._lock:
                        pty = self._attached_pty
                    if pty is not None:
                        self._handoff_shell(request, pty, screen)
                return
            if kind != "data":
                self._clear_shell()
                self._finish_screen(request, screen, None)
                return
            watch.mark()
            self._show_live(request, screen, chunk)

    def _read_marked_command(
        self,
        request: domain.CommandRequest,
        screen: ScreenLog,
        tag: str,
    ) -> None:
        fence = SudoFence(tag)
        cancelled_at: Optional[float] = None
        while fence.exit_code is None:
            with self._lock:
                cancelled = self._cancel
            if cancelled and cancelled_at is None:
                cancelled_at = time.monotonic()
            kind, chunk = self._next_chunk(0.3)
            if kind == "timeout":
                if (
                    cancelled_at is not None
                    and time.monotonic() - cancelled_at > 2
                ):
                    break
                continue
            if kind != "data":
                self._clear_shell()
                break
            shown = fence.feed(chunk)
            if shown:
                self._show_live(request, screen, shown)
        if fence.exit_code is None:
            self._show_live(request, screen, fence.flush())
        self._finish_screen(request, screen, fence.exit_code)

    def _finish_screen(
        self,
        request: domain.CommandRequest,
        screen: ScreenLog,
        return_code: Optional[int],
    ) -> None:
        snapshot = screen.primary_text()
        if snapshot:
            self._emit_chunk(request, snapshot, persist=True)
        self._finish(request, return_code)

    @staticmethod
    def _wait_exit(pty: PtyProcess) -> Optional[int]:
        code = pty.exitstatus
        if code is not None:
            return code
        for _ in range(40):
            if not pty.isalive():
                return pty.exitstatus
            time.sleep(0.05)
        return pty.exitstatus

    def _emit_size(self, cols: int, rows: int) -> None:
        with self._lock:
            request = self._live_request
        if request is None:
            return
        with self._io_lock:
            screen = self._screen
            alternate = bool(screen is not None and screen.in_alternate_screen)
        self._emit_chunk(
            request, "", persist=False, columns=cols, rows=rows, alternate=alternate
        )

    def _emit_chunk(
        self,
        request: domain.CommandRequest,
        text: str,
        persist: bool,
        columns: Optional[int] = None,
        rows: Optional[int] = None,
        alternate: bool = False,
    ) -> None:
        self._emit(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.OUTPUT_CHUNK,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.STDOUT,
            payload=text,
            persist=persist,
            columns=columns,
            rows=rows,
            alternate=alternate,
        ))

    def _emit_error(self, request: domain.CommandRequest, message: str) -> None:
        self._emit(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.ERROR,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload=message,
        ))

    def _finish(
        self,
        request: domain.CommandRequest,
        return_code: Optional[int],
    ) -> None:
        with self._lock:
            if self._live_request is not None and self._live_request is not request:
                return
            if not self._running and self._live_request is None:
                return
            self._running = False
            self._live_request = None
            self._pty = None
            self._cancel = False
        self._emit(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_FINISHED,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload="",
            return_code=return_code,
        ))

    def _emit(self, event: domain.OutputEvent) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            sink(event)
        except Exception:
            pass