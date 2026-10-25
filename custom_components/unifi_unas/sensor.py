from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import UNASConfigEntry
from .const import (
    BACKUP_STATUS_IDLE,
    BACKUP_STATUS_RUNNING,
    CONF_DEVICE_MODEL,
    format_remote_type,
    format_schedule,
)
from .coordinator import UNASDataCoordinator, UNASServiceCoordinator
from .devices import backup_device, drive_device, share_device
from .entity import UNASEntity
from .fan_state import FanState, pwm_to_percent

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

DIAGNOSTIC = EntityCategory.DIAGNOSTIC
TEMPERATURE = {
    "native_unit_of_measurement": UnitOfTemperature.CELSIUS,
    "device_class": SensorDeviceClass.TEMPERATURE,
    "state_class": SensorStateClass.MEASUREMENT,
    "suggested_display_precision": 0,
}
GB_IN_TB = {
    "native_unit_of_measurement": "GB",
    "device_class": SensorDeviceClass.DATA_SIZE,
    "suggested_unit_of_measurement": UnitOfInformation.TERABYTES,
}
POWER_ON_HOURS = {
    "native_unit_of_measurement": UnitOfTime.HOURS,
    "device_class": SensorDeviceClass.DURATION,
    "state_class": SensorStateClass.TOTAL_INCREASING,
}


def _sensor(key: str, translation_key: str | None = None, **kwargs: Any) -> SensorEntityDescription:
    return SensorEntityDescription(key=key, translation_key=translation_key or key, **kwargs)


