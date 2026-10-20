from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import posixpath
import shlex
from pathlib import Path
from typing import Optional

import asyncssh

from .const import HA_SSH_KEY_PATHS

_LOGGER = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).parent / "scripts"
SSH_CONNECT_TIMEOUT = 30
COMMAND_TIMEOUT = 30
DEPLOY_TIMEOUT = 120
KEEPALIVE_INTERVAL = 30
BUNDLE_DIR = "/root/unifi_unas"
UNIT_DIR = "/etc/systemd/system"
SERVICES = ("unas_monitor", "fan_control")
# left behind by 0.9.x
LEGACY_CLEANUP = (
    "if [ -f /root/unas_monitor.py ]; then "
    "pip3 uninstall -y --break-system-packages paho-mqtt >/dev/null 2>&1 "
    "|| pip3 uninstall -y paho-mqtt >/dev/null 2>&1; "
    "apt-get remove -y mosquitto-clients >/dev/null 2>&1; fi; "
    "rm -f /root/unas_monitor.py /root/fan_control.sh /root/fan_control.py /root/fan_control.json "
    "/root/fan_control.tmp /tmp/fan_control_state /tmp/fan_control_state.lock /tmp/fan_control_last_pwm "
    "/tmp/unas_hdd_temp /tmp/unas_hdd_temp.tmp /tmp/unas_monitor_interval "
    "/var/log/fan_control.log /var/log/fan_control.log.[1-9]"
)


class NASCommandError(Exception):
    pass


def outdated_files(files: dict[str, tuple[bytes, int]], remote_hashes: dict[str, str | None]) -> list[str]:
    return sorted(
        path for path, (content, _) in files.items()
        if remote_hashes.get(path) != hashlib.sha256(content).hexdigest()
    )


