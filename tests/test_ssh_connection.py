import asyncio
import json
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import asyncssh
import pytest

from custom_components.unifi_unas import ssh_manager
from custom_components.unifi_unas.ssh_manager import NASCommandError, SSHManager

PASSWORD = "secret"


class FakeNAS:
    # a real SSH server on localhost: SFTP into a temp dir, commands recorded or run locally
    def __init__(self, root: Path) -> None:
        self.root = root
        self.host_key = asyncssh.generate_private_key("ssh-ed25519")
        self.client_key = asyncssh.generate_private_key("ssh-ed25519")
        self.commands: list[str] = []
        self.replies: dict[str, str] = {}
        self.server = None
        self.port = 0

    async def start(self) -> None:
        nas = self

        class Server(asyncssh.SSHServer):
            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == "root" and password == PASSWORD

        authorized = asyncssh.import_authorized_keys(
            self.client_key.export_public_key().decode()
        )
        self.server = await asyncssh.create_server(
            Server, "127.0.0.1", 0,
            server_host_keys=[self.host_key],
            authorized_client_keys=authorized,
            process_factory=nas.handle,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=str(nas.root)),
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, process) -> None:
        command = process.command
        self.commands.append(command)
        if command.startswith("python3 - "):
            script = await process.stdin.read()
            result = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-", *shlex.split(command)[2:]],
                input=script, capture_output=True, text=True,
            )
            process.stdout.write(result.stdout)
            process.stderr.write(result.stderr)
            process.exit(result.returncode)
            return
        process.stdout.write(self.replies.get(command, ""))
        process.exit(0)

    def known_host(self) -> str:
        key = self.host_key.export_public_key().decode().split()
        return f"[127.0.0.1]:{self.port} {key[0]} {key[1]}"

    def manager(self, **kwargs) -> SSHManager:
        kwargs.setdefault("password", PASSWORD)
        return SSHManager(
            "127.0.0.1", "root", port=self.port, mqtt_host="192.0.2.2", mqtt_user="unas",
            mqtt_password="x", **kwargs,
        )


@pytest.fixture
async def nas(tmp_path, socket_enabled, monkeypatch):
    # asyncssh reads ~/.ssh/config and the agent, keep the developer's out of it
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    nas = FakeNAS(tmp_path)
    await nas.start()
    yield nas
    nas.server.close()
    await nas.server.wait_closed()


async def test_password_login_captures_the_host_key(nas):
    mgr = nas.manager()
    await mgr.connect()
    conn = mgr._conn
    await mgr.connect()
    assert mgr._conn is conn
    assert mgr.server_host_key == nas.known_host()
    await mgr.disconnect()
    assert mgr._conn is None


async def test_key_login_from_the_ha_ssh_dir(nas, tmp_path, monkeypatch):
    key_file = tmp_path / "id_ed25519"
    key_file.write_bytes(nas.client_key.export_private_key())
    monkeypatch.setattr(ssh_manager, "HA_SSH_KEY_PATHS", [tmp_path / "id_rsa", key_file])
    mgr = nas.manager(password=None)
    assert await mgr.execute_command("true") == ("", "")
    await mgr.disconnect()


async def test_explicit_key_login(nas, tmp_path):
    key_file = tmp_path / "key"
    key_file.write_bytes(nas.client_key.export_private_key())
    mgr = nas.manager(password=None, ssh_key=str(key_file))
    await mgr.connect()
    await mgr.disconnect()


async def test_wrong_password_is_rejected(nas):
    with pytest.raises(asyncssh.PermissionDenied):
        await nas.manager(password="wrong").connect()


async def test_pinned_host_key(nas):
    mgr = nas.manager(verify_host_key=True, pinned_host_key=nas.known_host())
    await mgr.connect()
    await mgr.disconnect()
    other = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().split()
    pin = f"[127.0.0.1]:{nas.port} {other[0]} {other[1]}"
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        await nas.manager(verify_host_key=True, pinned_host_key=pin).connect()


async def test_commands_reconnect_after_the_connection_drops(nas):
    nas.replies["uptime"] = "up 3 days\n"
    mgr = nas.manager()
    assert await mgr.execute_command("uptime") == ("up 3 days\n", "")
    mgr._conn.close()
    await mgr._conn.wait_closed()
    assert await mgr.execute_command("uptime") == ("up 3 days\n", "")
    await mgr.disconnect()


async def test_no_connection_outlives_a_disconnect(nas):
    # unload disconnects while a status call may still be connecting
    mgr = nas.manager()
    connecting = asyncio.ensure_future(mgr.connect())
    await asyncio.sleep(0)
    await mgr.disconnect()
    with pytest.raises(ConnectionAbortedError):
        await connecting
    assert mgr._conn is None
    with pytest.raises(ConnectionAbortedError):
        await mgr.execute_command("uptime")


