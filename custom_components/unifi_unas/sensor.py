from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import UNASConfigEntry
from .const import (
    BACKUP_STATUS_IDLE,
    BACKUP_STATUS_RUNNING,
    CONF_DEVICE_MODEL,
    DOMAIN,
    format_remote_type,
    format_schedule,
)
from .coordinator import UNASDataCoordinator, UNASServiceCoordinator
from .devices import backup_device, drive_device, main_device, share_device
from .fan_state import FanState, pwm_to_percent

_LOGGER = logging.getLogger(__name__)

# sensor definitions: (mqtt_key, name, unit, device_class, state_class, icon)
UNAS_SENSORS = [
    (
        "unas_cpu_temp",
        "CPU Temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    (
        "unas_cpu_usage",
        "CPU Usage",
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:chip",
    ),
    (
        "unas_fan_speed",
        "Fan Speed (PWM)",
        None,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:fan",
    ),
    (
        "unas_fan_speed_percent",
        "Fan Speed (Percent)",
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:fan",
    ),
    (
        "unas_memory_usage",
        "Memory Usage",
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:memory",
    ),
    (
        "unas_memory_used",
        "Memory Used",
        UnitOfInformation.MEBIBYTES,
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    (
        "unas_memory_total",
        "Memory Total",
        UnitOfInformation.MEBIBYTES,
        SensorDeviceClass.DATA_SIZE,
        None,
        None,
    ),
    (
        "unas_disk_read",
        "Disk Read",
        "MB/s",
        SensorDeviceClass.DATA_RATE,
        SensorStateClass.MEASUREMENT,
        "mdi:download",
    ),
    (
        "unas_disk_write",
        "Disk Write",
        "MB/s",
        SensorDeviceClass.DATA_RATE,
        SensorStateClass.MEASUREMENT,
        "mdi:upload",
    ),
    (
        "unas_smb_connections",
        "SMB Connections",
        None,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:server-network",
    ),
    (
        "unas_nfs_mounts",
        "NFS Mounts",
        None,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:folder-network",
    ),
    (
        "unas_uptime",
        "Uptime",
        None,
        SensorDeviceClass.TIMESTAMP,
        None,
        None,
    ),
    ("unas_os_version", "UniFi OS Version", None, None, None, "mdi:information"),
    ("unas_drive_version", "UniFi Drive Version", None, None, None, "mdi:information"),
    ("unas_protect_version", "UniFi Protect Version", None, None, None, "mdi:information"),
]

# storage pool sensor patterns (will be created dynamically for each pool)
STORAGE_POOL_SENSORS = [
    (
        "usage",
        "Usage",
        PERCENTAGE,
        None,
        SensorStateClass.MEASUREMENT,
        "mdi:harddisk",
    ),
    (
        "size",
        "Size",
        "GB",
        SensorDeviceClass.DATA_SIZE,
        None,
        None,
    ),
    (
        "used",
        "Used",
        "GB",
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    (
        "available",
        "Available",
        "GB",
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    (
        "status",
        "Status",
        None,
        None,
        None,
        "mdi:check-circle",
    ),
    (
        "raid_level",
        "RAID Level",
        None,
        None,
        None,
        "mdi:database",
    ),
]

SHARE_SENSORS = [
    ("usage", "Usage", "GB", SensorDeviceClass.DATA_SIZE, SensorStateClass.MEASUREMENT, "mdi:folder"),
    ("quota", "Quota", "GB", None, None, "mdi:folder-lock"),
    ("pool", "Storage Pool", None, None, None, "mdi:database"),
    ("member_count", "Members", None, None, None, "mdi:account-multiple"),
    ("snapshot_enabled", "Snapshots", None, None, None, "mdi:camera"),
    ("encryption", "Encryption", None, None, None, "mdi:lock"),
]
DRIVE_SENSORS = [
    (
        "temperature",
        "Temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    ("model", "Model", None, None, None, "mdi:harddisk"),
    ("serial", "Serial Number", None, None, None, "mdi:identifier"),
    ("rpm", "RPM", "rpm", None, None, "mdi:speedometer"),
    ("firmware", "Firmware", None, None, None, "mdi:information"),
    ("status", "Status", None, None, None, "mdi:check-circle"),
    ("total_size", "Total Size", "TB", SensorDeviceClass.DATA_SIZE, None, None),
    (
        "power_on_hours",
        "Power-On Hours",
        UnitOfTime.HOURS,
        SensorDeviceClass.DURATION,
        SensorStateClass.TOTAL_INCREASING,
        None,
    ),
    ("bad_sectors", "Bad Sectors", None, None, None, "mdi:alert-circle"),
]

NVME_SENSORS = [
    (
        "temperature",
        "Temperature",
        UnitOfTemperature.CELSIUS,
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    ("model", "Model", None, None, None, "mdi:expansion-card"),
    ("serial", "Serial Number", None, None, None, "mdi:identifier"),
    ("firmware", "Firmware", None, None, None, "mdi:information"),
    ("status", "Status", None, None, None, "mdi:check-circle"),
    ("total_size", "Total Size", "TB", SensorDeviceClass.DATA_SIZE, None, None),
    (
        "power_on_hours",
        "Power-On Hours",
        UnitOfTime.HOURS,
        SensorDeviceClass.DURATION,
        SensorStateClass.TOTAL_INCREASING,
        None,
    ),
    ("percentage_used", "Percentage Used", PERCENTAGE, None, SensorStateClass.MEASUREMENT, "mdi:chart-line"),
    ("available_spare", "Available Spare", PERCENTAGE, None, SensorStateClass.MEASUREMENT, "mdi:database"),
    ("media_errors", "Media Errors", None, None, None, "mdi:alert-circle"),
    ("unsafe_shutdowns", "Unsafe Shutdowns", None, None, SensorStateClass.TOTAL_INCREASING, "mdi:power"),
]


async def async_setup_entry(
        hass: HomeAssistant,
        entry: UNASConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data
    service = entry.runtime_data.service
    device_model = entry.data[CONF_DEVICE_MODEL]

    if device_model.startswith("UNVR"):
        excluded = {"unas_smb_connections", "unas_nfs_mounts", "unas_drive_version"}
    else:
        excluded = {"unas_protect_version"}

    entities = []

    for mqtt_key, name, unit, device_class, state_class, icon in UNAS_SENSORS:
        if mqtt_key not in excluded:
            entities.append(
                UNASSensor(coordinator, mqtt_key, name, unit, device_class, state_class, icon))

    entities.append(UNASFanCurveVisualizationSensor(coordinator))

    async_add_entities(entities)

    @callback
    def add_bays(bays: list[str]) -> None:
        async_add_entities(
            UNASDriveSensor(coordinator, f"unas_hdd_{bay}_{suffix}", name, bay, *rest)
            for bay in bays
            for suffix, name, *rest in DRIVE_SENSORS
        )

    @callback
    def add_nvmes(slots: list[str]) -> None:
        async_add_entities(
            UNASNVMeSensor(coordinator, f"unas_nvme_{slot}_{suffix}", name, slot, *rest)
            for slot in slots
            for suffix, name, *rest in NVME_SENSORS
        )

    @callback
    def add_pools(pools: list[str]) -> None:
        async_add_entities(
            UNASSensor(
                coordinator, f"unas_pool{pool}_{suffix}", f"Storage Pool {pool} {name}", *rest,
                item=("pools", pool),
            )
            for pool in pools
            for suffix, name, *rest in STORAGE_POOL_SENSORS
        )

    @callback
    def add_shares(shares: list[str]) -> None:
        async_add_entities(
            UNASShareSensor(coordinator, share, suffix, name, *rest)
            for share in shares
            for suffix, name, *rest in SHARE_SENSORS
        )

    @callback
    def add_backups(task_ids: list[str]) -> None:
        entities = []
        for task_id in task_ids:
            task = service.find_backup_task(task_id)
            entities.extend(sensor(service, task) for sensor in BACKUP_SENSORS)
        async_add_entities(entities)

    reconciler = entry.runtime_data.reconciler
    reconciler.register("bays", add_bays)
    reconciler.register("nvme", add_nvmes)
    reconciler.register("pools", add_pools)
    reconciler.register("shares", add_shares)
    reconciler.register("backups", add_backups)


class UNASSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataCoordinator,
            mqtt_key: str,
            name: str,
            unit: str | None,
            device_class: SensorDeviceClass | None,
            state_class: SensorStateClass | None,
            icon: str | None,
            item: tuple[str, str] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_key = mqtt_key
        self._item = item
        self._last_uptime_raw = None
        self._boot_timestamp = None
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{mqtt_key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        if device_class == SensorDeviceClass.TEMPERATURE:
            self._attr_suggested_display_precision = 0
        if device_class == SensorDeviceClass.DATA_SIZE:
            # storage pools
            if unit == "GB":
                self._attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
            # RAM
            elif unit == "MiB":
                self._attr_suggested_unit_of_measurement = UnitOfInformation.GIBIBYTES

        self._attr_device_info = main_device(coordinator.entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data
        self._attr_native_value = mqtt_data.get(self._mqtt_key)

        attr_key = f"{self._mqtt_key}_attributes"
        if attr_key in mqtt_data:
            self._attr_extra_state_attributes = {"clients": mqtt_data[attr_key]}

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        client = self.coordinator.mqtt_client
        if not client.is_available() or (self._item and not client.listed(*self._item)):
            return False
        return self._mqtt_key in self.coordinator.data and client.is_fresh(self._mqtt_key)

    @property
    def native_value(self):
        raw = self.coordinator.data.get(self._mqtt_key)
        if raw is not None and self._mqtt_key == "unas_uptime":
            try:
                uptime_secs = int(float(raw))
                if self._boot_timestamp is None or uptime_secs < (self._last_uptime_raw or 0):
                    self._boot_timestamp = dt_util.utcnow() - timedelta(seconds=uptime_secs)
                self._last_uptime_raw = uptime_secs
                return self._boot_timestamp
            except (ValueError, TypeError):
                return None
        return raw


class UNASFanCurveVisualizationSensor(CoordinatorEntity, SensorEntity):
    _unrecorded_attributes = frozenset({"curve_points", "curve_formula"})

    def __init__(self, coordinator: UNASDataCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Fan Curve"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fan_curve_viz"
        self._attr_icon = "mdi:chart-line"
        self._attr_device_info = main_device(coordinator.entry)

    @property
    def _curve(self) -> dict[str, int]:
        return FanState.from_data(self.coordinator.data).curve

    @property
    def available(self) -> bool:
        return all(key in self._curve for key in ("min_temp", "max_temp", "min_fan", "max_fan"))

    @property
    def native_value(self) -> str | None:
        if not self.available:
            return None
        curve = self._curve
        return (
            f"{curve['min_temp']}-{curve['max_temp']}°C → "
            f"{pwm_to_percent(curve['min_fan'])}-{pwm_to_percent(curve['max_fan'])}%"
        )

    @property
    def extra_state_attributes(self) -> dict | None:
        if not self.available:
            return None
        curve = self._curve
        min_temp, max_temp, min_fan, max_fan = (curve[k] for k in ("min_temp", "max_temp", "min_fan", "max_fan"))
        min_fan_pct, max_fan_pct = pwm_to_percent(min_fan), pwm_to_percent(max_fan)
        return {
            "min_temp": min_temp,
            "max_temp": max_temp,
            "min_fan_pwm": min_fan,
            "max_fan_pwm": max_fan,
            "min_fan_percent": min_fan_pct,
            "max_fan_percent": max_fan_pct,
            "curve_points": [
                [temp, pwm_to_percent(curve_pwm(temp, min_temp, max_temp, min_fan, max_fan))] for temp in range(30, 61)
            ],
            "curve_formula": f"Linear: {min_temp}°C→{min_fan_pct}%, {max_temp}°C→{max_fan_pct}%",
        }


def curve_pwm(temp: float, min_temp: float, max_temp: float, min_fan: float, max_fan: float) -> float:
    if temp < min_temp:
        return min_fan
    if temp > max_temp or max_temp <= min_temp:
        return max_fan
    return min_fan + (temp - min_temp) * (max_fan - min_fan) / (max_temp - min_temp)


class UNASNVMeSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataCoordinator,
            mqtt_key: str,
            name: str,
            nvme_slot: str,
            unit: str | None,
            device_class: SensorDeviceClass | None,
            state_class: SensorStateClass | None,
            icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_key = mqtt_key
        self._nvme_slot = nvme_slot
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{mqtt_key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        if device_class == SensorDeviceClass.TEMPERATURE:
            self._attr_suggested_display_precision = 0

        self._attr_device_info = drive_device(
            coordinator.hass, coordinator.entry, "nvme", nvme_slot, coordinator.mqtt_client.get_data()
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data
        self._attr_native_value = mqtt_data.get(self._mqtt_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        client = self.coordinator.mqtt_client
        if not client.is_available() or not client.listed("nvme", self._nvme_slot):
            return False
        mqtt_data = self.coordinator.data
        return self._mqtt_key in mqtt_data and client.is_fresh(self._mqtt_key)

    @property
    def native_value(self):
        mqtt_data = self.coordinator.data
        return mqtt_data.get(self._mqtt_key)


class UNASDriveSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataCoordinator,
            mqtt_key: str,
            name: str,
            bay_num: str,
            unit: str | None,
            device_class: SensorDeviceClass | None,
            state_class: SensorStateClass | None,
            icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_key = mqtt_key
        self._bay_num = bay_num
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{mqtt_key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        if device_class == SensorDeviceClass.TEMPERATURE:
            self._attr_suggested_display_precision = 0

        self._attr_device_info = drive_device(
            coordinator.hass, coordinator.entry, "bays", bay_num, coordinator.mqtt_client.get_data()
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data
        self._attr_native_value = mqtt_data.get(self._mqtt_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        client = self.coordinator.mqtt_client
        if not client.is_available() or not client.listed("bays", self._bay_num):
            return False
        mqtt_data = self.coordinator.data
        return self._mqtt_key in mqtt_data and client.is_fresh(self._mqtt_key)

    @property
    def native_value(self):
        mqtt_data = self.coordinator.data
        return mqtt_data.get(self._mqtt_key)


class UNASShareSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataCoordinator,
            share_name: str,
            sensor_suffix: str,
            name: str,
            unit: str | None,
            device_class: SensorDeviceClass | None,
            state_class: SensorStateClass | None,
            icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._share_name = share_name
        self._sensor_suffix = sensor_suffix
        self._mqtt_key = f"unas_share_{share_name}_{sensor_suffix}"
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{self._mqtt_key}"
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        if icon:
            self._attr_icon = icon
        if device_class == SensorDeviceClass.DATA_SIZE and unit == "GB":
            self._attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES

        self._attr_device_info = share_device(coordinator.hass, coordinator.entry, share_name)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._attr_native_value = self._resolve_value()
        mqtt_data = self.coordinator.data
        if self._sensor_suffix == "pool":
            pool_num = mqtt_data.get(self._mqtt_key)
            if pool_num is not None:
                self._attr_extra_state_attributes = {
                    "raid_level": mqtt_data.get(f"unas_pool{pool_num}_raid_level"),
                    "protection": mqtt_data.get(f"unas_pool{pool_num}_protection"),
                }
        elif self._sensor_suffix == "member_count":
            attr_key = f"{self._mqtt_key}_attributes"
            if attr_key in mqtt_data:
                self._attr_extra_state_attributes = {"members": mqtt_data[attr_key]}
        self.async_write_ha_state()

    def _unlimited(self) -> bool:
        value = self.coordinator.data.get(self._mqtt_key)
        return self._sensor_suffix == "quota" and value is not None and int(value) == -1

    def _resolve_value(self):
        if self._unlimited():
            return "Unlimited"
        return self.coordinator.data.get(self._mqtt_key)

    @property
    def native_unit_of_measurement(self) -> str | None:
        # HA reads the unit before the value, an "Unlimited" state must not carry one
        if self._unlimited():
            return None
        return self._attr_native_unit_of_measurement

    @property
    def available(self) -> bool:
        client = self.coordinator.mqtt_client
        if not client.is_available() or not client.listed("shares", self._share_name):
            return False
        return self._mqtt_key in self.coordinator.data and client.is_fresh(self._mqtt_key)

    @property
    def native_value(self):
        return self._resolve_value()


class UNASBackupStatusSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Status"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_status"
        self._attr_icon = "mdi:cloud-sync"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        last_run = task.get("lastTaskRun", {})
        status = last_run.get("status")
        if status == "pending" or status == BACKUP_STATUS_RUNNING:
            return BACKUP_STATUS_RUNNING
        elif status == BACKUP_STATUS_IDLE:
            return BACKUP_STATUS_IDLE
        elif status:
            return status
        return BACKUP_STATUS_IDLE

    @property
    def extra_state_attributes(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return {}
        attrs = {
            "task_id": self._task_id,
            "task_name": self._task_name,
        }
        if "sourceDirs" in task:
            attrs["source_dirs"] = task["sourceDirs"]
        if "destinationDir" in task:
            attrs["destination_dir"] = task["destinationDir"]
        if "remote" in task:
            attrs["remote_type"] = task["remote"].get("type")
        last_run = task.get("lastTaskRun", {})
        if last_run.get("trigger"):
            attrs["last_trigger"] = last_run["trigger"]
        if last_run.get("errorCodes"):
            attrs["error_codes"] = last_run["errorCodes"]
        return attrs


class UNASBackupLastRunSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Last run"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_last_run"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-check"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        last_run = task.get("lastTaskRun", {})
        started_at = last_run.get("startedAt")
        if started_at:
            try:
                return datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        return None


class UNASBackupNextRunSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Next run"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_next_run"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-outline"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not self.coordinator.ready or not task:
            return False
        schedule = task.get("schedule", {})
        return schedule.get("enable", False)

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        next_backup = task.get("nextBackup")
        if next_backup:
            try:
                return datetime.fromisoformat(next_backup.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None
        return None


class UNASBackupDurationSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Last run duration"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_duration"
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
        self._attr_icon = "mdi:timer-outline"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)
        self._cached_duration = None

    @property
    def available(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not self.coordinator.ready or not task:
            return False
        last_run = task.get("lastTaskRun", {})
        if last_run.get("startedAt") and last_run.get("finishedAt"):
            return True
        return self._cached_duration is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        last_run = task.get("lastTaskRun", {})
        started = last_run.get("startedAt")
        finished = last_run.get("finishedAt")
        if started and finished:
            try:
                start_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(finished.replace("Z", "+00:00"))
                self._cached_duration = int((end_dt - start_dt).total_seconds())
            except (ValueError, AttributeError):
                pass
        return self._cached_duration


class UNASBackupDestinationSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Destination"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_destination"
        self._attr_icon = "mdi:cloud-upload"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        remote = task.get("remote", {})
        remote_type = format_remote_type(remote.get("type"))
        account = remote.get("oauth2Account")
        if account:
            return f"{remote_type} ({account})"
        return remote_type


class UNASBackupSourceSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Source"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_source"
        self._attr_icon = "mdi:folder-multiple"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        source_dirs = task.get("sourceDirs", [])
        if not source_dirs:
            return None
        joined = ", ".join(source_dirs)
        if len(joined) <= 255:
            return joined
        return f"{len(source_dirs)} directories"

    @property
    def extra_state_attributes(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        source_dirs = task.get("sourceDirs", [])
        if source_dirs and len(", ".join(source_dirs)) > 255:
            return {"source_dirs": source_dirs}


class UNASBackupScheduleSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Schedule"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_schedule"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        return format_schedule(task.get("schedule"))


class UNASBackupNameSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASServiceCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Task name"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_name"
        self._attr_icon = "mdi:tag"
        self._attr_device_info = backup_device(coordinator.hass, coordinator.entry, task)

    @property
    def available(self):
        return self.coordinator.ready and self.coordinator.find_backup_task(self._task_id) is not None

    @property
    def native_value(self):
        task = self.coordinator.find_backup_task(self._task_id)
        if not task:
            return None
        return task.get("name")


BACKUP_SENSORS = (
    UNASBackupStatusSensor,
    UNASBackupLastRunSensor,
    UNASBackupNextRunSensor,
    UNASBackupDurationSensor,
    UNASBackupDestinationSensor,
    UNASBackupSourceSensor,
    UNASBackupScheduleSensor,
    UNASBackupNameSensor,
)
