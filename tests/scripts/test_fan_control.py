import json
from pathlib import Path

import pytest

import fan_control as fc
from fan_replay import scenarios

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fan_replay_expected.json"

SETTINGS = {
    "mode": "target_temp",
    "min_temp": 40,
    "max_temp": 50,
    "min_fan": 64,
    "max_fan": 255,
    "target_temp": 42,
    "temp_metric": "max",
    "response_speed": "balanced",
}


# expected values are the 0.9.7 bash controller's output with its trend history bug fixed
def test_replay_matches_bash_controller():
    expected = json.loads(FIXTURE.read_text())
    for rows, want in zip(scenarios(), expected):
        hw = {"pwm": 0}
        controller = fc.Controller(lambda: hw["pwm"])
        version = 0
        for n, (row, (pwm, written, integral, trend)) in enumerate(zip(rows, want)):
            if row["hw"] is not None:
                hw["pwm"] = row["hw"]
            version += row["sample"]
            settings = dict(row["settings"])
            action, got = controller.tick(row["t"], settings, row["temps"], version)
            if action == "write":
                hw["pwm"] = got
                assert (got, got) == (pwm, written), n
            else:
                assert written == -1, n
            assert controller.integral == integral, n
            assert controller.trend_mult == trend, n


@pytest.mark.parametrize("payload", [
    "target_temp; touch /tmp/x", "$(touch /tmp/x)", "auto`id`", "garbage", "1000", "", "12.5", "-5",
])
def test_invalid_mode_payloads_rejected(payload):
    assert fc.parse_setting("mode", payload) is None


@pytest.mark.parametrize("key,payload,value", [
    ("mode", "target_temp", "target_temp"),
    ("mode", "128", "128"),
    ("min_fan", "51", 51),
    ("temp_metric", "avg", "avg"),
    ("response_speed", "aggressive", "aggressive"),
    ("min_fan", "51.0", None),
    ("temp_metric", "min", None),
    ("target_temp", "43; reboot", None),
])
def test_setting_validation(key, payload, value):
    assert fc.parse_setting(key, payload) == value


def test_invalid_set_speed_falls_back_to_managed():
    settings = dict(SETTINGS, mode="300")
    controller = fc.Controller(lambda: 100)
    assert controller.tick(1000, settings, [44], 1) == ("managed", None)
    assert settings["mode"] == "unas_managed"


def test_curve_uses_hottest_drive():
    settings = dict(SETTINGS, mode="auto", min_fan=51)
    controller = fc.Controller(lambda: 0)
    assert controller.tick(1000, settings, [45, 30], 1) == ("write", 153)
    assert controller.tick(1001, settings, [39], 1) == ("write", 51)
    assert controller.tick(1002, settings, [50], 1) == ("write", 255)


def test_valid_temps_drops_garbage_and_sorts():
    assert fc.valid_temps([41, 0, None, "44", 120, 44, -3, 43.5, 38]) == [44, 41, 38]


def test_no_data_holds_then_ramps_to_full():
    failsafe = fc.FailSafe(started=1000)
    assert failsafe.apply(1000, "target_temp", [44], "write", 80, 80) == (80, None)
    assert failsafe.apply(1010, "target_temp", [], "no_data", None, 80) == (80, None)
    pwm, fault = failsafe.apply(1035, "target_temp", [], "no_data", None, 80)
    assert 80 < pwm < 255 and fault == "no_temperature_data"
    assert failsafe.apply(1060, "target_temp", [], "no_data", None, 80) == (255, "no_temperature_data")
    assert failsafe.apply(1061, "target_temp", [43], "write", 70, 255) == (70, None)


def test_no_data_ramp_is_linear_from_the_held_pwm():
    failsafe = fc.FailSafe(started=1000)
    held = 80
    seen = {}
    for now in range(1001, 1061):
        held, _ = failsafe.apply(now, "target_temp", [], "no_data", None, held)
        seen[now] = held
    assert seen[1010] == 80
    assert seen[1035] == 80 + (255 - 80) // 2
    assert seen[1059] < 255
    assert seen[1060] == 255


def test_no_data_at_startup_holds_current_pwm():
    failsafe = fc.FailSafe(started=1000)
    assert failsafe.apply(1005, "auto", [], "no_data", None, 120) == (120, None)
    assert failsafe.apply(1070, "auto", [], "no_data", None, 120) == (255, "no_temperature_data")


@pytest.mark.parametrize("mode,action", [("target_temp", "write"), ("auto", "write"), ("128", "write")])
def test_overheat_forces_full_speed_with_hysteresis(mode, action):
    failsafe = fc.FailSafe(started=1000)
    assert failsafe.apply(1000, mode, [61, 40], action, 128, 128) == (128, None)
    assert failsafe.apply(1001, mode, [62, 40], action, 128, 128) == (255, "overheat")
    assert failsafe.apply(1002, mode, [60, 40], action, 128, 255) == (255, "overheat")
    assert failsafe.apply(1003, mode, [59, 40], action, 128, 255) == (128, None)


def test_overheat_leaves_unas_managed_alone():
    failsafe = fc.FailSafe(started=1000)
    assert failsafe.apply(1000, "unas_managed", [70], "managed", None, 90) == (None, None)


def test_set_speed_keeps_speed_without_temps():
    failsafe = fc.FailSafe(started=1000)
    assert failsafe.apply(1100, "128", [], "write", 128, 128) == (128, "no_temperature_data")


def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fc, "SETTINGS_FILE", tmp_path / "fan.json")
    assert fc.load_settings() == fc.DEFAULT_SETTINGS
    stored = {**fc.DEFAULT_SETTINGS, **SETTINGS, "min_fan": 51}
    fc.save_settings(stored)
    assert fc.load_settings() == stored


def test_settings_file_from_before_the_speed_topic(tmp_path, monkeypatch):
    path = tmp_path / "fan.json"
    monkeypatch.setattr(fc, "SETTINGS_FILE", path)
    path.write_text(json.dumps(dict(SETTINGS, mode="153")))
    settings = fc.load_settings()
    assert (settings["mode"], settings["speed"]) == ("set_speed", 153)


def test_set_speed_reaches_the_controller_as_before():
    assert fc.controller_settings(dict(fc.DEFAULT_SETTINGS, mode="set_speed", speed=90))["mode"] == "90"
    assert fc.controller_settings(fc.DEFAULT_SETTINGS)["mode"] == "unas_managed"


def test_settings_file_ignores_invalid_values(tmp_path, monkeypatch):
    path = tmp_path / "fan.json"
    monkeypatch.setattr(fc, "SETTINGS_FILE", path)
    path.write_text(json.dumps({"mode": "$(reboot)", "min_fan": "51", "max_fan": -1}))
    settings = fc.load_settings()
    assert settings["mode"] == "unas_managed"
    assert settings["min_fan"] == 51
    assert settings["max_fan"] == 255


def test_data_version_keeps_counting_when_the_interval_grows():
    service = fc.FanService()
    service.monitor_interval = 5
    versions = [service._data_version(now) for now in range(1000, 1011)]
    assert versions == [1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 3]
    service.monitor_interval = 30
    assert service._data_version(1020) == 3
    assert service._data_version(1040) == 4
