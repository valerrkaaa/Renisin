# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""MCP-команда терминала: подтверждение, отказ, интерактив и HTTP."""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.service import ApplicationService, LlmUnavailable, _CHAT_SYSTEM_PROMPT
from application.terminal_mcp import (
    INTERACTIVE_REASON,
    CommandAnswer,
    lounge_origin,
    promote_shell_actions,
    refusal,
    refusal_reason,
    split_shell_commands,
)
from drivers.llm import LlmToolCall, LlmTurn, LmStudioClient
from mcp_host import TerminalMcpServer, parse_mcp_host, parse_mcp_ports
from storage.command_allow import FileCommandAllowStore
from storage.mcp_settings import FileMcpSettings


class _Journal:
    def __init__(self) -> None:
        self.events = []
        self.unlocked = set()
        self.next = 0

    def append(self, event: domain.TranscriptEvent) -> None:
        if event.sequence != self.next:
            raise domain.JournalCorruptedError(
                f"ждали {self.next}, пришло {event.sequence}"
            )
        self.events.append(event)
        self.next += 1

    def next_sequence(self, session_id: str) -> int:
        return self.next

    def create(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def delete(self, session_id: str) -> None:
        return None

    def lock(self, session_id: str) -> None:
        self.unlocked.discard(session_id)

    def unlock(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def is_unlocked(self, session_id: str) -> bool:
        return session_id in self.unlocked

    def get_by_session(self, session_id: str):
        return [event for event in self.events if event.session_id == session_id]


class _Catalog:
    def __init__(self) -> None:
        self.items = {}

    def list(self):
        return list(self.items.values())

    def get(self, session_id: str):
        return self.items.get(session_id)

    def save(self, info: domain.SessionInfo) -> None:
        self.items[info.session_id] = info

    def delete(self, session_id: str) -> None:
        self.items.pop(session_id, None)


class _Allow:
    def __init__(self) -> None:
        self.items: dict = {}

    def allows(self, session_id: str, command: str) -> bool:
        return command in self.items.get(session_id, [])

    def remember(self, session_id: str, command: str) -> None:
        bucket = self.items.setdefault(session_id, [])
        if command not in bucket:
            bucket.append(command)


class _Driver:
    def __init__(self) -> None:
        self.sent = []
        self.os_output = "a.txt"
        self.shell_for = ""
        self.cancelled = threading.Event()

    def set_event_sink(self, sink) -> None:
        self.sink = sink

    def open(self, config: domain.SessionConfig) -> str:
        return config.session_id

    def send(self, request: domain.CommandRequest) -> None:
        self.sent.append(request.text)
        now = datetime.now()
        self.sink(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_SENT,
            timestamp=now,
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload=request.text,
        ))
        if request.text.strip() == "nano":
            self.sink(domain.OutputEvent(
                session_id=request.session_id,
                event_type=domain.EventType.OUTPUT_CHUNK,
                timestamp=now,
                command_id=request.command_id,
                stream_type=domain.StreamType.STDOUT,
                payload="nano",
                persist=False,
                alternate=True,
            ))
            self.cancelled.wait(2)
        elif request.text.strip() == self.shell_for:
            self.sink(domain.OutputEvent(
                session_id=request.session_id,
                event_type=domain.EventType.SHELL_CONTEXT,
                timestamp=now,
                stream_type=domain.StreamType.SYSTEM,
                payload="shell",
                persist=False,
            ))
        elif self.os_output and request.text.strip() != "exit":
            self.sink(domain.OutputEvent(
                session_id=request.session_id,
                event_type=domain.EventType.OUTPUT_CHUNK,
                timestamp=now,
                command_id=request.command_id,
                stream_type=domain.StreamType.STDOUT,
                payload=self.os_output,
                persist=True,
            ))
        self.sink(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_FINISHED,
            timestamp=now,
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload="",
            return_code=0,
        ))

    def cancel(self, session_id: str) -> None:
        self.cancelled.set()

    def close(self) -> None:
        return None

    def complete(self, session_id: str, text: str):
        return []

    def write_input(self, session_id: str, data: str) -> None:
        return None

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        return None


class _Profiles:
    def __init__(self) -> None:
        self.items = {}

    def list(self):
        return list(self.items.values())

    def get(self, profile_id: str):
        return self.items.get(profile_id)

    def save(self, profile: domain.LlmProfile) -> None:
        self.items[profile.profile_id] = profile

    def delete(self, profile_id: str) -> None:
        self.items.pop(profile_id, None)


class _Gate:
    def __init__(self, answer: CommandAnswer) -> None:
        self.answer = answer
        self.asks = []

    def ask(self, prompt):
        self.asks.append(prompt)
        return self.answer


def _world(driver: _Driver | None = None, allow=None):
    driver = driver or _Driver()
    profiles = _Profiles()
    service = ApplicationService(
        lambda _provider: driver,
        _Catalog(),
        _Journal(),
        llm_profiles=profiles,
        llm_client=_Client(),
        command_allow=allow if allow is not None else _Allow(),
    )
    return service, driver, profiles


class _Client:
    def list_models(self, profile: domain.LlmProfile):
        return ["demo"]

    def complete(self, profile: domain.LlmProfile, messages):
        return "ответ"


def _session(service: ApplicationService, profiles: _Profiles):
    profiles.save(domain.LlmProfile(
        "m-1", "локальная", "http://127.0.0.1:9/v1", "demo"
    ))
    return service.create_session("работа", "pw", llm_profile_id="m-1")


