import json
from pathlib import Path

import pytest
from homeassistant.helpers import entity_registry as er

from conftest import ENTRY_ID, fire, fire_fan_settings, fire_monitor_cycle, settle, setup_entry

# recorded from 0.10.0-dev before the entity descriptions, existing installs must keep all of it
EXPECTED = json.loads((Path(__file__).parent / "fixtures" / "entities.json").read_text())
ICONS = json.loads((Path(__file__).parents[1] / "custom_components/unifi_unas/icons.json").read_text())["entity"]
NVME = {"temperature": 40, "model": "Samsung SSD 990 PRO 2TB", "serial": "S7", "firmware": "4B2QJXD7",
        "status": "Optimal", "total_size": 2.0, "power_on_hours": 10, "percentage_used": 1,
        "available_spare": 100, "media_errors": 0, "unsafe_shutdowns": 3}


@pytest.mark.parametrize("model", ["UNAS_PRO", "UNVR"])
async def test_entities_keep_their_names_units_and_icons(hass, mqtt_mock, ssh, model):
    await setup_entry(hass, device_model=model)
    fire_monitor_cycle(hass, inventory={"bays": ["1"], "nvme": ["0"], "pools": [1], "shares": ["plex"]})
    for key, value in NVME.items():
        fire(hass, f"nvme/0/{key}", value)
    fire(hass, "smb/clients", "[]")
    fire_fan_settings(hass)
    fire(hass, "fan/availability", "online")
    await settle(hass)

    actual = {}
    for entry in er.async_entries_for_config_entry(er.async_get(hass), ENTRY_ID):
        state = hass.states.get(entry.entity_id)
        platform = entry.entity_id.split(".")[0]
        actual[entry.unique_id.removeprefix(f"{ENTRY_ID}_")] = {
            "entity_id": entry.entity_id,
            "name": entry.original_name,
            "icon": (state and state.attributes.get("icon")) or entry.original_icon
            or ICONS.get(platform, {}).get(entry.translation_key or "", {}).get("default"),
            "unit": entry.unit_of_measurement,
            "device_class": entry.original_device_class,
            "state_class": (entry.capabilities or {}).get("state_class"),
            "precision": (entry.options.get("sensor") or {}).get("suggested_display_precision"),
            "suggested_unit": (entry.options.get("sensor.private") or {}).get("suggested_unit_of_measurement"),
        }
    assert actual == EXPECTED[model]
