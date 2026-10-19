import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from custom_components.unifi_unas import ssh_manager
from custom_components.unifi_unas.ssh_manager import SSHManager

CONFIG = "/root/unifi_unas/config.json"


def manager():
    return SSHManager(
        "192.0.2.1", "root", mqtt_host="192.0.2.2", mqtt_user="unas", mqtt_password='p"$x\\', mqtt_port=1883
    )


@pytest.fixture
def deployed(tmp_path, monkeypatch):
    # the bundle laid out in a temp dir, the hash check command then runs against it locally
    monkeypatch.setattr(ssh_manager, "BUNDLE_DIR", str(tmp_path / "unifi_unas"))
    monkeypatch.setattr(ssh_manager, "UNIT_DIR", str(tmp_path / "systemd"))
    mgr = manager()
    files, _ = mgr._read_bundle("UNAS_PRO", "unas/abc")
    for path, (content, _) in files.items():
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(content)

    async def run_locally(command):
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result.stdout, result.stderr

    mgr.execute_command = run_locally
    return mgr, files


def test_bundle_layout():
    files, _ = manager()._read_bundle("UNAS_PRO", "unas/abc")
    assert {path for path in files if path.startswith("/etc/")} == {
        "/etc/systemd/system/fan_control.service",
        "/etc/systemd/system/unas_monitor.service",
    }
    assert "/root/unifi_unas/vendor/paho/mqtt/client.py" in files
    assert not any("__pycache__" in path or "/." in path for path in files)
    assert files[CONFIG][1] == 0o600
    assert all(mode == 0o644 for path, (_, mode) in files.items() if path != CONFIG)


def test_config_keeps_credentials_verbatim():
    files, _ = manager()._read_bundle("UNAS_PRO_8", "unas/abc")
    assert json.loads(files[CONFIG][0]) == {
        "mqtt": {
            "host": "192.0.2.2", "port": 1883, "user": "unas", "password": 'p"$x\\',
            "tls": False, "tls_insecure": False,
        },
        "root": "unas/abc",
        "device_model": "UNAS_PRO_8",
    }


def test_bundle_hash_follows_the_config():
    mgr = manager()
    _, first = mgr._read_bundle("UNAS_PRO", "unas/abc")
    assert mgr._read_bundle("UNAS_PRO", "unas/abc")[1] == first
    assert mgr._read_bundle("UNAS_PRO", "unas/def")[1] != first
    assert mgr._read_bundle("UNAS_PRO_8", "unas/abc")[1] != first


def test_installed_bundle_is_recognised(deployed):
    mgr, _ = deployed
    assert asyncio.run(mgr.scripts_installed("UNAS_PRO", "unas/abc"))


def test_edited_file_is_not_installed(deployed):
    mgr, files = deployed
    path = Path(next(path for path in files if path.endswith("fan_control.py")))
    path.write_text(path.read_text() + "# edited\n")
    assert not asyncio.run(mgr.scripts_installed("UNAS_PRO", "unas/abc"))


def test_missing_file_is_not_installed(deployed):
    mgr, files = deployed
    Path(next(path for path in files if path.endswith(".service"))).unlink()
    assert not asyncio.run(mgr.scripts_installed("UNAS_PRO", "unas/abc"))


def test_other_config_is_not_installed(deployed):
    mgr, _ = deployed
    assert not asyncio.run(mgr.scripts_installed("UNAS_PRO", "unas/other"))
