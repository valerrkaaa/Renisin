# Copyright (c) 2026 Valery Andreevich Glushchenko. Dual-licensed under AGPL-3.0 and Commercial License.
# SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-Commercial

"""Фабрика драйверов."""
from __future__ import annotations

import domain
from application.ports import HostKeyVerifier, SessionDriver
from drivers.cmd import CmdSessionDriver
from drivers.ssh import SSHSessionDriver


class DriverFactory:
    def __init__(
        self,
        host_key_verifier: HostKeyVerifier,
        known_hosts_path: str,
        password_prompt=None,
    ) -> None:
        self._host_key_verifier = host_key_verifier
        self._known_hosts_path = known_hosts_path
        self._password_prompt = password_prompt

    def __call__(self, provider: domain.ProviderType) -> SessionDriver:
        if provider == domain.ProviderType.CMD:
            return CmdSessionDriver()
        if provider in (
            domain.ProviderType.SSH,
            domain.ProviderType.SSH_OVER_SSH,
        ):
            return SSHSessionDriver(
                host_key_verifier=self._host_key_verifier,
                known_hosts_path=self._known_hosts_path,
                password_prompt=self._password_prompt,
            )
        raise ValueError(f"Неподдерживаемый провайдер: {provider!r}")