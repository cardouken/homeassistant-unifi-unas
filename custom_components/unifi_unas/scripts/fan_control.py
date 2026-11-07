#!/usr/bin/env python3

import http.client
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from unas_common import BUNDLE_DIR, connect, load_config, mqtt_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

CONFIG = load_config()
MQTT_ROOT = CONFIG.get("root", "unas")
MQTT_CONTROL = f"{MQTT_ROOT}/control"
MQTT_FAN_CONTROL = f"{MQTT_CONTROL}/fan"
MQTT_MONITOR_INTERVAL = f"{MQTT_CONTROL}/monitor_interval"
MQTT_FAN_AVAILABILITY = f"{MQTT_ROOT}/fan/availability"
MQTT_FAN_STATE = f"{MQTT_ROOT}/fan/state"

SETTINGS_FILE = BUNDLE_DIR / "fan_settings.json"
HWMON = Path("/sys/class/hwmon/hwmon0")

KP = 10
KI = 0.05
MAX_RATE = 5
RESTORE_WINDOW = 300
INTEGRAL_REDUCE_FACTOR = 0.25
# the bash controller kept 7 samples in practice (off-by-one)
TREND_HISTORY_SIZE = 7
TREND_FALLING_FAST = -1.5
TREND_FALLING = 0
TREND_STABLE = 0.3
RESPONSE_MULTIPLIERS = {"relaxed": 0.5, "balanced": 1.0, "aggressive": 2.0}

TEMP_POLL_INTERVAL = 5
DEFAULT_MONITOR_INTERVAL = 30
NO_DATA_HOLD = 10
NO_DATA_FULL = 60
OVERHEAT_TEMP = 62
OVERHEAT_RELEASE = 60
FULL_PWM = 255
STATE_REPUBLISH_INTERVAL = 30
# uhwd keeps writing its own PWM for a while after a profile change
REASSERT_INTERVAL = 0.2
HEARTBEAT_INTERVAL = 600
CPU_THERMAL_PATH = "/sys/class/thermal/thermal_zone0/temp"
# stepped floor the mode output can never go below; each step releases 5°C under its threshold
CPU_FLOOR_STEPS = ((90, 153), (95, 255))
CPU_FLOOR_RELEASE = 5

DEFAULT_SETTINGS = {
    "mode": "unas_managed",
    "min_temp": 40,
    "max_temp": 50,
    "min_fan": 64,
    "max_fan": 255,
    "target_temp": 42,
    "temp_metric": "max",
    "response_speed": "balanced",
    "speed": 128,
    "cpu_target": 80,
    "cpu_min_temp": 70,
    "cpu_max_temp": 90,
}
SETTING_TOPICS = {
    "mode": "mode",
    "speed": "speed",
    "curve/min_temp": "min_temp",
    "curve/max_temp": "max_temp",
    "curve/min_fan": "min_fan",
    "curve/max_fan": "max_fan",
    "curve/target_temp": "target_temp",
    "curve/temp_metric": "temp_metric",
    "curve/response_speed": "response_speed",
    "curve/cpu_target": "cpu_target",
    "curve/cpu_min_temp": "cpu_min_temp",
    "curve/cpu_max_temp": "cpu_max_temp",
}
INT_SETTINGS = (
    "min_temp", "max_temp", "min_fan", "max_fan", "target_temp",
    "cpu_target", "cpu_min_temp", "cpu_max_temp",
)
MODE_PATTERN = re.compile(r"unas_managed|auto|target_temp|set_speed|[0-9]{1,3}")
# before the speed topic, Set Speed was the PWM written into the mode topic
LEGACY_SPEED_MODE = re.compile(r"[0-9]{1,3}")
INT_PATTERN = re.compile(r"[0-9]+")


def rounded(value, places):
    # the bash controller rounded every step through awk printf
    return float("%.*f" % (places, value))


def parse_setting(key, payload):
    if key == "mode":
        return payload if MODE_PATTERN.fullmatch(payload) else None
    if key == "temp_metric":
        return payload if payload in ("max", "avg") else None
    if key == "response_speed":
        return payload if payload in RESPONSE_MULTIPLIERS else None
    if key == "speed":
        return int(payload) if INT_PATTERN.fullmatch(payload) and int(payload) <= 255 else None
    if key in INT_SETTINGS:
        return int(payload) if INT_PATTERN.fullmatch(payload) else None
    return None


