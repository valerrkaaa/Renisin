# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Локальный MCP-сервер: инструмент run_command для активной сессии терминала."""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Sequence

from application.terminal_mcp import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    input_schema,
    os_command_note,
)


DEFAULT_MCP_HOST = "127.0.0.1"
DEFAULT_MCP_PORTS = "47321-47340"
_MAX_MCP_PORTS = 64


def parse_mcp_host(text: str) -> str:
    """IP, на котором слушает MCP. Пустая строка и имена хостов не подходят."""
    host = text.strip()
    if not host or any(char.isspace() for char in host) or "/" in host:
        raise ValueError("Укажите IP-адрес")
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("IP-адрес указан неверно") from exc
    return host


def parse_mcp_ports(text: str) -> tuple:
    """Один порт, список через запятую или диапазон: 47321-47340."""
    raw = text.strip()
    if not raw:
        raise ValueError("Укажите порт или диапазон портов")
    found = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            bounds = piece.split("-")
            if len(bounds) != 2 or not bounds[0].strip() or not bounds[1].strip():
                raise ValueError(f"Непонятный диапазон: {piece}")
            start = _port_number(bounds[0])
            end = _port_number(bounds[1])
            if end < start:
                raise ValueError(f"Диапазон идёт в обратную сторону: {piece}")
            found.extend(range(start, end + 1))
            continue
        found.append(_port_number(piece))
    unique = []
    seen = set()
    for port in found:
        if port in seen:
            continue
        seen.add(port)
        unique.append(port)
    if not unique:
        raise ValueError("Укажите порт или диапазон портов")
    if len(unique) > _MAX_MCP_PORTS:
        raise ValueError(f"Слишком много портов: не больше {_MAX_MCP_PORTS}")
    return tuple(unique)


def _port_number(text: str) -> int:
    piece = text.strip()
    try:
        value = int(piece)
    except ValueError as exc:
        raise ValueError(f"Порт должен быть числом: {piece}") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"Порт вне диапазона 1–65535: {value}")
    return value


class TerminalMcpServer:
    def __init__(self, service, host: str = DEFAULT_MCP_HOST, ports: Optional[Sequence[int]] = None) -> None:
        self._service = service
        self._host = host or DEFAULT_MCP_HOST
        self._ports = tuple(ports) if ports else parse_mcp_ports(DEFAULT_MCP_PORTS)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.endpoint = ""

    def listen(self) -> tuple:
        """Адрес и порты, которые сервер пытается занять. Это не число сессий."""
        return self._host, self._ports

    def running(self) -> bool:
        return self._httpd is not None

    def rebind(self, host: str, ports: Sequence[int]) -> str:
        """Останавливает текущий сокет и занимает новый адрес. Первый свободный порт из списка."""
        self.stop()
        self._host = host
        self._ports = tuple(ports)
        return self.start()

    def start(self) -> str:
        if self._httpd is not None:
            return self.endpoint
        self.endpoint = ""
        handler = _handler_class(self._service)
        last: Optional[OSError] = None
        httpd: Optional[_ExclusiveServer] = None
        for port in self._ports:
            try:
                httpd = _ExclusiveServer((self._host, port), handler)
            except OSError as exc:
                last = exc
                continue
            break
        if httpd is None:
            raise OSError(last or "Не удалось занять порт MCP")
        httpd.daemon_threads = True
        self._httpd = httpd
        shown = f"[{self._host}]" if ":" in self._host else self._host
        self.endpoint = f"http://{shown}:{httpd.server_address[1]}/mcp"
        thread = threading.Thread(target=httpd.serve_forever, name="mcp", daemon=True)
        self._thread = thread
        thread.start()
        return self.endpoint

    def stop(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None


class _ExclusiveServer(ThreadingHTTPServer):
    """Не делит порт с уже запущенным экземпляром. Иначе запрос уходит в чужой процесс."""

    def server_bind(self) -> None:
        # HTTPServer включает SO_REUSEADDR. На Windows из-за этого второй
        # процесс садится на тот же порт, и вызов команды уходит в чужое окно.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()


def _handler_class(service):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802
            if _session_from_path(self.path) == "":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw.decode("utf-8") or "null")
            except json.JSONDecodeError:
                self._send(400, _error(None, -32700, "Parse error"))
                return
            if not isinstance(message, dict):
                self._send(400, _error(None, -32600, "Invalid Request"))
                return
            method = str(message.get("method") or "")
            has_id = "id" in message
            if method.startswith("notifications/") or not has_id:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            result, error = _dispatch(
                service, method, message.get("params"), _session_from_path(self.path)
            )
            if error is not None:
                self._send(200, _error(message.get("id"), error[0], error[1]))
                return
            self._send(200, {"jsonrpc": "2.0", "id": message.get("id"), "result": result})

        def do_GET(self) -> None:  # noqa: N802
            self._send(405, _error(None, -32601, "Используйте POST /mcp"))

        def log_message(self, fmt: str, *args) -> None:
            return None

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _session_from_path(path: str) -> Optional[str]:
    """`/mcp` — активная сессия окна, `/mcp/<id>` — терминал конкретного чата."""
    clean = path.split("?", 1)[0].rstrip("/")
    if clean in ("/mcp", ""):
        return None
    prefix = "/mcp/"
    if not clean.startswith(prefix):
        return ""
    session_id = clean[len(prefix):]
    if not session_id or "/" in session_id:
        return ""
    return session_id


