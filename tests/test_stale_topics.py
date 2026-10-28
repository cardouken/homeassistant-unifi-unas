import asyncio
from unittest.mock import patch

from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from conftest import DOMAIN, ROOT_TOPIC, setup_entry
from custom_components.unifi_unas import repairs, stale_topics

RETAINED = {
    "unas/01KH6QGE/availability": "offline",
    "unas/01KH782Z/availability": "offline",
    "unas/01KH782Z/smb/connections": "0",
    "unas/OTHERHA1/availability": "online",
    "unas/OTHERHA2/availability": "offline",
    "unas/OTHERHA2/fan/availability": "online",
    f"{ROOT_TOPIC}/availability": "offline",
    f"{ROOT_TOPIC}/system/cpu_temp": "50",
}
STALE = {
    "unas/01KH6QGE": ["unas/01KH6QGE/availability"],
    "unas/01KH782Z": ["unas/01KH782Z/availability", "unas/01KH782Z/smb/connections"],
}


async def find_with_retained(hass):
    with patch.object(stale_topics, "COLLECT_TIME", 0.05):
        task = asyncio.create_task(stale_topics.async_find_stale_topics(hass))
        await asyncio.sleep(0.01)
        for topic, payload in RETAINED.items():
            async_fire_mqtt_message(hass, topic, payload, retain=True)
        async_fire_mqtt_message(hass, "unas/LIVEONLY/availability", "offline", retain=False)
        return await task


async def test_only_offline_roots_without_an_entry_are_stale(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    assert await find_with_retained(hass) == STALE


async def test_the_scan_raises_a_fixable_issue(hass, mqtt_mock, ssh):
    with patch.object(stale_topics, "async_find_stale_topics", return_value=STALE):
        await stale_topics.async_scan(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, stale_topics.ISSUE_ID)
    assert issue.is_fixable
    assert issue.translation_placeholders == {"count": "3", "roots": "unas/01KH6QGE, unas/01KH782Z"}


async def test_the_repair_clears_only_stale_topics(hass, mqtt_mock, ssh):
    await setup_entry(hass)
    ir.async_create_issue(
        hass, DOMAIN, stale_topics.ISSUE_ID, is_fixable=True, severity=ir.IssueSeverity.WARNING,
        translation_key=stale_topics.ISSUE_ID, translation_placeholders={
            "count": "3", "roots": "unas/x"
        },
    )
    flow = await repairs.async_create_fix_flow(hass, stale_topics.ISSUE_ID, None)
    flow.hass = hass
    form = await flow.async_step_init()
    assert form["description_placeholders"] == {"count": "3", "roots": "unas/x"}

    mqtt_mock.async_publish.reset_mock()
    with patch.object(repairs, "async_find_stale_topics", return_value=STALE):
        assert (await flow.async_step_confirm({}))["type"] == "create_entry"
    cleared = {
        call.args[0] for call in mqtt_mock.async_publish.call_args_list if call.args[1] == ""
    }
    assert cleared == {topic for topics in STALE.values() for topic in topics}
    assert ir.async_get(hass).async_get_issue(DOMAIN, stale_topics.ISSUE_ID) is None
