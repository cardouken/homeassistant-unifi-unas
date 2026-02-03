from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from packaging.version import Version, InvalidVersion

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components import mqtt
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_MQTT_HOST,
    CONF_MQTT_USER,
    CONF_MQTT_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_MODEL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_DEVICE_MODEL,
    get_mqtt_topics,
)
from .ssh_manager import SSHManager
from .mqtt_client import UNASMQTTClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
]

LAST_CLEANUP_VERSION_KEY = "last_cleanup_version"
LAST_DEPLOY_VERSION_KEY = "last_deploy_version"
PERFORM_MQTT_CLEANUP = True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.version == 1:
        new_data = {**entry.data}
        if CONF_DEVICE_MODEL not in new_data:
            new_data[CONF_DEVICE_MODEL] = DEFAULT_DEVICE_MODEL
            _LOGGER.info("Migrated config entry to version 2, added device model: %s", DEFAULT_DEVICE_MODEL)

        hass.config_entries.async_update_entry(entry, data=new_data, version=2)
        return True

    return True


def _version_at_least(stored: str | None, target: str) -> bool:
    if stored is None:
        return False
    try:
        return Version(stored.replace("-dev", "")) >= Version(target.replace("-dev", ""))
    except InvalidVersion:
        return stored == target


async def _cleanup_old_mqtt_configs_on_upgrade(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    if not PERFORM_MQTT_CLEANUP:
        return

    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    current_version = str(integration.version)
    last_cleanup_version = entry.data.get(LAST_CLEANUP_VERSION_KEY)

    if _version_at_least(last_cleanup_version, current_version):
        return

    _LOGGER.info(
        "Integration upgraded from %s to %s - running MQTT config cleanup",
        last_cleanup_version or "unknown",
        current_version,
    )

    topics_to_clear = [
        "unas_uptime",
        "unas_os_version",
        "unas_drive_version",
        "unas_cpu_usage",
        "unas_memory_used",
        "unas_memory_total",
        "unas_memory_usage",
        "unas_cpu",
        "unas_fan_speed",
        "unas_fan_speed_percent",
    ]

    for i in range(1, 6):
        topics_to_clear.extend(
            [
                f"unas_pool{i}_usage",
                f"unas_pool{i}_size",
                f"unas_pool{i}_used",
                f"unas_pool{i}_available",
            ]
        )

    for bay in range(1, 8):
        topics_to_clear.extend(
            [
                f"unas_hdd_{bay}_temperature",
                f"unas_hdd_{bay}_model",
                f"unas_hdd_{bay}_serial",
                f"unas_hdd_{bay}_rpm",
                f"unas_hdd_{bay}_firmware",
                f"unas_hdd_{bay}_status",
                f"unas_hdd_{bay}_total_size",
                f"unas_hdd_{bay}_power_hours",
                f"unas_hdd_{bay}_bad_sectors",
            ]
        )

    cleared_count = 0
    for topic in topics_to_clear:
        try:
            await mqtt.async_publish(
                hass,
                f"homeassistant/sensor/{topic}/config",
                "",
                qos=0,
                retain=True,
            )
            cleared_count += 1
        except Exception as err:
            _LOGGER.debug("Failed to clear MQTT config for %s: %s", topic, err)

    _LOGGER.info("Cleared %d old MQTT auto-discovery configs", cleared_count)

    new_data = dict(entry.data)
    new_data[LAST_CLEANUP_VERSION_KEY] = current_version
    hass.config_entries.async_update_entry(entry, data=new_data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    manager = SSHManager(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data.get(CONF_PASSWORD),
        mqtt_host=entry.data.get(CONF_MQTT_HOST),
        mqtt_user=entry.data.get(CONF_MQTT_USER),
        mqtt_password=entry.data.get(CONF_MQTT_PASSWORD),
    )

    await manager.connect()
    _LOGGER.info("SSH connection established to %s", entry.data[CONF_HOST])

    from homeassistant.loader import async_get_integration

    integration = await async_get_integration(hass, DOMAIN)
    current_version = str(integration.version)
    last_deploy_version = entry.data.get(LAST_DEPLOY_VERSION_KEY)
    scripts_installed = await manager.scripts_installed()
    is_dev_version = '-dev' in current_version
    device_model = entry.data[CONF_DEVICE_MODEL]

    if last_deploy_version != current_version or not scripts_installed or is_dev_version:
        mqtt_root = get_mqtt_topics(entry.entry_id)["root"]
        await manager.deploy_scripts(device_model, mqtt_root)
        new_data = dict(entry.data)
        new_data[LAST_DEPLOY_VERSION_KEY] = current_version
        hass.config_entries.async_update_entry(entry, data=new_data)

    mqtt_client_instance = UNASMQTTClient(hass, entry.entry_id)
    coordinator = UNASDataUpdateCoordinator(hass, manager, mqtt_client_instance, entry)
    mqtt_client_instance._coordinator = coordinator

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "ssh_manager": manager,
        "mqtt_client": mqtt_client_instance,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await mqtt_client_instance.async_subscribe()
    await _cleanup_old_mqtt_configs_on_upgrade(hass, entry)

    topics = get_mqtt_topics(entry.entry_id)
    scan_interval = entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    await mqtt.async_publish(
        hass,
        f"{topics['control']}/monitor_interval",
        str(scan_interval),
        qos=0,
        retain=True,
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await data["mqtt_client"].async_unsubscribe()

        manager = data["ssh_manager"]
        try:
            await manager.execute_command("systemctl stop unas_monitor || true")
            await manager.execute_command("systemctl stop fan_control || true")
            await manager.execute_command("systemctl disable unas_monitor || true")
            await manager.execute_command("systemctl disable fan_control || true")
            await manager.execute_command("rm -f /etc/systemd/system/unas_monitor.service")
            await manager.execute_command("rm -f /etc/systemd/system/fan_control.service")
            await manager.execute_command("rm -f /root/unas_monitor.py")
            await manager.execute_command("rm -f /root/fan_control.sh")
            await manager.execute_command("rm -f /tmp/fan_control_last_pwm")
            await manager.execute_command("rm -f /tmp/fan_control_state")
            await manager.execute_command("rm -f /tmp/unas_hdd_temp")
            await manager.execute_command("rm -f /tmp/unas_monitor_interval")
            await manager.execute_command("rm -f /var/log/fan_control.log /var/log/fan_control.log.[1-9]")
            await manager.execute_command("systemctl daemon-reload")
            await manager.execute_command("apt remove mosquitto-clients -y")
            await manager.execute_command("pip3 uninstall paho-mqtt -y")
            await manager.execute_command("apt remove python3-pip -y")
            await manager.execute_command("echo 2 > /sys/class/hwmon/hwmon0/pwm1_enable || true")
            await manager.execute_command("echo 2 > /sys/class/hwmon/hwmon0/pwm2_enable || true")
        except Exception as err:
            _LOGGER.error("Failed to clean up UNAS (non-critical): %s", err)

        await manager.disconnect()

    return unload_ok


class UNASDataUpdateCoordinator(DataUpdateCoordinator):
    def __init__(
        self,
        hass: HomeAssistant,
        ssh_manager: SSHManager,
        mqtt_client: UNASMQTTClient,
        entry: ConfigEntry,
    ) -> None:
        self.ssh_manager = ssh_manager
        self.mqtt_client = mqtt_client
        self.entry = entry
        self.discovered_bays: set[str] = set()
        self.discovered_nvmes: set[str] = set()
        self.discovered_pools: set[str] = set()
        self.discovered_backup_task_sensors: set[str] = set()
        self.discovered_backup_task_buttons: set[str] = set()
        self.discovered_backup_task_switches: set[str] = set()
        self.sensor_add_entities = None
        self.button_add_entities = None
        self.switch_add_entities = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        )

    async def _async_update_data(self):
        if mqtt.DOMAIN not in self.hass.data:
            _LOGGER.error("MQTT integration removed - UNAS Pro requires MQTT")
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "mqtt_missing",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="mqtt_missing",
            )
            raise UpdateFailed("MQTT integration is required but not found")

        data = {
            "scripts_installed": False,
            "ssh_connected": False,
            "monitor_running": False,
            "fan_control_running": False,
            "mqtt_data": self.mqtt_client.get_data(),
        }

        try:
            scripts_installed = await self.ssh_manager.scripts_installed()

            if not scripts_installed:
                _LOGGER.warning("Scripts missing, reinstalling...")
                device_model = self.entry.data[CONF_DEVICE_MODEL]
                mqtt_root = get_mqtt_topics(self.entry.entry_id)["root"]
                await self.ssh_manager.deploy_scripts(device_model, mqtt_root)

            monitor_running = await self.ssh_manager.service_running("unas_monitor")
            fan_control_running = await self.ssh_manager.service_running("fan_control")

            data.update({
                "scripts_installed": scripts_installed,
                "ssh_connected": True,
                "monitor_running": monitor_running,
                "fan_control_running": fan_control_running,
            })

            try:
                result = await self.ssh_manager.execute_backup_api("GET", "/api/v1/remote-backup/tasks")
                if result.get("data"):
                    data["backup_tasks"] = result["data"]
                else:
                    data["backup_tasks"] = []
            except Exception as err:
                _LOGGER.debug("Could not fetch backup tasks: %s", err)
                data["backup_tasks"] = []

            if self.sensor_add_entities is not None:
                from .sensor import (
                    _discover_and_add_drive_sensors,
                    _discover_and_add_nvme_sensors,
                    _discover_and_add_pool_sensors,
                    _discover_and_add_backup_sensors,
                )
                await _discover_and_add_drive_sensors(self, self.sensor_add_entities)
                await _discover_and_add_nvme_sensors(self, self.sensor_add_entities)
                await _discover_and_add_pool_sensors(self, self.sensor_add_entities)
                await _discover_and_add_backup_sensors(self, self.sensor_add_entities)

            if self.button_add_entities is not None:
                from .button import _discover_and_add_backup_buttons
                await _discover_and_add_backup_buttons(self, self.button_add_entities)

            if self.switch_add_entities is not None:
                from .switch import _discover_and_add_backup_switches
                await _discover_and_add_backup_switches(self, self.switch_add_entities)

        except Exception as err:
            _LOGGER.warning("SSH connection temporarily unavailable: %s", err)

        return data

    async def async_reinstall_scripts(self) -> None:
        device_model = self.entry.data[CONF_DEVICE_MODEL]
        mqtt_root = get_mqtt_topics(self.entry.entry_id)["root"]
        await self.ssh_manager.deploy_scripts(device_model, mqtt_root)
        await self.async_request_refresh()
