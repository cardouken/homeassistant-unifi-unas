import json
import logging
from types import SimpleNamespace

import pytest

import unas_monitor as um
from fakes import fake_connection, json_body


class FakeMQTT:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload


@pytest.fixture
def monitor(monkeypatch):
    mon = um.UNASMonitor(mqtt_enabled=False)
    mon.mqtt = FakeMQTT()
    monkeypatch.setattr(mon, "get_system_metrics", lambda: {"fan_speed": 80, "cpu_temp": 50})
    monkeypatch.setattr(mon, "get_device_info", lambda: {"shortname": "UNASPRO", "sysid": "ea51"})
    drives = [{"bay": "2", "temperature": 41}, {"bay": "10", "temperature": 40}]
    monkeypatch.setattr(mon, "get_drives", lambda: drives)
    monkeypatch.setattr(mon, "get_nvme_drives", lambda: [])
    monkeypatch.setattr(mon, "get_pools_from_api", lambda: [{"pool": 1, "usage": 52}])
    monkeypatch.setattr(
        mon, "get_smb_data", lambda: [{"username": "a", "ip": "1.2.3.4", "share": "x"}]
    )
    monkeypatch.setattr(mon, "get_nfs_mounts", lambda: [])
    monkeypatch.setattr(mon, "get_shares", lambda: [{"name": "plex", "usage": 1.5}])
    monkeypatch.setattr(mon, "_get_storage", lambda: STORAGE)
    return mon


def topic(name):
    return f"{um.MQTT_ROOT}/{name}"


STORAGE = {
    "pools": [{"id": 1}],
    "disks": [
        {"slotId": "2", "state": "optimal"}, {"slotId": "10", "state": "optimal"},
        {"slotId": "11", "state": "empty"},
    ],
}


def test_publishes_inventory_and_device(monitor):
    monitor.collect_and_publish()
    published = monitor.mqtt.published
    assert json.loads(published[topic("inventory")]) == {
        "bays": ["2", "10"], "nvme": [], "pools": [1], "shares": ["plex"],
    }
    assert json.loads(published[topic("system/device")])["sysid"] == "ea51"
    assert published[topic("hdd/2/temperature")] == "41"
    assert published[topic("smb/connections")] == "1"


def test_a_drive_smartctl_fails_on_stays_in_the_inventory(monitor, monkeypatch):
    storage = {**STORAGE, "disks": [*STORAGE["disks"], {"slotId": "3", "state": "optimal"}]}
    monkeypatch.setattr(monitor, "_get_storage", lambda: storage)
    monitor.collect_and_publish()
    assert json.loads(monitor.mqtt.published[topic("inventory")])["bays"] == ["2", "3", "10"]
    assert topic("hdd/3/temperature") not in monitor.mqtt.published


def test_pools_from_the_df_fallback_are_not_authoritative(monitor, monkeypatch):
    monkeypatch.setattr(monitor, "_get_storage", lambda: None)
    monitor.collect_and_publish()
    published = monitor.mqtt.published
    assert json.loads(published[topic("inventory")])["pools"] is None
    assert published[topic("pool/1/usage")] == "52"


