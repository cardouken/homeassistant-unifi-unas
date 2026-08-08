# UniFi UNAS for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/cardouken/homeassistant-unifi-unas.svg)](https://github.com/cardouken/homeassistant-unifi-unas/releases)
[![License](https://img.shields.io/github/license/cardouken/homeassistant-unifi-unas.svg)](https://github.com/cardouken/homeassistant-unifi-unas/blob/main/LICENSE.md)
[![GitHub Stars](https://img.shields.io/github/stars/cardouken/homeassistant-unifi-unas.svg)](https://github.com/cardouken/homeassistant-unifi-unas/stargazers)

Monitoring and fan control for UniFi UNAS with native Home Assistant integration.

## Table of Contents

- [Features](#features)
- [Known Limitations](#known-limitations)
- [Supported Devices](#supported-devices)
- [What's Included](#whats-included)
- [Installation](#installation)
- [Setup](#setup)
  - [Secure MQTT (TLS)](#secure-mqtt-tls-optional)
- [Fan Control Modes](#fan-control-modes)
- [Troubleshooting](#troubleshooting)
- [Advanced](#advanced)
- [Credits](#credits)

## Features

- **One-Click Setup** - Automatic script deployment via SSH
- **Full Monitoring** - 40+ sensors for drives, system metrics, storage pools, and network shares
- **Fan Control** - Three modes with custom temperature curves
- **Auto-Recovery** - Scripts redeploy automatically on integration updates or if missing after firmware updates
- **Native Integration** - Proper HA devices and entities with MQTT discovery

## Known Limitations

- **Device Model** - Cannot be changed after initial setup. Changing requires removing and re-adding the integration.

## Supported Devices

- **UNAS Pro**
- **UNAS Pro 8**
- **UNAS Pro 4**
- **UNAS 4**
- **UNAS 2**
- **UNVR** – Unofficial support (see note below)
- **UNVR Pro** – Unofficial support (see note below)

> **UNVR Note:** The UNVR and UNVR Pro are not UNAS devices, but this integration has been confirmed to work with them
> in [#11](https://github.com/cardouken/homeassistant-unifi-unas/issues/11). Support is unofficial and may have
> limitations, and not all current or future features may work in future releases. The integration will show UniFi
> Protect version instead of UniFi Drive version and will prefix entities with `unvr_` instead of `unas_`.
>
> During setup, just use your UNVR/UNVR Pro IP and credentials.

## What's Included

### Sensors

- **System** - CPU temperature & usage, memory usage, disk I/O throughput, fan speed (PWM & percentage), uptime, OS
  version
- **Drives (HDD)** - Temperature, SMART health status, model, serial, firmware, RPM, power-on hours, bad sectors
- **Drives (NVMe)** - Temperature, SMART health, percentage used (wear), available spare, media errors, unsafe shutdowns
- **Storage** - Pool usage, size, available space, status, RAID level
- **Shares** - Per-share usage, quota (or unlimited), storage pool, member count (with member details as
  attributes), snapshot status, encryption status
- **Network** - SMB connection count (with client details as attributes), NFS mount count (with share details as
  attributes)
- **Backup Tasks** - Status, progress percentage, last run time, next scheduled run, source/destination paths

### Binary Sensors

- **Scripts Installed** - Whether monitoring scripts are deployed on UNAS
- **Monitor Service** - Whether the monitoring service is running
- **Fan Control Service** - Whether the fan control service is running

### Controls

- **Fan Mode** (Select) - UNAS Managed, Custom Curve, Target Temperature, or Set Speed
- **Target Temperature** (Number) - Desired drive temperature for Target Temperature mode (30-50°C)
- **Temperature Metric** (Select) - Max (hottest drive) or Avg (average) for Target Temperature mode
- **Response Speed** (Select) - Relaxed, Balanced, or Aggressive for Target Temperature mode
- **Fan Speed** (Number) - Manual speed for Set Speed mode (0-100%)
- **Min/Max Temperature** (Numbers) - Temperature range for Custom Curve and Target Temperature modes (20-60°C)
- **Min/Max Fan Speed** (Numbers) - Fan speed range/limits (0-100%)

> **Note:** Controls are context-sensitive—only settings relevant to your selected fan mode are adjustable.

### Switches

- **Backup Schedule** - Enable/disable scheduled backup tasks (one switch per configured backup task)

### Buttons

- **Reinstall Scripts** - Manually redeploy scripts to UNAS
- **Reboot** - Reboot the UNAS device
- **Shutdown** - Shutdown the UNAS device
- **Trigger Backup** - Manually trigger a backup task (one button per configured backup task)

![dashboard](dashboard.png)

<details>
<summary><strong>Dashboard Card YAML</strong></summary>

The YAML for this dashboard card is included in [`card.yaml`](card.yaml). Credit
to [/u/Imaginary_Explorer99 on Reddit](https://www.reddit.com/r/synology/comments/1gwpq15/home_assistant_synology_integration_dashboard/lyazspd/)
for the original concept.

**Prerequisites:**

The card uses the following custom cards from HACS:

- [Mushroom Cards](https://github.com/piitaya/lovelace-mushroom)
- [card-mod](https://github.com/thomasloven/lovelace-card-mod)

**Adding the card to your dashboard:**

1. Create a new **Section** on your dashboard
2. Click **Edit Section**
3. Click the **ellipsis (⋮)** menu
4. Select **Edit in YAML**
5. Paste the contents of `card.yaml`
6. Save

**Average Drive Temperature Sensor:**

The card includes an average drive temperature sensor that isn't part of the integration. To use it, add this template
sensor to your `configuration.yaml`:

```yaml
template:
  - sensor:
      - name: "UNAS Average Drive Temperature"
        unit_of_measurement: "°C"
        device_class: temperature
        state: >
          {% set temps = states.sensor
            | selectattr('entity_id', 'search', 'sensor\.unas_hdd_\d+_temperature')
            | map(attribute='state') | map('float', 'none') | reject('none') | list %}
          {{ temps | average | round(1) if temps else 'unavailable' }}
```

After adding, restart Home Assistant or reload template entities.

**Note:** The card is configured for a 6-drive UNAS Pro. Adjust the drive headings and entity IDs to match your setup.

</details>

## Installation

### Prerequisites

1. **MQTT Integration** (Required) - Must be installed **before** adding UniFi UNAS
    - Settings → Devices & Services → Add Integration → MQTT
    - If using Mosquitto add-on: Select automatic discovery
    - If using external broker: Enter broker details manually

2. **Mosquitto MQTT Broker** (Recommended)
    - Settings → Add-ons → Add-on Store → Mosquitto broker
    - Install, start, and enable "Start on boot"
    - Configure login credentials under Mosquitto broker add-on → Configuration → Options → Logins
    - **Note**: You can use any MQTT broker, but Mosquitto add-on is easiest. Authentication (username/password) is
      required.

3. **SSH Access to UNAS**
    - Enable SSH access in UniFi Drive via Settings → Control Plane → Console → check "SSH"
    - Either use your UNAS SSH password when setting up the integration
      OR [set up SSH key authentication](#ssh-key-authentication-optional)

### Install Integration

**Via HACS (Recommended):**

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=cardouken&repository=homeassistant-unifi-unas&category=integration)

Or manually: HACS → Integrations → Search "UniFi UNAS" → Download → Restart HA

**Manual:**

1. Download latest release
2. Extract to `custom_components/unifi_unas/`
3. Restart HA

## Setup

### Add Integration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=unifi_unas)

Or manually: Settings → Devices & Services → Add Integration → Search "UniFi UNAS"

Enter details:

- **Host**: UNAS IP (e.g., `192.168.1.25`)
- **Username**: `root`
- **Password**: Your UNAS SSH password (optional, leave blank if using SSH keys)
- **MQTT Host**: IP address of your MQTT broker (e.g., `192.168.1.111`, will be your HA IP if using Mosquitto add-on)
- **MQTT User**: Your Mosquitto username (required)
- **MQTT Password**: Your Mosquitto password (required)
- **Use TLS**: Enable if your MQTT broker uses TLS (see [Secure MQTT (TLS)](#secure-mqtt-tls-optional))
- **Device Model**: Select your UNAS model from the dropdown
- **Polling Interval**: How often to poll for metrics (5-60 seconds)

The integration will automatically:

- Deploy scripts to UNAS via SSH
- Configure systemd services
- Set up MQTT auto-discovery
- Create all devices and entities

### SSH Key Authentication (Optional)

Instead of using a password, you can configure SSH key authentication for more secure, passwordless connections.

**Setup:**

1. Generate an SSH key pair (if you don't have one):
   ```bash
   ssh-keygen -t ed25519
   ```

2. Copy the public key to your UNAS:
   ```bash
   ssh-copy-id root@YOUR_UNAS_IP
   ```

3. Place the private key where Home Assistant can find it:
    - **HAOS/Supervised**: `/config/.ssh/id_ed25519` or `/config/.ssh/id_rsa`
    - **Core/Docker**: `~/.ssh/id_ed25519` or `~/.ssh/id_rsa`

4. Leave the password field empty during integration setup.

The integration will automatically detect and use the SSH key.

### SSH Host Key Verification (Optional)

By default the SSH connection to the NAS does not verify the host key. If you'd like the integration to verify it, enable **Verify SSH host key** during setup (or reconfigure). On the first successful connection the NAS host key is recorded (trust-on-first-use) and stored with the config entry; every connection afterwards is checked against it, so a man-in-the-middle that presents a different key is rejected.

Alternatively, you can point **SSH known_hosts file** at a `known_hosts` file on the Home Assistant host (for example one you manage yourself); when set, it is used to verify the host key and takes precedence over trust-on-first-use.

If the NAS host key legitimately changes (for example after a firmware reinstall), remove and re-add the integration - or clear the stored key - so a new key can be pinned. Both options are off/blank by default, preserving the current behavior.

### Secure MQTT (TLS) (Optional)

If your MQTT broker is configured for TLS (e.g., port 8883), enable the **Use TLS** toggle during setup. After submitting the main form, you'll be prompted for:

- **MQTT Broker Port** - defaults to 8883
- **Allow untrusted certificate** - enable if your broker uses a self-signed certificate

This applies to both the config flow connection test and the on-device scripts deployed to your UNAS. The standard plaintext setup (port 1883) requires no changes, just leave **Use TLS** off.

### Externalizing MQTT Credentials (Optional)

By default, the MQTT credentials are written directly into the monitoring scripts that are deployed to the NAS (`/root/unas_monitor.py`, `/root/fan_control.sh`). If you'd rather keep the MQTT password out of those on-device script bodies - for example because you manage secrets with a password manager or an env file - you can supply a **credentials file** instead.

**How it works:**

1. Create a root-readable env file on the Home Assistant host (for example under `/share`), defining at least the MQTT username and password:
   ```bash
   # /share/unifi_unas/mqtt.env
   MQTT_USER=your_mqtt_user
   MQTT_PASS=your_mqtt_password
   ```
   You may also override any other setting the scripts read: `MQTT_HOST`, `MQTT_PORT`, `MQTT_TLS`, `MQTT_TLS_INSECURE`, `MQTT_ROOT`.

2. Enter that path in the **MQTT credentials file** field during setup (or reconfigure).

When set, the integration uploads that file to a root-only (`chmod 600`) file on the NAS (`/root/.unas_monitor.env`) and the systemd services load it via `EnvironmentFile=`. The scripts read the MQTT settings from the environment, so the username and password are no longer inlined into the deployed script bodies. Any value not provided by the env file falls back to what you entered in the integration.

Leave the field blank for the standard setup (credentials are written into the scripts as before).

## Fan Control Modes

### 1. UNAS Managed

Lets UNAS control the fans automatically (default behavior). Use this if you only want monitoring without fan control.

### 2. Custom Curve

Linear temperature-based fan curve. The fan speed scales linearly between min and max based on temperature:

```
Fan Speed
    ▲
max ┤........╱
    │     ╱
    │   ╱
min ┤─╱
    └─────────────► Temperature
      min    max
```

**Configure via:** Settings → Devices & Services → UniFi UNAS → Device → Adjust the four curve parameters

**Example presets:**

| Preset     | Min Temp | Max Temp | Min Fan | Max Fan |
|------------|----------|----------|---------|---------|
| Quiet      | 40°C     | 50°C     | 15%     | 30%     |
| Balanced   | 38°C     | 48°C     | 30%     | 70%     |
| Aggressive | 35°C     | 45°C     | 70%     | 100%    |

### 3. Target Temperature

Automatically adjusts fan speed to maintain your drives at a specific temperature. Uses a PI (Proportional-Integral)
control algorithm that adapts to the current environment and adjusts fan speeds accordingly.

Unlike Custom Curve which simply reacts to current temps, Target Temperature actively works toward a goal and ramps up
when needed and backs off when stable.

**Available settings:**

| Setting                | Description                                                   |
|------------------------|---------------------------------------------------------------|
| **Target Temperature** | The temperature you want to maintain (20-50°C, default: 40°C) |
| **Temperature Metric** | **Max** = hottest drive, **Average** = average of all drives  |
| **Response Speed**     | How aggressively the controller reacts (see below)            |
| **Min/Max Fan Speed**  | Limits for the controller (it won't go outside this range)    |

**Response Speed options:**

| Option         | Behavior                    | Best For                                 |
|----------------|-----------------------------|------------------------------------------|
| **Relaxed**    | Slow, gentle changes        | Noise-sensitive setups, stable workloads |
| **Balanced**   | Moderate response (default) | Most users                               |
| **Aggressive** | Fast, reactive changes      | Variable workloads, maximum cooling      |

**How it works:**

- Above target → Fans ramp up (faster when further from target)
- At target → Fans hold steady
- Below target → Fans gradually reduce

The controller includes safeguards (rate limiting, anti-windup, warm start for smooth transitions, integral and output
clamping) against oscillation and overshooting. It typically reaches steady-state within 15-30 minutes of a target
change, depending on your environment and targets.

**When to use Target Temperature:**

- You want "set and forget" temperature control
- Your workload/environment varies (the controller adapts automatically)
- You care more about a specific temperature than fan speeds (reasonable temp targets will still find the lowest fan
  speed)

### 4. Set Speed

Lock fans to a fixed speed (0-100%). Use the Fan Speed slider to set the desired speed.

### Fan Safety / Failsafe

Whenever the fan-control daemon isn't actively driving the fans, control is handed back to the NAS firmware so the drives are never left under a stale fan speed:

- When the daemon stops, crashes, or is disabled, a systemd `ExecStopPost` hook (and the script's own exit handler) restores every fan channel to automatic mode (`pwmN_enable=2`).
- The daemon explicitly takes manual control (`pwmN_enable=1`) when it writes a PWM value, and hands control back to automatic when you switch to **UNAS Managed** mode.
- A systemd watchdog (`WatchdogSec`) restarts the daemon if it hangs, rather than leaving a frozen PWM in place.

**Minimum fan PWM floor (optional):** you can set a **Minimum fan PWM floor** (0-255, `0` disables it) in the integration options. When set, it is enforced in every actively-controlled mode (Custom Curve, Target Temperature, Set Speed), so a misconfigured curve or a low set speed can't drive the fans below a safe ratio. It does not apply to **UNAS Managed** mode, where the firmware controls the fans.

## Troubleshooting

### Scripts Not Installing

Check logs: Settings → System → Logs → search "unifi_unas"

Common issues:

- **Cannot connect** → Verify UNAS IP and root password
- **Timeout** → Check SSH access (port 22)
- **Permission denied** → Must use `root` account

### Sensors Not Appearing

1. Verify MQTT integration is installed (Settings → Devices & Services → MQTT)
2. Verify Mosquitto broker is running
3. Check MQTT credentials are correct in integration config
4. Check service status on UNAS:
   ```bash
   ssh root@YOUR_UNAS_IP
   systemctl status unas_monitor fan_control
   ```

### Drives Not Appearing

New or moved drives may take up to 60 seconds to appear (grace period for detection).

### Wrong Bay Numbers

Your device model may have incorrect bay mappings. See [Supported Devices](#supported-devices) section to help confirm
mappings.

### MQTT Integration Removed

If you remove the MQTT integration after setup, a repair issue will appear. Reinstall MQTT and reload the integration.

### After Firmware Update

Scripts redeploy automatically on startup if missing. If needed, manually reinstall via the "Reinstall Scripts" button
on the device page.

### Removing Integration

Removing the integration fully restores your UNAS to stock. The cleanup process:

1. **Stops and disables services** - `unas_monitor` and `fan_control` systemd services
2. **Removes all scripts** - `/root/unas_monitor.py`, `/root/fan_control.sh`
3. **Removes service files** - From `/etc/systemd/system/`
4. **Removes temp files** - State files from `/tmp/`
5. **Uninstalls packages** - `mosquitto-clients`, `paho-mqtt`, `python3-pip`
6. **Restores fan control** - Returns PWM control to UNAS-managed mode

No manual cleanup is required.

## Advanced

<details>
<summary><strong>Headless collector (--once --json)</strong></summary>

The monitor script can run a single collection cycle and print the full metric set as JSON, without connecting to MQTT. This is handy for troubleshooting, testing, and external consumers that want the same data over SSH:

```bash
ssh root@YOUR_UNAS_IP "python3 /root/unas_monitor.py --once --json"
```

It prints a JSON object with `system`, `hdd`, `nvme`, `pools`, and (on non-UNVR devices) `smb`, `nfs`, and `shares`, then exits. No MQTT connection is made and the fan-control shared temp file is not touched.

</details>

<details>
<summary><strong>Monitoring-only / independent services</strong></summary>

The two on-device services can be deployed independently via the **Enable monitoring service** and **Enable fan-control service** options (both on by default). Turning **Enable fan-control service** off gives you a monitoring-only install: the fan-control script is not deployed to the NAS and the fans stay fully UNAS-managed. Toggling a service off later stops, disables, and removes it on the next reload (and hands the fans back to firmware control when fan-control is disabled).

</details>

<details>
<summary><strong>MQTT Topics</strong></summary>

Topics use the prefix `unas/{entry_id}/` where `entry_id` is the first 8 characters of your config entry ID.

```
unas/{id}/
├── availability          # "online" or "offline"
├── system/               # CPU, memory, disk I/O, fan, uptime
├── hdd/{bay}/            # Per-drive SMART data
├── nvme/{slot}/          # NVMe drive data
├── pool/{num}/           # Storage pool stats
├── share/{name}/         # Per-share usage, quota, members
├── smb/                  # SMB connections
├── nfs/                  # NFS mounts
└── control/
    ├── monitor_interval  # Polling interval
    └── fan/              # Fan mode and curve parameters
```

</details>

<details>
<summary><strong>Debug Logging</strong></summary>

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.unifi_unas: debug
```

</details>

<details>
<summary><strong>Script Locations</strong></summary>

Scripts are deployed to `/root/` on the UNAS:

- `/root/unas_monitor.py` - Monitoring script
- `/root/fan_control.sh` - Fan control script

Systemd service files:

- `/etc/systemd/system/unas_monitor.service`
- `/etc/systemd/system/fan_control.service`

Manual service control:

```bash
systemctl status unas_monitor fan_control
systemctl restart unas_monitor
systemctl restart fan_control
```

</details>

## Credits

- **Fan control concept**: [hoxxep/UNAS-Pro-fan-control](https://github.com/hoxxep/UNAS-Pro-fan-control)

## License

MIT - See [LICENSE.md](LICENSE.md)

## Support

- [GitHub Issues](https://github.com/cardouken/homeassistant-unifi-unas/issues)
