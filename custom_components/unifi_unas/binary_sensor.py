from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    coordinator: UNASDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities([
        UNASScriptsInstalledSensor(coordinator),
        UNASMonitorRunningSensor(coordinator),
        UNASFanControlRunningSensor(coordinator),
    ])


class UNASScriptsInstalledSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Scripts Installed"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_scripts_installed"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("scripts_installed", False)


class UNASMonitorRunningSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Monitor Service"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_monitor_running"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("monitor_running", False)


class UNASFanControlRunningSensor(CoordinatorEntity, BinarySensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Fan Control Service"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fan_control_running"
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get("fan_control_running", False)
