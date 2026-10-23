from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import asyncssh
from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_DEVICE_MODEL,
    CONF_HOST,
    CONF_HOST_KEY,
    CONF_VERIFY_HOST_KEY,
    DOMAIN,
    SSH_STATUS_INTERVAL,
    get_mqtt_topics,
)
from .mqtt_client import UNASMQTTClient
from .ssh_manager import SSHManager

_LOGGER = logging.getLogger(__name__)

PUSH_DEBOUNCE = 0.5
# availability goes stale on a timer, not only on messages
AVAILABILITY_TICK = timedelta(seconds=30)
# the broker sends retained topics right after subscribing
RETAINED_WAIT = 5
RETAINED_QUIET = 0.25
SSH_UNAVAILABLE_THRESHOLD = 600


def host_key_repair_id(entry_id: str) -> str:
    return f"host_key_changed_{entry_id}"


@callback
def raise_host_key_repair(hass: HomeAssistant, entry: ConfigEntry) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        host_key_repair_id(entry.entry_id),
        is_fixable=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="host_key_changed",
        translation_placeholders={"host": entry.data[CONF_HOST]},
        data={"entry_id": entry.entry_id},
    )


@callback
def record_host_key_pin(hass: HomeAssistant, entry: ConfigEntry, manager: SSHManager) -> None:
    # after every verified connection, so a NAS that was down at startup still gets pinned
    if (
        entry.data.get(CONF_VERIFY_HOST_KEY)
        and not entry.data.get(CONF_HOST_KEY)
        and manager.server_host_key
    ):
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_HOST_KEY: manager.server_host_key}
        )
        manager.pinned_host_key = manager.server_host_key
        _LOGGER.info("Pinned SSH host key for %s", entry.data[CONF_HOST])
    ir.async_delete_issue(hass, DOMAIN, host_key_repair_id(entry.entry_id))


class UNASDataCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} mqtt")
        self.entry = entry
        self._debouncer = Debouncer(
            hass, _LOGGER, cooldown=PUSH_DEBOUNCE, immediate=False, function=self._async_push
        )
        self.mqtt_client = UNASMQTTClient(hass, entry.entry_id, self._debouncer.async_schedule_call)

    async def async_start(self) -> None:
        await self.mqtt_client.async_subscribe()
        await self._async_wait_for_retained()
        self.async_set_updated_data(self.mqtt_client.get_data())
        self.entry.async_on_unload(
            async_track_time_interval(self.hass, self._async_tick, AVAILABILITY_TICK)
        )

    async def _async_update_data(self) -> dict[str, Any]:
        # nothing to poll, update_entity gets what MQTT pushed
        return self.mqtt_client.get_data()

    async def _async_wait_for_retained(self) -> None:
        received = self.mqtt_client.received
        deadline = self.hass.loop.time() + RETAINED_WAIT
        timeout = RETAINED_WAIT
        while timeout > 0:
            try:
                await asyncio.wait_for(received.wait(), timeout)
            except TimeoutError:
                return
            received.clear()
            timeout = min(RETAINED_QUIET, deadline - self.hass.loop.time())

    async def async_shutdown(self) -> None:
        self._debouncer.async_shutdown()
        self.mqtt_client.async_unsubscribe()
        await super().async_shutdown()

    @callback
    def _async_push(self) -> None:
        self.async_set_updated_data(self.mqtt_client.get_data())
        self._check_duplicate_publishers()

    @callback
    def _async_tick(self, _now) -> None:
        self.async_update_listeners()
        self._check_duplicate_publishers()

    def _check_duplicate_publishers(self) -> None:
        machine_ids = self.mqtt_client.get_recent_machine_ids()
        issue_id = f"duplicate_publishers_{self.entry.entry_id}"
        if len(machine_ids) > 1:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="duplicate_publishers",
                translation_placeholders={"machine_ids": ", ".join(sorted(machine_ids))},
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)


class UNASServiceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, ssh: SSHManager) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} ssh",
            update_interval=timedelta(seconds=SSH_STATUS_INTERVAL),
        )
        self.entry = entry
        self.ssh_manager = ssh
        self._failing_since: float | None = None

    @property
    def ready(self) -> bool:
        return self.last_update_success and self.data is not None

    async def _async_update_data(self) -> dict[str, Any]:
        host = self.entry.data[CONF_HOST]
        try:
            status, outdated = await self.ssh_manager.bundle_status(
                self.entry.data[CONF_DEVICE_MODEL], get_mqtt_topics(self.entry.entry_id)["root"]
            )
        except asyncssh.HostKeyNotVerifiable as err:
            # not an outage, it repeats until the key is re-pinned through the repair
            raise_host_key_repair(self.hass, self.entry)
            raise UpdateFailed(f"SSH host key verification failed for {host}: {err}") from err
        except Exception as err:
            self._track_outage(host)
            raise UpdateFailed(f"Cannot check the UNAS at {host} over SSH: {err}") from err

        self._failing_since = None
        ir.async_delete_issue(self.hass, DOMAIN, f"ssh_unavailable_{self.entry.entry_id}")
        record_host_key_pin(self.hass, self.entry, self.ssh_manager)

        backup_tasks = status.get("backup_tasks")
        current = backup_tasks is not None
        if not current:
            # unknown, keep showing the last list but never remove tasks from it
            _LOGGER.debug("Could not fetch backup tasks: %s", status.get("backup_error"))
            backup_tasks = (self.data or {}).get("backup_tasks", [])

        services = status.get("services") or {}
        return {
            "outdated": outdated,
            "scripts_installed": not outdated,
            "monitor_running": services.get("unas_monitor") == "active",
            "fan_control_running": services.get("fan_control") == "active",
            "backup_tasks": backup_tasks,
            "backup_tasks_current": current,
        }

    def _track_outage(self, host: str) -> None:
        now = time.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        elif now - self._failing_since > SSH_UNAVAILABLE_THRESHOLD:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"ssh_unavailable_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="ssh_unavailable",
                translation_placeholders={"host": host},
            )

    def find_backup_task(self, task_id: str) -> dict | None:
        for task in (self.data or {}).get("backup_tasks") or []:
            if task["id"] == task_id:
                return task
        return None
