from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState

from conftest import DOMAIN, ROOT_TOPIC, entry_data, setup_entry


def interval_publishes(mqtt_mock):
    return [
        (call.args[1], call.args[3])
        for call in mqtt_mock.async_publish.call_args_list
        if call.args[0] == f"{ROOT_TOPIC}/control/monitor_interval"
    ]


async def test_migration_moves_the_interval_to_options(hass, mqtt_mock, ssh):
    entry = await setup_entry(
        hass, scan_interval=5.0, last_deploy_version="0.9.7", last_cleanup_version="0.9.7"
    )
    assert entry.minor_version == 2
    assert entry.options == {"scan_interval": 5}
    assert not {"scan_interval", "last_deploy_version", "last_cleanup_version"} & set(entry.data)
    assert entry.data["mqtt_password"] == "x"
    assert interval_publishes(mqtt_mock) == [("5", True)]


async def test_interval_change_applies_without_a_reload(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    runtime = entry.runtime_data
    mqtt_mock.async_publish.reset_mock()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})["scan_interval"] == 5
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"scan_interval": 10.0})
    await hass.async_block_till_done()

    assert entry.options == {"scan_interval": 10}
    assert interval_publishes(mqtt_mock) == [("10", True)]
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is runtime
    assert len(ssh.instances) == 1


async def test_new_entries_keep_the_interval_in_options(hass, mqtt_mock, ssh):
    with (
        patch("custom_components.unifi_unas.config_flow.UNASProConfigFlow._test_ssh", return_value=None),
        patch("custom_components.unifi_unas.config_flow.UNASProConfigFlow._test_mqtt", return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**entry_data(), "scan_interval": 15.0, "mqtt_tls": False}
        )
        await hass.async_block_till_done()
    entry = result["result"]
    assert entry.options == {"scan_interval": 15}
    assert "scan_interval" not in entry.data
    assert entry.minor_version == 2
