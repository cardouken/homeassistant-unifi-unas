from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UNASConfigEntry
from .const import DOMAIN, get_backup_device_info
from .coordinator import UNASServiceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service

    @callback
    def add_backups(task_ids: list[str]) -> None:
        async_add_entities(
            BackupScheduleSwitch(coordinator, coordinator.find_backup_task(task_id)) for task_id in task_ids
        )

    entry.runtime_data.reconciler.register("backups", add_backups)


class BackupScheduleSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Schedule enabled"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_schedule_enabled"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_device_info = get_backup_device_info(coordinator.entry.entry_id, coordinator.entry.data, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def is_on(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return False
        return task.get("schedule", {}).get("enable", False)

    async def async_turn_on(self, **kwargs):
        await self._set_schedule_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_schedule_enabled(False)

    async def _set_schedule_enabled(self, enabled):
        try:
            await self.coordinator.ssh_manager.set_backup_schedule(self._task_id, enabled)
        except Exception as err:
            raise HomeAssistantError(f"Failed to update the backup schedule: {err}") from err
        await self.coordinator.async_request_refresh()
