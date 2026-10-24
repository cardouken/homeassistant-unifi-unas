from __future__ import annotations

import asyncio
import logging
import json
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from datetime import datetime, timedelta

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .const import get_mqtt_root

_LOGGER = logging.getLogger(__name__)


"""
MQTT Topic Structure Mapping:

This client subscribes to unas/# and parses topics into keys for entity state.
When adding new topics, update both the publisher (unas_monitor.py/fan_control.py)
and the corresponding handler below.

Topic Pattern                        → Internal Key                   → Type
─────────────────────────────────────────────────────────────────────────────────
unas/availability                    → _status                        → status
unas/fan/availability                → _fan_status                    → status
unas/inventory                       → inventory                      → {bays, nvme, pools, shares}
unas/system/{metric}                 → unas_{metric}                  → value
unas/hdd/{bay}/{metric}              → unas_hdd_{bay}_{metric}        → value
unas/nvme/{slot}/{metric}            → unas_nvme_{slot}_{metric}      → value
unas/pool/{num}/{metric}             → unas_pool{num}_{metric}        → value
unas/share/{name}/{metric}           → unas_share_{name}_{metric}     → value
unas/smb/connections                 → unas_smb_connections           → value
unas/smb/clients                     → unas_smb_connections           → attributes
unas/nfs/mounts                      → unas_nfs_mounts                → value
unas/nfs/clients                     → unas_nfs_mounts                → attributes
unas/control/monitor_interval        → monitor_interval               → value
unas/control/fan/mode                → fan_mode                       → value
unas/control/fan/speed               → fan_speed_setting              → value
unas/control/fan/curve/{param}       → fan_curve_{param}              → value

Examples:
  unas/system/cpu_temp         → unas_cpu_temp = 45
  unas/hdd/1/temperature       → unas_hdd_1_temperature = 38
  unas/nvme/0/percentage_used  → unas_nvme_0_percentage_used = 5
  unas/smb/clients             → unas_smb_connections_attributes = [{...}]
"""


MACHINE_ID_WINDOW_SECONDS = 120
# the monitor republishes every value at least once a minute or two
STALE_AFTER = timedelta(minutes=5)
# for monitors older than the inventory topic
ITEM_KEYS = {
    "bays": re.compile(r"unas_hdd_(.+)_temperature"),
    "nvme": re.compile(r"unas_nvme_(.+)_temperature"),
    "pools": re.compile(r"unas_pool(\d+)_usage"),
    "shares": re.compile(r"unas_share_(.+)_usage"),
}


class UNASMQTTClient:
    def __init__(self, hass: HomeAssistant, entry_id: str, on_update: Callable[[], None]) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.mqtt_root = get_mqtt_root(entry_id)
        self._on_update = on_update
        self._data: dict[str, Any] = {}
        self._data_timestamps: dict[str, datetime] = {}
        self._subscriptions: list = []
        self._status: str = "unknown"
        self._fan_status: str | None = None
        self._last_update: datetime | None = None
        self._recent_machine_ids: dict[str, datetime] = {}
        self.inventory: dict[str, list[str] | None] | None = None
        self.topics: set[str] = set()
        self.received = asyncio.Event()

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

    @callback
    def async_unsubscribe(self) -> None:
        count = len(self._subscriptions)
        for unsub in self._subscriptions:
            unsub()
        self._subscriptions.clear()
        _LOGGER.debug("Unsubscribed from %d MQTT topics", count)

    def _schedule_refresh(self) -> None:
        self._on_update()

    @callback
    def _handle_message(self, msg) -> None:
        topic = msg.topic
        if not topic.startswith(self.mqtt_root):
            return
        self.received.set()
        if msg.payload:
            self.topics.add(topic)
        else:
            self.topics.discard(topic)
        
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
        if parts[0] == "inventory":
            self._store_inventory(payload)
        elif parts[0] == "availability":
            self._status = payload
            _LOGGER.debug("UNAS status: %s", self._status)
            if payload == "online":
                self._last_update = datetime.now()
            self._schedule_refresh()

    def _handle_two_part(self, parts, payload):
        category, item = parts[0], parts[1]
        
        # unas/system/<metric>
        if category == "system":
            self._store_value(f"unas_{item}", payload)
            if item == "machine_id" and payload:
                self._recent_machine_ids[payload] = datetime.now()
        
        # unas/smb/connections or unas/smb/clients
        elif category == "smb":
            if item == "connections":
                self._store_value("unas_smb_connections", payload)
            elif item == "clients":
                self._store_attributes("unas_smb_connections", payload)
        
        elif category == "fan":
            if item == "availability":
                self._fan_status = payload
                self._schedule_refresh()

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

        # unas/share/<name>/<metric>
        elif category == "share":
            if metric == "members":
                self._store_attributes(f"unas_share_{identifier}_member_count", payload)
            else:
                self._store_value(f"unas_share_{identifier}_{metric}", payload)

        # unas/control/fan/mode, unas/control/fan/speed
        elif category == "control" and identifier == "fan" and metric == "mode":
            self._store_value("fan_mode", payload)
        elif category == "control" and identifier == "fan" and metric == "speed":
            self._store_value("fan_speed_setting", payload)

    def _handle_four_part(self, parts, payload):
        if parts[0:3] == ["control", "fan", "curve"]:
            param = parts[3]
            self._store_value(f"fan_curve_{param}", payload)

    def _store_inventory(self, payload: str) -> None:
        try:
            inventory = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            _LOGGER.warning("Ignoring an invalid inventory: %s", payload[:200])
            return
        if isinstance(inventory, dict):
            # None means that source failed this cycle
            self.inventory = {
                category: None if items is None else [str(item) for item in items]
                for category, items in inventory.items()
                if items is None or isinstance(items, list)
            }
        elif inventory is None:
            self.inventory = None
        else:
            return
        self._schedule_refresh()

    def _store_value(self, key: str, payload: str) -> None:
        if not payload:
            # an emptied retained topic
            if self._data.pop(key, None) is not None:
                self._data_timestamps.pop(key, None)
                self._schedule_refresh()
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
        if not payload:
            self._store_value(f"{key}_attributes", payload)
            return
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
        if time_since_update <= 120:
            return True

        return False

    def is_fresh(self, key: str) -> bool:
        # a section of the monitor that keeps failing stops refreshing its values
        received = self._data_timestamps.get(key)
        return received is not None and datetime.now() - received <= STALE_AFTER

    @callback
    def async_apply(self, topic: str, payload: str) -> None:
        self._handle_message(SimpleNamespace(topic=topic, payload=payload))

    def fan_available(self) -> bool:
        # an older fan daemon never publishes fan/availability, follow the monitor then
        if self._fan_status is None:
            return self.is_available()
        return self._fan_status == "online"

    def get_data(self) -> dict[str, Any]:
        return self._data.copy()

    def items(self, category: str) -> tuple[set[str], bool]:
        # (items, whether the list is authoritative enough to remove what is missing)
        listed = (self.inventory or {}).get(category)
        if listed is not None:
            return set(listed), self._status == "online"
        pattern = ITEM_KEYS[category]
        return {match.group(1) for key in self._data if (match := pattern.fullmatch(key))}, False

    def listed(self, category: str, item: str) -> bool:
        listed = (self.inventory or {}).get(category)
        return listed is None or item in listed

    def get_recent_machine_ids(self) -> set[str]:
        cutoff = datetime.now() - timedelta(seconds=MACHINE_ID_WINDOW_SECONDS)
        self._recent_machine_ids = {
            mid: ts for mid, ts in self._recent_machine_ids.items() if ts > cutoff
        }
        return set(self._recent_machine_ids.keys())
