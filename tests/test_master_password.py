# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Пароль журнала: последняя машина или мастер-пароль запуска."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import domain
from application.master_password import MasterPassword, connection_journal_password
from ui.dialogs import MasterPasswordDialog, ask_journal_password


class _Master:
    def __init__(self) -> None:
        self._inner = MasterPassword()

    def has_master_password(self) -> bool:
        return self._inner.ready()

    def set_master_password(self, password: str, confirmation: str) -> None:
        self._inner.establish(password, confirmation)

    def master_password(self) -> str:
        return self._inner.reveal()


class JournalPasswordTests(unittest.TestCase):
    def test_last_machine_password_opens_the_journal(self) -> None:
        ssh = domain.SSHConfig(
            host="jump",
            username="root",
            auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
            hops=(
                domain.SSHHop(
                    host="target",
                    username="app",
                    auth_method=domain.SSHAuthMethod.PASSWORD,
                ),
            ),
        )
        self.assertEqual(
            connection_journal_password(
                domain.ProviderType.SSH_OVER_SSH, ssh, "key-pass", ("target-secret",)
            ),
            "target-secret",
        )
        self.assertIsNone(
            connection_journal_password(domain.ProviderType.CMD, None, None)
        )
        password_ssh = domain.SSHConfig(host="10.0.0.1", username="root")
        self.assertIsNone(
            connection_journal_password(
                domain.ProviderType.SSH,
                password_ssh,
                "machine",
                install_key=(True,),
            )
        )
        self.assertEqual(
            connection_journal_password(
                domain.ProviderType.SSH, password_ssh, "machine"
            ),
            "machine",
        )

    def test_master_password_is_checked_by_hash_once(self) -> None:
        master = MasterPassword()
        with self.assertRaises(ValueError):
            master.establish("one", "two")
        self.assertFalse(master.ready())
        master.establish("one", "one")
        self.assertEqual(master.reveal(), "one")
        master.forget_unproven()
        self.assertFalse(master.ready())
        master.establish("one", "one")
        master.prove()
        master.forget_unproven()
        self.assertEqual(master.reveal(), "one")
        master.reset()
        self.assertFalse(master.ready())
        master.establish("other", "other")
        self.assertEqual(master.reveal(), "other")
        with self.assertRaises(ValueError):
            master.establish("nope", "nope")

    def test_dialog_keeps_the_master_password_for_the_process(self) -> None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        service = _Master()
        dialog = MasterPasswordDialog(service)  # type: ignore[arg-type]
        dialog.show()
        app.processEvents()
        dialog._password.setText("alpha")
        dialog._confirm.setText("beta")
        dialog._on_ok()
        app.processEvents()
        self.assertTrue(dialog._error.isVisible())
        self.assertIn("не совпадают", dialog._error.text())
        self.assertFalse(service.has_master_password())

        dialog._confirm.setText("alpha")
        dialog._on_ok()
        self.assertFalse(dialog.isVisible())
        self.assertEqual(service.master_password(), "alpha")
        self.assertEqual(
            ask_journal_password(
                service,  # type: ignore[arg-type]
                domain.ProviderType.CMD,
                None,
                None,
                (),
            ),
            "alpha",
        )
        dialog.close()
