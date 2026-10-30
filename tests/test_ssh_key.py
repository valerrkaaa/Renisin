# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Ключ из настроек и галочка «Записать мой ключ» при новом SSH."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import domain
from application.service import ApplicationService
from application.ssh_key import (
    SETUP_REQUIRED,
    authorized_keys_command,
    generate_ed25519,
    public_line,
    require_configured_key,
)
from drivers.ssh import SSHSessionDriver
from storage.ssh_key import FileSshKeySettings


class _Journal:
    def create(self, session_id: str, password: str) -> None:
        return None

    def delete(self, session_id: str) -> None:
        return None

    def is_unlocked(self, session_id: str) -> bool:
        return False

    def next_sequence(self, session_id: str) -> int:
        return 0


class _Catalog:
    def __init__(self) -> None:
        self.items = {}

    def save(self, info: domain.SessionInfo) -> None:
        self.items[info.session_id] = info

    def get(self, session_id: str):
        return self.items.get(session_id)


class KeyFileTests(unittest.TestCase):
    def test_missing_settings_are_primary_setup(self) -> None:
        with self.assertRaises(ValueError) as caught:
            require_configured_key("")
        self.assertEqual(str(caught.exception), SETUP_REQUIRED)
        with self.assertRaises(ValueError) as missing:
            require_configured_key(os.path.join(tempfile.gettempdir(), "no-such-key"))
        self.assertEqual(str(missing.exception), SETUP_REQUIRED)

    def test_generate_refuses_overwrite_and_writes_public_line(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "id_ed25519")
            generate_ed25519(path)
            with self.assertRaises(ValueError):
                generate_ed25519(path)
            from application.ssh_key import load_private_key

            key = load_private_key(path, None)
            line = public_line(key)
            self.assertTrue(line.startswith("ssh-ed25519 "))
            self.assertTrue(line.endswith(" vterminal"))
            with open(path + ".pub", encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), line)
            command = authorized_keys_command(line)
            self.assertIn("umask 077", command)
            self.assertIn(line, command)

    def test_settings_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = FileSshKeySettings(os.path.join(folder, "ssh_key.json"))
            self.assertEqual(store.load(), "")
            store.save(r"C:\keys\id_ed25519")
            self.assertEqual(store.load(), r"C:\keys\id_ed25519")
            broken = os.path.join(folder, "bad.json")
            with open(broken, "w", encoding="utf-8") as handle:
                handle.write("{")
            self.assertEqual(FileSshKeySettings(broken).load(), "")


