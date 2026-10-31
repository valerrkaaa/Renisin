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
        if not isinstance(data, dict):
            return {}
        if _migrate_connections(data):
            self._write(data)
        return data

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

    def _load_connections(self) -> Dict[str, dict]:
        raw = self._read().get("connections")
        if not isinstance(raw, dict):
            return {}
        return raw

    def _save(self, sessions: Dict[str, dict]) -> None:
        data = self._read()
        data["sessions"] = sessions
        self._write(data)

    def _save_connections(self, connections: Dict[str, dict]) -> None:
        data = self._read()
        data["connections"] = connections
        self._write(data)

    @staticmethod
    def _hop_to_dict(hop: domain.SSHHop) -> dict:
        payload = {
            "host": hop.host,
            "port": int(hop.port),
            "username": hop.username,
        }
        if hop.auth_method != domain.SSHAuthMethod.PASSWORD:
            payload["auth_method"] = hop.auth_method.value
        if hop.private_key_path:
            payload["private_key_path"] = hop.private_key_path
        return payload

    @staticmethod
    def _hop_from_dict(raw: dict) -> domain.SSHHop:
        method = str(raw.get("auth_method") or "password")
        try:
            auth = domain.SSHAuthMethod(method)
        except ValueError:
            auth = domain.SSHAuthMethod.PASSWORD
        key = str(raw.get("private_key_path") or "").strip() or None
        return domain.SSHHop(
            host=str(raw.get("host") or ""),
            port=int(raw.get("port", 22)),
            username=str(raw.get("username") or ""),
            auth_method=auth,
            private_key_path=key,
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
    def _connection_to_dict(cls, info: domain.ConnectionInfo) -> dict:
        payload = {
            "connection_id": info.connection_id,
            "name": info.name,
            "provider": info.provider.value,
            "identity": info.identity.value,
            "mcp_alias": info.mcp_alias,
            "mcp_description": info.mcp_description,
            "os_name": info.os_name,
            "ip_addresses": info.ip_addresses,
            "username": info.username,
            "shell_family": domain.normalize_shell_family(info.shell_family),
            "expanded": bool(info.expanded),
        }
        if info.ssh is not None:
            payload["ssh"] = cls._ssh_to_dict(info.ssh)
        return payload

    @classmethod
    def _connection_from_dict(cls, raw: dict) -> domain.ConnectionInfo:
        ssh = None
        raw_ssh = raw.get("ssh")
        if isinstance(raw_ssh, dict):
            ssh = cls._ssh_from_dict(raw_ssh)
        provider = str(raw.get("provider") or domain.ProviderType.CMD.value)
        try:
            kind = domain.ProviderType(provider)
        except ValueError:
            kind = domain.ProviderType.CMD
        identity = str(raw.get("identity") or domain.ExecutionIdentityType.CURRENT_USER.value)
        try:
            who = domain.ExecutionIdentityType(identity)
        except ValueError:
            who = domain.ExecutionIdentityType.CURRENT_USER
        return domain.ConnectionInfo(
            connection_id=str(raw.get("connection_id") or ""),
            name=str(raw.get("name") or ""),
            provider=kind,
            identity=who,
            ssh=ssh,
            mcp_alias=str(raw.get("mcp_alias") or ""),
            mcp_description=str(raw.get("mcp_description") or ""),
            os_name=str(raw.get("os_name") or ""),
            ip_addresses=str(raw.get("ip_addresses") or ""),
            username=str(raw.get("username") or ""),
            shell_family=domain.normalize_shell_family(str(raw.get("shell_family") or "")),
            expanded=bool(raw.get("expanded", False)),
        )

    @classmethod
    def _session_to_dict(cls, info: domain.SessionInfo) -> dict:
        payload = {
            "session_id": info.session_id,
            "name": info.name,
            "connection_id": info.connection_id,
            "status": info.status.value,
        }
        if info.llm_profile_id:
            payload["llm_profile_id"] = info.llm_profile_id
        return payload

    def _blend(self, raw: dict, connections: Dict[str, dict]) -> domain.SessionInfo:
        cid = str(raw.get("connection_id") or "")
        conn_raw = connections.get(cid) if cid else None
        conn = self._connection_from_dict(conn_raw) if isinstance(conn_raw, dict) else None
        status = str(raw.get("status") or domain.SessionStatus.CLOSED.value)
        try:
            state = domain.SessionStatus(status)
        except ValueError:
            state = domain.SessionStatus.CLOSED
        info = domain.SessionInfo(
            session_id=str(raw.get("session_id") or ""),
            name=str(raw.get("name") or ""),
            provider=conn.provider if conn is not None else domain.ProviderType.CMD,
            identity=(
                conn.identity
                if conn is not None
                else domain.ExecutionIdentityType.CURRENT_USER
            ),
            status=state,
            ssh=conn.ssh if conn is not None else None,
            llm_profile_id=raw.get("llm_profile_id") or None,
            mcp_alias=conn.mcp_alias if conn is not None else "",
            mcp_description=conn.mcp_description if conn is not None else "",
            os_name=conn.os_name if conn is not None else "",
            ip_addresses=conn.ip_addresses if conn is not None else "",
            username=conn.username if conn is not None else "",
            shell_family=conn.shell_family if conn is not None else domain.SHELL_UNIX,
            connection_id=cid,
        )
        return info

    # --- SessionCatalog ---------------------------------------------------

    def save(self, info: domain.SessionInfo) -> None:
        with self._lock:
            sessions = self._load()
            connections = self._load_connections()
            cid = info.connection_id.strip()
            if not cid:
                cid = domain.connection_id_for_session(info.session_id)
                info = domain.SessionInfo(
                    session_id=info.session_id,
                    name=info.name,
                    provider=info.provider,
                    identity=info.identity,
                    status=info.status,
                    ssh=info.ssh,
                    llm_profile_id=info.llm_profile_id,
                    mcp_alias=info.mcp_alias,
                    mcp_description=info.mcp_description,
                    os_name=info.os_name,
                    ip_addresses=info.ip_addresses,
                    username=info.username,
                    shell_family=info.shell_family,
                    connection_id=cid,
                )
            current = connections.get(cid)
            if isinstance(current, dict):
                kept = self._connection_from_dict(current)
                name = kept.name
                expanded = kept.expanded
            else:
                name = info.name
                expanded = False
            connections[cid] = self._connection_to_dict(
                domain.ConnectionInfo(
                    connection_id=cid,
                    name=name,
                    provider=info.provider,
                    identity=info.identity,
                    ssh=info.ssh,
                    mcp_alias=info.mcp_alias,
                    mcp_description=info.mcp_description,
                    os_name=info.os_name,
                    ip_addresses=info.ip_addresses,
                    username=info.username,
                    shell_family=info.shell_family,
                    expanded=expanded,
                )
            )
            sessions[info.session_id] = self._session_to_dict(info)
            data = self._read()
            data["sessions"] = sessions
            data["connections"] = connections
            self._write(data)

    def list(self) -> List[domain.SessionInfo]:
        with self._lock:
            sessions = self._load()
            connections = self._load_connections()
            result = [self._blend(d, connections) for d in sessions.values()]
        result.sort(key=lambda s: s.name.lower())
        return result

    def get(self, session_id: domain.SessionId) -> Optional[domain.SessionInfo]:
        with self._lock:
            sessions = self._load()
            raw = sessions.get(session_id)
            if not isinstance(raw, dict):
                return None
            return self._blend(raw, self._load_connections())

    def list_connections(self) -> List[domain.ConnectionInfo]:
        with self._lock:
            raw = self._load_connections()
            result = [
                self._connection_from_dict(item)
                for item in raw.values()
                if isinstance(item, dict) and str(item.get("connection_id") or "").strip()
            ]
        result.sort(key=lambda item: item.name.lower())
        return result

    def get_connection(self, connection_id: str) -> Optional[domain.ConnectionInfo]:
        with self._lock:
            raw = self._load_connections().get(connection_id)
            if not isinstance(raw, dict):
                return None
            return self._connection_from_dict(raw)

    def save_connection(self, info: domain.ConnectionInfo) -> None:
        with self._lock:
            connections = self._load_connections()
            connections[info.connection_id] = self._connection_to_dict(info)
            self._save_connections(connections)

    def delete_connection(self, connection_id: str) -> None:
        with self._lock:
            connections = self._load_connections()
            if connection_id not in connections:
                return
            del connections[connection_id]
            self._save_connections(connections)

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


def _migrate_connections(data: dict) -> bool:
    """Старая сессия была и подключением, и журналом. Делим запись один раз."""
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return False
    connections = data.get("connections")
    if not isinstance(connections, dict):
        connections = {}
    layout = data.get("layout") if isinstance(data.get("layout"), dict) else None
    order = None
    if layout is not None and isinstance(layout.get("order"), dict):
        order = layout["order"]
    changed = False
    for session_id, raw in list(sessions.items()):
        if not isinstance(raw, dict) or raw.get("connection_id"):
            continue
        cid = domain.connection_id_for_session(str(session_id))
        if cid not in connections:
            connections[cid] = {
                "connection_id": cid,
                "name": raw.get("name") or session_id,
                "provider": raw.get("provider") or domain.ProviderType.CMD.value,
                "identity": raw.get("identity") or domain.ExecutionIdentityType.CURRENT_USER.value,
                "mcp_alias": raw.get("mcp_alias") or "",
                "mcp_description": raw.get("mcp_description") or "",
                "os_name": raw.get("os_name") or "",
                "ip_addresses": raw.get("ip_addresses") or "",
                "username": raw.get("username") or "",
                "shell_family": raw.get("shell_family") or domain.SHELL_UNIX,
                "expanded": False,
            }
            if raw.get("ssh"):
                connections[cid]["ssh"] = raw.get("ssh")
        slim = {
            "session_id": raw.get("session_id") or session_id,
            "name": raw.get("name") or session_id,
            "connection_id": cid,
            "status": raw.get("status") or domain.SessionStatus.CLOSED.value,
        }
        if raw.get("llm_profile_id"):
            slim["llm_profile_id"] = raw.get("llm_profile_id")
        sessions[session_id] = slim
        if order is not None:
            for parent, children in list(order.items()):
                if not isinstance(children, list) or session_id not in children:
                    continue
                order[parent] = [
                    cid if str(child) == str(session_id) else child for child in children
                ]
            order[cid] = [str(session_id)]
        changed = True
    if not changed:
        return False
    data["sessions"] = sessions
    data["connections"] = connections
    if layout is not None and order is not None:
        layout["order"] = order
        data["layout"] = layout
    return True


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