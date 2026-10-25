# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Каталог сессий: JSON-файл с несекретными метаданными."""
from __future__ import annotations

import json
import os
import threading
from typing import Dict, List, Optional

import domain


class FileSessionCatalog:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.RLock()

    # --- внутреннее -------------------------------------------------------

    def _read(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)

    def _load(self) -> Dict[str, dict]:
        sessions = self._read().get("sessions")
        if not isinstance(sessions, dict):
            return {}
        return sessions

    def _save(self, sessions: Dict[str, dict]) -> None:
        data = self._read()
        data["sessions"] = sessions
        self._write(data)

    @staticmethod
    def _hop_to_dict(hop: domain.SSHHop) -> dict:
        return {
            "host": hop.host,
            "port": int(hop.port),
            "username": hop.username,
        }

    @staticmethod
    def _hop_from_dict(raw: dict) -> domain.SSHHop:
        return domain.SSHHop(
            host=str(raw.get("host") or ""),
            port=int(raw.get("port", 22)),
            username=str(raw.get("username") or ""),
        )

    @classmethod
    def _ssh_to_dict(cls, ssh: domain.SSHConfig) -> dict:
        payload = {
            "host": ssh.host,
            "port": int(ssh.port),
            "username": ssh.username,
            "auth_method": ssh.auth_method.value,
            "private_key_path": ssh.private_key_path,
            "connect_timeout": float(ssh.connect_timeout),
        }
        if ssh.hops:
            payload["hops"] = [cls._hop_to_dict(hop) for hop in ssh.hops]
        return payload

    @classmethod
    def _ssh_from_dict(cls, raw: dict) -> domain.SSHConfig:
        hops_raw = raw.get("hops") or []
        hops = tuple(
            cls._hop_from_dict(item)
            for item in hops_raw
            if isinstance(item, dict)
        )
        return domain.SSHConfig(
            host=raw["host"],
            port=int(raw.get("port", 22)),
            username=raw.get("username", ""),
            auth_method=domain.SSHAuthMethod(
                raw.get("auth_method", "password")
            ),
            private_key_path=raw.get("private_key_path"),
            connect_timeout=float(raw.get("connect_timeout", 15.0)),
            hops=hops,
        )

    @classmethod
    def _to_dict(cls, info: domain.SessionInfo) -> dict:
        d = {
            "session_id": info.session_id,
            "name": info.name,
            "provider": info.provider.value,
            "identity": info.identity.value,
            "status": info.status.value,
        }
        if info.ssh is not None:
            d["ssh"] = cls._ssh_to_dict(info.ssh)
        if info.llm_profile_id:
            d["llm_profile_id"] = info.llm_profile_id
        if info.mcp_alias:
            d["mcp_alias"] = info.mcp_alias
        if info.mcp_description:
            d["mcp_description"] = info.mcp_description
        if info.os_name:
            d["os_name"] = info.os_name
        if info.ip_addresses:
            d["ip_addresses"] = info.ip_addresses
        if info.username:
            d["username"] = info.username
        return d

    @classmethod
    def _from_dict(cls, d: dict) -> domain.SessionInfo:
        ssh = None
        raw_ssh = d.get("ssh")
        if raw_ssh:
            ssh = cls._ssh_from_dict(raw_ssh)
        return domain.SessionInfo(
            session_id=d["session_id"],
            name=d["name"],
            provider=domain.ProviderType(d["provider"]),
            identity=domain.ExecutionIdentityType(d["identity"]),
            status=domain.SessionStatus(d["status"]),
            ssh=ssh,
            llm_profile_id=d.get("llm_profile_id") or None,
            mcp_alias=str(d.get("mcp_alias") or ""),
            mcp_description=str(d.get("mcp_description") or ""),
            os_name=str(d.get("os_name") or ""),
            ip_addresses=str(d.get("ip_addresses") or ""),
            username=str(d.get("username") or ""),
        )

    # --- SessionCatalog ---------------------------------------------------

    def save(self, info: domain.SessionInfo) -> None:
        with self._lock:
            sessions = self._load()
            sessions[info.session_id] = self._to_dict(info)
            self._save(sessions)

    def list(self) -> List[domain.SessionInfo]:
        with self._lock:
            sessions = self._load()
            result = [self._from_dict(d) for d in sessions.values()]
        result.sort(key=lambda s: s.name.lower())
        return result

    def get(self, session_id: domain.SessionId) -> Optional[domain.SessionInfo]:
        with self._lock:
            sessions = self._load()
            d = sessions.get(session_id)
            return self._from_dict(d) if d else None

    def delete(self, session_id: domain.SessionId) -> None:
        with self._lock:
            sessions = self._load()
            if session_id in sessions:
                del sessions[session_id]
                self._save(sessions)

    def load_layout(self) -> domain.ConnectionLayout:
        with self._lock:
            raw = self._read().get("layout")
        return _layout_from_dict(raw if isinstance(raw, dict) else {})

    def save_layout(self, layout: domain.ConnectionLayout) -> None:
        with self._lock:
            data = self._read()
            data["layout"] = _layout_to_dict(layout)
            self._write(data)


def _layout_to_dict(layout: domain.ConnectionLayout) -> dict:
    groups = {
        group.group_id: {
            "name": group.name,
            "parent": group.parent_id,
            "expanded": bool(group.expanded),
        }
        for group in layout.groups
    }
    order = {parent: list(children) for parent, children in layout.order}
    return {"groups": groups, "order": order}


def _layout_from_dict(raw: dict) -> domain.ConnectionLayout:
    groups_raw = raw.get("groups")
    order_raw = raw.get("order")
    groups = []
    if isinstance(groups_raw, dict):
        for group_id, item in groups_raw.items():
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not str(group_id).strip() or not name:
                continue
            groups.append(
                domain.ConnectionGroup(
                    group_id=str(group_id),
                    name=name,
                    parent_id=str(item.get("parent") or ""),
                    expanded=bool(item.get("expanded")),
                )
            )
    order = []
    if isinstance(order_raw, dict):
        for parent, children in order_raw.items():
            if not isinstance(children, list):
                continue
            order.append(
                (
                    str(parent),
                    tuple(str(child) for child in children if str(child).strip()),
                )
            )
    return domain.ConnectionLayout(groups=tuple(groups), order=tuple(order))