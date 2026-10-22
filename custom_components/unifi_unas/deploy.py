from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir

from .const import CONF_DEVICE_MODEL, CONF_HOST, DOMAIN, get_mqtt_topics
from .coordinator import UNASServiceCoordinator
from .ssh_manager import SSHManager

_LOGGER = logging.getLogger(__name__)

# seconds to wait after the 1st, 2nd, 3rd... attempt that left the scripts outdated
RETRY_DELAYS = (60, 300, 900, 3600)
ATTEMPTS_BEFORE_REPAIR = 3


class Deployer:
    def __init__(
            self, hass: HomeAssistant, entry: ConfigEntry, ssh: SSHManager, service: UNASServiceCoordinator
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.ssh = ssh
        self.service = service
        self.attempts = 0
        self.last_error: str | None = None
        self._retry_at = 0.0
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def _repair_id(self) -> str:
        return f"deploy_failed_{self.entry.entry_id}"

    @callback
    def async_check(self) -> None:
        if not self.service.ready:
            return
        outdated = self.service.data["outdated"]
        if not outdated:
            self.attempts = 0
            self.last_error = None
            self._retry_at = 0.0
            ir.async_delete_issue(self.hass, DOMAIN, self._repair_id)
            return
        if not self.running and time.monotonic() >= self._retry_at:
            if self.attempts >= ATTEMPTS_BEFORE_REPAIR:
                # deploys that went through but left files outdated, or kept failing
                names = ", ".join(path.rsplit("/", 1)[-1] for path in outdated)
                self._raise_repair(self.last_error or f"{names} still outdated after deploying")
            self._start(outdated)

    @callback
    def async_reinstall(self) -> None:
        if self.running:
            raise HomeAssistantError("The scripts are already being deployed")
        self._start(None)

    @callback
    def _start(self, outdated: list[str] | None) -> None:
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_deploy(outdated), f"{DOMAIN} deploy"
        )

    @callback
    def _raise_repair(self, error: str) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._repair_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="deploy_failed",
            translation_placeholders={"host": self.entry.data[CONF_HOST], "error": error},
        )

    async def _async_deploy(self, outdated: list[str] | None) -> None:
        host = self.entry.data[CONF_HOST]
        if outdated is None:
            _LOGGER.info("Reinstalling the scripts on %s", host)
        else:
            _LOGGER.info("Deploying %s to %s", ", ".join(path.rsplit("/", 1)[-1] for path in outdated), host)
        self.attempts += 1
        self._retry_at = time.monotonic() + RETRY_DELAYS[min(self.attempts, len(RETRY_DELAYS)) - 1]
        try:
            restarted = await self.ssh.deploy_scripts(
                self.entry.data[CONF_DEVICE_MODEL], get_mqtt_topics(self.entry.entry_id)["root"], outdated
            )
        except Exception as err:
            self.last_error = f"{type(err).__name__}: {err}"
            _LOGGER.warning(
                "Deploying the scripts to %s failed (attempt %d): %s", host, self.attempts, self.last_error
            )
            if self.attempts >= ATTEMPTS_BEFORE_REPAIR:
                self._raise_repair(self.last_error)
            return
        _LOGGER.info("Scripts deployed to %s, restarted: %s", host, ", ".join(restarted) or "nothing")
        self.last_error = None
        # attempts reset once a check finds nothing outdated
        await self.service.async_request_refresh()