def controller_settings(settings):
    # the controller still takes Set Speed as a numeric mode
    if settings["mode"] == "set_speed":
        return dict(settings, mode=str(settings["speed"]))
    return dict(settings)


def metric_value(temps, metric):
    if metric == "avg":
        return rounded(sum(temps) / len(temps), 1)
    return float(temps[0])


def curve_pwm(temp, min_temp, max_temp, min_fan, max_fan):
    if temp <= min_temp:
        return min_fan
    if temp >= max_temp:
        return max_fan
    return int(min_fan + (temp - min_temp) * (max_fan - min_fan) / (max_temp - min_temp))


def pi_step(state, now, temp, target, min_fan, max_fan, accum_mult, speed_mult):
    max_integral = max_fan - min_fan
    error = rounded(temp - target, 1)

    if state.last_time == 0:
        dt = 1
    else:
        dt = min(max(now - state.last_time, 1), 10)

    p_term = int(rounded(KP * error, 0))

    if not (state.last_pwm >= max_fan and error > 0):
        if error > 0:
            integral = state.integral + KI * error * dt * accum_mult
        else:
            integral = state.integral + KI * error * dt * speed_mult
        if integral > max_integral:
            integral = max_integral
        if integral < 0:
            integral = 0
        state.integral = rounded(integral, 2)

    pwm = int(rounded(min_fan + p_term + state.integral, 0))
    if state.last_pwm > 0:
        max_change = MAX_RATE * dt
        change = pwm - state.last_pwm
        if change > max_change:
            pwm = state.last_pwm + max_change
        if change < -max_change:
            pwm = state.last_pwm - max_change
    if pwm < min_fan:
        pwm = min_fan
    if pwm > max_fan:
        pwm = max_fan

    state.last_pwm = pwm
    state.last_time = now
    return pwm


class CpuPI:
    def __init__(self):
        self.integral = 0.0
        self.last_pwm = 0
        self.last_time = 0
        self.prev_target = None

    def reset(self):
        self.integral = 0.0
        self.last_pwm = 0
        self.last_time = 0

    def step(self, now, temp, settings):
        target = settings["cpu_target"]
        if self.prev_target is not None and target != self.prev_target and temp < target:
            self.integral = rounded(self.integral * INTEGRAL_REDUCE_FACTOR, 2)
        self.prev_target = target
        speed_mult = RESPONSE_MULTIPLIERS.get(settings["response_speed"], 1.0)
        return pi_step(
            self, now, float(temp), target, settings["min_fan"], settings["max_fan"],
            speed_mult, speed_mult,
        )


