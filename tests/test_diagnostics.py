import json

from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from conftest import fire, fire_monitor_cycle, settle, setup_entry

# long enough to search as substrings, the short ones only as whole values
SECRETS = (
    "sshpass", "AAAA", "0cea14c0c079", "someone@example.com", "ZR1", "10.9.8.7", "backupuser"
)
SHORT_SECRETS = ("x", "unas", "abc")


async def test_diagnostics_are_redacted(hass, hass_client, mqtt_mock, ssh):
    ssh_task = {
        "id": "t1", "name": "B2", "createdBy": {"email": "someone@example.com"},
        "remote": {"type": "rsync", "ip": "10.9.8.7", "user": "backupuser", "port": 22},
    }
    entry = await setup_entry(hass, password="sshpass", host_key="192.168.1.25 ssh-ed25519 AAAA")
    ssh.instances[0].backup_tasks = [ssh_task]
    await entry.runtime_data.service.async_refresh()
    fire_monitor_cycle(hass)
    device = {"name": "UNAS Pro", "serial": "0cea14c0c079", "mac": "0CEA14C0C079"}
    fire(hass, "system/device", json.dumps(device))
    await settle(hass)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    dump = json.dumps(diagnostics)
    assert diagnostics["mqtt"]["inventory"]["bays"] == ["1", "2"]
    assert diagnostics["ssh"]["data"]["backup_tasks"][0]["name"] == "B2"
    assert diagnostics["deploy"]["attempts"] == 0
    for secret in SECRETS:
        assert secret not in dump, secret
    for secret in SHORT_SECRETS:
        assert f'"{secret}"' not in dump, secret
