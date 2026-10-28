from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import (
    CONF_DEVICE_MODEL,
    CONF_HOST,
    CONF_HOST_KEY,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_TLS,
    CONF_MQTT_TLS_INSECURE,
    CONF_MQTT_USER,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
    CONF_VERIFY_HOST_KEY,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_MQTT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    get_mqtt_root,
    get_mqtt_topics,
)
from .coordinator import UNASDataCoordinator, UNASServiceCoordinator
from .deploy import Deployer
from .devices import DeviceSync, async_register_main_device
from .reconcile import Reconciler
from .ssh_manager import BUNDLE_DIR, LEGACY_CLEANUP, SSHManager
from .stale_topics import async_schedule_scan

_LOGGER = logging.getLogger(__name__)

OBSOLETE_DATA = (CONF_SCAN_INTERVAL, "last_cleanup_version", "last_deploy_version")

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
]

UNAS_UNINSTALL_COMMANDS = [
    "systemctl stop unas_monitor || true",
    "systemctl stop fan_control || true",
    "systemctl disable unas_monitor || true",
    "systemctl disable fan_control || true",
    "rm -f /etc/systemd/system/unas_monitor.service",
    "rm -f /etc/systemd/system/fan_control.service",
    f"rm -rf {BUNDLE_DIR}",
    LEGACY_CLEANUP,
    "systemctl daemon-reload",
    "echo 2 > /sys/class/hwmon/hwmon0/pwm1_enable || true",
    "echo 2 > /sys/class/hwmon/hwmon0/pwm2_enable || true",
]


@dataclass
class UNASRuntime:
    data: UNASDataCoordinator
    service: UNASServiceCoordinator
    deployer: Deployer
    reconciler: Reconciler
    ssh: SSHManager
    connection: dict


def connection_data(entry: ConfigEntry) -> dict:
    # what setup connects with; a pinned host key is recorded at runtime and needs no reload
    return {key: value for key, value in entry.data.items() if key != CONF_HOST_KEY}


type UNASConfigEntry = ConfigEntry[UNASRuntime]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version > 2:
        return False

    if entry.version == 1:
        new_data = {**entry.data}
        if CONF_DEVICE_MODEL not in new_data:
            new_data[CONF_DEVICE_MODEL] = DEFAULT_DEVICE_MODEL
            _LOGGER.info(
                "Migrated config entry to version 2, added device model: %s", DEFAULT_DEVICE_MODEL
            )

        hass.config_entries.async_update_entry(entry, data=new_data, version=2, minor_version=1)

    if entry.minor_version < 2:
        # the interval is an option now, and the deploy version bookkeeping gave way to file hashes
        data = {key: value for key, value in entry.data.items() if key not in OBSOLETE_DATA}
        interval = int(entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        options = {**entry.options, CONF_SCAN_INTERVAL: interval}
        hass.config_entries.async_update_entry(entry, data=data, options=options, minor_version=2)
        _LOGGER.info("Migrated config entry to version 2.2")

    return True


def _build_ssh_manager(entry: ConfigEntry) -> SSHManager:
    return SSHManager(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data.get(CONF_PASSWORD),
        mqtt_host=entry.data.get(CONF_MQTT_HOST),
        mqtt_user=entry.data.get(CONF_MQTT_USER),
        mqtt_password=entry.data.get(CONF_MQTT_PASSWORD),
        mqtt_port=entry.data.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
        mqtt_tls=entry.data.get(CONF_MQTT_TLS, False),
        mqtt_tls_insecure=entry.data.get(CONF_MQTT_TLS_INSECURE, False),
        verify_host_key=entry.data.get(CONF_VERIFY_HOST_KEY, False),
        pinned_host_key=entry.data.get(CONF_HOST_KEY),
    )


async def async_setup_entry(hass: HomeAssistant, entry: UNASConfigEntry) -> bool:
    if not await mqtt.async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("The MQTT integration is not available")
    # raised by 0.9.x and never cleared there
    ir.async_delete_issue(hass, DOMAIN, "mqtt_missing")

    ssh = _build_ssh_manager(entry)
    data = UNASDataCoordinator(hass, entry)
    service = UNASServiceCoordinator(hass, entry, ssh)
    deployer = Deployer(hass, entry, ssh, service)
    reconciler = Reconciler(hass, entry, data, service)
    entry.runtime_data = UNASRuntime(
        data, service, deployer, reconciler, ssh, connection_data(entry)
    )

    await data.async_start()

    async_register_main_device(hass, entry, data.data)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    reconciler.async_start()
    DeviceSync(hass, entry, data).async_start()

    # SSH never blocks setup, the MQTT entities work without it
    entry.async_on_unload(service.async_add_listener(deployer.async_check))
    entry.async_create_background_task(hass, service.async_refresh(), f"{DOMAIN} ssh status")

    await _async_publish_interval(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    async_schedule_scan(hass, entry)

    return True


async def _async_entry_updated(hass: HomeAssistant, entry: UNASConfigEntry) -> None:
    if connection_data(entry) != entry.runtime_data.connection:
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return
    await _async_publish_interval(hass, entry)


async def _async_publish_interval(hass: HomeAssistant, entry: UNASConfigEntry) -> None:
    # the NAS daemons pick it up live, nothing to reload or redeploy
    interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    await mqtt.async_publish(
        hass,
        f"{get_mqtt_topics(entry.entry_id)['control']}/monitor_interval",
        str(interval),
        qos=0,
        retain=True,
    )


async def _clear_retained_mqtt_topics(hass: HomeAssistant, entry_id: str) -> None:
    if mqtt.DOMAIN not in hass.data:
        return
    mqtt_root = get_mqtt_root(entry_id)
    collected: list[str] = []

    @callback
    def on_message(msg):
        collected.append(msg.topic)

    try:
        unsub = await mqtt.async_subscribe(hass, f"{mqtt_root}/#", on_message, qos=0)
        await asyncio.sleep(0.5)
        unsub()
        for topic in collected:
            await mqtt.async_publish(hass, topic, "", qos=0, retain=True)
        if collected:
            _LOGGER.info("Cleared %d retained MQTT topics under %s", len(collected), mqtt_root)
    except Exception:
        _LOGGER.debug("Failed to clear retained MQTT topics for %s", mqtt_root)


async def async_unload_entry(hass: HomeAssistant, entry: UNASConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.ssh.disconnect()
    return unload_ok


async def async_remove_config_entry_device(
        hass: HomeAssistant, entry: UNASConfigEntry, device: dr.DeviceEntry
) -> bool:
    if entry.state is not ConfigEntryState.LOADED:
        # whatever still exists comes back with the next setup
        return (DOMAIN, entry.entry_id) not in device.identifiers
    # only what the UNAS no longer reports, the rest would just come back
    return entry.runtime_data.reconciler.async_remove_device(device)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    manager = _build_ssh_manager(entry)
    try:
        for command in UNAS_UNINSTALL_COMMANDS:
            await manager.execute_command(command)
        _LOGGER.info("Removed monitor and fan control from UNAS at %s", entry.data[CONF_HOST])
    except Exception as err:
        _LOGGER.error("Failed to clean up UNAS (non-critical): %s", err)
    finally:
        await manager.disconnect()

    # daemons first, their offline messages would be retained again
    await _clear_retained_mqtt_topics(hass, entry.entry_id)