class Controller:
    def __init__(self, read_hw_pwm):
        self.read_hw_pwm = read_hw_pwm
        self.integral = 0.0
        self.last_pwm = 0
        self.last_time = 0
        self.trend_mult = 1.0
        self.history = []
        self.seen_version = 0
        self.prev_mode = None
        self.prev_target = None
        self.saved_integral = None
        self.saved_time = 0
        self.cpu = CpuPI()
        self.source = None

    def reset_pi(self):
        self.integral = 0.0
        self.last_pwm = 0
        self.last_time = 0
        self.cpu.reset()

    def tick(self, now, settings, temps, data_version, cpu=None):
        self.source = None
        self._target_change(settings, temps)
        self._mode_transition(now, settings, temps)
        mode = settings["mode"]

        if mode == "unas_managed":
            return "managed", None
        if mode == "auto":
            if not temps:
                return "no_data", None
            pwm = curve_pwm(
                temps[0], settings["min_temp"], settings["max_temp"],
                settings["min_fan"], settings["max_fan"],
            )
            self.source = "drives"
            if cpu is not None:
                cpu_pwm = curve_pwm(
                    cpu, settings["cpu_min_temp"], settings["cpu_max_temp"],
                    settings["min_fan"], settings["max_fan"],
                )
                if cpu_pwm > pwm:
                    pwm = cpu_pwm
                    self.source = "cpu"
            return "write", pwm
        if mode == "target_temp":
            if not temps:
                return "no_data", None
            temp = metric_value(temps, settings["temp_metric"])
            pwm = self._pi(now, temp, settings, data_version)
            self.source = "drives"
            if cpu is not None:
                cpu_pwm = self.cpu.step(now, cpu, settings)
                if cpu_pwm > pwm:
                    pwm = cpu_pwm
                    self.source = "cpu"
            return "write", pwm
        if mode.isdigit() and int(mode) <= 255:
            return "write", int(mode)

        logger.warning("Invalid mode %s, falling back to UNAS Managed", mode)
        settings["mode"] = "unas_managed"
        self.reset_pi()
        return "managed", None

    def _target_change(self, settings, temps):
        target = settings["target_temp"]
        if self.prev_target is None or target == self.prev_target:
            self.prev_target = target
            return
        if temps and int(metric_value(temps, settings["temp_metric"])) < target:
            self.integral = rounded(self.integral * INTEGRAL_REDUCE_FACTOR, 2)
            logger.info(
                "Target changed %s°C -> %s°C, now below target, integral reduced to %s",
                self.prev_target, target, self.integral,
            )
        self.prev_target = target

    def _mode_transition(self, now, settings, temps):
        mode = settings["mode"]
        if self.prev_mode == "target_temp" and mode != "target_temp":
            self.saved_integral = self.integral
            self.saved_time = now

        if mode == "target_temp" and self.prev_mode != "target_temp":
            self.reset_pi()
            self.history = []
            self.seen_version = 0
            min_fan = settings["min_fan"]
            max_integral = settings["max_fan"] - min_fan
            calculated = 0
            current_pwm = self.read_hw_pwm()
            if current_pwm > min_fan:
                temp = int(metric_value(temps, settings["temp_metric"])) if temps else 0
                expected_p = max((temp - settings["target_temp"]) * KP, 0)
                calculated = current_pwm - min_fan - expected_p
                if calculated < 0:
                    calculated = 0
                if calculated > max_integral:
                    calculated = max_integral

            if self.saved_integral is not None and now - self.saved_time < RESTORE_WINDOW:
                self.integral = self.saved_integral
                logger.info(
                    "Restored saved integral %s after %ss", self.integral, now - self.saved_time
                )
            elif calculated > 0:
                self.integral = float(calculated)
            self.saved_integral = None

        self.prev_mode = mode

    def _update_trend(self, temp, data_version):
        if data_version <= self.seen_version:
            return
        self.seen_version = data_version
        self.history.append(temp)
        if len(self.history) > TREND_HISTORY_SIZE:
            self.history.pop(0)
        if len(self.history) < 3:
            return
        diff = rounded(self.history[-1] - self.history[0], 1)
        if diff <= TREND_FALLING_FAST:
            self.trend_mult = 0.0
        elif diff < TREND_FALLING:
            self.trend_mult = 0.2
        elif diff < TREND_STABLE:
            self.trend_mult = 1.0
        else:
            self.trend_mult = 1.5

    def _pi(self, now, temp, settings, data_version):
        self._update_trend(temp, data_version)
        speed_mult = RESPONSE_MULTIPLIERS.get(settings["response_speed"], 1.0)
        accum_mult = rounded(self.trend_mult * speed_mult, 2)
        return pi_step(
            self, now, temp, settings["target_temp"], settings["min_fan"], settings["max_fan"],
            accum_mult, speed_mult,
        )


