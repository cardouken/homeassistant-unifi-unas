from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import UNASConfigEntry
from .const import DOMAIN
from .coordinator import UNASDataCoordinator
from .entity import UNASEntity
from .fan_state import FanState, async_publish_setting, percent_to_pwm, pwm_to_percent

PARALLEL_UPDATES = 1

FAN_CURVE_PARAMS = tuple(
    NumberEntityDescription(
        key=key, translation_key=f"curve_{key}", native_min_value=low, native_max_value=high,
        native_step=1, native_unit_of_measurement=unit, mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
    )
    for key, low, high, unit in (
        ("min_temp", 20, 50, "°C"),
        ("max_temp", 30, 60, "°C"),
        ("min_fan", 0, 100, "%"),
        ("max_fan", 1, 100, "%"),
        ("target_temp", 20, 50, "°C"),
    )
)
FAN_SPEED = NumberEntityDescription(
    key="fan_speed", translation_key="fan_speed", native_min_value=0, native_max_value=100,
    native_step=1, native_unit_of_measurement="%", mode=NumberMode.SLIDER,
)
FAN_PARAMS = ("min_fan", "max_fan")
CURVE_ORDER = (("min_temp", "max_temp", "temperature_order"), ("min_fan", "max_fan", "fan_order"))
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
        *(UNASFanCurveNumber(coordinator, description) for description in FAN_CURVE_PARAMS),
    ])


class UNASFanNumber(UNASEntity, NumberEntity):
    coordinator: UNASDataCoordinator

    @property
    def fan(self) -> FanState:
        return FanState.from_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict:
        # kept from when these restored their own state, automations may read it
        return {"current_mode": self.fan.mode} if self.fan.mode is not None else {}


class UNASFanSpeedNumber(UNASFanNumber):
    def __init__(self, coordinator: UNASDataCoordinator) -> None:
        super().__init__(coordinator, "fan_speed_control", FAN_SPEED)

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
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="set_speed_only"
            )
        await async_publish_setting(self.coordinator, "speed", percent_to_pwm(value))


class UNASFanCurveNumber(UNASFanNumber):
    def __init__(
            self, coordinator: UNASDataCoordinator, description: NumberEntityDescription
    ) -> None:
        super().__init__(coordinator, f"fan_curve_{description.key}", description)
        self._key = description.key

    def _value(self, fan: FanState, key: str) -> int | None:
        value = fan.curve.get(key)
        if value is not None and key in FAN_PARAMS:
            value = pwm_to_percent(value)
        return value

    @property
    def native_value(self) -> int | None:
        value = self._value(self.fan, self._key)
        if value is None or not self.native_min_value <= value <= self.native_max_value:
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
        for low, high, error in CURVE_ORDER:
            if self._key not in (low, high) or None in (curve[low], curve[high]):
                continue
            if curve[high] <= curve[low]:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key=error,
                    translation_placeholders={"min": str(curve[low]), "max": str(curve[high])},
                )
        await async_publish_setting(
            self.coordinator, f"curve/{self._key}",
            percent_to_pwm(value) if self._key in FAN_PARAMS else value,
        )
