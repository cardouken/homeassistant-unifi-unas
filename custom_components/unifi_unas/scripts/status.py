#!/usr/bin/env python3
# Home Assistant runs this over SSH from stdin, so it works even when the bundle is missing

import hashlib
import http.client
import json
import re
import subprocess
import sys

SERVICES = ("unas_monitor", "fan_control")
USERS_FILE = "/data/unifi-core/config/cache/users.json"
UIDB_FILE = "/usr/share/unifi-core/app/data/uidb.json"
TASKS_PATH = "/api/v1/remote-backup/tasks"
TASK_ID = re.compile(r"[\w-]{1,64}")
USAGE = "usage: status [paths] | identity | run-backup <id> | set-schedule <id> on|off"


class ApiError(Exception):
    pass


def file_hashes(paths):
    hashes = {}
    for path in paths:
        try:
            with open(path, "rb") as f:
                hashes[path] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            hashes[path] = None
    return hashes


def service_states():
    states = {}
    for service in SERVICES:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", service], capture_output=True, text=True, timeout=10
            )
            states[service] = result.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            states[service] = "unknown"
    return states


def owner_id():
    try:
        with open(USERS_FILE) as f:
            users = json.load(f)
    except (OSError, ValueError) as err:
        raise ApiError(f"cannot read {USERS_FILE}: {err}")
    if not isinstance(users, list):
        raise ApiError(f"unexpected {USERS_FILE} format")
    users = [user for user in users if isinstance(user, dict) and user.get("id")]
    owners = [user for user in users if user.get("isOwner")] or users
    if not owners:
        raise ApiError("no console user found")
    return owners[0]["id"]


def backup_api(method, path, body=None):
    headers = {
        "X-UserId": owner_id(),
        "X-UserRole": "owner",
        "X-UserAccessMask": "114654",
        "X-UserPermissionMask": "16382",
    }
    if body is not None:
        body = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", 16080, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
    except (OSError, http.client.HTTPException) as err:
        raise ApiError(f"{method} {path}: {err}")
    finally:
        conn.close()
    try:
        data = json.loads(raw)
    except ValueError:
        raise ApiError(f"{method} {path}: HTTP {resp.status}, not JSON")
    if resp.status != 200 or not isinstance(data, dict) or data.get("err"):
        error = data.get("err") if isinstance(data, dict) else None
        raise ApiError(f"{method} {path}: HTTP {resp.status} {json.dumps(error)}")
    return data.get("data")


def backup_tasks():
    tasks = backup_api("GET", TASKS_PATH)
    if not isinstance(tasks, list):
        raise ApiError(f"GET {TASKS_PATH}: no task list")
    return [task for task in tasks if isinstance(task, dict) and task.get("id")]


def identity():
    # what the console is, for the setup flow: model detection and the entry's unique_id
    conn = http.client.HTTPConnection("127.0.0.1", 11081, timeout=5)
    try:
        conn.request("GET", "/api/system")
        resp = conn.getresponse()
        data = json.loads(resp.read()) if resp.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException) as err:
        raise ApiError(f"GET /api/system: {err}")
    finally:
        conn.close()
    hardware = (data or {}).get("hardware") if isinstance(data, dict) else None
    if not isinstance(hardware, dict) or not hardware:
        raise ApiError("GET /api/system: no hardware info")
    sysid = hardware.get("sysid")
    sysid = "%04x" % sysid if isinstance(sysid, int) else None
    result = {
        "sysid": sysid,
        "serial": hardware.get("serialno"),
        "shortname": hardware.get("shortname"),
        "name": hardware.get("name"),
        "product": None,
        "sku": None,
    }
    try:
        with open(UIDB_FILE) as f:
            devices = json.load(f).get("devices") or []
    except (OSError, ValueError, AttributeError):
        devices = []
    for device in devices:
        if isinstance(device, dict) and sysid and device.get("sysid") == sysid:
            product = device.get("product") or {}
            result["product"] = product.get("abbrev") or product.get("name")
            result["sku"] = device.get("sku")
            break
    return result


def status(paths):
    result = {"services": service_states(), "files": file_hashes(paths)}
    try:
        result["backup_tasks"] = backup_tasks()
    except ApiError as err:
        result["backup_tasks"] = None
        result["backup_error"] = str(err)
    return result


def run_backup(task_id):
    if backup_api("POST", f"/api/v1/remote-backup/run-task/{task_id}") != "OK":
        raise ApiError("backup did not start")
    return {}


def set_schedule(task_id, enabled):
    task = next((task for task in backup_tasks() if task["id"] == task_id), None)
    if task is None:
        raise ApiError(f"backup task {task_id} not found")
    schedule = dict(task.get("schedule") or {})
    schedule["enable"] = enabled
    backup_api("PATCH", f"{TASKS_PATH}/{task_id}", {"schedule": schedule})
    return {}


def main(argv):
    command, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if command == "status":
            result = status(args)
        elif command == "identity":
            result = identity()
        elif command == "run-backup" and len(args) == 1 and TASK_ID.fullmatch(args[0]):
            result = run_backup(args[0])
        elif command == "set-schedule" and len(args) == 2 and TASK_ID.fullmatch(args[0]):
            if args[1] not in ("on", "off"):
                raise ApiError(f"invalid schedule state {args[1]!r}")
            result = set_schedule(args[0], args[1] == "on")
        else:
            result = {"error": USAGE}
    except ApiError as err:
        result = {"error": str(err)}
    print(json.dumps(result))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