class SSHManager:
    def __init__(
            self,
            host: str,
            username: str,
            password: Optional[str] = None,
            ssh_key: Optional[str] = None,
            port: int = 22,
            mqtt_host: Optional[str] = None,
            mqtt_user: Optional[str] = None,
            mqtt_password: Optional[str] = None,
            mqtt_port: int = 1883,
            mqtt_tls: bool = False,
            mqtt_tls_insecure: bool = False,
            verify_host_key: bool = False,
            pinned_host_key: Optional[str] = None,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.ssh_key = ssh_key
        self.port = port
        self.mqtt_host = mqtt_host
        self.mqtt_user = mqtt_user
        self.mqtt_password = mqtt_password
        self.mqtt_port = mqtt_port
        self.mqtt_tls = mqtt_tls
        self.mqtt_tls_insecure = mqtt_tls_insecure
        self.verify_host_key = verify_host_key
        self.pinned_host_key = pinned_host_key
        # Populated on connect with the server's host key in known_hosts form
        # ("host keytype base64"), for trust-on-first-use pinning by the caller.
        self.server_host_key: Optional[str] = None
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self) -> None:
        # the lock only guards (re)connecting, commands run concurrently over the one connection
        async with self._lock:
            if self._closed:
                raise ConnectionAbortedError("SSH connection closed")
            if self._conn is not None and not self._conn.is_closed():
                return
            self._conn = None

            _LOGGER.debug("Establishing SSH connection to %s", self.host)

            client_keys = None
            if self.ssh_key:
                client_keys = [self.ssh_key]
            elif not self.password:
                for key_path in HA_SSH_KEY_PATHS:
                    if key_path.exists():
                        client_keys = [str(key_path)]
                        _LOGGER.debug("Using SSH key from %s", key_path)
                        break

            known_hosts = self._resolve_known_hosts()

            conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password if self.password else None,
                    client_keys=client_keys,
                    known_hosts=known_hosts,
                    keepalive_interval=KEEPALIVE_INTERVAL,
                    keepalive_count_max=3,
                ),
                timeout=SSH_CONNECT_TIMEOUT,
            )
            if self._closed:
                # disconnected (unloaded) while this was connecting, nothing would close it
                conn.close()
                raise ConnectionAbortedError("SSH connection closed")
            self._conn = conn
            self._capture_server_host_key()
            _LOGGER.debug("SSH connection established")

    def _resolve_known_hosts(self):
        """Choose the asyncssh known_hosts argument.

        When verification is enabled and a key has been pinned, verify
        against just that key -- asyncssh itself fails closed with
        HostKeyNotVerifiable on a mismatch or an unparseable pin, so there's
        no separate error path to handle here. Otherwise None: verification
        disabled (default behavior), and the permissive first connection
        that trust-on-first-use captures from.
        """
        if self.verify_host_key and self.pinned_host_key:
            return asyncssh.import_known_hosts(self.pinned_host_key)
        return None

    def _capture_server_host_key(self) -> None:
        """Record the server's host key in known_hosts form for TOFU pinning."""
        if self._conn is None:
            return
        try:
            key = self._conn.get_server_host_key()
            parts = key.export_public_key().decode().strip().split()
        except Exception as err:  # noqa: BLE001 - best-effort capture
            _LOGGER.warning(
                "Could not capture SSH host key for %s for TOFU pinning: %s",
                self.host,
                err,
            )
            return
        if len(parts) < 2:
            _LOGGER.warning(
                "Unexpected SSH host key format for %s; not pinning", self.host
            )
            return
        hostspec = self.host if self.port == 22 else f"[{self.host}]:{self.port}"
        self.server_host_key = f"{hostspec} {parts[0]} {parts[1]}"

    async def disconnect(self) -> None:
        # closing aborts commands still running instead of waiting for them
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()
            await conn.wait_closed()

    async def execute_command(
            self, command: str, timeout: float = COMMAND_TIMEOUT, input: str | None = None
    ) -> tuple[str, str]:
        await self.connect()
        conn = self._conn
        if conn is None:
            raise ConnectionError("SSH connection not established")
        try:
            result = await conn.run(command, check=False, timeout=timeout, input=input)
        except asyncssh.DisconnectError:
            if self._conn is conn:
                self._conn = None
            raise
        return getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or ""

    async def _helper(self, *args: str) -> dict:
        script = await asyncio.get_running_loop().run_in_executor(
            None, (SCRIPTS_DIR / "status.py").read_text
        )
        stdout, stderr = await self.execute_command(
            "python3 - " + " ".join(shlex.quote(arg) for arg in args), input=script
        )
        try:
            result = json.loads(stdout)
        except ValueError:
            raise NASCommandError(f"status helper failed: {(stderr or stdout).strip()[-300:]}") from None
        if not isinstance(result, dict):
            raise NASCommandError("status helper returned no result")
        if "error" in result:
            raise NASCommandError(result["error"])
        return result

    async def status(self, paths: list[str]) -> dict:
        return await self._helper("status", *paths)

    async def bundle_status(self, device_model: str, mqtt_root: str) -> tuple[dict, list[str]]:
        files = await self.bundle(device_model, mqtt_root)
        status = await self.status(sorted(files))
        return status, outdated_files(files, status.get("files") or {})

    async def run_backup(self, task_id: str) -> None:
        await self._helper("run-backup", task_id)

    async def set_backup_schedule(self, task_id: str, enabled: bool) -> None:
        await self._helper("set-schedule", task_id, "on" if enabled else "off")

    async def kick_native_fan_control(self) -> bool:
        # uhwd (native fan daemon) calculates PID values but doesn't write them to sysfs until it receives an
        # onFanProfileChanged event for some reason. Toggling the fan profile and back triggers this event, kicking uhwd
        # into active control mode.
        # Uses internal ustd APIs — returns False gracefully if they change.
        #
        # Mainly here to ensure that when fan control is given back to UNAS, it actually starts calculating new fan
        # values. It seems like it doesn't always do this and gets stuck at whatever PWM it was set to earlier.
        cmd = (
            "python3 -c '"
            "from ustd.tools.uhardware_fan import FanProfileManager; "
            "fpm = FanProfileManager(); "
            "cur = fpm.get_current_profile(); "
            "alt = \"quiet\" if cur != \"quiet\" else \"default\"; "
            "fpm.switch_profile(alt); "
            "fpm.switch_profile(cur); "
            "print(\"kicked\")' 2>&1"
        )
        stdout, _ = await self.execute_command(cmd)
        success = "kicked" in stdout
        if not success:
            _LOGGER.warning("Failed to kick native fan control: %s", stdout.strip())
        return success

    def _read_bundle(self, device_model: str, mqtt_root: str) -> dict[str, tuple[bytes, int]]:
        files: dict[str, tuple[bytes, int]] = {}
        for path in sorted(SCRIPTS_DIR.rglob("*")):
            rel = path.relative_to(SCRIPTS_DIR)
            if path.is_dir() or any(part.startswith((".", "__pycache__")) for part in rel.parts):
                continue
            if path.suffix == ".service":
                files[f"{UNIT_DIR}/{rel.as_posix()}"] = (path.read_bytes(), 0o644)
            else:
                files[f"{BUNDLE_DIR}/{rel.as_posix()}"] = (path.read_bytes(), 0o644)
        config = {
            "mqtt": {
                "host": self.mqtt_host,
                "port": int(self.mqtt_port),
                "user": self.mqtt_user,
                "password": self.mqtt_password,
                "tls": bool(self.mqtt_tls),
                "tls_insecure": bool(self.mqtt_tls_insecure),
            },
            "root": mqtt_root,
            "device_model": device_model,
        }
        files[f"{BUNDLE_DIR}/config.json"] = (json.dumps(config, indent=2).encode(), 0o600)
        return files

    async def bundle(self, device_model: str, mqtt_root: str) -> dict[str, tuple[bytes, int]]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._read_bundle, device_model, mqtt_root
        )

    async def deploy_scripts(self, device_model: str, mqtt_root: str) -> None:
        await self.connect()
        _LOGGER.info("Deploying scripts for device model: %s", device_model)

        try:
            files = await self.bundle(device_model, mqtt_root)
            await self.execute_command(f"rm -rf {BUNDLE_DIR}/vendor")
            await self._upload_files(files)

            await self.execute_command("systemctl daemon-reload")
            for service in SERVICES:
                await self.execute_command(f"systemctl enable {service}")
                await self.execute_command(f"systemctl restart {service}", timeout=DEPLOY_TIMEOUT)
            await self.execute_command(LEGACY_CLEANUP, timeout=DEPLOY_TIMEOUT)

            _LOGGER.info("Scripts deployed and services started")

        except Exception as err:
            _LOGGER.error("Failed to deploy scripts: %s", err)
            raise

    async def _upload_files(self, files: dict[str, tuple[bytes, int]]) -> None:
        conn = self._conn
        if conn is None:
            raise ConnectionError("SSH connection not established")
        async with asyncio.timeout(DEPLOY_TIMEOUT), conn.start_sftp_client() as sftp:
            await sftp.makedirs(BUNDLE_DIR, attrs=asyncssh.SFTPAttrs(permissions=0o700), exist_ok=True)
            await sftp.chmod(BUNDLE_DIR, 0o700)
            for remote_path, (content, mode) in files.items():
                await sftp.makedirs(posixpath.dirname(remote_path), exist_ok=True)
                attrs = asyncssh.SFTPAttrs(permissions=mode)
                async with sftp.open(remote_path, "wb", attrs=attrs) as remote_file:
                    await remote_file.write(content)
                await sftp.chmod(remote_path, mode)
