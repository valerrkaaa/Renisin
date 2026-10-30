# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Цепочка SSH: каталог без паролей, проверка узлов и кнопка «+»."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.service import ApplicationService
from storage.catalog import FileSessionCatalog


class _Journal:
    def __init__(self) -> None:
        self.unlocked = set()
        self.deleted = []
        self.next = 0

    def create(self, session_id: str, password: str) -> None:
        self.unlocked.add(session_id)

    def delete(self, session_id: str) -> None:
        self.deleted.append(session_id)
        self.unlocked.discard(session_id)

    def next_sequence(self, session_id: str) -> int:
        return self.next

    def is_unlocked(self, session_id: str) -> bool:
        return session_id in self.unlocked

    def get_by_session(self, session_id: str):
        return []


class _Catalog:
    def __init__(self) -> None:
        self.items = {}

    def save(self, info: domain.SessionInfo) -> None:
        self.items[info.session_id] = info

    def get(self, session_id: str):
        return self.items.get(session_id)

    def list(self):
        return list(self.items.values())

    def delete(self, session_id: str) -> None:
        self.items.pop(session_id, None)


class _Driver:
    def __init__(self) -> None:
        self.config = None

    def set_event_sink(self, sink) -> None:
        return None

    def open(self, config: domain.SessionConfig) -> None:
        self.config = config

    def close(self) -> None:
        return None


def _chain() -> domain.SSHConfig:
    return domain.SSHConfig(
        host="10.0.0.1",
        username="jump",
        hops=(domain.SSHHop(host="10.0.0.2", username="app"),),
    )


