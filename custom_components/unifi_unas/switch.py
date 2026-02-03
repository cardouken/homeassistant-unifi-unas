from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import UNASDataUpdateCoordinator
from .const import DOMAIN, format_remote_type

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: UNASDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.switch_add_entities = async_add_entities


async def _discover_and_add_backup_switches(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er

    backup_tasks = coordinator.data.get("backup_tasks", [])
    task_ids = {task["id"] for task in backup_tasks}

    entity_reg = er.async_get(coordinator.hass)
    entry_id = coordinator.entry.entry_id

    missing_tasks = coordinator.discovered_backup_task_switches - task_ids
    if missing_tasks:
        coordinator.discovered_backup_task_switches -= missing_tasks
        for task_id in missing_tasks:
            unique_id = f"{entry_id}_backup_{task_id}_schedule_enabled"
            if entity_id := entity_reg.async_get_entity_id("switch", DOMAIN, unique_id):
                entity_reg.async_remove(entity_id)

    # clean up orphaned entities
    prefix = f"{entry_id}_backup_"
    suffix = "_schedule_enabled"
    for entity in er.async_entries_for_config_entry(entity_reg, entry_id):
        if entity.domain != "switch" or not entity.unique_id.startswith(prefix):
            continue
        if not entity.unique_id.endswith(suffix):
            continue
        task_id = entity.unique_id[len(prefix):-len(suffix)]
        if task_id not in task_ids:
            entity_reg.async_remove(entity.entity_id)
            _LOGGER.info("Removed orphaned backup switch %s", entity.entity_id)

    new_tasks = task_ids - coordinator.discovered_backup_task_switches
    if not new_tasks:
        return

    entities = []
    for task in backup_tasks:
        if task["id"] in new_tasks:
            entities.append(BackupScheduleSwitch(coordinator, task))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_backup_task_switches.update(new_tasks)
        _LOGGER.info("Added %d backup schedule switches", len(entities))


def _find_backup_task(coordinator, task_id):
    for task in coordinator.data.get("backup_tasks", []):
        if task["id"] == task_id:
            return task
    return None


class BackupScheduleSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Schedule enabled"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_schedule_enabled"
        self._attr_icon = "mdi:calendar-clock"
        remote = task.get("remote", {})
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_backup_{self._task_id}")},
            name=f"UNAS Backup {self._task_name}",
            manufacturer=format_remote_type(remote.get("type")),
            model=remote.get("oauth2Account") or task.get("destinationDir", ""),
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    @property
    def available(self):
        if not self.coordinator.data.get("ssh_connected", False):
            return False
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def is_on(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return False
        return task.get("schedule", {}).get("enable", False)

    async def async_turn_on(self, **kwargs):
        await self._set_schedule_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_schedule_enabled(False)

    async def _set_schedule_enabled(self, enabled):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return
        schedule = task.get("schedule", {}).copy()
        schedule["enable"] = enabled
        await self.coordinator.ssh_manager.update_backup_task(self._task_id, {"schedule": schedule})
        await self.coordinator.async_request_refresh()
