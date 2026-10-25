# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""SSH-драйвер на базе paramiko."""
from __future__ import annotations

import base64
import codecs
import hashlib
import posixpath
import re
import shlex
import socket
import stat
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, List, Optional

import paramiko

import domain
import file_edit
import shell_takeover
from application.ports import HostKeyVerifier
from screen_log import ScreenLog


def _fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


_SUDO_RE = re.compile(r"(^|[\s;&|()])sudo(\s|$|[;&|()])")

# Один PTY на всё SSH-подключение. sudo пишет билет в свою службу
# на этой ОС (tty ticket) и сам решает, когда спросить пароль снова.
# Пароль в процесс клиента и во временные файлы не попадает.
_SUDO_BROKER = """\
stty -echo 2>/dev/null || true
printf '\\n__VIBE_READY__\\n'
while IFS= read -r __vibe_line; do
    case $__vibe_line in
        __VIBE_RUN__*)
            __vibe_rest=${__vibe_line#__VIBE_RUN__}
            __vibe_tag=${__vibe_rest%%__*}
            __vibe_path=${__vibe_rest#*__}
            stty echo 2>/dev/null || true
            bash --noprofile --norc "$__vibe_path"
            __vibe_ec=$?
            rm -f "$__vibe_path" 2>/dev/null || true
            stty -echo 2>/dev/null || true
            printf '\\r\\n__VIBE_DONE__%s__%s__\\r\\n' "$__vibe_tag" "$__vibe_ec"
            ;;
    esac
done
"""


class SudoFence:
    """Прячет служебную метку конца команды и забирает код возврата."""

    def __init__(self, tag: str) -> None:
        self._marker = f"__VIBE_DONE__{tag}__"
        self._buf = ""
        self.exit_code: Optional[int] = None

    def feed(self, text: str) -> str:
        if not text or self.exit_code is not None:
            return ""
        self._buf += text
        index = self._buf.find(self._marker)
        if index < 0:
            hold = _marker_prefix_len(self._buf, self._marker)
            if hold == 0:
                visible = self._buf
                self._buf = ""
                return visible
            if hold == len(self._buf):
                return ""
            visible = self._buf[:-hold]
            self._buf = self._buf[-hold:]
            return visible
        visible = self._buf[:index]
        tail = self._buf[index + len(self._marker) :]
        matched = re.match(r"(-?\d+)__", tail)
        if matched is None:
            self._buf = self._buf[index:]
            return _strip_broker_newline(visible)
        self.exit_code = int(matched.group(1))
        self._buf = ""
        return _strip_broker_newline(visible)

    def flush(self) -> str:
        if self.exit_code is not None:
            return ""
        visible = self._buf
        self._buf = ""
        return visible


def _marker_prefix_len(buf: str, marker: str) -> int:
    """Хвост buf, который ещё может оказаться началом служебной метки.

    Остальной текст отдаётся сразу: приглашения вроде «New password:»
    не ждут следующего нажатия клавиши.
    """
    limit = min(len(buf), len(marker) - 1)
    for size in range(limit, 0, -1):
        if marker.startswith(buf[-size:]):
            return size
    return 0


def _strip_broker_newline(visible: str) -> str:
    if visible.endswith("\r\n"):
        return visible[:-2]
    if visible.endswith("\n") or visible.endswith("\r"):
        return visible[:-1]
    return visible