class PromptTests(unittest.TestCase):
    def test_prompt_offers_shown_code_or_mcp_until_done(self) -> None:
        self.assertIn("обычный текст", _CHAT_SYSTEM_PROMPT)
        self.assertIn("```bash", _CHAT_SYSTEM_PROMPT)
        self.assertIn("run_command", _CHAT_SYSTEM_PROMPT)
        self.assertIn("пока не достигнешь результата", _CHAT_SYSTEM_PROMPT)
        self.assertIn("одна команда", _CHAT_SYSTEM_PROMPT)
        self.assertIn("OLD:", _CHAT_SYSTEM_PROMPT)

    def test_new_prompt_clears_the_previous_tool_error(self) -> None:
        service, _driver, profiles = _world()
        info = _session(service, profiles)
        service._llm_problem[info.session_id] = (
            "Модель завершила ход вызовом инструмента, но команда не разобрана. "
            "Окно подтверждения не открывалось."
        )
        seen = []

        def complete(profile, messages):
            seen.append(service.llm_problem(info.session_id))
            return "ок"

        service._llm.complete = complete
        service.send_chat(info.session_id, "ещё раз")
        self.assertEqual(seen, [""])
        self.assertEqual(service.llm_problem(info.session_id), "")

    def test_interactive_reject_interrupts_and_tells_the_model(self) -> None:
        service, driver, profiles = _world()
        info = _session(service, profiles)
        started = threading.Event()
        typed = []

        def send(request: domain.CommandRequest) -> None:
            started.set()
            now = datetime.now()
            driver.sink(
                domain.OutputEvent(
                    session_id=request.session_id,
                    event_type=domain.EventType.COMMAND_SENT,
                    timestamp=now,
                    command_id=request.command_id,
                    stream_type=domain.StreamType.SYSTEM,
                    payload=request.text,
                )
            )
            driver.cancelled.wait(2)
            driver.sink(
                domain.OutputEvent(
                    session_id=request.session_id,
                    event_type=domain.EventType.COMMAND_FINISHED,
                    timestamp=now,
                    command_id=request.command_id,
                    stream_type=domain.StreamType.SYSTEM,
                    payload="",
                    return_code=130,
                )
            )

        driver.send = send

        def write_input(session_id: str, data: str) -> None:
            typed.append(data)
            if data == "\x03":
                driver.cancelled.set()

        driver.write_input = write_input
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        box: dict = {}

        def work() -> None:
            box["result"] = service.run_mcp_command(
                info.session_id, "войти", "ssh root@host"
            )

        thread = threading.Thread(target=work)
        thread.start()
        self.assertTrue(started.wait(2))
        service.reject_running_command(info.session_id, "не тот сервер")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertIn("\x03", typed)
        self.assertIn("не тот сервер", box.get("result", ""))

    def test_tool_outcome_stays_out_of_the_model_prompt(self) -> None:
        service, _driver, profiles = _world()
        info = _session(service, profiles)
        service.record_tool_outcome(info.session_id, "выполнено: cat /etc/passwd")
        self.assertEqual(service._active_chat(info.session_id), [])
        notes = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_TOOL
        ]
        self.assertEqual(notes, ["выполнено: cat /etc/passwd"])
        tab = service.open_lounge_tab()
        service.record_tool_outcome(
            info.session_id,
            "отклонено с комментарием нет\ncat /etc/passwd",
            tab.tab_id,
        )
        self.assertEqual(
            service.lounge_messages(tab.tab_id),
            [("result", "отклонено с комментарием нет\ncat /etc/passwd")],
        )
        self.assertEqual(service._lounge_model_history(tab.tab_id), [])

    def test_lounge_command_record_survives_reload(self) -> None:
        from storage.lounge import FileLoungeStore

        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "lounge.json")
        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            lounge_store=FileLoungeStore(path),
        )
        tab = service.list_lounge_tabs()[0]
        payload = json.dumps(
            {"mcp": "консоль", "command": "ls", "output": "a.txt", "code": 0},
            ensure_ascii=False,
        )
        service.remember_lounge_command(tab.tab_id, payload)
        service.record_tool_outcome("s", "выполнено: ls\nMCP: консоль", tab.tab_id)
        self.assertEqual(service._lounge_model_history(tab.tab_id), [])
        loaded = FileLoungeStore(path).load()
        self.assertEqual(
            [role for role, _text in loaded[0].messages],
            ["command", "result"],
        )
        self.assertIn("консоль", loaded[0].messages[0][1])
        self.assertIn("a.txt", loaded[0].messages[0][1])

    def test_unparsed_tool_call_stays_broken(self) -> None:
        from drivers.llm import _read_message

        _content, calls = _read_message(
            {"content": "Сейчас проверю.", "tool_calls": [{}]}
        )
        self.assertTrue(calls[0].broken)

    def test_bash_block_stays_text_for_the_user(self) -> None:
        text, calls = promote_shell_actions("Смотрю каталог\n```bash\nls -la\n```")
        self.assertEqual(calls, [])
        self.assertIn("```bash", text)
        self.assertIn("ls -la", text)

    def test_plain_text_and_edit_block_are_not_commands(self) -> None:
        note, calls = promote_shell_actions("В каталоге три файла.")
        self.assertEqual(calls, [])
        self.assertEqual(note, "В каталоге три файла.")
        edit = "Заменю фрагмент\n```edit\npath: a\nOLD:\nx\nNEW:\ny\n```"
        text, calls = promote_shell_actions(edit)
        self.assertEqual(calls, [])
        self.assertIn("OLD:", text)
        sample, calls = promote_shell_actions("пример\n```python\nprint(1)\n```")
        self.assertEqual(calls, [])
        self.assertIn("print(1)", sample)
        quoted, calls = promote_shell_actions(
            "сейчас\n<function_call>\ncommand: ls\nintent: список\ntarget: консоль\n"
            "</function_call>"
        )
        self.assertEqual(calls[0].arguments["command"], "ls")
        self.assertEqual(calls[0].arguments["target"], "консоль")
        self.assertNotIn("function_call", quoted)
        fenced, calls = promote_shell_actions(
            "пример\n```python\nrun_command(command=\"ls\", target=\"консоль\")\n```"
        )
        self.assertEqual(calls, [])
        self.assertIn("run_command", fenced)


class AllowStoreTests(unittest.TestCase):
    def test_file_keeps_command_without_intent(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "mcp_allow.json")
            store = FileCommandAllowStore(path)
            store.remember("s-1", " ls \n")
            self.assertTrue(store.allows("s-1", "ls"))
            self.assertFalse(store.allows("s-1", "dir"))
            raw = Path(path).read_text(encoding="utf-8")
            self.assertIn("ls", raw)
            self.assertNotIn("intent", raw)
            self.assertNotIn("посмотреть", raw)


