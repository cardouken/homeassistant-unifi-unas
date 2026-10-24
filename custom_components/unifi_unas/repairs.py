"""Repair flows for the UniFi UNAS integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_HOST, CONF_HOST_KEY, DOMAIN
from .stale_topics import ISSUE_ID as STALE_TOPICS_ISSUE, async_clear_topics, async_find_stale_topics

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
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if user_input is not None:
            if entry is not None:
                # Clear the stored pin; the next connection re-pins via
                # trust-on-first-use and reloading applies it immediately.
                new_data = dict(entry.data)
                new_data.pop(CONF_HOST_KEY, None)
                self.hass.config_entries.async_update_entry(entry, data=new_data)
                await self.hass.config_entries.async_reload(self._entry_id)
            return self.async_create_entry(title="", data={})

        host = entry.data.get(CONF_HOST, "") if entry is not None else ""
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"host": host},
        )


class StaleTopicsRepairFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is None:
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, STALE_TOPICS_ISSUE)
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders=dict(issue.translation_placeholders or {}) if issue else None,
            )
        # scanned again, something may have come online since the issue was raised
        stale = await async_find_stale_topics(self.hass)
        topics = [topic for root_topics in stale.values() for topic in root_topics]
        await async_clear_topics(self.hass, topics)
        _LOGGER.info("Cleared %d retained MQTT topics left by removed installs", len(topics))
        ir.async_delete_issue(self.hass, DOMAIN, STALE_TOPICS_ISSUE)
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str] | None,
) -> RepairsFlow:
    if issue_id == STALE_TOPICS_ISSUE:
        return StaleTopicsRepairFlow()
    return HostKeyChangedRepairFlow((data or {}).get("entry_id", ""))
