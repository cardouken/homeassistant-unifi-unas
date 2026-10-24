from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, get_mqtt_root

_LOGGER = logging.getLogger(__name__)

ISSUE_ID = "stale_topics"
SCAN_DELAY = 60
COLLECT_TIME = 2
SCANNED_KEY = f"{DOMAIN}_stale_topics_scanned"


async def async_find_stale_topics(hass: HomeAssistant) -> dict[str, list[str]]:
    # retained topics of installs no config entry owns, as long as nothing there is online
    configured = {get_mqtt_root(entry.entry_id) for entry in hass.config_entries.async_entries(DOMAIN)}
    retained: dict[str, dict[str, str]] = defaultdict(dict)

    @callback
    def collect(msg) -> None:
        parts = msg.topic.split("/", 2)
        if msg.retain and msg.payload and len(parts) == 3:
            retained[f"{parts[0]}/{parts[1]}"][parts[2]] = msg.payload

    unsubscribe = await mqtt.async_subscribe(hass, "unas/#", collect)
    try:
        await asyncio.sleep(COLLECT_TIME)
    finally:
        unsubscribe()

    return {
        root: sorted(f"{root}/{topic}" for topic in topics)
        for root, topics in retained.items()
        if root not in configured and "online" not in (topics.get("availability"), topics.get("fan/availability"))
    }


async def async_clear_topics(hass: HomeAssistant, topics: list[str]) -> None:
    for topic in topics:
        await mqtt.async_publish(hass, topic, "", qos=0, retain=True)


async def async_scan(hass: HomeAssistant) -> None:
    try:
        await asyncio.sleep(SCAN_DELAY)
    except asyncio.CancelledError:
        # unloaded before the scan ran, the next setup schedules it again
        hass.data.pop(SCANNED_KEY, None)
        raise
    stale = await async_find_stale_topics(hass)
    if not stale:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    count = sum(len(topics) for topics in stale.values())
    _LOGGER.info("Found %d retained MQTT topics left by %d removed installs", count, len(stale))
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_ID,
        translation_placeholders={"count": str(count), "roots": ", ".join(sorted(stale))},
    )


@callback
def async_schedule_scan(hass: HomeAssistant, entry) -> None:
    # once per Home Assistant run
    if hass.data.get(SCANNED_KEY):
        return
    hass.data[SCANNED_KEY] = True
    entry.async_create_background_task(hass, async_scan(hass), f"{DOMAIN} stale topic scan")
