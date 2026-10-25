# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Диалог открытия SSH-сессии: фокус и пароль журнала."""
from __future__ import annotations

import unittest

from PySide6.QtWidgets import QApplication

import domain
from ui.dialogs import UnlockSessionDialog


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

    def test_empty_journal_uses_ssh_password(self) -> None:
        dialog = UnlockSessionDialog(object(), self._ssh_info())  # type: ignore[arg-type]
        self.assertFalse(dialog._journal_hint.isHidden())
        self.assertEqual(dialog.password_edit.placeholderText(), "пароль SSH")
        self.assertEqual(
            dialog._resolved_journal_password(
                domain.SSHAuthMethod.PASSWORD, "secret"
            ),
            "secret",
        )
        dialog.password_edit.setText("journal")
        self.assertEqual(
            dialog._resolved_journal_password(
                domain.SSHAuthMethod.PASSWORD, "secret"
            ),
            "journal",
        )
        dialog.close()

    def test_key_auth_does_not_fill_journal_from_passphrase(self) -> None:
        dialog = UnlockSessionDialog(
            object(),  # type: ignore[arg-type]
            self._ssh_info(domain.SSHAuthMethod.PRIVATE_KEY),
        )
        self.assertFalse(dialog._journal_hint.isVisible())
        self.assertEqual(
            dialog._resolved_journal_password(
                domain.SSHAuthMethod.PRIVATE_KEY, "phrase"
            ),
            "",
        )
        dialog.close()

    def test_open_focuses_ssh_password(self) -> None:
        dialog = UnlockSessionDialog(object(), self._ssh_info())  # type: ignore[arg-type]
        dialog.show()
        for _ in range(5):
            QApplication.processEvents()
        self.assertIs(dialog.focusWidget(), dialog.ssh_secret)
        dialog.close()
