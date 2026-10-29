import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from conftest import DOMAIN, entry_data, fire, settle, setup_entry
from custom_components.unifi_unas import discovery
from custom_components.unifi_unas.discovery import discovered_model

FLOW = "custom_components.unifi_unas.config_flow"
MAC = "0c:ea:14:c0:c0:79"
PIN = "192.168.1.25 ssh-ed25519 AAAA"
UNAS = SimpleNamespace(
    hw_addr=MAC, source_ip="192.168.1.25", platform="UNASPRO", hostname="LIL-NAS-X"
)
UDM = SimpleNamespace(hw_addr="0c:ea:14:62:66:01", source_ip="192.168.1.1", platform="UDMPROSE",
                      hostname="UDM-SE")


def flows(hass):
    return [f for f in hass.config_entries.flow.async_progress() if f["handler"] == DOMAIN]


@pytest.mark.parametrize("platform,model", [
    ("UNASPRO", "UNAS_PRO"), ("UNAS-Pro-8", "UNAS_PRO_8"), ("UNAS4B", "UNAS_4"),
    ("unvr-pro", "UNVR_PRO"), ("UDMPROSE", None), ("UNVR-G2", None), ("ENAS", None), (None, None),
])
def test_discovered_models(platform, model):
    assert discovered_model(platform) == model


async def test_a_scan_offers_the_unas_only(hass, mqtt_mock, ssh, scanner):
    scanner.devices = [UNAS, UDM]
    await discovery._async_discover(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    (flow,) = flows(hass)
    assert flow["context"]["source"] == config_entries.SOURCE_INTEGRATION_DISCOVERY
    assert flow["context"]["title_placeholders"] == {"name": "LIL-NAS-X"}
    assert flow["context"]["unique_id"] == "0cea14c0c079"
    form = await hass.config_entries.flow.async_configure(flow["flow_id"])
    host = next(key for key in form["data_schema"].schema if str(key) == "host")
    assert host.description == {"suggested_value": "192.168.1.25"}

    identity = {"sysid": "ea51", "serial": "0CEA14C0C079"}
    with (
        patch(f"{FLOW}.UNASProConfigFlow._probe_ssh", return_value=(None, identity)),
        patch(f"{FLOW}.probe_mqtt", return_value=None),
    ):
        data = {key: value for key, value in entry_data().items() if key != "device_model"}
        result = await hass.config_entries.flow.async_configure(flow["flow_id"], data)
        await hass.async_block_till_done()
    assert result["result"].unique_id == "0cea14c0c079"


@pytest.fixture
async def entry(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass, verify_host_key=True, host_key=PIN)
    hass.config_entries.async_update_entry(entry, unique_id="0cea14c0c079")
    return entry


async def discover(hass, scanner, host):
    scanner.devices = [SimpleNamespace(**{**vars(UNAS), "source_ip": host})]
    await discovery._async_discover(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_a_known_nas_that_answers_stays_where_it_is(hass, entry, scanner):
    await discover(hass, scanner, "192.168.1.26")
    assert entry.data["host"] == "192.168.1.25"
    assert entry.data["host_key"] == PIN
    assert flows(hass) == []


def host_key(key="ssh-ed25519 AAAA", error=None):
    return patch(f"{FLOW}.fetch_host_key", AsyncMock(return_value=key, side_effect=error))


async def stop_answering(entry, ssh):
    ssh.instances[-1].error = OSError("no route to host")
    await entry.runtime_data.service.async_refresh()


async def test_a_known_nas_that_stopped_answering_follows_its_new_address(
        hass, entry, scanner, ssh
):
    await stop_answering(entry, ssh)
    with host_key() as fetch:
        await discover(hass, scanner, "192.168.1.26")
        await settle(hass)
    fetch.assert_awaited_once_with("192.168.1.26")
    assert entry.data["host"] == "192.168.1.26"
    # same NAS, same key
    assert entry.data["host_key"] == "192.168.1.26 ssh-ed25519 AAAA"
    assert entry.title == "UNAS (192.168.1.26)"
    assert entry.runtime_data.connection["host"] == "192.168.1.26"
    assert flows(hass) == []


@pytest.mark.parametrize("answer", [
    {"key": "ssh-ed25519 BBBB"}, {"error": OSError("connection refused")},
    {"error": TimeoutError()},
])
async def test_an_address_without_the_pinned_key_is_not_followed(
        hass, entry, scanner, ssh, answer
):
    # a forged discovery reply must not get the login
    await stop_answering(entry, ssh)
    with host_key(**answer):
        await discover(hass, scanner, "192.168.1.26")
        await settle(hass)
    assert entry.data["host"] == "192.168.1.25"
    assert entry.data["host_key"] == PIN


async def test_a_nas_that_is_not_set_up_is_not_moved(hass, mqtt_mock, scanner):
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(), unique_id="0cea14c0c079")
    entry.add_to_hass(hass)
    with host_key() as fetch:
        await discover(hass, scanner, "192.168.1.26")
    fetch.assert_not_called()
    assert entry.data["host"] == "192.168.1.25"


async def test_an_ignored_nas_stays_ignored(hass, mqtt_mock, scanner, caplog):
    MockConfigEntry(
        domain=DOMAIN, data={}, unique_id="0cea14c0c079", source=config_entries.SOURCE_IGNORE
    ).add_to_hass(hass)
    await discover(hass, scanner, "192.168.1.25")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.1.25", hostname="lil-nas-x", macaddress="0cea14c0c079"),
    )
    # just some UniFi device to the DHCP step, the scan's flow then aborts
    assert result["reason"] == "discovery_started"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert flows(hass) == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_dhcp_from_another_unifi_device_starts_a_scan(hass, mqtt_mock, scanner):
    scanner.devices = [UNAS]
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_DHCP},
        data=DhcpServiceInfo(ip="192.168.1.25", hostname="lil-nas-x", macaddress="0cea14c0c079"),
    )
    assert result["reason"] == "discovery_started"
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [f["context"]["source"] for f in flows(hass)] == ["integration_discovery"]


async def test_dhcp_follows_an_address_keyed_entry_by_its_mac(hass, mqtt_mock, ssh):
    entry = await setup_entry(hass)
    fire(hass, "system/device", json.dumps({"name": "UNAS Pro", "mac": "0CEA14C0C079"}))
    await settle(hass)
    await stop_answering(entry, ssh)
    with host_key():
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_DHCP},
            data=DhcpServiceInfo(
                ip="192.168.1.27", hostname="lil-nas-x", macaddress="0cea14c0c079"
            ),
        )
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.27"
