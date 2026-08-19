"""Repair flows for the UniFi UNAS integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import CONF_HOST_KEY

_LOGGER = logging.getLogger(__name__)


class HostKeyChangedRepairFlow(RepairsFlow):
    """Offer to re-pin the NAS SSH host key after a legitimate key change."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                # Clear the stored pin; the next connection re-pins via
                # trust-on-first-use and reloading applies it immediately.
                new_data = dict(entry.data)
                new_data.pop(CONF_HOST_KEY, None)
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(self._entry_id)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str] | None,
) -> RepairsFlow:
    """Create the fix flow for a repair issue."""
    entry_id = (data or {}).get("entry_id", "")
    if issue_id.startswith("host_key_changed") and entry_id:
        return HostKeyChangedRepairFlow(entry_id)
    # Unknown/non-fixable issue: a no-op confirm flow.
    return _NoopRepairFlow()


class _NoopRepairFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return self.async_create_entry(title="", data={})
