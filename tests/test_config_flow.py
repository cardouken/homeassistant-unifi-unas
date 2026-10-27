from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import DOMAIN, entry_data

FLOW = "custom_components.unifi_unas.config_flow"


def user_input(**overrides):
    data = {key: value for key, value in entry_data().items() if key not in ("device_name",)}
    data.update(overrides)
    return data


async def start(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})


@pytest.mark.parametrize("probe_result", ["mqtt_invalid_auth", "mqtt_timeout", "mqtt_cannot_connect"])
async def test_mqtt_errors_are_shown(hass, mqtt_mock, ssh, probe_result):
    with (
        patch(f"{FLOW}.UNASProConfigFlow._test_ssh", return_value=None),
        patch(f"{FLOW}.probe_mqtt", return_value=probe_result),
    ):
        result = await hass.config_entries.flow.async_configure((await start(hass))["flow_id"], user_input())
    assert result["errors"] == {"base": probe_result}


async def test_a_configured_host_aborts_before_any_network_test(hass, mqtt_mock, ssh):
    MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id="192.168.1.25").add_to_hass(hass)
    test_ssh = AsyncMock(return_value=None)
    with patch(f"{FLOW}.UNASProConfigFlow._test_ssh", test_ssh):
        result = await hass.config_entries.flow.async_configure((await start(hass))["flow_id"], user_input())
    assert result["reason"] == "already_configured"
    test_ssh.assert_not_called()
