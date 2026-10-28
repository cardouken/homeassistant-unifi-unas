from __future__ import annotations

import logging
import ssl
import threading
import time
from collections.abc import Mapping
from typing import Any

import asyncssh
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    CONF_HOST_KEY,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_TLS,
    CONF_MQTT_TLS_INSECURE,
    CONF_MQTT_USER,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_HOST_KEY,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TLS_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_USERNAME,
    DEVICE_MODELS,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SYSID_MODELS,
)
from .ssh_manager import NASCommandError, SSHManager

_LOGGER = logging.getLogger(__name__)

MQTT_PROBE_TIMEOUT = 5
SSH_PROBE_TIMEOUT = 10
SECRET_FIELDS = (CONF_PASSWORD, CONF_MQTT_PASSWORD)

MODEL_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[{"value": k, "label": v} for k, v in DEVICE_MODELS.items()],
        mode=SelectSelectorMode.DROPDOWN,
    )
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Required(CONF_MQTT_HOST): str,
        vol.Required(CONF_MQTT_USER): str,
        vol.Required(CONF_MQTT_PASSWORD): str,
        vol.Optional(CONF_MQTT_TLS, default=False): BooleanSelector(),
        vol.Optional(CONF_DEVICE_NAME, default="UNAS"): str,
        vol.Optional(CONF_VERIFY_HOST_KEY, default=False): BooleanSelector(),
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, mode=NumberSelectorMode.BOX
            )
        ),
    }
)


class UNASProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UNASProOptionsFlow()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            # the form never sends passwords back, a blank one keeps what is stored
            data = {
                **entry.data,
                **{
                    key: value for key, value in user_input.items()
                    if value or key not in SECRET_FIELDS
                },
            }
            error_key, identity = await self._probe_ssh(
                data[CONF_HOST], data[CONF_USERNAME], data.get(CONF_PASSWORD)
            )
            serial = ((identity or {}).get("serial") or "").lower()
            if error_key:
                errors["base"] = error_key
            elif serial and entry.unique_id not in (serial, entry.data[CONF_HOST]):
                return self.async_abort(reason="wrong_device")
            else:
                if data[CONF_HOST] != entry.data[CONF_HOST]:
                    # the pin belongs to the old address, the next connection pins again
                    data.pop(CONF_HOST_KEY, None)
                if not data.get(CONF_MQTT_TLS):
                    # the TLS step set these, plain MQTT is on the default port
                    data.pop(CONF_MQTT_PORT, None)
                    data.pop(CONF_MQTT_TLS_INSECURE, None)
                self._pending_input = data
                self._serial = serial or None
                if data.get(CONF_MQTT_TLS):
                    return await self.async_step_reconfigure_mqtt_tls()
                if error_key := await self._test_mqtt(
                    data[CONF_MQTT_HOST], data[CONF_MQTT_USER], data[CONF_MQTT_PASSWORD]
                ):
                    errors["base"] = error_key
                else:
                    return self._update_reconfigured(data)

        reconfigure_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(
                    CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
                ): str,
                vol.Optional(CONF_PASSWORD): str,
                vol.Required(CONF_MQTT_HOST, default=entry.data[CONF_MQTT_HOST]): str,
                vol.Required(CONF_MQTT_USER, default=entry.data[CONF_MQTT_USER]): str,
                vol.Optional(CONF_MQTT_PASSWORD): str,
                vol.Optional(
                    CONF_MQTT_TLS, default=entry.data.get(CONF_MQTT_TLS, False)
                ): BooleanSelector(),
                vol.Optional(
                    CONF_DEVICE_NAME, default=entry.data.get(CONF_DEVICE_NAME) or "UNAS"
                ): str,
                vol.Optional(
                    CONF_VERIFY_HOST_KEY,
                    default=entry.data.get(CONF_VERIFY_HOST_KEY, False),
                ): BooleanSelector(),
            }
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=reconfigure_schema,
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    async def async_step_reconfigure_mqtt_tls(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            merged = {**self._pending_input, **user_input}
            if error_key := await self._test_mqtt(
                merged[CONF_MQTT_HOST], merged[CONF_MQTT_USER],
                merged[CONF_MQTT_PASSWORD],
                port=merged.get(CONF_MQTT_PORT, DEFAULT_MQTT_TLS_PORT),
                use_tls=True,
                tls_insecure=merged.get(CONF_MQTT_TLS_INSECURE, False),
            ):
                errors["base"] = error_key
            else:
                return self._update_reconfigured(merged)

        schema = vol.Schema({
            vol.Optional(
                CONF_MQTT_PORT,
                default=entry.data.get(CONF_MQTT_PORT, DEFAULT_MQTT_TLS_PORT),
            ): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(
                CONF_MQTT_TLS_INSECURE,
                default=entry.data.get(CONF_MQTT_TLS_INSECURE, False),
            ): BooleanSelector(),
        })
        return self.async_show_form(
            step_id="reconfigure_mqtt_tls", data_schema=schema, errors=errors
        )

    def _update_reconfigured(self, data: dict[str, Any]) -> FlowResult:
        entry = self._get_reconfigure_entry()
        device_name = data.get(CONF_DEVICE_NAME) or DEVICE_MODELS[data[CONF_DEVICE_MODEL]]
        # the update listener reloads when connection settings changed
        return self.async_update_and_abort(
            entry,
            unique_id=self._serial or entry.unique_id,
            title=f"{device_name} ({data[CONF_HOST]})",
            data=data,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if mqtt.DOMAIN not in self.hass.data:
            return self.async_abort(reason="mqtt_required")

        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            error_key, identity = await self._probe_ssh(
                user_input[CONF_HOST], user_input[CONF_USERNAME], user_input.get(CONF_PASSWORD)
            )
            if error_key:
                errors["base"] = error_key
            else:
                serial = (identity or {}).get("serial")
                await self.async_set_unique_id(serial.lower() if serial else user_input[CONF_HOST])
                # the same NAS on a new address just gets its host updated
                self._abort_if_unique_id_configured(updates={CONF_HOST: user_input[CONF_HOST]})
                self._pending_input = dict(user_input)
                self._identity = identity
                model = SYSID_MODELS.get((identity or {}).get("sysid"))
                if model is None:
                    return await self.async_step_unrecognized_device()
                self._pending_input[CONF_DEVICE_MODEL] = model
                return await self._async_step_mqtt()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_unrecognized_device(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        # installing onto some other UniFi console would leave two publishers on one topic, see #25
        if user_input is not None:
            self._pending_input[CONF_DEVICE_MODEL] = user_input[CONF_DEVICE_MODEL]
            return await self._async_step_mqtt()
        identity = self._identity or {}
        return self.async_show_form(
            step_id="unrecognized_device",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_MODEL, default=DEFAULT_DEVICE_MODEL): MODEL_SELECTOR}
            ),
            description_placeholders={
                "host": self._pending_input[CONF_HOST],
                "device": identity.get("product") or identity.get("name") or "an unknown device",
            },
        )

    async def _async_step_mqtt(self) -> FlowResult:
        if self._pending_input.get(CONF_MQTT_TLS):
            return await self.async_step_mqtt_tls()
        user_input = self._pending_input
        if error_key := await self._test_mqtt(
            user_input[CONF_MQTT_HOST], user_input[CONF_MQTT_USER], user_input[CONF_MQTT_PASSWORD]
        ):
            return self.async_show_form(
                step_id="user",
                data_schema=self.add_suggested_values_to_schema(STEP_USER_DATA_SCHEMA, user_input),
                errors={"base": error_key},
            )
        return self._create_entry(user_input)

    async def async_step_mqtt_tls(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            merged = {**self._pending_input, **user_input}
            if error_key := await self._test_mqtt(
                merged[CONF_MQTT_HOST], merged[CONF_MQTT_USER],
                merged[CONF_MQTT_PASSWORD],
                port=merged.get(CONF_MQTT_PORT, DEFAULT_MQTT_TLS_PORT),
                use_tls=True,
                tls_insecure=merged.get(CONF_MQTT_TLS_INSECURE, False),
            ):
                errors["base"] = error_key
            else:
                return self._create_entry(merged)

        schema = vol.Schema({
            vol.Optional(CONF_MQTT_PORT, default=DEFAULT_MQTT_TLS_PORT): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_MQTT_TLS_INSECURE, default=False): BooleanSelector(),
        })
        return self.async_show_form(step_id="mqtt_tls", data_schema=schema, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
            self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            password = user_input.get(CONF_PASSWORD) or ""
            error_key, identity = await self._probe_ssh(
                entry.data[CONF_HOST], user_input[CONF_USERNAME], password
            )
            serial = ((identity or {}).get("serial") or "").lower()
            if error_key:
                errors["base"] = error_key
            elif serial and entry.unique_id not in (serial, entry.data[CONF_HOST]):
                return self.async_abort(reason="wrong_device")
            else:
                updates = {CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: password}
                if all(entry.data.get(key) == value for key, value in updates.items()):
                    # same login, fixed on the NAS side; polling stopped at the auth failure
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                # changed credentials reload through the update listener
                return self.async_update_and_abort(entry, data_updates=updates)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)
                ): str,
                vol.Optional(CONF_PASSWORD): str,
            }),
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    def _create_entry(self, user_input: dict[str, Any]) -> FlowResult:
        data = {key: value for key, value in user_input.items() if key != CONF_SCAN_INTERVAL}
        interval = int(user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        device_name = (
            user_input.get(CONF_DEVICE_NAME) or DEVICE_MODELS[user_input[CONF_DEVICE_MODEL]]
        )
        return self.async_create_entry(
            title=f"{device_name} ({user_input[CONF_HOST]})",
            data=data,
            options={CONF_SCAN_INTERVAL: interval},
        )

    async def _probe_ssh(
            self, host: str, username: str, password: str | None
    ) -> tuple[str | None, dict[str, Any] | None]:
        # one login: credentials, and what the console is
        manager = SSHManager(host, username, password or None, connect_timeout=SSH_PROBE_TIMEOUT)
        try:
            return None, await manager.identity()
        except asyncssh.PermissionDenied:
            return "invalid_auth", None
        except TimeoutError:
            return "timeout_connect", None
        except NASCommandError as err:
            # reachable, but not a console that answers /api/system
            _LOGGER.debug("Could not identify %s: %s", host, err)
            return None, None
        except (asyncssh.Error, OSError):
            return "cannot_connect", None
        except Exception:
            _LOGGER.exception("Unexpected error connecting to %s", host)
            return "unknown", None
        finally:
            await manager.disconnect()

    async def _test_mqtt(
        self, host: str, username: str, password: str,
        port: int = DEFAULT_MQTT_PORT, use_tls: bool = False,
        tls_insecure: bool = False,
    ) -> str | None:
        return await self.hass.async_add_executor_job(
            probe_mqtt, host, int(port), username, password, use_tls, tls_insecure
        )


def probe_mqtt(
        host: str, port: int, username: str, password: str, use_tls: bool, tls_insecure: bool
) -> str | None:
    # blocking socket and TLS setup, runs in the executor
    import paho.mqtt.client as mqtt_client

    connected = threading.Event()
    result: dict[str, Any] = {}

    def on_connect(_client, _userdata, _flags, reason_code, _properties):
        result["reason_code"] = reason_code
        connected.set()

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
    client.username_pw_set(username, password)
    client.connect_timeout = MQTT_PROBE_TIMEOUT
    client.on_connect = on_connect
    try:
        if use_tls:
            if tls_insecure:
                client.tls_set(cert_reqs=ssl.CERT_NONE)
                client.tls_insecure_set(True)
            else:
                client.tls_set()
        client.connect(host, port, keepalive=30)
        deadline = time.monotonic() + MQTT_PROBE_TIMEOUT
        while not connected.is_set() and time.monotonic() < deadline:
            client.loop(timeout=0.2)
    except ssl.SSLCertVerificationError as err:
        _LOGGER.debug("MQTT TLS certificate verification failed: %s", err)
        return "mqtt_tls_verify_failed"
    except (OSError, ValueError) as err:
        _LOGGER.debug("MQTT connection failed: %s", err)
        return "mqtt_cannot_connect"
    finally:
        client.disconnect()

    reason_code = result.get("reason_code")
    if reason_code is None:
        return "mqtt_timeout"
    if not reason_code.is_failure:
        return None
    # 134 bad user name or password, 135 not authorized (MQTT 3.1.1 codes 4 and 5)
    if reason_code.value in (134, 135):
        return "mqtt_invalid_auth"
    _LOGGER.debug("MQTT connection refused: %s", reason_code)
    return "mqtt_cannot_connect"


class UNASProOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            # applied by the update listener, no reload
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)
