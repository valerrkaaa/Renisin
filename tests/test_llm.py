# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Профили LM Studio, чат сессии и независимые обрывы."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.service import (
    ApplicationService,
    LlmUnavailable,
    _clean_os_output,
    _clean_stored_addresses,
    _collect_addresses,
)
from drivers.llm import LmStudioClient
from drivers.llm_factory import RoutingLlmClient
from storage.catalog import FileSessionCatalog
from storage.llm_profiles import FileLlmProfileStore


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


class _Driver:
    def __init__(self) -> None:
        self.closed = False
        self.sent = []
        self.os_output = ""

    def set_event_sink(self, sink) -> None:
        self.sink = sink

    def open(self, config: domain.SessionConfig) -> str:
        return config.session_id

    def send(self, request: domain.CommandRequest) -> None:
        self.sent.append(request.text)
        sink = getattr(self, "sink", None)
        if sink is None:
            return
        if self.os_output:
            sink(domain.OutputEvent(
                session_id=request.session_id,
                event_type=domain.EventType.OUTPUT_CHUNK,
                timestamp=datetime.now(),
                command_id=request.command_id,
                stream_type=domain.StreamType.STDOUT,
                payload=self.os_output,
                persist=True,
            ))
        sink(domain.OutputEvent(
            session_id=request.session_id,
            event_type=domain.EventType.COMMAND_FINISHED,
            timestamp=datetime.now(),
            command_id=request.command_id,
            stream_type=domain.StreamType.SYSTEM,
            payload="",
            return_code=0,
        ))

    def close(self) -> None:
        self.closed = True

    def complete(self, session_id: str, text: str):
        return []

    def write_input(self, session_id: str, data: str) -> None:
        return None

    def resize(self, session_id: str, cols: int, rows: int) -> None:
        return None


class _Client:
    def __init__(self) -> None:
        self.models = ["demo"]
        self.reply = "ответ"
        self.seen = []
        self.fail = False

    def list_models(self, profile: domain.LlmProfile):
        if self.fail:
            raise ConnectionError("нет сервера")
        return list(self.models)

    def complete(self, profile: domain.LlmProfile, messages):
        self.seen.append(list(messages))
        if self.fail:
            raise ConnectionError("модель молчит")
        return self.reply


def _service(client: _Client | None = None):
    driver = _Driver()
    journal = _Journal()
    catalog = _Catalog()
    profiles = {}

    class Store:
        def list(self):
            return list(profiles.values())

        def get(self, profile_id: str):
            return profiles.get(profile_id)

        def save(self, profile: domain.LlmProfile) -> None:
            profiles[profile.profile_id] = profile

        def delete(self, profile_id: str) -> None:
            profiles.pop(profile_id, None)

    llm = client or _Client()
    service = ApplicationService(
        lambda _provider: driver,
        catalog,
        journal,
        llm_profiles=Store(),
        llm_client=llm,
    )
    return service, driver, journal, profiles, llm


class ProfileStoreTests(unittest.TestCase):
    def test_roundtrip_keeps_key_out_of_session_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = FileLlmProfileStore(os.path.join(folder, "llm_profiles.json"))
            profile = domain.LlmProfile(
                profile_id="m-1",
                name="локальная",
                base_url="http://127.0.0.1:1234/v1",
                model_id="demo",
                api_key="secret-key",
            )
            store.save(profile)
            loaded = store.get("m-1")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.api_key, "secret-key")

            catalog = FileSessionCatalog(os.path.join(folder, "sessions.json"))
            catalog.save(
                domain.SessionInfo(
                    session_id="s-1",
                    name="работа",
                    provider=domain.ProviderType.CMD,
                    identity=domain.ExecutionIdentityType.CURRENT_USER,
                    status=domain.SessionStatus.READY,
                    llm_profile_id="m-1",
                )
            )
            raw = Path(folder, "sessions.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-key", raw)
            self.assertIn("m-1", raw)
            again = catalog.get("s-1")
            assert again is not None
            self.assertEqual(again.llm_profile_id, "m-1")
            self.assertEqual(loaded.api_kind, domain.LlmApiKind.OPENAI)
            raw_profiles = Path(folder, "llm_profiles.json").read_text(encoding="utf-8")
            self.assertIn('"api_kind": "openai"', raw_profiles)

    def test_missing_kind_loads_as_lm_studio_and_unknown_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "llm_profiles.json")
            Path(path).write_text(
                json.dumps({
                    "profiles": [
                        {
                            "profile_id": "m-old",
                            "name": "старая",
                            "base_url": "http://127.0.0.1:1234/v1",
                            "model_id": "demo",
                            "api_key": "",
                        },
                        {
                            "profile_id": "m-bad",
                            "name": "чужая",
                            "base_url": "http://example",
                            "model_id": "x",
                            "api_kind": "anthropic",
                        },
                    ]
                }),
                encoding="utf-8",
            )
            store = FileLlmProfileStore(path)
            loaded = store.list()
            self.assertEqual([item.profile_id for item in loaded], ["m-old"])
            self.assertEqual(loaded[0].api_kind, domain.LlmApiKind.LM_STUDIO)


