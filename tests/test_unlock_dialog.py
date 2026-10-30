# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Диалог открытия SSH-сессии: фокус и пароль журнала."""
from __future__ import annotations

import unittest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

import domain
from application.master_password import connection_journal_password
from ui.dialogs import UnlockSessionDialog, _ChainPasswordRow


class UnlockDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QApplication.instance() or QApplication([])

    def _ssh_info(
        self,
        method: domain.SSHAuthMethod = domain.SSHAuthMethod.PASSWORD,
    ) -> domain.SessionInfo:
        return domain.SessionInfo(
            session_id="s-ssh",
            name="box",
            provider=domain.ProviderType.SSH,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.CLOSED,
            ssh=domain.SSHConfig(host="h", username="root", auth_method=method),
        )

    def test_ssh_password_is_the_journal_password(self) -> None:
        info = self._ssh_info()
        dialog = UnlockSessionDialog(object(), info)  # type: ignore[arg-type]
        self.assertFalse(hasattr(dialog, "password_edit"))
        self.assertEqual(
            connection_journal_password(
                domain.ProviderType.SSH, info.ssh, "secret"
            ),
            "secret",
        )
        dialog.close()

    def test_key_auth_does_not_use_the_passphrase_as_journal(self) -> None:
        info = self._ssh_info(domain.SSHAuthMethod.PRIVATE_KEY)
        dialog = UnlockSessionDialog(object(), info)  # type: ignore[arg-type]
        self.assertIsNone(
            connection_journal_password(
                domain.ProviderType.SSH, info.ssh, "phrase"
            )
        )
        dialog.close()

    def test_open_focuses_ssh_password(self) -> None:
        dialog = UnlockSessionDialog(object(), self._ssh_info())  # type: ignore[arg-type]
        dialog.show()
        for _ in range(5):
            QApplication.processEvents()
        self.assertIs(dialog.focusWidget(), dialog.ssh_secret)
        dialog.close()

    def test_reconnect_chain_is_one_row_per_hop(self) -> None:
        info = domain.SessionInfo(
            session_id="s-chain",
            name="цепочка",
            provider=domain.ProviderType.SSH_OVER_SSH,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.CLOSED,
            ssh=domain.SSHConfig(
                host="10.0.0.1",
                username="root",
                hops=(domain.SSHHop(host="10.0.0.2", username="ubuntu", port=2200),),
            ),
        )
        dialog = UnlockSessionDialog(object(), info, reconnect=True)  # type: ignore[arg-type]
        dialog.show()
        for _ in range(5):
            QApplication.processEvents()
        self.assertEqual(len(dialog._chain_rows), 2)
        self.assertTrue(all(isinstance(row, _ChainPasswordRow) for row in dialog._chain_rows))
        self.assertEqual(dialog._chain_rows[0].endpoint.text(), "root@10.0.0.1")
        self.assertEqual(dialog._chain_rows[1].endpoint.text(), "ubuntu@10.0.0.2:2200")
        self.assertEqual(dialog.findChildren(QPushButton, "auxButton"), [])
        self.assertIs(dialog.focusWidget(), dialog._chain_rows[0].password)
        QTest.keyClick(dialog.focusWidget(), Qt.Key.Key_Tab)
        self.assertIs(dialog.focusWidget(), dialog._chain_rows[1].password)
        QTest.keyClick(dialog.focusWidget(), Qt.Key.Key_Tab)
        self.assertIs(dialog.focusWidget(), dialog._ok_button)
        dialog._chain_rows[0].password.setText("one")
        dialog._chain_rows[1].password.setText("two")
        built = dialog._unlock_chain()
        self.assertIsNotNone(built)
        config, secret, hops = built
        self.assertEqual(secret, "one")
        self.assertEqual(hops, ("two",))
        self.assertEqual(config.host, "10.0.0.1")
        self.assertEqual(config.username, "root")
        self.assertEqual(config.hops[0].host, "10.0.0.2")
        self.assertEqual(config.hops[0].username, "ubuntu")
        self.assertEqual(config.hops[0].port, 2200)
        dialog.close()

    def test_open_uses_the_same_rows_as_reconnect(self) -> None:
        from PySide6.QtWidgets import QLabel, QSizePolicy

        info = domain.SessionInfo(
            session_id="s-chain",
            name="цепочка",
            provider=domain.ProviderType.SSH_OVER_SSH,
            identity=domain.ExecutionIdentityType.CURRENT_USER,
            status=domain.SessionStatus.CLOSED,
            ssh=domain.SSHConfig(
                host="10.0.0.1",
                username="root",
                auth_method=domain.SSHAuthMethod.PASSWORD,
                hops=(domain.SSHHop(
                    host="10.0.0.2",
                    username="ubuntu",
                    port=2200,
                    auth_method=domain.SSHAuthMethod.PRIVATE_KEY,
                    private_key_path="C:/keys/ubuntu",
                ),),
            ),
        )
        dialog = UnlockSessionDialog(object(), info)  # type: ignore[arg-type]
        dialog.show()
        for _ in range(5):
            QApplication.processEvents()
        intro = dialog.findChild(QLabel, "sessionIntro")
        assert intro is not None
        self.assertEqual(
            intro.sizePolicy().verticalPolicy(),
            QSizePolicy.Policy.Maximum,
        )
        self.assertEqual(len(dialog._chain_rows), 2)
        self.assertTrue(all(isinstance(row, _ChainPasswordRow) for row in dialog._chain_rows))
        self.assertIsNotNone(dialog._chain_rows[0].password)
        self.assertIsNone(dialog._chain_rows[1].password)
        assert dialog._chain_rows[1].note is not None
        self.assertEqual(dialog._chain_rows[1].note.text(), "Используется ключ")
        self.assertEqual(dialog.findChildren(QPushButton, "auxButton"), [])
        assert dialog._chain_rows[0].password is not None
        dialog._chain_rows[0].password.setText("one")
        built = dialog._unlock_chain()
        assert built is not None
        config, secret, hops = built
        self.assertEqual(secret, "one")
        self.assertEqual(hops, ("",))
        self.assertEqual(config.auth_method, domain.SSHAuthMethod.PASSWORD)
        self.assertEqual(config.hops[0].auth_method, domain.SSHAuthMethod.PRIVATE_KEY)
        self.assertEqual(config.hops[0].private_key_path, "C:/keys/ubuntu")
        self.assertIsNone(
            connection_journal_password(
                domain.ProviderType.SSH_OVER_SSH, config, secret, hops
            )
        )
        dialog.close()
