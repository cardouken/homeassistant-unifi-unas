from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfInformation,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import UNASDataUpdateCoordinator
from .const import (
    BACKUP_STATUS_IDLE,
    BACKUP_STATUS_RUNNING,
    CONF_DEVICE_MODEL,
    DOMAIN,
    format_remote_type,
    format_schedule,
    get_device_info,
)

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
        UnitOfInformation.MEGABYTES,
        SensorDeviceClass.DATA_SIZE,
        SensorStateClass.MEASUREMENT,
        None,
    ),
    (
        "unas_memory_total",
        "Memory Total",
        UnitOfInformation.MEGABYTES,
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
        UnitOfTime.SECONDS,
        SensorDeviceClass.DURATION,
        SensorStateClass.TOTAL_INCREASING,
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
]

SHARE_SENSORS = [
    ("usage", "Usage", "GB", SensorDeviceClass.DATA_SIZE, SensorStateClass.MEASUREMENT, "mdi:folder"),
    ("quota", "Quota", "GB", None, None, "mdi:folder-lock"),
    ("status", "Status", None, None, None, "mdi:check-circle"),
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
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: UNASDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    device_model = entry.data[CONF_DEVICE_MODEL]

    if device_model == "UNVR":
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

    coordinator.sensor_add_entities = async_add_entities

    async def discover_drives():
        await _discover_and_add_drive_sensors(coordinator, async_add_entities)
        await _discover_and_add_nvme_sensors(coordinator, async_add_entities)
        await _discover_and_add_pool_sensors(coordinator, async_add_entities)
        await _discover_and_add_share_sensors(coordinator, async_add_entities)

        if coordinator.discovered_bays or coordinator.discovered_nvmes or coordinator.discovered_pools:
            return

        # if drives not found immediately, poll with shorter intervals (12 × 5s = 60s max)
        for _ in range(12):
            await asyncio.sleep(5)
            await _discover_and_add_drive_sensors(coordinator, async_add_entities)
            await _discover_and_add_nvme_sensors(coordinator, async_add_entities)
            await _discover_and_add_pool_sensors(coordinator, async_add_entities)
            await _discover_and_add_share_sensors(coordinator, async_add_entities)

            if coordinator.discovered_bays or coordinator.discovered_nvmes or coordinator.discovered_pools:
                break

    hass.async_create_task(discover_drives())


async def _discover_and_add_drive_sensors(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    from homeassistant.components import mqtt
    
    mqtt_data = coordinator.mqtt_client.get_data()
    detected_bays = {key.split("_")[2] for key in mqtt_data.keys() if
                     key.startswith("unas_hdd_") and "_temperature" in key}

    missing_bays = coordinator.discovered_bays - detected_bays
    if missing_bays:
        _LOGGER.info("Drives no longer detected in bays: %s", sorted(missing_bays))
        coordinator.discovered_bays -= missing_bays
        
        entity_reg = er.async_get(coordinator.hass)
        device_reg = dr.async_get(coordinator.hass)
        
        mqtt_root = coordinator.mqtt_client.mqtt_root
        
        for bay in missing_bays:
            for sensor_suffix, _, _, _, _, _ in DRIVE_SENSORS:
                unique_id = f"{coordinator.entry.entry_id}_unas_hdd_{bay}_{sensor_suffix}"
                if entity_id := entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
                    entity_reg.async_remove(entity_id)
                    _LOGGER.debug("Removed entity %s", entity_id)
                
                await mqtt.async_publish(
                    coordinator.hass,
                    f"{mqtt_root}/hdd/{bay}/{sensor_suffix}",
                    "",
                    qos=0,
                    retain=True,
                )
            
            device_id = (DOMAIN, f"{coordinator.entry.entry_id}_hdd_{bay}")
            if device := device_reg.async_get_device(identifiers={device_id}):
                device_reg.async_remove_device(device.id)
                _LOGGER.info("Removed device for HDD bay %s", bay)

    new_bays = detected_bays - coordinator.discovered_bays
    if not new_bays:
        return

    _LOGGER.debug("Discovered new drive bays: %s", sorted(new_bays))

    entities = []
    for bay_num in sorted(new_bays):
        for sensor_suffix, name, unit, device_class, state_class, icon in DRIVE_SENSORS:
            mqtt_key = f"unas_hdd_{bay_num}_{sensor_suffix}"
            entities.append(
                UNASDriveSensor(coordinator, mqtt_key, name, bay_num, unit, device_class, state_class, icon))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_bays.update(new_bays)
        _LOGGER.info("Added %d sensors for %d new drives", len(entities), len(new_bays))


async def _discover_and_add_nvme_sensors(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    from homeassistant.components import mqtt
    
    mqtt_data = coordinator.mqtt_client.get_data()
    detected_nvmes = {key.split("_")[2] for key in mqtt_data.keys() if
                      key.startswith("unas_nvme_") and "_temperature" in key}

    missing_nvmes = coordinator.discovered_nvmes - detected_nvmes
    if missing_nvmes:
        _LOGGER.info("NVMe drives no longer detected in slots: %s", sorted(missing_nvmes))
        coordinator.discovered_nvmes -= missing_nvmes
        
        entity_reg = er.async_get(coordinator.hass)
        device_reg = dr.async_get(coordinator.hass)
        
        mqtt_root = coordinator.mqtt_client.mqtt_root
        
        for slot in missing_nvmes:
            for sensor_suffix, _, _, _, _, _ in NVME_SENSORS:
                unique_id = f"{coordinator.entry.entry_id}_unas_nvme_{slot}_{sensor_suffix}"
                if entity_id := entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
                    entity_reg.async_remove(entity_id)
                    _LOGGER.debug("Removed entity %s", entity_id)
                
                await mqtt.async_publish(
                    coordinator.hass,
                    f"{mqtt_root}/nvme/{slot}/{sensor_suffix}",
                    "",
                    qos=0,
                    retain=True,
                )
            
            device_id = (DOMAIN, f"{coordinator.entry.entry_id}_nvme_{slot}")
            if device := device_reg.async_get_device(identifiers={device_id}):
                device_reg.async_remove_device(device.id)
                _LOGGER.info("Removed device for NVMe slot %s", slot)

    new_nvmes = detected_nvmes - coordinator.discovered_nvmes
    if not new_nvmes:
        return

    _LOGGER.debug("Discovered new NVMe drives: %s", sorted(new_nvmes))

    entities = []
    for nvme_slot in sorted(new_nvmes):
        for sensor_suffix, name, unit, device_class, state_class, icon in NVME_SENSORS:
            mqtt_key = f"unas_nvme_{nvme_slot}_{sensor_suffix}"
            entities.append(
                UNASNVMeSensor(coordinator, mqtt_key, name, nvme_slot, unit, device_class, state_class, icon))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_nvmes.update(new_nvmes)
        _LOGGER.info("Added %d sensors for %d new NVMe drives", len(entities), len(new_nvmes))


async def _discover_and_add_pool_sensors(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er
    from homeassistant.components import mqtt
    
    mqtt_data = coordinator.mqtt_client.get_data()
    pool_pattern = re.compile(r"^unas_pool(\d+)_usage$")
    detected_pools = set()
    for key in mqtt_data.keys():
        if match := pool_pattern.match(key):
            detected_pools.add(match.group(1))

    missing_pools = coordinator.discovered_pools - detected_pools
    if missing_pools:
        _LOGGER.info("Storage pools no longer detected: %s", sorted(missing_pools))
        coordinator.discovered_pools -= missing_pools
        
        entity_reg = er.async_get(coordinator.hass)
        mqtt_root = coordinator.mqtt_client.mqtt_root
        
        for pool_num in missing_pools:
            for sensor_suffix, _, _, _, _, _ in STORAGE_POOL_SENSORS:
                unique_id = f"{coordinator.entry.entry_id}_unas_pool{pool_num}_{sensor_suffix}"
                if entity_id := entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
                    entity_reg.async_remove(entity_id)
                    _LOGGER.debug("Removed entity %s", entity_id)
                
                await mqtt.async_publish(
                    coordinator.hass,
                    f"{mqtt_root}/pool/{pool_num}/{sensor_suffix}",
                    "",
                    qos=0,
                    retain=True,
                )
        
        _LOGGER.info("Removed entities for %d pools", len(missing_pools))

    new_pools = detected_pools - coordinator.discovered_pools
    if not new_pools:
        return

    _LOGGER.debug("Discovered new storage pools: %s", sorted(new_pools))

    entities = []
    for pool_num in sorted(new_pools):
        for sensor_suffix, name, unit, device_class, state_class, icon in STORAGE_POOL_SENSORS:
            mqtt_key = f"unas_pool{pool_num}_{sensor_suffix}"
            full_name = f"Storage Pool {pool_num} {name}"
            if sensor_suffix == "status":
                entities.append(
                    UNASPoolStatusSensor(coordinator, mqtt_key, full_name, pool_num, icon))
            else:
                entities.append(
                    UNASSensor(coordinator, mqtt_key, full_name, unit, device_class, state_class, icon))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_pools.update(new_pools)
        _LOGGER.info("Added %d sensors for %d new pools", len(entities), len(new_pools))


class UNASSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataUpdateCoordinator,
            mqtt_key: str,
            name: str,
            unit: str | None,
            device_class: SensorDeviceClass | None,
            state_class: SensorStateClass | None,
            icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_key = mqtt_key
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
        if device_class == SensorDeviceClass.DURATION:
            # uptime
            self._attr_suggested_unit_of_measurement = UnitOfTime.DAYS
        if device_class == SensorDeviceClass.DATA_SIZE:
            # storage pools
            if unit == "GB":
                self._attr_suggested_unit_of_measurement = UnitOfInformation.TERABYTES
            # RAM
            elif unit == "MB":
                self._attr_suggested_unit_of_measurement = UnitOfInformation.GIGABYTES

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        self._attr_native_value = mqtt_data.get(self._mqtt_key)

        attr_key = f"{self._mqtt_key}_attributes"
        if attr_key in mqtt_data:
            self._attr_extra_state_attributes = {"clients": mqtt_data[attr_key]}

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not self.coordinator.mqtt_client.is_available():
            return False
        return self._mqtt_key in self.coordinator.data.get("mqtt_data", {})

    @property
    def native_value(self):
        return self.coordinator.data.get("mqtt_data", {}).get(self._mqtt_key)


class UNASFanCurveVisualizationSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_has_entity_name = True
        self._attr_name = "Fan Curve"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fan_curve_viz"
        self._attr_icon = "mdi:chart-line"

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})

        min_temp = mqtt_data.get("fan_curve_min_temp", 43)
        max_temp = mqtt_data.get("fan_curve_max_temp", 47)
        min_fan = mqtt_data.get("fan_curve_min_fan", 204)
        max_fan = mqtt_data.get("fan_curve_max_fan", 255)

        # convert PWM to percentage for display
        min_fan_pct = round((min_fan * 100) / 255)
        max_fan_pct = round((max_fan * 100) / 255)

        # state: summary string
        self._attr_native_value = (
            f"{min_temp}-{max_temp}°C → {min_fan_pct}-{max_fan_pct}%"
        )

        # generate curve points for charting (temp, fan%)
        curve_points = self._generate_curve_points(min_temp, max_temp, min_fan, max_fan)

        # Set attributes for charting
        self._attr_extra_state_attributes = {
            "min_temp": min_temp,
            "max_temp": max_temp,
            "min_fan_pwm": min_fan,
            "max_fan_pwm": max_fan,
            "min_fan_percent": min_fan_pct,
            "max_fan_percent": max_fan_pct,
            "curve_points": curve_points,
            "curve_formula": f"Linear: {min_temp}°C→{min_fan_pct}%, {max_temp}°C→{max_fan_pct}%",
        }

    def _generate_curve_points(
            self, min_temp: float, max_temp: float, min_fan: float, max_fan: float
    ) -> list:
        points = []

        # Generate points from 30°C to 60°C
        for temp in range(30, 61):
            if temp < min_temp:
                fan_pwm = min_fan
            elif temp > max_temp:
                fan_pwm = max_fan
            else:
                fan_pwm = min_fan + (temp - min_temp) * (max_fan - min_fan) / (
                        max_temp - min_temp
                )

            fan_percent = round((fan_pwm * 100) / 255)
            points.append([temp, fan_percent])

        return points

    @property
    def available(self) -> bool:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        return (
                "fan_curve_min_temp" in mqtt_data
                and "fan_curve_max_temp" in mqtt_data
                and "fan_curve_min_fan" in mqtt_data
                and "fan_curve_max_fan" in mqtt_data
        )


class UNASNVMeSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataUpdateCoordinator,
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

        mqtt_data = coordinator.mqtt_client.get_data()
        model = mqtt_data.get(f"unas_nvme_{nvme_slot}_model", "Unknown")
        serial = mqtt_data.get(f"unas_nvme_{nvme_slot}_serial", "")

        device_name, _ = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_nvme_{nvme_slot}")},
            name=f"{device_name} NVMe {nvme_slot}",
            manufacturer=model.split()[0] if model != "Unknown" else "Unknown",
            model=model,
            serial_number=serial,
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        self._attr_native_value = mqtt_data.get(self._mqtt_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not self.coordinator.mqtt_client.is_available():
            return False
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        return self._mqtt_key in mqtt_data

    @property
    def native_value(self):
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        return mqtt_data.get(self._mqtt_key)


class UNASDriveSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataUpdateCoordinator,
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

        mqtt_data = coordinator.mqtt_client.get_data()
        model = mqtt_data.get(f"unas_hdd_{bay_num}_model", "Unknown")
        serial = mqtt_data.get(f"unas_hdd_{bay_num}_serial", "")

        device_name, _ = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_hdd_{bay_num}")},
            name=f"{device_name} HDD {bay_num}",
            manufacturer=model.split()[0] if model != "Unknown" else "Unknown",
            model=model,
            serial_number=serial,
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        self._attr_native_value = mqtt_data.get(self._mqtt_key)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not self.coordinator.mqtt_client.is_available():
            return False
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        return self._mqtt_key in mqtt_data

    @property
    def native_value(self):
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        return mqtt_data.get(self._mqtt_key)


class UNASPoolStatusSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataUpdateCoordinator,
            mqtt_key: str,
            name: str,
            pool_num: str,
            icon: str | None,
    ) -> None:
        super().__init__(coordinator)
        self._mqtt_key = mqtt_key
        self._pool_num = pool_num
        self._attr_has_entity_name = True
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{mqtt_key}"
        if icon:
            self._attr_icon = icon

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        self._attr_native_value = mqtt_data.get(self._mqtt_key)
        self._attr_extra_state_attributes = {
            "raid_level": mqtt_data.get(f"unas_pool{self._pool_num}_raid_level"),
            "protection": mqtt_data.get(f"unas_pool{self._pool_num}_protection"),
        }
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not self.coordinator.mqtt_client.is_available():
            return False
        return self._mqtt_key in self.coordinator.data.get("mqtt_data", {})

    @property
    def native_value(self):
        return self.coordinator.data.get("mqtt_data", {}).get(self._mqtt_key)


class UNASShareSensor(CoordinatorEntity, SensorEntity):
    def __init__(
            self,
            coordinator: UNASDataUpdateCoordinator,
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

        device_name, _ = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_share_{share_name}")},
            name=f"{device_name} Share: {share_name}",
            entry_type=DeviceEntryType.SERVICE,
            via_device=(DOMAIN, coordinator.entry.entry_id),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        value = mqtt_data.get(self._mqtt_key)

        if self._sensor_suffix == "quota":
            if value is not None and int(value) == -1:
                self._attr_native_value = "Unlimited"
                self._attr_native_unit_of_measurement = None
            else:
                self._attr_native_value = value
                self._attr_native_unit_of_measurement = "GB"
        else:
            self._attr_native_value = value

        if self._sensor_suffix == "usage":
            prefix = f"unas_share_{self._share_name}_"
            pool_num = mqtt_data.get(f"{prefix}pool")
            self._attr_extra_state_attributes = {
                "storage_pool": f"Storage Pool {pool_num}" if pool_num else None,
                "member_count": mqtt_data.get(f"{prefix}member_count"),
                "snapshot_enabled": mqtt_data.get(f"{prefix}snapshot_enabled"),
                "encryption": mqtt_data.get(f"{prefix}encryption"),
                "backup_enabled": mqtt_data.get(f"{prefix}backup_enabled"),
            }
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not self.coordinator.mqtt_client.is_available():
            return False
        return self._mqtt_key in self.coordinator.data.get("mqtt_data", {})

    @property
    def native_value(self):
        mqtt_data = self.coordinator.data.get("mqtt_data", {})
        value = mqtt_data.get(self._mqtt_key)
        if self._sensor_suffix == "quota":
            if value is not None and int(value) == -1:
                return "Unlimited"
            return value
        return value


async def _discover_and_add_share_sensors(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er, device_registry as dr
    from homeassistant.components import mqtt

    mqtt_data = coordinator.mqtt_client.get_data()
    share_pattern = re.compile(r"^unas_share_(.+)_usage$")
    detected_shares = set()
    for key in mqtt_data.keys():
        if match := share_pattern.match(key):
            detected_shares.add(match.group(1))

    missing_shares = coordinator.discovered_shares - detected_shares
    if missing_shares:
        _LOGGER.info("Shares no longer detected: %s", sorted(missing_shares))
        coordinator.discovered_shares -= missing_shares

        entity_reg = er.async_get(coordinator.hass)
        device_reg = dr.async_get(coordinator.hass)
        mqtt_root = coordinator.mqtt_client.mqtt_root

        for share_name in missing_shares:
            for sensor_suffix, _, _, _, _, _ in SHARE_SENSORS:
                unique_id = f"{coordinator.entry.entry_id}_unas_share_{share_name}_{sensor_suffix}"
                if entity_id := entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
                    entity_reg.async_remove(entity_id)

            # clear all retained share topics (including metadata)
            for metric in ("usage", "quota", "status", "pool", "member_count",
                           "snapshot_enabled", "encryption", "backup_enabled"):
                await mqtt.async_publish(
                    coordinator.hass,
                    f"{mqtt_root}/share/{share_name}/{metric}",
                    "",
                    qos=0,
                    retain=True,
                )

            device_id = (DOMAIN, f"{coordinator.entry.entry_id}_share_{share_name}")
            if device := device_reg.async_get_device(identifiers={device_id}):
                device_reg.async_remove_device(device.id)
                _LOGGER.info("Removed service device for share %s", share_name)

    new_shares = detected_shares - coordinator.discovered_shares
    if not new_shares:
        return

    _LOGGER.debug("Discovered new shares: %s", sorted(new_shares))

    entities = []
    for share_name in sorted(new_shares):
        for sensor_suffix, name, unit, device_class, state_class, icon in SHARE_SENSORS:
            entities.append(
                UNASShareSensor(
                    coordinator, share_name, sensor_suffix, name,
                    unit, device_class, state_class, icon,
                ))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_shares.update(new_shares)
        _LOGGER.info("Added %d sensors for %d new shares", len(entities), len(new_shares))


async def _discover_and_add_backup_sensors(
        coordinator: UNASDataUpdateCoordinator,
        async_add_entities: AddEntitiesCallback,
) -> None:
    from homeassistant.helpers import entity_registry as er, device_registry as dr

    backup_tasks = coordinator.data.get("backup_tasks", [])
    task_ids = {task["id"] for task in backup_tasks}

    entity_reg = er.async_get(coordinator.hass)
    device_reg = dr.async_get(coordinator.hass)
    entry_id = coordinator.entry.entry_id

    known_suffixes = ("_status", "_last_run", "_next_run", "_duration", "_destination", "_source", "_schedule", "_name")

    missing_tasks = coordinator.discovered_backup_task_sensors - task_ids
    if missing_tasks:
        _LOGGER.info("Backup tasks no longer detected: %s", sorted(missing_tasks))
        coordinator.discovered_backup_task_sensors -= missing_tasks

        for task_id in missing_tasks:
            for suffix in known_suffixes:
                unique_id = f"{entry_id}_backup_{task_id}{suffix}"
                if entity_id := entity_reg.async_get_entity_id("sensor", DOMAIN, unique_id):
                    entity_reg.async_remove(entity_id)

            device_id = (DOMAIN, f"{entry_id}_backup_{task_id}")
            if device := device_reg.async_get_device(identifiers={device_id}):
                device_reg.async_remove_device(device.id)
                _LOGGER.info("Removed service device for backup task %s", task_id)

    # clean up orphaned entities from previous sessions
    prefix = f"{entry_id}_backup_"
    for entity in er.async_entries_for_config_entry(entity_reg, entry_id):
        if entity.domain != "sensor" or not entity.unique_id.startswith(prefix):
            continue
        remainder = entity.unique_id[len(prefix):]
        for suffix in known_suffixes:
            if remainder.endswith(suffix):
                task_id = remainder[:-len(suffix)]
                if task_id not in task_ids:
                    entity_reg.async_remove(entity.entity_id)
                    _LOGGER.info("Removed orphaned backup sensor %s", entity.entity_id)
                break

    new_tasks = task_ids - coordinator.discovered_backup_task_sensors
    if not new_tasks:
        return

    _LOGGER.debug("Discovered new backup tasks: %s", sorted(new_tasks))

    entities = []
    for task in backup_tasks:
        if task["id"] in new_tasks:
            entities.append(UNASBackupStatusSensor(coordinator, task))
            entities.append(UNASBackupLastRunSensor(coordinator, task))
            entities.append(UNASBackupNextRunSensor(coordinator, task))
            entities.append(UNASBackupDurationSensor(coordinator, task))
            entities.append(UNASBackupDestinationSensor(coordinator, task))
            entities.append(UNASBackupSourceSensor(coordinator, task))
            entities.append(UNASBackupScheduleSensor(coordinator, task))
            entities.append(UNASBackupNameSensor(coordinator, task))

    if entities:
        async_add_entities(entities)
        coordinator.discovered_backup_task_sensors.update(new_tasks)
        _LOGGER.info("Added %d sensors for %d new backup tasks", len(entities), len(new_tasks))


def _find_backup_task(coordinator, task_id):
    for task in coordinator.data.get("backup_tasks", []):
        if task["id"] == task_id:
            return task
    return None


def _get_backup_device_info(coordinator: UNASDataUpdateCoordinator, task: dict) -> DeviceInfo:
    remote = task.get("remote", {})
    return DeviceInfo(
        identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_backup_{task['id']}")},
        name=f"UNAS Backup {task['name']}",
        manufacturer=format_remote_type(remote.get("type")),
        model=remote.get("oauth2Account") or task.get("destinationDir", ""),
        entry_type=DeviceEntryType.SERVICE,
        via_device=(DOMAIN, coordinator.entry.entry_id),
    )


class UNASBackupStatusSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._task_name = task["name"]
        self._attr_has_entity_name = True
        self._attr_name = "Status"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_status"
        self._attr_icon = "mdi:cloud-sync"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
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
        task = _find_backup_task(self.coordinator, self._task_id)
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
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Last run"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_last_run"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-check"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
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
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Next run"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_next_run"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-outline"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return False
        schedule = task.get("schedule", {})
        return schedule.get("enable", False)

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
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
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Last run duration"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_duration"
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_native_unit_of_measurement = UnitOfTime.SECONDS
        self._attr_icon = "mdi:timer-outline"
        self._attr_device_info = _get_backup_device_info(coordinator, task)
        self._cached_duration = None

    @property
    def available(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return False
        last_run = task.get("lastTaskRun", {})
        if last_run.get("startedAt") and last_run.get("finishedAt"):
            return True
        return self._cached_duration is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
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
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Destination"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_destination"
        self._attr_icon = "mdi:cloud-upload"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return None
        remote = task.get("remote", {})
        remote_type = format_remote_type(remote.get("type"))
        account = remote.get("oauth2Account")
        if account:
            return f"{remote_type} ({account})"
        return remote_type


class UNASBackupSourceSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Source"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_source"
        self._attr_icon = "mdi:folder-multiple"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return None
        source_dirs = task.get("sourceDirs", [])
        return ", ".join(source_dirs) if source_dirs else None


class UNASBackupScheduleSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Schedule"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_schedule"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return None
        return format_schedule(task.get("schedule"))


class UNASBackupNameSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, task: dict) -> None:
        super().__init__(coordinator)
        self._task_id = task["id"]
        self._attr_has_entity_name = True
        self._attr_name = "Task name"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_backup_{self._task_id}_name"
        self._attr_icon = "mdi:tag"
        self._attr_device_info = _get_backup_device_info(coordinator, task)

    @property
    def available(self):
        return _find_backup_task(self.coordinator, self._task_id) is not None

    @property
    def native_value(self):
        task = _find_backup_task(self.coordinator, self._task_id)
        if not task:
            return None
        return task.get("name")
