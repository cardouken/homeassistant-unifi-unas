from __future__ import annotations

import asyncio
import logging
import json
from typing import Any
from datetime import datetime

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import get_mqtt_root

_LOGGER = logging.getLogger(__name__)


"""
MQTT Topic Structure Mapping:

This client subscribes to unas/# and parses topics into keys for entity state.
When adding new topics, update both the publisher (unas_monitor.py/fan_control.sh) 
and the corresponding handler below.

Topic Pattern                        → Internal Key                   → Type
─────────────────────────────────────────────────────────────────────────────────
unas/availability                    → _status                        → status
unas/system/{metric}                 → unas_{metric}                  → value
unas/hdd/{bay}/{metric}              → unas_hdd_{bay}_{metric}        → value
unas/nvme/{slot}/{metric}            → unas_nvme_{slot}_{metric}      → value
unas/pool/{num}/{metric}             → unas_pool{num}_{metric}        → value
unas/smb/connections                 → unas_smb_connections           → value
unas/smb/clients                     → unas_smb_connections           → attributes
unas/nfs/mounts                      → unas_nfs_mounts                → value
unas/nfs/clients                     → unas_nfs_mounts                → attributes
unas/control/monitor_interval        → monitor_interval               → value
unas/control/fan/mode                → fan_mode                       → value
unas/control/fan/curve/{param}       → fan_curve_{param}              → value

Examples:
  unas/system/cpu_temp         → unas_cpu_temp = 45
  unas/hdd/1/temperature       → unas_hdd_1_temperature = 38
  unas/nvme/0/percentage_used  → unas_nvme_0_percentage_used = 5
  unas/smb/clients             → unas_smb_connections_attributes = [{...}]
"""


REFRESH_DEBOUNCE_SECONDS = 0.5


class UNASMQTTClient:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.mqtt_root = get_mqtt_root(entry_id)
        self._data: dict[str, Any] = {}
        self._data_timestamps: dict[str, datetime] = {}
        self._subscriptions: list = []
        self._status: str = "unknown"
        self._last_update: datetime | None = None
        self._pending_refresh: asyncio.TimerHandle | None = None
        self._coordinator = None

    async def async_subscribe(self) -> None:
        if mqtt.DOMAIN not in self.hass.data:
            _LOGGER.error("MQTT integration not loaded")
            return

        try:
            sub = await mqtt.async_subscribe(self.hass, f"{self.mqtt_root}/#", self._handle_message, qos=0)
            self._subscriptions.append(sub)
            _LOGGER.debug("Subscribed to MQTT topic: %s/#", self.mqtt_root)
        except Exception as err:
            _LOGGER.error("Failed to subscribe to %s/#: %s", self.mqtt_root, err)

    async def async_unsubscribe(self) -> None:
        pending = self._pending_refresh
        self._pending_refresh = None
        if pending:
            pending.cancel()
        count = len(self._subscriptions)
        for unsub in self._subscriptions:
            unsub()
        self._subscriptions.clear()
        _LOGGER.debug("Unsubscribed from %d MQTT topics", count)

    def _schedule_refresh(self) -> None:
        pending = self._pending_refresh
        if pending:
            pending.cancel()

        def do_refresh():
            self._pending_refresh = None
            coordinator = self._coordinator
            if coordinator is not None:
                self.hass.async_create_task(coordinator.async_request_refresh())

        self._pending_refresh = self.hass.loop.call_later(REFRESH_DEBOUNCE_SECONDS, do_refresh)

    @callback
    def _handle_message(self, msg) -> None:
        topic = msg.topic
        if not topic.startswith(self.mqtt_root):
            return
        
        parts = topic[len(self.mqtt_root):].lstrip("/").split("/")
        if not parts:
            return
        
        num_parts = len(parts)
        
        if num_parts == 1:
            self._handle_one_part(parts, msg.payload)
        elif num_parts == 2:
            self._handle_two_part(parts, msg.payload)
        elif num_parts == 3:
            self._handle_three_part(parts, msg.payload)
        elif num_parts == 4:
            self._handle_four_part(parts, msg.payload)

    def _handle_one_part(self, parts, payload):
        if parts[0] == "availability":
            self._status = payload
            _LOGGER.debug("UNAS status: %s", self._status)
            self._schedule_refresh()

    def _handle_two_part(self, parts, payload):
        category, item = parts[0], parts[1]
        
        # unas/system/<metric>
        if category == "system":
            self._store_value(f"unas_{item}", payload)
        
        # unas/smb/connections or unas/smb/clients
        elif category == "smb":
            if item == "connections":
                self._store_value("unas_smb_connections", payload)
            elif item == "clients":
                self._store_attributes("unas_smb_connections", payload)
        
        # unas/nfs/mounts or unas/nfs/clients
        elif category == "nfs":
            if item == "mounts":
                self._store_value("unas_nfs_mounts", payload)
            elif item == "clients":
                self._store_attributes("unas_nfs_mounts", payload)
        
        # unas/control/<setting>
        elif category == "control":
            self._store_value(item, payload)

    def _handle_three_part(self, parts, payload):
        category, identifier, metric = parts[0], parts[1], parts[2]
        
        # unas/hdd/<bay>/<metric> or unas/nvme/<slot>/<metric>
        if category in ("hdd", "nvme"):
            self._store_value(f"unas_{category}_{identifier}_{metric}", payload)
        
        # unas/pool/<num>/<metric>
        elif category == "pool":
            self._store_value(f"unas_pool{identifier}_{metric}", payload)
        
        # unas/control/fan/mode
        elif category == "control" and identifier == "fan" and metric == "mode":
            self._store_value("fan_mode", payload)

    def _handle_four_part(self, parts, payload):
        if parts[0:3] == ["control", "fan", "curve"]:
            param = parts[3]
            self._store_value(f"fan_curve_{param}", payload)

    def _store_value(self, key: str, payload: str) -> None:
        if not payload:
            return

        value: str | int | float = payload
        if "." in payload:
            try:
                value = float(payload)
            except ValueError:
                pass
        else:
            try:
                value = int(payload)
            except ValueError:
                pass

        self._data[key] = value
        self._data_timestamps[key] = datetime.now()
        self._last_update = datetime.now()
        self._schedule_refresh()

    def _store_attributes(self, key: str, payload: str) -> None:
        try:
            self._data[f"{key}_attributes"] = json.loads(payload)
            self._data_timestamps[f"{key}_attributes"] = datetime.now()
            self._last_update = datetime.now()
            self._schedule_refresh()
        except json.JSONDecodeError:
            _LOGGER.warning("Failed to parse JSON attributes for %s", key)

    def is_available(self) -> bool:
        if self._status == "offline":
            return False

        if self._last_update is None:
            return False

        time_since_update = (datetime.now() - self._last_update).total_seconds()
        if time_since_update > 120:
            return False

        return True

    def get_data(self) -> dict[str, Any]:
        self._cleanup_stale_data()
        return self._data.copy()

    def _cleanup_stale_data(self) -> None:
        now = datetime.now()
        stale_keys = []
        
        for key, timestamp in self._data_timestamps.items():
            if key.startswith(("fan_curve_", "fan_mode", "monitor_interval")):
                continue
            
            if (now - timestamp).total_seconds() > 120:
                stale_keys.append(key)
        
        for key in stale_keys:
            del self._data[key]
            del self._data_timestamps[key]
