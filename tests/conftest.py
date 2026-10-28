import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import custom_components  # noqa: E402

# pytest-homeassistant-custom-component imports its own custom_components package first
if str(ROOT / "custom_components") not in custom_components.__path__:
    custom_components.__path__.append(str(ROOT / "custom_components"))

import pytest  # noqa: E402
from homeassistant.util import dt as dt_util  # noqa: E402
from pytest_homeassistant_custom_component.common import (  # noqa: E402
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
)

DOMAIN = "unifi_unas"
ENTRY_ID = "01KM6KMCMW1YRG522SPA9G1566"
ROOT_TOPIC = "unas/01KM6KMC"

TASK = {
    "id": "198597d5-df3e-4a71-8a96-f9f816045b25",
    "name": "B2 backup",
    "remote": {"type": "b2"},
    "destinationDir": "bucket/x",
    "sourceDirs": ["/a"],
    "schedule": {"enable": True, "firstRunTime": "03:00", "weekdays": "*"},
    "lastTaskRun": {
        "status": "idle", "startedAt": "2026-10-17T00:00:00Z", "finishedAt": "2026-10-17T00:10:00Z"
    },
    "nextBackup": "2026-10-19T00:00:00Z",
}


class FakeSSH:
    instances = []

    def __init__(self, *args, **kwargs):
        self.error = None
        self.deploy_error = None
        self.deploy_gate = None
        self.deploy_fixes = True
        self.outdated = []
        self.deploys = []
        self.commands = []
        self.services = {"unas_monitor": "active", "fan_control": "active"}
        self.backup_tasks = [TASK]
        self.server_host_key = None
        self.pinned_host_key = kwargs.get("pinned_host_key")
        self.kwargs = kwargs
        FakeSSH.instances.append(self)

    async def bundle_status(self, device_model, mqtt_root):
        if self.error:
            raise self.error
        status = {"services": dict(self.services), "files": {}, "backup_tasks": self.backup_tasks}
        return status, list(self.outdated)

    async def deploy_scripts(self, device_model, mqtt_root, only=None):
        self.deploys.append(only)
        if self.deploy_gate:
            await self.deploy_gate.wait()
        if self.deploy_error:
            raise self.deploy_error
        if self.deploy_fixes:
            self.outdated = []
        return ["unas_monitor", "fan_control"]

    async def execute_command(self, command, timeout=None, input=None):
        self.commands.append(command)
        return "", ""

    async def disconnect(self):
        pass

    async def kick_native_fan_control(self):
        return True

    async def run_backup(self, task_id):
        self.commands.append(f"run-backup {task_id}")

    async def set_backup_schedule(self, task_id, enabled):
        self.commands.append(f"set-schedule {task_id} {enabled}")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_mqtt_misc_timer():
    # the mocked MQTT client never cancels this timer, it would mask our own lingering timers
    with patch("homeassistant.components.mqtt.client.MQTT._async_start_misc_periodic"):
        yield


@pytest.fixture
def ssh():
    FakeSSH.instances.clear()
    with (
        patch("custom_components.unifi_unas.SSHManager", FakeSSH),
        patch("custom_components.unifi_unas.coordinator.RETAINED_WAIT", 0),
        patch("custom_components.unifi_unas.stale_topics.SCAN_DELAY", 0),
        patch("custom_components.unifi_unas.stale_topics.COLLECT_TIME", 0),
    ):
        yield FakeSSH


def entry_data(**overrides):
    data = {
        "host": "192.168.1.25",
        "username": "root",
        "mqtt_host": "192.168.1.111",
        "mqtt_user": "unas",
        "mqtt_password": "x",
        "mqtt_tls": False,
        "device_model": "UNAS_PRO",
        "device_name": "UNAS",
        "scan_interval": 5,
    }
    data.update(overrides)
    return data


async def setup_entry(hass, **overrides):
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data(**overrides), entry_id=ENTRY_ID, version=2,
        unique_id="192.168.1.25", title="UNAS (192.168.1.25)",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def settle(hass):
    # past the 0.5 s push debounce
    await hass.async_block_till_done(wait_background_tasks=True)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done(wait_background_tasks=True)


def fire(hass, topic, payload, retain=False):
    # a broker sets the retain flag only when replaying to a new subscriber
    async_fire_mqtt_message(hass, f"{ROOT_TOPIC}/{topic}", str(payload), retain=retain)


def fire_monitor_cycle(hass, bays=("1", "2"), shares=("plex",), pools=("1",), inventory=True):
    fire(hass, "availability", "online")
    for key, value in {"cpu_temp": 50, "fan_speed": 76, "memory_total": 7900, "uptime": 1000,
                       "machine_id": "abc"}.items():
        fire(hass, f"system/{key}", value)
    for bay in bays:
        for key, value in {"temperature": 43, "model": "ST18000NM000J", "serial": f"ZR{bay}",
                           "status": "Optimal"}.items():
            fire(hass, f"hdd/{bay}/{key}", value)
    for pool in pools:
        for key, value in {"usage": 60, "size": 71966, "status": "fullyOperational"}.items():
            fire(hass, f"pool/{pool}/{key}", value)
    for share in shares:
        for key, value in {"usage": 39107.2, "quota": -1, "pool": "1"}.items():
            fire(hass, f"share/{share}/{key}", value)
    if inventory:
        fire(hass, "inventory", json.dumps({
            "bays": list(bays), "nvme": [], "pools": [int(pool) for pool in pools],
            "shares": list(shares),
        } if inventory is True else inventory))


def fire_fan_settings(hass, mode="target_temp"):
    settings = {
        "mode": mode, "curve/min_temp": 40, "curve/max_temp": 50, "curve/min_fan": 51,
        "curve/max_fan": 255, "curve/target_temp": 43, "curve/temp_metric": "avg",
        "curve/response_speed": "balanced",
    }
    for key, value in settings.items():
        fire(hass, f"control/fan/{key}", value)
