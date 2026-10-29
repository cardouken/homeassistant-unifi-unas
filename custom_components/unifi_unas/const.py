from pathlib import Path

DOMAIN = "unifi_unas"

CONF_HOST = "host"

HA_SSH_KEY_PATHS = [
    Path("/config/.ssh/id_rsa"),
    Path("/config/.ssh/id_ed25519"),
    Path.home() / ".ssh" / "id_rsa",
    Path.home() / ".ssh" / "id_ed25519",
]
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_MQTT_HOST = "mqtt_host"
CONF_MQTT_USER = "mqtt_user"
CONF_MQTT_PASSWORD = "mqtt_password"
CONF_MQTT_PORT = "mqtt_port"
CONF_MQTT_TLS = "mqtt_tls"
CONF_MQTT_TLS_INSECURE = "mqtt_tls_insecure"
CONF_VERIFY_HOST_KEY = "verify_host_key"
CONF_HOST_KEY = "host_key"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTT_TLS_PORT = 8883

DEFAULT_USERNAME = "root"
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 60
SSH_STATUS_INTERVAL = 60

BACKUP_STATUS_IDLE = "idle"
BACKUP_STATUS_RUNNING = "in-progress"

ATTR_SCRIPTS_INSTALLED = "scripts_installed"
ATTR_SSH_CONNECTED = "ssh_connected"
ATTR_MONITOR_RUNNING = "monitor_running"
ATTR_FAN_CONTROL_RUNNING = "fan_control_running"

CONF_DEVICE_MODEL = "device_model"
CONF_DEVICE_NAME = "device_name"
DEFAULT_DEVICE_MODEL = "UNAS_PRO"

DEVICE_MODELS = {
    "UNAS_PRO": "UNAS Pro (7-bay)",
    "UNAS_PRO_8": "UNAS Pro 8",
    "UNAS_PRO_4": "UNAS Pro 4",
    "UNAS_4": "UNAS 4",
    "UNAS_2": "UNAS 2",
    "UNVR": "UNVR",
    "UNVR_INSTANT": "UNVR Instant",
    "UNVR_PRO": "UNVR Pro",
}

# hardware sysid from /api/system, as in UniFi's own uidb.json;
# the Gen 2 UNVRs are not known to work yet
SYSID_MODELS = {
    "ea51": "UNAS_PRO",
    "ea63": "UNAS_PRO_8",
    "ea67": "UNAS_PRO_4",
    "ea65": "UNAS_4",
    "ea66": "UNAS_4",
    "ea62": "UNAS_2",
    "ea72": "UNAS_2",
    "ea16": "UNVR",
    "ea1a": "UNVR",
    "ea20": "UNVR_PRO",
    "ea61": "UNVR_INSTANT",
}
# what the same consoles answer UniFi discovery with (uidb.json short names, "-" removed)
DISCOVERY_PLATFORMS = {
    "UNASPRO": "UNAS_PRO",
    "UNASPRO8": "UNAS_PRO_8",
    "UNASPRO4": "UNAS_PRO_4",
    "UNAS4W": "UNAS_4",
    "UNAS4B": "UNAS_4",
    "UNAS2W": "UNAS_2",
    "UNAS2B": "UNAS_2",
    "UNVR": "UNVR",
    "UNVR4": "UNVR",
    "NVR": "UNVR",
    "UNVRPRO": "UNVR_PRO",
    "UNVRINS": "UNVR_INSTANT",
    "UNVRINSTANT": "UNVR_INSTANT",
}


def get_device_info(entry_data: dict) -> tuple[str, str]:
    device_model = entry_data[CONF_DEVICE_MODEL]
    custom_name = entry_data.get(CONF_DEVICE_NAME)
    if device_model == "UNVR_INSTANT":
        return custom_name or "UNVR Instant", "UniFi UNVR Instant"
    if device_model.startswith("UNVR"):
        return custom_name or "UNVR", "UniFi UNVR"
    return custom_name or "UNAS", "UniFi UNAS"


REMOTE_TYPE_LABELS = {
    "googleDrive": "Google Drive",
    "oneDrive": "OneDrive",
    "dropbox": "Dropbox",
    "s3": "Amazon S3",
    "sftp": "SFTP",
    "b2": "Backblaze B2",
    "wasabi": "Wasabi",
}


def format_remote_type(remote_type):
    if not remote_type:
        return "Local"
    return REMOTE_TYPE_LABELS.get(remote_type, remote_type.title())


def format_schedule(schedule):
    if not schedule or not schedule.get("enable"):
        return "Disabled"
    time = schedule.get("firstRunTime", "")
    weekdays = schedule.get("weekdays", "*")
    if weekdays == "*":
        return f"Daily at {time}"
    return f"{weekdays} at {time}"


# MQTT topic structure
def get_mqtt_root(entry_id: str) -> str:
    return f"unas/{entry_id[:8]}"

def get_mqtt_topics(entry_id: str):
    root = get_mqtt_root(entry_id)
    return {
        "root": root,
        "availability": f"{root}/availability",
        "control": f"{root}/control",
        "system": f"{root}/system",
        "hdd": f"{root}/hdd",
        "nvme": f"{root}/nvme",
        "pool": f"{root}/pool",
        "smb": f"{root}/smb",
        "nfs": f"{root}/nfs",
        "share": f"{root}/share",
    }
