from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import UNASConfigEntry
from .const import CONF_HOST_KEY, CONF_MQTT_PASSWORD, CONF_MQTT_USER, CONF_PASSWORD
from .devices import nas_info

TO_REDACT = {
    CONF_PASSWORD,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_USER,
    CONF_HOST_KEY,
    "serial",
    "mac",
    "machine_id",
    "unas_machine_id",
    "email",
    "createdBy",
    "oauth2Account",
    "clients",
    "members",
    # a backup task's remote server
    "ip",
    "user",
}
REDACT_SUFFIXES = ("_serial", "_attributes", "machine_id", "_device")


def _redact_keys(data: dict[str, Any]) -> dict[str, Any]:
    # by key name: unas_hdd_1_serial, the client lists, and unas_device (shown redacted on its own)
    return {
        key: "**REDACTED**" if key.endswith(REDACT_SUFFIXES) else value
        for key, value in data.items()
    }


async def async_get_config_entry_diagnostics(
        hass: HomeAssistant, entry: UNASConfigEntry
) -> dict[str, Any]:
    runtime = entry.runtime_data
    client = runtime.data.mqtt_client
    service = runtime.service
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "version": f"{entry.version}.{entry.minor_version}",
        },
        "mqtt": {
            "monitor_available": client.is_available(),
            "fan_available": client.fan_available(),
            "inventory": client.inventory,
            "device": async_redact_data(nas_info(runtime.data.data) or {}, TO_REDACT),
            "topics": len(client.topics),
            "data": _redact_keys(runtime.data.data),
        },
        "ssh": {
            "last_update_success": service.last_update_success,
            "last_exception": repr(service.last_exception) if service.last_exception else None,
            "data": async_redact_data(service.data, TO_REDACT) if service.data else None,
        },
        "deploy": {
            "running": runtime.deployer.running,
            "attempts": runtime.deployer.attempts,
            "last_error": runtime.deployer.last_error,
        },
    }
