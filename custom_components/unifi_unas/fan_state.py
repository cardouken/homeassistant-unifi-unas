from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import mqtt

from .const import get_mqtt_topics

MODES = ("unas_managed", "auto", "target_temp", "set_speed")
CURVE_KEYS = ("min_temp", "max_temp", "min_fan", "max_fan", "target_temp")


def pwm_to_percent(pwm: int) -> int:
    return round(pwm * 100 / 255)


def percent_to_pwm(percent: float) -> int:
    return round(percent * 255 / 100)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class FanState:
    mode: str | None = None
    speed: int | None = None
    pwm: int | None = None
    curve: dict[str, int] = field(default_factory=dict)
    temp_metric: str | None = None
    response_speed: str | None = None

    @classmethod
    def from_data(cls, data: dict[str, Any]) -> FanState:
        mode, speed = data.get("fan_mode"), _int(data.get("fan_speed_setting"))
        if _int(mode) is not None:
            # retained from before the speed topic, the fan daemon converts it on start
            mode, speed = "set_speed", mode
        elif mode not in MODES:
            mode = None
        return cls(
            mode=mode,
            speed=speed,
            pwm=_int(data.get("unas_fan_speed")),
            curve={key: value for key in CURVE_KEYS if (value := _int(data.get(f"fan_curve_{key}"))) is not None},
            temp_metric=data.get("fan_curve_temp_metric"),
            response_speed=data.get("fan_curve_response_speed"),
        )


async def async_publish_setting(coordinator, topic: str, value: Any) -> None:
    full_topic = f"{get_mqtt_topics(coordinator.entry.entry_id)['control']}/fan/{topic}"
    await mqtt.async_publish(coordinator.hass, full_topic, str(value), qos=0, retain=True)
    # shown right away instead of after the broker echo and the push debounce
    coordinator.mqtt_client.async_apply(full_topic, str(value))
    coordinator.async_set_updated_data(coordinator.mqtt_client.get_data())
