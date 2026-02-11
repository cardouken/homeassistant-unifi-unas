from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.components import mqtt

from . import UNASDataUpdateCoordinator
from .const import CONF_DEVICE_MODEL, DOMAIN, get_device_info, get_mqtt_topics

DEFAULT_FAN_SPEED_50_PCT = 128

_LOGGER = logging.getLogger(__name__)

MODE_CUSTOM_CURVE = "Custom Curve"
MODE_SET_SPEED = "Set Speed"
MODE_TARGET_TEMP = "Target Temp"

TEMP_METRIC_MAX = "Max (Hottest)"
TEMP_METRIC_AVG = "Average"

RESPONSE_RELAXED = "Relaxed"
RESPONSE_BALANCED = "Balanced"
RESPONSE_AGGRESSIVE = "Aggressive"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: UNASDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([
        UNASFanModeSelect(coordinator, hass),
        UNASTempMetricSelect(coordinator, hass),
        UNASResponseSpeedSelect(coordinator, hass),
    ])


class UNASFanModeSelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    def __init__(self, coordinator: UNASDataUpdateCoordinator, hass: HomeAssistant) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self._topics = get_mqtt_topics(coordinator.entry.entry_id)
        self._attr_has_entity_name = True
        self._attr_name = "Fan Mode"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_fan_mode"
        self._attr_icon = "mdi:fan-auto"
        self._current_option = None
        self._last_pwm = None
        self._unsubscribe = None

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._mode_managed = f"{device_name} Managed"
        self._attr_options = [self._mode_managed, MODE_CUSTOM_CURVE, MODE_TARGET_TEMP, MODE_SET_SPEED]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None and last_state.state in self._attr_options:
            self._current_option = last_state.state
            self._last_pwm = last_state.attributes.get("last_pwm")
        else:
            self._current_option = self._mode_managed

        @callback
        def message_received(msg):
            payload = msg.payload

            if payload == "unas_managed":
                self._current_option = self._mode_managed
            elif payload == "auto":
                self._current_option = MODE_CUSTOM_CURVE
            elif payload == "target_temp":
                self._current_option = MODE_TARGET_TEMP
            elif payload.isdigit():
                self._current_option = MODE_SET_SPEED
                try:
                    self._last_pwm = int(payload)
                except (ValueError, TypeError):
                    pass
            else:
                self._current_option = self._mode_managed

            self.async_write_ha_state()

        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/mode", message_received, qos=0
        )

    async def _publish_mode(self, mode: str) -> None:
        try:
            await mqtt.async_publish(self.hass, f"{self._topics['control']}/fan/mode", mode, qos=0, retain=True)
        except Exception as err:
            _LOGGER.error("Failed to publish fan mode: %s", err)

    async def _ensure_service_running(self) -> None:
        try:
            if not await self.coordinator.ssh_manager.service_running("fan_control"):
                await self.coordinator.ssh_manager.execute_command("systemctl start fan_control")
        except Exception as err:
            _LOGGER.error("Failed to start fan_control service: %s", err)

    @property
    def available(self) -> bool:
        mqtt_available = self.coordinator.mqtt_client.is_available()
        service_running = self.coordinator.data.get("fan_control_running", False)
        has_state = self._current_option is not None
        return mqtt_available and service_running and has_state

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from fan mode: %s", err)
        await super().async_will_remove_from_hass()

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def extra_state_attributes(self) -> dict:
        return {"last_pwm": self._last_pwm} if self._last_pwm is not None else {}

    async def async_select_option(self, option: str) -> None:
        await self._ensure_service_running()

        if option == self._mode_managed:
            await self._publish_mode("unas_managed")
            try:
                await self.coordinator.ssh_manager.kick_native_fan_control()
            except Exception as err:
                _LOGGER.warning("Could not kick native fan control (non-critical): %s", err)
        elif option == MODE_CUSTOM_CURVE:
            await self._publish_mode("auto")
        elif option == MODE_TARGET_TEMP:
            await self._publish_mode("target_temp")
        elif option == MODE_SET_SPEED:
            mqtt_data = self.coordinator.mqtt_client.get_data()
            current_speed = mqtt_data.get("unas_fan_speed", DEFAULT_FAN_SPEED_50_PCT)
            self._last_pwm = current_speed
            await self._publish_mode(str(current_speed))

        self._current_option = option
        self.async_write_ha_state()


class UNASTempMetricSelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Select entity for choosing temperature metric (max or average) for Target Temp mode."""

    def __init__(self, coordinator: UNASDataUpdateCoordinator, hass: HomeAssistant) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self._topics = get_mqtt_topics(coordinator.entry.entry_id)
        self._attr_has_entity_name = True
        self._attr_name = "Temperature Metric"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_temp_metric"
        self._attr_icon = "mdi:thermometer-lines"
        self._current_option = None
        self._unsubscribe = None
        self._current_mode = None
        self._unsubscribe_mode = None

        self._attr_options = [TEMP_METRIC_MAX, TEMP_METRIC_AVG]

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in self._attr_options:
                self._current_option = last_state.state
            self._current_mode = last_state.attributes.get("current_mode")
        if self._current_option is None:
            self._current_option = TEMP_METRIC_MAX

        @callback
        def message_received(msg):
            payload = msg.payload

            if payload == "avg":
                self._current_option = TEMP_METRIC_AVG
            else:
                self._current_option = TEMP_METRIC_MAX

            self.async_write_ha_state()

        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/curve/temp_metric", message_received, qos=0
        )

        @callback
        def mode_message_received(msg):
            payload = msg.payload
            if payload == "unas_managed":
                self._current_mode = "unas_managed"
            elif payload == "auto":
                self._current_mode = "auto"
            elif payload == "target_temp":
                self._current_mode = "target_temp"
            elif payload.isdigit():
                self._current_mode = "set_speed"
            else:
                self._current_mode = None
            self.async_write_ha_state()

        self._unsubscribe_mode = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/mode", mode_message_received, qos=0
        )

    async def _publish_metric(self, metric: str) -> None:
        try:
            await mqtt.async_publish(
                self.hass,
                f"{self._topics['control']}/fan/curve/temp_metric",
                metric,
                qos=0,
                retain=True,
            )
        except Exception as err:
            _LOGGER.error("Failed to publish temp metric: %s", err)

    @property
    def available(self) -> bool:
        mqtt_available = self.coordinator.mqtt_client.is_available()
        service_running = self.coordinator.data.get("fan_control_running", False)
        has_state = self._current_option is not None

        if not (mqtt_available and service_running and has_state):
            return False

        # Only available in Target Temp mode
        return self._current_mode == "target_temp"

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from temp metric: %s", err)
        if self._unsubscribe_mode:
            try:
                self._unsubscribe_mode()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from fan mode: %s", err)
        await super().async_will_remove_from_hass()

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        if self._current_mode is not None:
            attrs["current_mode"] = self._current_mode
        return attrs

    async def async_select_option(self, option: str) -> None:
        mqtt_value = "avg" if option == TEMP_METRIC_AVG else "max"
        await self._publish_metric(mqtt_value)

        self._current_option = option
        self.async_write_ha_state()


class UNASResponseSpeedSelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Select entity for choosing fan response speed preset."""

    def __init__(self, coordinator: UNASDataUpdateCoordinator, hass: HomeAssistant) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self._topics = get_mqtt_topics(coordinator.entry.entry_id)
        self._attr_has_entity_name = True
        self._attr_name = "Response Speed"
        self._attr_unique_id = f"{coordinator.entry.entry_id}_response_speed"
        self._attr_icon = "mdi:speedometer"
        self._current_option = None
        self._unsubscribe = None
        self._current_mode = None
        self._unsubscribe_mode = None

        self._attr_options = [RESPONSE_RELAXED, RESPONSE_BALANCED, RESPONSE_AGGRESSIVE]

        device_name, device_model = get_device_info(coordinator.entry.data[CONF_DEVICE_MODEL])
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=device_name,
            manufacturer="Ubiquiti",
            model=device_model,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        if (last_state := await self.async_get_last_state()) is not None:
            if last_state.state in self._attr_options:
                self._current_option = last_state.state
            self._current_mode = last_state.attributes.get("current_mode")
        if self._current_option is None:
            self._current_option = RESPONSE_BALANCED

        @callback
        def message_received(msg):
            payload = msg.payload

            if payload == "relaxed":
                self._current_option = RESPONSE_RELAXED
            elif payload == "aggressive":
                self._current_option = RESPONSE_AGGRESSIVE
            else:
                self._current_option = RESPONSE_BALANCED

            self.async_write_ha_state()

        self._unsubscribe = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/curve/response_speed", message_received, qos=0
        )

        @callback
        def mode_message_received(msg):
            payload = msg.payload
            if payload == "unas_managed":
                self._current_mode = "unas_managed"
            elif payload == "auto":
                self._current_mode = "auto"
            elif payload == "target_temp":
                self._current_mode = "target_temp"
            elif payload.isdigit():
                self._current_mode = "set_speed"
            else:
                self._current_mode = None
            self.async_write_ha_state()

        self._unsubscribe_mode = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/mode", mode_message_received, qos=0
        )

    def _option_to_mqtt(self, option: str) -> str:
        if option == RESPONSE_RELAXED:
            return "relaxed"
        elif option == RESPONSE_AGGRESSIVE:
            return "aggressive"
        return "balanced"

    async def _publish_speed(self, speed: str) -> None:
        try:
            await mqtt.async_publish(
                self.hass,
                f"{self._topics['control']}/fan/curve/response_speed",
                speed,
                qos=0,
                retain=True,
            )
        except Exception as err:
            _LOGGER.error("Failed to publish response speed: %s", err)

    @property
    def available(self) -> bool:
        mqtt_available = self.coordinator.mqtt_client.is_available()
        service_running = self.coordinator.data.get("fan_control_running", False)
        has_state = self._current_option is not None

        if not (mqtt_available and service_running and has_state):
            return False

        # Only available in Target Temp mode
        return self._current_mode == "target_temp"

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            try:
                self._unsubscribe()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from response speed: %s", err)
        if self._unsubscribe_mode:
            try:
                self._unsubscribe_mode()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from fan mode: %s", err)
        await super().async_will_remove_from_hass()

    @property
    def current_option(self) -> str | None:
        return self._current_option

    @property
    def extra_state_attributes(self) -> dict:
        attrs = {}
        if self._current_mode is not None:
            attrs["current_mode"] = self._current_mode
        return attrs

    async def async_select_option(self, option: str) -> None:
        mqtt_value = self._option_to_mqtt(option)
        await self._publish_speed(mqtt_value)

        self._current_option = option
        self.async_write_ha_state()
