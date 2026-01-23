from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import UNASDataUpdateCoordinator
from .const import CONF_DEVICE_MODEL, DOMAIN, get_device_info


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
