from __future__ import annotations

import asyncio
import logging
import re

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
from homeassistant.helpers.device_registry import DeviceInfo

from . import UNASDataUpdateCoordinator
from .const import CONF_DEVICE_MODEL, DOMAIN, get_device_info

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

        if coordinator.discovered_bays or coordinator.discovered_nvmes or coordinator.discovered_pools:
            return

        # if drives not found immediately, poll with shorter intervals (12 × 5s = 60s max)
        for _ in range(12):
            await asyncio.sleep(5)
            await _discover_and_add_drive_sensors(coordinator, async_add_entities)
            await _discover_and_add_nvme_sensors(coordinator, async_add_entities)
            await _discover_and_add_pool_sensors(coordinator, async_add_entities)

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