# Скрипт для удалённого bash: восстанавливает контекст дополнения
# и печатает COMPREPLY построчно. Учитывает префикс sudo/env/nice и т.п.
_BASH_COMPLETE_SCRIPT = r'''
set +e
__vibe_line=$(printf '%s' "$1" | base64 -d 2>/dev/null)
__vibe_cwd="$2"
if [ -n "$__vibe_cwd" ]; then
    cd -- "$__vibe_cwd" 2>/dev/null || true
fi

if [ -f /usr/share/bash-completion/bash_completion ]; then
    . /usr/share/bash-completion/bash_completion 2>/dev/null
elif [ -f /etc/bash_completion ]; then
    . /etc/bash_completion 2>/dev/null
fi

COMP_LINE="$__vibe_line"
COMP_POINT=${#COMP_LINE}

COMP_WORDS=()
eval "COMP_WORDS=($__vibe_line)" 2>/dev/null
case "$__vibe_line" in
    *[[:space:]]) COMP_WORDS+=("") ;;
esac
if [ ${#COMP_WORDS[@]} -eq 0 ] && [ -n "$__vibe_line" ]; then
    read -ra COMP_WORDS <<< "$__vibe_line"
fi
if [ ${#COMP_WORDS[@]} -eq 0 ]; then
    COMP_WORDS=("")
fi

COMP_CWORD=$((${#COMP_WORDS[@]} - 1))
COMP_WORDBREAKS=${COMP_WORDBREAKS:-"'><=;|&(:"}

# Пропускаем sudo/env/nice/... и их опции, чтобы найти реальную команду.
__vibe_shift=0
while [ $__vibe_shift -lt $COMP_CWORD ]; do
    case "${COMP_WORDS[$__vibe_shift]}" in
        sudo|doas|nohup|time|command|builtin|exec|env)
            __vibe_shift=$((__vibe_shift+1))
            while [ $__vibe_shift -lt $COMP_CWORD ]; do
                case "${COMP_WORDS[$__vibe_shift]}" in
                    -*|*=*) __vibe_shift=$((__vibe_shift+1)) ;;
                    *) break ;;
                esac
            done
            ;;
        *) break ;;
    esac
done

__vibe_eff_cword=$((COMP_CWORD - __vibe_shift))
__vibe_cur="${COMP_WORDS[$COMP_CWORD]}"
if [ "$COMP_CWORD" -gt 0 ]; then
    __vibe_prev="${COMP_WORDS[$((COMP_CWORD-1))]}"
else
    __vibe_prev=""
fi
__vibe_cmd="${COMP_WORDS[$__vibe_shift]}"

COMPREPLY=()

if [ "$__vibe_eff_cword" -eq 0 ]; then
    while IFS= read -r __vibe_r; do
        [ -n "$__vibe_r" ] && COMPREPLY+=("$__vibe_r")
    done < <(compgen -c -- "$__vibe_cur" 2>/dev/null | sort -u | head -n 300)
else
    if type _completion_loader >/dev/null 2>&1; then
        _completion_loader "$__vibe_cmd" 2>/dev/null
    fi
    __vibe_spec=$(complete -p "$__vibe_cmd" 2>/dev/null)
    __vibe_func=$(printf '%s' "$__vibe_spec" | sed -n 's/.*-F \([^ ]*\).*/\1/p')
    if [ -n "$__vibe_func" ]; then
        __vibe_saved_words=("${COMP_WORDS[@]}")
        __vibe_saved_cword=$COMP_CWORD
        COMP_WORDS=("${__vibe_saved_words[@]:$__vibe_shift}")
        COMP_CWORD=$__vibe_eff_cword
        "$__vibe_func" "$__vibe_cmd" "$__vibe_cur" "$__vibe_prev" 2>/dev/null
        COMP_WORDS=("${__vibe_saved_words[@]}")
        COMP_CWORD=$__vibe_saved_cword
    fi
    if [ ${#COMPREPLY[@]} -eq 0 ]; then
        while IFS= read -r __vibe_r; do
            [ -n "$__vibe_r" ] && COMPREPLY+=("$__vibe_r")
        done < <(compgen -f -- "$__vibe_cur" 2>/dev/null | head -n 300)
    fi
fi

for __vibe_r in "${COMPREPLY[@]}"; do
    printf '%s\n' "$__vibe_r"
done
'''


class _VerifierPolicy(paramiko.MissingHostKeyPolicy):
    """Вызывает verifier для неизвестного ключа."""

    def __init__(self, verifier: HostKeyVerifier, host: str, port: int) -> None:
        self._verifier = verifier
        self._host = host
        self._port = port

    def missing_host_key(self, client, hostname, key):
        is_known = bool(client.get_host_keys().lookup(hostname))
        decision = self._verifier(domain.HostKeyPrompt(
            host=self._host,
            port=self._port,
            key_type=key.get_name(),
            fingerprint=_fingerprint(key),
            is_known=is_known,
            is_changed=False,
        ))
        if not decision.accept:
            raise paramiko.SSHException("Ключ хоста отклонён пользователем")
        if decision.remember:
            client.get_host_keys().add(hostname, key.get_name(), key)


