import json
import re
from pathlib import Path

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "unifi_unas"
STRINGS = json.loads((INTEGRATION / "strings.json").read_text())
SOURCE = "\n".join(path.read_text() for path in INTEGRATION.glob("*.py"))


def test_english_translations_match_strings():
    assert json.loads((INTEGRATION / "translations" / "en.json").read_text()) == STRINGS


def test_every_exception_key_is_translated():
    used = set(re.findall(r'translation_domain=DOMAIN,\s*translation_key="([a-z_]+)"', SOURCE))
    used |= {"temperature_order", "fan_order"}
    assert used and used <= set(STRINGS["exceptions"])


def test_every_icon_has_a_name():
    icons = json.loads((INTEGRATION / "icons.json").read_text())["entity"]
    for platform, keys in icons.items():
        assert set(keys) <= set(STRINGS["entity"][platform])
