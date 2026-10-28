import json
import sys
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BUNDLE_DIR / "config.json"

sys.path.insert(0, str(BUNDLE_DIR / "vendor"))


def load_config():
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def mqtt_client(config, will_topic):
    import paho.mqtt.client as mqtt

    broker = config.get("mqtt", {})
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set(broker.get("user"), broker.get("password"))
    if broker.get("tls"):
        import ssl

        # paho's tls_set() needs ssl.PROTOCOL_TLSv1_2, which Python built on OpenSSL 4 lacks
        context = ssl.create_default_context()
        if broker.get("tls_insecure"):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(context)
    client.will_set(will_topic, "offline", retain=True)
    return client


def connect(client, config):
    broker = config.get("mqtt", {})
    client.connect_async(broker.get("host", ""), int(broker.get("port", 1883)), 60)
