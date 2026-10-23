from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import UNASConfigEntry
from .const import DOMAIN, get_backup_device_info, get_device_info
from .coordinator import UNASServiceCoordinator
from .deploy import Deployer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service

    async_add_entities([
        UNASReinstallScriptsButton(coordinator, entry.runtime_data.deployer),
        UNASRebootButton(coordinator),
        UNASShutdownButton(coordinator),
    ])

    @callback
    def add_backups(task_ids: list[str]) -> None:
        async_add_entities(
            UNASBackupTriggerButton(coordinator, coordinator.find_backup_task(task_id)) for task_id in task_ids
        )

    entry.runtime_data.reconciler.register("backups", add_backups)


class UNASReinstallScriptsButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, deployer: Deployer) -> None:
        super().__init__(coordinator)
        self._deployer = deployer
        self._attr_has_entity_name = True
        self._attr_name = "Reinstall Scripts"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reinstall_scripts"
        self._attr_icon = "mdi:cog-refresh"
        device_name, device_model = get_device_info(coordinator.entry.data)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.ready

    async def async_press(self) -> None:
        self._deployer.async_reinstall()


class UNASRebootButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASServiceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Reboot"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reboot"
        self._attr_icon = "mdi:restart"
        device_name, device_model = get_device_info(coordinator.entry.data)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.ready

    async def async_press(self) -> None:
        await self.coordinator.ssh_manager.execute_command("reboot")


class UNASShutdownButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASServiceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Shutdown"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_shutdown"
        self._attr_icon = "mdi:power"
        device_name, device_model = get_device_info(coordinator.entry.data)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @property
    def available(self) -> bool:
        return self.coordinator.ready

    async def async_press(self) -> None:
        await self.coordinator.ssh_manager.execute_command("shutdown -h now")


class UNASBackupTriggerButton(CoordinatorEntity, ButtonEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Run backup"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}"
        self._attr_icon = "mdi:cloud-upload"
        self._attr_device_info = get_backup_device_info(coordinator.entry.entry_id, coordinator.entry.data, task)

    @property
    def available(self) -> bool:
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    async def async_press(self) -> None:
        try:
            await self.coordinator.ssh_manager.run_backup(self._task_id)
        except Exception as err:
            raise HomeAssistantError(f"Failed to trigger backup: {err}") from err
        await self.coordinator.async_request_refresh()