class ClientTests(unittest.TestCase):
    def test_lists_models_and_completes(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"data": [{"id": "demo"}]}).encode()
                self._send(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.server.seen = payload  # type: ignore[attr-defined]
                body = json.dumps(
                    {"choices": [{"message": {"content": "готово"}}]}
                ).encode()
                self._send(body)

            def _send(self, body: bytes) -> None:
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
                profile_id="m-1",
                name="локальная",
                base_url=f"http://127.0.0.1:{port}/v1",
                model_id="demo",
            )
            client = LmStudioClient(timeout=2, complete_timeout=2)
            self.assertEqual(client.list_models(profile), ["demo"])
            answer = client.complete(profile, [("user", "привет")])
            self.assertEqual(answer, "готово")
            self.assertEqual(server.seen["messages"], [{"role": "user", "content": "привет"}])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_openrouter_sends_title_and_openai_does_not(self) -> None:
        seen: list = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.headers.get("X-Title"))
                body = json.dumps({"data": [{"id": "demo"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            client = RoutingLlmClient(timeout=2, complete_timeout=2)
            base = f"http://127.0.0.1:{port}/v1"
            client.list_models(domain.LlmProfile(
                "m-1", "роутер", base, "demo",
                api_key="key", api_kind=domain.LlmApiKind.OPENROUTER,
            ))
            client.list_models(domain.LlmProfile(
                "m-2", "облако", base, "demo",
                api_key="key", api_kind=domain.LlmApiKind.OPENAI,
            ))
            self.assertEqual(seen, ["vterminal", None])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cancel_interrupts_a_hanging_completion(self) -> None:
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                release.wait(30)

            def log_message(self, fmt: str, *args) -> None:
                return None

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stop = domain.ChatStop()
        token = domain.bind_chat_stop(stop)
        try:
            port = server.server_address[1]
            profile = domain.LlmProfile(
                "m-1", "локальная", f"http://127.0.0.1:{port}/v1", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
            client = LmStudioClient(timeout=2, complete_timeout=8)
            threading.Timer(0.2, stop.cancel).start()
            started = time.monotonic()
            with self.assertRaises(domain.ChatStopped):
                client.complete(profile, [("user", "привет")])
            self.assertLess(time.monotonic() - started, 3)
        finally:
            domain.reset_chat_stop(token)
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class ServiceTests(unittest.TestCase):
    def test_save_requires_model_in_server_list(self) -> None:
        service, _driver, _journal, profiles, _client = _service()
        with self.assertRaises(ConnectionError):
            service.save_llm_profile(
                domain.LlmProfile(
                    "", "локальная", "http://127.0.0.1:1234/v1", "нет",
                    api_kind=domain.LlmApiKind.LM_STUDIO,
                )
            )
        self.assertEqual(profiles, {})
        saved = service.save_llm_profile(
            domain.LlmProfile(
                "", "локальная", "http://127.0.0.1:1234/v1/", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
        )
        self.assertTrue(saved.profile_id)
        self.assertEqual(saved.base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual(saved.api_kind, domain.LlmApiKind.LM_STUDIO)
        self.assertEqual(len(profiles), 1)
        with self.assertRaises(ValueError):
            service.save_llm_profile(
                domain.LlmProfile("", "облако", "https://api.openai.com/v1", "gpt-4.1")
            )
        self.assertEqual(len(profiles), 1)

    def test_chat_uses_only_chat_and_model_failure_keeps_terminal(self) -> None:
        client = _Client()
        service, driver, journal, profiles, _client = _service(client)
        saved = service.save_llm_profile(
            domain.LlmProfile(
                "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
        )
        info = service.create_session(
            "работа", "pw", llm_profile_id=saved.profile_id
        )
        service._handle_driver_event(
            domain.OutputEvent(
                session_id=info.session_id,
                event_type=domain.EventType.COMMAND_SENT,
                timestamp=datetime.now(),
                stream_type=domain.StreamType.SYSTEM,
                payload="ls\n",
                persist=True,
            )
        )
        service.send_chat(info.session_id, "что это")
        sent = client.seen[-1]
        self.assertEqual(sent[0][0], "system")
        self.assertIn("одна команда", sent[0][1])
        self.assertIn("OLD:", sent[0][1])
        self.assertEqual(sent[-1], ("user", "что это"))
        dialog = [item for item in sent if item[0] != "system"]
        self.assertNotIn("ls", json.dumps(dialog, ensure_ascii=False))

        client.fail = True
        with self.assertRaises(LlmUnavailable):
            service.send_chat(info.session_id, "ещё")
        self.assertEqual(service.session_state(info.session_id), "ready")
        self.assertEqual(service.session_problem(info.session_id), "")
        self.assertIn("молчит", service.llm_problem(info.session_id))
        self.assertFalse(driver.closed)

        service._drivers.pop(info.session_id)
        service._offline_reason[info.session_id] = "SSH-соединение разорвано"
        client.fail = False
        service.send_chat(info.session_id, "терминал лежит")
        self.assertEqual(service.session_state(info.session_id), "offline")
        self.assertEqual(service.llm_problem(info.session_id), "")
        kinds = [event.event_type for event in journal.events]
        self.assertIn(domain.EventType.CHAT_ASSISTANT, kinds)

        profiles.clear()
        with self.assertRaises(LlmUnavailable):
            service.send_chat(info.session_id, "где модель")
        self.assertEqual(service.llm_problem(info.session_id), "Сохранённая модель не найдена")
        self.assertEqual(service.session_state(info.session_id), "offline")

    def test_stop_ends_the_turn_without_an_error_banner(self) -> None:
        class Client:
            def list_models(self, profile):
                return ["demo"]

            def complete(self, profile, messages):
                raise AssertionError("ожидался complete_turn")

            def complete_turn(self, profile, messages, tools, allow_tools=True):
                raise domain.ChatStopped()

        service, _driver, journal, _profiles, _client = _service(Client())  # type: ignore[arg-type]
        saved = service.save_llm_profile(
            domain.LlmProfile(
                "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
        )
        info = service.create_session("работа", "pw", llm_profile_id=saved.profile_id)
        service.send_chat(info.session_id, "стоп")
        self.assertEqual(service.llm_problem(info.session_id), "")
        kinds = [event.event_type for event in journal.events]
        self.assertIn(domain.EventType.CHAT_USER, kinds)
        self.assertNotIn(domain.EventType.CHAT_ASSISTANT, kinds)

    def test_reset_context_hides_earlier_chat_from_the_model(self) -> None:
        client = _Client()
        service, _driver, journal, _profiles, _client = _service(client)
        saved = service.save_llm_profile(
            domain.LlmProfile(
                "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
        )
        info = service.create_session("работа", "pw", llm_profile_id=saved.profile_id)
        service.send_chat(info.session_id, "старое")
        before = service.chat_context(info.session_id)
        self.assertGreater(before.messages, 0)
        self.assertGreater(before.tokens, 0)
        service.reset_chat_context(info.session_id)
        after = service.chat_context(info.session_id)
        self.assertEqual(after.messages, 0)
        self.assertLess(after.characters, before.characters)
        service.send_chat(info.session_id, "новое")
        texts = [text for role, text in client.seen[-1] if role != "system"]
        self.assertEqual(texts, ["новое"])
        self.assertTrue(
            any(
                event.event_type == domain.EventType.CHAT_USER and event.payload == "старое"
                for event in journal.events
            )
        )


class OsPromptTests(unittest.TestCase):
    def test_windows_and_linux_lines_are_kept(self) -> None:
        self.assertEqual(
            _clean_os_output("\nMicrosoft Windows [Version 10.0.26200.8037]\n"),
            "Microsoft Windows [Version 10.0.26200.8037]",
        )
        self.assertEqual(
            _clean_os_output("Linux 6.8.0-45-generic x86_64\n"),
            "Linux 6.8.0-45-generic x86_64",
        )
        self.assertEqual(_clean_os_output("ver\n"), "")
        self.assertEqual(
            _collect_addresses(
                "IPv4 Address. . . . . . . . . . . : 192.168.1.20\n"
                "IPv4-адрес . . . . . . . . . . . . : 10.0.0.5\n"
                "inet6 2001:db8::20/64\n"
                "0.0.0.0\n"
                "999.1.1.1\n"
            ),
            ["192.168.1.20", "10.0.0.5", "2001:db8::20"],
        )
        self.assertEqual(
            _collect_addresses(
                "Physical Address. . . . . . . . . : 00-1A-2B-3C-4D-5E\n"
                "IPv4 Address. . . . . . . . . . . : 192.168.1.20\n"
                "Subnet Mask . . . . . . . . . . . : 255.255.255.0\n"
                "Default Gateway . . . . . . . . . : 192.168.1.1\n"
                "DNS Servers . . . . . . . . . . . : 8.8.8.8\n"
            ),
            ["192.168.1.20"],
        )
        self.assertEqual(
            _collect_addresses(
                "link/ether b8:27:eb:1a:2b:3c brd ff:ff:ff:ff:ff:ff\n"
                "inet 192.168.1.20/24 brd 192.168.1.255 scope global eth0\n"
                "inet6 fe80::ba27:ebff:fe1a:2b3c/64 scope link\n"
                "inet 127.0.0.1/8 scope host lo\n"
            ),
            ["192.168.1.20", "fe80::ba27:ebff:fe1a:2b3c", "127.0.0.1"],
        )
        self.assertEqual(
            _collect_addresses(
                "inet 10.0.0.5 netmask 255.255.255.0 broadcast 10.0.0.255\n"
                "ether b8:27:eb:1a:2b:3c\n"
            ),
            ["10.0.0.5"],
        )
        self.assertEqual(
            _collect_addresses("192.168.1.20 2001:db8::20\n"),
            ["192.168.1.20", "2001:db8::20"],
        )
        self.assertEqual(
            _clean_stored_addresses(
                "192.168.1.20, 255.255.255.0, b8:27:eb:1a:2b:3c, 10.0.0.5"
            ),
            "192.168.1.20, 10.0.0.5",
        )

    def test_new_chat_asks_once_and_old_chat_stays_empty(self) -> None:
        service, driver, journal, _profiles, client = _service()
        saved = service.save_llm_profile(
            domain.LlmProfile(
                "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo",
                api_kind=domain.LlmApiKind.LM_STUDIO,
            )
        )
        driver.os_output = (
            "\nMicrosoft Windows [Version 10.0.26200.8037]\n"
            "   IPv4 Address. . . . . . . . . . . : 192.168.1.20\n"
            "val_g\n"
        )
        info = service.create_session(
            "новая", "pw", provider=domain.ProviderType.CMD, llm_profile_id=saved.profile_id
        )
        self.assertEqual(driver.sent, ["ver", "ipconfig", "whoami"])
        stored_new = service.get_session(info.session_id)
        self.assertNotIn("IP:", stored_new.mcp_description)
        self.assertEqual(stored_new.ip_addresses, "192.168.1.20")
        self.assertEqual(stored_new.username, "val_g")
        prompt = service._system_prompt(info.session_id)
        self.assertIn(
            "ОС этой сессии: Microsoft Windows [Version 10.0.26200.8037]",
            prompt,
        )
        self.assertIn("IP этой сессии: 192.168.1.20", prompt)
        self.assertIn("Пользователь этой сессии: val_g", prompt)
        service._maybe_probe_os(info.session_id)
        self.assertEqual(driver.sent, ["ver", "ipconfig", "whoami"])
        self.assertIn("Microsoft Windows", service.get_session(info.session_id).os_name)
        service.send_chat(info.session_id, "привет")
        system = client.seen[-1][0]
        self.assertEqual(system[0], "system")
        self.assertIn("Microsoft Windows", system[1])
        self.assertEqual(
            [item for item in client.seen[-1] if item[0] != "system"],
            [("user", "привет")],
        )
        service.reset_chat_context(info.session_id)
        service.send_chat(info.session_id, "после сброса")
        system = client.seen[-1][0]
        self.assertEqual(system[0], "system")
        self.assertIn("Microsoft Windows", system[1])
        self.assertIn("192.168.1.20", system[1])
        self.assertIn("val_g", system[1])
        self.assertEqual(
            [item for item in client.seen[-1] if item[0] != "system"],
            [("user", "после сброса")],
        )
        kinds = [event.event_type for event in journal.get_by_session(info.session_id)]
        self.assertEqual(kinds.count(domain.EventType.CHAT_OS), 1)
        self.assertNotIn(domain.EventType.COMMAND_SENT, kinds)

        plain = service.create_session("без модели", "pw", provider=domain.ProviderType.CMD)
        self.assertEqual(driver.sent[-1], "whoami")
        self.assertIn("Microsoft Windows", service.get_session(plain.session_id).os_name)
        service._append_chat(plain.session_id, domain.EventType.CHAT_USER, "давно")
        before = list(driver.sent)
        service._maybe_probe_os(plain.session_id)
        self.assertEqual(driver.sent, before)

        driver.os_output = ""
        unknown = service.create_session("пустая", "pw", provider=domain.ProviderType.CMD)
        self.assertEqual(
            driver.sent[len(before):],
            ["ver", "cmd /c ver", "uname -srm"],
        )
        self.assertEqual(service.get_session(unknown.session_id).os_name, "")
        self.assertIn("не определена", service._system_prompt(unknown.session_id))

        driver.os_output = "Linux 6.8.0-45-generic x86_64\n"
        stored = service.get_session(unknown.session_id)
        assert stored is not None
        service._sessions[unknown.session_id] = domain.SessionInfo(
            session_id=stored.session_id,
            name=stored.name,
            provider=domain.ProviderType.SSH,
            identity=stored.identity,
            status=stored.status,
            ssh=stored.ssh,
        )
        service._catalog.save(service._sessions[unknown.session_id])
        service._maybe_probe_os(unknown.session_id)
        self.assertEqual(driver.sent[-1], "id -un")
        self.assertIn(
            "Linux 6.8.0-45-generic x86_64",
            service._system_prompt(unknown.session_id),
        )


class ChatUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_tool_failure_banner_names_the_unfinished_reply(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.chat_pane import ChatPane
        from ui.styles import build_mono_font

        pane = ChatPane(build_mono_font())
        pane.show()
        QApplication.processEvents()
        pane.set_problem(
            "Модель вызвала инструмент, но команда не разобрана. "
            "Окно подтверждения не открывалось.",
            "Ответ не завершён",
        )
        QApplication.processEvents()
        self.assertTrue(pane._banner.isVisible())
        self.assertEqual(pane._banner_title.text(), "Ответ не завершён")
        self.assertFalse(pane._retry.isVisible())
        pane.set_busy(True)
        self.assertEqual(pane._send.text(), "Остановить")
        self.assertEqual(pane._send.objectName(), "stopButton")
        self.assertTrue(pane._send.isEnabled())
        self.assertTrue(pane.is_busy())
        pane.set_busy(False)
        self.assertEqual(pane._send.text(), "Отправить")
        self.assertTrue(pane._send.isEnabled())
        pane.close()

    def test_new_model_form_defaults_to_openai(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.models_dialog import ProfileFormDialog

        dialog = ProfileFormDialog(object())  # type: ignore[arg-type]
        dialog.show()
        QApplication.processEvents()
        self.assertEqual(dialog._selected_kind(), domain.LlmApiKind.OPENAI)
        self.assertEqual(dialog.url_edit.text(), "https://api.openai.com/v1")
        self.assertEqual(dialog.key_edit.placeholderText(), "Обязательно")
        dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("lm_studio"))
        QApplication.processEvents()
        self.assertEqual(dialog.url_edit.text(), "http://127.0.0.1:1234/v1")
        dialog.url_edit.setText("http://10.0.0.2:1234/v1")
        dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("openai"))
        QApplication.processEvents()
        self.assertEqual(dialog.url_edit.text(), "http://10.0.0.2:1234/v1")
        dialog.close()

    def test_settings_hold_models_rules_and_mcp(self) -> None:
        from PySide6.QtWidgets import QApplication, QTabWidget

        from ui.settings_dialog import SettingsDialog

        class Stub:
            def list_llm_profiles(self):
                return []

            def command_rule_lists(self):
                return {}

            def mcp_listen(self):
                return ("127.0.0.1", "47321-47340")

            def mcp_endpoint(self, session_id=None):
                return "http://127.0.0.1:47321/mcp"

        dialog = SettingsDialog(Stub())  # type: ignore[arg-type]
        dialog.show()
        QApplication.processEvents()
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        self.assertEqual(
            [tabs.tabText(index) for index in range(tabs.count())],
            ["MCP", "Ключ SSH", "Модели", "Правила"],
        )
        self.assertEqual(dialog._host.text(), "127.0.0.1")
        self.assertEqual(dialog._ports.text(), "47321-47340")
        self.assertFalse(dialog._exposure.isVisible())
        dialog._host.setText("10.0.0.8")
        QApplication.processEvents()
        self.assertTrue(dialog._exposure.isVisible())
        dialog.close()

    def test_split_attach_and_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog

        from ui.command_card import CommandCard
        from ui.dialogs import CreateSessionDialog
        from ui.main_window import MainWindow
        from ui.styles import build_text_formats

        profile = domain.LlmProfile("m-1", "локальная", "http://127.0.0.1:1234/v1", "demo")

        class FakeService:
            def __init__(self) -> None:
                self.sessions = [
                    domain.SessionInfo(
                        session_id="a",
                        name="A",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                        llm_profile_id="m-1",
                    ),
                    domain.SessionInfo(
                        session_id="b",
                        name="B",
                        provider=domain.ProviderType.SSH,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                    ),
                ]
                self.profiles = {"m-1": profile}
                self.state = {"a": "offline", "b": "ready"}

            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return list(self.sessions)

            def session_state(self, session_id: str) -> str:
                return self.state.get(session_id, "closed")

            def session_problem(self, session_id: str) -> str:
                if self.state.get(session_id) == "offline":
                    return "SSH-соединение разорвано"
                return ""

            def get_session(self, session_id: str):
                for info in self.sessions:
                    if info.session_id == session_id:
                        return info
                return None

            def get_history(self, session_id: str):
                return []

            def get_llm_profile(self, profile_id: str):
                return self.profiles.get(profile_id)

            def llm_problem(self, session_id: str) -> str:
                return ""

            def list_llm_profiles(self):
                return list(self.profiles.values())

            def send_command(self, session_id: str, text: str) -> str:
                raise AssertionError("терминал не должен получить команду из чата")

        service = FakeService()
        window = MainWindow(service)
        self.assertIn("Настройки", window.settings_button.toolTip())
        self.assertFalse(hasattr(window, "models_button"))
        self.assertFalse(hasattr(window, "rules_button"))
        window._activate_session("a")
        self.assertFalse(window._offline_banner.isHidden())
        self.assertFalse(window.chat_pane.isHidden())
        self.assertIn("разорвано", window._offline_detail.text())
        self.assertEqual(window.chat_pane._problem.text(), "")

        formats = build_text_formats()
        card = CommandCard("ls", formats.mono)
        card.append_output("file.txt\n", formats.stdout)
        window._add_history_widget(card)
        self.assertFalse(card.attach_button.isHidden())
        card.attach_button.click()
        draft = window.chat_pane.input_text()
        self.assertIn("$ ls", draft)
        self.assertIn("file.txt", draft)
        self.assertTrue(draft.strip().startswith("```terminal\n"))
        self.assertTrue(draft.strip().endswith("```"))

        window._activate_session("b")
        self.assertTrue(window.chat_pane.isHidden())
        window._activate_session("a")
        self.assertEqual(window.chat_pane.input_text(), draft)

        service.profiles.clear()
        window._sync_chat("a")
        self.assertIn("не найдена", window.chat_pane._problem.text())
        self.assertFalse(window.chat_pane.isHidden())

        dialog = CreateSessionDialog(service, None)
        index = dialog.provider_combo.findData("llm")
        self.assertGreaterEqual(index, 0)
        dialog.provider_combo.setCurrentIndex(index)
        self.assertFalse(dialog._ok_button.isEnabled())
        self.assertFalse(dialog._llm_empty.isHidden())
        dialog.close()
        window.close()
        self.assertIsNotNone(QDialog)

    def test_shell_blocks_and_thinking(self) -> None:
        from PySide6.QtWidgets import QApplication, QPushButton

        from ui.chat_pane import ChatPane, split_shell_blocks
        from ui.command_card import wrap_block
        from ui.main_window import MainWindow
        from ui.styles import build_mono_font

        wrapped = wrap_block("terminal", "see ``` inside")
        self.assertTrue(wrapped.startswith("````terminal\n"))
        self.assertTrue(wrapped.endswith("````"))

        parts = split_shell_blocks(
            "смотри\n```bash\nls -la\n```\n```python\nprint(1)\n```"
        )
        self.assertEqual(parts[1], ("shell", "ls -la"))
        self.assertTrue(any(kind == "text" and "python" in payload for kind, payload in parts))
        self.assertFalse(any(kind == "shell" and "print" in payload for kind, payload in parts))

        pane = ChatPane(build_mono_font())
        pane.show()
        runs = []
        inserts = []
        pane.run_command.connect(runs.append)
        pane.insert_command.connect(inserts.append)
        pane.append_message("assistant", "```bash\necho hi\n```")
        pane.append_message("assistant", "```python\nprint(1)\n```")
        run_buttons = [
            button for button in pane.findChildren(QPushButton) if button.text() == "Выполнить"
        ]
        insert_buttons = [
            button for button in pane.findChildren(QPushButton) if button.text() == "Вставить"
        ]
        self.assertEqual(len(run_buttons), 1)
        self.assertEqual(len(insert_buttons), 1)
        run_buttons[0].click()
        insert_buttons[0].click()
        edits = []
        pane.apply_edit.connect(lambda path, old, new: edits.append((path, old, new)))
        pane.append_message(
            "assistant",
            "```edit\npath: note.txt\nOLD:\ntimeout = 5\nNEW:\ntimeout = 15\n```",
        )
        replace_buttons = [
            button for button in pane.findChildren(QPushButton) if button.text() == "Заменить"
        ]
        self.assertEqual(len(replace_buttons), 1)
        replace_buttons[0].click()
        self.assertEqual(edits, [("note.txt", "timeout = 5", "timeout = 15")])
        self.assertEqual(runs, ["echo hi"])
        self.assertEqual(inserts, ["echo hi"])

        pane.set_thinking(True)
        self.assertFalse(pane._thinking.isHidden())
        self.assertEqual(pane._thinking.text(), "Думает")
        pane.append_message("user", "вопрос")
        self.assertLess(
            pane._messages.indexOf(pane._messages.itemAt(0).widget()),
            pane._messages.indexOf(pane._thinking),
        )
        pane._advance_thinking()
        self.assertEqual(pane._thinking.text(), "Думает.")
        pane.set_thinking(False)
        self.assertTrue(pane._thinking.isHidden())
        pane.close()

        class ReadyService:
            def __init__(self) -> None:
                self.sent = []
                self.sessions = [
                    domain.SessionInfo(
                        session_id="a",
                        name="A",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                        llm_profile_id="m-1",
                    )
                ]

            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return list(self.sessions)

            def session_state(self, session_id: str) -> str:
                return "ready"

            def session_problem(self, session_id: str) -> str:
                return ""

            def get_session(self, session_id: str):
                return self.sessions[0]

            def get_history(self, session_id: str):
                return []

            def get_llm_profile(self, profile_id: str):
                return domain.LlmProfile(
                    "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo"
                )

            def llm_problem(self, session_id: str) -> str:
                return ""

            def list_llm_profiles(self):
                return []

            def send_command(self, session_id: str, text: str) -> str:
                self.sent.append((session_id, text))
                return "c-1"

        service = ReadyService()
        window = MainWindow(service)
        window.show()
        window._activate_session("a")
        QApplication.processEvents()
        window.chat_pane.insert_command.emit("echo edit")
        QApplication.processEvents()
        self.assertEqual(window.input.text(), "echo edit")
        self.assertTrue(window.input.hasFocus())
        window.chat_pane.run_command.emit("echo run")
        self.assertEqual(service.sent, [("a", "echo run")])
        window.close()

    def test_assistant_bubble_hugs_text(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QFontMetrics, QTextCursor
        from PySide6.QtWidgets import QApplication, QTextEdit

        from ui.chat_pane import ChatPane
        from ui.styles import STYLESHEET, build_mono_font

        self._app.setStyleSheet(STYLESHEET)
        pane = ChatPane(build_mono_font())
        pane.resize(380, 640)
        pane.show()
        text = "\n".join(
            f"строка {i} достаточно длинная чтобы занять свою высоту" for i in range(6)
        )
        pane.append_message("assistant", "\n\n" + text + "\n\n")
        QApplication.processEvents()
        label = pane.findChild(QTextEdit, "chatAssistant")
        self.assertIsNotNone(label)
        assert label is not None
        self.assertTrue(label.isReadOnly())
        cursor = label.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        label.setTextCursor(cursor)
        self.assertTrue(label.textCursor().hasSelection())
        self.assertFalse(label.text().startswith("\n"))
        wrapped = QFontMetrics(label.font()).boundingRect(
            0,
            0,
            max(40, label.width() - 20),
            4000,
            int(Qt.TextFlag.TextWordWrap),
            label.text(),
        )
        self.assertGreaterEqual(label.height(), wrapped.height())
        self.assertLess(label.height(), wrapped.height() + 64)
        pane.set_input_text("\n".join(f"строка {i}" for i in range(12)))
        QApplication.processEvents()
        self.assertGreater(pane._input.height(), pane._input_min_height())
        self.assertLessEqual(pane._input.height(), pane._input_max_height())
        composer = pane._input.parentWidget().layout().itemAt(0).layout()
        siblings = [composer.itemAt(i).widget() for i in range(composer.count())]
        self.assertIs(siblings[0], pane._input)
        self.assertIs(siblings[1], pane._send)
        pane.close()
        self._app.setStyleSheet("")

    def test_reply_scrolls_to_the_bottom(self) -> None:
        from PySide6.QtWidgets import QApplication, QTextEdit

        from ui.chat_pane import ChatPane
        from ui.styles import build_mono_font

        pane = ChatPane(build_mono_font())
        pane.resize(320, 180)
        pane.show()
        pane.append_message("user", "\n".join(f"строка {i}" for i in range(40)))
        pane.append_message("assistant", "начало ответа")
        for _ in range(4):
            QApplication.processEvents()
        label = pane.findChild(QTextEdit, "chatAssistant")
        self.assertIsNotNone(label)
        bar = pane._scroll.verticalScrollBar()
        self.assertEqual(bar.value(), bar.maximum())
        pane.close()

    def test_deleting_live_command_unlocks_input(self) -> None:
        from ui.command_card import CommandCard
        from ui.main_window import MainWindow
        from ui.styles import build_text_formats

        class Service:
            def __init__(self) -> None:
                self.state = "running"
                self.cancelled = []
                self.sessions = [
                    domain.SessionInfo(
                        session_id="a",
                        name="A",
                        provider=domain.ProviderType.CMD,
                        identity=domain.ExecutionIdentityType.CURRENT_USER,
                        status=domain.SessionStatus.READY,
                    )
                ]

            def subscribe(self, callback) -> None:
                return None

            def unsubscribe(self, callback) -> None:
                return None

            def close_all(self) -> None:
                return None

            def list_sessions(self):
                return list(self.sessions)

            def session_state(self, session_id: str) -> str:
                return self.state

            def session_problem(self, session_id: str) -> str:
                return ""

            def get_session(self, session_id: str):
                return self.sessions[0]

            def get_history(self, session_id: str):
                return []

            def cancel_command(self, session_id: str) -> None:
                self.cancelled.append(session_id)
                self.state = "ready"

            def send_command(self, session_id: str, text: str) -> str:
                return "c"

            def complete(self, session_id: str, text: str):
                return []

            def write_input(self, session_id: str, data: str) -> None:
                return None

            def resize(self, session_id: str, cols: int, rows: int) -> None:
                return None

        service = Service()
        window = MainWindow(service)
        window._activate_session("a")
        self.assertFalse(window.input.isEnabled())
        card = CommandCard("nano", build_text_formats().mono)
        window._live_cards["a"] = card
        window._on_card_remove(card)
        self.assertEqual(service.cancelled, ["a"])
        self.assertTrue(window.input.isEnabled())
        window.close()
