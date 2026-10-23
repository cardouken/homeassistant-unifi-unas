import asyncio
from datetime import timedelta

import asyncssh
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from conftest import DOMAIN, ENTRY_ID, TASK, fire, fire_fan_settings, fire_monitor_cycle, settle, setup_entry

OUTDATED = ["/root/unifi_unas/fan_control.py"]


def state(hass, platform, key):
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{ENTRY_ID}_{key}")
    return hass.states.get(entity_id).state


async def refresh(hass, entry):
    await entry.runtime_data.service.async_refresh()
    await hass.async_block_till_done()


async def test_mqtt_entities_work_without_ssh(hass, mqtt_mock, ssh):
    ssh.error = None
    entry = await setup_entry(hass)
    ssh.instances[0].error = OSError("no route to host")
    await refresh(hass, entry)
    fire_monitor_cycle(hass)
    fire_fan_settings(hass)
    fire(hass, "fan/availability", "online")
    await settle(hass)

    assert entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor", "unas_cpu_temp") == "50"
    assert state(hass, "select", "fan_mode") == "Target Temp"
    assert state(hass, "number", "fan_curve_target_temp") == "43"
    # unknown, not a false "off"
    assert state(hass, "binary_sensor", "fan_control_running") == "unavailable"
    assert state(hass, "binary_sensor", "scripts_installed") == "unavailable"


