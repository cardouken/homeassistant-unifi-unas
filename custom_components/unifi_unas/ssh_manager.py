from __future__ import annotations

import asyncio
import json
import logging
import shlex
from pathlib import Path
from typing import Optional

import aiofiles
import asyncssh

from .const import ENV_MQTT_SECRET_KEYS, HA_SSH_KEY_PATHS, UNAS_ENV_FILE

_LOGGER = logging.getLogger(__name__)

SCRIPTS_DIR = Path(__file__).parent / "scripts"
SSH_CONNECT_TIMEOUT = 30


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
            credentials_file: Optional[str] = None,
            min_pwm_floor: int = 0,
            verify_host_key: bool = False,
            known_hosts_path: Optional[str] = None,
            pinned_host_key: Optional[str] = None,
            enable_monitor: bool = True,
            enable_fan_control: bool = True,
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
        self.credentials_file = credentials_file
        self.min_pwm_floor = min_pwm_floor
        self.verify_host_key = verify_host_key
        self.known_hosts_path = known_hosts_path
        self.pinned_host_key = pinned_host_key
        self.enable_monitor = enable_monitor
        self.enable_fan_control = enable_fan_control
        # Captured on connect: the server's host key in "host keytype base64"
        # known_hosts form, for trust-on-first-use pinning by the caller.
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
        """Determine the asyncssh known_hosts argument.

        - A user-supplied known_hosts file path takes precedence.
        - Otherwise, if host-key verification is enabled and a key has already
          been pinned, verify against just that key.
        - Otherwise return None (verification disabled), preserving the prior
          default behavior and allowing trust-on-first-use capture.
        """
        if self.known_hosts_path:
            return self.known_hosts_path
        if self.verify_host_key and self.pinned_host_key:
            try:
                return asyncssh.import_known_hosts(self.pinned_host_key)
            except (ValueError, asyncssh.Error) as err:
                _LOGGER.warning("Could not parse pinned host key, skipping verification: %s", err)
                return None
        return None

    def _capture_server_host_key(self) -> None:
        """Record the server's host key in known_hosts form for TOFU pinning."""
        if self._conn is None:
            return
        try:
            key = self._conn.get_server_host_key()
        except Exception:  # noqa: BLE001 - best-effort capture
            key = None
        if key is None:
            return
        try:
            parts = key.export_public_key().decode().strip().split()
        except Exception:  # noqa: BLE001 - best-effort capture
            return
        if len(parts) < 2:
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

    async def scripts_installed(self) -> bool:
        checks = []
        if self.enable_monitor:
            checks.append("test -f /root/unas_monitor.py")
        if self.enable_fan_control:
            checks.append("test -f /root/fan_control.sh")
        checks.append("python3 -c 'import paho.mqtt.client' 2>/dev/null")
        checks.append("which mosquitto_sub >/dev/null 2>&1")
        cmd = " && ".join(checks) + " && echo 'yes' || echo 'no'"
        stdout, _ = await self.execute_command(cmd)
        installed = stdout.strip() == "yes"
        _LOGGER.debug("Scripts installed: %s", installed)
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

    def _replace_mqtt_credentials(
        self, script: str, mqtt_root: str, skip_keys: tuple[str, ...] = ()
    ) -> str:
        replacements = {
            "MQTT_HOST": self.mqtt_host,
            "MQTT_USER": self.mqtt_user,
            "MQTT_PASS": self.mqtt_password,
            "MQTT_ROOT": mqtt_root,
            "MQTT_PORT": str(int(self.mqtt_port)),
            "MQTT_TLS": "true" if self.mqtt_tls else "false",
            "MQTT_TLS_INSECURE": "true" if self.mqtt_tls_insecure else "false",
        }

        for key, value in replacements.items():
            # Keys delivered via the on-device env file are left as their
            # "REPLACE_ME" placeholder so the secret is never inlined into the
            # deployed script body; the environment supplies the real value.
            if key in skip_keys:
                continue
            # unas_monitor.py — escape for Python string literal
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            script = script.replace(f'{key} = "REPLACE_ME"', f'{key} = "{escaped}"')
            # fan_control.sh
            script = script.replace(f'{key}="REPLACE_ME"', f'{key}={shlex.quote(value)}')

        return script

    async def deploy_scripts(self, device_model: str, mqtt_root: str) -> None:
        await self.connect()
        _LOGGER.info("Deploying scripts for device model: %s", device_model)

        try:
            async with aiofiles.open(SCRIPTS_DIR / "unas_monitor.py", "r") as f:
                monitor_script = await f.read()
            async with aiofiles.open(SCRIPTS_DIR / "unas_monitor.service", "r") as f:
                monitor_service = await f.read()
            async with aiofiles.open(SCRIPTS_DIR / "fan_control.sh", "r") as f:
                fan_control_script = await f.read()
            async with aiofiles.open(SCRIPTS_DIR / "fan_control.service", "r") as f:
                fan_control_service = await f.read()

            # MQTT credential delivery.
            #
            # Default (no credentials_file): substitute the values directly into
            # the script bodies, exactly as before.
            #
            # Opt-in (credentials_file set): deliver the MQTT settings through a
            # root-only (chmod 600) EnvironmentFile on the NAS, referenced by the
            # systemd units via `EnvironmentFile=`. The scripts read these from
            # the environment, so the sensitive values (username/password) are
            # never inlined into the deployed script bodies.
            skip_keys: tuple[str, ...] = ()
            if self.credentials_file:
                await self._deploy_credentials_file()
                skip_keys = ENV_MQTT_SECRET_KEYS
            else:
                # Remove any env file left over from a previous credentials_file
                # deployment so it can't override the freshly inlined values.
                await self.execute_command(f"rm -f {shlex.quote(UNAS_ENV_FILE)}")

            if self.mqtt_host and self.mqtt_user and self.mqtt_password:
                monitor_script = self._replace_mqtt_credentials(
                    monitor_script, mqtt_root, skip_keys=skip_keys
                )
                fan_control_script = self._replace_mqtt_credentials(
                    fan_control_script, mqtt_root, skip_keys=skip_keys
                )

            escaped_model = device_model.replace("\\", "\\\\").replace('"', '\\"')
            monitor_script = monitor_script.replace(
                'DEVICE_MODEL = "UNAS_PRO"', f'DEVICE_MODEL = "{escaped_model}"'
            )

            fan_control_script = fan_control_script.replace(
                'MIN_PWM_FLOOR="0"', f'MIN_PWM_FLOOR="{int(self.min_pwm_floor)}"'
            )

            await self.execute_command("apt-get update && apt-get install -y mosquitto-clients python3-pip")
            await self.execute_command("pip3 install --ignore-installed paho-mqtt==2.1.0")

            # Deploy / enable each on-device service independently so a
            # monitoring-only install can leave the fans fully UNAS-managed
            # (and vice-versa). A disabled service is stopped and removed.
            if self.enable_monitor:
                await self._upload_file("/root/unas_monitor.py", monitor_script, executable=True)
                await self._upload_file("/etc/systemd/system/unas_monitor.service", monitor_service)
            else:
                await self.execute_command("systemctl stop unas_monitor 2>/dev/null || true")
                await self.execute_command("systemctl disable unas_monitor 2>/dev/null || true")
                await self.execute_command(
                    "rm -f /root/unas_monitor.py /etc/systemd/system/unas_monitor.service"
                )

            if self.enable_fan_control:
                await self._upload_file("/root/fan_control.sh", fan_control_script, executable=True)
                await self._upload_file(
                    "/etc/systemd/system/fan_control.service", fan_control_service
                )
            else:
                await self.execute_command("systemctl stop fan_control 2>/dev/null || true")
                await self.execute_command("systemctl disable fan_control 2>/dev/null || true")
                await self.execute_command(
                    "rm -f /root/fan_control.sh /etc/systemd/system/fan_control.service"
                )
                # Hand the fans back to firmware/automatic control.
                await self.execute_command(
                    'for e in /sys/class/hwmon/hwmon0/pwm*_enable; do '
                    '[ -w "$e" ] && echo 2 > "$e"; done || true'
                )

            await self.execute_command("systemctl daemon-reload")
            if self.enable_monitor:
                await self.execute_command("systemctl enable unas_monitor")
                await self.execute_command("systemctl restart unas_monitor")
            if self.enable_fan_control:
                await self.execute_command("systemctl enable fan_control")
                await self.execute_command("systemctl restart fan_control")

            _LOGGER.info("Scripts deployed and services started")

        except Exception as err:
            _LOGGER.error("Failed to deploy scripts: %s", err)
            raise

    async def _upload_file(self, remote_path: str, content: str, executable: bool = False) -> None:
        async with self._lock:
            if self._conn is None:
                raise ConnectionError("SSH connection not established")
            async with self._conn.start_sftp_client() as sftp:
                async with sftp.open(remote_path, "w") as remote_file:
                    await remote_file.write(content)

        if executable:
            safe_path = shlex.quote(remote_path)
            await self.execute_command(f"chmod +x {safe_path}")

    async def _deploy_credentials_file(self) -> None:
        """Upload the operator-supplied MQTT env file to the NAS (chmod 600).

        The file at ``self.credentials_file`` lives on the Home Assistant host
        and is expected to be a systemd EnvironmentFile defining at least
        ``MQTT_USER`` and ``MQTT_PASS`` (and optionally any other ``MQTT_*``
        settings). It is written to a root-only file on the NAS that the
        systemd units load via ``EnvironmentFile=``.
        """
        cred_path = Path(self.credentials_file)
        if not cred_path.is_file():
            raise FileNotFoundError(
                f"credentials_file not found on the Home Assistant host: {self.credentials_file}"
            )
        async with aiofiles.open(cred_path, "r") as f:
            env_content = await f.read()

        await self._upload_file(UNAS_ENV_FILE, env_content)
        await self.execute_command(f"chmod 600 {shlex.quote(UNAS_ENV_FILE)}")
        _LOGGER.info("Deployed MQTT credentials via EnvironmentFile %s", UNAS_ENV_FILE)

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
