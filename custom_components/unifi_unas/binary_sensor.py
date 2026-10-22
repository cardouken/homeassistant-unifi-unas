from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from . import UNASConfigEntry
from .const import DOMAIN, get_device_info
from .coordinator import UNASServiceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service

    async_add_entities([
        UNASScriptsInstalledSensor(coordinator),
        UNASMonitorRunningSensor(coordinator),
        UNASFanControlRunningSensor(coordinator),
    ])


class UNASScriptsInstalledSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Scripts Installed"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_scripts_installed"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
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

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("scripts_installed", False)


class UNASMonitorRunningSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Monitor Service"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_monitor_running"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
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

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("monitor_running", False)


class UNASFanControlRunningSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Fan Control Service"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fan_control_running"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
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

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("fan_control_running", False)
