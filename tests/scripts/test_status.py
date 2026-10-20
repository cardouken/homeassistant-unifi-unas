import hashlib
import http.client
import json
import subprocess

import pytest

import status
from fakes import fake_connection, json_body

TASK = {"id": "198597d5-df3e", "name": "B2 backup", "schedule": {"enable": True, "firstRunTime": "2:30"}}


@pytest.fixture
def users(tmp_path, monkeypatch):
    path = tmp_path / "users.json"
    path.write_text(json.dumps([{"id": "member", "isOwner": False}, {"id": "owner", "isOwner": True}]))
    monkeypatch.setattr(status, "USERS_FILE", str(path))
    return path


@pytest.fixture
def api(monkeypatch, users):
    requests = []

    def install(**kwargs):
        monkeypatch.setattr(status.http.client, "HTTPConnection", fake_connection(requests=requests, **kwargs))
        return requests

    return install


def test_status_reports_services_files_and_tasks(tmp_path, monkeypatch, api):
    monkeypatch.setattr(
        status.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout="active\n")
    )
    requests = api(body=json_body({"err": None, "data": [TASK]}))
    script = tmp_path / "fan_control.py"
    script.write_bytes(b"print()\n")

    result = status.status([str(script), str(tmp_path / "missing.py")])

    assert result["services"] == {"unas_monitor": "active", "fan_control": "active"}
    assert result["files"] == {
        str(script): hashlib.sha256(b"print()\n").hexdigest(),
        str(tmp_path / "missing.py"): None,
    }
    assert result["backup_tasks"] == [TASK]
    assert requests[0][3]["X-UserId"] == "owner"


def test_failed_backup_api_is_unknown_not_empty(api):
    api(status=403, body=json_body({"err": {"msg": "invalid auth user ID"}, "data": None}))
    result = status.status([])
    assert result["backup_tasks"] is None
    assert "403" in result["backup_error"]


def test_a_broken_backup_api_response_is_unknown_not_a_crash(api):
    api(error=http.client.BadStatusLine("garbage"))
    result = status.status([])
    assert result["backup_tasks"] is None
    assert "garbage" in result["backup_error"]


def test_owner_falls_back_to_the_first_user(users):
    users.write_text(json.dumps([{"id": "first"}, {"id": "second"}]))
    assert status.owner_id() == "first"


def test_set_schedule_only_changes_enable(api):
    requests = api(body=lambda method, path: json_body({"err": None, "data": [TASK] if method == "GET" else {}}))
    assert status.main(["set-schedule", TASK["id"], "off"]) == 0
    method, path, body, _ = requests[-1]
    assert (method, path) == ("PATCH", f"/api/v1/remote-backup/tasks/{TASK['id']}")
    assert json.loads(body) == {"schedule": {"enable": False, "firstRunTime": "2:30"}}


def test_backup_must_report_ok(api, capsys):
    api(body=json_body({"err": None, "data": "QUEUED"}))
    assert status.main(["run-backup", TASK["id"]]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "backup did not start"}


@pytest.mark.parametrize("argv", [
    ["run-backup", "../../api/v1/system"],
    ["set-schedule", TASK["id"], "maybe"],
    ["set-schedule", "a b", "on"],
    ["reboot"],
])
def test_bad_arguments_never_reach_the_api(api, argv):
    requests = api(body=json_body({"err": None, "data": "OK"}))
    assert status.main(argv) == 1
    assert requests == []
