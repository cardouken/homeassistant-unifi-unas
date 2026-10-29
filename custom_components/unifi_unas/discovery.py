from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.event import async_track_time_interval
from unifi_discovery import AIOUnifiScanner

from .const import DISCOVERY_PLATFORMS, DOMAIN

_LOGGER = logging.getLogger(__name__)

DISCOVERY_INTERVAL = timedelta(minutes=60)
DATA_DISCOVERY_STARTED = f"{DOMAIN}_discovery_started"


def discovered_model(platform: str | None) -> str | None:
    # the console answers with a uidb short name, "UNASPRO" or "UNAS-Pro"
    return DISCOVERY_PLATFORMS.get((platform or "").upper().replace("-", ""))


@callback
def async_start_discovery(hass: HomeAssistant) -> None:
    if hass.data.get(DATA_DISCOVERY_STARTED):
        return
    hass.data[DATA_DISCOVERY_STARTED] = True

    @callback
    def _async_scan(*_: Any) -> None:
        hass.async_create_background_task(_async_discover(hass), f"{DOMAIN} discovery")

    _async_scan()
    async_track_time_interval(hass, _async_scan, DISCOVERY_INTERVAL, cancel_on_shutdown=True)


async def _async_discover(hass: HomeAssistant) -> None:
    try:
        devices = await AIOUnifiScanner().async_scan()
    except OSError as err:
        _LOGGER.debug("UniFi discovery failed: %s", err)
        return
    for device in devices:
        if device.hw_addr and device.source_ip and discovered_model(device.platform):
            discovery_flow.async_create_flow(
                hass,
                DOMAIN,
                context={"source": config_entries.SOURCE_INTEGRATION_DISCOVERY},
                data={
                    "host": device.source_ip,
                    "mac": format_mac(device.hw_addr),
                    "hostname": device.hostname,
                    "platform": device.platform,
                },
            )
