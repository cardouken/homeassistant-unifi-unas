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
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_USERNAME = "root"
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 60

ATTR_SCRIPTS_INSTALLED = "scripts_installed"
ATTR_SSH_CONNECTED = "ssh_connected"
ATTR_MONITOR_RUNNING = "monitor_running"
ATTR_FAN_CONTROL_RUNNING = "fan_control_running"

CONF_DEVICE_MODEL = "device_model"
DEFAULT_DEVICE_MODEL = "UNAS_PRO"

DEVICE_MODELS = {
    "UNAS_PRO": "UNAS Pro (7-bay)",
    "UNAS_PRO_8": "UNAS Pro 8",
    "UNAS_PRO_4": "UNAS Pro 4",
    "UNAS_4": "UNAS 4",
    "UNAS_2": "UNAS 2",
    "UNVR": "UNVR",
}


def get_device_info(device_model: str) -> tuple[str, str]:
    if device_model == "UNVR":
        return "UNVR", "UniFi UNVR"
    return "UNAS", "UniFi UNAS"


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
    }