class ServiceKeyTests(unittest.TestCase):
    def _service(self, folder: str, driver) -> ApplicationService:
        return ApplicationService(
            lambda _provider: driver,
            _Catalog(),
            _Journal(),
            ssh_key_settings=FileSshKeySettings(os.path.join(folder, "ssh_key.json")),
        )

    def test_generate_is_remembered_and_missing_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = self._service(folder, object())
            service.set_master_password("one", "one")
            path = service.generate_ssh_key(os.path.join(folder, "id_ed25519"))
            self.assertEqual(service.ssh_key_path(), path)
            with self.assertRaises(ValueError):
                service.set_ssh_key_path(os.path.join(folder, "absent"))
            service.set_ssh_key_path("")
            self.assertEqual(service.ssh_key_path(), "")

    def test_key_needs_a_master_password_and_reset_drops_both(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service = self._service(folder, object())
            target = os.path.join(folder, "id_ed25519")
            with self.assertRaises(ValueError) as caught:
                service.generate_ssh_key(target)
            self.assertIn("мастер-пароль", str(caught.exception).lower())
            service.set_master_password("one", "one")
            service.prove_master_password()
            path = service.generate_ssh_key(target)
            service.reset_master_password()
            self.assertFalse(service.has_master_password())
            self.assertEqual(service.ssh_key_path(), path)
            self.assertTrue(service.ssh_key_uses_master_passphrase())
            self.assertTrue(os.path.isfile(path))
            from application.ssh_key import load_private_key

            service.set_master_password("one", "one")
            self.assertIsNotNone(load_private_key(path, "one"))
            ssh = domain.SSHConfig(
                host="10.0.0.1",
                username="root",
                auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                private_key_path=path,
            )
            secret, hops = service._with_master_passphrase(ssh, None, ())
            self.assertEqual(secret, "one")
            self.assertEqual(hops, ())
            info = domain.SessionInfo(
                session_id="s",
                name="n",
                provider=domain.ProviderType.SSH,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.CLOSED,
                ssh=ssh,
            )
            service.reset_master_password()
            self.assertFalse(service.has_master_password())
            self.assertTrue(service.opens_with_master_passphrase(info))
            cmd = domain.SessionInfo(
                session_id="c",
                name="console",
                provider=domain.ProviderType.CMD,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.CLOSED,
            )
            self.assertTrue(service.opens_with_master_passphrase(cmd))
            self.assertTrue(service.needs_master_password(info))
            self.assertTrue(service.needs_master_password(info, reconnect=True))
            service.set_master_password("one", "one")

            custom = os.path.join(folder, "custom")
            service.generate_ssh_key(custom, "phrase")
            self.assertFalse(service.ssh_key_uses_master_passphrase())
            self.assertIsNotNone(load_private_key(custom, "phrase"))
            typed, _hops = service._with_master_passphrase(
                domain.SSHConfig(
                    host="10.0.0.1",
                    username="root",
                    auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                    private_key_path=custom,
                ),
                "phrase",
                (),
            )
            self.assertEqual(typed, "phrase")

            from application.ssh_key import generate_ed25519

            plain = os.path.join(folder, "plain")
            generate_ed25519(plain)
            service.set_ssh_key_path(plain, passphrase_is_master=False)
            plain_ssh = domain.SSHConfig(
                host="10.0.0.1",
                username="root",
                auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                private_key_path=plain,
            )
            ignored, _hops = service._with_master_passphrase(plain_ssh, "one", ())
            self.assertIsNone(ignored)
            plain_info = domain.SessionInfo(
                session_id="s",
                name="n",
                provider=domain.ProviderType.SSH,
                identity=domain.ExecutionIdentityType.CURRENT_USER,
                status=domain.SessionStatus.CLOSED,
                ssh=plain_ssh,
            )
            self.assertTrue(service.opens_with_master_passphrase(plain_info))

    def test_install_flag_allows_empty_password_and_passes_the_settings_key(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            captured = {}

            class Driver:
                def set_event_sink(self, sink) -> None:
                    return None

                def open(self, config: domain.SessionConfig) -> None:
                    captured["config"] = config

                def resolved_ssh(self):
                    return domain.SSHConfig(
                        host="10.0.0.1",
                        username="root",
                        auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                        private_key_path=captured["config"].identity_key_path,
                    )

                def close(self) -> None:
                    return None

            catalog = _Catalog()
            service = ApplicationService(
                lambda _provider: Driver(),
                catalog,
                _Journal(),
                ssh_key_settings=FileSshKeySettings(os.path.join(folder, "ssh_key.json")),
            )
            service.set_master_password("one", "one")
            path = service.generate_ssh_key(os.path.join(folder, "id_ed25519"))
            ssh = domain.SSHConfig(host="10.0.0.1", username="root")
            info = service.create_session(
                "узел",
                "journal",
                provider=domain.ProviderType.SSH,
                ssh=ssh,
                auth_secret=None,
                install_key=(True,),
            )
            self.assertEqual(captured["config"].identity_key_path, path)
            self.assertEqual(captured["config"].install_key, (True,))
            stored = catalog.get(info.session_id)
            assert stored is not None and stored.ssh is not None
            self.assertEqual(stored.ssh.auth_method, domain.SSHAuthMethod.PRIVATE_KEY)
            self.assertEqual(stored.ssh.private_key_path, path)

            chain = domain.SSHConfig(
                host="10.0.0.1",
                username="jump",
                hops=(domain.SSHHop(host="10.0.0.2", username="app"),),
            )
            service._check_ssh(
                domain.ProviderType.SSH_OVER_SSH,
                chain,
                "",
                ("",),
                (True, True),
            )
            with self.assertRaises(ValueError):
                service._check_ssh(
                    domain.ProviderType.SSH_OVER_SSH,
                    chain,
                    "one",
                    ("",),
                    (False, False),
                )

    def test_connect_without_a_configured_key_fails_before_the_network(self) -> None:
        driver = SSHSessionDriver(lambda prompt: domain.HostKeyDecision(accept=False), "")
        config = domain.SessionConfig(
            session_id="s",
            name="n",
            provider=domain.ProviderType.SSH,
            ssh=domain.SSHConfig(host="10.0.0.1", username="root"),
            install_key=(True,),
            identity_key_path="",
        )
        with self.assertRaises(ValueError) as caught:
            driver._connect_chain(config)
        self.assertEqual(str(caught.exception), SETUP_REQUIRED)

    def test_rejected_key_uses_the_password_field_then_reconnects_with_the_key(self) -> None:
        driver = SSHSessionDriver(
            lambda prompt: domain.HostKeyDecision(accept=False),
            "",
            password_prompt=lambda prompt: "from-dialog",
        )
        calls = []

        def try_connect(ssh, secret, sock):
            calls.append(("probe", ssh.auth_method))
            return None

        def connect_one(ssh, secret, sock):
            calls.append(("connect", ssh.auth_method, secret))
            return type("Client", (), {"close": lambda self: None})()

        driver._try_connect = try_connect  # type: ignore[method-assign]
        driver._connect_one = connect_one  # type: ignore[method-assign]
        driver._load_identity = lambda path: object()  # type: ignore[method-assign]
        import drivers.ssh as sshmod

        original_line = sshmod.public_line
        original_install = sshmod.install_authorized_key
        sshmod.public_line = lambda key: "ssh-ed25519 AAAA vterminal"
        installed = []
        sshmod.install_authorized_key = lambda client, line: installed.append(line)
        try:
            target = domain.SSHConfig(host="10.0.0.1", username="root")
            resolved, client = driver._connect_installing(
                target, "from-field", r"C:\keys\id", lambda: None
            )
            self.assertIsNotNone(client)
            self.assertEqual(resolved.auth_method, domain.SSHAuthMethod.PRIVATE_KEY)
            self.assertEqual(resolved.private_key_path, r"C:\keys\id")
            self.assertEqual(installed, ["ssh-ed25519 AAAA vterminal"])
            self.assertEqual(
                calls,
                [
                    ("probe", domain.SSHAuthMethod.PRIVATE_KEY),
                    ("connect", domain.SSHAuthMethod.PASSWORD, "from-field"),
                    ("connect", domain.SSHAuthMethod.PRIVATE_KEY, None),
                ],
            )

            calls.clear()
            driver._connect_installing(target, None, r"C:\keys\id", lambda: None)
            self.assertEqual(calls[1][2], "from-dialog")
        finally:
            sshmod.public_line = original_line
            sshmod.install_authorized_key = original_install


class KeyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls._app = QApplication.instance() or QApplication([])

    def test_checkbox_is_only_for_password_auth(self) -> None:
        from PySide6.QtWidgets import QApplication

        from ui.dialogs import CreateSessionDialog

        class Service:
            def list_llm_profiles(self):
                return []

        dialog = CreateSessionDialog(Service())  # type: ignore[arg-type]
        dialog.show()
        QApplication.processEvents()
        dialog.provider_combo.setCurrentIndex(dialog.provider_combo.findData("ssh"))
        QApplication.processEvents()
        self.assertTrue(dialog.ssh_install.isVisible())
        dialog.ssh_install.setChecked(True)
        key = dialog.ssh_auth.findData(domain.SSHAuthMethod.PRIVATE_KEY.value)
        dialog.ssh_auth.setCurrentIndex(key)
        QApplication.processEvents()
        self.assertFalse(dialog.ssh_install.isVisible())
        self.assertFalse(dialog.ssh_install.isChecked())

        dialog.provider_combo.setCurrentIndex(
            dialog.provider_combo.findData("ssh_over_ssh")
        )
        dialog._add_hop.click()
        QApplication.processEvents()
        first, second = dialog._hop_rows
        self.assertTrue(first.install_key.isVisible())
        second.auth.setCurrentIndex(
            second.auth.findData(domain.SSHAuthMethod.PRIVATE_KEY.value)
        )
        QApplication.processEvents()
        self.assertFalse(second.install_key.isVisible())
        first.install_key.setChecked(True)
        first.host.setText("10.0.0.1")
        first.user.setText("jump")
        second.host.setText("10.0.0.2")
        second.user.setText("app")
        second.key_path.setText(r"C:\keys\id")
        dialog.name_edit.setText("цепочка")
        built = dialog._chain_request("цепочка", "", None)
        assert built is not None
        self.assertEqual(built.install_key, (True, False))
        dialog.close()

    def test_settings_tab_shows_the_saved_path(self) -> None:
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
                return ""

            def ssh_key_path(self):
                return r"C:\keys\id_ed25519"

        dialog = SettingsDialog(Stub())  # type: ignore[arg-type]
        dialog.show()
        QApplication.processEvents()
        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        self.assertIn("Ключ SSH", [tabs.tabText(index) for index in range(tabs.count())])
        self.assertEqual(dialog._key_path.text(), r"C:\keys\id_ed25519")
        dialog.close()

    def test_master_password_gates_adding_a_key(self) -> None:
        from PySide6.QtWidgets import QApplication, QMessageBox

        from ui.settings_dialog import SettingsDialog

        class Stub:
            def __init__(self) -> None:
                self.ready = False
                self.key = ""

            def list_llm_profiles(self):
                return []

            def command_rule_lists(self):
                return {}

            def mcp_listen(self):
                return ("127.0.0.1", "47321-47340")

            def mcp_endpoint(self, session_id=None):
                return ""

            def ssh_key_path(self):
                return self.key

            def has_master_password(self):
                return self.ready

            def set_master_password(self, password: str, confirmation: str) -> None:
                if password != confirmation or not password:
                    raise ValueError("Пароли не совпадают")
                self.ready = True

            def reset_master_password(self) -> None:
                self.ready = False
                self.key = ""

        stub = Stub()
        dialog = SettingsDialog(stub)  # type: ignore[arg-type]
        dialog.show()
        from PySide6.QtWidgets import QTabWidget

        tabs = dialog.findChild(QTabWidget)
        assert tabs is not None
        tabs.setCurrentIndex(1)
        QApplication.processEvents()
        self.assertTrue(dialog._master_set.isVisible())
        self.assertFalse(dialog._key_apply.isEnabled())
        dialog._master_password.setText("alpha")
        dialog._master_confirm.setText("beta")
        dialog._set_master()
        QApplication.processEvents()
        self.assertTrue(dialog._master_error.isVisible())
        self.assertIn("не совпадают", dialog._master_error.text())
        self.assertFalse(dialog._key_apply.isEnabled())

        dialog._master_confirm.setText("alpha")
        dialog._set_master()
        QApplication.processEvents()
        self.assertFalse(dialog._master_error.isVisible())
        self.assertTrue(dialog._key_apply.isEnabled())
        self.assertTrue(dialog._master_reset.isVisible())
        self.assertFalse(dialog._master_password.isVisible())

        original = QMessageBox.question
        QMessageBox.question = (  # type: ignore[method-assign]
            lambda *args, **kwargs: QMessageBox.StandardButton.Yes
        )
        try:
            dialog._reset_master()
        finally:
            QMessageBox.question = original  # type: ignore[method-assign]
        QApplication.processEvents()
        self.assertFalse(stub.ready)
        self.assertFalse(dialog._key_apply.isEnabled())
        self.assertTrue(dialog._master_set.isVisible())
        dialog.close()


def ssh_config_from_targets_keeps_each_node() -> None:
    rebuilt = domain.ssh_config_from_targets((
        domain.SSHConfig(
            host="10.0.0.1",
            username="jump",
            auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
            private_key_path=r"C:\keys\id",
        ),
        domain.SSHConfig(host="10.0.0.2", username="app"),
    ))
    assert rebuilt.auth_method == domain.SSHAuthMethod.PRIVATE_KEY
    assert rebuilt.hops[0].auth_method == domain.SSHAuthMethod.PASSWORD


class RebuildTests(unittest.TestCase):
    def test_targets_round_trip(self) -> None:
        ssh_config_from_targets_keeps_each_node()
