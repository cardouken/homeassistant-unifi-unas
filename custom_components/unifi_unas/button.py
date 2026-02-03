from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import UNASDataUpdateCoordinator
from .const import CONF_DEVICE_MODEL, DOMAIN, format_remote_type, get_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: UNASDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    async_add_entities([
        UNASReinstallScriptsButton(coordinator),
        UNASRebootButton(coordinator),
        UNASShutdownButton(coordinator),
    ])

    coordinator.button_add_entities = async_add_entities


class UNASReinstallScriptsButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Reinstall Scripts"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reinstall_scripts"
        self._attr_icon = "mdi:cog-refresh"
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data.get("ssh_connected", False)

    async def async_press(self) -> None:
        await self.coordinator.async_reinstall_scripts()


class UNASRebootButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Reboot"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reboot"
        self._attr_icon = "mdi:restart"
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data.get("ssh_connected", False)

    async def async_press(self) -> None:
        await self.coordinator.ssh_manager.execute_command("reboot")


class UNASShutdownButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Shutdown"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_shutdown"
        self._attr_icon = "mdi:power"
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and self.coordinator.data.get("ssh_connected", False)

    async def async_press(self) -> None:
        await self.coordinator.ssh_manager.execute_command("shutdown -h now")


async def _discover_and_add_backup_buttons(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er

    backup_tasks = coordinator.data.get("backup_tasks", [])
    task_ids = {task["id"] for task in backup_tasks}

    entity_reg = er.async_get(coordinator.hass)
    entry_id = coordinator.entry.entry_id

    missing_tasks = coordinator.discovered_backup_task_buttons - task_ids
    if missing_tasks:
        _LOGGER.info("Backup task buttons no longer needed: %s", sorted(missing_tasks))
        coordinator.discovered_backup_task_buttons -= missing_tasks

        for task_id in missing_tasks:
            unique_id = f"{entry_id}_backup_{task_id}"
            if entity_id := entity_reg.async_get_entity_id("button", DOMAIN, unique_id):
                entity_reg.async_remove(entity_id)
                _LOGGER.debug("Removed backup button entity %s", entity_id)

    # clean up orphaned entities from previous sessions if needed
    prefix = f"{entry_id}_backup_"
    for entity in er.async_entries_for_config_entry(entity_reg, entry_id):
        if entity.domain != "button" or not entity.unique_id.startswith(prefix):
            continue
        task_id = entity.unique_id[len(prefix):]
        if task_id not in task_ids:
            entity_reg.async_remove(entity.entity_id)
            _LOGGER.info("Removed orphaned backup button %s", entity.entity_id)

    new_tasks = task_ids - coordinator.discovered_backup_task_buttons
    if not new_tasks:
        return

    entities = []
    for task in backup_tasks:
        if task["id"] in new_tasks:
            entities.append(UNASBackupTriggerButton(coordinator, task))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_backup_task_buttons.update(new_tasks)
        _LOGGER.info("Added %d backup trigger buttons", len(entities))


class UNASBackupTriggerButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Run backup"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}"
        self._attr_icon = "mdi:cloud-upload"
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
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        if not self.coordinator.data.get("ssh_connected", False):
            return False
        backup_tasks = self.coordinator.data.get("backup_tasks", [])
        return any(task["id"] == self._task_id for task in backup_tasks)

    async def async_press(self) -> None:
        result = await self.coordinator.ssh_manager.execute_backup_api(
            "POST", f"/api/v1/remote-backup/run-task/{self._task_id}"
        )
        if result.get("data") != "OK":
            raise HomeAssistantError(f"Failed to trigger backup: {result}")
        await self.coordinator.async_request_refresh()
