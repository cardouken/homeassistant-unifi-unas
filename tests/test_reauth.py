from unittest.mock import patch

import asyncssh
from homeassistant.config_entries import SOURCE_REAUTH

from conftest import DOMAIN, settle, setup_entry

TEST_SSH = "custom_components.unifi_unas.config_flow.UNASProConfigFlow._test_ssh"


async def start_reauth(hass, ssh):
    entry = await setup_entry(hass, password="old")
    ssh.instances[0].error = asyncssh.PermissionDenied("bad password")
    await entry.runtime_data.service.async_refresh()
    await settle(hass)
    flows = [flow for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
             if flow["context"]["source"] == SOURCE_REAUTH]
    assert len(flows) == 1
    return entry, flows[0]["flow_id"]


async def test_rejected_login_starts_reauth(hass, mqtt_mock, ssh):
    entry, flow_id = await start_reauth(hass, ssh)
    with patch(TEST_SSH, return_value=None):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "new"})
        await settle(hass)
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.data["mqtt_password"] == "x"
    assert len(ssh.instances) == 2


async def test_reauth_keeps_asking_on_a_wrong_password(hass, mqtt_mock, ssh):
    entry, flow_id = await start_reauth(hass, ssh)
    with patch(TEST_SSH, return_value="invalid_auth"):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "nope"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old"
