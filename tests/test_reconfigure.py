from unittest.mock import patch

import pytest
from homeassistant import config_entries

from conftest import DOMAIN, setup_entry

FLOW = "custom_components.unifi_unas.config_flow"
NAS = {"sysid": "ea51", "serial": "0CEA14C0C079"}
PIN = "192.168.1.25 ssh-ed25519 AAAAC3Nz"


async def reconfigure(hass, entry, identity=NAS, mqtt_error=None, **changes):
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(None, identity)) as probe,
        patch(f"{FLOW}.probe_mqtt", return_value=mqtt_error) as mqtt_probe,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        form = result
        fields = {
            "host": entry.data["host"], "username": entry.data["username"], "mqtt_host": entry.data["mqtt_host"],
            "mqtt_user": entry.data["mqtt_user"], "mqtt_tls": False, "device_name": "UNAS", "verify_host_key": True,
        }
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {**fields, **changes})
        await hass.async_block_till_done()
    return form, result, probe, mqtt_probe


@pytest.fixture
async def entry(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass, password="sshpass", verify_host_key=True, host_key=PIN)
    hass.config_entries.async_update_entry(entry, unique_id="0cea14c0c079")
    return entry


async def test_passwords_never_reach_the_form(hass, entry):
    form, *_ = await reconfigure(hass, entry)
    defaults = {str(key): getattr(key, "default", None) for key in form["data_schema"].schema}
    assert all(not callable(defaults[field]) for field in ("password", "mqtt_password"))


async def test_blank_passwords_keep_the_stored_ones_and_the_pin(hass, entry):
    _, result, probe, mqtt_probe = await reconfigure(hass, entry, device_name="LIL NAS X")
    assert result["reason"] == "reconfigure_successful"
    assert (entry.data["password"], entry.data["mqtt_password"], entry.data["host_key"]) == ("sshpass", "x", PIN)
    assert probe.call_args.args == ("192.168.1.25", "root", "sshpass")
    assert mqtt_probe.call_args.args[3] == "x"
    assert entry.title == "LIL NAS X (192.168.1.25)"


async def test_a_new_address_for_the_same_nas_pins_again(hass, entry):
    _, result, *_ = await reconfigure(hass, entry, host="192.168.1.99", password="newpass")
    assert result["reason"] == "reconfigure_successful"
    assert (entry.data["host"], entry.data["password"]) == ("192.168.1.99", "newpass")
    assert "host_key" not in entry.data
    assert entry.unique_id == "0cea14c0c079"


async def test_a_different_nas_is_refused(hass, entry):
    _, result, *_ = await reconfigure(hass, entry, identity={"serial": "aabbccddeeff"}, host="192.168.1.30")
    assert result["reason"] == "wrong_device"
    assert entry.data["host"] == "192.168.1.25"


async def test_an_ip_keyed_entry_takes_the_serial(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    _, result, *_ = await reconfigure(hass, entry)
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id == "0cea14c0c079"


async def test_mqtt_errors_keep_the_form(hass, entry):
    _, result, *_ = await reconfigure(hass, entry, mqtt_error="mqtt_invalid_auth")
    assert (result["step_id"], result["errors"]) == ("reconfigure", {"base": "mqtt_invalid_auth"})


async def test_only_changed_settings_reload(hass, entry, ssh):
    await reconfigure(hass, entry)
    assert len(ssh.instances) == 1
    await reconfigure(hass, entry, mqtt_password="newmqtt")
    assert len(ssh.instances) == 2
    assert entry.runtime_data.connection["mqtt_password"] == "newmqtt"


async def test_switching_tls_off_goes_back_to_the_plain_port(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass, mqtt_tls=True, mqtt_port=8883, mqtt_tls_insecure=True)
    hass.config_entries.async_update_entry(entry, unique_id="0cea14c0c079")
    _, result, _, mqtt_probe = await reconfigure(hass, entry)
    assert result["reason"] == "reconfigure_successful"
    assert mqtt_probe.call_args.args[1] == 1883
    assert not entry.data["mqtt_tls"]
    assert "mqtt_port" not in entry.data
    assert "mqtt_tls_insecure" not in entry.data
    # what the NAS gets in config.json
    assert ssh.instances[-1].kwargs["mqtt_port"] == 1883
