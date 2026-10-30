# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Доменные модели и состояния."""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, List, Optional
from uuid import uuid4


SessionId = str
CommandId = str
EventId = str
CredentialRef = str
ProfileId = str

# Как делить текст команды в запросе. В терминал уходит исходная строка.
SHELL_UNIX = "unix"
SHELL_WINDOWS = "windows"
SHELL_OTHER = "other"


def normalize_shell_family(value: str) -> str:
    """linux/macOS, windows или other. Неизвестное значение — linux/macOS."""
    text = (value or "").strip().lower().replace(" ", "")
    if text in {SHELL_WINDOWS, "win", "cmd"}:
        return SHELL_WINDOWS
    if text in {SHELL_OTHER, "другая"}:
        return SHELL_OTHER
    return SHELL_UNIX


def new_session_id() -> SessionId:
    return f"s-{uuid4().hex}"


def new_command_id() -> CommandId:
    return f"c-{uuid4().hex}"


def new_event_id() -> EventId:
    return f"e-{uuid4().hex}"


def new_profile_id() -> ProfileId:
    return f"m-{uuid4().hex}"


class ChatStopped(Exception):
    """Пользователь остановил ответ модели. Это не сбой соединения."""


class ChatStop:
    """Флаг остановки одного ответа. Драйвер вешает на него закрытие сокета."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flag = False
        self._abort: Optional[Callable[[], None]] = None

    def cancel(self) -> None:
        with self._lock:
            self._flag = True
            abort = self._abort
        if abort is not None:
            abort()

    def cancelled(self) -> bool:
        with self._lock:
            return self._flag

    def attach(self, abort: Callable[[], None]) -> None:
        with self._lock:
            self._abort = abort
            fire = self._flag
        if fire:
            abort()

    def detach(self) -> None:
        with self._lock:
            self._abort = None


_chat_stop: contextvars.ContextVar[Optional[ChatStop]] = contextvars.ContextVar(
    "vterminal_chat_stop",
    default=None,
)


def bind_chat_stop(stop: Optional[ChatStop]) -> contextvars.Token:
    return _chat_stop.set(stop)


def reset_chat_stop(token: contextvars.Token) -> None:
    _chat_stop.reset(token)


def current_chat_stop() -> Optional[ChatStop]:
    return _chat_stop.get()


class ProviderType(str, Enum):
    CMD = "cmd"
    SSH = "ssh"
    SSH_OVER_SSH = "ssh_over_ssh"


class LlmApiKind(str, Enum):
    """Вид HTTP API модели. Класс клиента по этому значению выбирает main.py."""

    OPENAI = "openai"
    LM_STUDIO = "lm_studio"
    OPENROUTER = "openrouter"


@dataclass(frozen=True)
class LlmApiSpec:
    kind: LlmApiKind
    label: str
    base_url: str
    key_required: bool
    model_hint: str


LLM_API_SPECS: tuple[LlmApiSpec, ...] = (
    LlmApiSpec(
        LlmApiKind.OPENAI,
        "OpenAI",
        "https://api.openai.com/v1",
        True,
        "например gpt-4.1",
    ),
    LlmApiSpec(
        LlmApiKind.LM_STUDIO,
        "LM Studio",
        "http://127.0.0.1:1234/v1",
        False,
        "id из LM Studio",
    ),
    LlmApiSpec(
        LlmApiKind.OPENROUTER,
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        True,
        "vendor/model",
    ),
)


def llm_api_spec(kind: LlmApiKind) -> LlmApiSpec:
    for spec in LLM_API_SPECS:
        if spec.kind == kind:
            return spec
    raise ValueError(f"Неизвестный вид API: {kind}")


class SessionStatus(str, Enum):
    CREATING = "creating"
    READY = "ready"
    RUNNING = "running"
    CLOSED = "closed"
    ERROR = "error"


class ExecutionIdentityType(str, Enum):
    CURRENT_USER = "current_user"
    ELEVATED = "elevated"


class StreamType(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"
    SYSTEM = "system"


class EventType(str, Enum):
    COMMAND_SENT = "command_sent"
    OUTPUT_CHUNK = "output_chunk"
    COMMAND_FINISHED = "command_finished"
    ERROR = "error"
    SESSION_STATUS = "session_status"
    CHAT_USER = "chat_user"
    CHAT_ASSISTANT = "chat_assistant"
    CHAT_RESET = "chat_reset"
    # Одна строка с ОС сессии. В старых чатах события нет — промпт без ОС.
    CHAT_OS = "chat_os"
    # Заметка человеку после карточки команды. Модели не отправляется.
    CHAT_TOOL = "chat_tool"
    # Нижняя строка ввода теперь пишет в эту оболочку. Пустой payload — снова сессия.
    SHELL_CONTEXT = "shell_context"


class SSHAuthMethod(str, Enum):
    PASSWORD = "password"
    PRIVATE_KEY = "private_key"
    AGENT = "agent"


@dataclass(frozen=True)
class SSHHop:
    """Следующий узел цепочки. Пароль сюда не входит и не сохраняется."""

    host: str
    port: int = 22
    username: str = ""
    auth_method: SSHAuthMethod = SSHAuthMethod.PASSWORD
    private_key_path: Optional[str] = None


@dataclass(frozen=True)
class SSHConfig:
    host: str
    port: int = 22
    username: str = ""
    auth_method: SSHAuthMethod = SSHAuthMethod.PASSWORD
    private_key_path: Optional[str] = None
    connect_timeout: float = 15.0
    # Узлы после первого. Первый — поля выше.
    hops: tuple[SSHHop, ...] = ()


def ssh_targets(ssh: SSHConfig) -> tuple[SSHConfig, ...]:
    """Первый узел и каждый следующий как отдельное SSH-подключение."""
    first = SSHConfig(
        host=ssh.host,
        port=ssh.port,
        username=ssh.username,
        auth_method=ssh.auth_method,
        private_key_path=ssh.private_key_path,
        connect_timeout=ssh.connect_timeout,
    )
    rest = tuple(
        SSHConfig(
            host=hop.host,
            port=hop.port,
            username=hop.username,
            auth_method=hop.auth_method,
            private_key_path=hop.private_key_path,
            connect_timeout=ssh.connect_timeout,
        )
        for hop in ssh.hops
    )
    return (first, *rest)


def ssh_location(ssh: SSHConfig) -> str:
    parts = [f"{ssh.username}@{ssh.host}:{ssh.port}"]
    parts.extend(f"{hop.username}@{hop.host}:{hop.port}" for hop in ssh.hops)
    return " → ".join(parts)


def ssh_config_from_targets(targets: tuple[SSHConfig, ...]) -> SSHConfig:
    """Собрать цепочку обратно после того, как у узлов сменился способ входа."""
    if not targets:
        raise ValueError("Нет узлов SSH")
    first, *rest = targets
    hops = tuple(
        SSHHop(
            host=item.host,
            port=item.port,
            username=item.username,
            auth_method=item.auth_method,
            private_key_path=item.private_key_path,
        )
        for item in rest
    )
    return SSHConfig(
        host=first.host,
        port=first.port,
        username=first.username,
        auth_method=first.auth_method,
        private_key_path=first.private_key_path,
        connect_timeout=first.connect_timeout,
        hops=hops,
    )


@dataclass(frozen=True)
class SessionConfig:
    session_id: SessionId
    name: str
    provider: ProviderType = ProviderType.CMD
    identity: ExecutionIdentityType = ExecutionIdentityType.CURRENT_USER
    ssh: Optional[SSHConfig] = None
    # Транзиентный секрет (пароль SSH или passphrase). Нигде не сохраняется.
    auth_secret: Optional[str] = None
    # Пароли узлов после первого. Тоже не сохраняются.
    hop_secrets: tuple[str, ...] = ()
    # Галочки «записать мой ключ» по узлам. В каталог не попадают.
    install_key: tuple[bool, ...] = ()
    # Путь из настроек. Пустой — первичная настройка ключа не сделана.
    identity_key_path: Optional[str] = None
    # Passphrase ключа из настроек. Не сохраняется: это мастер-пароль запуска.
    identity_passphrase: Optional[str] = None


@dataclass(frozen=True)
class SessionInfo:
    session_id: SessionId
    name: str
    provider: ProviderType
    identity: ExecutionIdentityType
    status: SessionStatus
    ssh: Optional[SSHConfig] = None
    llm_profile_id: Optional[ProfileId] = None
    # Человеческое имя MCP. Пустое — в списке показывается name.
    mcp_alias: str = ""
    mcp_description: str = ""
    os_name: str = ""
    # Адреса машины и имя пользователя. Не часть описания.
    ip_addresses: str = ""
    username: str = ""
    # ОС, выбранная при создании: от неё зависят разделители команд в запросе.
    shell_family: str = SHELL_UNIX


@dataclass(frozen=True)
class ConnectionGroup:
    """Папка в дереве подключений. Пустой parent_id — группа в корне."""

    group_id: str
    name: str
    parent_id: str = ""
    expanded: bool = False


@dataclass(frozen=True)
class ConnectionLayout:
    """Группы и порядок детей. Пустой ключ порядка — корень."""

    groups: tuple[ConnectionGroup, ...] = ()
    order: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def group(self, group_id: str) -> Optional[ConnectionGroup]:
        for item in self.groups:
            if item.group_id == group_id:
                return item
        return None

    def children_of(self, parent_id: str) -> tuple[str, ...]:
        for parent, children in self.order:
            if parent == parent_id:
                return children
        return ()


@dataclass(frozen=True)
class LoungeTab:
    """Вкладка общего чата. Сообщения — пары role, text."""

    tab_id: str
    title: str
    profile_id: str = ""
    messages: tuple[tuple[str, str], ...] = ()


DEFAULT_CONTEXT_LIMIT = 8192


@dataclass(frozen=True)
class LlmToolCall:
    id: str
    name: str
    arguments: dict
    # arguments не разобрались: вызов оборван и выполнять его нельзя.
    broken: bool = False


@dataclass(frozen=True)
class LlmTurn:
    content: str
    tool_calls: List[LlmToolCall]
    # stop — ответ закончен, tool_calls — нужны инструменты,
    # length — сервер оборвал генерацию по лимиту длины.
    finish_reason: str = "stop"


@dataclass(frozen=True)
class LlmProfile:
    """Параметры одной модели. На сессию приходится один профиль."""

    profile_id: ProfileId
    name: str
    base_url: str
    model_id: str
    api_key: str = ""
    context_limit: int = DEFAULT_CONTEXT_LIMIT
    api_kind: LlmApiKind = LlmApiKind.OPENAI


@dataclass(frozen=True)
class CommandRequest:
    command_id: CommandId
    session_id: SessionId
    text: str


@dataclass(frozen=True)
class OutputEvent:
    session_id: SessionId
    event_type: EventType
    timestamp: datetime
    command_id: Optional[CommandId] = None
    stream_type: Optional[StreamType] = None
    payload: str = ""
    return_code: Optional[int] = None
    status: Optional[SessionStatus] = None
    # False — живой кадр терминала: подписчикам отдать, в журнал не писать.
    persist: bool = True
    # Размер экрана в момент этого кадра. Журнал его не хранит.
    columns: Optional[int] = None
    rows: Optional[int] = None
    alternate: bool = False


@dataclass(frozen=True)
class TranscriptEvent:
    event_id: EventId
    session_id: SessionId
    sequence: int
    event_type: EventType
    timestamp: datetime
    command_id: Optional[CommandId] = None
    stream_type: Optional[StreamType] = None
    payload: str = ""
    return_code: Optional[int] = None
    status: Optional[SessionStatus] = None
    persist: bool = True
    columns: Optional[int] = None
    rows: Optional[int] = None
    alternate: bool = False


@dataclass(frozen=True)
class CommandResult:
    command_id: CommandId
    session_id: SessionId
    return_code: Optional[int]
    status: SessionStatus


# --- Проверка ключа хоста -------------------------------------------------

@dataclass(frozen=True)
class HostKeyPrompt:
    host: str
    port: int
    key_type: str
    fingerprint: str
    is_known: bool
    is_changed: bool
    old_fingerprint: Optional[str] = None


@dataclass(frozen=True)
class HostKeyDecision:
    accept: bool
    remember: bool = True


# --- Ошибки ---------------------------------------------------------------

class JournalError(Exception): ...
class JournalAuthError(JournalError): ...
class JournalCorruptedError(JournalError): ...
class JournalNotFoundError(JournalError): ...
class JournalAlreadyExistsError(JournalError): ...
class JournalLockedError(JournalError): ...


class LlmError(Exception):
    """Модель не ответила. Терминал сессии при этом не закрывается."""