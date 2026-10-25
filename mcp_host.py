# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Локальный MCP-сервер: инструмент run_command для активной сессии терминала."""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from application.terminal_mcp import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    input_schema,
    os_command_note,
)


_HOST = "127.0.0.1"
_PORT_FIRST = 47321
_PORT_LAST = 47340


class TerminalMcpServer:
    def __init__(self, service) -> None:
        self._service = service
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.endpoint = ""

    def start(self) -> str:
        if self._httpd is not None:
            return self.endpoint
        handler = _handler_class(self._service)
        last: Optional[OSError] = None
        httpd: Optional[_ExclusiveServer] = None
        for port in range(_PORT_FIRST, _PORT_LAST + 1):
            try:
                httpd = _ExclusiveServer((_HOST, port), handler)
            except OSError as exc:
                last = exc
                continue
            break
        if httpd is None:
            raise OSError(last or "Не удалось занять порт MCP")
        httpd.daemon_threads = True
        self._httpd = httpd
        self.endpoint = f"http://{_HOST}:{httpd.server_address[1]}/mcp"
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
