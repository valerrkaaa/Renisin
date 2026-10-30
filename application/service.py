# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""ApplicationService — сценарии приложения."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable, Dict, List, Optional
from uuid import uuid4

import domain
import file_edit
from application.command_rules import command_spans
from application.master_password import MasterPassword
from application.terminal_mcp import (
    TOOL_NAME,
    INTERACTIVE_REASON,
    CommandAnswer,
    CommandAsk,
    finished_text,
    lounge_origin,
    lounge_tab_id,
    lounge_tools,
    normalize_command,
    openai_tools,
    command_text,
    split_shell_commands,
    promote_shell_actions,
    refusal,
    refusal_reason,
)
from application.ports import (
    DriverFactory,
    EventCallback,
    JournalRepository,
    LlmClient,
    LlmProfileStore,
    SessionCatalog,
    SessionDriver,
)


class NoActiveSessionError(Exception):
    """Операция требует открытой сессии."""


class SessionUnavailable(Exception):
    """Журнал открыт, но соединение с сессией установить не удалось."""

    def __init__(self, info: domain.SessionInfo, reason: str) -> None:
        self.info = info
        self.reason = reason
        super().__init__(reason)


class LlmUnavailable(domain.LlmError):
    """Модель не ответила или профиль исчез. Терминал остаётся открытым."""


_CHAT_SYSTEM_PROMPT = """Ты помощник в терминале этой сессии. На каждый ответ выбери один из двух режимов.

Показать. Команда или код нужны человеку как справка, запускать их должен он. Пиши обычный текст и оформляй код специальным блоком. В одном блоке ровно одна команда, без символа $ и без комментариев внутри. Такой блок не выполняется.
Показать файлы
```bash
ls -la
```
Поясняющий код на другом языке оформляй так же, со своим языком блока.

Сделать самому. Результат нужно получить из терминала. Не оформляй команду блоком. Вызывай инструмент run_command и продолжай, пока не достигнешь результата, которого просили. По одному вызову — одна команда. intent — зачем она нужна, command — её текст. Прочитай ответ инструмента и, если цели ещё нет, вызови следующую команду. Когда данных достаточно, ответь обычным текстом по этому выводу.
Нельзя вызывать интерактивные команды: nano, vim, vi, less, more, top, htop, man. MCP их не поддерживает.

Не смешивай режимы: показанная команда остаётся текстом, выполненная приходит только из run_command.

Замена текста в файле — блок edit, не команда и не run_command. OLD копируй из файла как есть, вместе с соседними строками, чтобы кусок был один на весь файл.
```edit
path: путь/файл
OLD:
точный старый кусок
NEW:
новый кусок
```
"""

_LOUNGE_SYSTEM_PROMPT = """Ты помощник в общем чате. К нему подключены отдельные терминалы, у каждого своё имя MCP.

На каждый ответ выбери один из двух режимов.

Показать. Команда нужна человеку как справка. Пиши обычный текст и оформляй её блоком. В одном блоке ровно одна команда, без символа $ и без комментариев внутри. Такой блок не выполняется.
```bash
ls -la
```

Сделать самому. Результат нужно получить из терминала. Вызывай run_command и продолжай, пока не достигнешь результата. По одному вызову — одна команда. intent — зачем она нужна, command — её текст, target — точное имя MCP из списка ниже. Прочитай ответ инструмента. Когда данных достаточно, ответь обычным текстом.
Нельзя вызывать интерактивные команды: nano, vim, vi, less, more, top, htop, man. MCP их не поддерживает.
Не смешивай режимы: показанная команда остаётся текстом, выполненная приходит только из run_command.

У каждого MCP своя операционная система. Она указана в списке ниже и в цитате, если пользователь вставил «MCP: имя». Команду пиши для ОС того MCP, куда она уйдёт. Не переноси синтаксис одной ОС на другую. Если у MCP указано «ОС не определена», не выбирай Windows или Linux наугад: сначала короткой командой узнай ОС в этом MCP или спроси.
"""


@dataclass(frozen=True)
class ChatContext:
    """Сколько переписки после последнего сброса уйдёт в модель вместе с инструкцией."""

    messages: int
    characters: int
    tokens: int


