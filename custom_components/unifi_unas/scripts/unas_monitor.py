#!/usr/bin/env python3

import time
import subprocess
import logging
import json
import http.client
import re
from pathlib import Path

from unas_common import connect, load_config, mqtt_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

CONFIG = load_config()
MQTT_ROOT = CONFIG.get("root", "unas")
DEFAULT_MONITOR_INTERVAL = 30
MQTT_AVAILABILITY = f"{MQTT_ROOT}/availability"
MQTT_SYSTEM = f"{MQTT_ROOT}/system"
MQTT_HDD = f"{MQTT_ROOT}/hdd"
MQTT_NVME = f"{MQTT_ROOT}/nvme"
MQTT_POOL = f"{MQTT_ROOT}/pool"
MQTT_SMB = f"{MQTT_ROOT}/smb"
MQTT_NFS = f"{MQTT_ROOT}/nfs"
MQTT_SHARE = f"{MQTT_ROOT}/share"
MQTT_CONTROL = f"{MQTT_ROOT}/control"
MONITOR_INTERVAL_TOPIC = f"{MQTT_CONTROL}/monitor_interval"
# HA drops MQTT keys it has not seen for 120s, so unchanged values are
# re-asserted at least this often; changed values publish immediately.
REPUBLISH_INTERVAL = 60
SHARE_CACHE_TTL = 300
DEVICE_INFO_TTL = 3600
HEARTBEAT_INTERVAL = 600
_UNSET = object()

DEVICE_MODEL = CONFIG.get("device_model", "UNAS_PRO")

BAY_MAPPINGS = {
    "UNAS_PRO": {
        "1": "6",
        "3": "7",
        "4": "3",
        "5": "5",
        "6": "2",
        "7": "4",
        "8": "1"
    },
    "UNAS_PRO_8": {
        "1": "1",
        "2": "2",
        "3": "3",
        "4": "4",
        "5": "5",
        "6": "6",
        "7": "7",
        "8": "8"
    },
    "UNAS_PRO_4": {
        "1": "1",
        "2": "2",
        "3": "3",
        "4": "4"
    },
    # UNAS 4 confirmed on reddit
    "UNAS_4": {
        "1": "1",
        "2": "2",
        "3": "3",
        "4": "4"
    },
    "UNAS_2": {
        "1": "1",
        "2": "2"
    },
    # UNVR - user-reported mapping from issue #11
    "UNVR": {
        "1": "3",
        "3": "4",
        "5": "2",
        "7": "1"
    },
    # UNVR Instant - user-reported mapping from pr #35
    "UNVR_INSTANT": {
        "1": "1"
    },
    # UNVR Pro - user-reported mapping from issue #11
    "UNVR_PRO": {
        "1": "6",
        "3": "7",
        "4": "3",
        "5": "5",
        "6": "2",
        "7": "4",
        "8": "1"
    }
}

ATA_TO_BAY = BAY_MAPPINGS.get(DEVICE_MODEL)