class McpCommandTests(unittest.TestCase):
    def test_reject_and_reason_do_not_run(self) -> None:
        service, driver, profiles = _world()
        info = _session(service, profiles)
        gate = _Gate(CommandAnswer("reject"))
        service.set_command_approver(gate.ask)
        self.assertEqual(
            service.run_mcp_command(info.session_id, "удалить", "rm x"),
            refusal(),
        )
        self.assertEqual(gate.asks[0].intent, "удалить")
        self.assertEqual(gate.asks[0].command, "rm x")
        self.assertEqual(gate.asks[0].parts, ("rm x",))
        self.assertEqual(gate.asks[0].origin_id, "")
        self.assertNotIn("rm x", driver.sent)

        gate.answer = CommandAnswer("reject_reason", "это лишнее")
        self.assertEqual(
            service.run_mcp_command(info.session_id, "удалить", "rm y"),
            refusal_reason("это лишнее"),
        )
        self.assertNotIn("rm y", driver.sent)

    def test_deliver_returns_body_without_running_again(self) -> None:
        service, driver, profiles = _world()
        info = _session(service, profiles)
        body = (
            "данные отредактированы пользователем, вероятно чтобы скрыть "
            "чувствительные данные\nвыполнено, код 0\nскрыто"
        )
        gate = _Gate(CommandAnswer("deliver", body=body))
        service.set_command_approver(gate.ask)
        result = service.run_mcp_command(info.session_id, "посмотреть", "ls")
        self.assertEqual(result, body)
        self.assertNotIn("ls", driver.sent)

    def test_run_returns_output_and_always_skips_the_next_ask(self) -> None:
        allow = _Allow()
        service, driver, profiles = _world(allow=allow)
        info = _session(service, profiles)
        gate = _Gate(CommandAnswer("always"))
        service.set_command_approver(gate.ask)
        first = service.run_mcp_command(
            info.session_id, "посмотреть файлы", "ls"
        )
        self.assertIn("выполнено, код 0", first)
        self.assertIn("a.txt", first)
        self.assertEqual(allow.items[info.session_id], ["ls"])
        self.assertNotIn("посмотреть", json.dumps(allow.items, ensure_ascii=False))

        second = service.run_mcp_command(info.session_id, "ещё раз", "ls")
        self.assertIn("a.txt", second)
        self.assertEqual(len(gate.asks), 1)

        third = service.run_mcp_command(
            info.session_id, "из общего чата", "ls", origin_id=lounge_origin("tab-1")
        )
        self.assertIn("a.txt", third)
        self.assertEqual(len(gate.asks), 1)

    def test_empty_fields_and_missing_chat(self) -> None:
        service, _driver, profiles = _world()
        info = _session(service, profiles)
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        self.assertIn(
            "выполнено",
            service.run_mcp_command(info.session_id, "  ", "ls"),
        )
        self.assertEqual(gate.asks[0].intent, "выполнить команду")
        self.assertEqual(gate.asks[0].command, "ls")
        plain = service.create_session("без модели", "pw")
        service.set_foreground_session(plain.session_id)
        self.assertEqual(
            service.run_foreground_mcp("посмотреть", "ls"),
            refusal_reason("у активной сессии нет чата для подтверждения"),
        )
        origin = lounge_origin("tab-1")
        gate.answer = CommandAnswer("reject")
        self.assertEqual(
            service.run_mcp_command(plain.session_id, "список", "dir", origin_id=origin),
            refusal(),
        )
        self.assertEqual(gate.asks[-1].origin_id, origin)
        self.assertEqual(gate.asks[-1].session_id, plain.session_id)

    def test_interactive_screen_is_cancelled(self) -> None:
        driver = _Driver()
        service, driver, profiles = _world(driver)
        info = _session(service, profiles)
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        result = service.run_mcp_command(info.session_id, "открыть редактор", "nano")
        self.assertEqual(result, refusal_reason(INTERACTIVE_REASON))
        self.assertIn("nano", driver.sent)
        self.assertTrue(driver.cancelled.is_set())

    def test_spawned_shell_is_closed(self) -> None:
        driver = _Driver()
        driver.shell_for = "bash"
        service, driver, profiles = _world(driver)
        info = _session(service, profiles)
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        result = service.run_mcp_command(info.session_id, "открыть оболочку", "bash")
        self.assertEqual(result, refusal_reason(INTERACTIVE_REASON))
        self.assertEqual(driver.sent[-2:], ["bash", "exit"])


class ToolLoopTests(unittest.TestCase):
    def test_model_tool_call_goes_through_approval(self) -> None:
        driver = _Driver()
        profiles = _Profiles()

        class Client:
            def __init__(self) -> None:
                self.seen = []

            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools):
                self.seen.append((messages, tools))
                if any(item.get("role") == "tool" for item in messages):
                    return LlmTurn("в каталоге a.txt", [])
                return LlmTurn(
                    "",
                    [LlmToolCall("c1", "run_command", {
                        "intent": "посмотреть файлы",
                        "command": "ls",
                    })],
                )

        client = Client()
        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=client,
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.set_command_approver(lambda prompt: CommandAnswer("run"))
        service.send_chat(info.session_id, "что в папке")
        tool_message = [
            item for item in client.seen[1][0] if item.get("role") == "tool"
        ][0]
        self.assertIn("a.txt", tool_message["content"])
        self.assertEqual(client.seen[0][1][0]["function"]["name"], "run_command")
        self.assertIn("nano", client.seen[0][1][0]["function"]["description"])
        self.assertIn("ls", driver.sent)
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertEqual(texts, ["в каталоге a.txt"])

    def test_shown_bash_block_is_not_executed(self) -> None:
        driver = _Driver()
        profiles = _Profiles()
        asks = []

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools):
                return LlmTurn("Можно посмотреть так\n```bash\nls\n```", [])

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.set_command_approver(
            lambda prompt: asks.append(prompt) or CommandAnswer("run")
        )
        service.send_chat(info.session_id, "как посмотреть файлы")
        self.assertEqual(asks, [])
        self.assertNotIn("ls", driver.sent)
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertIn("```bash", texts[0])
        self.assertIn("ls", texts[0])

    def test_empty_followup_asks_for_a_text_answer(self) -> None:
        driver = _Driver()
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                if not allow_tools:
                    return LlmTurn("итог по файлам", [])
                if any(item.get("role") == "tool" for item in messages):
                    return LlmTurn("", [])
                return LlmTurn(
                    "",
                    [LlmToolCall("c1", "run_command", {
                        "intent": "посмотреть файлы",
                        "command": "ls",
                    })],
                )

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        service.send_chat(info.session_id, "что в папке")
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertEqual(texts, ["итог по файлам"])

    def test_length_cutoff_is_continued(self) -> None:
        profiles = _Profiles()

        class Client:
            def __init__(self) -> None:
                self.n = 0

            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                self.n += 1
                if self.n == 1:
                    return LlmTurn("начало ответа", [], "length")
                return LlmTurn("продолжение ответа", [])

        client = Client()
        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=client,
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.send_chat(info.session_id, "расскажи")
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertEqual(texts, ["начало ответа\nпродолжение ответа"])
        self.assertGreaterEqual(client.n, 2)

    def test_tool_round_limit_still_answers(self) -> None:
        driver = _Driver()
        profiles = _Profiles()

        class Client:
            def __init__(self) -> None:
                self.n = 0

            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                if not allow_tools:
                    return LlmTurn("хватит данных", [])
                self.n += 1
                return LlmTurn(
                    "",
                    [LlmToolCall(f"c{self.n}", "run_command", {
                        "intent": "шаг",
                        "command": f"echo {self.n}",
                    })],
                )

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        service.send_chat(info.session_id, "исследуй")
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertEqual(texts[-1], "хватит данных")
        self.assertGreaterEqual(sum(1 for text in driver.sent if text.startswith("echo ")), 24)
        self.assertIn("Лимит вызовов", service.llm_problem(info.session_id))

    def test_narrated_tool_without_command_is_shown_as_error(self) -> None:
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                return LlmTurn(
                    "Сейчас использую инструмент.",
                    [LlmToolCall("c1", "run_command", {"intent": "посмотреть"})],
                )

        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.set_command_approver(lambda _prompt: CommandAnswer("run"))
        with self.assertRaises(LlmUnavailable) as caught:
            service.send_chat(info.session_id, "что в папке")
        self.assertIn("не открывалось", str(caught.exception))
        self.assertIn("не открывалось", service.llm_problem(info.session_id))
        texts = [
            event.payload
            for event in service.get_history(info.session_id)
            if event.event_type == domain.EventType.CHAT_ASSISTANT
        ]
        self.assertEqual(texts, ["Сейчас использую инструмент."])

    def test_flat_tool_call_opens_approval(self) -> None:
        from drivers.llm import _read_message

        content, calls = _read_message({
            "content": "Сейчас использую инструмент.",
            "tool_calls": [{
                "id": "c1",
                "name": "run_command",
                "arguments": {"intent": "список", "cmd": "dir"},
            }],
        })
        self.assertEqual(content, "Сейчас использую инструмент.")
        self.assertEqual(calls[0].arguments["cmd"], "dir")

        driver = _Driver()
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                if any(item.get("role") == "tool" for item in messages):
                    return LlmTurn("готово", [])
                return LlmTurn(content, calls)

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        asked = []

        def approve(prompt):
            asked.append(prompt.command)
            return CommandAnswer("run")

        service.set_command_approver(approve)
        service.send_chat(info.session_id, "покажи")
        self.assertEqual(asked, ["dir"])
        self.assertIn("dir", driver.sent)
        self.assertEqual(service.llm_problem(info.session_id), "")

    def test_tools_list_and_call(self) -> None:
        service, _driver, profiles = _world()
        info = _session(service, profiles)
        service.set_foreground_session(info.session_id)
        service.set_command_approver(
            lambda _prompt: CommandAnswer("reject_reason", "не сейчас")
        )
        server = TerminalMcpServer(service)
        endpoint = server.start()
        try:
            listed = _post(endpoint, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            description = listed["result"]["tools"][0]["description"]
            self.assertIn("nano", description)
            self.assertEqual(listed["result"]["tools"][0]["name"], "run_command")
            called = _post(endpoint, {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "run_command",
                    "arguments": {
                        "intent": "посмотреть файлы",
                        "command": "ls",
                    },
                },
            })
            self.assertEqual(
                called["result"]["content"][0]["text"],
                refusal_reason("не сейчас"),
            )
        finally:
            server.stop()

    def test_each_chat_has_its_own_url(self) -> None:
        service, _driver, profiles = _world()
        first = _session(service, profiles)
        second = service.create_session("другая", "pw", llm_profile_id="m-1")
        seen = []
        service.set_command_approver(
            lambda prompt: seen.append(prompt) or CommandAnswer("reject")
        )
        service.set_foreground_session(first.session_id)
        server = TerminalMcpServer(service)
        base = service.attach_mcp(server)
        try:
            self.assertEqual(
                service.mcp_endpoint(second.session_id),
                f"{base}/{second.session_id}",
            )
            self.assertNotEqual(
                service.mcp_endpoint(first.session_id),
                service.mcp_endpoint(second.session_id),
            )
            listed = _post(
                f"{base}/{second.session_id}",
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            self.assertIn("другая", listed["result"]["tools"][0]["description"])
            ready = _post(
                f"{base}/{second.session_id}",
                {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}},
            )
            self.assertIn("другая", ready["result"]["serverInfo"]["name"])
            _post(
                f"{base}/{second.session_id}",
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "run_command",
                        "arguments": {"intent": "список", "command": "ls"},
                    },
                },
            )
            self.assertEqual([item.session_id for item in seen], [second.session_id])
            self.assertEqual(seen[0].origin_id, "")
        finally:
            server.stop()


