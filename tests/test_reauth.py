from unittest.mock import patch

import asyncssh
from homeassistant.config_entries import SOURCE_REAUTH

from conftest import DOMAIN, settle, setup_entry

PROBE = "custom_components.unifi_unas.config_flow.UNASProConfigFlow._probe_ssh"


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
    with patch(PROBE, return_value=(None, {"serial": "0CEA14C0C079"})):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "new"})
        await settle(hass)
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
    assert entry.data["mqtt_password"] == "x"
    assert len(ssh.instances) == 2


async def test_reauth_keeps_asking_on_a_wrong_password(hass, mqtt_mock, ssh):
    entry, flow_id = await start_reauth(hass, ssh)
    with patch(PROBE, return_value=("invalid_auth", None)):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "nope"})
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data["password"] == "old"


async def test_reauth_refuses_a_different_nas(hass, mqtt_mock, ssh):
    entry, flow_id = await start_reauth(hass, ssh)
    hass.config_entries.async_update_entry(entry, unique_id="0cea14c0c079")
    with patch(PROBE, return_value=(None, {"serial": "aabbccddeeff"})):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "new"})
    assert result["reason"] == "wrong_device"
    assert entry.data["password"] == "old"


async def test_reauth_with_the_same_login_reloads(hass, mqtt_mock, ssh):
    entry, flow_id = await start_reauth(hass, ssh)
    with patch(PROBE, return_value=(None, {"serial": "0CEA14C0C079"})):
        result = await hass.config_entries.flow.async_configure(flow_id, {"username": "root", "password": "old"})
        await settle(hass)
    assert result["reason"] == "reauth_successful"
    assert len(ssh.instances) == 2