class SSHSessionDriver:
    """Реализация SessionDriver для SSH."""

    def __init__(
        self,
        host_key_verifier: HostKeyVerifier,
        known_hosts_path: str,
    ) -> None:
        self._host_key_verifier = host_key_verifier
        self._known_hosts_path = known_hosts_path

        self._sink: Optional[Callable[[domain.OutputEvent], None]] = None
        self._session_id: Optional[domain.SessionId] = None
        self._client: Optional[paramiko.SSHClient] = None
        self._running = False
        self._closed = False
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._cwd: Optional[str] = None
        self._monitor_stop = threading.Event()
        self._sudo_channel: Optional[paramiko.Channel] = None
        self._cols = 80
        self._rows = 24
        self._channel: Optional[paramiko.Channel] = None
        self._screen: Optional[ScreenLog] = None
        self._live_request: Optional[domain.CommandRequest] = None
        self._pending_input: List[str] = []
        self._cancel = False
        self._shell_labels: List[str] = []
        self._attached_channel: Optional[paramiko.Channel] = None
        self._attached_is_broker = False
        self._outer_tag = ""
        self._jump_clients: List[paramiko.SSHClient] = []

    # --- SessionDriver ----------------------------------------------------

    def set_event_sink(self, sink: Callable[[domain.OutputEvent], None]) -> None:
        self._sink = sink

    def open(self, config: domain.SessionConfig) -> domain.SessionId:
        if config.provider not in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            raise ValueError(f"Неподдерживаемый провайдер: {config.provider!r}")
        if config.ssh is None:
            raise ValueError("Не указаны параметры SSH-подключения")
        ssh = config.ssh
        client, jumps = self._connect_chain(config)

        with self._lock:
            self._client = client
            self._jump_clients = jumps
            self._session_id = config.session_id
            self._closed = False
            self._cwd = None
            self._sudo_channel = None
            self._monitor_stop.clear()

        self._emit(domain.OutputEvent(
            session_id=config.session_id,
            event_type=domain.EventType.SESSION_STATUS,
            timestamp=datetime.now(),
            stream_type=domain.StreamType.SYSTEM,
            status=domain.SessionStatus.READY,
            payload=domain.ssh_location(ssh),
        ))

        threading.Thread(
            target=self._monitor_connection, daemon=True
        ).start()

        return config.session_id

    def send(self, request: domain.CommandRequest) -> None:
        with self._lock:
            if self._closed or self._session_id is None:
                raise RuntimeError("Сессия закрыта")
            if self._running:
                raise RuntimeError("Другая команда уже выполняется")
            self._running = True
            nested = (
                bool(self._shell_labels) and self._attached_channel is not None
            )

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
            channel = self._channel
            broker = self._sudo_channel
            attached = self._attached_channel
            attached_broker = self._attached_is_broker
        if channel is None:
            return
        if channel is broker or (channel is attached and attached_broker):
            try:
                channel.send("\x03")
            except Exception:
                pass
            return
        try:
            channel.close()
        except Exception:
            pass
        if channel is attached:
            self._clear_shell()

    def preview_edit(self, session_id: domain.SessionId, path: str, old: str, new: str):
        data, _remote, _sftp, _mode = self._open_remote(session_id, path)
        try:
            view, _updated = file_edit.prepare_edit(data, old, new)
            return view
        finally:
            _sftp.close()

    def apply_edit(self, session_id: domain.SessionId, path: str, old: str, new: str) -> str:
        data, remote, sftp, mode = self._open_remote(session_id, path)
        tmp = remote + ".vibe-tmp"
        try:
            view, updated = file_edit.prepare_edit(data, old, new)
            if not view.ok or updated is None:
                raise file_edit.FileEditError(view.message)
            with sftp.open(tmp, "wb") as handle:
                handle.write(updated)
            try:
                sftp.chmod(tmp, mode & 0o777)
            except Exception:
                pass
            try:
                sftp.posix_rename(tmp, remote)
            except Exception as exc:
                try:
                    sftp.remove(tmp)
                except Exception:
                    pass
                raise file_edit.FileEditError(
                    "Не удалось заменить файл. Исходный не изменён."
                ) from exc
        finally:
            sftp.close()
        return f"Заменено в {remote}. Остальные байты файла не менялись."

    def _open_remote(self, session_id: domain.SessionId, path: str):
        with self._lock:
            if session_id != self._session_id or self._closed or self._client is None:
                raise file_edit.FileEditError("SSH-соединение не установлено")
            client = self._client
            cwd = self._cwd or ""
        cleaned = path.strip().strip('"').strip("'")
        if not cleaned or "\n" in cleaned or cleaned.startswith("-"):
            raise file_edit.FileEditError("Не указан путь к файлу")
        remote = cleaned if cleaned.startswith("/") else posixpath.join(cwd, cleaned)
        remote = posixpath.normpath(remote)
        try:
            sftp = client.open_sftp()
        except Exception as exc:
            raise file_edit.FileEditError(f"Не удалось открыть файлы на сервере: {exc}") from exc
        try:
            info = sftp.stat(remote)
        except OSError as exc:
            sftp.close()
            raise file_edit.FileEditError("Файл не найден. Новый файл не создаётся.") from exc
        if stat.S_ISDIR(info.st_mode or 0):
            sftp.close()
            raise file_edit.FileEditError("Это каталог, не файл.")
        if (info.st_size or 0) > file_edit.MAX_BYTES:
            sftp.close()
            raise file_edit.FileEditError("Файл слишком большой. Он не изменён.")
        try:
            with sftp.open(remote, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            sftp.close()
            raise file_edit.FileEditError(f"Не удалось прочитать файл: {exc}") from exc
        return data, remote, sftp, info.st_mode or 0o644

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._monitor_stop.set()
            client = self._client
            jumps = list(self._jump_clients)
            self._client = None
            self._jump_clients = []
            self._sudo_channel = None
            session_id = self._session_id

        for item in (client, *reversed(jumps)):
            if item is None:
                continue
            try:
                item.close()
            except Exception:
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

    def write_input(self, session_id: domain.SessionId, data: str) -> None:
        if not data:
            return
        with self._lock:
            if session_id != self._session_id or self._closed:
                return
            channel = self._channel
            if channel is None:
                self._pending_input.append(data)
                return
        try:
            channel.send(data)
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
            channel = self._channel or self._attached_channel
            screen = self._screen
        if screen is not None:
            with self._io_lock:
                screen.resize(cols, rows)
        if channel is not None:
            try:
                channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass
        if screen is not None:
            self._emit_size(cols, rows)

    # --- автодополнение ---------------------------------------------------

    def complete(
        self, session_id: domain.SessionId, text: str
    ) -> List[str]:
        with self._lock:
            if session_id != self._session_id or self._closed:
                return []
            if self._running or self._shell_labels:
                return []
            client = self._client
            cwd = self._cwd

        if client is None:
            return []
        if not text or text[-1].isspace():
            return []

        idx = max(text.rfind(" "), text.rfind("\t"))
        token_full = text[idx + 1:] if idx >= 0 else text

        opening_quote = ""
        if token_full.startswith("'") or token_full.startswith('"'):
            opening_quote = token_full[0]

        completions = self._complete_via_bash(client, cwd, text)
        if not completions:
            return []

        matches: List[str] = []
        for c in completions:
            completion = c
            if any(ch in completion for ch in " \t\"'\\$`&|;<>()*?[]{}~!#"):
                if opening_quote == "'":
                    escaped = completion.replace("'", "'\\''")
                    completion = "'" + escaped + "'"
                elif opening_quote == '"':
                    escaped = completion.replace('"', '\\"')
                    completion = '"' + escaped + '"'
                else:
                    completion = shlex.quote(completion)
            matches.append(completion)

        seen: set = set()
        unique: List[str] = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        unique.sort(key=str.lower)
        return unique

    def _complete_via_bash(
        self,
        client: paramiko.SSHClient,
        cwd: Optional[str],
        line: str,
    ) -> List[str]:
        """Спрашивает у удалённого bash список вариантов дополнения строки."""
        line_b64 = base64.b64encode(line.encode("utf-8")).decode("ascii")
        remote_cmd = (
            "bash -c " + shlex.quote(_BASH_COMPLETE_SCRIPT)
            + " -- " + shlex.quote(line_b64)
            + " " + shlex.quote(cwd or "")
        )

        try:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                return []
            chan = transport.open_session(timeout=5.0)
        except Exception:
            return []

        data = b""
        try:
            chan.settimeout(5.0)
            try:
                chan.exec_command(remote_cmd)
            except Exception:
                return []
            try:
                chan.shutdown_write()
            except Exception:
                pass
            while True:
                try:
                    chunk = chan.recv(8192)
                except socket.timeout:
                    break
                except Exception:
                    break
                if not chunk:
                    break
                data += chunk
                if len(data) > 1024 * 1024:
                    break
        finally:
            try:
                chan.close()
            except Exception:
                pass

        if not data:
            return []
        raw = data.decode("utf-8", errors="replace")
        return [ln for ln in raw.split("\n") if ln]

    # --- подключение ------------------------------------------------------

    def _connect_chain(
        self, config: domain.SessionConfig
    ) -> tuple[paramiko.SSHClient, List[paramiko.SSHClient]]:
        """Первый узел напрямую, каждый следующий — каналом через предыдущий."""
        ssh = config.ssh
        assert ssh is not None
        targets = domain.ssh_targets(ssh)
        secrets: List[Optional[str]] = [config.auth_secret, *config.hop_secrets]
        if len(secrets) < len(targets):
            raise ValueError("Не хватает пароля для узла цепочки")
        if config.provider == domain.ProviderType.SSH_OVER_SSH and len(targets) < 2:
            raise ValueError("В цепочке SSH нужно хотя бы два узла")

        opened: List[paramiko.SSHClient] = []
        sock = None
        try:
            for index, target in enumerate(targets):
                client = self._connect_one(target, secrets[index], sock)
                opened.append(client)
                if index + 1 >= len(targets):
                    break
                nxt = targets[index + 1]
                transport = client.get_transport()
                if transport is None or not transport.is_active():
                    raise ValueError(
                        f"Узел {index + 1} ({target.username}@{target.host}) "
                        "не держит соединение"
                    )
                try:
                    sock = transport.open_channel(
                        "direct-tcpip",
                        (nxt.host, int(nxt.port)),
                        ("127.0.0.1", 0),
                    )
                except Exception as exc:
                    raise ValueError(
                        f"С узла {index + 1} не открылся SSH "
                        f"к {nxt.username}@{nxt.host}:{nxt.port}: {exc}"
                    ) from exc
        except Exception:
            for client in reversed(opened):
                try:
                    client.close()
                except Exception:
                    pass
            raise
        final = opened.pop()
        return final, opened

    def _connect_one(
        self,
        ssh: domain.SSHConfig,
        secret: Optional[str],
        sock,
    ) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        try:
            client.load_host_keys(self._known_hosts_path)
        except OSError:
            pass
        client.set_missing_host_key_policy(
            _VerifierPolicy(self._host_key_verifier, ssh.host, ssh.port)
        )
        where = f"{ssh.username}@{ssh.host}:{ssh.port}"
        try:
            self._do_connect(client, ssh, secret, sock)
        except paramiko.BadHostKeyException as exc:
            decision = self._host_key_verifier(domain.HostKeyPrompt(
                host=ssh.host,
                port=ssh.port,
                key_type=exc.key.get_name(),
                fingerprint=_fingerprint(exc.key),
                is_known=True,
                is_changed=True,
                old_fingerprint=_fingerprint(exc.expected_key),
            ))
            if not decision.accept:
                client.close()
                raise ValueError(f"Ключ хоста {where} отклонён") from exc
            stored = client.get_host_keys().lookup(ssh.host)
            if stored is not None:
                stored.pop(exc.key.get_name(), None)
            if decision.remember:
                client.get_host_keys().add(ssh.host, exc.key.get_name(), exc.key)
            try:
                self._do_connect(client, ssh, secret, sock)
            except Exception as exc2:
                client.close()
                raise ValueError(
                    f"Не удалось подключиться к {where} после смены ключа: {exc2}"
                ) from exc2
        except paramiko.AuthenticationException as exc:
            client.close()
            raise ValueError(f"Ошибка аутентификации на {where}: {exc}") from exc
        except paramiko.SSHException as exc:
            client.close()
            raise ValueError(f"Ошибка SSH на {where}: {exc}") from exc
        except socket.timeout:
            client.close()
            raise ValueError(f"Таймаут подключения к {where}") from None
        except OSError as exc:
            client.close()
            raise ValueError(f"Сетевая ошибка на {where}: {exc}") from exc
        try:
            client.save_host_keys(self._known_hosts_path)
        except OSError:
            pass
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        return client

    def _do_connect(
        self,
        client: paramiko.SSHClient,
        ssh: domain.SSHConfig,
        secret: Optional[str],
        sock=None,
    ) -> None:
        common = dict(
            hostname=ssh.host,
            port=ssh.port,
            username=ssh.username,
            timeout=ssh.connect_timeout,
            banner_timeout=ssh.connect_timeout,
            auth_timeout=ssh.connect_timeout,
            sock=sock,
        )
        if ssh.auth_method == domain.SSHAuthMethod.PASSWORD:
            client.connect(
                password=secret or None,
                allow_agent=False,
                look_for_keys=False,
                **common,
            )
        elif ssh.auth_method == domain.SSHAuthMethod.PRIVATE_KEY:
            client.connect(
                key_filename=ssh.private_key_path or None,
                passphrase=secret or None,
                allow_agent=False,
                look_for_keys=False,
                **common,
            )
        elif ssh.auth_method == domain.SSHAuthMethod.AGENT:
            client.connect(
                allow_agent=True,
                look_for_keys=True,
                **common,
            )
        else:
            raise ValueError(
                f"Неизвестный способ аутентификации: {ssh.auth_method!r}"
            )

    # --- монитор соединения -----------------------------------------------

    def _monitor_connection(self) -> None:
        while not self._monitor_stop.wait(5):
            with self._lock:
                if self._closed:
                    return
                client = self._client
                session_id = self._session_id
                jumps = list(self._jump_clients)
            if client is None or session_id is None:
                return
            transport = client.get_transport()
            alive = transport is not None and transport.is_active()
            if alive:
                for jump in jumps:
                    jump_transport = jump.get_transport()
                    if jump_transport is None or not jump_transport.is_active():
                        alive = False
                        break
            if not alive:
                self._emit(domain.OutputEvent(
                    session_id=session_id,
                    event_type=domain.EventType.ERROR,
                    timestamp=datetime.now(),
                    stream_type=domain.StreamType.SYSTEM,
                    payload="SSH-соединение разорвано удалённой стороной",
                    persist=False,
                ))
                self._emit(domain.OutputEvent(
                    session_id=session_id,
                    event_type=domain.EventType.SESSION_STATUS,
                    timestamp=datetime.now(),
                    stream_type=domain.StreamType.SYSTEM,
                    status=domain.SessionStatus.ERROR,
                    payload="disconnected",
                    persist=False,
                ))
                with self._lock:
                    self._closed = True
                return

    # --- выполнение команды -----------------------------------------------

    def _build_remote_script(self, text: str, cwd_file: str) -> str:
        lines: List[str] = []
        if self._cwd:
            lines.append(f"cd -- {shlex.quote(self._cwd)} 2>/dev/null || true")
        lines.append("export TERM=${TERM:-xterm-256color}")
        lines.append(text)
        lines.append("__vibe_ec=$?")
        lines.append(f"pwd > {shlex.quote(cwd_file)}")
        lines.append("exit $__vibe_ec")
        return "\n".join(lines) + "\n"

    def _run_command(self, request: domain.CommandRequest) -> None:
        with self._lock:
            client = self._client
            cols, rows = self._cols, self._rows
        if client is None:
            self._emit_error(request, "SSH-соединение не установлено")
            self._finish(request, None)
            return

        if _SUDO_RE.search(request.text) and self._run_sudo_command(
            request, client, cols, rows
        ):
            return

        tag = uuid.uuid4().hex[:10]
        cwd_file = f"/tmp/vibe-cwd-{tag}"
        script = self._build_remote_script(request.text, cwd_file)

        try:
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("SSH-соединение не установлено")
            channel = transport.open_session(timeout=15)
            channel.get_pty(
                term="xterm-256color", width=cols, height=rows
            )
            channel.exec_command(script)
        except Exception as exc:
            self._emit_error(request, f"Не удалось выполнить команду: {exc}")
            self._finish(request, None)
            return

        screen = ScreenLog(cols, rows)
        with self._lock:
            if self._closed or self._cancel:
                self._cancel = False
                try:
                    channel.close()
                except Exception:
                    pass
                self._finish(request, None)
                return
            self._channel = channel
            self._screen = screen
            self._live_request = request
            pending = "".join(self._pending_input)
            self._pending_input.clear()
        if pending:
            try:
                channel.send(pending)
            except Exception:
                pass

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        watch = (
            shell_takeover.PromptWatch()
            if shell_takeover.opens_shell(request.text)
            else None
        )
        if watch is not None:
            try:
                channel.settimeout(0.3)
            except Exception:
                pass
        try:
            while True:
                try:
                    data = channel.recv(8192)
                except (socket.timeout, TimeoutError):
                    if watch is not None and self._prompt_ready(watch, screen):
                        self._handoff_shell(
                            request, channel, screen, broker=False
                        )
                        return
                    continue
                except Exception:
                    break
                if not data:
                    break
                if watch is not None:
                    watch.mark()
                text = decoder.decode(data)
                if text:
                    with self._io_lock:
                        screen.feed(text)
                        fed_cols, fed_rows = screen.columns, screen.lines
                        fed_alt = screen.in_alternate_screen
                    self._emit_chunk(
                        request,
                        domain.StreamType.STDOUT,
                        text,
                        persist=False,
                        columns=fed_cols,
                        rows=fed_rows,
                        alternate=fed_alt,
                    )
            tail = decoder.decode(b"", final=True)
            if tail:
                with self._io_lock:
                    screen.feed(tail)
                    fed_cols, fed_rows = screen.columns, screen.lines
                    fed_alt = screen.in_alternate_screen
                self._emit_chunk(
                    request,
                    domain.StreamType.STDOUT,
                    tail,
                    persist=False,
                    columns=fed_cols,
                    rows=fed_rows,
                    alternate=fed_alt,
                )
        finally:
            with self._lock:
                if self._channel is channel:
                    self._channel = None
                self._screen = None
                self._live_request = None

        try:
            exit_code: Optional[int] = channel.recv_exit_status()
        except Exception:
            exit_code = None

        new_cwd = self._read_remote_cwd(client, cwd_file)
        if new_cwd:
            self._cwd = new_cwd

        with self._lock:
            alive = (
                not self._closed
                and self._client is not None
                and self._client.get_transport() is not None
                and self._client.get_transport().is_active()
            )
        if not alive:
            self._emit_error(
                request, "SSH-соединение разорвано удалённой стороной"
            )
            self._emit(domain.OutputEvent(
                session_id=request.session_id,
                event_type=domain.EventType.SESSION_STATUS,
                timestamp=datetime.now(),
                stream_type=domain.StreamType.SYSTEM,
                status=domain.SessionStatus.ERROR,
                payload="disconnected",
            ))

        snapshot = screen.primary_text()
        if snapshot:
            self._emit_chunk(
                request, domain.StreamType.STDOUT, snapshot, persist=True
            )
        self._finish(request, exit_code)

    def _run_sudo_command(
        self,
        request: domain.CommandRequest,
        client: paramiko.SSHClient,
        cols: int,
        rows: int,
    ) -> bool:
        """Команда с sudo идёт на один и тот же PTY, пока живо SSH-соединение.

        Пароль читает сам sudo. Билет хранит удалённая ОС, не клиент.
        False — не удалось открыть этот PTY, команду можно выполнить как обычно.
        """
        tag = uuid.uuid4().hex
        cwd_file = f"/tmp/vibe-cwd-{tag[:10]}"
        script_path = f"/tmp/vibe-run-{tag}.sh"
        script = self._build_remote_script(request.text, cwd_file)
        try:
            self._upload_text(client, script_path, script)
            channel = self._ensure_sudo_broker(client, cols, rows)
        except Exception:
            self._remove_remote(client, script_path)
            return False

        screen = ScreenLog(cols, rows)
        fence = SudoFence(tag)
        with self._lock:
            if self._closed or self._cancel:
                self._cancel = False
                self._remove_remote(client, script_path)
                self._finish(request, None)
                return True
            self._channel = channel
            self._screen = screen
            self._live_request = request
            pending = "".join(self._pending_input)
            self._pending_input.clear()
        try:
            channel.send(f"__VIBE_RUN__{tag}__{script_path}\n")
        except Exception as exc:
            self._drop_sudo_broker()
            self._remove_remote(client, script_path)
            self._emit_error(request, f"Не удалось выполнить команду: {exc}")
            self._finish(request, None)
            return True
        if pending:
            try:
                channel.send(pending)
            except Exception:
                pass

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            channel.settimeout(0.3)
        except Exception:
            pass
        cancelled_at: Optional[float] = None
        watch = (
            shell_takeover.PromptWatch()
            if shell_takeover.opens_shell(request.text)
            else None
        )
        handed = False
        try:
            while fence.exit_code is None:
                with self._lock:
                    cancelled = self._cancel
                if cancelled and cancelled_at is None:
                    cancelled_at = time.monotonic()
                try:
                    data = channel.recv(8192)
                except (socket.timeout, TimeoutError):
                    if watch is not None and self._prompt_ready(watch, screen):
                        handed = True
                        self._handoff_shell(
                            request, channel, screen, broker=True, outer_tag=tag
                        )
                        return True
                    if (
                        cancelled_at is not None
                        and time.monotonic() - cancelled_at > 2
                    ):
                        self._drop_sudo_broker()
                        break
                    continue
                except Exception:
                    self._drop_sudo_broker()
                    break
                if not data:
                    self._drop_sudo_broker()
                    break
                if watch is not None:
                    watch.mark()
                visible = fence.feed(decoder.decode(data))
                if visible:
                    self._show_sudo_output(request, screen, visible)
        finally:
            if not handed and fence.exit_code is None:
                self._show_sudo_output(request, screen, fence.flush())
            with self._lock:
                if self._channel is channel:
                    self._channel = None
                self._screen = None
                self._live_request = None

        new_cwd = self._read_remote_cwd(client, cwd_file)
        if new_cwd:
            self._cwd = new_cwd
        snapshot = screen.primary_text()
        if snapshot:
            self._emit_chunk(
                request, domain.StreamType.STDOUT, snapshot, persist=True
            )
        self._finish(request, fence.exit_code)
        return True

    def _prompt_ready(self, watch: shell_takeover.PromptWatch, screen: ScreenLog) -> bool:
        with self._io_lock:
            alternate = screen.in_alternate_screen
            line = shell_takeover.last_text_line(screen.primary_text())
        return watch.ready(line, alternate)

    def _handoff_shell(
        self,
        request: domain.CommandRequest,
        channel: paramiko.Channel,
        screen: ScreenLog,
        *,
        broker: bool,
        outer_tag: str = "",
    ) -> None:
        with self._io_lock:
            line = shell_takeover.last_text_line(screen.primary_text())
            snapshot = screen.primary_text()
        self._silence_echo(channel, screen.columns, screen.lines)
        with self._lock:
            self._shell_labels.append(shell_takeover.prompt_label(line))
            self._attached_channel = channel
            self._attached_is_broker = broker
            if outer_tag:
                self._outer_tag = outer_tag
        self._emit_shell_context()
        if snapshot:
            self._emit_chunk(
                request, domain.StreamType.STDOUT, snapshot, persist=True
            )
        self._finish(request, 0)

    def _silence_echo(
        self, channel: paramiko.Channel, cols: int, rows: int
    ) -> None:
        try:
            channel.send(shell_takeover.silence_command())
        except Exception:
            return
        try:
            channel.settimeout(0.3)
        except Exception:
            pass
        watch = shell_takeover.PromptWatch(idle_s=0.3)
        screen = ScreenLog(cols, rows)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                data = channel.recv(8192)
            except (socket.timeout, TimeoutError):
                if self._prompt_ready(watch, screen):
                    return
                continue
            except Exception:
                return
            if not data:
                return
            watch.mark()
            screen.feed(decoder.decode(data))

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
            had = bool(self._shell_labels) or self._attached_channel is not None
            self._shell_labels.clear()
            self._attached_channel = None
            self._attached_is_broker = False
            self._outer_tag = ""
        if had:
            self._emit_shell_context()

    def _pop_shell(self) -> None:
        with self._lock:
            if self._shell_labels:
                self._shell_labels.pop()
            if not self._shell_labels:
                self._attached_channel = None
                self._attached_is_broker = False
                self._outer_tag = ""
        self._emit_shell_context()

    def _run_attached(self, request: domain.CommandRequest) -> None:
        with self._lock:
            channel = self._attached_channel
            cols, rows = self._cols, self._rows
            outer_tag = self._outer_tag
            broker = self._attached_is_broker
        if channel is None or channel.closed:
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
            self._channel = channel
            self._screen = screen
            self._live_request = request
            pending = "".join(self._pending_input)
            self._pending_input.clear()
        try:
            channel.settimeout(0.3)
        except Exception:
            pass
        try:
            if shell_takeover.is_exit_command(request.text):
                channel.send("exit\n")
                self._read_shell_transition(request, channel, screen, mode="exit")
                return
            if shell_takeover.opens_shell(request.text):
                line = request.text.replace("\r\n", "\n").replace("\r", "\n")
                channel.send(line.rstrip("\n") + "\n")
                if pending:
                    channel.send(pending)
                self._read_shell_transition(
                    request, channel, screen, mode="enter", broker=broker
                )
                return
            tag = uuid.uuid4().hex
            channel.send(shell_takeover.wrap_command(request.text, tag))
            if pending:
                channel.send(pending)
            self._read_marked_command(request, channel, screen, tag, outer_tag)
        except Exception as exc:
            self._emit_error(request, f"Не удалось выполнить команду: {exc}")
            self._finish(request, None)
        finally:
            with self._lock:
                if self._channel is channel:
                    self._channel = None
                if self._screen is screen:
                    self._screen = None
                if self._live_request is request:
                    self._live_request = None

    def _read_shell_transition(
        self,
        request: domain.CommandRequest,
        channel: paramiko.Channel,
        screen: ScreenLog,
        *,
        mode: str,
        broker: bool = False,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        watch = shell_takeover.PromptWatch()
        outer = SudoFence(self._outer_tag) if self._outer_tag else None
        while True:
            try:
                data = channel.recv(8192)
            except (socket.timeout, TimeoutError):
                if self._prompt_ready(watch, screen):
                    if mode == "exit":
                        self._pop_shell()
                        self._finish_screen(request, screen, 0)
                    else:
                        self._handoff_shell(
                            request, channel, screen,
                            broker=broker, outer_tag=self._outer_tag,
                        )
                    return
                continue
            except Exception:
                self._clear_shell()
                self._finish_screen(request, screen, None)
                return
            if not data:
                self._clear_shell()
                self._finish_screen(request, screen, None)
                return
            watch.mark()
            text = decoder.decode(data)
            visible = outer.feed(text) if outer is not None else text
            if outer is not None and outer.exit_code is not None:
                if visible:
                    self._show_sudo_output(request, screen, visible)
                self._clear_shell()
                self._finish_screen(request, screen, outer.exit_code)
                return
            if visible:
                self._show_sudo_output(request, screen, visible)

    def _read_marked_command(
        self,
        request: domain.CommandRequest,
        channel: paramiko.Channel,
        screen: ScreenLog,
        tag: str,
        outer_tag: str,
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        fence = SudoFence(tag)
        outer = SudoFence(outer_tag) if outer_tag else None
        cancelled_at: Optional[float] = None
        while fence.exit_code is None:
            with self._lock:
                cancelled = self._cancel
            if cancelled and cancelled_at is None:
                cancelled_at = time.monotonic()
            try:
                data = channel.recv(8192)
            except (socket.timeout, TimeoutError):
                if (
                    cancelled_at is not None
                    and time.monotonic() - cancelled_at > 2
                ):
                    break
                continue
            except Exception:
                self._clear_shell()
                break
            if not data:
                self._clear_shell()
                break
            text = decoder.decode(data)
            visible = outer.feed(text) if outer is not None else text
            if outer is not None and outer.exit_code is not None:
                if visible:
                    self._show_sudo_output(request, screen, visible)
                self._clear_shell()
                self._finish_screen(request, screen, outer.exit_code)
                return
            shown = fence.feed(visible)
            if shown:
                self._show_sudo_output(request, screen, shown)
        if fence.exit_code is None:
            self._show_sudo_output(request, screen, fence.flush())
        self._finish_screen(request, screen, fence.exit_code)

    def _finish_screen(
        self,
        request: domain.CommandRequest,
        screen: ScreenLog,
        return_code: Optional[int],
    ) -> None:
        snapshot = screen.primary_text()
        if snapshot:
            self._emit_chunk(
                request, domain.StreamType.STDOUT, snapshot, persist=True
            )
        self._finish(request, return_code)

    def _show_sudo_output(
        self,
        request: domain.CommandRequest,
        screen: ScreenLog,
        text: str,
    ) -> None:
        if not text:
            return
        with self._io_lock:
            screen.feed(text)
            fed_cols, fed_rows = screen.columns, screen.lines
            fed_alt = screen.in_alternate_screen
        self._emit_chunk(
            request,
            domain.StreamType.STDOUT,
            text,
            persist=False,
            columns=fed_cols,
            rows=fed_rows,
            alternate=fed_alt,
        )

    def _ensure_sudo_broker(
        self,
        client: paramiko.SSHClient,
        cols: int,
        rows: int,
    ) -> paramiko.Channel:
        with self._lock:
            current = self._sudo_channel
        if current is not None and not current.closed:
            try:
                current.resize_pty(width=cols, height=rows)
            except Exception:
                pass
            return current
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH-соединение не установлено")
        channel = transport.open_session(timeout=15)
        channel.get_pty(term="xterm-256color", width=cols, height=rows)
        channel.exec_command(
            "bash --noprofile --norc -c " + shlex.quote(_SUDO_BROKER)
        )
        channel.settimeout(10)
        ready = ""
        while "__VIBE_READY__" not in ready:
            data = channel.recv(256)
            if not data:
                raise RuntimeError("Не удалось открыть сессию sudo")
            ready += data.decode("utf-8", errors="replace")
            if len(ready) > 8192:
                raise RuntimeError("Не удалось открыть сессию sudo")
        with self._lock:
            self._sudo_channel = channel
        return channel

    def _drop_sudo_broker(self) -> None:
        with self._lock:
            channel = self._sudo_channel
            self._sudo_channel = None
            if self._channel is channel:
                self._channel = None
        if channel is None:
            return
        try:
            channel.close()
        except Exception:
            pass

    def _upload_text(
        self,
        client: paramiko.SSHClient,
        path: str,
        text: str,
    ) -> None:
        sftp = client.open_sftp()
        try:
            with sftp.file(path, "wb") as handle:
                handle.write(text.encode("utf-8"))
            try:
                sftp.chmod(path, 0o700)
            except Exception:
                pass
        finally:
            sftp.close()

    def _remove_remote(self, client: paramiko.SSHClient, path: str) -> None:
        try:
            sftp = client.open_sftp()
        except Exception:
            return
        try:
            sftp.remove(path)
        except Exception:
            pass
        finally:
            sftp.close()

    def _read_remote_cwd(
        self, client: paramiko.SSHClient, path: str
    ) -> Optional[str]:
        quoted = shlex.quote(path)
        try:
            _stdin, stdout, _stderr = client.exec_command(
                f"cat -- {quoted}; rm -f -- {quoted}",
                timeout=8,
            )
            data = stdout.read(8192)
        except Exception:
            return None
        text = data.decode("utf-8", errors="replace").strip()
        return text or None

    # --- эмиссия ----------------------------------------------------------

    def _emit_size(self, cols: int, rows: int) -> None:
        with self._lock:
            request = self._live_request
        if request is None:
            return
        with self._io_lock:
            screen = self._screen
            alternate = bool(screen is not None and screen.in_alternate_screen)
        self._emit_chunk(
            request,
            domain.StreamType.STDOUT,
            "",
            persist=False,
            columns=cols,
            rows=rows,
            alternate=alternate,
        )

    def _emit_chunk(
        self,
        request: domain.CommandRequest,
        stream_type: domain.StreamType,
        text: str,
        persist: bool = True,
        columns: Optional[int] = None,
        rows: Optional[int] = None,
        alternate: bool = False,
    ) -> None:
        self._emit(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.OUTPUT_CHUNK,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=stream_type,
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
            self._channel = None
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