async def test_fan_controls_follow_the_fan_daemon(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    fire_fan_settings(hass)
    await settle(hass)
    # an older daemon without fan/availability: follow the monitor
    assert state(hass, "select", "fan_mode") == "Target Temp"

    fire(hass, "fan/availability", "offline")
    await settle(hass)
    assert state(hass, "select", "fan_mode") == "unavailable"
    assert state(hass, "sensor", "unas_cpu_temp") == "50"

    fire(hass, "fan/availability", "online")
    fire(hass, "availability", "offline")
    await settle(hass)
    assert state(hass, "select", "fan_mode") == "Target Temp"
    assert state(hass, "sensor", "unas_cpu_temp") == "unavailable"


async def test_service_sensors(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    assert state(hass, "binary_sensor", "fan_control_running") == "on"
    assert state(hass, "binary_sensor", "scripts_installed") == "on"

    ssh.instances[0].services["fan_control"] = "inactive"
    await refresh(hass, entry)
    assert state(hass, "binary_sensor", "fan_control_running") == "off"


async def test_outdated_scripts_deploy_in_the_background(hass, mqtt_mock, ssh):
    gate = asyncio.Event()
    fake = None

    def hold(instance):
        instance.outdated = list(OUTDATED)
        instance.deploy_gate = gate

    original_init = ssh.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        hold(self)

    ssh.__init__ = init
    try:
        entry = await setup_entry(hass)
    finally:
        ssh.__init__ = original_init
    fake = ssh.instances[0]

    # setup returned while the deploy is still running
    assert entry.state is ConfigEntryState.LOADED
    assert fake.deploys == [OUTDATED]
    assert entry.runtime_data.deployer.running

    gate.set()
    await settle(hass)
    await refresh(hass, entry)
    assert fake.deploys == [OUTDATED]
    assert entry.runtime_data.deployer.attempts == 0
    assert state(hass, "binary_sensor", "scripts_installed") == "on"


async def test_failed_deploys_back_off_then_raise_a_repair(hass, mqtt_mock, ssh, freezer):
    entry = await setup_entry(hass)
    fake = ssh.instances[0]
    deployer = entry.runtime_data.deployer
    fake.outdated = list(OUTDATED)
    fake.deploy_error = OSError("disk full")

    await refresh(hass, entry)
    await settle(hass)
    assert len(fake.deploys) == 1

    # the next regular checks wait out the backoff
    await refresh(hass, entry)
    await settle(hass)
    assert len(fake.deploys) == 1

    issues = ir.async_get(hass)
    for delay in (61, 301):
        freezer.tick(timedelta(seconds=delay))
        await refresh(hass, entry)
        await settle(hass)
    assert len(fake.deploys) == 3
    issue = issues.async_get_issue(DOMAIN, f"deploy_failed_{ENTRY_ID}")
    assert issue.translation_placeholders["error"] == "OSError: disk full"

    fake.deploy_error = None
    freezer.tick(timedelta(seconds=901))
    await refresh(hass, entry)
    await settle(hass)
    await refresh(hass, entry)
    assert len(fake.deploys) == 4
    assert deployer.attempts == 0
    assert issues.async_get_issue(DOMAIN, f"deploy_failed_{ENTRY_ID}") is None


async def test_reinstall_runs_in_the_background(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    fake = ssh.instances[0]
    fake.deploy_gate = asyncio.Event()
    button = er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{ENTRY_ID}_reinstall_scripts")

    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert fake.deploys == [None]
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)

    fake.deploy_gate.set()
    await settle(hass)
    assert not entry.runtime_data.deployer.running


async def test_host_key_mismatch_raises_the_repair(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    ssh.instances[0].error = asyncssh.HostKeyNotVerifiable("key mismatch")
    await refresh(hass, entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"host_key_changed_{ENTRY_ID}")
    assert state(hass, "binary_sensor", "monitor_running") == "unavailable"


async def test_unload_leaves_nothing_running(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unlimited_share_quota_has_no_unit(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ENTRY_ID}_unas_share_plex_quota")
    quota = hass.states.get(entity_id)
    assert quota.state == "Unlimited"
    assert "unit_of_measurement" not in quota.attributes


async def test_ssh_down_at_startup_keeps_backup_entities(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    status_uid = f"{ENTRY_ID}_backup_{TASK['id']}_status"
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, status_uid)
    assert await hass.config_entries.async_unload(entry.entry_id)

    original_init = ssh.__init__

    def failing(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.error = OSError("no route to host")

    ssh.__init__ = failing
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await settle(hass)
    finally:
        ssh.__init__ = original_init
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, status_uid)
    assert entity_id
    assert hass.states.get(entity_id).state == "unavailable"


async def test_values_a_failing_monitor_section_stops_sending_go_unavailable(
        hass, mqtt_mock, ssh, freezer
):
    await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    for _ in range(11):
        # only the system section keeps reporting
        freezer.tick(timedelta(seconds=30))
        fire(hass, "system/cpu_temp", 51)
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
    assert state(hass, "sensor", "unas_cpu_temp") == "51"
    assert state(hass, "sensor", "unas_hdd_1_temperature") == "unavailable"


async def test_update_entity_on_a_pushed_entity(hass, mqtt_mock, ssh, caplog):
    entry = await setup_entry(hass)
    fire_monitor_cycle(hass)
    await settle(hass)
    await async_setup_component(hass, "homeassistant", {})
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{ENTRY_ID}_unas_cpu_temp"
    )
    await hass.services.async_call(
        "homeassistant", "update_entity", {"entity_id": entity_id}, blocking=True
    )
    assert entry.runtime_data.data.last_update_success
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert state(hass, "sensor", "unas_cpu_temp") == "50"


async def test_deploys_that_leave_files_outdated_raise_a_repair(hass, mqtt_mock, ssh, freezer):
    entry = await setup_entry(hass)
    fake = ssh.instances[0]
    # every deploy goes through, the NAS keeps reporting the file outdated
    fake.outdated = list(OUTDATED)
    fake.deploy_fixes = False
    issues = ir.async_get(hass)
    for delay in (0, 61, 301, 901):
        freezer.tick(timedelta(seconds=delay))
        await refresh(hass, entry)
        await settle(hass)
    assert len(fake.deploys) == 4
    issue = issues.async_get_issue(DOMAIN, f"deploy_failed_{ENTRY_ID}")
    error = issue.translation_placeholders["error"]
    assert error == "fan_control.py still outdated after deploying"