def _dispatch(service, method: str, params, session_id: Optional[str]) -> tuple:
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        version = str(params.get("protocolVersion") or "2025-03-26")
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            version = "2025-03-26"
        name = "vterminal"
        if session_id:
            info = service.get_session(session_id) if hasattr(service, "get_session") else None
            title = info.name if info is not None else session_id
            name = f"vterminal: {title}"
        return (
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": name, "version": "1.0.0"},
            },
            None,
        )
    if method == "ping":
        return ({}, None)
    if method == "tools/list":
        return (
            {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": _tool_description(service, session_id),
                        "inputSchema": input_schema(),
                    }
                ]
            },
            None,
        )
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if name != TOOL_NAME or not isinstance(arguments, dict):
            return (
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "отказ, причина: нужны поля intent и command",
                        }
                    ],
                    "isError": False,
                },
                None,
            )
        intent = str(arguments.get("intent") or "")
        command = str(arguments.get("command") or "")
        if session_id:
            text = service.run_mcp_command(session_id, intent, command)
        else:
            text = service.run_foreground_mcp(intent, command)
        return ({"content": [{"type": "text", "text": text}], "isError": False}, None)
    return (None, (-32601, f"Method not found: {method}"))


def _tool_description(service, session_id: Optional[str]) -> str:
    bound = session_id or _foreground_id(service)
    os_name = _known_os(service, bound)
    if not bound:
        return TOOL_DESCRIPTION + os_command_note(os_name)
    info = service.get_session(bound) if hasattr(service, "get_session") else None
    title = info.name if info is not None else bound
    described = TOOL_DESCRIPTION.replace(
        "в терминале этой сессии",
        f"в терминале сессии «{title}»",
        1,
    )
    return described + os_command_note(os_name)


def _foreground_id(service) -> str:
    reader = getattr(service, "foreground_session_id", None)
    if not callable(reader):
        return ""
    return str(reader() or "")


def _known_os(service, session_id: str) -> str:
    if not session_id:
        return ""
    reader = getattr(service, "session_operating_system", None)
    if callable(reader):
        return str(reader(session_id) or "").strip()
    info = service.get_session(session_id) if hasattr(service, "get_session") else None
    if info is None:
        return ""
    return str(getattr(info, "os_name", "") or "").strip()


def _error(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