async def test_status_runs_the_helper_from_stdin(nas):
    mgr = nas.manager()
    result = await mgr.status(["/nonexistent/file"])
    assert result["files"] == {"/nonexistent/file": None}
    assert set(result["services"]) == {"unas_monitor", "fan_control"}
    assert nas.commands == ["python3 - status /nonexistent/file"]
    await mgr.disconnect()


async def test_helper_errors(nas, monkeypatch):
    mgr = nas.manager()
    # no UniFi OS here, so the identity lookup fails on the NAS side
    with pytest.raises(NASCommandError, match="/api/system"):
        await mgr.identity()
    for output, message in (("not json", "status helper failed"), ("[]", "no result")):
        monkeypatch.setattr(mgr, "execute_command", lambda *a, out=output, **kw: _reply(out))
        with pytest.raises(NASCommandError, match=message):
            await mgr.run_backup("t1")
    await mgr.disconnect()


async def _reply(stdout):
    return stdout, ""


async def test_backup_commands(nas, monkeypatch):
    mgr = nas.manager()
    calls = []

    async def helper(*args):
        calls.append(args)
        return {}

    monkeypatch.setattr(mgr, "_helper", helper)
    await mgr.run_backup("t1")
    await mgr.set_backup_schedule("t1", True)
    await mgr.set_backup_schedule("t1", False)
    assert calls == [
        ("run-backup", "t1"), ("set-schedule", "t1", "on"), ("set-schedule", "t1", "off")
    ]


async def test_kick_native_fan_control(nas):
    mgr = nas.manager()
    assert await mgr.kick_native_fan_control() is False
    nas.replies[nas.commands[-1]] = "kicked\n"
    assert await mgr.kick_native_fan_control() is True
    await mgr.disconnect()


@pytest.fixture
def bundle_paths(nas, monkeypatch):
    # the SFTP server is chrooted, so the bundle lands under the temp dir at the real paths
    monkeypatch.setattr(ssh_manager, "LEGACY_CLEANUP", "cleanup")
    return lambda path: nas.root / path.lstrip("/")


async def test_full_deploy(nas, bundle_paths):
    mgr = nas.manager()
    (nas.root / "root/unifi_unas").mkdir(parents=True)
    (nas.root / "root/unifi_unas").chmod(0o755)
    assert await mgr.deploy_scripts("UNAS_PRO", "unas/abc") == ["unas_monitor", "fan_control"]
    files = await mgr.bundle("UNAS_PRO", "unas/abc")
    for path, (content, mode) in files.items():
        assert bundle_paths(path).read_bytes() == content
        assert stat.S_IMODE(bundle_paths(path).stat().st_mode) == mode
    assert stat.S_IMODE((nas.root / "root/unifi_unas").stat().st_mode) == 0o700
    config = json.loads(bundle_paths("/root/unifi_unas/config.json").read_text())
    assert config["root"] == "unas/abc"
    assert nas.commands == [
        "rm -rf /root/unifi_unas/vendor/",
        "systemctl daemon-reload",
        "systemctl enable unas_monitor",
        "systemctl restart unas_monitor",
        "systemctl enable fan_control",
        "systemctl restart fan_control",
        "cleanup",
    ]
    await mgr.disconnect()


async def test_selective_deploy(nas, bundle_paths):
    mgr = nas.manager()
    script = "/root/unifi_unas/fan_control.py"
    assert await mgr.deploy_scripts("UNAS_PRO", "unas/abc", [script]) == ["fan_control"]
    assert bundle_paths(script).exists()
    assert not bundle_paths("/root/unifi_unas/unas_monitor.py").exists()
    assert nas.commands == [
        "systemctl enable fan_control", "systemctl restart fan_control", "cleanup"
    ]
    await mgr.disconnect()


async def test_vendor_change_replaces_the_whole_package(nas, bundle_paths):
    mgr = nas.manager()
    files = await mgr.bundle("UNAS_PRO", "unas/abc")
    vendor = sorted(path for path in files if "/vendor/" in path)
    assert await mgr.deploy_scripts("UNAS_PRO", "unas/abc", vendor[:1]) == [
        "unas_monitor", "fan_control"
    ]
    assert all(bundle_paths(path).exists() for path in vendor)
    assert nas.commands[0] == "rm -rf /root/unifi_unas/vendor/"
    await mgr.disconnect()


async def test_unit_change_reloads_systemd(nas, bundle_paths):
    mgr = nas.manager()
    unit = "/etc/systemd/system/unas_monitor.service"
    assert await mgr.deploy_scripts("UNAS_PRO", "unas/abc", [unit]) == ["unas_monitor"]
    assert nas.commands[0] == "systemctl daemon-reload"
    await mgr.disconnect()


async def test_nothing_to_deploy_does_not_connect(nas):
    mgr = nas.manager()
    assert await mgr.deploy_scripts("UNAS_PRO", "unas/abc", []) == []
    assert mgr._conn is None and nas.commands == []
