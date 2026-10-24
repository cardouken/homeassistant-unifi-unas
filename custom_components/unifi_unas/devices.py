from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import CONF_HOST, DOMAIN, format_remote_type, get_device_info

_LOGGER = logging.getLogger(__name__)

# model prefix -> manufacturer, longest prefixes first where they overlap
MANUFACTURERS = (
    ("WDC", "Western Digital"),
    ("WD", "Western Digital"),
    ("WUH", "Western Digital"),
    ("HGST", "HGST"),
    ("HUH", "HGST"),
    ("HUS", "HGST"),
    ("TOSHIBA", "Toshiba"),
    ("MG0", "Toshiba"),
    ("HDWG", "Toshiba"),
    ("ST", "Seagate"),
    ("SAMSUNG", "Samsung"),
    ("CT", "Crucial"),
    ("KINGSTON", "Kingston"),
    ("MICRON", "Micron"),
    ("INTEL", "Intel"),
    ("SK HYNIX", "SK hynix"),
)
DRIVE_KINDS = {"bays": ("hdd", "HDD"), "nvme": ("nvme", "NVMe")}


def _text(value: Any) -> str | None:
    # MQTT values arrive typed, a numeric serial would otherwise be an int
    return str(value) if value not in (None, "") else None


def drive_manufacturer(model: str | None) -> str | None:
    if not model or model == "Unknown":
        return None
    upper = model.upper()
    for prefix, manufacturer in MANUFACTURERS:
        if upper.startswith(prefix):
            return manufacturer
    return model.split()[0] if " " in model else None


def nas_info(data: dict[str, Any]) -> dict[str, Any] | None:
    try:
        info = json.loads(data.get("unas_device") or "null")
    except (TypeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def main_device_fields(entry: ConfigEntry, nas: dict[str, Any] | None) -> dict[str, Any]:
    _, fallback_model = get_device_info(entry.data)
    host = entry.data[CONF_HOST]
    fields: dict[str, Any] = {
        "manufacturer": "Ubiquiti",
        "model": fallback_model,
        # an IPv6 address needs brackets in a URL
        "configuration_url": f"https://[{host}]" if ":" in host else f"https://{host}",
    }
    if nas:
        fields.update(
            model=nas.get("product") or fallback_model,
            model_id=nas.get("sku") or nas.get("shortname"),
            serial_number=nas.get("serial"),
            sw_version=nas.get("firmware"),
            hw_version=str(nas["hw_revision"]) if nas.get("hw_revision") is not None else None,
        )
    return fields


def main_connections(nas: dict[str, Any] | None) -> set[tuple[str, str]]:
    if nas and nas.get("mac"):
        return {(dr.CONNECTION_NETWORK_MAC, dr.format_mac(nas["mac"]))}
    return set()


@callback
def async_register_main_device(hass: HomeAssistant, entry: ConfigEntry, data: dict[str, Any]) -> None:
    nas = nas_info(data)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        connections=main_connections(nas),
        name=get_device_info(entry.data)[0],
        **main_device_fields(entry, nas),
    )


def main_device(entry: ConfigEntry) -> DeviceInfo:
    # registered in setup, entities only link to it
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})


def _parent_id(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    parent = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    return parent.id if parent else None


def drive_device(hass: HomeAssistant, entry: ConfigEntry, category: str, slot: str, data: dict[str, Any]) -> DeviceInfo:
    prefix, label = DRIVE_KINDS[category]
    model = _text(data.get(f"unas_{prefix}_{slot}_model"))
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{prefix}_{slot}")},
        name=f"{get_device_info(entry.data)[0]} {label} {slot}",
        manufacturer=drive_manufacturer(model) or "Unknown",
        model=model or "Unknown",
        serial_number=_text(data.get(f"unas_{prefix}_{slot}_serial")),
        sw_version=_text(data.get(f"unas_{prefix}_{slot}_firmware")),
        via_device_id=_parent_id(hass, entry),
    )


def share_device(hass: HomeAssistant, entry: ConfigEntry, share: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_share_{share}")},
        name=f"{get_device_info(entry.data)[0]} Share: {share}",
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=_parent_id(hass, entry),
    )


def backup_device(hass: HomeAssistant, entry: ConfigEntry, task: dict[str, Any]) -> DeviceInfo:
    remote = task.get("remote", {})
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_backup_{task['id']}")},
        name=f"{get_device_info(entry.data)[0]} Backup {task['name']}",
        manufacturer=format_remote_type(remote.get("type")),
        model=remote.get("oauth2Account") or task.get("destinationDir", ""),
        entry_type=DeviceEntryType.SERVICE,
        via_device_id=_parent_id(hass, entry),
    )


class DeviceSync:
    # keeps the registry in step with the NAS: firmware updates, swapped drives
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self._applied: dict[str, Any] = {}

    @callback
    def async_start(self) -> None:
        self.entry.async_on_unload(self.coordinator.async_add_listener(self._async_sync))
        self._async_sync()

    @callback
    def _async_sync(self) -> None:
        data = self.coordinator.data or {}
        dev_reg = dr.async_get(self.hass)
        entry_id = self.entry.entry_id

        nas = nas_info(data)
        if nas and nas != self._applied.get("main"):
            if device := dev_reg.async_get_device_by_identifier((DOMAIN, entry_id), entry_id):
                dev_reg.async_update_device(
                    device.id, new_connections=main_connections(nas), **main_device_fields(self.entry, nas)
                )
            self._applied["main"] = nas

        for category, (prefix, _) in DRIVE_KINDS.items():
            for slot in self.coordinator.mqtt_client.items(category)[0]:
                model = _text(data.get(f"unas_{prefix}_{slot}_model"))
                serial = _text(data.get(f"unas_{prefix}_{slot}_serial"))
                if model is None or serial is None:
                    continue
                wanted = {
                    "manufacturer": drive_manufacturer(model) or "Unknown",
                    "model": model,
                    "serial_number": serial,
                    "sw_version": _text(data.get(f"unas_{prefix}_{slot}_firmware")),
                }
                key = f"{prefix}_{slot}"
                if wanted == self._applied.get(key):
                    continue
                device = dev_reg.async_get_device_by_identifier((DOMAIN, f"{entry_id}_{key}"), entry_id)
                if device is None:
                    continue
                if device.serial_number not in (None, wanted["serial_number"]):
                    _LOGGER.info("%s %s now holds %s (was %s)", prefix.upper(), slot, wanted["serial_number"],
                                 device.serial_number)
                dev_reg.async_update_device(device.id, **wanted)
                self._applied[key] = wanted