# key is the MQTT key without the unas_ prefix
SYSTEM_SENSORS = (
    _sensor("cpu_temp", **TEMPERATURE),
    _sensor("cpu_usage", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    _sensor("fan_speed", state_class=SensorStateClass.MEASUREMENT),
    _sensor("fan_speed_percent", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    _sensor("memory_usage", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    _sensor(
        "memory_used", native_unit_of_measurement=UnitOfInformation.MEBIBYTES, device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT, suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
    ),
    _sensor(
        "memory_total", native_unit_of_measurement=UnitOfInformation.MEBIBYTES,
        device_class=SensorDeviceClass.DATA_SIZE, suggested_unit_of_measurement=UnitOfInformation.GIBIBYTES,
    ),
    _sensor("disk_read", native_unit_of_measurement="MB/s", device_class=SensorDeviceClass.DATA_RATE,
            state_class=SensorStateClass.MEASUREMENT),
    _sensor("disk_write", native_unit_of_measurement="MB/s", device_class=SensorDeviceClass.DATA_RATE,
            state_class=SensorStateClass.MEASUREMENT),
    _sensor("smb_connections", state_class=SensorStateClass.MEASUREMENT),
    _sensor("nfs_mounts", state_class=SensorStateClass.MEASUREMENT),
    _sensor("uptime", device_class=SensorDeviceClass.TIMESTAMP, entity_category=DIAGNOSTIC),
    _sensor("os_version", entity_category=DIAGNOSTIC),
    _sensor("drive_version", entity_category=DIAGNOSTIC),
    _sensor("protect_version", entity_category=DIAGNOSTIC),
)
UNVR_ONLY = {"protect_version"}
UNAS_ONLY = {"smb_connections", "nfs_mounts", "drive_version"}

POOL_SENSORS = (
    _sensor("usage", "pool_usage", native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT),
    _sensor("size", "pool_size", **GB_IN_TB),
    _sensor("used", "pool_used", state_class=SensorStateClass.MEASUREMENT, **GB_IN_TB),
    _sensor("available", "pool_available", state_class=SensorStateClass.MEASUREMENT, **GB_IN_TB),
    _sensor("status", "pool_status"),
    _sensor("raid_level", "pool_raid_level"),
)

SHARE_SENSORS = (
    _sensor("usage", "share_usage", state_class=SensorStateClass.MEASUREMENT, **GB_IN_TB),
    _sensor("quota", "share_quota", native_unit_of_measurement="GB"),
    _sensor("pool", "share_pool"),
    _sensor("member_count", "share_member_count"),
    _sensor("snapshot_enabled", "share_snapshot_enabled"),
    _sensor("encryption", "share_encryption", entity_registry_enabled_default=False),
)

DRIVE_SENSORS = (
    _sensor("temperature", "drive_temperature", **TEMPERATURE),
    _sensor("model", "drive_model", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("serial", "drive_serial", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("rpm", "drive_rpm", native_unit_of_measurement="rpm"),
    _sensor("firmware", "drive_firmware", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("status", "drive_status"),
    _sensor("total_size", "drive_total_size", native_unit_of_measurement="TB", device_class=SensorDeviceClass.DATA_SIZE),
    _sensor("power_on_hours", "drive_power_on_hours", entity_category=DIAGNOSTIC, **POWER_ON_HOURS),
    _sensor("bad_sectors", "drive_bad_sectors"),
)

NVME_SENSORS = (
    _sensor("temperature", "drive_temperature", **TEMPERATURE),
    _sensor("model", "nvme_model", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("serial", "drive_serial", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("firmware", "drive_firmware", entity_category=DIAGNOSTIC, entity_registry_enabled_default=False),
    _sensor("status", "drive_status"),
    _sensor("total_size", "drive_total_size", native_unit_of_measurement="TB", device_class=SensorDeviceClass.DATA_SIZE),
    _sensor("power_on_hours", "drive_power_on_hours", entity_category=DIAGNOSTIC, **POWER_ON_HOURS),
    _sensor("percentage_used", "nvme_percentage_used", native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT),
    _sensor("available_spare", "nvme_available_spare", native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT),
    _sensor("media_errors", "nvme_media_errors"),
    _sensor("unsafe_shutdowns", "nvme_unsafe_shutdowns", state_class=SensorStateClass.TOTAL_INCREASING),
)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _backup_status(task: dict) -> str:
    status = (task.get("lastTaskRun") or {}).get("status")
    if status in ("pending", BACKUP_STATUS_RUNNING):
        return BACKUP_STATUS_RUNNING
    return status or BACKUP_STATUS_IDLE


def _backup_status_attrs(task: dict) -> dict:
    attrs = {"task_id": task["id"], "task_name": task.get("name")}
    if "sourceDirs" in task:
        attrs["source_dirs"] = task["sourceDirs"]
    if "destinationDir" in task:
        attrs["destination_dir"] = task["destinationDir"]
    if "remote" in task:
        attrs["remote_type"] = task["remote"].get("type")
    last_run = task.get("lastTaskRun") or {}
    if last_run.get("trigger"):
        attrs["last_trigger"] = last_run["trigger"]
    if last_run.get("errorCodes"):
        attrs["error_codes"] = last_run["errorCodes"]
    return attrs


def _backup_duration(task: dict) -> int | None:
    last_run = task.get("lastTaskRun") or {}
    started, finished = _timestamp(last_run.get("startedAt")), _timestamp(last_run.get("finishedAt"))
    return int((finished - started).total_seconds()) if started and finished else None


def _backup_destination(task: dict) -> str:
    remote = task.get("remote") or {}
    remote_type = format_remote_type(remote.get("type"))
    account = remote.get("oauth2Account")
    return f"{remote_type} ({account})" if account else remote_type


def _backup_source(task: dict) -> str | None:
    source_dirs = task.get("sourceDirs") or []
    if not source_dirs:
        return None
    joined = ", ".join(source_dirs)
    return joined if len(joined) <= 255 else f"{len(source_dirs)} directories"


def _backup_source_attrs(task: dict) -> dict | None:
    source_dirs = task.get("sourceDirs") or []
    return {"source_dirs": source_dirs} if source_dirs and len(", ".join(source_dirs)) > 255 else None


@dataclass(frozen=True, kw_only=True)
class UNASBackupSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], Any]
    attrs_fn: Callable[[dict], dict | None] = lambda task: None
    available_fn: Callable[[dict], bool] = lambda task: True
    # a running task has no duration yet, show the previous one meanwhile
    keep_last: bool = False


BACKUP_SENSORS = (
    UNASBackupSensorDescription(key="status", translation_key="backup_status", value_fn=_backup_status,
                                attrs_fn=_backup_status_attrs),
    UNASBackupSensorDescription(
        key="last_run", translation_key="backup_last_run", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda task: _timestamp((task.get("lastTaskRun") or {}).get("startedAt")),
    ),
    UNASBackupSensorDescription(
        key="next_run", translation_key="backup_next_run", device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda task: _timestamp(task.get("nextBackup")),
        available_fn=lambda task: bool((task.get("schedule") or {}).get("enable", False)),
    ),
    UNASBackupSensorDescription(
        key="duration", translation_key="backup_duration", device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS, value_fn=_backup_duration, keep_last=True,
    ),
    UNASBackupSensorDescription(key="destination", translation_key="backup_destination",
                                value_fn=_backup_destination),
    UNASBackupSensorDescription(key="source", translation_key="backup_source", value_fn=_backup_source,
                                attrs_fn=_backup_source_attrs),
    UNASBackupSensorDescription(key="schedule", translation_key="backup_schedule",
                                value_fn=lambda task: format_schedule(task.get("schedule"))),
    UNASBackupSensorDescription(key="name", translation_key="backup_name", value_fn=lambda task: task.get("name")),
)


async def async_setup_entry(
        hass: HomeAssistant,
        entry: UNASConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.data
    service = entry.runtime_data.service
    excluded = UNAS_ONLY if entry.data[CONF_DEVICE_MODEL].startswith("UNVR") else UNVR_ONLY

    async_add_entities([
        *(UNASSensor(coordinator, f"unas_{d.key}", d) for d in SYSTEM_SENSORS if d.key not in excluded),
        UNASFanCurveSensor(coordinator),
    ])

    @callback
    def add_bays(bays: list[str]) -> None:
        async_add_entities(
            UNASSensor(coordinator, f"unas_hdd_{bay}_{d.key}", d, item=("bays", bay),
                       device=drive_device(hass, entry, "bays", bay, coordinator.data))
            for bay in bays for d in DRIVE_SENSORS
        )

    @callback
    def add_nvmes(slots: list[str]) -> None:
        async_add_entities(
            UNASSensor(coordinator, f"unas_nvme_{slot}_{d.key}", d, item=("nvme", slot),
                       device=drive_device(hass, entry, "nvme", slot, coordinator.data))
            for slot in slots for d in NVME_SENSORS
        )

    @callback
    def add_pools(pools: list[str]) -> None:
        async_add_entities(
            UNASSensor(coordinator, f"unas_pool{pool}_{d.key}", d, item=("pools", pool),
                       placeholders={"pool": pool})
            for pool in pools for d in POOL_SENSORS
        )

    @callback
    def add_shares(shares: list[str]) -> None:
        async_add_entities(
            UNASShareSensor(coordinator, share, d) for share in shares for d in SHARE_SENSORS
        )

    @callback
    def add_backups(task_ids: list[str]) -> None:
        async_add_entities(
            UNASBackupSensor(service, service.find_backup_task(task_id), d)
            for task_id in task_ids for d in BACKUP_SENSORS
        )

    reconciler = entry.runtime_data.reconciler
    reconciler.register("bays", add_bays)
    reconciler.register("nvme", add_nvmes)
    reconciler.register("pools", add_pools)
    reconciler.register("shares", add_shares)
    reconciler.register("backups", add_backups)


class UNASSensor(UNASEntity, SensorEntity):
    coordinator: UNASDataCoordinator

    def __init__(
            self,
            coordinator: UNASDataCoordinator,
            mqtt_key: str,
            description: SensorEntityDescription,
            item: tuple[str, str] | None = None,
            **kwargs: Any,
    ) -> None:
        super().__init__(coordinator, mqtt_key, description, **kwargs)
        self._mqtt_key = mqtt_key
        self._item = item
        self._boot_time: datetime | None = None
        self._last_uptime: int | None = None

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
            return self._boot_timestamp(raw)
        return raw

    def _boot_timestamp(self, raw) -> datetime | None:
        # recomputed only after a reboot, otherwise the timestamp jitters every cycle
        try:
            uptime = int(float(raw))
        except (ValueError, TypeError):
            return None
        if self._boot_time is None or uptime < (self._last_uptime or 0):
            self._boot_time = dt_util.utcnow() - timedelta(seconds=uptime)
        self._last_uptime = uptime
        return self._boot_time

    @property
    def extra_state_attributes(self) -> dict | None:
        clients = self.coordinator.data.get(f"{self._mqtt_key}_attributes")
        return {"clients": clients} if clients is not None else None


class UNASShareSensor(UNASSensor):
    def __init__(self, coordinator: UNASDataCoordinator, share: str, description: SensorEntityDescription) -> None:
        super().__init__(
            coordinator, f"unas_share_{share}_{description.key}", description, item=("shares", share),
            device=share_device(coordinator.hass, coordinator.entry, share),
        )

    def _unlimited(self) -> bool:
        value = self.coordinator.data.get(self._mqtt_key)
        return self.entity_description.key == "quota" and value is not None and int(value) == -1

    @property
    def native_value(self):
        if self._unlimited():
            return "Unlimited"
        return self.coordinator.data.get(self._mqtt_key)

    @property
    def native_unit_of_measurement(self) -> str | None:
        # HA reads the unit before the value, an "Unlimited" state must not carry one
        if self._unlimited():
            return None
        return super().native_unit_of_measurement

    @property
    def extra_state_attributes(self) -> dict | None:
        data = self.coordinator.data
        if self.entity_description.key == "pool" and (pool := data.get(self._mqtt_key)) is not None:
            return {
                "raid_level": data.get(f"unas_pool{pool}_raid_level"),
                "protection": data.get(f"unas_pool{pool}_protection"),
            }
        if self.entity_description.key == "member_count":
            members = data.get(f"{self._mqtt_key}_attributes")
            return {"members": members} if members is not None else None
        return None


class UNASFanCurveSensor(UNASEntity, SensorEntity):
    coordinator: UNASDataCoordinator
    _unrecorded_attributes = frozenset({"curve_points", "curve_formula"})

    def __init__(self, coordinator: UNASDataCoordinator) -> None:
        super().__init__(
            coordinator, "fan_curve_viz",
            SensorEntityDescription(key="fan_curve", translation_key="fan_curve", entity_category=DIAGNOSTIC),
        )

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


class UNASBackupSensor(UNASEntity, SensorEntity):
    coordinator: UNASServiceCoordinator
    entity_description: UNASBackupSensorDescription

    def __init__(self, coordinator: UNASServiceCoordinator, task: dict, description: UNASBackupSensorDescription) -> None:
        super().__init__(
            coordinator, f"backup_{task['id']}_{description.key}", description,
            device=backup_device(coordinator.hass, coordinator.entry, task),
        )
        self._task_id = task["id"]
        self._last_value = None

    @property
    def _task(self) -> dict | None:
        return self.coordinator.find_backup_task(self._task_id)

    @property
    def available(self) -> bool:
        task = self._task
        if not self.coordinator.ready or task is None:
            return False
        if self.entity_description.keep_last and self.entity_description.value_fn(task) is None:
            return self._last_value is not None
        return self.entity_description.available_fn(task)

    @property
    def native_value(self):
        task = self._task
        if task is None:
            return None
        value = self.entity_description.value_fn(task)
        if self.entity_description.keep_last:
            if value is None:
                return self._last_value
            self._last_value = value
        return value

    @property
    def extra_state_attributes(self) -> dict | None:
        task = self._task
        return self.entity_description.attrs_fn(task) if task else None
