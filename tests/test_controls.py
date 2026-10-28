import json
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from conftest import (
    DOMAIN,
    ENTRY_ID,
    ROOT_TOPIC,
    TASK,
    fire,
    fire_fan_settings,
    fire_monitor_cycle,
    settle,
    setup_entry,
)


def entity_id(hass, platform, key):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ENTRY_ID}_{key}")


def published(mqtt_mock):
    return [call.args[:2] for call in mqtt_mock.async_publish.call_args_list]


@pytest.fixture
async def nas(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    fire_fan_settings(hass)
    await settle(hass)
    return ssh.instances[0]


async def test_backup_button_and_schedule_switch(hass, nas):
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id(hass, "button", f"backup_{TASK['id']}")},
        blocking=True,
    )
    schedule = entity_id(hass, "switch", f"backup_{TASK['id']}_schedule_enabled")
    assert hass.states.get(schedule).state == "on"
    for service in ("turn_off", "turn_on"):
        await hass.services.async_call("switch", service, {"entity_id": schedule}, blocking=True)
    assert nas.commands == [
        f"run-backup {TASK['id']}",
        f"set-schedule {TASK['id']} False",
        f"set-schedule {TASK['id']} True",
    ]


@pytest.mark.parametrize("platform,service,key,method,translation_key", [
    ("button", "press", f"backup_{TASK['id']}", "run_backup", "backup_failed"),
    ("switch", "turn_off", f"backup_{TASK['id']}_schedule_enabled", "set_backup_schedule",
     "schedule_failed"),
])
async def test_backup_api_failures_are_reported(
        hass, nas, platform, service, key, method, translation_key
):
    async def failing(*args):
        raise RuntimeError("HTTP 500")

    with patch.object(nas, method, failing), pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            platform, service, {"entity_id": entity_id(hass, platform, key)}, blocking=True
        )
    assert err.value.translation_key == translation_key
    assert err.value.translation_placeholders == {"error": "HTTP 500"}


async def test_handing_back_to_the_nas_kicks_uhwd(hass, nas, mqtt_mock, caplog):
    select = entity_id(hass, "select", "fan_mode")

    async def failing():
        raise OSError("gone")

    with patch.object(nas, "kick_native_fan_control", failing):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select, "option": "UNAS Managed"},
            blocking=True,
        )
    assert "Could not kick native fan control" in caplog.text
    assert (f"{ROOT_TOPIC}/control/fan/mode", "unas_managed") in published(mqtt_mock)


async def test_curve_options_publish_their_value(hass, nas, mqtt_mock):
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": entity_id(hass, "select", "temp_metric"), "option": "Max (Hottest)"},
        blocking=True,
    )
    assert (f"{ROOT_TOPIC}/control/fan/curve/temp_metric", "max") in published(mqtt_mock)


async def test_client_lists_become_attributes(hass, nas):
    clients = [{"ip": "192.168.1.141", "share": "plex"}]
    fire(hass, "nfs/mounts", "1")
    fire(hass, "nfs/clients", json.dumps(clients))
    fire(hass, "smb/clients", "not json")
    await settle(hass)
    mounts = entity_id(hass, "sensor", "unas_nfs_mounts")
    assert hass.states.get(mounts).attributes["clients"] == clients
    fire(hass, "nfs/clients", "")
    await settle(hass)
    assert "clients" not in hass.states.get(mounts).attributes


async def test_inventory_payloads(hass, nas):
    client = hass.config_entries.async_get_entry(ENTRY_ID).runtime_data.data.mqtt_client
    fire(hass, "inventory", "{broken")
    await settle(hass)
    assert client.inventory["bays"] == ["1", "2"]
    fire(hass, "inventory", "[1, 2]")
    await settle(hass)
    assert client.inventory["bays"] == ["1", "2"]
    fire(hass, "inventory", json.dumps({"bays": None, "pools": [1], "shares": "x"}))
    await settle(hass)
    assert client.inventory == {"bays": None, "pools": ["1"]}
    fire(hass, "inventory", "null")
    await settle(hass)
    assert client.inventory is None
