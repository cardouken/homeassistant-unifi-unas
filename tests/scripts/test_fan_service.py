import logging
import threading
from types import SimpleNamespace

import pytest

import fan_control as fc


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "SETTINGS_FILE", tmp_path / "fan_settings.json")
    monkeypatch.setattr(fc, "HWMON", tmp_path)
    (tmp_path / "pwm1").write_text("64")
    svc = fc.FanService()
    svc.published = []
    svc.client.publish = (
        lambda topic, payload, retain=False: svc.published.append((topic, payload, retain))
    )
    return svc


def message(topic, payload):
    return SimpleNamespace(topic=topic, payload=payload.encode())


def setting(name, payload):
    return message(f"{fc.MQTT_FAN_CONTROL}/{name}", payload)


def test_valid_setting_is_applied_and_saved(service):
    service._on_message(None, None, setting("curve/min_fan", "51"))
    assert service.settings["min_fan"] == 51
    assert fc.load_settings()["min_fan"] == 51


def test_invalid_setting_is_ignored_and_logged_once(service, caplog):
    for _ in range(3):
        service._on_message(None, None, setting("mode", "target_temp; reboot"))
    assert service.settings["mode"] == "unas_managed"
    assert "mode" not in service.received
    assert sum("Ignoring invalid mode" in r.message for r in caplog.records) == 1


def test_unknown_topics_are_ignored(service):
    service._on_message(None, None, setting("curve/kp", "100"))
    service._on_message(None, None, setting("speed", "128"))
    assert service.settings == fc.DEFAULT_SETTINGS


@pytest.mark.parametrize(
    "payload,expected", [("10", 10), ("10.0", 10), ("2", 5), ("300", 60), ("fast", 30)]
)
def test_monitor_interval_is_clamped(service, payload, expected):
    service._on_message(None, None, message(fc.MQTT_MONITOR_INTERVAL, payload))
    assert service.monitor_interval == expected


def test_only_missing_settings_are_republished(service):
    service._on_message(None, None, setting("mode", "target_temp"))
    service._on_message(None, None, setting("curve/target_temp", "43"))
    service._publish_missing_settings()
    published = {topic: (payload, retain) for topic, payload, retain in service.published}
    assert f"{fc.MQTT_FAN_CONTROL}/mode" not in published
    assert f"{fc.MQTT_FAN_CONTROL}/curve/target_temp" not in published
    assert published[f"{fc.MQTT_FAN_CONTROL}/curve/min_fan"] == ("64", True)
    assert published[f"{fc.MQTT_FAN_CONTROL}/speed"] == ("128", True)
    assert len(published) == 7


def test_legacy_speed_mode_moves_to_the_speed_topic(service):
    service._on_message(None, None, setting("mode", "153"))
    assert (service.settings["mode"], service.settings["speed"]) == ("set_speed", 153)
    assert {"mode", "speed"} <= service.received
    published = {topic: (payload, retain) for topic, payload, retain in service.published}
    assert published == {
        f"{fc.MQTT_FAN_CONTROL}/speed": ("153", True),
        f"{fc.MQTT_FAN_CONTROL}/mode": ("set_speed", True),
    }


@pytest.mark.parametrize("payload", ["256", "-1", "fast", "12.5"])
def test_invalid_speed_is_ignored(service, payload):
    service._on_message(None, None, setting("speed", payload))
    assert service.settings["speed"] == 128


def test_foreign_pwm_write_is_overwritten(service, tmp_path):
    (tmp_path / "pwm1").write_text("57")
    threading.Timer(0.5, service.stop.set).start()
    service._hold_pwm(90)
    assert (tmp_path / "pwm1").read_text() == "90"


def test_managed_mode_leaves_the_pwm_alone(service, tmp_path):
    (tmp_path / "pwm1").write_text("57")
    threading.Timer(0.5, service.stop.set).start()
    service._hold_pwm(None)
    assert (tmp_path / "pwm1").read_text() == "57"


def test_out_of_range_pwm_is_never_written(service, tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    for pwm in (-765, 256):
        service._write_pwm(pwm)
    assert (tmp_path / "pwm1").read_text() == "64"
    assert sum("Refusing to write" in r.message for r in caplog.records) == 2
