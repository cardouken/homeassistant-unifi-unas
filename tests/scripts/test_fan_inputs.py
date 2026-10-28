import http.client
import subprocess

import pytest

import fan_control as fc
from fakes import fake_connection, json_body


def test_drive_api_skips_empty_slots(monkeypatch):
    body = json_body({"disks": [
        {"state": "optimal", "temperature": 44},
        {"state": "empty", "temperature": 0},
        {"state": "degraded", "temperature": 51},
    ]})
    monkeypatch.setattr(fc.http.client, "HTTPConnection", fake_connection(body=body))
    assert fc.drive_api_temps() == [44, 51]


@pytest.mark.parametrize("connection", [
    fake_connection(status=500),
    fake_connection(body=b"<html>"),
    fake_connection(error=OSError("connection refused")),
    fake_connection(error=http.client.IncompleteRead(b"{")),
    fake_connection(body=json_body({"pools": []})),
    fake_connection(body=json_body([1, 2])),
])
def test_drive_api_failures_give_no_temps(monkeypatch, connection):
    monkeypatch.setattr(fc.http.client, "HTTPConnection", connection)
    assert fc.drive_api_temps() == []


def test_read_temps_prefers_the_storage_api(monkeypatch):
    def smartctl():
        raise AssertionError("smartctl should not run")

    monkeypatch.setattr(fc, "drive_api_temps", lambda: [41, None, 44])
    monkeypatch.setattr(fc, "smartctl_temps", smartctl)
    assert fc.read_temps() == [44, 41]


def test_read_temps_falls_back_to_smartctl(monkeypatch):
    monkeypatch.setattr(fc, "drive_api_temps", lambda: [0, None])
    monkeypatch.setattr(fc, "smartctl_temps", lambda: [40, None, 42])
    assert fc.read_temps() == [42, 40]


@pytest.mark.parametrize("jobs,expected", [
    ("11629 fan_control.service stop running\n", True),
    ("11461 fan_control.service restart running\n", False),
    ("", False),
])
def test_hand_back_only_on_an_explicit_stop(monkeypatch, jobs, expected):
    monkeypatch.setattr(
        fc.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=jobs),
    )
    assert fc.stopping_for_good() is expected


def test_hand_back_when_systemctl_fails(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("systemctl missing")

    monkeypatch.setattr(fc.subprocess, "run", broken)
    assert fc.stopping_for_good() is True
