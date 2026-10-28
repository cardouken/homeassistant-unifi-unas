import json
from unittest.mock import AsyncMock, patch

import asyncssh
import pytest
from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import DOMAIN, ENTRY_ID, entry_data, fire, settle, setup_entry

FLOW = "custom_components.unifi_unas.config_flow"
IDENTITY = {
    "sysid": "ea51", "serial": "0CEA14C0C079", "product": "UNAS Pro",
    "name": "UniFi Network Attached Storage Pro",
}


def user_input(**overrides):
    data = {key: value for key, value in entry_data().items() if key != "device_model"}
    data.update(overrides)
    return data


async def submit(hass, identity=IDENTITY, ssh_error=None, mqtt_error=None, **overrides):
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(ssh_error, identity)),
        patch(f"{FLOW}.probe_mqtt", return_value=mqtt_error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input(**overrides)
        )
        await hass.async_block_till_done()
    return result


async def test_the_model_and_unique_id_come_from_the_nas(hass, mqtt_mock, ssh):
    result = await submit(hass)
    entry = result["result"]
    assert (entry.unique_id, entry.data["device_model"], entry.title) == (
        "0cea14c0c079", "UNAS_PRO", "UNAS (192.168.1.25)"
    )
    assert entry.options == {"scan_interval": 5}


async def test_an_unrecognized_device_needs_confirmation(hass, mqtt_mock, ssh):
    udm = {
        "sysid": "ea15", "serial": "aabbccddeeff", "product": "UDM SE",
        "name": "UniFi Dream Machine SE",
    }
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(None, udm)),
        patch(f"{FLOW}.probe_mqtt", return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input())
        assert (result["step_id"], result["description_placeholders"]["device"]) == (
            "unrecognized_device", "UDM SE"
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_model": "UNAS_PRO_8"}
        )
        await hass.async_block_till_done()
    assert result["result"].data["device_model"] == "UNAS_PRO_8"


async def test_a_console_without_identity_still_works_by_host(hass, mqtt_mock, ssh):
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(None, None)),
        patch(f"{FLOW}.probe_mqtt", return_value=None),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input())
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device_model": "UNAS_PRO"}
        )
        await hass.async_block_till_done()
    assert result["result"].unique_id == "192.168.1.25"


@pytest.mark.parametrize("ssh_error", ["invalid_auth", "timeout_connect", "cannot_connect"])
async def test_ssh_errors_are_shown(hass, mqtt_mock, ssh, ssh_error):
    result = await submit(hass, identity=None, ssh_error=ssh_error)
    assert result["errors"] == {"base": ssh_error}


@pytest.mark.parametrize("mqtt_error", ["mqtt_invalid_auth", "mqtt_timeout", "mqtt_cannot_connect"])
async def test_mqtt_errors_are_shown(hass, mqtt_mock, ssh, mqtt_error):
    result = await submit(hass, mqtt_error=mqtt_error)
    assert (result["step_id"], result["errors"]) == ("user", {"base": mqtt_error})


async def test_a_configured_host_aborts_before_any_network_test(hass, mqtt_mock, ssh):
    MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id="192.168.1.25").add_to_hass(hass)
    probe = AsyncMock(return_value=(None, IDENTITY))
    with patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", probe):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input())
    assert result["reason"] == "already_configured"
    probe.assert_not_called()


async def test_the_same_nas_on_a_new_address_updates_the_entry(hass, mqtt_mock, ssh):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(), unique_id="0cea14c0c079", version=2, minor_version=2
    )
    entry.add_to_hass(hass)
    result = await submit(hass, host="192.168.1.99")
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.99"


async def test_existing_entries_adopt_the_serial(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    assert entry.unique_id == "192.168.1.25"
    device = {"sysid": "ea51", "serial": "0cea14c0c079", "name": "UNAS Pro"}
    fire(hass, "system/device", json.dumps(device))
    await settle(hass)
    assert entry.unique_id == "0cea14c0c079"
    assert entry.entry_id == ENTRY_ID


@pytest.mark.parametrize("error,expected", [
    (asyncssh.PermissionDenied("denied"), ("invalid_auth", None)),
    (TimeoutError(), ("timeout_connect", None)),
    (OSError("no route"), ("cannot_connect", None)),
])
async def test_ssh_probe_errors(hass, error, expected):
    from custom_components.unifi_unas.config_flow import UNASProConfigFlow

    flow = UNASProConfigFlow()
    flow.hass = hass
    with patch(f"{FLOW}.SSHManager.identity", side_effect=error):
        assert await flow._probe_ssh("192.0.2.1", "root", "x") == expected


async def test_ssh_probe_tolerates_a_console_without_identity(hass):
    from custom_components.unifi_unas.config_flow import UNASProConfigFlow
    from custom_components.unifi_unas.ssh_manager import NASCommandError

    flow = UNASProConfigFlow()
    flow.hass = hass
    with patch(
        f"{FLOW}.SSHManager.identity", side_effect=NASCommandError("GET /api/system: refused")
    ):
        assert await flow._probe_ssh("192.0.2.1", "root", "x") == (None, None)


async def test_mqtt_is_required(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["reason"] == "mqtt_required"


@pytest.mark.parametrize("mqtt_error", [None, "mqtt_tls_verify_failed"])
async def test_tls_asks_for_the_port(hass, mqtt_mock, ssh, mqtt_error):
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(None, IDENTITY)),
        patch(f"{FLOW}.probe_mqtt", return_value=mqtt_error) as mqtt_probe,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input(mqtt_tls=True)
        )
        assert result["step_id"] == "mqtt_tls"
        mqtt_probe.assert_not_called()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"mqtt_port": 8884, "mqtt_tls_insecure": True}
        )
        await hass.async_block_till_done()
    assert mqtt_probe.call_args.args == ("192.168.1.111", 8884, "unas", "x", True, True)
    if mqtt_error:
        assert (result["step_id"], result["errors"]) == ("mqtt_tls", {"base": mqtt_error})
    else:
        data = result["result"].data
        assert (data["mqtt_tls"], data["mqtt_port"], data["mqtt_tls_insecure"]) == (
            True, 8884, True
        )


async def test_unexpected_ssh_probe_errors(hass):
    from custom_components.unifi_unas.config_flow import UNASProConfigFlow

    flow = UNASProConfigFlow()
    flow.hass = hass
    with patch(f"{FLOW}.SSHManager.identity", side_effect=RuntimeError("boom")):
        assert await flow._probe_ssh("192.0.2.1", "root", "x") == ("unknown", None)


async def test_ssh_probe_returns_the_identity(hass):
    from custom_components.unifi_unas.config_flow import UNASProConfigFlow

    flow = UNASProConfigFlow()
    flow.hass = hass
    with patch(f"{FLOW}.SSHManager.identity", return_value=IDENTITY):
        assert await flow._probe_ssh("192.0.2.1", "root", "x") == (None, IDENTITY)
