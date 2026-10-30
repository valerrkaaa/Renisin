# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""IP и порты локального MCP. Файл появляется после первого сохранения в настройках."""
from __future__ import annotations

import json
import os
import threading

from mcp_host import DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS, parse_mcp_host, parse_mcp_ports


class FileMcpSettings:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> tuple:
        """Адрес и текст портов. Повреждённый файл заменяется значениями по умолчанию."""
        with self._lock:
            if not os.path.exists(self._path):
                return DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS
            try:
                with open(self._path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except (OSError, json.JSONDecodeError):
                return DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS
        if not isinstance(data, dict):
            return DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS
        host = str(data.get("host") or "").strip()
        ports = str(data.get("ports") or "").strip()
        try:
            parse_mcp_host(host)
            parse_mcp_ports(ports)
        except ValueError:
            return DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS
        return host, ports

    def save(self, host: str, ports: str) -> None:
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        payload = json.dumps(
            {"host": host, "ports": ports},
            ensure_ascii=False,
            indent=2,
        )
        temporary = self._path + ".tmp"
        with self._lock:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
