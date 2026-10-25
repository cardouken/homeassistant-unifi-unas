from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UNASConfigEntry
from .coordinator import UNASServiceCoordinator
from .entity import UNASEntity

PARALLEL_UPDATES = 0

# key is the service coordinator's data key
SERVICE_SENSORS = tuple(
    BinarySensorEntityDescription(
        key=key, translation_key=key, device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    for key in ("scripts_installed", "monitor_running", "fan_control_running")
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.service
    async_add_entities(UNASServiceSensor(coordinator, description) for description in SERVICE_SENSORS)


class UNASServiceSensor(UNASEntity, BinarySensorEntity):
    coordinator: UNASServiceCoordinator

    def __init__(self, coordinator: UNASServiceCoordinator, description: BinarySensorEntityDescription) -> None:
        super().__init__(coordinator, description.key, description)

    @property
    def available(self) -> bool:
        return self.coordinator.ready

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.get(self.entity_description.key, False)