def test_failing_section_does_not_stop_the_cycle(monitor, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def broken():
        raise OSError("api down")

    monkeypatch.setattr(monitor, "get_shares", broken)
    monitor.collect_and_publish()
    monitor._last_published.clear()
    monitor.collect_and_publish()
    published = monitor.mqtt.published
    assert json.loads(published[topic("inventory")])["shares"] is None
    assert published[topic("pool/1/usage")] == "52"
    assert published[topic("nfs/mounts")] == "0"
    assert sum("Collecting shares failed" in r.message for r in caplog.records) == 1

    monkeypatch.setattr(monitor, "get_shares", lambda: [])
    monitor.collect_and_publish()
    assert any("Collecting shares recovered" in r.message for r in caplog.records)
    assert json.loads(published[topic("inventory")])["shares"] == []


def test_storage_api_slot_wins_over_ata_map(monkeypatch):
    mon = um.UNASMonitor(mqtt_enabled=False)
    mon._storage = {"disks": [{"serial": "ZR52FR9Y", "slotId": "1"}, {"serial": "", "slotId": "7"}]}
    monkeypatch.setattr(mon, "run_cmd", lambda cmd: "/devices/ata8/host7/target7")
    assert mon.get_bay_number("sdf", "ZR52FR9Y") == "1"
    assert mon.get_bay_number("sdz", "UNKNOWN") == um.ATA_TO_BAY.get("8")


def test_bay_lookup_ignores_missing_serials(monkeypatch):
    mon = um.UNASMonitor(mqtt_enabled=False)
    mon._storage = {"disks": [{"serial": None, "slotId": "3"}, {"serial": "", "slotId": "7"}]}
    monkeypatch.setattr(mon, "run_cmd", lambda cmd: "/devices/ata8/host7/target7")
    assert mon.get_bay_number("sdz", "") == um.ATA_TO_BAY.get("8")
    assert mon.get_bay_number("sdz", None) == um.ATA_TO_BAY.get("8")


SYSTEM = {"hardware": {
    "name": "UniFi Network Attached Storage Pro", "shortname": "UNASPRO", "sysid": 59985,
    "serialno": "0cea14c0c079", "mac": "0CEA14C0C079", "hwrev": 960263, "firmwareVersion": "6.0.11",
}}


def test_device_info_from_the_system_api(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        um.http.client, "HTTPConnection", fake_connection(body=json_body(SYSTEM), calls=calls)
    )
    table = tmp_path / "uidb.json"
    table.write_text(json.dumps({
        "devices": [{"sysid": "ea51", "sku": "UNAS-Pro", "product": {"abbrev": "UNAS Pro"}}]
    }))
    monkeypatch.setattr(um, "UIDB_FILE", table)
    mon = um.UNASMonitor(mqtt_enabled=False)
    info = mon.get_device_info()
    assert info == {
        "name": "UniFi Network Attached Storage Pro", "shortname": "UNASPRO", "sysid": "ea51",
        "product": "UNAS Pro", "sku": "UNAS-Pro",
        "serial": "0cea14c0c079", "mac": "0CEA14C0C079", "hw_revision": 960263,
        "firmware": "6.0.11",
    }
    assert mon.get_device_info() == info
    assert calls == [("127.0.0.1", 11081)]


@pytest.mark.parametrize("connection", [
    fake_connection(status=401),
    fake_connection(error=OSError("connection refused")),
    fake_connection(body=json_body({"hardware": {}})),
])
def test_device_info_unavailable(monkeypatch, connection):
    monkeypatch.setattr(um.http.client, "HTTPConnection", connection)
    assert um.UNASMonitor(mqtt_enabled=False).get_device_info() is None


def interval_message(payload):
    return SimpleNamespace(topic=um.MONITOR_INTERVAL_TOPIC, payload=payload.encode())


@pytest.mark.parametrize(
    "payload,expected", [("10", 10), ("10.0", 10), ("4", 30), ("61", 30), ("x", 30)]
)
def test_monitor_interval(monitor, payload, expected):
    monitor.monitor_interval = 30
    monitor._on_message(None, None, interval_message(payload))
    assert monitor.monitor_interval == expected


def test_unchanged_interval_is_not_logged(monitor, caplog):
    caplog.set_level(logging.INFO)
    monitor.monitor_interval = 30
    monitor._on_message(None, None, interval_message("30"))
    assert "Monitor interval" not in caplog.text


def test_product_name_comes_from_the_unifi_device_table(tmp_path, monkeypatch):
    table = tmp_path / "uidb.json"
    table.write_text(json.dumps({"devices": [
        {"sysid": "ea63", "sku": "UNAS-Pro-8",
         "product": {"abbrev": "UNAS Pro 8", "name": "UNAS Pro 8"}},
        {"sysid": "ea51", "sku": "UNAS-Pro", "product": {"abbrev": "UNAS Pro", "name": "UNAS Pro"}},
    ]}))
    monkeypatch.setattr(um, "UIDB_FILE", table)
    assert um.product_info("ea51") == {"abbrev": "UNAS Pro", "name": "UNAS Pro", "sku": "UNAS-Pro"}
    assert um.product_info("ffff") == {}
    assert um.product_info(None) == {}


def test_missing_device_table_gives_no_product(tmp_path, monkeypatch):
    monkeypatch.setattr(um, "UIDB_FILE", tmp_path / "missing.json")
    assert um.product_info("ea51") == {}
