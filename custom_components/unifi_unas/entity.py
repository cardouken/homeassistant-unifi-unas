from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .devices import main_device


class UNASEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(
            self,
            coordinator,
            unique_key: str,
            description: EntityDescription | None = None,
            device: DeviceInfo | None = None,
            placeholders: dict[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        if description is not None:
            self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_key}"
        self._attr_device_info = device or main_device(coordinator.entry)
        if placeholders:
            self._attr_translation_placeholders = placeholders
