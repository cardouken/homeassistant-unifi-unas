import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from conftest import DOMAIN, ENTRY_ID, ROOT_TOPIC, fire, fire_fan_settings, fire_monitor_cycle, settle, setup_entry


def entity_id(hass, platform, key):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ENTRY_ID}_{key}")


def state(hass, platform, key):
    return hass.states.get(entity_id(hass, platform, key))


def published(mqtt_mock):
    return [(call.args[0].removeprefix(f"{ROOT_TOPIC}/control/fan/"), call.args[1])
            for call in mqtt_mock.async_publish.call_args_list if "/control/fan/" in call.args[0]]


@pytest.fixture
async def fan(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    fire_fan_settings(hass)
    fire(hass, "fan/availability", "online")
    await settle(hass)
    mqtt_mock.async_publish.reset_mock()
    return mqtt_mock


async def test_settings_come_from_the_retained_topics(hass, fan):
    assert state(hass, "select", "fan_mode").state == "Target Temp"
    assert state(hass, "select", "temp_metric").state == "Average"
    assert state(hass, "number", "fan_curve_target_temp").state == "43"
    assert state(hass, "number", "fan_curve_min_fan").state == "20"
    assert state(hass, "number", "fan_speed_control").state == "30"
    assert state(hass, "select", "temp_metric").attributes["current_mode"] == "target_temp"


async def test_controls_follow_the_mode(hass, fan):
    assert state(hass, "number", "fan_curve_min_temp").state == "unavailable"
    fire(hass, "control/fan/mode", "auto")
    await settle(hass)
    assert state(hass, "number", "fan_curve_min_temp").state == "40"
    assert state(hass, "number", "fan_curve_target_temp").state == "unavailable"
    assert state(hass, "select", "response_speed").state == "unavailable"


async def test_a_legacy_numeric_mode_is_set_speed(hass, fan):
    fire(hass, "control/fan/mode", "153")
    await settle(hass)
    mode = state(hass, "select", "fan_mode")
    assert (mode.state, mode.attributes["last_pwm"]) == ("Set Speed", 153)


async def test_set_speed_starts_from_the_current_speed(hass, fan):
    await hass.services.async_call(
        "select", "select_option", {"entity_id": entity_id(hass, "select", "fan_mode"), "option": "Set Speed"},
        blocking=True,
    )
    assert published(fan) == [("speed", "76"), ("mode", "set_speed")]
    # shown before the broker echoes it back
    assert state(hass, "select", "fan_mode").state == "Set Speed"

    fan.async_publish.reset_mock()
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id(hass, "number", "fan_speed_control"), "value": 50},
        blocking=True,
    )
    assert published(fan) == [("speed", "128")]
    # the slider shows what was set, not the speed of the last monitor cycle
    assert state(hass, "number", "fan_speed_control").state == "50"


async def test_fan_speed_outside_set_speed_is_rejected(hass, fan):
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id(hass, "number", "fan_speed_control"), "value": 50},
            blocking=True,
        )
    assert err.value.translation_key == "set_speed_only"
    assert published(fan) == []


async def test_an_inverted_curve_is_rejected(hass, fan):
    fire(hass, "control/fan/mode", "auto")
    await settle(hass)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id(hass, "number", "fan_curve_min_temp"), "value": 50},
            blocking=True,
        )
    assert (err.value.translation_key, err.value.translation_placeholders) == (
        "temperature_order", {"min": "50", "max": "50"}
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id(hass, "number", "fan_curve_min_fan"), "value": 40},
        blocking=True,
    )
    assert published(fan) == [("curve/min_fan", "102")]


async def test_the_daemon_going_offline_takes_the_controls_with_it(hass, fan):
    fire(hass, "fan/availability", "offline")
    await settle(hass)
    assert state(hass, "select", "fan_mode").state == "unavailable"
    assert state(hass, "number", "fan_speed_control").state == "unavailable"


async def test_fan_curve_sensor(hass, fan):
    curve = state(hass, "sensor", "fan_curve_viz")
    assert curve.state == "40-50°C → 20-100%"
    assert curve.attributes["curve_points"][0] == [30, 20] and curve.attributes["curve_points"][-1] == [60, 100]
    fire(hass, "control/fan/curve/max_temp", "40")
    await settle(hass)
    assert state(hass, "sensor", "fan_curve_viz").attributes["curve_points"][15] == [45, 100]