class ApplicationService:
    def __init__(
        self,
        driver_factory: DriverFactory,
        catalog: SessionCatalog,
        journal: JournalRepository,
        llm_profiles: Optional[LlmProfileStore] = None,
        llm_client: Optional[LlmClient] = None,
        command_allow=None,
        lounge_store=None,
        command_rules=None,
        mcp_settings=None,
        ssh_key_settings=None,
    ) -> None:
        self._driver_factory = driver_factory
        self._catalog = catalog
        self._llm_profiles = llm_profiles
        self._llm = llm_client
        self._journal = journal
        self._command_allow = command_allow
        self._command_rules = command_rules
        self._mcp_settings = mcp_settings
        self._ssh_key = ssh_key_settings
        self._master = MasterPassword()
        self._lounge_store = lounge_store
        self._lounge_tabs: List[domain.LoungeTab] = []
        self._lounge_command: Dict[str, domain.SessionId] = {}
        self._approver: Optional[Callable[[CommandAsk], CommandAnswer]] = None
        self._foreground: Optional[domain.SessionId] = None
        self._mcp_endpoint = ""
        self._mcp_server = None
        self._mcp_watch: Dict[domain.SessionId, _McpWatch] = {}
        self._chat_stops: Dict[domain.SessionId, domain.ChatStop] = {}
        self._mcp_gate = threading.Lock()

        self._drivers: Dict[domain.SessionId, SessionDriver] = {}
        self._sessions: Dict[domain.SessionId, domain.SessionInfo] = {}
        self._running: Dict[domain.SessionId, domain.CommandId] = {}
        self._states: Dict[domain.SessionId, domain.SessionStatus] = {}

        self._subscribers: List[EventCallback] = []
        self._lock = threading.RLock()
        self._sequences: Dict[domain.SessionId, int] = {}
        self._offline_reason: Dict[domain.SessionId, str] = {}
        self._llm_problem: Dict[domain.SessionId, str] = {}
        self._shell_labels: Dict[domain.SessionId, str] = {}
        self._os_probe: Dict[domain.SessionId, domain.CommandId] = {}
        self._os_step: Dict[domain.SessionId, int] = {}
        self._os_phase: Dict[domain.SessionId, str] = {}
        self._ip_plan: Dict[domain.SessionId, tuple] = {}
        self._os_live: Dict[domain.SessionId, str] = {}
        self._os_snapshot: Dict[domain.SessionId, str] = {}
        self._groups: Dict[str, domain.ConnectionGroup] = {}
        self._order: Dict[str, List[str]] = {"": []}
        self._load_connection_layout()
        self._load_lounge()

    # --- подписка ---------------------------------------------------------

    def subscribe(self, callback: EventCallback) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    # --- запросы ----------------------------------------------------------

    def available_providers(self) -> List[domain.ProviderType]:
        return [
            domain.ProviderType.CMD,
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ]

    def list_sessions(self) -> List[domain.SessionInfo]:
        return self._catalog.list()

    def connection_layout(self) -> domain.ConnectionLayout:
        groups = tuple(self._groups[key] for key in sorted(self._groups))
        order = tuple(
            (parent, tuple(children))
            for parent, children in self._order.items()
        )
        return domain.ConnectionLayout(groups=groups, order=order)

    def create_connection_group(self, name: str, parent_id: str = "") -> domain.ConnectionGroup:
        cleaned = " ".join(name.split())
        if not cleaned:
            raise ValueError("Имя группы не может быть пустым")
        parent = parent_id.strip()
        if parent and parent not in self._groups:
            raise ValueError("Группа не найдена")
        group = domain.ConnectionGroup(group_id=uuid4().hex, name=cleaned, parent_id=parent)
        self._groups[group.group_id] = group
        self._order.setdefault(parent, []).append(group.group_id)
        self._order.setdefault(group.group_id, [])
        self._store_connection_layout()
        return group

    def rename_connection_group(self, group_id: str, name: str) -> None:
        group = self._groups.get(group_id)
        if group is None:
            raise ValueError("Группа не найдена")
        cleaned = " ".join(name.split())
        if not cleaned:
            raise ValueError("Имя группы не может быть пустым")
        self._groups[group_id] = replace(group, name=cleaned)
        self._store_connection_layout()

    def delete_connection_group(self, group_id: str) -> None:
        """Удаляет папку. Её дети остаются и поднимаются на её место."""
        group = self._groups.get(group_id)
        if group is None:
            raise ValueError("Группа не найдена")
        children = list(self._order.get(group_id, []))
        for child in children:
            nested = self._groups.get(child)
            if nested is not None:
                self._groups[child] = replace(nested, parent_id=group.parent_id)
        parent_children = self._order.setdefault(group.parent_id, [])
        if group_id in parent_children:
            index = parent_children.index(group_id)
            parent_children[index:index + 1] = children
        else:
            parent_children.extend(children)
        self._order.pop(group_id, None)
        self._groups.pop(group_id, None)
        self._store_connection_layout()

    def move_connection_node(self, node_id: str, parent_id: str, index: int) -> None:
        node = node_id.strip()
        parent = parent_id.strip()
        if not node:
            raise ValueError("Нечего переносить")
        if parent and parent not in self._groups:
            raise ValueError("Группа не найдена")
        if node in self._groups:
            if parent == node or self._group_contains(node, parent):
                raise ValueError("Группу нельзя вложить саму в себя")
        elif self.get_session(node) is None:
            raise ValueError("Сессия не найдена")
        old_parent, old_index = self._locate(node)
        self._detach_node(node)
        children = self._order.setdefault(parent, [])
        place = int(index)
        if old_parent == parent and old_index < place:
            place -= 1
        place = max(0, min(place, len(children)))
        children.insert(place, node)
        group = self._groups.get(node)
        if group is not None:
            self._groups[node] = replace(group, parent_id=parent)
        self._store_connection_layout()

    def set_connection_group_expanded(self, group_id: str, expanded: bool) -> None:
        group = self._groups.get(group_id)
        if group is None:
            raise ValueError("Группа не найдена")
        self._groups[group_id] = replace(group, expanded=bool(expanded))
        self._store_connection_layout()

    def _load_connection_layout(self) -> None:
        loader = getattr(self._catalog, "load_layout", None)
        raw = loader() if callable(loader) else domain.ConnectionLayout()
        self._groups = {}
        self._order = {"": []}
        if raw is None:
            return
        for group in raw.groups:
            if group.group_id and group.group_id not in self._groups:
                self._groups[group.group_id] = group
        for parent, children in raw.order:
            self._order[parent] = list(children)
        self._normalize_layout()

    def _store_connection_layout(self) -> None:
        saver = getattr(self._catalog, "save_layout", None)
        if callable(saver):
            saver(self.connection_layout())

    def _normalize_layout(self) -> None:
        seen: set = set()

        def walk(parent: str, trail: set) -> None:
            cleaned = []
            for child in self._order.get(parent, []):
                if not child or child in seen or child in trail:
                    continue
                seen.add(child)
                cleaned.append(child)
                if child in self._groups:
                    self._groups[child] = replace(self._groups[child], parent_id=parent)
                    self._order.setdefault(child, [])
                    walk(child, trail | {child})
            self._order[parent] = cleaned

        self._order.setdefault("", [])
        walk("", set())
        for group_id in list(self._groups):
            if group_id in seen:
                continue
            self._groups[group_id] = replace(self._groups[group_id], parent_id="")
            self._order[""].append(group_id)
            seen.add(group_id)
            self._order.setdefault(group_id, [])
            walk(group_id, {group_id})
        for parent in list(self._order):
            if parent and parent not in self._groups:
                for child in self._order[parent]:
                    if child not in seen:
                        self._order[""].append(child)
                        seen.add(child)
                del self._order[parent]

    def _group_contains(self, ancestor: str, node: str) -> bool:
        parent = node
        seen: set = set()
        while parent:
            if parent == ancestor:
                return True
            if parent in seen:
                return False
            seen.add(parent)
            group = self._groups.get(parent)
            if group is None:
                return False
            parent = group.parent_id
        return False

    def _locate(self, node_id: str):
        for parent, children in self._order.items():
            if node_id in children:
                return parent, children.index(node_id)
        return None, -1

    def _detach_node(self, node_id: str) -> None:
        for children in self._order.values():
            if node_id in children:
                children.remove(node_id)

    def _remember_session_node(self, session_id: str) -> None:
        if any(session_id in children for children in self._order.values()):
            return
        self._order.setdefault("", []).append(session_id)
        self._store_connection_layout()

    def _forget_session_node(self, session_id: str) -> None:
        self._detach_node(session_id)
        self._store_connection_layout()

    def get_session(
        self, session_id: domain.SessionId
    ) -> Optional[domain.SessionInfo]:
        with self._lock:
            info = self._sessions.get(session_id)
        if info is not None:
            return info
        return self._catalog.get(session_id)

    def session_state(self, session_id: domain.SessionId) -> str:
        """'closed' | 'offline' | 'error' | 'running' | 'ready'.

        offline — журнал открыт, живого драйвера нет.
        error — драйвер есть, но соединение потеряно.
        """
        with self._lock:
            has_driver = session_id in self._drivers
            st = self._states.get(session_id)
            running = session_id in self._running
        if not has_driver:
            if self._journal.is_unlocked(session_id):
                return "offline"
            return "closed"
        if st == domain.SessionStatus.ERROR:
            return "error"
        if running:
            return "running"
        return "ready"

    def shell_label(self, session_id: domain.SessionId) -> str:
        """Подпись нижней строки, если команды идут во вложенную оболочку."""
        with self._lock:
            return self._shell_labels.get(session_id, "")

    def rename_session(
        self, session_id: domain.SessionId, name: str
    ) -> domain.SessionInfo:
        """Сменить имя сессии в каталоге и в открытой копии."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Имя сессии не может быть пустым")
        stored = self._catalog.get(session_id)
        if stored is None:
            raise ValueError("Сессия не найдена")
        updated = replace(stored, name=cleaned)
        self._catalog.save(updated)
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None:
                current = replace(current, name=cleaned)
                self._sessions[session_id] = current
                return current
        return updated

    def update_session_settings(
        self,
        session_id: domain.SessionId,
        name: str,
        mcp_alias: str,
        mcp_description: str,
        os_name: Optional[str] = None,
        username: Optional[str] = None,
        ip_addresses: Optional[str] = None,
        shell_family: Optional[str] = None,
    ) -> domain.SessionInfo:
        """Имя сессии, человеческое имя MCP, описание, ОС, пользователь и IP."""
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("Имя сессии не может быть пустым")
        alias = " ".join(mcp_alias.split())
        description = mcp_description.strip()
        stored = self._catalog.get(session_id)
        if stored is None:
            raise ValueError("Сессия не найдена")
        system = stored.os_name if os_name is None else os_name.strip()
        user = stored.username if username is None else username.strip()
        addresses = stored.ip_addresses if ip_addresses is None else ip_addresses.strip()
        family = (
            stored.shell_family
            if shell_family is None
            else domain.normalize_shell_family(shell_family)
        )
        updated = replace(
            stored,
            name=cleaned,
            mcp_alias=alias,
            mcp_description=description,
            os_name=system,
            username=user,
            ip_addresses=addresses,
            shell_family=family,
        )
        self._catalog.save(updated)
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None:
                current = replace(
                    current,
                    name=cleaned,
                    mcp_alias=alias,
                    mcp_description=description,
                    os_name=system,
                    username=user,
                    ip_addresses=addresses,
                    shell_family=family,
                )
                self._sessions[session_id] = current
                return current
        return updated

    def session_problem(self, session_id: domain.SessionId) -> str:
        """Почему сессия недоступна. Пустая строка, если соединение есть."""
        with self._lock:
            return self._offline_reason.get(session_id, "")

    def get_history(
        self, session_id: domain.SessionId
    ) -> List[domain.TranscriptEvent]:
        if not self._journal.is_unlocked(session_id):
            return []
        return self._journal.get_by_session(session_id)
    
    def complete(
        self, session_id: domain.SessionId, text: str
    ) -> List[str]:
        with self._lock:
            driver = self._drivers.get(session_id)
        if driver is None:
            return []
        try:
            return driver.complete(session_id, text)
        except Exception:
            return []

    def write_input(self, session_id: domain.SessionId, data: str) -> None:
        if not data:
            return
        with self._lock:
            if session_id not in self._running:
                return
            driver = self._drivers.get(session_id)
        if driver is None:
            return
        try:
            driver.write_input(session_id, data)
        except Exception:
            pass

    def resize(
        self, session_id: domain.SessionId, cols: int, rows: int
    ) -> None:
        with self._lock:
            driver = self._drivers.get(session_id)
        if driver is None:
            return
        try:
            driver.resize(session_id, cols, rows)
        except Exception:
            pass

    def cancel_chat(self, session_id: domain.SessionId) -> None:
        """Остановить текущий ответ модели. Сессию и журнал не закрывает."""
        with self._lock:
            stop = self._chat_stops.get(session_id)
            watch = self._mcp_watch.get(session_id)
        if stop is not None:
            stop.cancel()
        if watch is not None:
            watch.done.set()
        self.cancel_command(session_id)

    def cancel_lounge_chat(self, tab_id: str) -> None:
        """Остановить ответ вкладки общего чата и команду, которую она ждёт."""
        origin = lounge_origin(tab_id)
        with self._lock:
            stop = self._chat_stops.get(origin)
            session_id = self._lounge_command.get(origin)
            watch = self._mcp_watch.get(session_id) if session_id else None
        if stop is not None:
            stop.cancel()
        if watch is not None:
            watch.done.set()
        if session_id:
            self.cancel_command(session_id)

    def cancel_command(self, session_id: domain.SessionId) -> None:
        """Остановить текущую интерактивную команду, не закрывая сессию."""
        with self._lock:
            if session_id not in self._running:
                return
            driver = self._drivers.get(session_id)
        if driver is None:
            with self._lock:
                self._running.pop(session_id, None)
            return
        cancel = getattr(driver, "cancel", None)
        if cancel is None:
            return
        try:
            cancel(session_id)
        except Exception:
            pass

    def preview_file_edit(
        self, session_id: domain.SessionId, path: str, old: str, new: str
    ) -> file_edit.FileEditView:
        driver = self._edit_driver(session_id)
        return driver.preview_edit(session_id, path, old, new)

    def apply_file_edit(
        self, session_id: domain.SessionId, path: str, old: str, new: str
    ) -> str:
        driver = self._edit_driver(session_id)
        return driver.apply_edit(session_id, path, old, new)

    def _edit_driver(self, session_id: domain.SessionId):
        if self.session_state(session_id) == "running":
            raise file_edit.FileEditError("Сначала завершите текущую команду")
        if self.session_state(session_id) != "ready":
            raise file_edit.FileEditError("Сессия недоступна. Файл не изменён.")
        with self._lock:
            driver = self._drivers.get(session_id)
        if driver is None or not hasattr(driver, "preview_edit"):
            raise file_edit.FileEditError("Эта сессия не умеет менять файлы")
        return driver

    # --- мастер-пароль журнала -------------------------------------------

    def has_master_password(self) -> bool:
        return self._master.ready()

    def set_master_password(self, password: str, confirmation: str) -> None:
        """Первый ввод за запуск. Хеш пароля сравнивается с хешем подтверждения."""
        self._master.establish(password, confirmation)

    def master_password(self) -> str:
        """Пароль для журналов без пароля подключения. Перед выдачей хеш сверяется."""
        return self._master.reveal()

    def prove_master_password(self) -> None:
        self._master.prove()

    def forget_unproven_master_password(self) -> None:
        self._master.forget_unproven()

    def reset_master_password(self) -> None:
        """Забыть мастер-пароль этого запуска. Ключ SSH и его passphrase остаются."""
        self._master.reset()

    def ssh_key_path(self) -> str:
        settings = self._ssh_key
        if settings is None:
            return ""
        return settings.load()

    def ssh_key_uses_master_passphrase(self) -> bool:
        settings = self._ssh_key
        if settings is None or not settings.load():
            return False
        return bool(settings.passphrase_is_master())

    def opens_with_master_passphrase(self, info: domain.SessionInfo) -> bool:
        """Вход по ключу или агенту: окно сессии не нужно, хватает мастер-пароля."""
        if info.provider not in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            return False
        ssh = info.ssh
        if ssh is None:
            return False
        for node in domain.ssh_targets(ssh):
            if node.auth_method == domain.SSHAuthMethod.PASSWORD:
                return False
            if node.auth_method != domain.SSHAuthMethod.PRIVATE_KEY:
                continue
            path = (node.private_key_path or "").strip()
            if not path:
                return False
            if self._key_needs_passphrase(path) and not self._same_master_key(path):
                return False
        return True

    def needs_master_password(
        self, info: domain.SessionInfo, reconnect: bool = False
    ) -> bool:
        """Открытие журнала просит мастер-пароль. Повтор соединения — только passphrase ключа."""
        if not reconnect:
            return True
        ssh = info.ssh
        if ssh is None:
            return False
        for node in domain.ssh_targets(ssh):
            path = (node.private_key_path or "").strip()
            if (
                node.auth_method == domain.SSHAuthMethod.PRIVATE_KEY
                and path
                and self._same_master_key(path)
                and self._key_needs_passphrase(path)
            ):
                return True
        return False

    def _same_master_key(self, path: str) -> bool:
        if not self.ssh_key_uses_master_passphrase():
            return False
        configured = self.ssh_key_path()
        if not configured or not path.strip():
            return False
        left = os.path.normcase(os.path.abspath(path.strip()))
        right = os.path.normcase(os.path.abspath(configured))
        if left == right:
            return True
        try:
            return os.path.samefile(path, configured)
        except OSError:
            return False

    def _key_needs_passphrase(self, path: str) -> bool:
        cleaned = (path or "").strip()
        if not cleaned or not os.path.isfile(cleaned):
            return False
        from application.ssh_key import load_private_key

        try:
            load_private_key(cleaned, None)
        except Exception as exc:
            return exc.__class__.__name__ == "PasswordRequiredException"
        return False

    def _passphrase_for_file(self, path: str) -> Optional[str]:
        """Ключ без шифрования не получает passphrase: чужая строка для него неверна."""
        if not self._key_needs_passphrase(path):
            return None
        if not self._master.ready():
            return None
        return self.master_password()

    def set_ssh_key_path(
        self,
        path: str,
        passphrase_is_master: Optional[bool] = None,
    ) -> None:
        """Запомнить ключ. Пустая строка сбрасывает первичную настройку."""
        if self._ssh_key is None:
            raise RuntimeError("Хранилище ключа SSH не подключено")
        cleaned = path.strip()
        if cleaned and not self._master.ready():
            raise ValueError("Сначала задайте мастер-пароль в настройках")
        if cleaned:
            if not os.path.isfile(cleaned):
                raise ValueError("Файл ключа не найден")
            from application.ssh_key import load_private_key

            try:
                load_private_key(cleaned, None)
            except Exception as exc:
                if exc.__class__.__name__ != "PasswordRequiredException":
                    raise ValueError(str(exc) or "Файл не является ключом SSH") from exc
        self._ssh_key.save(cleaned, passphrase_is_master)

    def generate_ssh_key(self, path: str, passphrase: str = "") -> str:
        """Создать новый ключ и запомнить его.

        Пустая passphrase шифрует ключ мастер-паролем. При следующем запуске
        этот пароль подставляется в passphrase и подключение открывается сразу.
        """
        cleaned = path.strip()
        if not cleaned:
            raise ValueError("Укажите, куда сохранить ключ")
        if not self._master.ready():
            raise ValueError("Сначала задайте мастер-пароль в настройках")
        use_master = not passphrase
        secret = self.master_password() if use_master else passphrase
        from application.ssh_key import generate_ed25519

        generate_ed25519(cleaned, secret)
        self.set_ssh_key_path(cleaned, passphrase_is_master=use_master)
        return cleaned

    def _master_key_passphrase(self) -> Optional[str]:
        path = self.ssh_key_path()
        if not path or not self._same_master_key(path):
            return None
        return self._passphrase_for_file(path)

    def _with_master_passphrase(
        self,
        ssh: Optional[domain.SSHConfig],
        auth_secret: Optional[str],
        hop_secrets: tuple[str, ...],
    ) -> tuple[Optional[str], tuple[str, ...]]:
        """В зашифрованный ключ ставится мастер-пароль. Ключ без passphrase идёт без неё."""
        if ssh is None:
            return auth_secret, hop_secrets
        targets = domain.ssh_targets(ssh)
        secrets: List[Optional[str]] = [auth_secret, *hop_secrets]
        while len(secrets) < len(targets):
            secrets.append(None)
        filled: List[Optional[str]] = []
        for node, given in zip(targets, secrets):
            path = (node.private_key_path or "").strip()
            if node.auth_method == domain.SSHAuthMethod.PRIVATE_KEY and path:
                if not self._key_needs_passphrase(path):
                    filled.append(None)
                elif self._same_master_key(path) and self._master.ready():
                    filled.append(self.master_password())
                else:
                    filled.append(given)
            else:
                filled.append(given)
        rest = tuple(item or "" for item in filled[1:len(targets)])
        return filled[0], rest

    # --- сценарии ---------------------------------------------------------

    def create_session(
        self,
        name: str,
        password: str,
        provider: domain.ProviderType = domain.ProviderType.CMD,
        ssh: Optional[domain.SSHConfig] = None,
        auth_secret: Optional[str] = None,
        hop_secrets: tuple[str, ...] = (),
        llm_profile_id: Optional[domain.ProfileId] = None,
        shell_family: str = domain.SHELL_UNIX,
        install_key: tuple[bool, ...] = (),
    ) -> domain.SessionInfo:
        if not name.strip():
            raise ValueError("Имя сессии не может быть пустым")
        if not password:
            raise ValueError("Пароль журнала не может быть пустым")
        if provider in (domain.ProviderType.SSH, domain.ProviderType.SSH_OVER_SSH):
            auth_secret, hop_secrets = self._with_master_passphrase(
                ssh, auth_secret, hop_secrets
            )
            self._check_ssh(provider, ssh, auth_secret, hop_secrets, install_key)
        if llm_profile_id and (
            self._llm_profiles is None
            or self._llm_profiles.get(llm_profile_id) is None
        ):
            raise ValueError("Выбранная модель не найдена")

        session_id = domain.new_session_id()
        config = domain.SessionConfig(
            session_id=session_id,
            name=name.strip(),
            provider=provider,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            ssh=ssh,
            auth_secret=auth_secret,
            hop_secrets=hop_secrets,
            install_key=tuple(install_key),
            identity_key_path=self.ssh_key_path() if any(install_key) else None,
            identity_passphrase=(
                self._master_key_passphrase() if any(install_key) else None
            ),
        )

        self._journal.create(session_id, password)

        driver = self._driver_factory(config.provider)
        driver.set_event_sink(self._handle_driver_event)
        try:
            driver.open(config)
        except Exception:
            self._journal.delete(session_id)
            raise

        saved_ssh = config.ssh
        resolved = getattr(driver, "resolved_ssh", None)
        if callable(resolved):
            updated = resolved()
            if updated is not None:
                saved_ssh = updated
        info = domain.SessionInfo(
            session_id=session_id,
            name=config.name,
            provider=config.provider,
            identity=config.identity,
            status=domain.SessionStatus.READY,
            ssh=saved_ssh,
            llm_profile_id=llm_profile_id,
            shell_family=domain.normalize_shell_family(shell_family),
        )
        try:
            self._catalog.save(info)
        except Exception:
            driver.close()
            self._journal.delete(session_id)
            raise
        self._remember_session_node(session_id)

        with self._lock:
            self._drivers[session_id] = driver
            self._sessions[session_id] = info
            self._states[session_id] = domain.SessionStatus.READY
            self._sequences[session_id] = self._journal.next_sequence(session_id)
        self._maybe_probe_os(session_id)
        return info

    def open_session(
        self,
        session_id: domain.SessionId,
        password: str,
        auth_secret: Optional[str] = None,
        hop_secrets: tuple[str, ...] = (),
        ssh_override: Optional[domain.SSHConfig] = None,
    ) -> domain.SessionInfo:
        """Открыть существующую сессию.

        ssh_override позволяет войти под другим логином / другим способом
        аутентификации. При успехе новое значение сохраняется в каталоге.
        """
        with self._lock:
            existing = self._sessions.get(session_id)
        if existing is not None and self._drivers.get(session_id) is not None:
            return existing

        stored = self._catalog.get(session_id)
        if stored is None:
            raise ValueError(f"Сессия {session_id} не найдена в каталоге")
        if not password:
            raise ValueError("Пароль журнала не может быть пустым")

        ssh = stored.ssh
        if stored.provider in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            if ssh is None and ssh_override is None:
                raise ValueError("У сессии отсутствуют SSH-параметры")
            if ssh_override is not None and ssh_override != stored.ssh:
                ssh = ssh_override
                updated = replace(stored, ssh=ssh)
                self._catalog.save(updated)
                stored = updated
            auth_secret, hop_secrets = self._with_master_passphrase(
                ssh, auth_secret, hop_secrets
            )
            self._check_ssh(stored.provider, ssh, auth_secret, hop_secrets)

        self._journal.unlock(session_id, password)

        config = domain.SessionConfig(
            session_id=stored.session_id,
            name=stored.name,
            provider=stored.provider,
            identity=stored.identity,
            ssh=ssh,
            auth_secret=auth_secret,
            hop_secrets=hop_secrets,
            identity_passphrase=self._master_key_passphrase(),
        )
        driver = self._driver_factory(config.provider)
        driver.set_event_sink(self._handle_driver_event)
        try:
            driver.open(config)
        except Exception as exc:
            raise self._fail_open(driver, stored, ssh, exc) from exc

        info = replace(stored, status=domain.SessionStatus.READY, ssh=ssh)
        with self._lock:
            self._drivers[session_id] = driver
            self._sessions[session_id] = info
            self._states[session_id] = domain.SessionStatus.READY
            self._sequences[session_id] = self._journal.next_sequence(session_id)
            self._offline_reason.pop(session_id, None)
        self._maybe_probe_os(session_id)
        return info

    def reconnect_session(
        self,
        session_id: domain.SessionId,
        auth_secret: Optional[str] = None,
        hop_secrets: tuple[str, ...] = (),
        ssh_override: Optional[domain.SSHConfig] = None,
    ) -> domain.SessionInfo:
        """Поднять соединение заново, не закрывая журнал."""
        if self.session_state(session_id) == "running":
            raise RuntimeError("В этой сессии выполняется команда")
        if not self._journal.is_unlocked(session_id):
            raise ValueError("Журнал закрыт")

        stored = self._catalog.get(session_id)
        if stored is None:
            raise ValueError(f"Сессия {session_id} не найдена в каталоге")

        ssh = stored.ssh
        if stored.provider in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            if ssh is None and ssh_override is None:
                raise ValueError("У сессии отсутствуют SSH-параметры")
            if ssh_override is not None and ssh_override != stored.ssh:
                ssh = ssh_override
                updated = replace(stored, ssh=ssh)
                self._catalog.save(updated)
                stored = updated
            auth_secret, hop_secrets = self._with_master_passphrase(
                ssh, auth_secret, hop_secrets
            )
            self._check_ssh(stored.provider, ssh, auth_secret, hop_secrets)

        self._release_driver(session_id)
        config = domain.SessionConfig(
            session_id=stored.session_id,
            name=stored.name,
            provider=stored.provider,
            identity=stored.identity,
            ssh=ssh,
            auth_secret=auth_secret,
            hop_secrets=hop_secrets,
            identity_passphrase=self._master_key_passphrase(),
        )
        driver = self._driver_factory(config.provider)
        driver.set_event_sink(self._handle_driver_event)
        try:
            driver.open(config)
        except Exception as exc:
            raise self._fail_open(driver, stored, ssh, exc) from exc

        info = replace(stored, status=domain.SessionStatus.READY, ssh=ssh)
        with self._lock:
            self._drivers[session_id] = driver
            self._sessions[session_id] = info
            self._states[session_id] = domain.SessionStatus.READY
            self._sequences[session_id] = self._journal.next_sequence(session_id)
            self._offline_reason.pop(session_id, None)
        self._maybe_probe_os(session_id)
        return info

    def _fail_open(
        self,
        driver: SessionDriver,
        stored: domain.SessionInfo,
        ssh: Optional[domain.SSHConfig],
        exc: BaseException,
    ) -> SessionUnavailable:
        self._abandon_driver(driver)
        info = self._keep_journal_offline(stored, ssh, exc)
        reason = str(exc) or exc.__class__.__name__
        return SessionUnavailable(info, reason)

    def _abandon_driver(self, driver: SessionDriver) -> None:
        try:
            driver.set_event_sink(lambda _event: None)
        except Exception:
            pass
        try:
            driver.close()
        except Exception:
            pass

    def _keep_journal_offline(
        self,
        stored: domain.SessionInfo,
        ssh: Optional[domain.SSHConfig],
        exc: BaseException,
    ) -> domain.SessionInfo:
        """Соединение не поднялось. Журнал остаётся открытым."""
        reason = str(exc) or exc.__class__.__name__
        info = replace(stored, status=domain.SessionStatus.ERROR, ssh=ssh)
        with self._lock:
            self._drivers.pop(stored.session_id, None)
            self._sessions[stored.session_id] = info
            self._states[stored.session_id] = domain.SessionStatus.ERROR
            self._sequences[stored.session_id] = self._journal.next_sequence(
                stored.session_id
            )
            self._offline_reason[stored.session_id] = reason
        return info

    def _release_driver(self, session_id: domain.SessionId) -> None:
        """Закрыть драйвер и не запирать журнал."""
        with self._lock:
            driver = self._drivers.pop(session_id, None)
            self._running.pop(session_id, None)
            self._shell_labels.pop(session_id, None)
        if driver is None:
            return
        try:
            driver.set_event_sink(lambda _event: None)
        except Exception:
            pass
        try:
            driver.close()
        except Exception:
            pass

    def close_session(self, session_id: domain.SessionId) -> None:
        with self._lock:
            driver = self._drivers.pop(session_id, None)
            self._sessions.pop(session_id, None)
            self._running.pop(session_id, None)
            self._shell_labels.pop(session_id, None)
            self._states.pop(session_id, None)
            self._offline_reason.pop(session_id, None)
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
        try:
            self._journal.lock(session_id)
        except Exception:
            pass

    def delete_session(self, session_id: domain.SessionId) -> None:
        """Закрыть сессию, удалить журнал и запись в каталоге."""
        if self.session_state(session_id) == "running":
            raise RuntimeError("В этой сессии выполняется команда")
        stored = self.get_session(session_id)
        if stored is None and self.session_state(session_id) == "closed":
            raise ValueError("Сессия не найдена")
        self.close_session(session_id)
        self._journal.delete(session_id)
        self._catalog.delete(session_id)
        self._forget_session_node(session_id)

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._drivers.keys())
            server = self._mcp_server
            self._mcp_server = None
        for sid in ids:
            self.close_session(sid)
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass

    def send_command(
        self, session_id: domain.SessionId, text: str, *, os_probe: bool = False
    ) -> domain.CommandId:
        with self._lock:
            driver = self._drivers.get(session_id)
            if driver is None:
                raise NoActiveSessionError(f"Сессия {session_id} не открыта")
            if self._states.get(session_id) == domain.SessionStatus.ERROR:
                raise NoActiveSessionError("SSH-соединение разорвано")
            if session_id in self._running:
                raise RuntimeError("В этой сессии уже выполняется команда")
            command_id = domain.new_command_id()
            self._running[session_id] = command_id
            if os_probe:
                self._os_probe[session_id] = command_id
                self._os_live[session_id] = ""
                self._os_snapshot.pop(session_id, None)

        request = domain.CommandRequest(
            command_id=command_id,
            session_id=session_id,
            text=text,
        )
        try:
            driver.send(request)
        except Exception:
            with self._lock:
                self._running.pop(session_id, None)
                if os_probe:
                    self._os_probe.pop(session_id, None)
                    self._os_live.pop(session_id, None)
                    self._os_snapshot.pop(session_id, None)
            raise
        return command_id

    # --- обработка событий драйвера ---------------------------------------

    def _handle_driver_event(self, event: domain.OutputEvent) -> None:
        abort_mcp = False
        follow_command = ""
        ip_addresses: Optional[list] = None
        user_name: Optional[str] = None
        with self._lock:
            sequence = (
                self._sequences.get(event.session_id, 0) if event.persist else 0
            )

            if (
                event.event_type == domain.EventType.SESSION_STATUS
                and event.status is not None
            ):
                self._states[event.session_id] = event.status
                if event.status == domain.SessionStatus.ERROR and event.payload:
                    if event.payload != "disconnected":
                        self._offline_reason[event.session_id] = event.payload
                elif event.status == domain.SessionStatus.READY:
                    self._offline_reason.pop(event.session_id, None)
            if (
                event.event_type == domain.EventType.ERROR
                and event.command_id is None
                and event.payload
            ):
                self._offline_reason[event.session_id] = event.payload

            transcript = domain.TranscriptEvent(
                event_id=domain.new_event_id(),
                session_id=event.session_id,
                sequence=sequence,
                event_type=event.event_type,
                timestamp=event.timestamp,
                command_id=event.command_id,
                stream_type=event.stream_type,
                payload=event.payload,
                return_code=event.return_code,
                status=event.status,
                persist=event.persist,
                columns=event.columns,
                rows=event.rows,
                alternate=event.alternate,
            )

            probe_id = self._os_probe.get(event.session_id)
            is_probe = (
                probe_id is not None and event.command_id == probe_id
            )
            if is_probe and event.event_type == domain.EventType.OUTPUT_CHUNK:
                if event.persist:
                    self._os_snapshot[event.session_id] = event.payload
                else:
                    self._os_live[event.session_id] = (
                        self._os_live.get(event.session_id, "") + event.payload
                    )
                return
            if is_probe and event.event_type in (
                domain.EventType.COMMAND_SENT,
                domain.EventType.ERROR,
            ):
                return

            os_text: Optional[str] = None
            if event.event_type == domain.EventType.COMMAND_FINISHED:
                self._running.pop(event.session_id, None)
                if is_probe:
                    raw = self._os_snapshot.pop(event.session_id, "")
                    if not raw.strip():
                        raw = self._os_live.pop(event.session_id, "")
                    else:
                        self._os_live.pop(event.session_id, None)
                    self._os_probe.pop(event.session_id, None)
                    phase = self._os_phase.get(event.session_id, "os")
                    if phase == "ip":
                        addresses = _collect_addresses(raw)
                        if addresses:
                            ip_addresses = addresses
                            follow_command = self._begin_user_probe(event.session_id)
                        else:
                            follow_command = self._next_ip_command(event.session_id)
                    elif phase == "user":
                        cleaned_user = _clean_username(raw)
                        if cleaned_user:
                            user_name = cleaned_user
                            self._finish_probe(event.session_id)
                        else:
                            follow_command = self._next_user_command(event.session_id)
                    else:
                        cleaned_os = _clean_os_output(raw)
                        if cleaned_os:
                            os_text = cleaned_os
                            follow_command = self._begin_ip_probe(
                                event.session_id, cleaned_os
                            )
                        else:
                            follow_command = self._next_os_command(event.session_id)
                    transcript = domain.TranscriptEvent(
                        event_id=transcript.event_id,
                        session_id=transcript.session_id,
                        sequence=0,
                        event_type=transcript.event_type,
                        timestamp=transcript.timestamp,
                        command_id=transcript.command_id,
                        stream_type=transcript.stream_type,
                        payload=transcript.payload,
                        return_code=transcript.return_code,
                        status=transcript.status,
                        persist=False,
                    )
            if event.event_type == domain.EventType.SHELL_CONTEXT:
                if event.payload:
                    self._shell_labels[event.session_id] = event.payload
                else:
                    self._shell_labels.pop(event.session_id, None)
            if (
                event.event_type == domain.EventType.SESSION_STATUS
                and event.status in (
                    domain.SessionStatus.CLOSED,
                    domain.SessionStatus.ERROR,
                )
            ):
                self._shell_labels.pop(event.session_id, None)

            abort_mcp = self._observe_mcp_locked(event)
            subscribers = list(self._subscribers)

            journal_error: Optional[BaseException] = None
            if event.persist and os_text is None:
                try:
                    self._journal.append(transcript)
                except Exception as exc:
                    journal_error = exc
                else:
                    self._sequences[event.session_id] = sequence + 1

        if abort_mcp:
            self.cancel_command(event.session_id)

        for cb in subscribers:
            try:
                cb(transcript)
            except Exception:
                pass

        if os_text:
            self._store_os(event.session_id, os_text)
            self._append_chat(event.session_id, domain.EventType.CHAT_OS, os_text)
        if ip_addresses:
            self._store_ips(event.session_id, ip_addresses)
        if user_name:
            self._store_username(event.session_id, user_name)
        if follow_command:
            try:
                self.send_command(event.session_id, follow_command, os_probe=True)
            except Exception:
                pass

        if journal_error is not None:
            with self._lock:
                err_event = domain.TranscriptEvent(
                    event_id=domain.new_event_id(),
                    session_id=event.session_id,
                    sequence=0,
                    event_type=domain.EventType.ERROR,
                    timestamp=datetime.now(),
                    stream_type=domain.StreamType.SYSTEM,
                    payload=f"Ошибка записи журнала: {journal_error}",
                    persist=False,
                )
                subscribers = list(self._subscribers)
            for cb in subscribers:
                try:
                    cb(err_event)
                except Exception:
                    pass

    # --- модели -----------------------------------------------------------

    def list_llm_profiles(self) -> List[domain.LlmProfile]:
        if self._llm_profiles is None:
            return []
        return self._llm_profiles.list()

    def get_llm_profile(
        self, profile_id: domain.ProfileId
    ) -> Optional[domain.LlmProfile]:
        if self._llm_profiles is None:
            return None
        return self._llm_profiles.get(profile_id)

    def save_llm_profile(self, profile: domain.LlmProfile) -> domain.LlmProfile:
        """Проверяет GET /v1/models и пишет профиль только если id есть в списке."""
        if self._llm_profiles is None or self._llm is None:
            raise RuntimeError("Хранилище моделей не подключено")
        name = profile.name.strip()
        base_url = profile.base_url.strip().rstrip("/")
        model_id = profile.model_id.strip()
        if not name:
            raise ValueError("Укажите имя профиля")
        if not base_url:
            raise ValueError("Укажите адрес сервера")
        if not model_id:
            raise ValueError("Укажите id модели")
        spec = domain.llm_api_spec(profile.api_kind)
        if spec.key_required and not profile.api_key.strip():
            raise ValueError("Укажите API-ключ")
        probe = replace(
            profile,
            name=name,
            base_url=base_url,
            model_id=model_id,
            profile_id=profile.profile_id or domain.new_profile_id(),
        )
        try:
            models = self._llm.list_models(probe)
        except Exception as exc:
            raise ConnectionError(str(exc) or exc.__class__.__name__) from exc
        if model_id not in models:
            shown = ", ".join(models[:8]) or "список пуст"
            raise ConnectionError(
                f"Модель «{model_id}» не найдена на сервере. Доступны: {shown}"
            )
        self._llm_profiles.save(probe)
        return probe

    def delete_llm_profile(self, profile_id: domain.ProfileId) -> None:
        if self._llm_profiles is None:
            return
        self._llm_profiles.delete(profile_id)

    def llm_problem(self, session_id: domain.SessionId) -> str:
        with self._lock:
            return self._llm_problem.get(session_id, "")

    def check_llm(self, session_id: domain.SessionId) -> None:
        """Повторная проверка модели. Терминал сессии не трогает."""
        profile = self._require_profile(session_id)
        if self._llm is None:
            raise LlmUnavailable("Клиент модели не подключён")
        try:
            models = self._llm.list_models(profile)
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            with self._lock:
                self._llm_problem[session_id] = reason
            raise LlmUnavailable(reason) from exc
        if profile.model_id not in models:
            reason = f"Модель «{profile.model_id}» не найдена на сервере"
            with self._lock:
                self._llm_problem[session_id] = reason
            raise LlmUnavailable(reason)
        with self._lock:
            self._llm_problem.pop(session_id, None)

    def set_command_approver(
        self, approver: Callable[[CommandAsk], CommandAnswer]
    ) -> None:
        self._approver = approver

    def set_foreground_session(self, session_id: Optional[domain.SessionId]) -> None:
        with self._lock:
            self._foreground = session_id

    def attach_mcp(self, server) -> str:
        """Поднимает MCP и запоминает адрес. Повторный вызов возвращает тот же адрес."""
        with self._lock:
            if self._mcp_server is not None:
                return self._mcp_endpoint
            self._mcp_server = server
        endpoint = server.start()
        with self._lock:
            self._mcp_endpoint = endpoint
        return endpoint

    def mcp_listen(self) -> tuple:
        """Сохранённые IP и текст портов. Пока файл не записан — 127.0.0.1 и 47321-47340."""
        settings = self._mcp_settings
        if settings is None:
            from mcp_host import DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS

            return DEFAULT_MCP_HOST, DEFAULT_MCP_PORTS
        return settings.load()

    def update_mcp_listen(self, host: str, ports: tuple, ports_text: str) -> str:
        """Занимает новый адрес MCP и запоминает его. Уже слушающий тот же адрес не перезапускается."""
        server = self._mcp_server
        if server is None or not hasattr(server, "rebind"):
            raise RuntimeError("MCP-сервер не подключён")
        current_host, current_ports = server.listen()
        if server.running() and current_host == host and tuple(current_ports) == tuple(ports):
            endpoint = server.endpoint
        else:
            try:
                endpoint = server.rebind(host, ports)
            except OSError as exc:
                restored = ""
                try:
                    restored = server.rebind(current_host, current_ports)
                except OSError:
                    restored = ""
                with self._lock:
                    self._mcp_endpoint = restored
                message = str(exc) or "Не удалось занять порт MCP"
                raise OSError(message) from exc
        if self._mcp_settings is not None:
            self._mcp_settings.save(host, ports_text)
        with self._lock:
            self._mcp_endpoint = endpoint
        return endpoint

    def mcp_endpoint(self, session_id: Optional[domain.SessionId] = None) -> str:
        """Адрес MCP. С id сессии — только её терминал, его можно указать в Cursor."""
        with self._lock:
            base = self._mcp_endpoint
        if not base or session_id is None:
            return base
        info = self.get_session(session_id)
        if info is None or not info.llm_profile_id:
            return ""
        return f"{base.rstrip('/')}/{session_id}"

    def run_foreground_mcp(self, intent: str, command: str) -> str:
        """Инструмент MCP: команда идёт в текущую активную сессию."""
        with self._lock:
            session_id = self._foreground
        if not session_id:
            return refusal_reason("нет активной сессии")
        return self.run_mcp_command(session_id, intent, command)

    def run_mcp_command(
        self,
        session_id: domain.SessionId,
        intent: str,
        command: str,
        origin_id: str = "",
    ) -> str:
        """Спросить подтверждение в чате-источнике и выполнить команду в терминале."""
        with self._mcp_gate:
            return self._run_mcp_command(session_id, intent, command, origin_id)

    def send_chat(self, session_id: domain.SessionId, text: str) -> None:
        """Пишет сообщение в журнал и запрашивает ответ только по переписке."""
        message = text.strip()
        if not message:
            raise ValueError("Пустое сообщение")
        if not self._journal.is_unlocked(session_id):
            raise ValueError("Журнал закрыт")
        profile = self._require_profile(session_id)
        if self._llm is None:
            raise LlmUnavailable("Клиент модели не подключён")
        self._append_chat(session_id, domain.EventType.CHAT_USER, message)
        with self._lock:
            self._llm_problem.pop(session_id, None)
        warning = ""
        stop = domain.ChatStop()
        with self._lock:
            self._chat_stops[session_id] = stop
        token = domain.bind_chat_stop(stop)
        try:
            if getattr(self._llm, "complete_turn", None) is None:
                history = [("system", self._system_prompt(session_id))]
                history.extend(self._active_chat(session_id))
                answer = self._llm.complete(profile, history)
            else:
                answer, warning = self._complete_with_tools(session_id, profile)
        except domain.ChatStopped:
            with self._lock:
                self._llm_problem.pop(session_id, None)
            return
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            with self._lock:
                self._llm_problem[session_id] = reason
            raise LlmUnavailable(reason) from exc
        else:
            with self._lock:
                if warning:
                    self._llm_problem[session_id] = warning
                else:
                    self._llm_problem.pop(session_id, None)
            self._append_chat(session_id, domain.EventType.CHAT_ASSISTANT, answer)
        finally:
            domain.reset_chat_stop(token)
            with self._lock:
                if self._chat_stops.get(session_id) is stop:
                    self._chat_stops.pop(session_id, None)

    def _complete_with_tools(
        self,
        session_id: domain.SessionId,
        profile: domain.LlmProfile,
        *,
        history: Optional[List[tuple]] = None,
        tools: Optional[list] = None,
        note: Optional[Callable[[domain.EventType, str], None]] = None,
        dispatch: Optional[Callable[[object], str]] = None,
        system: Optional[str] = None,
    ) -> tuple:
        if note is None:
            def note(kind: domain.EventType, text: str) -> None:
                self._append_chat(session_id, kind, text)
        if dispatch is None:
            def dispatch(call: object) -> str:
                return self._dispatch_tool(session_id, call)
        if history is None:
            history = self._active_chat(session_id)
        if tools is None:
            tools = openai_tools(self._session_os(session_id))
        if system is None:
            system = self._system_prompt(session_id)
        messages: List[dict] = [{"role": "system", "content": system}]
        for role, text in history:
            messages.append({"role": role, "content": text})
        executed: set = set()
        last_result = ""
        cut_parts: List[str] = []
        repair_used = False
        promise_nudged = False
        warning = ""
        # Предел только чтобы цикл не крутился бесконечно. Это не лимит модели.
        # По его достижении ход не обрывается: модель дописывает ответ без новых команд.
        for _round in range(_TOOL_ROUNDS):
            self._raise_if_chat_stopped()
            try:
                turn = self._llm.complete_turn(profile, messages, tools)  # type: ignore[union-attr]
            except domain.ChatStopped:
                raise
            except Exception as exc:
                if _round == 0 and _tools_rejected(exc):
                    history = [
                        (str(item.get("role") or "user"), str(item.get("content") or ""))
                        for item in messages
                        if item.get("role") in ("system", "user", "assistant")
                    ]
                    content = self._llm.complete(profile, history)  # type: ignore[union-attr]
                    warning = (
                        "Сервер не принял инструменты, окно подтверждения "
                        f"команды не открывалось. {exc}"
                    )
                    return _join_cut(cut_parts, str(content or "")), warning
                if last_result or _last_assistant_text(messages):
                    text = self._answer_without_tools(
                        profile, messages, tools, last_result
                    )
                    warning = warning or (
                        "Ход с инструментом прервался, команда могла не запуститься. "
                        f"{exc}"
                    )
                    return _join_cut(cut_parts, text), warning
                raise
            else:
                content = str(getattr(turn, "content", "") or "")
                calls = list(getattr(turn, "tool_calls", []) or [])
                finish = str(getattr(turn, "finish_reason", "") or "stop")
            prose, recovered = promote_shell_actions(content)
            content = prose
            cut = finish == "length"
            broken = any(getattr(call, "broken", False) for call in calls)
            tool_stop = finish in ("tool_calls", "tool_use", "function_call")
            if broken or (cut and calls) or (tool_stop and not calls):
                # Вызов оборван или заявка на инструмент пришла без команды.
                # Выполнять её нельзя и нельзя закрывать ход как обычный текст.
                if repair_used:
                    if content.strip():
                        note(domain.EventType.CHAT_ASSISTANT, content)
                    raise ConnectionError(_unfinished_tool_error(finish))
                repair_used = True
                if content.strip():
                    cut_parts.append(content.strip())
                self._ask_to_continue(messages, content, cut=True)
                continue
            if not calls and not cut:
                calls = recovered
            parsed = calls
            calls = _fresh_calls(parsed, executed)
            if _calls_without_command(parsed) and not calls and not cut:
                if content.strip():
                    note(domain.EventType.CHAT_ASSISTANT, content)
                raise ConnectionError(
                    "Модель вызвала инструмент, но команда не разобрана. "
                    "Окно подтверждения не открывалось."
                )
            if not calls:
                if cut or tool_stop:
                    if repair_used:
                        if content.strip():
                            note(domain.EventType.CHAT_ASSISTANT, content)
                        raise ConnectionError(_unfinished_tool_error(finish))
                    repair_used = True
                    if content.strip():
                        cut_parts.append(content.strip())
                    self._ask_to_continue(messages, content, cut=True)
                    continue
                if (
                    content.strip()
                    and not promise_nudged
                    and _promises_action(content)
                ):
                    # «Сейчас проверю» без run_command — ход ещё не закончен.
                    promise_nudged = True
                    self._ask_for_tool(messages, content)
                    continue
                if content.strip():
                    if promise_nudged and _promises_action(content):
                        warning = warning or _MISSED_TOOL
                    elif repair_used and not last_result:
                        warning = warning or _unfinished_tool_error(finish)
                    return _join_cut(cut_parts, content), warning
                # Пустой ход сразу после команды — не конец ответа.
                # Модель ещё должна сказать, что вышло из вывода.
                return (
                    _join_cut(
                        cut_parts,
                        self._answer_without_tools(
                            profile, messages, tools, last_result
                        ),
                    ),
                    warning,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [_tool_call_payload(call) for call in calls],
                }
            )
            if content.strip():
                note(domain.EventType.CHAT_ASSISTANT, content)
            for call in calls:
                self._raise_if_chat_stopped()
                result = dispatch(call)
                self._raise_if_chat_stopped()
                last_result = result
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(getattr(call, "id", "") or "call"),
                        "content": result,
                    }
                )
        return (
            _join_cut(
                cut_parts,
                self._answer_without_tools(profile, messages, tools, last_result),
            ),
            warning or (
                "Лимит вызовов инструментов исчерпан. "
                "Окно подтверждения для следующей команды не открывалось."
            ),
        )

    def _ask_to_continue(self, messages: List[dict], content: str, cut: bool) -> None:
        if content.strip():
            messages.append({"role": "assistant", "content": content})
        if cut:
            note = (
                "Ответ оборвался по лимиту длины, генерация на сервере остановилась. "
                "Продолжи с места обрыва и не повторяй уже написанное. "
                "Если нужна команда, вызови run_command целиком, одним вызовом."
            )
        else:
            note = (
                "Вызов инструмента пришёл повреждённым и не выполнялся. "
                "Повтори run_command целиком или ответь текстом."
            )
        messages.append({"role": "user", "content": note})

    def _ask_for_tool(self, messages: List[dict], content: str) -> None:
        if content.strip():
            messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Инструмент run_command не вызван, команда не запускалась "
                    "и окно подтверждения не открывалось. Блок ```bash сам не "
                    "выполняется. Если нужна проверка в терминале — вызови "
                    "run_command сейчас, одним целым вызовом. Если команда не "
                    "нужна — ответь по сути и не пиши, что сейчас проверишь."
                ),
            }
        )

    def _answer_without_tools(
        self,
        profile: domain.LlmProfile,
        messages: List[dict],
        tools: list,
        last_result: str,
    ) -> str:
        """Дописать ответ пользователю, не вызывая новые команды."""
        closing = messages + [
            {
                "role": "user",
                "content": (
                    "Новые команды не вызывай. Ответь пользователю по уже "
                    "полученному выводу. Если вывода мало, так и напиши."
                ),
            }
        ]
        self._raise_if_chat_stopped()
        try:
            turn = self._llm.complete_turn(  # type: ignore[union-attr]
                profile, closing, tools, allow_tools=False
            )
        except domain.ChatStopped:
            raise
        except Exception as exc:
            if not _tools_rejected(exc):
                text = _last_assistant_text(messages)
                if text:
                    return text
                if last_result:
                    return last_result
                raise
            history = [
                (str(item.get("role") or "user"), str(item.get("content") or ""))
                for item in closing
                if item.get("role") in ("system", "user", "assistant")
            ]
            text = self._llm.complete(profile, history)  # type: ignore[union-attr]
            if str(text or "").strip():
                return str(text)
        else:
            text = str(getattr(turn, "content", "") or "").strip()
            if text:
                return text
        fallback = _last_assistant_text(messages)
        if fallback:
            return fallback
        if last_result:
            return last_result
        raise ConnectionError("Пустой ответ модели")

    def _dispatch_tool(self, session_id: domain.SessionId, call: object) -> str:
        name = str(getattr(call, "name", "") or "")
        arguments = getattr(call, "arguments", {}) or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return refusal_reason("не удалось прочитать аргументы")
        if name != TOOL_NAME or not isinstance(arguments, dict):
            raise ConnectionError(
                "Модель вызвала инструмент "
                f"«{name or 'без имени'}», а не {TOOL_NAME}. "
                "Окно подтверждения команды не открывалось."
            )
        command = command_text(arguments)
        if not command:
            raise ConnectionError(
                "Модель вызвала run_command без текста команды. "
                "Окно подтверждения не открывалось."
            )
        return self.run_mcp_command(
            session_id,
            str(arguments.get("intent") or ""),
            command,
        )

    def command_rule_lists(self) -> dict:
        store = self._command_rules
        if store is None:
            return {
                domain.SHELL_UNIX: (),
                domain.SHELL_WINDOWS: (),
                domain.SHELL_OTHER: (),
            }
        return store.lists()

    def add_command_rule(self, family: str, pattern: str) -> None:
        store = self._command_rules
        if store is None:
            raise RuntimeError("Хранилище правил не подключено")
        store.add(family, pattern)

    def remove_command_rule(self, family: str, pattern: str) -> None:
        store = self._command_rules
        if store is None:
            raise RuntimeError("Хранилище правил не подключено")
        store.remove(family, pattern)

    def command_preview(self, session_id: domain.SessionId, command: str) -> tuple:
        """Строки и подсветка команды с учётом текущих шаблонов её ОС."""
        info = self.get_session(session_id)
        family = info.shell_family if info is not None else domain.SHELL_UNIX
        text = normalize_command(command)
        return split_shell_commands(text, family), self._command_spans(text, family)

    def _command_spans(self, command: str, family: str) -> tuple:
        store = self._command_rules
        if store is None:
            return ()
        return command_spans(command, family, store.rule_set_for(family))

    def _run_mcp_command(
        self,
        session_id: domain.SessionId,
        intent: str,
        command: str,
        origin_id: str = "",
    ) -> str:
        text = normalize_command(command)
        why = " ".join(intent.split()) or "выполнить команду"
        origin = origin_id.strip()
        if not text:
            return refusal_reason("пустая команда")
        info = self.get_session(session_id)
        if info is None:
            return refusal_reason("сессия не найдена")
        if not lounge_tab_id(origin) and not info.llm_profile_id:
            return refusal_reason("у активной сессии нет чата для подтверждения")
        if self.session_state(session_id) != "ready":
            return refusal_reason("сессия не готова принять команду")
        allow = self._command_allow
        if allow is not None and allow.allows(session_id, text):
            return self._execute_mcp_command(session_id, text)
        parts = split_shell_commands(text, info.shell_family)
        spans = self._command_spans(text, info.shell_family)
        approver = self._approver
        if approver is None:
            return refusal_reason("подтверждение команды недоступно")
        answer = approver(
            CommandAsk(
                request_id=f"mcp-{uuid4().hex}",
                session_id=session_id,
                intent=why,
                command=text,
                origin_id=origin,
                parts=parts,
                spans=spans,
            )
        )
        action = answer.action
        if action == "reject":
            return refusal()
        if action == "reject_reason":
            reason = answer.reason.strip() or "причина не указана"
            return refusal_reason(reason)
        if action == "always":
            if allow is not None:
                allow.remember(session_id, text)
            return self._execute_mcp_command(session_id, text)
        if action == "run":
            return self._execute_mcp_command(session_id, text)
        if action == "deliver":
            return answer.body.strip() or refusal_reason("пустой вывод")
        return refusal()

    def capture_mcp_output(
        self, session_id: domain.SessionId, command: str
    ) -> tuple:
        """Выполнить команду. Вернуть текст для модели, сырой вывод и код."""
        watch = _McpWatch()
        with self._lock:
            self._mcp_watch[session_id] = watch
        try:
            try:
                self.send_command(session_id, command)
            except Exception as exc:
                reason = refusal_reason(str(exc) or "не удалось запустить команду")
                return reason, "", None
            if not self._wait_mcp(watch, 600):
                self.cancel_command(session_id)
                watch.done.wait(5)
            self._raise_if_chat_stopped()
            output = watch.output if watch.output.strip() else watch.live
            if watch.refusal:
                return refusal_reason(watch.refusal), output, watch.code
            if watch.interactive or watch.shell:
                if watch.shell and not watch.interactive:
                    self._exit_spawned_shell(session_id)
                return refusal_reason(INTERACTIVE_REASON), output, watch.code
            return finished_text(watch.code, output), output, watch.code
        finally:
            with self._lock:
                if self._mcp_watch.get(session_id) is watch:
                    self._mcp_watch.pop(session_id, None)

    def _execute_mcp_command(self, session_id: domain.SessionId, command: str) -> str:
        model_text, _output, _code = self.capture_mcp_output(session_id, command)
        return model_text

    def _raise_if_chat_stopped(self) -> None:
        stop = domain.current_chat_stop()
        if stop is not None and stop.cancelled():
            raise domain.ChatStopped()

    def _wait_mcp(self, watch: "_McpWatch", seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not watch.done.wait(0.2):
            self._raise_if_chat_stopped()
            if time.monotonic() >= deadline:
                return False
        return True

    def _exit_spawned_shell(self, session_id: domain.SessionId) -> None:
        watch = _McpWatch()
        watch.ignore_shell = True
        with self._lock:
            self._mcp_watch[session_id] = watch
        try:
            try:
                self.send_command(session_id, "exit")
            except Exception:
                return
            watch.done.wait(8)
        finally:
            with self._lock:
                if self._mcp_watch.get(session_id) is watch:
                    self._mcp_watch.pop(session_id, None)

    def _observe_mcp_locked(self, event: domain.OutputEvent) -> bool:
        watch = self._mcp_watch.get(event.session_id)
        if watch is None:
            return False
        if event.command_id and watch.command_id is None:
            watch.command_id = event.command_id
        if (
            watch.command_id
            and event.command_id
            and event.command_id != watch.command_id
        ):
            return False
        if event.event_type == domain.EventType.SHELL_CONTEXT:
            if event.payload and not watch.ignore_shell:
                watch.shell = True
            return False
        if event.event_type == domain.EventType.OUTPUT_CHUNK:
            if event.alternate and not watch.interactive and not watch.ignore_shell:
                watch.interactive = True
                return True
            if event.persist:
                watch.output = event.payload
            else:
                watch.live += event.payload
            return False
        if event.event_type == domain.EventType.COMMAND_FINISHED:
            watch.code = event.return_code
            watch.done.set()
        return False

    def _require_profile(self, session_id: domain.SessionId) -> domain.LlmProfile:
        info = self.get_session(session_id)
        if info is None or not info.llm_profile_id:
            raise ValueError("У сессии нет модели")
        if self._llm_profiles is None:
            raise LlmUnavailable("Хранилище моделей не подключено")
        profile = self._llm_profiles.get(info.llm_profile_id)
        if profile is None:
            reason = "Сохранённая модель не найдена"
            with self._lock:
                self._llm_problem[session_id] = reason
            raise LlmUnavailable(reason)
        return profile

    def chat_limit(self, session_id: domain.SessionId) -> int:
        """Лимит контекста профиля этой сессии. 0 — чата с моделью нет."""
        info = self.get_session(session_id)
        if info is None or not info.llm_profile_id or self._llm_profiles is None:
            return 0
        profile = self._llm_profiles.get(info.llm_profile_id)
        if profile is None:
            return 0
        return profile.context_limit or domain.DEFAULT_CONTEXT_LIMIT

    def set_chat_limit(self, session_id: domain.SessionId, limit: int) -> None:
        """Запомнить лимит контекста в профиле модели этой сессии."""
        info = self.get_session(session_id)
        if info is None or not info.llm_profile_id or self._llm_profiles is None:
            return
        profile = self._llm_profiles.get(info.llm_profile_id)
        if profile is None:
            return
        bounded = max(256, min(1_000_000, int(limit)))
        if profile.context_limit == bounded:
            return
        self._llm_profiles.save(replace(profile, context_limit=bounded))

    def chat_context(self, session_id: domain.SessionId) -> ChatContext:
        """Оценка контекста, который сейчас видит модель. Это не токены самой модели."""
        parts = [self._system_prompt(session_id)]
        messages = self._active_chat(session_id)
        parts.extend(text for _role, text in messages)
        characters = sum(len(part) for part in parts)
        return ChatContext(
            messages=len(messages),
            characters=characters,
            tokens=_approx_tokens("\n".join(parts)),
        )

    def reset_chat_context(self, session_id: domain.SessionId) -> None:
        """Дальше модель не видит переписку выше. Журнал и лента чата остаются."""
        if not self._journal.is_unlocked(session_id):
            raise ValueError("Журнал закрыт")
        self._require_profile(session_id)
        self._append_chat(session_id, domain.EventType.CHAT_RESET, "")

    def _maybe_probe_os(self, session_id: domain.SessionId) -> None:
        """Первый запуск: узнать ОС, даже если у сессии нет модели."""
        info = self.get_session(session_id)
        if info is None:
            return
        if info.os_name.strip():
            self._probe_identity(session_id, info)
            return
        remembered = self._journal_os(session_id)
        if remembered:
            self._store_os(session_id, remembered)
            fresh = self.get_session(session_id)
            if fresh is not None:
                self._probe_identity(session_id, fresh)
            return
        if self.session_state(session_id) != "ready":
            return
        if session_id in self._os_probe:
            return
        plan = self._os_command_plan(info)
        with self._lock:
            self._os_phase[session_id] = "os"
            self._os_step[session_id] = 0
        try:
            self.send_command(session_id, plan[0], os_probe=True)
        except Exception:
            with self._lock:
                self._finish_probe(session_id)

    def _os_command_plan(self, info: domain.SessionInfo) -> tuple:
        """Сначала команды вероятной ОС, затем команды другой семьи."""
        windows = ("ver", "cmd /c ver")
        unix = ("uname -srm",)
        if info.provider == domain.ProviderType.CMD:
            return windows + unix
        return unix + windows

    def _next_os_command(self, session_id: domain.SessionId) -> str:
        info = self.get_session(session_id)
        if info is None:
            self._finish_probe(session_id)
            return ""
        step = self._os_step.get(session_id, 0) + 1
        plan = self._os_command_plan(info)
        if step >= len(plan):
            self._finish_probe(session_id)
            return ""
        self._os_step[session_id] = step
        return plan[step]

    def _store_os(self, session_id: domain.SessionId, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        stored = self._catalog.get(session_id)
        if stored is not None and stored.os_name != cleaned:
            self._catalog.save(replace(stored, os_name=cleaned))
        with self._lock:
            current = self._sessions.get(session_id)
            if current is not None and current.os_name != cleaned:
                self._sessions[session_id] = replace(current, os_name=cleaned)

    def _begin_ip_probe(self, session_id: domain.SessionId, os_name: str) -> str:
        plan = _ip_commands(os_name)
        if not plan:
            self._finish_probe(session_id)
            return ""
        self._os_phase[session_id] = "ip"
        self._ip_plan[session_id] = plan
        self._os_step[session_id] = 0
        return plan[0]

    def _next_ip_command(self, session_id: domain.SessionId) -> str:
        plan = self._ip_plan.get(session_id, ())
        step = self._os_step.get(session_id, 0) + 1
        if step >= len(plan):
            return self._begin_user_probe(session_id)
        self._os_step[session_id] = step
        return plan[step]

    def _probe_identity(self, session_id: domain.SessionId, info: domain.SessionInfo) -> None:
        """IP и username, если ОС уже есть, а эти поля ещё пустые."""
        if session_id in self._os_probe:
            return
        if self.session_state(session_id) != "ready":
            return
        cleaned_ips = _clean_stored_addresses(info.ip_addresses)
        if cleaned_ips != info.ip_addresses.strip():
            self._store_ips(session_id, [part for part in cleaned_ips.split(", ") if part])
            fresh = self.get_session(session_id)
            if fresh is not None:
                info = fresh
        if not info.ip_addresses.strip():
            command = self._begin_ip_probe(session_id, info.os_name)
        elif not info.username.strip():
            command = self._begin_user_probe(session_id)
        else:
            return
        if not command:
            return
        try:
            self.send_command(session_id, command, os_probe=True)
        except Exception:
            self._finish_probe(session_id)

    def _begin_user_probe(self, session_id: domain.SessionId) -> str:
        info = self.get_session(session_id)
        if info is None or info.username.strip():
            self._finish_probe(session_id)
            return ""
        plan = _user_commands(info)
        if not plan:
            self._finish_probe(session_id)
            return ""
        self._os_phase[session_id] = "user"
        self._ip_plan[session_id] = plan
        self._os_step[session_id] = 0
        return plan[0]

    def _next_user_command(self, session_id: domain.SessionId) -> str:
        plan = self._ip_plan.get(session_id, ())
        step = self._os_step.get(session_id, 0) + 1
        if step >= len(plan):
            info = self.get_session(session_id)
            fallback = ""
            if info is not None and info.ssh is not None:
                fallback = info.ssh.username.strip()
            if fallback:
                self._store_username(session_id, fallback)
            self._finish_probe(session_id)
            return ""
        self._os_step[session_id] = step
        return plan[step]

    def _finish_probe(self, session_id: domain.SessionId) -> None:
        self._os_step.pop(session_id, None)
        self._os_phase.pop(session_id, None)
        self._ip_plan.pop(session_id, None)

    def _store_ips(self, session_id: domain.SessionId, addresses: list) -> None:
        joined = ", ".join(part for part in addresses if part)
        info = self.get_session(session_id)
        if info is None:
            return
        if not joined and not info.ip_addresses:
            return
        description = _description_without_ip(info.mcp_description)
        if info.ip_addresses == joined and info.mcp_description == description:
            return
        updated = replace(info, ip_addresses=joined, mcp_description=description)
        self._catalog.save(updated)
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = updated

    def _store_username(self, session_id: domain.SessionId, name: str) -> None:
        cleaned = name.strip()
        if not cleaned:
            return
        info = self.get_session(session_id)
        if info is None or info.username == cleaned:
            return
        updated = replace(info, username=cleaned)
        self._catalog.save(updated)
        with self._lock:
            if session_id in self._sessions:
                self._sessions[session_id] = updated

    def reject_running_command(self, session_id: domain.SessionId, reason: str) -> None:
        """Отказ интерактивного ввода: причина модели и Ctrl+C в терминал."""
        text = reason.strip() or "отклонено"
        with self._lock:
            watch = self._mcp_watch.get(session_id)
            if watch is not None:
                watch.refusal = text
        try:
            self.write_input(session_id, "\x03")
        except Exception:
            pass
        self.cancel_command(session_id)
        with self._lock:
            watch = self._mcp_watch.get(session_id)
            if watch is not None:
                watch.done.set()

    def _check_ssh(
        self,
        provider: domain.ProviderType,
        ssh: Optional[domain.SSHConfig],
        auth_secret: Optional[str],
        hop_secrets: tuple[str, ...],
        install_key: tuple[bool, ...] = (),
    ) -> None:
        if ssh is None:
            raise ValueError("Не указаны параметры SSH-подключения")
        if not ssh.host.strip():
            raise ValueError("Адрес хоста не может быть пустым")
        if not ssh.username.strip():
            raise ValueError("Имя пользователя SSH не может быть пустым")
        if provider != domain.ProviderType.SSH_OVER_SSH:
            return
        if not ssh.hops:
            raise ValueError("В цепочке SSH нужно хотя бы два узла")
        for index, hop in enumerate(ssh.hops, start=2):
            if not hop.host.strip():
                raise ValueError(f"Укажите адрес узла {index}")
            if not hop.username.strip():
                raise ValueError(f"Укажите логин узла {index}")
        self._require_ssh_secret(
            1, ssh.auth_method, auth_secret, ssh.private_key_path, install_key, 0
        )
        if len(hop_secrets) != len(ssh.hops):
            raise ValueError("Не хватает данных входа для узла цепочки")
        for index, (hop, secret) in enumerate(zip(ssh.hops, hop_secrets), start=2):
            self._require_ssh_secret(
                index,
                hop.auth_method,
                secret,
                hop.private_key_path,
                install_key,
                index - 1,
            )

    @staticmethod
    def _require_ssh_secret(
        index: int,
        method: domain.SSHAuthMethod,
        secret: Optional[str],
        key_path: Optional[str],
        install_key: tuple[bool, ...] = (),
        install_index: int = 0,
    ) -> None:
        if method == domain.SSHAuthMethod.PRIVATE_KEY:
            if not (key_path or "").strip():
                raise ValueError(f"Укажите файл ключа узла {index}")
            return
        if method == domain.SSHAuthMethod.AGENT:
            return
        if install_index < len(install_key) and install_key[install_index]:
            return
        if not secret:
            raise ValueError(f"Введите пароль узла {index}")

    def _journal_os(self, session_id: domain.SessionId) -> str:
        if not self._journal.is_unlocked(session_id):
            return ""
        found = ""
        for event in self._journal.get_by_session(session_id):
            if event.event_type == domain.EventType.CHAT_OS:
                found = event.payload.strip()
        return found

    def _session_os(self, session_id: domain.SessionId) -> str:
        info = self.get_session(session_id)
        if info is not None and info.os_name.strip():
            return info.os_name.strip()
        return self._journal_os(session_id)

    def _os_fact(self, session_id: domain.SessionId) -> str:
        name = self._session_os(session_id)
        if name:
            return "ОС: " + name
        return "ОС не определена"

    def _mcp_facts(self, session_id: domain.SessionId) -> str:
        info = self.get_session(session_id)
        parts = [self._os_fact(session_id)]
        if info is not None and info.ip_addresses.strip():
            parts.append("IP: " + info.ip_addresses.strip())
        else:
            parts.append("IP не определены")
        if info is not None and info.username.strip():
            parts.append("Пользователь: " + info.username.strip())
        else:
            parts.append("Пользователь не определён")
        return ". ".join(parts)

    def session_operating_system(self, session_id: domain.SessionId) -> str:
        """Строка ОС из настроек сессии или из журнала, если поле ещё пустое."""
        return self._session_os(session_id)

    def foreground_session_id(self) -> str:
        with self._lock:
            return self._foreground or ""

    def _system_prompt(self, session_id: domain.SessionId) -> str:
        os_name = self._session_os(session_id)
        info = self.get_session(session_id)
        ip_line = ""
        user_line = ""
        if info is not None and info.ip_addresses.strip():
            ip_line = f"IP этой сессии: {info.ip_addresses.strip()}\n"
        else:
            ip_line = (
                "IP этой сессии не определены. Не выдумывай адреса.\n"
            )
        if info is not None and info.username.strip():
            user_line = f"Пользователь этой сессии: {info.username.strip()}\n"
        else:
            user_line = (
                "Имя пользователя этой сессии не определено. Не выдумывай логин.\n"
            )
        tail = (
            ip_line
            + user_line
            + "ОС, IP и имя пользователя заданы этой инструкцией и остаются после сброса контекста.\n"
        )
        if os_name:
            note = (
                f"\nОС этой сессии: {os_name}\n"
                "Команды и run_command пиши только для этой ОС. "
                "Не подменяй её Windows, Linux или другой системой. "
                "Блок для показа человеку по-прежнему один ```bash.\n"
                + tail
            )
        else:
            note = (
                "\nОС этой сессии не определена. "
                "Не угадывай Windows, Linux или другую систему и не выбирай команды наугад. "
                "Если команда зависит от ОС, сначала узнай её короткой командой через run_command "
                "или спроси пользователя.\n"
                + tail
            )
        return _CHAT_SYSTEM_PROMPT + note

    def _active_chat(self, session_id: domain.SessionId) -> List[tuple]:
        messages: List[tuple] = []
        for event in self._journal.get_by_session(session_id):
            if event.event_type == domain.EventType.CHAT_RESET:
                messages.clear()
            elif event.event_type == domain.EventType.CHAT_USER:
                messages.append(("user", event.payload))
            elif event.event_type == domain.EventType.CHAT_ASSISTANT:
                messages.append(("assistant", event.payload))
        return messages

    def list_lounge_tabs(self) -> List[domain.LoungeTab]:
        with self._lock:
            return list(self._lounge_tabs)

    def lounge_messages(self, tab_id: str) -> List[tuple]:
        tab = self._lounge_tab(tab_id)
        return list(tab.messages)

    def remember_lounge_command(self, tab_id: str, text: str) -> None:
        """Карточка вызова в ленте вкладки: команда, вывод и MCP. Модели не уходит."""
        cleaned = text.strip()
        if not cleaned:
            return
        self._remember_lounge(tab_id, "command", cleaned)

    def record_tool_outcome(
        self, session_id: domain.SessionId, text: str, lounge_tab: str = ""
    ) -> None:
        """Строка в переписке после карточки команды. В запрос модели не входит."""
        cleaned = text.strip()
        if not cleaned:
            return
        if lounge_tab:
            self._remember_lounge(lounge_tab, "result", cleaned)
            return
        if not self._journal.is_unlocked(session_id):
            return
        self._append_chat(session_id, domain.EventType.CHAT_TOOL, cleaned)

    def lounge_problem(self, tab_id: str) -> str:
        with self._lock:
            return self._llm_problem.get(lounge_origin(tab_id), "")

    def lounge_mcp_entries(self) -> List[tuple]:
        """Имя, описание и адрес MCP, которые сейчас подключены."""
        entries = []
        for info in self._lounge_targets():
            entries.append(
                (
                    info.mcp_alias.strip() or info.name,
                    info.mcp_description,
                    self.mcp_endpoint(info.session_id),
                )
            )
        return entries

    def open_lounge_tab(self) -> domain.LoungeTab:
        with self._lock:
            title = "чат" if not self._lounge_tabs else f"чат {len(self._lounge_tabs) + 1}"
            tab = domain.LoungeTab(tab_id=uuid4().hex, title=title)
            self._lounge_tabs.append(tab)
        self._save_lounge()
        return tab

    def rename_lounge_tab(self, tab_id: str, title: str) -> domain.LoungeTab:
        cleaned = " ".join(title.split())
        if not cleaned:
            raise ValueError("Имя чата не может быть пустым")
        return self._update_lounge(tab_id, title=cleaned)

    def choose_lounge_model(self, tab_id: str, profile_id: str) -> domain.LoungeTab:
        profile = self.get_llm_profile(profile_id)
        if profile is None:
            raise ValueError("Модель не найдена")
        tab = self._lounge_tab(tab_id)
        if tab.profile_id and tab.profile_id != profile_id:
            raise ValueError("Модель уже выбрана")
        return self._update_lounge(tab_id, profile_id=profile.profile_id)

    def close_lounge_tab(self, tab_id: str) -> None:
        self.cancel_lounge_chat(tab_id)
        with self._lock:
            if len(self._lounge_tabs) < 2:
                raise ValueError("Последний чат закрыть нельзя")
            kept = [tab for tab in self._lounge_tabs if tab.tab_id != tab_id]
            if len(kept) == len(self._lounge_tabs):
                raise ValueError("Чат не найден")
            self._lounge_tabs = kept
            self._llm_problem.pop(lounge_origin(tab_id), None)
        self._save_lounge()

    def send_lounge_chat(self, tab_id: str, text: str) -> None:
        """Пишет сообщение вкладки и запрашивает ответ модели с вызовом MCP."""
        message = text.strip()
        if not message:
            raise ValueError("Пустое сообщение")
        tab = self._lounge_tab(tab_id)
        if not tab.profile_id:
            raise ValueError("Сначала выберите модель")
        if self._llm_profiles is None:
            raise LlmUnavailable("Хранилище моделей не подключено")
        profile = self._llm_profiles.get(tab.profile_id)
        if profile is None:
            raise LlmUnavailable("Сохранённая модель не найдена")
        if self._llm is None:
            raise LlmUnavailable("Клиент модели не подключён")
        origin = lounge_origin(tab_id)
        self._remember_lounge(tab_id, "user", message)
        with self._lock:
            self._llm_problem.pop(origin, None)
        warning = ""
        stop = domain.ChatStop()
        with self._lock:
            self._chat_stops[origin] = stop
        token = domain.bind_chat_stop(stop)
        try:
            if getattr(self._llm, "complete_turn", None) is None:
                history = [("system", self._lounge_system())]
                history.extend(self._lounge_model_history(tab_id))
                answer = self._llm.complete(profile, history)
            else:
                targets = self._lounge_targets()
                names = []
                os_by_name = {}
                for info in targets:
                    label = info.mcp_alias.strip() or info.name
                    names.append(label)
                    os_by_name.setdefault(label, self._session_os(info.session_id))
                answer, warning = self._complete_with_tools(
                    "",
                    profile,
                    history=self._lounge_model_history(tab_id),
                    tools=lounge_tools(names, os_by_name),
                    note=lambda kind, payload: self._remember_lounge(
                        tab_id,
                        "assistant" if kind == domain.EventType.CHAT_ASSISTANT else "user",
                        payload,
                    ),
                    dispatch=lambda call: self._dispatch_lounge_tool(tab_id, call),
                    system=self._lounge_system(),
                )
        except domain.ChatStopped:
            with self._lock:
                self._llm_problem.pop(origin, None)
            return
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            with self._lock:
                self._llm_problem[origin] = reason
            raise LlmUnavailable(reason) from exc
        else:
            with self._lock:
                if warning:
                    self._llm_problem[origin] = warning
                else:
                    self._llm_problem.pop(origin, None)
            self._remember_lounge(tab_id, "assistant", answer)
        finally:
            domain.reset_chat_stop(token)
            with self._lock:
                if self._chat_stops.get(origin) is stop:
                    self._chat_stops.pop(origin, None)
                self._lounge_command.pop(origin, None)

    def _load_lounge(self) -> None:
        tabs = []
        if self._lounge_store is not None:
            tabs = list(self._lounge_store.load())
        if not tabs:
            tabs = [domain.LoungeTab(tab_id=uuid4().hex, title="чат")]
        self._lounge_tabs = tabs
        self._save_lounge()

    def _save_lounge(self) -> None:
        if self._lounge_store is None:
            return
        with self._lock:
            tabs = list(self._lounge_tabs)
        self._lounge_store.save(tabs)

    def _lounge_tab(self, tab_id: str) -> domain.LoungeTab:
        with self._lock:
            for tab in self._lounge_tabs:
                if tab.tab_id == tab_id:
                    return tab
        raise ValueError("Чат не найден")

    def _update_lounge(self, tab_id: str, **changes) -> domain.LoungeTab:
        with self._lock:
            updated = None
            tabs = []
            for tab in self._lounge_tabs:
                if tab.tab_id == tab_id:
                    updated = replace(tab, **changes)
                    tabs.append(updated)
                else:
                    tabs.append(tab)
            if updated is None:
                raise ValueError("Чат не найден")
            self._lounge_tabs = tabs
        self._save_lounge()
        return updated

    def _remember_lounge(self, tab_id: str, role: str, text: str) -> None:
        tab = self._lounge_tab(tab_id)
        self._update_lounge(tab_id, messages=tab.messages + ((role, text),))
        self._emit_lounge(tab_id, role, text)

    def _emit_lounge(self, tab_id: str, role: str, text: str) -> None:
        if role == "user":
            kind = domain.EventType.CHAT_USER
        elif role == "result":
            kind = domain.EventType.CHAT_TOOL
        elif role == "command":
            kind = domain.EventType.CHAT_COMMAND
        else:
            kind = domain.EventType.CHAT_ASSISTANT
        event = domain.TranscriptEvent(
            event_id=domain.new_event_id(),
            session_id=lounge_origin(tab_id),
            sequence=0,
            event_type=kind,
            timestamp=datetime.now(),
            stream_type=domain.StreamType.SYSTEM,
            payload=text,
            persist=False,
        )
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass

    def _lounge_targets(self) -> List[domain.SessionInfo]:
        found = []
        for info in self.list_sessions():
            if self.session_state(info.session_id) not in ("ready", "running"):
                continue
            if not self.mcp_endpoint(info.session_id):
                continue
            found.append(info)
        return found

    def _lounge_model_history(self, tab_id: str) -> List[tuple]:
        """Переписка для модели: цитата MCP раскрывается в имя, описание и ОС."""
        history = []
        for role, text in self._lounge_tab(tab_id).messages:
            if role in ("result", "command"):
                continue
            if role == "user":
                history.append((role, self._expand_mcp_cite(text)))
            else:
                history.append((role, text))
        return history

    def _expand_mcp_cite(self, text: str) -> str:
        def replace_cite(match: re.Match) -> str:
            name = " ".join(match.group(1).split())
            info = self._mcp_by_label(name)
            if info is None:
                return f"Подключи MCP «{name}». ОС не определена."
            label = info.mcp_alias.strip() or info.name
            sentence = f"Подключи MCP «{label}»."
            description = info.mcp_description.strip()
            if description:
                sentence += f" Описание: {description}."
            sentence += " " + self._mcp_facts(info.session_id) + "."
            return sentence

        return re.sub(r"«MCP:\s*([^»]+)»", replace_cite, text)

    def _mcp_by_label(self, name: str) -> Optional[domain.SessionInfo]:
        for info in self.list_sessions():
            label = info.mcp_alias.strip() or info.name
            if name == label or name == info.name or name == info.session_id:
                return info
        return None

    def _lounge_system(self) -> str:
        lines = []
        for info in self._lounge_targets():
            label = info.mcp_alias.strip() or info.name
            details = []
            if info.mcp_description.strip():
                details.append(info.mcp_description.strip())
            details.append(self._mcp_facts(info.session_id))
            lines.append(f"- {label}: {'. '.join(details)}")
        catalog = "\n".join(lines) if lines else "- нет подключённых MCP"
        return _LOUNGE_SYSTEM_PROMPT + "\nПодключённые MCP:\n" + catalog + "\n"

    def _dispatch_lounge_tool(self, tab_id: str, call: object) -> str:
        name = str(getattr(call, "name", "") or "")
        arguments = getattr(call, "arguments", {}) or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return refusal_reason("не удалось прочитать аргументы")
        if name != TOOL_NAME or not isinstance(arguments, dict):
            raise ConnectionError(
                "Модель вызвала инструмент "
                f"«{name or 'без имени'}», а не {TOOL_NAME}. "
                "Окно подтверждения команды не открывалось."
            )
        command = command_text(arguments)
        if not command:
            raise ConnectionError(
                "Модель вызвала run_command без текста команды. "
                "Окно подтверждения не открывалось."
            )
        target = str(arguments.get("target") or "").strip()
        wanted = " ".join(target.split())
        matches = []
        for info in self._lounge_targets():
            label = info.mcp_alias.strip() or info.name
            if wanted and wanted in (label, info.name, info.session_id):
                matches.append(info.session_id)
        if not wanted:
            return refusal_reason("не указан MCP")
        if not matches:
            return refusal_reason(f"MCP «{wanted}» не подключён")
        if len(matches) > 1:
            return refusal_reason(f"несколько MCP с именем «{wanted}»")
        session_id = matches[0]
        origin = lounge_origin(tab_id)
        with self._lock:
            self._lounge_command[origin] = session_id
        return self.run_mcp_command(
            session_id,
            str(arguments.get("intent") or ""),
            command,
            origin_id=origin,
        )

    def _append_chat(
        self,
        session_id: domain.SessionId,
        event_type: domain.EventType,
        payload: str,
    ) -> None:
        self._handle_driver_event(
            domain.OutputEvent(
                session_id=session_id,
                event_type=event_type,
                timestamp=datetime.now(),
                stream_type=domain.StreamType.SYSTEM,
                payload=payload,
                persist=True,
            )
        )


def _ip_commands(os_name: str) -> tuple:
    if "windows" in os_name.lower():
        return ("ipconfig",)
    return ("ip addr", "ifconfig", "hostname -I")


def _user_commands(info: domain.SessionInfo) -> tuple:
    os_name = info.os_name.lower()
    if "windows" in os_name or (
        info.provider == domain.ProviderType.CMD and "linux" not in os_name
    ):
        return ("whoami", "echo %USERNAME%")
    return ("whoami", "id -un")


def _clean_username(raw: str) -> str:
    for line in raw.replace("\r", "").split("\n"):
        text = line.strip()
        if not text or len(text) > 64 or " " in text:
            continue
        low = text.lower()
        if low in {"whoami", "id", "id-un", "echo", "%username%"}:
            continue
        if any(
            hint in low
            for hint in ("windows", "linux", "microsoft", "not found", "не является")
        ):
            continue
        if not re.fullmatch(r"[\w.\\@-]{1,64}", text):
            continue
        if text.count(".") == 3 and all(part.isdigit() for part in text.split(".")):
            continue
        return text
    return ""


def _description_without_ip(text: str) -> str:
    kept = [line for line in text.splitlines() if not line.strip().lower().startswith("ip:")]
    return "\n".join(kept).strip()


_MAC = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
_INET = re.compile(r"(?i)\binet6?(?:\s+addr)?[:\s]+([0-9A-Fa-f:.%]+)")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_IPV6 = re.compile(
    r"(?<![:0-9A-Fa-f.])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![:0-9A-Fa-f])"
)
_HOST_LINE = re.compile(r"(?i)ipv[46]|ip[\s-]*(?:address|адрес)")
_SKIP_LINE = re.compile(
    r"(?i)(?:subnet\s*mask|netmask|маска|physical\s*address|физическ|"
    r"default\s*gateway|основн\w*\s+шлюз|\bgateway\b|"
    r"\bdns\b|\bdhcp\b|link/ether|\bether\b|\bhwaddr\b)"
)


def _is_prefix_mask(address: str) -> bool:
    """255.255.255.0 и другие маски: единицы, затем нули."""
    try:
        value = int(ipaddress.IPv4Address(address))
    except ValueError:
        return False
    if value in (0, 0xFFFFFFFF):
        return True
    return "01" not in f"{value:032b}"


def _valid_address(token: str) -> str:
    raw = token.strip().strip("[]").split("%", 1)[0].split("/", 1)[0]
    if not raw or _MAC.fullmatch(raw):
        return ""
    try:
        parsed = ipaddress.ip_address(raw)
    except ValueError:
        return ""
    if parsed.is_unspecified:
        return ""
    if isinstance(parsed, ipaddress.IPv4Address):
        if int(parsed) == 0xFFFFFFFF or _is_prefix_mask(str(parsed)):
            return ""
        return str(parsed)
    return parsed.compressed


def _clean_stored_addresses(text: str) -> str:
    """Убрать из уже сохранённой строки маски и MAC, оставить адреса."""
    found = []
    seen = set()
    for token in re.split(r"[\s,;]+", text.strip()):
        address = _valid_address(token)
        if address and address not in seen:
            seen.add(address)
            found.append(address)
    return ", ".join(found)


def _collect_addresses(raw: str) -> list:
    """Адреса самой машины из ipconfig, ip addr, ifconfig или hostname -I.

    Маски, MAC, шлюз и DNS не попадают в список. Широковещательный адрес
    на строке inet тоже пропускается: берётся только адрес интерфейса.
    """
    text = _MAC.sub(" ", raw.replace("\r", ""))
    found: list = []
    seen = set()
    labeled = False

    def add(token: str) -> None:
        address = _valid_address(token)
        if address and address not in seen:
            seen.add(address)
            found.append(address)

    for line in text.splitlines():
        inet_hits = list(_INET.finditer(line))
        if inet_hits:
            labeled = True
            for hit in inet_hits:
                add(hit.group(1))
            continue
        if _SKIP_LINE.search(line):
            labeled = True
            continue
        if _HOST_LINE.search(line):
            labeled = True
            for hit in _IPV4.finditer(line):
                add(hit.group(1))
            for hit in _IPV6.finditer(line):
                add(hit.group(1))
    if labeled:
        return found
    for hit in _IPV4.finditer(text):
        add(hit.group(1))
    for hit in _IPV6.finditer(text):
        add(hit.group(1))
    return found


def _clean_os_output(raw: str) -> str:
    """Оставить строку с названием ОС из вывода ver или uname."""
    lines = []
    for line in raw.replace("\r", "").splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned in {"ver", "uname -srm", "cmd /c ver"}:
            continue
        if cleaned.startswith("__VIBE"):
            continue
        lines.append(cleaned)
    if not lines:
        return ""
    hints = (
        "windows", "linux", "darwin", "freebsd", "ubuntu", "debian",
        "fedora", "alpine", "centos", "opensuse", "arch",
    )
    for line in lines:
        low = line.lower()
        if any(hint in low for hint in hints):
            return line[:240]
    return lines[-1][:240]


class _McpWatch:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.command_id: Optional[domain.CommandId] = None
        self.output = ""
        self.live = ""
        self.code: Optional[int] = None
        self.interactive = False
        self.shell = False
        self.ignore_shell = False
        self.refusal = ""


# Сколько команд модель может выполнить за один ответ пользователю.
# Дальше ход не рвётся: модель дописывает текст без новых вызовов.
_TOOL_ROUNDS = 24


def _join_cut(parts: List[str], tail: str) -> str:
    chunks = [part.strip() for part in parts if part and part.strip()]
    text = tail.strip()
    if text and (not chunks or text != chunks[-1]):
        chunks.append(text)
    return "\n".join(chunks)


def _last_assistant_text(messages: list) -> str:
    for item in reversed(messages):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        text = str(item.get("content") or "").strip()
        if text:
            return text
    return ""


def _calls_without_command(calls: list) -> bool:
    return any(
        not command_text(getattr(call, "arguments", {}) or {})
        for call in calls
    )


def _fresh_calls(calls: list, executed: set) -> list:
    fresh = []
    for call in calls:
        arguments = getattr(call, "arguments", {}) or {}
        if not isinstance(arguments, dict):
            arguments = {}
        command = command_text(arguments)
        target = str(arguments.get("target") or "").strip()
        key = f"{target}\0{command}"
        if not command or key in executed:
            continue
        executed.add(key)
        fresh.append(call)
    return fresh


_ACTION_PROMISE = re.compile(
    r"(?:"
    r"сейчас\s+\S{0,24}(?:провер|посмотр|выполн|запущ|узна|глян|вызов)"
    r"|щас\s+\S{0,24}(?:провер|посмотр|выполн|запущ|узна)"
    r"|(?:^|[\s.!?])(?<!не )(?<!Не )(?:проверю|посмотрю|выполню|запущу|вызываю)\b"
    r"|let me (?:check|look|run|execute)"
    r"|i['’]ll (?:check|look|run|execute)"
    r"|i will (?:check|look|run|execute)"
    r")",
    re.IGNORECASE,
)

_MISSED_TOOL = (
    "Модель написала, что проверит, но не вызвала инструмент. "
    "Окно подтверждения не открывалось."
)


def _promises_action(text: str) -> bool:
    return _ACTION_PROMISE.search(text or "") is not None


def _unfinished_tool_error(finish: str) -> str:
    if finish == "length":
        return (
            "Ответ оборвался на вызове инструмента. "
            "Команда не запускалась, окно подтверждения не открывалось."
        )
    return (
        "Модель завершила ход вызовом инструмента, но команда не разобрана. "
        "Окно подтверждения не открывалось."
    )


def _tools_rejected(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "tool" in text and ("http 4" in text or "unsupported" in text or "unknown" in text)


def _tool_call_payload(call: object) -> dict:
    arguments = getattr(call, "arguments", {}) or {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": str(getattr(call, "id", "") or "call"),
        "type": "function",
        "function": {
            "name": str(getattr(call, "name", "") or ""),
            "arguments": arguments,
        },
    }


def _approx_tokens(text: str) -> int:
    """Грубая оценка: кириллица плотнее в токенах, чем латиница."""
    if not text:
        return 0
    cyrillic = sum(1 for char in text if "\u0400" <= char <= "\u04ff")
    other = len(text) - cyrillic
    return max(1, (cyrillic + 1) // 2 + (other + 3) // 4)