class UNASMonitor:
    def __init__(self, mqtt_enabled=True):
        self.mqtt_enabled = mqtt_enabled
        self.mqtt = None
        self._connected = False
        self.monitor_interval = DEFAULT_MONITOR_INTERVAL
        # anything _on_connect touches must exist before the connect wait below,
        # paho fires the callback from its own thread
        self._last_published = {}  # topic -> (payload, monotonic ts)
        self._storage = _UNSET  # /api/v2/storage, fetched once per cycle
        self._user_map_cache = None  # (ts, user_map, console_owner)
        self._share_members_cache = {}  # drive_id -> (ts, member_count, members)
        self._device_info = None
        self._section_errors = {}
        self._last_heartbeat = None

        if mqtt_enabled:
            self.mqtt = mqtt_client(CONFIG, MQTT_AVAILABILITY)
            self.mqtt.on_connect = self._on_connect
            self.mqtt.on_disconnect = self._on_disconnect
            self.mqtt.on_message = self._on_message
            self.mqtt.loop_start()
            connect(self.mqtt, CONFIG)

            for _ in range(30):
                if self._connected:
                    break
                time.sleep(1)

            if not self._connected:
                logger.warning("MQTT not connected after 30s - will keep retrying in background")

        self._admin_uid = None
        self._api_warned = False
        self._protect_api_warned = False
        self.bay_cache = {}
        self.known_drives = set()
        self.previous_drive_map = {}  # serial -> bay
        self.prev_cpu_idle = None
        self.prev_cpu_total = None
        self.prev_disk_read = None
        self.prev_disk_write = None
        self.prev_time = None

        try:
            with open('/etc/machine-id') as f:
                self.machine_id = f.read().strip()
        except OSError:
            self.machine_id = ""

    def _on_connect(self, _client, _userdata, _flags, reason_code, _properties):
        if reason_code == 0:
            logger.info("MQTT connected")
            self._connected = True
            self.mqtt.subscribe(MONITOR_INTERVAL_TOPIC)
            self.mqtt.publish(MQTT_AVAILABILITY, "online", retain=True)
            self._last_published.clear()
        else:
            logger.error(f"MQTT failed: {reason_code}")
            self._connected = False

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            logger.warning("MQTT disconnected")

    def _on_message(self, _client, _userdata, msg):
        if msg.topic == MONITOR_INTERVAL_TOPIC:
            try:
                new_interval = int(float(msg.payload.decode()))
                if 5 <= new_interval <= 60:
                    old = self.monitor_interval
                    self.monitor_interval = new_interval
                    logger.info(f"Monitor interval: {old}s -> {new_interval}s")
            except (ValueError, TypeError):
                pass

    def _publish(self, topic, value):
        payload = str(value)
        now = time.monotonic()
        last = self._last_published.get(topic)
        if last and last[0] == payload and now - last[1] < REPUBLISH_INTERVAL:
            return
        self.mqtt.publish(topic, payload, retain=True)
        self._last_published[topic] = (payload, now)

    def publish_system(self, metric, value):
        self._publish(f"{MQTT_SYSTEM}/{metric}", value)

    def publish_hdd(self, bay, metric, value):
        self._publish(f"{MQTT_HDD}/{bay}/{metric}", value)

    def publish_nvme(self, slot, metric, value):
        self._publish(f"{MQTT_NVME}/{slot}/{metric}", value)
    
    def publish_pool(self, pool_num, metric, value):
        self._publish(f"{MQTT_POOL}/{pool_num}/{metric}", value)

    def publish_share(self, name, metric, value):
        self._publish(f"{MQTT_SHARE}/{name}/{metric}", value)

    def _get_admin_user_id(self):
        if self._admin_uid:
            return self._admin_uid
        try:
            with open('/data/unifi-core/config/cache/users.json') as f:
                users = json.load(f)
            if users and isinstance(users, list):
                owners = [u for u in users if isinstance(u, dict) and u.get('isOwner')]
                self._admin_uid = (owners or users)[0].get('id')
                return self._admin_uid
        except (OSError, json.JSONDecodeError, KeyError, IndexError, AttributeError):
            pass
        return None

    def _fetch_api(self, path, need_auth=False):
        headers = {}
        if need_auth:
            uid = self._get_admin_user_id()
            if not uid:
                if not self._api_warned:
                    logger.warning("Cannot determine admin user ID for Drive API auth")
                    self._api_warned = True
                return None
            headers['X-UserId'] = uid
            headers['X-UserRole'] = 'admin'
        conn = http.client.HTTPConnection('127.0.0.1', 16080, timeout=5)
        try:
            conn.request('GET', path, headers=headers)
            resp = conn.getresponse()
            if resp.status == 200:
                return json.loads(resp.read())
            if not self._api_warned:
                logger.warning("Drive API %s returned %d", path, resp.status)
                self._api_warned = True
        except (OSError, json.JSONDecodeError, http.client.HTTPException) as e:
            if not self._api_warned:
                logger.warning("Drive API unavailable (%s), falling back to df", e)
                self._api_warned = True
        finally:
            conn.close()
        return None

    def _get_storage(self):
        if self._storage is _UNSET:
            self._storage = self._fetch_api('/api/v2/storage')
        return self._storage

    def _api_bays(self):
        # occupied bays even when smartctl fails on a drive, so a failing drive isn't removed
        disks = (self._get_storage() or {}).get('disks') or []
        return {
            str(disk['slotId']) for disk in disks
            if isinstance(disk, dict) and disk.get('state') != 'empty'
            and str(disk.get('slotId') or '').isdigit()
        }

    def _api_bay(self, serial):
        serial = (serial or '').strip()
        if not serial:
            return None
        storage = self._get_storage()
        for disk in (storage or {}).get('disks') or []:
            if (disk.get('serial') or '').strip() == serial and disk.get('slotId'):
                return str(disk['slotId'])
        return None

    def get_device_info(self):
        cached = self._device_info
        if cached and time.monotonic() - cached[0] < DEVICE_INFO_TTL:
            return cached[1]
        conn = http.client.HTTPConnection('127.0.0.1', 11081, timeout=5)
        try:
            conn.request('GET', '/api/system')
            resp = conn.getresponse()
            data = json.loads(resp.read()) if resp.status == 200 else None
        except (OSError, json.JSONDecodeError, http.client.HTTPException):
            data = None
        finally:
            conn.close()
        hardware = (data or {}).get('hardware') or {}
        if not hardware:
            return None
        sysid = hardware.get('sysid')
        info = {
            'name': hardware.get('name'),
            'shortname': hardware.get('shortname'),
            'sysid': f"{sysid:04x}" if isinstance(sysid, int) else None,
            'serial': hardware.get('serialno'),
            'mac': hardware.get('mac'),
            'hw_revision': hardware.get('hwrev'),
            'firmware': hardware.get('firmwareVersion'),
        }
        self._device_info = (time.monotonic(), info)
        return info

    def _fetch_protect_bootstrap(self):
        uid = self._get_admin_user_id()
        if not uid:
            if not self._protect_api_warned:
                logger.warning("Cannot determine admin user ID for Protect API auth")
                self._protect_api_warned = True
            return None

        headers = {
            'X-UserId': uid,
            'X-UserRole': 'admin',
        }
        conn = http.client.HTTPConnection('127.0.0.1', 7080, timeout=5)
        try:
            conn.request('GET', '/api/bootstrap', headers=headers)
            resp = conn.getresponse()
            if resp.status == 200:
                return json.loads(resp.read())
            if not self._protect_api_warned:
                logger.warning("Protect API /api/bootstrap returned %d", resp.status)
                self._protect_api_warned = True
        except (OSError, json.JSONDecodeError, http.client.HTTPException) as e:
            if not self._protect_api_warned:
                logger.warning("Protect API unavailable (%s)", e)
                self._protect_api_warned = True
        finally:
            conn.close()
        return None

    def get_pools_from_api(self):
        if DEVICE_MODEL.startswith("UNVR"):
            pools = self.get_pools()
            bootstrap = self._fetch_protect_bootstrap()
            if bootstrap:
                corruption_state = (bootstrap.get('nvr') or {}).get('corruptionState') or 'unknown'
                for pool in pools:
                    pool['status'] = corruption_state
            return pools

        data = self._get_storage()
        if not data or 'pools' not in data:
            return self.get_pools()

        pools = []
        for pool in data['pools']:
            capacity = pool.get('capacity', 0)
            usage = pool.get('usage', 0)
            capacity_gb = round(capacity / (1000 ** 3))
            usage_gb = round(usage / (1000 ** 3))
            available_gb = capacity_gb - usage_gb
            usage_pct = round((usage / capacity) * 100) if capacity else 0
            raid_groups = pool.get('raidGroups', [])
            raid_level = raid_groups[0].get('currentLevel', 'unknown') if raid_groups else 'unknown'
            protection = raid_groups[0].get('currentProtection', 0) if raid_groups else 0
            pools.append({
                'pool': pool.get('number', 1),
                'size': capacity_gb,
                'used': usage_gb,
                'available': available_gb,
                'usage': usage_pct,
                'status': pool.get('status', 'unknown'),
                'raid_level': raid_level,
                'protection': protection,
            })
        return pools

    def _get_user_map(self):
        cached = self._user_map_cache
        if cached and time.monotonic() - cached[0] < SHARE_CACHE_TTL:
            return cached[1], cached[2]
        data = self._fetch_api('/api/v1/users', need_auth=True)
        if not data or 'data' not in data:
            return {}, None
        user_map = {}
        console_owner = None
        for u in data['data']:
            uid = u['id']
            name = u.get('fullName', u.get('firstName', 'Unknown'))
            user_map[uid] = name
            if console_owner is None:
                console_owner = {'id': uid, 'name': name}
        self._user_map_cache = (time.monotonic(), user_map, console_owner)
        return user_map, console_owner

    def _get_share_members(self, drive_id, member_count):
        cached = self._share_members_cache.get(drive_id)
        if (
            cached
            and cached[1] == member_count
            and time.monotonic() - cached[0] < SHARE_CACHE_TTL
        ):
            return cached[2]
        user_map, console_owner = self._get_user_map()
        detail = self._fetch_api(f'/api/v2/drives/{drive_id}', need_auth=True)
        if detail is None:
            return []
        # the API omits "members" entirely when only the console owner has access
        listed = detail.get('members') or []
        members = [
            {"name": user_map.get(m['id'], 'Unknown'), "role": m.get('role', 'unknown')}
            for m in listed
        ]
        # console owner has implicit access to all shares but isn't in the members list
        if console_owner and member_count > len(members):
            listed_ids = {m['id'] for m in listed}
            if console_owner['id'] not in listed_ids:
                members.insert(0, {"name": console_owner['name'], "role": "admin"})
        self._share_members_cache[drive_id] = (time.monotonic(), member_count, members)
        return members

    def get_shares(self):
        storage_data = self._get_storage()
        pool_id_to_num = {}
        if storage_data and 'pools' in storage_data:
            for pool in storage_data['pools']:
                pool_id_to_num[pool['id']] = pool.get('number', 1)

        drives_data = self._fetch_api('/api/v2/drives', need_auth=True)
        if not drives_data or 'drives' not in drives_data:
            return None

        shares = []
        for drive in drives_data['drives']:
            if drive.get('type') != 'shared':
                continue
            usage_bytes = drive.get('usage', 0)
            quota_raw = drive.get('quota', -1)
            protections = drive.get('protections', {})
            pool_id = drive.get('storagePoolId', '')
            pool_num = pool_id_to_num.get(pool_id, '?')
            member_count = drive.get('memberCount', 0)
            members = self._get_share_members(drive['id'], member_count)
            shares.append({
                'name': drive.get('name', 'unknown'),
                'usage': round(usage_bytes / (1000 ** 3), 2),
                'quota': quota_raw,
                'pool': str(pool_num),
                'member_count': drive.get('memberCount', 0),
                'members': json.dumps(members),
                'snapshot_enabled': str(protections.get('snapshotEnabled', False)).lower(),
                'encryption': protections.get('encryptionStatus', 'unknown'),
            })
        return shares

    def run_cmd(self, cmd, timeout=10):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str))
            return result.stdout
        except (subprocess.SubprocessError, OSError):
            return ""

    def get_system_metrics(self):
        data = {}

        data['machine_id'] = self.machine_id

        try:
            with open('/proc/uptime') as f:
                data['uptime'] = int(float(f.read().split()[0]))
        except (OSError, ValueError, IndexError):
            pass

        try:
            with open('/usr/lib/version') as f:
                version_str = f.read().strip()
            match = re.search(r'\.v(\d+\.\d+\.\d+)\.', version_str)
            data['os_version'] = match.group(1) if match else version_str
        except OSError:
            pass
        if DEVICE_MODEL.startswith("UNVR"):
            data['protect_version'] = self.run_cmd(['dpkg-query', '-W', '-f=${Version}', 'unifi-protect']).strip()
        else:
            data['drive_version'] = self.run_cmd(['dpkg-query', '-W', '-f=${Version}', 'unifi-drive']).strip()
        data['cpu_usage'] = self.get_cpu_usage()
        data['disk_read'], data['disk_write'] = self.get_disk_throughput()

        with open('/proc/meminfo') as f:
            meminfo = {parts[0].rstrip(':'): int(parts[1]) for line in f if len(parts := line.split()) >= 2}

        mem_total = meminfo.get('MemTotal', 0) // 1024
        mem_avail = meminfo.get('MemAvailable', 0) // 1024
        mem_used = mem_total - mem_avail

        data['memory_total'] = mem_total
        data['memory_used'] = mem_used
        data['memory_usage'] = round((mem_used / mem_total) * 100, 1) if mem_total else 0

        try:
            with open('/sys/class/thermal/thermal_zone0/temp') as f:
                data['cpu_temp'] = int(f.read().strip()) // 1000
        except (OSError, ValueError):
            data['cpu_temp'] = 0

        try:
            with open('/sys/class/hwmon/hwmon0/pwm1') as f:
                pwm = int(f.read().strip())
                data['fan_speed'] = pwm
                data['fan_speed_percent'] = int((pwm * 100) / 255)
        except (OSError, ValueError):
            data['fan_speed'] = 0
            data['fan_speed_percent'] = 0

        return data

    def get_cpu_usage(self):
        def read_proc_stat():
            with open('/proc/stat') as f:
                parts = f.readline().split()
                values = list(map(int, parts[1:]))
            idle_time = values[3] + values[4]  # idle + iowait
            total_time = sum(values)
            return idle_time, total_time

        # handle script startup/restart to avoid reporting 0% usage to HA
        if self.prev_cpu_idle is None:
            idle_start, total_start = read_proc_stat()
            time.sleep(1.0)
            idle_end, total_end = read_proc_stat()

            self.prev_cpu_idle = idle_end
            self.prev_cpu_total = total_end

            delta_idle = idle_end - idle_start
            delta_total = total_end - total_start

            if delta_total <= 0:
                return 0

            return int(100 * (1 - delta_idle / delta_total))

        idle_now, total_now = read_proc_stat()
        delta_idle = idle_now - self.prev_cpu_idle
        delta_total = total_now - self.prev_cpu_total

        self.prev_cpu_idle = idle_now
        self.prev_cpu_total = total_now

        if delta_total <= 0:
            return 0

        return int(100 * (1 - delta_idle / delta_total))

    def get_disk_throughput(self):
        def read_diskstats():
            read_sectors = 0
            write_sectors = 0
            with open('/proc/diskstats') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 10:
                        continue
                    device = parts[2]
                    if device.startswith('sd') and len(device) == 3:
                        read_sectors += int(parts[5])
                        write_sectors += int(parts[9])
            return read_sectors, write_sectors

        # handle script startup/restart to avoid reporting 0mbps usage to HA
        if self.prev_disk_read is None:
            read_start, write_start = read_diskstats()
            time_start = time.time()
            time.sleep(1.0)
            read_end, write_end = read_diskstats()
            time_end = time.time()

            self.prev_disk_read = read_end
            self.prev_disk_write = write_end
            self.prev_time = time_end

            time_delta = time_end - time_start
            read_bytes = (read_end - read_start) * 512
            write_bytes = (write_end - write_start) * 512

            read_mbps = (read_bytes / time_delta) / (1024 * 1024)
            write_mbps = (write_bytes / time_delta) / (1024 * 1024)

            return round(read_mbps, 2), round(write_mbps, 2)

        read_now, write_now = read_diskstats()
        time_now = time.time()

        time_delta = time_now - self.prev_time
        read_bytes = (read_now - self.prev_disk_read) * 512
        write_bytes = (write_now - self.prev_disk_write) * 512

        self.prev_disk_read = read_now
        self.prev_disk_write = write_now
        self.prev_time = time_now

        if time_delta <= 0:
            return 0.0, 0.0

        read_mbps = (read_bytes / time_delta) / (1024 * 1024)
        write_mbps = (write_bytes / time_delta) / (1024 * 1024)

        return round(read_mbps, 2), round(write_mbps, 2)

    def get_bay_number(self, device, serial):
        # ATA port maps are only needed on UNVR, which has no storage API
        bay = self._api_bay(serial)
        if bay:
            return bay
        if device in self.bay_cache:
            return self.bay_cache[device]

        output = self.run_cmd(['udevadm', 'info', '-q', 'path', '-n', f'/dev/{device}'])
        bay = None
        for part in output.split('/'):
            if part.startswith('ata') and (ata_num := part[3:]) in ATA_TO_BAY:
                bay = ATA_TO_BAY[ata_num]
                break

        self.bay_cache[device] = bay
        return bay

    def get_drives(self):
        current_drives = {p.name for p in Path('/dev').glob('sd?')}

        if current_drives != self.known_drives:
            self.bay_cache.clear()
            self.known_drives = current_drives

        drives = []
        current_drive_map = {}

        for device_path in Path('/dev').glob('sd?'):
            device = device_path.name
            output = self.run_cmd(['smartctl', '-a', '-j', f'/dev/{device}'])
            if not output:
                logger.debug(f"No smartctl output for /dev/{device}")
                continue

            try:
                data = json.loads(output)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse smartctl JSON for /dev/{device}: {e}")
                continue

            if 'error' in data or not data.get('smart_status'):
                continue

            serial = data.get('serial_number', 'Unknown')
            bay = self.get_bay_number(device, serial)
            if not bay:
                continue

            temp = data.get('temperature', {}).get('current', 0)

            drive = {
                'bay': bay,
                'model': data.get('model_name') or data.get('product', 'Unknown'),
                'serial': serial,
                'firmware': data.get('firmware_version', 'Unknown'),
                'status': "Optimal" if data.get('smart_status', {}).get('passed') else "Warning",
                'temperature': temp
            }

            rotation = data.get('rotation_rate', 0)
            if rotation > 0:
                drive['rpm'] = rotation

            for attr in data.get('ata_smart_attributes', {}).get('table', []):
                name = attr.get('name', '').lower()
                if name == 'power_on_hours':
                    # smartctl JSON: raw.value can be vendor-packed; prefer decoded hours
                    poh = (data.get('power_on_time') or {}).get('hours')
                    if isinstance(poh, (int, float)) and poh >= 0:
                        drive['power_on_hours'] = int(poh)
                    else:
                        raw = attr.get('raw') or {}
                        # raw.string looks like: "40311 (52 181 0)"
                        s = raw.get('string', '')
                        if isinstance(s, str) and s:
                            try:
                                drive['power_on_hours'] = int(s.split()[0])
                            except (ValueError, IndexError):
                                drive['power_on_hours'] = int(raw.get('value', 0) or 0)
                        else:
                            drive['power_on_hours'] = int(raw.get('value', 0) or 0)
                elif name == 'reallocated_sector_ct':
                    drive['bad_sectors'] = attr.get('raw', {}).get('value', 0)

            if 'bad_sectors' not in drive:
                drive['bad_sectors'] = 0

            if 'power_on_hours' not in drive:
                drive['power_on_hours'] = data.get('power_on_time', {}).get('hours', 0)

            size_bytes = data.get('user_capacity', {}).get('bytes', 0)
            drive['total_size'] = round(size_bytes / (1000 ** 4), 2)

            drives.append(drive)
            current_drive_map[serial] = bay

        # HA removes a bay once the inventory leaves it out, this only logs the change
        for serial, old_bay in self.previous_drive_map.items():
            new_bay = current_drive_map.get(serial)
            if new_bay is None:
                logger.info(f"Drive {serial} no longer in bay {old_bay}")
            elif new_bay != old_bay:
                logger.info(f"Drive {serial} moved from bay {old_bay} to bay {new_bay}")

        self.previous_drive_map = current_drive_map
        return drives

    def get_nvme_drives(self):
        nvmes = []

        for device_path in sorted(Path('/dev').glob('nvme*n1')):
            device = device_path.name
            slot = device.replace('nvme', '').replace('n1', '')

            output = self.run_cmd(['smartctl', '-a', '-j', f'/dev/{device}'])
            if not output:
                logger.debug(f"No smartctl output for /dev/{device}")
                continue

            try:
                data = json.loads(output)
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse smartctl JSON for NVMe /dev/{device}: {e}")
                continue

            if 'error' in data:
                continue

            health = data.get('nvme_smart_health_information_log', {})

            nvme = {
                'slot': slot,
                'model': data.get('model_name', 'Unknown'),
                'serial': data.get('serial_number', 'Unknown'),
                'firmware': data.get('firmware_version', 'Unknown'),
                'status': "Optimal",
                'temperature': health.get('temperature', 0),
                'power_on_hours': health.get('power_on_hours', 0),
                'percentage_used': health.get('percentage_used', 0),
                'available_spare': health.get('available_spare', 100),
                'media_errors': health.get('media_errors', 0),
                'unsafe_shutdowns': health.get('unsafe_shutdowns', 0)
            }

            size_bytes = data.get('user_capacity', {}).get('bytes', 0)
            nvme['total_size'] = round(size_bytes / (1000 ** 4), 2)

            if health.get('critical_warning', 0) != 0 or health.get('available_spare', 100) < 10:
                nvme['status'] = "Warning"

            nvmes.append(nvme)

        return nvmes

    def get_pools(self):
        pools = []
        pool_num = 1

        for volume_dir in sorted(Path('/volume').glob('*')):
            if not volume_dir.is_dir():
                continue

            df_output = self.run_cmd(['df', '-B1', str(volume_dir)])
            lines = df_output.strip().split('\n')

            if len(lines) < 2:
                continue

            parts = lines[1].split()
            size_gb = round(int(parts[1]) / (1000 ** 3))

            if size_gb <= 75:
                continue

            pool = {
                'pool': pool_num,
                'size': size_gb,
                'used': round(int(parts[2]) / (1000 ** 3)),
                'available': round(int(parts[3]) / (1000 ** 3)),
                'usage': int(parts[4].rstrip('%')),
            }
            if ATA_TO_BAY and len(set(ATA_TO_BAY.values())) <= 1:
                pool['raid_level'] = 'none'

            pools.append(pool)
            pool_num += 1

        return pools

    def get_smb_connections(self):
        output = self.run_cmd(['smbstatus', '-b'])
        lines = output.strip().split('\n')

        connections = {}
        for line in lines[3:]:
            if not line.strip() or line.startswith('---'):
                continue

            parts = line.split()
            if len(parts) < 6:
                continue

            pid = parts[0]
            username = parts[1]
            ip = parts[3].split('(')[1].split(':')[0] if '(' in parts[3] else parts[3]

            connections[pid] = {
                'username': username,
                'ip': ip
            }

        return connections

    def get_smb_shares(self):
        output = self.run_cmd(['smbstatus', '-S'])
        lines = output.strip().split('\n')

        shares = []
        for line in lines[2:]:
            if not line.strip() or line.startswith('---'):
                continue

            parts = line.split()
            if len(parts) < 3:
                continue

            shares.append({
                'share': parts[0],
                'pid': parts[1],
                'ip': parts[2]
            })

        return shares

    def get_nfs_mounts(self):
        output = self.run_cmd(['showmount', '-a'])
        lines = output.strip().split('\n')[1:]

        mounts = []
        for line in lines:
            if not line.strip():
                continue

            parts = line.split(':')
            if len(parts) != 2:
                continue

            ip = parts[0]
            path = parts[1]

            share_match = path.split('/.srv/.unifi-drive/')
            if len(share_match) == 2:
                share = share_match[1].split('/')[0]
            else:
                share = 'unknown'

            mounts.append({
                'ip': ip,
                'share': share
            })

        return mounts

    def _section(self, name, collect):
        try:
            result = collect()
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            if self._section_errors.get(name) != error:
                logger.error(f"Collecting {name} failed: {error}")
                self._section_errors[name] = error
            return None
        if self._section_errors.pop(name, None):
            logger.info(f"Collecting {name} recovered")
        return result

    def get_smb_data(self):
        smb_connections = self.get_smb_connections()
        clients = []
        for share in self.get_smb_shares():
            conn = smb_connections.get(share['pid'], {})
            clients.append({
                'username': conn.get('username', 'unknown'),
                'ip': share['ip'],
                'share': share['share'],
            })
        return clients

    def collect_and_publish(self):
        self._storage = _UNSET
        inventory = {}

        system = self._section("system metrics", self.get_system_metrics)
        if system is not None:
            for key, value in system.items():
                self.publish_system(key, value)

        device = self._section("device info", self.get_device_info)
        if device:
            self.publish_system("device", json.dumps(device, sort_keys=True))

        drives = self._section("drives", self.get_drives)
        if drives is not None:
            inventory['bays'] = sorted({d['bay'] for d in drives} | self._api_bays(), key=int)
        else:
            inventory['bays'] = None
        for drive in drives or []:
            drive = dict(drive)
            bay = drive.pop('bay')
            for key, value in drive.items():
                self.publish_hdd(bay, key, value)

        nvmes = self._section("nvme drives", self.get_nvme_drives)
        inventory['nvme'] = None if nvmes is None else sorted((n['slot'] for n in nvmes), key=int)
        for nvme in nvmes or []:
            nvme = dict(nvme)
            slot = nvme.pop('slot')
            for key, value in nvme.items():
                self.publish_nvme(slot, key, value)

        pools = self._section("storage pools", self.get_pools_from_api)
        storage = self._get_storage()
        # the df fallback numbers pools differently, it can't tell which ones are gone
        from_api = isinstance(storage, dict) and 'pools' in storage
        if pools is None or not (from_api or DEVICE_MODEL.startswith("UNVR")):
            inventory['pools'] = None
        else:
            inventory['pools'] = sorted(p['pool'] for p in pools)
        for pool in pools or []:
            pool = dict(pool)
            pool_num = pool.pop('pool')
            for key, value in pool.items():
                self.publish_pool(pool_num, key, value)

        # UNVR doesn't have SMB/NFS/shares
        inventory['shares'] = []
        if not DEVICE_MODEL.startswith("UNVR"):
            smb_clients = self._section("smb connections", self.get_smb_data)
            if smb_clients is not None:
                self._publish(f"{MQTT_SMB}/connections", len(smb_clients))
                self._publish(f"{MQTT_SMB}/clients", json.dumps(smb_clients))

            nfs_mounts = self._section("nfs mounts", self.get_nfs_mounts)
            if nfs_mounts is not None:
                self._publish(f"{MQTT_NFS}/mounts", len(nfs_mounts))
                self._publish(f"{MQTT_NFS}/clients", json.dumps(nfs_mounts))

            shares = self._section("shares", self.get_shares)
            inventory['shares'] = None if shares is None else sorted(s['name'] for s in shares)
            for share in shares or []:
                share = dict(share)
                name = share.pop('name')
                for key, value in share.items():
                    self.publish_share(name, key, value)

        self._publish(f"{MQTT_ROOT}/inventory", json.dumps(inventory, sort_keys=True))
        self._heartbeat(system, drives, nvmes)

    def _heartbeat(self, system, drives, nvmes):
        now = time.monotonic()
        if self._last_heartbeat is not None and now - self._last_heartbeat < HEARTBEAT_INTERVAL:
            return
        self._last_heartbeat = now
        system = system or {}
        drive_temps = [d.get('temperature', 0) for d in drives or [] if 'temperature' in d]
        nvme_temps = [n.get('temperature', 0) for n in nvmes or [] if 'temperature' in n]
        hdd_str = ', '.join(f"{t}°C" for t in drive_temps) if drive_temps else "no drives"
        nvme_str = f" | NVMe {', '.join(f'{t}°C' for t in nvme_temps)}" if nvme_temps else ""
        logger.info(
            f"{system.get('fan_speed')} PWM ({system.get('fan_speed_percent')}%) | "
            f"CPU {system.get('cpu_temp')}°C | "
            f"HDD {hdd_str}{nvme_str} | "
            f"R: {system.get('disk_read')} MB/s W: {system.get('disk_write')} MB/s"
        )

    def collect(self) -> dict:
        """Gather the full metric set as a structured dict (no MQTT required).

        This reuses the same collector methods as the live publish path and is
        used by the headless `--once --json` mode for troubleshooting, tests,
        and external consumers.
        """
        self._storage = _UNSET
        data: dict = {}
        data["system"] = self.get_system_metrics()
        data["device"] = self.get_device_info()

        hdd = {}
        for drive in self.get_drives():
            d = dict(drive)
            hdd[d.pop("bay")] = d
        data["hdd"] = hdd

        nvme = {}
        for n in self.get_nvme_drives():
            d = dict(n)
            nvme[d.pop("slot")] = d
        data["nvme"] = nvme

        pools = {}
        for pool in self.get_pools_from_api():
            d = dict(pool)
            pools[d.pop("pool")] = d
        data["pools"] = pools

        # UNVR doesn't have SMB/NFS/shares
        if not DEVICE_MODEL.startswith("UNVR"):
            clients = self.get_smb_data()
            data["smb"] = {"connections": len(clients), "clients": clients}

            nfs_mounts = self.get_nfs_mounts()
            data["nfs"] = {"mounts": len(nfs_mounts), "clients": nfs_mounts}

            shares = {}
            for share in self.get_shares() or []:
                d = dict(share)
                shares[d.pop("name")] = d
            data["shares"] = shares

        return data

    def run(self):
        logger.info(f"UNAS monitor started (interval: {self.monitor_interval}s)")

        while True:
            try:
                self.collect_and_publish()
            except Exception as e:
                logger.error(f"Error: {e}")

            time.sleep(self.monitor_interval)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="UniFi UNAS monitor")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single collection cycle and exit, without connecting to MQTT.",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the collected metrics as JSON to stdout (implies --once).",
    )
    args = parser.parse_args()

    if args.once or args.json:
        monitor = UNASMonitor(mqtt_enabled=False)
        data = monitor.collect()
        print(json.dumps(data, indent=2, default=str))
        return

    monitor = UNASMonitor()
    monitor.run()


if __name__ == '__main__':
    main()