class FailSafe:
    def __init__(self, started):
        self.last_valid = started
        self.ramp_from = None
        self.overheat = False
        self.cpu_level = 0

    def apply(self, now, mode, temps, action, pwm, held_pwm, cpu=None):
        if temps:
            self.last_valid = now
            self.ramp_from = None
            hottest = temps[0]
            if hottest >= OVERHEAT_TEMP:
                self.overheat = True
            elif hottest < OVERHEAT_RELEASE:
                self.overheat = False

        if cpu is not None:
            while (
                self.cpu_level < len(CPU_FLOOR_STEPS)
                and cpu >= CPU_FLOOR_STEPS[self.cpu_level][0]
            ):
                self.cpu_level += 1
            while (
                self.cpu_level > 0
                and cpu < CPU_FLOOR_STEPS[self.cpu_level - 1][0] - CPU_FLOOR_RELEASE
            ):
                self.cpu_level -= 1

        if action == "managed":
            return None, None

        fault = None
        missing_for = now - self.last_valid
        if missing_for > NO_DATA_HOLD:
            fault = "no_temperature_data"
        if action == "no_data":
            if self.ramp_from is None:
                self.ramp_from = held_pwm
            if missing_for <= NO_DATA_HOLD:
                pwm = self.ramp_from
            elif missing_for >= NO_DATA_FULL:
                pwm = FULL_PWM
            else:
                ramp = (missing_for - NO_DATA_HOLD) / (NO_DATA_FULL - NO_DATA_HOLD)
                pwm = int(self.ramp_from + (FULL_PWM - self.ramp_from) * ramp)

        if self.cpu_level:
            floor = CPU_FLOOR_STEPS[self.cpu_level - 1][1]
            if pwm is None or pwm < floor:
                pwm = floor
            if floor == FULL_PWM and fault is None:
                fault = "cpu_overheat"

        if self.overheat:
            return FULL_PWM, "overheat"
        return pwm, fault


def drive_api_temps():
    conn = http.client.HTTPConnection("127.0.0.1", 16080, timeout=3)
    try:
        conn.request("GET", "/api/v2/storage")
        resp = conn.getresponse()
        if resp.status != 200:
            return []
        data = json.loads(resp.read())
    except (OSError, ValueError, http.client.HTTPException):
        return []
    finally:
        conn.close()
    disks = data.get("disks") if isinstance(data, dict) else None
    if not isinstance(disks, list):
        return []
    return [
        disk.get("temperature") for disk in disks
        if isinstance(disk, dict) and disk.get("state") != "empty"
    ]


def smartctl_temps():
    temps = []
    for device in sorted(Path("/dev").glob("sd?")):
        try:
            result = subprocess.run(
                ["smartctl", "-n", "standby", "-A", "-j", str(device)],
                capture_output=True, text=True, timeout=5,
            )
            temps.append(json.loads(result.stdout).get("temperature", {}).get("current"))
        except (subprocess.SubprocessError, OSError, ValueError, AttributeError):
            continue
    return temps


def valid_temps(temps):
    return sorted((t for t in temps if isinstance(t, int) and 1 <= t <= 99), reverse=True)


def read_temps():
    temps = valid_temps(drive_api_temps())
    if not temps:
        temps = valid_temps(smartctl_temps())
    return temps


def read_cpu_temp():
    temp = int(Path(CPU_THERMAL_PATH).read_text().strip()) // 1000
    if not 1 <= temp <= 150:
        raise ValueError(f"out of range: {temp}")
    return temp


def pwm_channels():
    return [path for path in sorted(HWMON.glob("pwm[1-9]")) if os.access(path, os.W_OK)]


def read_hw_pwm():
    try:
        return int((HWMON / "pwm1").read_text().strip())
    except (OSError, ValueError):
        return 0


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    try:
        stored = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return settings
    if isinstance(stored, dict):
        for key in DEFAULT_SETTINGS:
            value = parse_setting(key, str(stored.get(key, "")))
            if value is not None:
                settings[key] = value
    if LEGACY_SPEED_MODE.fullmatch(settings["mode"]) and int(settings["mode"]) <= 255:
        settings["speed"] = int(settings["mode"])
        settings["mode"] = "set_speed"
    return settings


def save_settings(settings):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(settings))
        tmp.replace(SETTINGS_FILE)
    except OSError as err:
        logger.warning("Could not save fan settings: %s", err)


