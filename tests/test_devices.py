import json

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from conftest import DOMAIN, ENTRY_ID, TASK, fire, fire_monitor_cycle, settle, setup_entry
from custom_components.unifi_unas.devices import drive_manufacturer

NAS = {
    "name": "UniFi Network Attached Storage Pro", "shortname": "UNASPRO", "sysid": "ea51",
    "product": "UNAS Pro", "sku": "UNAS-Pro",
    "serial": "0cea14c0c079", "mac": "0CEA14C0C079", "hw_revision": 960263, "firmware": "6.0.11",
}


def device(hass, identifier=None):
    suffix = f"_{identifier}" if identifier else ""
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{ENTRY_ID}{suffix}"), ENTRY_ID
    )


@pytest.mark.parametrize("model,manufacturer", [
    ("ST18000NM000J-2TV103", "Seagate"),
    ("WDC WD180EDGZ-11B2DA0", "Western Digital"),
    ("WUH721818ALE6L4", "Western Digital"),
    ("HGST HUH721212ALE604", "HGST"),
    ("TOSHIBA MG09ACA18TE", "Toshiba"),
    ("Samsung SSD 990 PRO 2TB", "Samsung"),
    ("CT1000P3PSSD8", "Crucial"),
    ("Acme Drive 1", "Acme"),
    ("X123", None),
    ("Unknown", None),
])
def test_drive_manufacturer(model, manufacturer):
    assert drive_manufacturer(model) == manufacturer


async def test_main_device_comes_from_the_nas(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    main = device(hass)
    dr.async_get(hass).async_update_device(main.id, name_by_user="LIL NAS X", area_id="hallway")
    fire(hass, "system/device", json.dumps(NAS))
    await settle(hass)

    main = device(hass)
    assert (main.name, main.name_by_user, main.area_id) == ("UNAS", "LIL NAS X", "hallway")
    assert (main.manufacturer, main.model, main.model_id) == ("Ubiquiti", "UNAS Pro", "UNAS-Pro")
    assert (main.serial_number, main.sw_version, main.hw_version) == (
        "0cea14c0c079", "6.0.11", "960263"
    )
    assert (dr.CONNECTION_NETWORK_MAC, "0c:ea:14:c0:c0:79") in main.connections
    assert main.configuration_url == "https://192.168.1.25"


async def test_an_ipv6_host_gets_a_valid_url(hass, mqtt_mock, ssh):
    await setup_entry(hass, host="fd00::25")
    assert device(hass).configuration_url == "https://[fd00::25]"


async def test_child_devices_link_to_the_main_device(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    main = device(hass)
    for identifier in ("hdd_1", "share_plex", f"backup_{TASK['id']}"):
        assert device(hass, identifier).via_device_id == main.id
    hdd = device(hass, "hdd_1")
    assert (hdd.manufacturer, hdd.model, hdd.serial_number) == ("Seagate", "ST18000NM000J", "ZR1")


async def test_a_swapped_drive_updates_its_device(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    before = device(hass, "hdd_1")
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ENTRY_ID}_unas_hdd_1_temperature"
    )

    for key, value in {"model": "WDC WD180EDGZ", "serial": "9MG0", "firmware": "85.00A85"}.items():
        fire(hass, f"hdd/1/{key}", value)
    await settle(hass)

    after = device(hass, "hdd_1")
    assert after.id == before.id
    assert (after.manufacturer, after.model, after.serial_number, after.sw_version) == (
        "Western Digital", "WDC WD180EDGZ", "9MG0", "85.00A85"
    )
    assert er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ENTRY_ID}_unas_hdd_1_temperature"
    ) == entity_id


async def test_main_device_without_the_product_table(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire(hass, "system/device", json.dumps({**NAS, "product": None, "sku": None}))
    await settle(hass)
    assert (device(hass).model, device(hass).model_id) == ("UniFi UNAS", "UNASPRO")
