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
BUNDLE_DIR = "/root/unifi_unas"
# same hash as _read_bundle, run against the deployed files
REMOTE_HASH_SCRIPT = (
    "import hashlib,sys\n"
    "d=hashlib.sha256()\n"
    "for p in sys.argv[1:]:\n"
    "    d.update(p.encode()+b'\\0'+open(p,'rb').read()+b'\\0')\n"
    "print(d.hexdigest())"
)
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

    async def connect(self) -> None:
        async with self._lock:
            if self._conn:
                try:
                    await self._conn.run("true", timeout=2, check=False)
                    _LOGGER.debug("SSH connection reused")
                    return
                except asyncssh.Error:
                    _LOGGER.debug("SSH connection stale, reconnecting")
                except asyncio.TimeoutError:
                    _LOGGER.debug("SSH connection timed out, reconnecting")
                try:
                    self._conn.close()
                    await self._conn.wait_closed()
                except asyncssh.Error:
                    pass
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

            self._conn = await asyncio.wait_for(
                asyncssh.connect(
                    self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password if self.password else None,
                    client_keys=client_keys,
                    known_hosts=known_hosts,
                ),
                timeout=SSH_CONNECT_TIMEOUT,
            )
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
        async with self._lock:
            if self._conn:
                self._conn.close()
                await self._conn.wait_closed()
                self._conn = None

    async def execute_command(self, command: str) -> tuple[str, str]:
        await self.connect()
        async with self._lock:
            if self._conn is None:
                raise ConnectionError("SSH connection not established")
            result = await self._conn.run(command, check=False)
        return getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or ""

    async def scripts_installed(self, device_model: str, mqtt_root: str) -> bool:
        files, digest = await self._bundle(device_model, mqtt_root)
        paths = " ".join(shlex.quote(path) for path in sorted(files))
        stdout, _ = await self.execute_command(
            f"python3 -c {shlex.quote(REMOTE_HASH_SCRIPT)} {paths} 2>/dev/null"
        )
        installed = stdout.strip() == digest
        _LOGGER.debug("Scripts installed and current: %s", installed)
        return installed

    async def service_running(self, service_name: str) -> bool:
        safe_name = shlex.quote(service_name)
        stdout, _ = await self.execute_command(
            f"systemctl is-active {safe_name} 2>/dev/null || echo 'inactive'"
        )
        running = stdout.strip() == "active"
        _LOGGER.debug("Service %s running: %s", service_name, running)
        return running

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

    def _read_bundle(self, device_model: str, mqtt_root: str) -> tuple[dict[str, tuple[bytes, int]], str]:
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

        digest = hashlib.sha256()
        for remote, (content, _) in sorted(files.items()):
            digest.update(remote.encode() + b"\0" + content + b"\0")
        return files, digest.hexdigest()

    async def _bundle(self, device_model: str, mqtt_root: str) -> tuple[dict[str, tuple[bytes, int]], str]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._read_bundle, device_model, mqtt_root
        )

    async def deploy_scripts(self, device_model: str, mqtt_root: str) -> None:
        await self.connect()
        _LOGGER.info("Deploying scripts for device model: %s", device_model)

        try:
            files, _ = await self._bundle(device_model, mqtt_root)
            await self.execute_command(f"rm -rf {BUNDLE_DIR}/vendor")
            await self._upload_files(files)

            await self.execute_command("systemctl daemon-reload")
            for service in SERVICES:
                await self.execute_command(f"systemctl enable {service}")
                await self.execute_command(f"systemctl restart {service}")
            await self.execute_command(LEGACY_CLEANUP)

            _LOGGER.info("Scripts deployed and services started")

        except Exception as err:
            _LOGGER.error("Failed to deploy scripts: %s", err)
            raise

    async def _upload_files(self, files: dict[str, tuple[bytes, int]]) -> None:
        async with self._lock:
            if self._conn is None:
                raise ConnectionError("SSH connection not established")
            async with self._conn.start_sftp_client() as sftp:
                await sftp.makedirs(BUNDLE_DIR, attrs=asyncssh.SFTPAttrs(permissions=0o700), exist_ok=True)
                await sftp.chmod(BUNDLE_DIR, 0o700)
                for remote_path, (content, mode) in files.items():
                    await sftp.makedirs(posixpath.dirname(remote_path), exist_ok=True)
                    attrs = asyncssh.SFTPAttrs(permissions=mode)
                    async with sftp.open(remote_path, "wb", attrs=attrs) as remote_file:
                        await remote_file.write(content)
                    await sftp.chmod(remote_path, mode)

    async def execute_backup_api(self, method: str, endpoint: str) -> dict:
        cmd = f'''curl -s -X {method} "http://localhost:16080{endpoint}" \
            -H "X-UserId: $(jq -r '.[0].id' /data/unifi-core/config/cache/users.json)" \
            -H "X-UserRole: owner" \
            -H "X-UserAccessMask: 114654" \
            -H "X-UserPermissionMask: 16382"'''
        stdout, stderr = await self.execute_command(cmd)
        if not stdout.strip():
            _LOGGER.debug("Backup API returned empty response for %s %s", method, endpoint)
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as err:
            _LOGGER.warning("Failed to parse backup API response: %s", err)
            return {}

    async def update_backup_task(self, task_id: str, updates: dict) -> dict:
        payload = json.dumps(updates)
        escaped_payload = shlex.quote(payload)
        cmd = f'''curl -s -X PATCH "http://localhost:16080/api/v1/remote-backup/tasks/{task_id}" \
            -H "Content-Type: application/json" \
            -H "X-UserId: $(jq -r '.[0].id' /data/unifi-core/config/cache/users.json)" \
            -H "X-UserRole: owner" \
            -H "X-UserAccessMask: 114654" \
            -H "X-UserPermissionMask: 16382" \
            -d {escaped_payload}'''
        stdout, stderr = await self.execute_command(cmd)
        if not stdout.strip():
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as err:
            _LOGGER.warning("Failed to parse backup API update response: %s", err)
            return {}