def stopping_for_good():
    # restarts and crashes are followed by a new start
    try:
        jobs = subprocess.run(
            ["systemctl", "list-jobs", "--no-legend", "--no-pager", "fan_control.service"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return True
    return any(line.split()[2:3] == ["stop"] for line in jobs.splitlines())


def hand_back():
    # uhwd only resumes writing PWM after a fan profile change
    try:
        from ustd.tools.uhardware_fan import FanProfileManager
        manager = FanProfileManager()
        current = manager.get_current_profile()
        manager.switch_profile("quiet" if current != "quiet" else "default")
        manager.switch_profile(current)
        logger.info("Handed fan control back to the UNAS")
    except Exception as err:
        logger.warning("Could not hand fan control back to the UNAS: %s", err)


class FanService:
    def __init__(self):
        self.settings = load_settings()
        self.received = set()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.subscribed = threading.Event()
        self.monitor_interval = DEFAULT_MONITOR_INTERVAL
        self.data_version = 0
        self.data_version_at = None
        self.channels = pwm_channels()
        self.controller = Controller(read_hw_pwm)
        self.failsafe = FailSafe(int(time.time()))
        self.held_pwm = read_hw_pwm()
        self.temps = []
        self.cpu_temp = None
        self.cpu_error = None
        self.last_poll = None
        self.last_state = None
        self.last_state_at = 0
        self.last_log = None
        self.last_log_at = 0
        self.rejected = set()

        self.client = mqtt_client(CONFIG, MQTT_FAN_AVAILABILITY)
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    def _on_connect(self, client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            logger.error("MQTT connection failed: %s", reason_code)
            return
        logger.info("MQTT connected")
        client.publish(MQTT_FAN_AVAILABILITY, "online", retain=True)
        client.subscribe([(f"{MQTT_FAN_CONTROL}/#", 0), (MQTT_MONITOR_INTERVAL, 0)])

    def _on_subscribe(self, _client, _userdata, _mid, _reason_codes, _properties):
        self.subscribed.set()

    def _on_message(self, _client, _userdata, msg):
        try:
            payload = msg.payload.decode()
        except UnicodeDecodeError:
            return
        if msg.topic == MQTT_MONITOR_INTERVAL:
            try:
                self.monitor_interval = min(max(int(float(payload)), 5), 60)
            except ValueError:
                pass
            return
        key = SETTING_TOPICS.get(msg.topic[len(MQTT_FAN_CONTROL) + 1:])
        if key is None:
            return
        value = parse_setting(key, payload)
        if value is None:
            if (key, payload) not in self.rejected:
                self.rejected.add((key, payload))
                logger.warning("Ignoring invalid %s value %r", key, payload)
            return
        updates = {key: value}
        if key == "mode" and LEGACY_SPEED_MODE.fullmatch(value) and int(value) <= 255:
            updates = {"mode": "set_speed", "speed": int(value)}
            logger.info("Converting the legacy Set Speed mode %s to the speed topic", value)
            self.client.publish(f"{MQTT_FAN_CONTROL}/speed", str(updates["speed"]), retain=True)
            self.client.publish(f"{MQTT_FAN_CONTROL}/mode", "set_speed", retain=True)
        with self.lock:
            self.received.update(updates)
            changed = {k: v for k, v in updates.items() if self.settings[k] != v}
            if changed:
                self.settings.update(changed)
                save_settings(self.settings)
        for k, v in changed.items():
            logger.info("Setting %s = %s", k, v)

    def _publish_missing_settings(self):
        with self.lock:
            missing = {
                key: self.settings[key] for key in DEFAULT_SETTINGS if key not in self.received
            }
        for topic, key in SETTING_TOPICS.items():
            if key in missing:
                self.client.publish(f"{MQTT_FAN_CONTROL}/{topic}", str(missing[key]), retain=True)

    def _write_pwm(self, pwm):
        if not 0 <= pwm <= 255:
            logger.warning("Refusing to write invalid PWM value %s", pwm)
            return
        for channel in self.channels:
            try:
                channel.write_text(str(pwm))
            except OSError as err:
                logger.warning("Could not write %s: %s", channel, err)

    def _report(self, now, settings, temps, action, pwm, fault):
        mode = settings["mode"]
        if mode.isdigit():
            mode = "set_speed"
        state = {
            "mode": mode,
            "pwm": pwm,
            "temps": temps,
            "cpu": self.cpu_temp,
            "source": self.controller.source,
            "fault": fault,
            "target": settings["target_temp"] if mode == "target_temp" else None,
            "integral": self.controller.integral if mode == "target_temp" else None,
            "trend": self.controller.trend_mult if mode == "target_temp" else None,
        }
        summary = (mode, pwm, fault)
        if summary != self.last_state or now - self.last_state_at >= STATE_REPUBLISH_INTERVAL:
            self.client.publish(MQTT_FAN_STATE, json.dumps(state), retain=True)
            self.last_state = summary
            self.last_state_at = now

        if summary != self.last_log or now - self.last_log_at >= HEARTBEAT_INTERVAL:
            temps_text = ",".join(str(t) for t in temps) if temps else "none"
            cpu_text = f" cpu {self.cpu_temp}°C" if self.cpu_temp is not None else ""
            if action == "managed":
                line = f"UNAS managed, drives [{temps_text}]°C{cpu_text}"
            else:
                line = (
                    f"{mode}: drives [{temps_text}]°C{cpu_text}"
                    f" -> {pwm} PWM ({pwm * 100 // 255}%)"
                )
                if mode == "target_temp" and temps:
                    line += (
                        f", {metric_value(temps, settings['temp_metric'])}°C"
                        f" {settings['temp_metric']}"
                        f" vs {settings['target_temp']}°C target, I:{self.controller.integral}"
                        f" T:{self.controller.trend_mult}"
                    )
            if fault:
                line += f" [{fault}]"
            (logger.warning if fault else logger.info)(line)
            self.last_log = summary
            self.last_log_at = now

    def _poll_cpu(self):
        try:
            temp = read_cpu_temp()
        except (OSError, ValueError) as e:
            error = f"{type(e).__name__}: {e}"
            if self.cpu_error != error:
                logger.error("CPU temperature unavailable (%s), following the drives only", error)
                self.cpu_error = error
            return None
        if self.cpu_error:
            logger.info("CPU temperature readings recovered")
            self.cpu_error = None
        return temp

    def _data_version(self, now):
        # a counter, not now // interval: that would go backwards when the interval grows
        if self.data_version_at is None or now - self.data_version_at >= self.monitor_interval:
            self.data_version += 1
            self.data_version_at = now
        return self.data_version

    def _hold_pwm(self, pwm):
        deadline = time.monotonic() + 1
        while not self.stop.wait(REASSERT_INTERVAL) and time.monotonic() < deadline:
            if pwm is not None and read_hw_pwm() != pwm:
                self._write_pwm(pwm)

    def _wait_for_retained(self):
        if self.subscribed.wait(5):
            self.stop.wait(1)
        self._publish_missing_settings()

    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        channels = ", ".join(c.name for c in self.channels) or "none"
        logger.info("Fan control starting, PWM channels: %s", channels)
        self.client.loop_start()
        connect(self.client, CONFIG)
        self._wait_for_retained()

        while not self.stop.is_set():
            now = int(time.time())
            if self.last_poll is None or time.monotonic() - self.last_poll >= TEMP_POLL_INTERVAL:
                self.temps = read_temps()
                self.cpu_temp = self._poll_cpu()
                self.last_poll = time.monotonic()

            with self.lock:
                settings = controller_settings(self.settings)
            mode_before = settings["mode"]
            action, pwm = self.controller.tick(
                now, settings, self.temps, self._data_version(now), self.cpu_temp
            )
            if settings["mode"] != mode_before:
                with self.lock:
                    self.settings["mode"] = settings["mode"]
                    save_settings(self.settings)

            pwm, fault = self.failsafe.apply(
                now, settings["mode"], self.temps, action, pwm, self.held_pwm, self.cpu_temp
            )
            if pwm is not None:
                self._write_pwm(pwm)
                self.held_pwm = pwm
                if pwm != self.controller.last_pwm and settings["mode"] == "target_temp":
                    self.controller.last_pwm = pwm
            else:
                self.held_pwm = read_hw_pwm()
            reported = pwm if pwm is not None else self.held_pwm
            self._report(now, settings, self.temps, action, reported, fault)
            self._hold_pwm(pwm)

        logger.info("Fan control stopping")
        info = self.client.publish(MQTT_FAN_AVAILABILITY, "offline", retain=True)
        try:
            info.wait_for_publish(2)
        except (RuntimeError, ValueError):
            pass
        self.client.disconnect()
        self.client.loop_stop()


def main():
    if "--handback" in sys.argv:
        if stopping_for_good():
            hand_back()
        return
    FanService().run()


if __name__ == "__main__":
    main()
