from datetime import timedelta

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from conftest import DOMAIN, ENTRY_ID, ROOT_TOPIC, TASK, fire, fire_monitor_cycle, settle, setup_entry

GRACE = timedelta(seconds=601)


def device(hass, identifier):
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"{ENTRY_ID}_{identifier}"), ENTRY_ID)


def entity_id(hass, platform, key):
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ENTRY_ID}_{key}")


async def later(hass, freezer, delta=GRACE, **cycle):
    freezer.tick(delta)
    fire_monitor_cycle(hass, **cycle)
    await settle(hass)


async def test_items_are_added_from_the_inventory(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"))
    await settle(hass)
    assert device(hass, "hdd_1") and device(hass, "hdd_2") and device(hass, "share_plex")
    assert hass.states.get(entity_id(hass, "sensor", "unas_pool1_usage")).state == "60"


async def test_silence_never_removes_anything(hass, mqtt_mock, ssh, freezer):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)

    fire(hass, "availability", "offline")
    for _ in range(4):
        freezer.tick(timedelta(minutes=30))
        await settle(hass)
    assert device(hass, "hdd_1") and device(hass, "share_plex")
    assert entity_id(hass, "sensor", "unas_pool1_usage")
    assert hass.states.get(entity_id(hass, "sensor", "unas_hdd_1_temperature")).state == "unavailable"


async def test_an_item_gone_from_the_inventory_is_removed_after_the_grace(hass, mqtt_mock, ssh, freezer):
    await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"))
    await settle(hass)
    temperature = entity_id(hass, "sensor", "unas_hdd_2_temperature")

    await later(hass, freezer, timedelta(seconds=5), bays=("1",))
    assert hass.states.get(temperature).state == "unavailable"
    assert device(hass, "hdd_2")

    mqtt_mock.async_publish.reset_mock()
    await later(hass, freezer, bays=("1",))
    assert device(hass, "hdd_2") is None
    assert entity_id(hass, "sensor", "unas_hdd_2_temperature") is None
    assert device(hass, "hdd_1")
    cleared = {call.args[0] for call in mqtt_mock.async_publish.call_args_list if call.args[1] == ""}
    assert f"{ROOT_TOPIC}/hdd/2/temperature" in cleared
    assert not any("/hdd/1/" in topic for topic in cleared)


async def test_a_blip_restarts_the_grace(hass, mqtt_mock, ssh, freezer):
    await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"))
    await settle(hass)
    for bays in (("1",), ("1", "2"), ("1",)):
        await later(hass, freezer, timedelta(seconds=400), bays=bays)
    assert device(hass, "hdd_2")


async def test_a_failed_source_or_an_offline_monitor_never_removes(hass, mqtt_mock, ssh, freezer):
    await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"), shares=("plex", "yarr"))
    await settle(hass)

    failed = {"bays": None, "nvme": [], "pools": [1], "shares": ["plex", "yarr"]}
    await later(hass, freezer, bays=("1",), shares=("plex", "yarr"), inventory=failed)
    await later(hass, freezer, bays=("1",), shares=("plex", "yarr"), inventory=failed)
    assert device(hass, "hdd_2")

    fire_monitor_cycle(hass, bays=("1", "2"), shares=("plex",))
    fire(hass, "availability", "offline")
    await settle(hass)
    freezer.tick(GRACE)
    await settle(hass)
    assert device(hass, "share_yarr")


async def test_items_gone_while_home_assistant_was_down(hass, mqtt_mock, ssh, freezer):
    entry = await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2", "3"))
    await settle(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert await hass.config_entries.async_setup(entry.entry_id)
    fire_monitor_cycle(hass, bays=("1", "2"))
    await settle(hass)
    assert device(hass, "hdd_3")
    await later(hass, freezer, bays=("1", "2"))
    assert device(hass, "hdd_3") is None


async def test_backup_tasks_go_only_when_the_api_says_so(hass, mqtt_mock, ssh, freezer):
    entry = await setup_entry(hass)
    fake = ssh.instances[0]
    service = entry.runtime_data.service
    assert device(hass, f"backup_{TASK['id']}")

    # API failure: the last list is kept, never removed from
    fake.backup_tasks = None
    for _ in range(2):
        freezer.tick(GRACE)
        await service.async_refresh()
        await settle(hass)
    assert device(hass, f"backup_{TASK['id']}")
    assert hass.states.get(entity_id(hass, "sensor", f"backup_{TASK['id']}_status")).state == "idle"

    # SSH down: unavailable, kept
    fake.error = OSError("no route to host")
    freezer.tick(GRACE)
    await service.async_refresh()
    await settle(hass)
    assert hass.states.get(entity_id(hass, "button", f"backup_{TASK['id']}")).state == "unavailable"
    assert device(hass, f"backup_{TASK['id']}")

    fake.error = None
    fake.backup_tasks = []
    for _ in range(2):
        freezer.tick(GRACE)
        await service.async_refresh()
        await settle(hass)
    assert device(hass, f"backup_{TASK['id']}") is None
    assert entity_id(hass, "switch", f"backup_{TASK['id']}_schedule_enabled") is None


async def test_only_devices_the_nas_no_longer_reports_can_be_deleted(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"))
    await settle(hass)
    fire_monitor_cycle(hass, bays=("1",))
    await settle(hass)

    reconciler = entry.runtime_data.reconciler
    main = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ENTRY_ID), ENTRY_ID)
    assert not reconciler.async_remove_device(main)
    assert not reconciler.async_remove_device(device(hass, "hdd_1"))
    assert reconciler.async_remove_device(device(hass, "hdd_2"))


async def test_an_old_monitor_without_inventory(hass, mqtt_mock, ssh, freezer):
    await setup_entry(hass)
    fire_monitor_cycle(hass, bays=("1", "2"), inventory=False)
    await settle(hass)
    assert device(hass, "hdd_2")
    await later(hass, freezer, bays=("1",), inventory=False)
    await later(hass, freezer, bays=("1",), inventory=False)
    assert device(hass, "hdd_2")


async def test_devices_of_an_unloaded_entry_can_be_deleted(hass, mqtt_mock, ssh):
    from custom_components.unifi_unas import async_remove_config_entry_device

    entry = await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    main = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ENTRY_ID), ENTRY_ID)
    assert not await async_remove_config_entry_device(hass, entry, main)
    assert await async_remove_config_entry_device(hass, entry, device(hass, "hdd_1"))
