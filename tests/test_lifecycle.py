import asyncio
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from conftest import (
    DOMAIN,
    ENTRY_ID,
    ROOT_TOPIC,
    entry_data,
    fire_monitor_cycle,
    settle,
    setup_entry,
)
from custom_components.unifi_unas import (
    UNAS_UNINSTALL_COMMANDS,
    async_remove_config_entry_device,
    repairs,
)


async def test_version_1_entries_migrate_to_2_2(hass, mqtt_mock, ssh):
    data = {key: value for key, value in entry_data().items() if key != "device_model"}
    entry = MockConfigEntry(
        domain=DOMAIN, data={**data, "last_deploy_version": "0.9.6"}, entry_id=ENTRY_ID,
        version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert (entry.version, entry.minor_version, entry.state) == (2, 2, ConfigEntryState.LOADED)
    assert entry.data["device_model"] == "UNAS_PRO"
    assert "last_deploy_version" not in entry.data and "scan_interval" not in entry.data
    assert entry.options == {"scan_interval": 5}


async def test_entries_from_a_newer_version_are_left_alone(hass, mqtt_mock, ssh):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), entry_id=ENTRY_ID, version=3)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


async def test_setup_waits_for_mqtt(hass, mqtt_mock, ssh):
    with patch("homeassistant.components.mqtt.async_wait_for_mqtt_client", return_value=False):
        entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), entry_id=ENTRY_ID, version=2)
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def remove_with_retained(hass, entry):
    task = asyncio.create_task(hass.config_entries.async_remove(entry.entry_id))
    await asyncio.sleep(0.1)
    for topic in ("availability", "system/cpu_temp"):
        async_fire_mqtt_message(hass, f"{ROOT_TOPIC}/{topic}", "1", retain=True)
    await task
    await hass.async_block_till_done()


def cleared(mqtt_mock):
    return {
        call.args[0] for call in mqtt_mock.async_publish.call_args_list
        if call.args[1] == "" and call.args[3]
    }


async def test_removal_uninstalls_then_clears_the_retained_topics(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    mqtt_mock.async_publish.reset_mock()
    await remove_with_retained(hass, entry)
    # the last manager is the one removal built
    assert ssh.instances[-1].commands == list(UNAS_UNINSTALL_COMMANDS)
    assert cleared(mqtt_mock) == {f"{ROOT_TOPIC}/availability", f"{ROOT_TOPIC}/system/cpu_temp"}


async def test_removal_clears_the_topics_even_when_the_nas_is_gone(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)

    async def unreachable(self, command, timeout=None, input=None):
        raise OSError("no route to host")

    mqtt_mock.async_publish.reset_mock()
    with patch.object(ssh, "execute_command", unreachable):
        await remove_with_retained(hass, entry)
    assert cleared(mqtt_mock) == {f"{ROOT_TOPIC}/availability", f"{ROOT_TOPIC}/system/cpu_temp"}


async def test_the_main_device_cannot_be_deleted(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, ENTRY_ID), ENTRY_ID)
    assert not await async_remove_config_entry_device(hass, entry, device)


async def test_the_host_key_repair_pins_again(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass, verify_host_key=True, host_key="192.168.1.25 ssh-ed25519 AAAA")
    flow = await repairs.async_create_fix_flow(
        hass, f"host_key_changed_{ENTRY_ID}", {"entry_id": ENTRY_ID}
    )
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["description_placeholders"] == {"host": "192.168.1.25"}
    assert (await flow.async_step_confirm({}))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert "host_key" not in entry.data
    assert entry.state is ConfigEntryState.LOADED
