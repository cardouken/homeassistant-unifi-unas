from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UNASConfigEntry
from .const import DOMAIN
from .coordinator import UNASDataCoordinator
from .devices import main_device
from .fan_state import FanState, async_publish_setting, percent_to_pwm, pwm_to_percent

# fan curve parameter definitions: (key, name, min, max, unit, icon)
FAN_CURVE_PARAMS = [
    ("min_temp", "Min Temperature", 20, 50, "°C", "mdi:thermometer-low"),
    ("max_temp", "Max Temperature", 30, 60, "°C", "mdi:thermometer-high"),
    ("min_fan", "Min Fan Speed", 0, 100, "%", "mdi:fan-speed-1"),
    ("max_fan", "Max Fan Speed", 1, 100, "%", "mdi:fan-speed-3"),
    ("target_temp", "Target Temperature", 20, 50, "°C", "mdi:thermometer-check"),
]
FAN_PARAMS = ("min_fan", "max_fan")
# which fan modes each curve parameter applies to
CURVE_MODES = {
    "min_temp": ("auto",),
    "max_temp": ("auto",),
    "min_fan": ("auto", "target_temp"),
    "max_fan": ("auto", "target_temp"),
    "target_temp": ("target_temp",),
}
MODE_ICONS = {"unas_managed": "mdi:fan-off", "auto": "mdi:fan-auto", "target_temp": "mdi:fan-auto"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: UNASConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data
    async_add_entities([
        UNASFanSpeedNumber(coordinator),
        *(UNASFanCurveNumber(coordinator, *param) for param in FAN_CURVE_PARAMS),
    ])


class UNASFanNumber(CoordinatorEntity, NumberEntity):
    coordinator: UNASDataCoordinator

    def __init__(self, coordinator: UNASDataCoordinator, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{key}"
        self._attr_native_step = 1
        self._attr_device_info = main_device(coordinator.entry)

    @property
    def fan(self) -> FanState:
        return FanState.from_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        # kept from when these restored their own state, automations may read it
        return {"current_mode": self.fan.mode} if self.fan.mode is not None else {}


class UNASFanSpeedNumber(UNASFanNumber):
    def __init__(self, coordinator: UNASDataCoordinator) -> None:
        super().__init__(coordinator, "fan_speed_control", "Fan Speed")
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_unit_of_measurement = "%"
        self._attr_mode = NumberMode.SLIDER

    @property
    def available(self) -> bool:
        return self.coordinator.mqtt_client.fan_available()

    @property
    def native_value(self) -> float | None:
        # the speed that was set in Set Speed, what the fans actually run at in the other modes
        fan = self.fan
        pwm = fan.speed if fan.mode == "set_speed" and fan.speed is not None else fan.pwm
        return pwm_to_percent(pwm) if pwm is not None else None

    @property
    def icon(self) -> str:
        return MODE_ICONS.get(self.fan.mode, "mdi:fan")

    async def async_set_native_value(self, value: float) -> None:
        if self.fan.mode != "set_speed":
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="set_speed_only")
        await async_publish_setting(self.coordinator, "speed", percent_to_pwm(value))


class UNASFanCurveNumber(UNASFanNumber):
    def __init__(
            self, coordinator: UNASDataCoordinator, key: str, name: str, min_val: int, max_val: int, unit: str,
            icon: str,
    ) -> None:
        super().__init__(coordinator, f"fan_curve_{key}", name)
        self._key = key
        self._attr_native_min_value = min_val
        self._attr_native_max_value = max_val
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_mode = NumberMode.BOX

    def _value(self, fan: FanState, key: str) -> int | None:
        value = fan.curve.get(key)
        if value is not None and key in FAN_PARAMS:
            value = pwm_to_percent(value)
        return value

    @property
    def native_value(self) -> int | None:
        value = self._value(self.fan, self._key)
        if value is None or not self._attr_native_min_value <= value <= self._attr_native_max_value:
            return None
        return value

    @property
    def available(self) -> bool:
        return (
            self.coordinator.mqtt_client.fan_available()
            and self.native_value is not None
            and self.fan.mode in CURVE_MODES[self._key]
        )

    async def async_set_native_value(self, value: float) -> None:
        value = int(value)
        fan = self.fan
        curve = {key: self._value(fan, key) for key in CURVE_MODES}
        curve[self._key] = value
        for low, high, error in (("min_temp", "max_temp", "temperature_order"), ("min_fan", "max_fan", "fan_order")):
            if self._key in (low, high) and None not in (curve[low], curve[high]) and curve[high] <= curve[low]:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=error,
                    translation_placeholders={"min": str(curve[low]), "max": str(curve[high])},
                )
        await async_publish_setting(
            self.coordinator, f"curve/{self._key}", percent_to_pwm(value) if self._key in FAN_PARAMS else value
        )
