from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import UNASDataCoordinator, UNASServiceCoordinator

_LOGGER = logging.getLogger(__name__)

# how long the NAS must report an item gone before its entities are removed
REMOVE_AFTER = 600

STORAGE = ("bays", "nvme", "pools", "shares")
CATEGORIES = (*STORAGE, "backups")
# device identifier prefix (after the entry id) and retained topic prefix per category
DEVICE_PREFIX = {"bays": "hdd_", "nvme": "nvme_", "shares": "share_", "backups": "backup_"}
TOPIC_PREFIX = {"bays": "hdd", "nvme": "nvme", "pools": "pool", "shares": "share"}

Adder = Callable[[list[str]], None]


class Reconciler:
    def __init__(
            self,
            hass: HomeAssistant,
            entry: ConfigEntry,
            data: UNASDataCoordinator,
            service: UNASServiceCoordinator,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.data = data
        self.service = service
        self._adders: dict[str, list[Adder]] = {category: [] for category in CATEGORIES}
        self._added: dict[str, set[str]] = {category: set() for category in CATEGORIES}
        self._known: dict[str, set[str]] = {category: set() for category in CATEGORIES}
        self._present: dict[str, set[str]] = {category: set() for category in CATEGORIES}
        self._missing_since: dict[str, dict[str, float]] = {category: {} for category in CATEGORIES}

    @callback
    def register(self, category: str, adder: Adder) -> None:
        self._adders[category].append(adder)

    @callback
    def async_start(self) -> None:
        # also covers items that went away while Home Assistant was down
        self._known = self._registry_items()
        self.entry.async_on_unload(self.data.async_add_listener(self._async_storage))
        self.entry.async_on_unload(self.service.async_add_listener(self._async_backups))
        self._async_storage()

    @callback
    def _async_storage(self) -> None:
        for category in STORAGE:
            present, authoritative = self.data.mqtt_client.items(category)
            self._reconcile(category, present, authoritative)

    @callback
    def _async_backups(self) -> None:
        data = self.service.data
        if data is None:
            return
        present = {task["id"] for task in data["backup_tasks"]}
        authoritative = self.service.last_update_success and data["backup_tasks_current"]
        self._reconcile("backups", present, authoritative)

    def _reconcile(self, category: str, present: set[str], authoritative: bool) -> None:
        self._present[category] = present
        if new := sorted(present - self._added[category]):
            for adder in self._adders[category]:
                adder(new)
            self._added[category].update(new)
            self._known[category].update(new)

        missing = self._missing_since[category]
        if not authoritative:
            missing.clear()
            return
        now = time.monotonic()
        for item in present:
            missing.pop(item, None)
        for item in sorted(self._known[category] - present):
            if now - missing.setdefault(item, now) >= REMOVE_AFTER:
                self._remove(category, item)

    def _forget(self, category: str, item: str) -> None:
        self._added[category].discard(item)
        self._known[category].discard(item)
        self._missing_since[category].pop(item, None)

    def _remove(self, category: str, item: str) -> None:
        entry_id = self.entry.entry_id
        _LOGGER.info(
            "Removing %s %s, the UNAS has not reported it for %d s",
            category.removesuffix("s"), item, REMOVE_AFTER,
        )
        self._forget(category, item)
        if category in DEVICE_PREFIX:
            dev_reg = dr.async_get(self.hass)
            identifier = (DOMAIN, f"{entry_id}_{DEVICE_PREFIX[category]}{item}")
            if device := dev_reg.async_get_device_by_identifier(identifier, entry_id):
                dev_reg.async_remove_device(device.id)
        else:
            ent_reg = er.async_get(self.hass)
            prefix = f"{entry_id}_unas_pool{item}_"
            for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
                if entity.unique_id.startswith(prefix):
                    ent_reg.async_remove(entity.entity_id)
        if category in TOPIC_PREFIX:
            self._clear_topics(f"{self.data.mqtt_client.mqtt_root}/{TOPIC_PREFIX[category]}/{item}/")

    def _clear_topics(self, prefix: str) -> None:
        topics = sorted(topic for topic in self.data.mqtt_client.topics if topic.startswith(prefix))

        async def clear() -> None:
            for topic in topics:
                await mqtt.async_publish(self.hass, topic, "", qos=0, retain=True)

        if topics:
            self.entry.async_create_background_task(self.hass, clear(), f"{DOMAIN} clear {prefix}")

    def _registry_items(self) -> dict[str, set[str]]:
        entry_id = self.entry.entry_id
        known: dict[str, set[str]] = {category: set() for category in CATEGORIES}
        for device in dr.async_entries_for_config_entry(dr.async_get(self.hass), entry_id):
            for category, item in self._device_items(device):
                known[category].add(item)
        pool = re.compile(rf"{re.escape(entry_id)}_unas_pool(\d+)_usage")
        for entity in er.async_entries_for_config_entry(er.async_get(self.hass), entry_id):
            if match := pool.fullmatch(entity.unique_id):
                known["pools"].add(match.group(1))
        return known

    def _device_items(self, device: dr.DeviceEntry) -> list[tuple[str, str]]:
        own = f"{self.entry.entry_id}_"
        items = []
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or not identifier.startswith(own):
                continue
            rest = identifier[len(own):]
            for category, prefix in DEVICE_PREFIX.items():
                if rest.startswith(prefix):
                    items.append((category, rest[len(prefix):]))
        return items

    @callback
    def async_remove_device(self, device: dr.DeviceEntry) -> bool:
        items = self._device_items(device)
        if not items or any(item in self._present[category] for category, item in items):
            return False
        for category, item in items:
            self._forget(category, item)
            if category in TOPIC_PREFIX:
                self._clear_topics(f"{self.data.mqtt_client.mqtt_root}/{TOPIC_PREFIX[category]}/{item}/")
        return True