class OutputClipTests(unittest.TestCase):
    def test_long_console_output_is_cut(self) -> None:
        from application.terminal_mcp import OUTPUT_CHAR_LIMIT, finished_text

        short = finished_text(0, "ok")
        self.assertNotIn("обрезано", short)
        text = finished_text(0, "x" * (OUTPUT_CHAR_LIMIT + 80))
        self.assertIn("обрезано из-за большого числа символов", text)
        self.assertIn(f"показано {OUTPUT_CHAR_LIMIT} из {OUTPUT_CHAR_LIMIT + 80}", text)
        self.assertLess(text.index("обрезано"), len(text))
        self.assertNotIn("x" * (OUTPUT_CHAR_LIMIT + 1), text)


class ClientToolTests(unittest.TestCase):
    def test_complete_turn_reads_tool_call(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.server.seen = payload  # type: ignore[attr-defined]
                body = json.dumps({
                    "choices": [{
                        "message": {
                            "content": None,
                            "tool_calls": [{
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "run_command",
                                    "arguments": json.dumps({
                                        "intent": "посмотреть файлы",
                                        "command": "ls",
                                    }),
                                },
                            }],
                        }
                    }]
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.seen = None  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            profile = domain.LlmProfile(
                "m-1", "локальная", f"http://127.0.0.1:{port}/v1", "demo"
            )
            turn = LmStudioClient(timeout=2, complete_timeout=2).complete_turn(
                profile,
                [{"role": "user", "content": "покажи файлы"}],
                [{"type": "function", "function": {"name": "run_command"}}],
            )
            self.assertEqual(turn.tool_calls[0].name, "run_command")
            self.assertEqual(turn.tool_calls[0].arguments["command"], "ls")
            self.assertEqual(server.seen["tool_choice"], "auto")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class CommandSplitTests(unittest.TestCase):
    def test_unix_splits_semicolons_and_keeps_chains(self) -> None:
        self.assertEqual(
            split_shell_commands("apt install; apt update", "unix"),
            ("apt install", "apt update"),
        )
        self.assertEqual(
            split_shell_commands("cmd1 && cmd2 || cmd3", "unix"),
            ("cmd1 && cmd2 || cmd3",),
        )
        self.assertEqual(
            split_shell_commands("cmd4 | grep x", "unix"),
            ("cmd4 | grep x",),
        )
        self.assertEqual(
            split_shell_commands(
                'echo "a; b"; cmd1 && cmd2 || cmd3; cmd4 | grep x',
                "unix",
            ),
            ('echo "a; b"', "cmd1 && cmd2 || cmd3", "cmd4 | grep x"),
        )
        self.assertEqual(
            split_shell_commands("cmd1 &&\ncmd2\ncmd3", "unix"),
            ("cmd1 &&\ncmd2", "cmd3"),
        )

    def test_windows_splits_ampersand_not_conditional(self) -> None:
        self.assertEqual(
            split_shell_commands("dir & echo hi", "windows"),
            ("dir", "echo hi"),
        )
        self.assertEqual(
            split_shell_commands("dir && echo hi || echo no", "windows"),
            ("dir && echo hi || echo no",),
        )
        self.assertEqual(
            split_shell_commands("dir >out.txt 2>&1 & echo hi", "windows"),
            ("dir >out.txt 2>&1", "echo hi"),
        )
        self.assertEqual(
            split_shell_commands('echo "a & b" & echo c', "windows"),
            ('echo "a & b"', "echo c"),
        )
        self.assertEqual(
            split_shell_commands("echo a; echo b", "windows"),
            ("echo a; echo b",),
        )

    def test_other_stays_one_string(self) -> None:
        self.assertEqual(
            split_shell_commands("apt install; apt update", "other"),
            ("apt install; apt update",),
        )

    def test_ask_keeps_original_text_and_exposes_parts(self) -> None:
        service, driver, profiles = _world()
        info = _session(service, profiles)
        service.update_session_settings(
            info.session_id,
            info.name,
            "",
            "",
            shell_family="unix",
        )
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        service.run_mcp_command(info.session_id, "обновить", "apt install; apt update")
        self.assertEqual(gate.asks[0].command, "apt install; apt update")
        self.assertEqual(gate.asks[0].parts, ("apt install", "apt update"))
        self.assertEqual(driver.sent[-1], "apt install; apt update")

        service.update_session_settings(
            info.session_id, info.name, "", "", shell_family="windows"
        )
        service.run_mcp_command(info.session_id, "список", "dir & echo hi")
        self.assertEqual(gate.asks[-1].parts, ("dir", "echo hi"))
        self.assertEqual(driver.sent[-1], "dir & echo hi")

        service.update_session_settings(
            info.session_id, info.name, "", "", shell_family="other"
        )
        service.run_mcp_command(info.session_id, "как есть", "echo a; echo b")
        self.assertEqual(gate.asks[-1].parts, ("echo a; echo b",))


class ChatCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_four_buttons_and_reason_field(self) -> None:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

        from ui.chat_pane import ChatPane

        pane = ChatPane(QFont("Consolas"))
        pane.resize(500, 400)
        pane.show()
        QApplication.processEvents()
        seen = []
        pane.command_decided.connect(lambda *args: seen.append(args))
        pane.show_command_request("id-1", "посмотреть файлы в текущей директории", "ls")
        QApplication.processEvents()
        self.assertEqual(pane.findChild(QLabel, "shellCode").text(), "Команда: ls")
        buttons = {button.text(): button for button in pane.findChildren(QPushButton)}
        for title in ("Выполнить", "Добавить правило", "Отклонить", "Отклонить по причине"):
            self.assertIn(title, buttons)
        self.assertNotIn("Всегда выполнять", buttons)
        field = pane.findChild(QLineEdit, "commandReason")
        self.assertIsNotNone(field)
        assert field is not None
        self.assertFalse(field.isVisible())
        buttons["Отклонить по причине"].click()
        QApplication.processEvents()
        self.assertTrue(field.isVisible())
        field.setText("не надо")
        buttons["Отправить отказ"].click()
        self.assertEqual(seen, [("id-1", "reject_reason", "не надо")])
        pane.show_command_request(
            "id-2",
            "обновить",
            "apt install; apt update",
            ("apt install", "apt update"),
        )
        QApplication.processEvents()
        labels = pane.findChildren(QLabel, "shellCode")
        self.assertEqual(labels[-1].text(), "Команда:\napt install\napt update")
        pane.close()

    def test_manual_approval_keeps_output_for_redaction(self) -> None:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QFrame, QPlainTextEdit, QPushButton

        from ui.chat_pane import ChatPane

        pane = ChatPane(QFont("Consolas"))
        pane.resize(500, 500)
        pane.show()
        QApplication.processEvents()
        delivered = []
        pane.output_decided.connect(lambda *args: delivered.append(args))
        pane.show_command_request("id-1", "посмотреть", "ls")
        buttons = {button.text(): button for button in pane.findChildren(QPushButton)}
        buttons["Выполнить"].click()
        QApplication.processEvents()
        pane.show_command_running("id-1")
        pane.note_command_output("id-1", "token=secret\n", False)
        pane.show_command_result(
            "id-1", "token=secret\n", "выполнено, код 0\ntoken=secret\n", 0
        )
        QApplication.processEvents()
        edit = pane.findChild(QPlainTextEdit, "commandCapture")
        assert edit is not None
        self.assertFalse(edit.isEnabled())
        self.assertIn("token=secret", edit.toPlainText())
        card = pane.findChild(QFrame, "commandRequest")
        assert card is not None
        card_buttons = {
            button.text(): button for button in card.findChildren(QPushButton)
        }
        self.assertTrue(card_buttons["Отправить"].isEnabled())
        self.assertTrue(card_buttons["Обезличить"].isVisible())
        card_buttons["Обезличить"].click()
        QApplication.processEvents()
        self.assertTrue(edit.isEnabled())
        self.assertFalse(card_buttons["Обезличить"].isVisible())
        edit.setPlainText("token=\n")
        card_buttons["Отправить"].click()
        self.assertEqual(delivered[0][0], "id-1")
        self.assertEqual(delivered[0][1], "token=\n")
        self.assertTrue(delivered[0][2])
        self.assertEqual(delivered[0][3], "выполнено, код 0\ntoken=secret\n")
        self.assertEqual(delivered[0][4], 0)
        pane.close()

    def test_review_checkbox_sends_the_answer_immediately_when_off(self) -> None:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

        from application.terminal_mcp import CommandAsk
        from ui.chat_pane import ChatPane
        from ui.main_window import MainWindow

        pane = ChatPane(QFont("Consolas"))
        box = pane.findChild(QCheckBox, "reviewOutput")
        assert box is not None
        self.assertTrue(box.isChecked())
        self.assertTrue(pane.review_before_send())
        box.setChecked(False)
        self.assertFalse(pane.review_before_send())
        pane.close()

        service, _driver, profiles = _world()
        info = _session(service, profiles)
        window = MainWindow(service)
        window.show()
        window._activate_session(info.session_id)
        QApplication.processEvents()
        window.chat_pane.findChild(QCheckBox, "reviewOutput").setChecked(False)
        answers = []
        prompt = CommandAsk(
            request_id="id-off",
            session_id=info.session_id,
            intent="список",
            command="ls",
            parts=("ls",),
        )
        window._mcp_pending[info.session_id] = (prompt, answers.append)
        window.chat_pane.show_command_request("id-off", "список", "ls")
        QApplication.processEvents()
        run = next(
            button
            for button in window.chat_pane.findChildren(QPushButton)
            if button.text() == "Выполнить"
        )
        run.click()
        QApplication.processEvents()
        self.assertEqual([item.action for item in answers], ["run"])
        self.assertNotIn("id-off", window._reviewing)
        self.assertIsNone(window.chat_pane._request_card)
        window.close()

    def test_add_rule_keeps_the_request_and_refreshes_highlight(self) -> None:
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextEdit

        from application.command_rules import command_spans
        from application.terminal_mcp import CommandAsk
        from storage.command_rules import FileCommandRuleStore
        from ui.main_window import MainWindow
        from ui.rules_dialog import PatternDialog

        folder = tempfile.mkdtemp()
        service, _driver, profiles = _world()
        service._command_rules = FileCommandRuleStore(
            os.path.join(folder, "command_rules.json")
        )
        profiles.save(domain.LlmProfile(
            "m-1", "локальная", "http://127.0.0.1:9/v1", "demo"
        ))
        info = service.create_session(
            "работа", "pw", llm_profile_id="m-1", shell_family=domain.SHELL_WINDOWS
        )
        window = MainWindow(service)
        command = "dir /b && echo hi"
        parts = ("dir /b && echo hi",)
        prompt = CommandAsk(
            request_id="id-rule",
            session_id=info.session_id,
            intent="список",
            command=command,
            parts=parts,
        )
        window._mcp_pending[info.session_id] = (prompt, lambda _answer: None)
        window.chat_pane.show_command_request(
            prompt.request_id, prompt.intent, command, parts
        )
        decided = []
        window.chat_pane.command_decided.connect(lambda *args: decided.append(args))
        seen_os: list[str] = []

        def fake_exec(dialog: PatternDialog) -> int:
            seen_os.extend(item.text() for item in dialog.findChildren(QLabel))
            dialog._pattern.setText("dir *")
            dialog._on_save()
            return int(dialog.result())

        original = PatternDialog.exec
        PatternDialog.exec = fake_exec  # type: ignore[method-assign]
        try:
            window._on_add_command_rule(prompt.request_id)
        finally:
            PatternDialog.exec = original  # type: ignore[method-assign]
        QApplication.processEvents()
        self.assertIn("ОС: windows", seen_os)
        self.assertEqual(decided, [])
        self.assertEqual(
            service.command_rule_lists()[domain.SHELL_WINDOWS],
            ("dir *",),
        )
        self.assertEqual(service.command_rule_lists()[domain.SHELL_UNIX], ())
        run = next(
            button
            for button in window.chat_pane.findChildren(QPushButton)
            if button.text() == "Выполнить"
        )
        self.assertTrue(run.isEnabled())
        edit = window.chat_pane.findChild(QTextEdit, "shellCodeRich")
        self.assertIsNotNone(edit)
        assert edit is not None
        cursor = edit.textCursor()
        cursor.setPosition(len("Команда: ") + 1)
        self.assertEqual(cursor.charFormat().foreground().color().name(), "#86efac")
        cursor.setPosition(len(edit.toPlainText()) - 1)
        self.assertEqual(cursor.charFormat().foreground().color().name(), "#e4e4e7")
        spans = command_spans(
            command,
            domain.SHELL_WINDOWS,
            service._command_rules.rule_set_for(domain.SHELL_WINDOWS),
        )
        self.assertTrue(spans[0][0].matched)
        self.assertFalse(spans[0][-1].matched)
        self.assertEqual(window._mcp_pending[info.session_id][0].spans, spans)
        window.close()

    def test_auto_run_shows_the_block_then_executes_a_full_match(self) -> None:
        from PySide6.QtWidgets import QApplication, QCheckBox, QFrame

        from application.command_rules import TextSpan
        from application.terminal_mcp import CommandAsk, lounge_origin
        from ui.general_chat import GENERAL_CHAT_ID
        from ui.main_window import MainWindow

        service, _driver, profiles = _world()
        info = _session(service, profiles)
        window = MainWindow(service)
        window.show()
        window._activate_session(info.session_id)
        QApplication.processEvents()
        tip = "выполнять автоматически при полном совпадении с паттернами"
        personal = window.chat_pane.findChild(QCheckBox, "autoRun")
        lounge_box = window._lounge.findChild(QCheckBox, "autoRun")
        assert personal is not None and lounge_box is not None
        self.assertTrue(personal.isChecked())
        self.assertTrue(lounge_box.isChecked())
        self.assertEqual(personal.text(), "Авто")
        self.assertEqual(lounge_box.text(), "Авто")
        self.assertEqual(personal.toolTip(), tip)
        self.assertEqual(lounge_box.toolTip(), tip)

        full = ((TextSpan("ls", True, True),),)
        answers: list = []
        window._present_mcp((
            CommandAsk(
                "auto-1", info.session_id, "список", "ls", parts=("ls",), spans=full
            ),
            answers.append,
        ))
        self.assertIsNotNone(window.chat_pane.findChild(QFrame, "commandRequest"))
        self.assertEqual(answers, [])
        QApplication.processEvents()
        self.assertEqual([item.action for item in answers], ["run"])
        QApplication.processEvents()
        self.assertIsNone(window.chat_pane._request_card)
        self.assertIsNone(window.chat_pane.findChild(QFrame, "commandRequest"))

        personal.setChecked(False)
        answers.clear()
        window._present_mcp((
            CommandAsk(
                "auto-2", info.session_id, "список", "ls", parts=("ls",), spans=full
            ),
            answers.append,
        ))
        QApplication.processEvents()
        self.assertEqual(answers, [])
        self.assertIsNotNone(window.chat_pane.findChild(QFrame, "commandRequest"))

        personal.setChecked(True)
        answers.clear()
        partial = ((
            TextSpan("grep a", True, True),
            TextSpan(" && ", False, False),
            TextSpan("rm x", False, True),
        ),)
        window._present_mcp((
            CommandAsk(
                "auto-3",
                info.session_id,
                "смешать",
                "grep a && rm x",
                parts=("grep a && rm x",),
                spans=partial,
            ),
            answers.append,
        ))
        QApplication.processEvents()
        self.assertEqual(answers, [])
        self.assertIsNotNone(window.chat_pane.findChild(QFrame, "commandRequest"))

        window._activate_session(GENERAL_CHAT_ID)
        QApplication.processEvents()
        answers.clear()
        tab_id = window._lounge.current_tab_id()
        window._present_mcp((
            CommandAsk(
                "auto-4",
                info.session_id,
                "список",
                "ls",
                origin_id=lounge_origin(tab_id),
                parts=("ls",),
                spans=full,
            ),
            answers.append,
        ))
        self.assertIsNotNone(window._lounge.findChild(QFrame, "commandRequest"))
        QApplication.processEvents()
        self.assertEqual([item.action for item in answers], ["run"])
        QApplication.processEvents()
        self.assertIsNone(window._lounge._tabs[window._lounge._current].request)
        self.assertIsNone(window._lounge.findChild(QFrame, "commandRequest"))
        window.close()

    def test_window_shows_request_from_background_thread(self) -> None:
        from PySide6.QtWidgets import QApplication, QLabel, QPushButton

        from ui.main_window import MainWindow

        service, _driver, profiles = _world()
        info = _session(service, profiles)
        window = MainWindow(service)
        window.show()
        window._activate_session(info.session_id)
        box = {}

        def work() -> None:
            box["result"] = service.run_mcp_command(
                info.session_id, "посмотреть файлы", "ls"
            )

        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.time() + 3
        clicked = False
        while time.time() < deadline and thread.is_alive():
            QApplication.processEvents()
            for button in window.chat_pane.findChildren(QPushButton):
                if button.text() == "Отклонить" and button.isEnabled():
                    button.click()
                    clicked = True
                    break
            if clicked:
                break
            time.sleep(0.02)
        thread.join(timeout=3)
        QApplication.processEvents()
        notes = [
            label.text()
            for label in window.chat_pane.findChildren(QLabel, "toolOutcome")
        ]
        window.close()
        self.assertTrue(clicked)
        self.assertEqual(box.get("result"), refusal())
        self.assertIn("отклонено\nls", notes)

    def test_lounge_origin_shows_the_card_in_the_general_chat(self) -> None:
        from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

        from ui.main_window import MainWindow

        service, driver, profiles = _world()
        console = _session(service, profiles)
        plain = service.create_session("пульт", "pw")
        window = MainWindow(service)
        window.show()
        window._activate_session(console.session_id)
        QApplication.processEvents()
        origin = lounge_origin(window._lounge.current_tab_id())
        box = {}

        def work() -> None:
            box["result"] = service.run_mcp_command(
                plain.session_id, "список", "ls", origin_id=origin
            )

        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.time() + 3
        clicked = False
        saw_target = False
        while time.time() < deadline and thread.is_alive():
            QApplication.processEvents()
            if window._pages.currentWidget() is not window._lounge:
                time.sleep(0.02)
                continue
            labels = [label.text() for label in window._lounge.findChildren(QLabel)]
            saw_target = saw_target or "MCP: пульт" in labels
            for button in window._lounge.findChildren(QPushButton):
                if button.text() == "Отклонить" and button.isEnabled():
                    self.assertIsNone(window.chat_pane.findChild(QFrame, "commandRequest"))
                    button.click()
                    clicked = True
                    break
            if clicked:
                break
            time.sleep(0.02)
        thread.join(timeout=3)
        QApplication.processEvents()
        notes = [
            label.text()
            for label in window._lounge.findChildren(QLabel, "toolOutcome")
        ]
        window.close()
        self.assertTrue(saw_target)
        self.assertTrue(clicked)
        self.assertEqual(box.get("result"), refusal())
        self.assertNotIn("ls", driver.sent)
        self.assertTrue(
            any(note.startswith("отклонено\nls") and "MCP: пульт" in note for note in notes)
        )

    def test_lounge_command_accepts_a_password(self) -> None:
        from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

        from ui.command_card import CommandCard
        from ui.main_window import MainWindow

        service, driver, profiles = _world()
        info = _session(service, profiles)
        typed = []
        service.write_input = lambda session_id, data: typed.append((session_id, data))
        window = MainWindow(service)
        window.show()
        window._show_general_chat(None)
        QApplication.processEvents()
        tab_id = window._lounge.current_tab_id()
        window._lounge.set_busy(tab_id, True)
        window._lounge_runs[info.session_id] = tab_id

        def emit(kind, payload, persist=True, code=None):
            window._on_event_ui(
                domain.TranscriptEvent(
                    event_id="e",
                    session_id=info.session_id,
                    sequence=0,
                    event_type=kind,
                    timestamp=datetime.now(),
                    command_id="c1",
                    stream_type=domain.StreamType.STDOUT,
                    payload=payload,
                    persist=persist,
                    return_code=code,
                )
            )

        emit(domain.EventType.COMMAND_SENT, "ssh root@host\n")
        emit(domain.EventType.OUTPUT_CHUNK, "root@host's password: ", persist=False)
        QApplication.processEvents()
        cards = window._lounge.findChildren(CommandCard)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertIn("ssh root@host", card.command_label.text())
        self.assertTrue(card.is_live())
        self.assertTrue(card.terminal.isVisible())
        thinking = [
            label
            for label in window._lounge.findChildren(QLabel, "loungeThinking")
            if label.isVisible()
        ]
        self.assertEqual(thinking, [])
        card.input_generated.emit("secret\r")
        self.assertEqual(typed, [(info.session_id, "secret\r")])
        emit(domain.EventType.OUTPUT_CHUNK, "ok", persist=True)
        card.reject_button.click()
        QApplication.processEvents()
        reason = card.findChild(QLineEdit, "commandReason")
        self.assertIsNotNone(reason)
        reason.setText("не надо")
        for button in card.findChildren(QPushButton):
            if button.text() == "Отправить отказ":
                button.click()
        self.assertIn((info.session_id, "\x03"), typed)
        emit(domain.EventType.COMMAND_FINISHED, "", code=0)
        QApplication.processEvents()
        self.assertFalse(card.is_live())
        self.assertIn("ok", card.output_text())
        self.assertNotIn(info.session_id, window._lounge_runs)
        window.close()


class LoungeChatTests(unittest.TestCase):
    def test_message_roundtrip_and_mcp_target(self) -> None:
        driver = _Driver()
        profiles = _Profiles()
        calls = {"n": 0}

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                calls["n"] += 1
                if calls["n"] == 1:
                    return domain.LlmTurn(
                        "",
                        [
                            domain.LlmToolCall(
                                "c1",
                                "run_command",
                                {
                                    "intent": "список",
                                    "command": "ls",
                                    "target": "консоль",
                                },
                            )
                        ],
                        "tool_calls",
                    )
                return domain.LlmTurn("в каталоге a.txt", [])

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.update_session_settings(info.session_id, info.name, "консоль", "основной")
        service._mcp_endpoint = "http://127.0.0.1:9/mcp"
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        service.send_lounge_chat(tab.tab_id, "покажи файлы")
        self.assertEqual(gate.asks[0].session_id, info.session_id)
        self.assertEqual(gate.asks[0].origin_id, lounge_origin(tab.tab_id))
        self.assertIn("ls", driver.sent)
        messages = service.lounge_messages(tab.tab_id)
        self.assertEqual(messages[0], ("user", "покажи файлы"))
        self.assertEqual(messages[-1], ("assistant", "в каталоге a.txt"))
        self.assertEqual(service.lounge_problem(tab.tab_id), "")

    def test_mcp_cite_in_the_draft_expands_in_the_prompt(self) -> None:
        profiles = _Profiles()
        seen = {}

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                seen["messages"] = messages
                seen["tools"] = tools
                return domain.LlmTurn("ок", [])

        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.update_session_settings(
            info.session_id,
            info.name,
            "консоль",
            "основной терминал",
            os_name="Linux 6.8.0-45-generic x86_64",
            username="root",
            ip_addresses="10.0.0.5",
        )
        service._mcp_endpoint = "http://127.0.0.1:9/mcp"
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        service.send_lounge_chat(tab.tab_id, "смотри «MCP: консоль»")
        user = next(item["content"] for item in seen["messages"] if item["role"] == "user")
        system = next(item["content"] for item in seen["messages"] if item["role"] == "system")
        self.assertIn("Подключи MCP «консоль»", user)
        self.assertIn("Описание: основной терминал", user)
        self.assertIn("ОС: Linux 6.8.0-45-generic x86_64", user)
        self.assertIn("IP: 10.0.0.5", user)
        self.assertIn("Пользователь: root", user)
        self.assertNotIn("IP:", "основной терминал")
        self.assertIn("ОС: Linux 6.8.0-45-generic x86_64", system)
        self.assertIn("IP: 10.0.0.5", system)
        self.assertIn("Пользователь: root", system)
        self.assertNotIn("«MCP:", user)
        description = seen["tools"][0]["function"]["description"]
        self.assertIn("консоль — Linux 6.8.0-45-generic x86_64", description)
        target = seen["tools"][0]["function"]["parameters"]["properties"]["target"]
        self.assertIn("консоль", target["description"])
        self.assertNotIn("Linux", target["description"])
        stored = service.lounge_messages(tab.tab_id)[0][1]
        self.assertEqual(stored, "смотри «MCP: консоль»")

    def test_promise_without_a_tool_is_asked_again(self) -> None:
        driver = _Driver()
        profiles = _Profiles()
        calls = {"n": 0}
        seen = []

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                calls["n"] += 1
                seen.append([dict(item) for item in messages])
                if calls["n"] == 1:
                    return domain.LlmTurn("Сейчас проверю каталог.", [], "stop")
                if calls["n"] == 2:
                    return domain.LlmTurn(
                        "",
                        [
                            domain.LlmToolCall(
                                "c1",
                                "run_command",
                                {
                                    "intent": "список",
                                    "command": "ls",
                                    "target": "консоль",
                                },
                            )
                        ],
                        "tool_calls",
                    )
                return domain.LlmTurn("в каталоге a.txt", [], "stop")

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service.update_session_settings(info.session_id, info.name, "консоль", "")
        service._mcp_endpoint = "http://127.0.0.1:9/mcp"
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        service.send_lounge_chat(tab.tab_id, "что в каталоге")
        self.assertGreaterEqual(calls["n"], 2)
        self.assertIn("run_command не вызван", seen[1][-1]["content"])
        self.assertEqual(gate.asks[0].session_id, info.session_id)
        self.assertIn("ls", driver.sent)
        self.assertEqual(service.lounge_problem(tab.tab_id), "")
        self.assertEqual(service.lounge_messages(tab.tab_id)[-1][1], "в каталоге a.txt")

    def test_repeated_promise_shows_the_error(self) -> None:
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                return domain.LlmTurn("Сейчас проверю каталог.", [], "stop")

        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        _session(service, profiles)
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        service.send_lounge_chat(tab.tab_id, "что в каталоге")
        self.assertEqual(gate.asks, [])
        self.assertIn("не вызвала инструмент", service.lounge_problem(tab.tab_id))
        self.assertIn("Сейчас проверю", service.lounge_messages(tab.tab_id)[-1][1])

    def test_unknown_mcp_does_not_open_approval(self) -> None:
        driver = _Driver()
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                if any(item.get("role") == "tool" for item in messages):
                    return domain.LlmTurn("такого MCP нет", [])
                return domain.LlmTurn(
                    "",
                    [
                        domain.LlmToolCall(
                            "c1",
                            "run_command",
                            {"intent": "список", "command": "ls", "target": "чужой"},
                        )
                    ],
                    "tool_calls",
                )

        service = ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        info = _session(service, profiles)
        service._mcp_endpoint = "http://127.0.0.1:9/mcp"
        gate = _Gate(CommandAnswer("run"))
        service.set_command_approver(gate.ask)
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        service.send_lounge_chat(tab.tab_id, "запусти")
        self.assertEqual(gate.asks, [])
        self.assertNotIn("ls", driver.sent)
        self.assertEqual(service.lounge_messages(tab.tab_id)[-1][1], "такого MCP нет")

    def test_stop_keeps_the_user_message_without_a_problem(self) -> None:
        profiles = _Profiles()

        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                return "ответ"

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                stop = domain.current_chat_stop()
                deadline = time.time() + 2
                while time.time() < deadline:
                    if stop is not None and stop.cancelled():
                        raise domain.ChatStopped()
                    time.sleep(0.02)
                raise AssertionError("остановка не пришла")

        service = ApplicationService(
            lambda _provider: _Driver(),
            _Catalog(),
            _Journal(),
            llm_profiles=profiles,
            llm_client=Client(),
            command_allow=_Allow(),
        )
        profiles.save(domain.LlmProfile(
            "m-1", "локальная", "http://127.0.0.1:9/v1", "demo"
        ))
        tab = service.list_lounge_tabs()[0]
        service.choose_lounge_model(tab.tab_id, "m-1")
        box = {}

        def work() -> None:
            try:
                service.send_lounge_chat(tab.tab_id, "стоп")
                box["error"] = None
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=work)
        thread.start()
        deadline = time.time() + 2
        while time.time() < deadline and thread.is_alive():
            if service.lounge_messages(tab.tab_id):
                service.cancel_lounge_chat(tab.tab_id)
                break
            time.sleep(0.02)
        thread.join(timeout=2)
        self.assertIsNone(box.get("error"))
        self.assertEqual(service.lounge_messages(tab.tab_id), [("user", "стоп")])
        self.assertEqual(service.lounge_problem(tab.tab_id), "")


class McpListenTests(unittest.TestCase):
    def test_ports_accept_a_range_or_a_list(self) -> None:
        self.assertEqual(parse_mcp_ports("47321-47323"), (47321, 47322, 47323))
        self.assertEqual(parse_mcp_ports("9, 9, 10"), (9, 10))
        self.assertEqual(parse_mcp_host(" 127.0.0.1 "), "127.0.0.1")
        with self.assertRaises(ValueError):
            parse_mcp_ports("47330-47321")
        with self.assertRaises(ValueError):
            parse_mcp_host("localhost")

    def test_saved_port_is_the_one_that_listens(self) -> None:
        first, second, busy = _free_ports(3)
        service, _driver, _profiles = _world()
        directory = tempfile.mkdtemp()
        settings = FileMcpSettings(os.path.join(directory, "mcp.json"))
        service._mcp_settings = settings
        server = TerminalMcpServer(service, "127.0.0.1", (first,))
        service.attach_mcp(server)
        held = socket.socket()
        held.bind(("127.0.0.1", busy))
        try:
            endpoint = service.update_mcp_listen("127.0.0.1", (second,), str(second))
            self.assertEqual(endpoint, f"http://127.0.0.1:{second}/mcp")
            self.assertEqual(settings.load(), ("127.0.0.1", str(second)))
            self.assertEqual(
                _post(endpoint, {"jsonrpc": "2.0", "id": 1, "method": "ping"})["result"],
                {},
            )
            again = service.update_mcp_listen("127.0.0.1", (second,), str(second))
            self.assertEqual(again, endpoint)
            self.assertEqual(
                _post(endpoint, {"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"],
                {},
            )
            with self.assertRaises(OSError):
                service.update_mcp_listen("127.0.0.1", (busy,), str(busy))
            self.assertEqual(service.mcp_endpoint(), f"http://127.0.0.1:{second}/mcp")
            self.assertEqual(settings.load(), ("127.0.0.1", str(second)))
            self.assertEqual(
                _post(endpoint, {"jsonrpc": "2.0", "id": 3, "method": "ping"})["result"],
                {},
            )
        finally:
            held.close()
            server.stop()


def _free_ports(count: int) -> list:
    sockets = []
    ports = []
    for _index in range(count):
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        ports.append(held.getsockname()[1])
        sockets.append(held)
    for held in sockets:
        held.close()
    return ports


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