class ChainRulesTests(unittest.TestCase):
    def test_targets_keep_the_order(self) -> None:
        targets = domain.ssh_targets(_chain())
        self.assertEqual(
            [(item.username, item.host) for item in targets],
            [("jump", "10.0.0.1"), ("app", "10.0.0.2")],
        )
        self.assertEqual(
            domain.ssh_location(_chain()),
            "jump@10.0.0.1:22 → app@10.0.0.2:22",
        )
        keyed = domain.SSHConfig(
            host="10.0.0.1",
            username="jump",
            auth_method=domain.SSHAuthMethod.AGENT,
            hops=(domain.SSHHop(
                host="10.0.0.2",
                username="app",
                auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                private_key_path="C:/keys/app",
            ),),
        )
        second = domain.ssh_targets(keyed)[1]
        self.assertEqual(second.auth_method, domain.SSHAuthMethod.PRIVATE_KEY)
        self.assertEqual(second.private_key_path, "C:/keys/app")

    def test_catalog_stores_hops_without_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            catalog = FileSessionCatalog(os.path.join(folder, "sessions.json"))
            info = domain.SessionInfo(
                session_id="s-chain",
                name="цепочка",
                provider=domain.ProviderType.SSH_OVER_SSH,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.CLOSED,
                ssh=_chain(),
                llm_profile_id="m-1",
            )
            catalog.save(info)
            loaded = catalog.get("s-chain")
            assert loaded is not None
            assert loaded.ssh is not None
            self.assertEqual(loaded.provider, domain.ProviderType.SSH_OVER_SSH)
            self.assertEqual(loaded.ssh.hops[0].username, "app")
            self.assertEqual(loaded.llm_profile_id, "m-1")
            with open(os.path.join(folder, "sessions.json"), encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("secret", raw)
            self.assertEqual(loaded.ssh.hops[0].auth_method, domain.SSHAuthMethod.PASSWORD)

            old_path = os.path.join(folder, "old.json")
            old = {
                "sessions": {
                    "s-old": {
                        "session_id": "s-old",
                        "name": "старая",
                        "provider": "ssh_over_ssh",
                        "identity": "current_user",
                        "status": "closed",
                        "ssh": {
                            "host": "10.0.0.1",
                            "username": "jump",
                            "hops": [{
                                "host": "10.0.0.2",
                                "port": 22,
                                "username": "app",
                            }],
                        },
                    }
                }
            }
            with open(old_path, "w", encoding="utf-8") as handle:
                json.dump(old, handle)
            aged = FileSessionCatalog(old_path).get("s-old")
            assert aged is not None and aged.ssh is not None
            self.assertEqual(aged.ssh.hops[0].auth_method, domain.SSHAuthMethod.PASSWORD)
            self.assertIsNone(aged.ssh.hops[0].private_key_path)

    def test_one_node_is_rejected_before_connect(self) -> None:
        opened = []

        def factory(provider):
            driver = _Driver()
            opened.append(driver)
            return driver

        service = ApplicationService(factory, _Catalog(), _Journal())
        with self.assertRaises(ValueError):
            service.create_session(
                "цепочка",
                "journal",
                provider=domain.ProviderType.SSH_OVER_SSH,
                ssh=domain.SSHConfig(host="10.0.0.1", username="jump"),
                auth_secret="pw",
            )
        self.assertEqual(opened, [])

    def test_create_passes_secrets_only_to_the_driver(self) -> None:
        driver = _Driver()
        catalog = _Catalog()
        service = ApplicationService(lambda _provider: driver, catalog, _Journal())
        info = service.create_session(
            "цепочка",
            "journal",
            provider=domain.ProviderType.SSH_OVER_SSH,
            ssh=_chain(),
            auth_secret="one",
            hop_secrets=("two",),
            llm_profile_id=None,
        )
        assert driver.config is not None
        self.assertEqual(driver.config.auth_secret, "one")
        self.assertEqual(driver.config.hop_secrets, ("two",))
        stored = catalog.get(info.session_id)
        assert stored is not None and stored.ssh is not None
        self.assertEqual(stored.ssh.hops[0].host, "10.0.0.2")
        self.assertFalse(hasattr(stored, "hop_secrets") and getattr(stored, "hop_secrets", None))

        keyed = domain.SSHConfig(
            host="10.0.0.1",
            username="jump",
            auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
            private_key_path="C:/keys/jump",
            hops=(domain.SSHHop(
                host="10.0.0.2",
                username="app",
                auth_method=domain.SSHAuthMethod.AGENT,
            ),),
        )
        service.create_session(
            "ключ",
            "journal",
            provider=domain.ProviderType.SSH_OVER_SSH,
            ssh=keyed,
            auth_secret=None,
            hop_secrets=("",),
        )
        assert driver.config is not None
        self.assertIsNone(driver.config.auth_secret)
        self.assertEqual(driver.config.hop_secrets, ("",))
        with self.assertRaises(ValueError):
            service.create_session(
                "пустой пароль",
                "journal",
                provider=domain.ProviderType.SSH_OVER_SSH,
                ssh=_chain(),
                auth_secret="one",
                hop_secrets=("",),
            )


class ChainDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_plus_adds_a_hop_for_plain_and_llm(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.dialogs import CreateSessionDialog

        class Service:
            def list_llm_profiles(self):
                return [
                    domain.LlmProfile("m-1", "локальная", "http://127.0.0.1:1234/v1", "demo")
                ]

        dialog = CreateSessionDialog(Service())  # type: ignore[arg-type]
        self.assertEqual(dialog.provider_combo.currentData(), "cmd")
        dialog.show()
        QApplication.processEvents()
        chain = dialog.provider_combo.findData("ssh_over_ssh")
        dialog.provider_combo.setCurrentIndex(chain)
        QApplication.processEvents()
        self.assertTrue(dialog._chain_container.isVisible())
        self.assertEqual(len(dialog._hop_rows), 1)
        self.assertFalse(dialog._hop_rows[0].remove_button.isVisible())
        dialog._add_hop.click()
        QApplication.processEvents()
        self.assertEqual(len(dialog._hop_rows), 2)
        self.assertEqual(dialog._hop_rows[1].title.text(), "Узел 2")
        self.assertTrue(dialog._hop_rows[0].remove_button.isVisible())

        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("ssh_over_ssh"))
        QApplication.processEvents()
        self.assertTrue(dialog._chain_container.isVisible())
        self.assertFalse(dialog._ssh_container.isVisible())
        self.assertEqual(dialog._hop_rows[0].port.value(), 22)
        dialog._hop_rows[0].host.setText("10.0.0.1")
        dialog._hop_rows[0].user.setText("jump")
        dialog._hop_rows[0].port.setValue(2222)
        dialog._hop_rows[0].password.setText("one")
        dialog._hop_rows[1].host.setText("10.0.0.2")
        dialog._hop_rows[1].user.setText("app")
        dialog._hop_rows[1].port.setValue(2200)
        dialog._hop_rows[1].auth.setCurrentIndex(
            dialog._hop_rows[1].auth.findData(domain.SSHAuthMethod.PRIVATE_KEY.value)
        )
        dialog._hop_rows[1].key_path.setText("C:/keys/app")
        self.assertEqual(
            dialog._chain_route.text(),
            "jump@10.0.0.1:2222 → app@10.0.0.2:2200",
        )
        request = dialog._chain_request("цепочка", "journal", "m-1")
        assert request is not None and request.ssh is not None
        self.assertEqual(request.ssh.port, 2222)
        self.assertEqual(request.ssh.auth_method, domain.SSHAuthMethod.PASSWORD)
        self.assertEqual(request.auth_secret, "one")
        self.assertEqual(request.ssh.hops[0].port, 2200)
        self.assertEqual(request.ssh.hops[0].auth_method, domain.SSHAuthMethod.PRIVATE_KEY)
        self.assertEqual(request.ssh.hops[0].private_key_path, "C:/keys/app")
        self.assertEqual(request.hop_secrets, ("",))
        dialog.close()

    def test_ok_shows_the_error_and_does_not_stay_busy(self) -> None:
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication

        from ui.dialogs import CreateSessionDialog, UnlockSessionDialog

        class Service:
            def list_llm_profiles(self):
                return [
                    domain.LlmProfile(
                        "m-1", "локальная", "http://127.0.0.1:1234/v1", "demo"
                    )
                ]

            def create_session(self, **_kwargs):
                raise ConnectionError("Нет маршрута до 10.0.0.1")

        dialog = CreateSessionDialog(Service())  # type: ignore[arg-type]
        dialog.resize(560, 640)
        dialog.show()
        QApplication.processEvents()
        dialog.provider_combo.setCurrentIndex(
            dialog.provider_combo.findData("ssh_over_ssh")
        )
        dialog.name_edit.setText("цепочка")
        dialog._add_hop.click()
        QApplication.processEvents()
        dialog._ok_button.click()
        QApplication.processEvents()
        self.assertTrue(dialog._error_label.isVisible())
        self.assertIn("IP", dialog._error_label.text())
        self.assertTrue(dialog._ok_button.isEnabled())
        self.assertNotEqual(dialog._ok_button.text(), "Подключение…")

        dialog._hop_rows[0].host.setText("10.0.0.1")
        dialog._hop_rows[0].user.setText("jump")
        dialog._hop_rows[0].password.setText("one")
        dialog._hop_rows[1].host.setText("10.0.0.2")
        dialog._hop_rows[1].user.setText("app")
        dialog._hop_rows[1].password.setText("two")
        dialog._ok_button.click()
        for _ in range(50):
            QApplication.processEvents()
            if "маршрута" in dialog._error_label.text():
                break
            QTest.qWait(20)
        self.assertIn("маршрута", dialog._error_label.text())
        self.assertTrue(dialog._error_label.isVisible())
        self.assertTrue(dialog._ok_button.isEnabled())
        self.assertNotEqual(dialog._ok_button.text(), "Подключение…")
        dialog.close()

        info = domain.SessionInfo(
            session_id="s-chain",
            name="цепочка",
            provider=domain.ProviderType.SSH_OVER_SSH,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.CLOSED,
            ssh=_chain(),
            llm_profile_id="m-1",
        )
        unlock = UnlockSessionDialog(Service(), info)  # type: ignore[arg-type]
        unlock.resize(560, 640)
        unlock.show()
        QApplication.processEvents()
        unlock._ok_button.click()
        QApplication.processEvents()
        self.assertTrue(unlock._error_label.isVisible())
        self.assertIn("пароль", unlock._error_label.text().lower())
        self.assertTrue(unlock._ok_button.isEnabled())
        self.assertNotEqual(unlock._ok_button.text(), "Подключение…")
        unlock.close()
