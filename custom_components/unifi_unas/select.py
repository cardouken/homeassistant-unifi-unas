from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UNASConfigEntry
from .const import CONF_DEVICE_MODEL
from .coordinator import UNASDataCoordinator
from .entity import UNASEntity
from .fan_state import FanState, async_publish_setting
from .ssh_manager import SSHManager

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

DEFAULT_SPEED = 128

MODE_CUSTOM_CURVE = "Custom Curve"
MODE_SET_SPEED = "Set Speed"
MODE_TARGET_TEMP = "Target Temp"

TEMP_METRICS = {"max": "Max (Hottest)", "avg": "Average"}
RESPONSE_SPEEDS = {"relaxed": "Relaxed", "balanced": "Balanced", "aggressive": "Aggressive"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data
    async_add_entities([
        UNASFanModeSelect(coordinator, entry.runtime_data.ssh),
        UNASFanOptionSelect(coordinator, "temp_metric", TEMP_METRICS),
        UNASFanOptionSelect(coordinator, "response_speed", RESPONSE_SPEEDS),
    ])


class UNASFanSelect(UNASEntity, SelectEntity):
    coordinator: UNASDataCoordinator

    @property
    def fan(self) -> FanState:
        return FanState.from_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        # kept from when these restored their own state, automations may read it
        return {"current_mode": self.fan.mode} if self.fan.mode is not None else {}


class UNASFanModeSelect(UNASFanSelect):
    def __init__(self, coordinator: UNASDataCoordinator, ssh: SSHManager) -> None:
        super().__init__(coordinator, "fan_mode", SelectEntityDescription(key="fan_mode", translation_key="fan_mode"))
        self._ssh = ssh
        base_type = "UNVR" if coordinator.entry.data[CONF_DEVICE_MODEL].startswith("UNVR") else "UNAS"
        self._modes = {
            "unas_managed": f"{base_type} Managed",
            "auto": MODE_CUSTOM_CURVE,
            "target_temp": MODE_TARGET_TEMP,
            "set_speed": MODE_SET_SPEED,
        }
        self._attr_options = list(self._modes.values())

    @property
    def available(self) -> bool:
        return self.coordinator.mqtt_client.fan_available() and self.fan.mode is not None

    @property
    def current_option(self) -> str | None:
        return self._modes.get(self.fan.mode)

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_pwm": self.fan.speed} if self.fan.speed is not None else {}

    async def async_select_option(self, option: str) -> None:
        mode = next(mode for mode, label in self._modes.items() if label == option)
        if mode == "set_speed":
            # start from the speed the fans run at now instead of jumping
            pwm = self.fan.pwm
            await async_publish_setting(self.coordinator, "speed", pwm if pwm is not None else DEFAULT_SPEED)
        await async_publish_setting(self.coordinator, "mode", mode)
        if mode == "unas_managed":
            try:
                await self._ssh.kick_native_fan_control()
            except Exception as err:
                _LOGGER.warning("Could not kick native fan control (non-critical): %s", err)


class UNASFanOptionSelect(UNASFanSelect):
    def __init__(self, coordinator: UNASDataCoordinator, setting: str, labels: dict[str, str]) -> None:
        super().__init__(
            coordinator, setting,
            SelectEntityDescription(key=setting, translation_key=setting, entity_category=EntityCategory.CONFIG),
        )
        self._setting = setting
        self._labels = labels
        self._attr_options = list(labels.values())

    @property
    def available(self) -> bool:
        fan = self.fan
        return (
            self.coordinator.mqtt_client.fan_available()
            and getattr(fan, self._setting) is not None
            and fan.mode == "target_temp"
        )

    @property
    def current_option(self) -> str | None:
        return self._labels.get(getattr(self.fan, self._setting))

    async def async_select_option(self, option: str) -> None:
        value = next(value for value, label in self._labels.items() if label == option)
        await async_publish_setting(self.coordinator, f"curve/{self._setting}", value)
