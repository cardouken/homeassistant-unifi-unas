from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncssh
import voluptuous as vol
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    DEFAULT_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_DEVICE_MODEL,
    DEFAULT_MQTT_PORT,
    DEFAULT_MQTT_TLS_PORT,
    MIN_SCAN_INTERVAL,
    MAX_SCAN_INTERVAL,
    CONF_MQTT_HOST,
    CONF_MQTT_USER,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_TLS,
    CONF_MQTT_TLS_INSECURE,
    CONF_SCAN_INTERVAL,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DEVICE_MODELS,
    HA_SSH_KEY_PATHS,
    get_mqtt_topics,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Optional(CONF_PASSWORD): str,
        vol.Required(CONF_MQTT_HOST): str,
        vol.Required(CONF_MQTT_USER): str,
        vol.Required(CONF_MQTT_PASSWORD): str,
        vol.Optional(CONF_MQTT_TLS, default=False): BooleanSelector(),
        vol.Required(CONF_DEVICE_MODEL, default=DEFAULT_DEVICE_MODEL): SelectSelector(
            SelectSelectorConfig(
                options=[{"value": k, "label": v} for k, v in DEVICE_MODELS.items()],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(CONF_DEVICE_NAME, default="UNAS"): str,
        vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, mode=NumberSelectorMode.BOX
            )
        ),
    }
)


class UNASProConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return UNASProOptionsFlow()

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        old_model = entry.data[CONF_DEVICE_MODEL]

        if user_input is not None:
            new_model = user_input[CONF_DEVICE_MODEL]

            if new_model != old_model:
                errors["base"] = "model_changed"
            elif error_key := await self._test_ssh(user_input[CONF_HOST], user_input[CONF_USERNAME],
                                                   user_input.get(CONF_PASSWORD)):
                errors["base"] = error_key
            elif user_input.get(CONF_MQTT_TLS):
                self._pending_input = user_input
                self._reconfigure_entry = entry
                return await self.async_step_reconfigure_mqtt_tls()
            elif error_key := await self._test_mqtt(user_input[CONF_MQTT_HOST], user_input[CONF_MQTT_USER],
                                                    user_input[CONF_MQTT_PASSWORD]):
                errors["base"] = error_key
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                device_name = (
                    user_input.get(CONF_DEVICE_NAME)
                    or DEVICE_MODELS[user_input[CONF_DEVICE_MODEL]]
                )
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=f"{device_name} ({user_input[CONF_HOST]})",
                    data=user_input,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        reconfigure_schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data[CONF_HOST]): str,
                vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
                vol.Optional(CONF_PASSWORD, default=entry.data.get(CONF_PASSWORD) or ""): str,
                vol.Required(CONF_MQTT_HOST, default=entry.data[CONF_MQTT_HOST]): str,
                vol.Required(CONF_MQTT_USER, default=entry.data[CONF_MQTT_USER]): str,
                vol.Required(CONF_MQTT_PASSWORD, default=entry.data[CONF_MQTT_PASSWORD]): str,
                vol.Optional(CONF_MQTT_TLS, default=entry.data.get(CONF_MQTT_TLS, False)): BooleanSelector(),
                vol.Required(CONF_DEVICE_MODEL, default=old_model): SelectSelector(
                    SelectSelectorConfig(
                        options=[{"value": k, "label": v} for k, v in DEVICE_MODELS.items()],
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(
                    CONF_DEVICE_NAME,
                    default=entry.data.get(CONF_DEVICE_NAME) or "UNAS",
                ): str,
                vol.Optional(CONF_SCAN_INTERVAL,
                             default=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): NumberSelector(
                    NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, mode=NumberSelectorMode.BOX)
                ),
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
        entry = self._reconfigure_entry

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
                await self.async_set_unique_id(merged[CONF_HOST])
                device_name = (
                    merged.get(CONF_DEVICE_NAME)
                    or DEVICE_MODELS[merged[CONF_DEVICE_MODEL]]
                )
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=f"{device_name} ({merged[CONF_HOST]})",
                    data=merged,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if mqtt.DOMAIN not in self.hass.data:
            return self.async_abort(reason="mqtt_required")

        if user_input is not None:
            if error_key := await self._test_ssh(user_input[CONF_HOST], user_input[CONF_USERNAME],
                                                 user_input.get(CONF_PASSWORD)):
                errors["base"] = error_key
            elif user_input.get(CONF_MQTT_TLS):
                self._pending_input = user_input
                return await self.async_step_mqtt_tls()
            elif error_key := await self._test_mqtt(user_input[CONF_MQTT_HOST], user_input[CONF_MQTT_USER],
                                                    user_input[CONF_MQTT_PASSWORD]):
                errors["base"] = error_key
            else:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()

                device_name = (
                    user_input.get(CONF_DEVICE_NAME)
                    or DEVICE_MODELS[user_input[CONF_DEVICE_MODEL]]
                )
                return self.async_create_entry(
                    title=f"{device_name} ({user_input[CONF_HOST]})",
                    data=user_input,
                )

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors)

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
                await self.async_set_unique_id(merged[CONF_HOST])
                self._abort_if_unique_id_configured()

                device_name = (
                    merged.get(CONF_DEVICE_NAME)
                    or DEVICE_MODELS[merged[CONF_DEVICE_MODEL]]
                )
                return self.async_create_entry(
                    title=f"{device_name} ({merged[CONF_HOST]})",
                    data=merged,
                )

        schema = vol.Schema({
            vol.Optional(CONF_MQTT_PORT, default=DEFAULT_MQTT_TLS_PORT): NumberSelector(
                NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_MQTT_TLS_INSECURE, default=False): BooleanSelector(),
        })
        return self.async_show_form(step_id="mqtt_tls", data_schema=schema, errors=errors)

    async def _test_ssh(self, host: str, username: str, password: str | None) -> str | None:
        try:
            client_keys = None
            if not password:
                for key_path in HA_SSH_KEY_PATHS:
                    if key_path.exists():
                        client_keys = [str(key_path)]
                        _LOGGER.debug("Using SSH key from %s", key_path)
                        break

            conn = await asyncio.wait_for(
                asyncssh.connect(
                    host,
                    username=username,
                    password=password if password else None,
                    client_keys=client_keys,
                    known_hosts=None,
                ),
                timeout=10.0,
            )
            result = await conn.run("echo 'test'", check=True)
            conn.close()
            await conn.wait_closed()

            if result.stdout.strip() != "test":
                return "unknown"
            return None
        except asyncssh.Error:
            return "cannot_connect"
        except asyncio.TimeoutError:
            return "timeout_connect"
        except Exception:
            return "unknown"

    async def _test_mqtt(
        self, host: str, username: str, password: str,
        port: int = DEFAULT_MQTT_PORT, use_tls: bool = False,
        tls_insecure: bool = False,
    ) -> str | None:
        try:
            import paho.mqtt.client as mqtt_client

            result = {"rc": None}

            try:
                client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION2)
                def on_connect(_client, _userdata, _flags, rc, _properties):
                    result["rc"] = rc
                    _client.disconnect()
            except (AttributeError, TypeError):
                client = mqtt_client.Client()
                def on_connect(_client, _userdata, _flags, rc):
                    result["rc"] = rc
                    _client.disconnect()

            import ssl
            client.username_pw_set(username, password)
            if use_tls:
                if tls_insecure:
                    client.tls_set(cert_reqs=ssl.CERT_NONE)
                    client.tls_insecure_set(True)
                else:
                    client.tls_set()
            client.on_connect = on_connect

            try:
                client.connect(host, int(port), 60)
            except Exception as e:
                if isinstance(e, ssl.SSLCertVerificationError):
                    _LOGGER.debug("MQTT TLS certificate verification failed: %s", e)
                    return "mqtt_tls_verify_failed"
                _LOGGER.debug("MQTT connection failed: %s", e)
                return "mqtt_cannot_connect"

            client.loop_start()
            await asyncio.sleep(3)
            client.loop_stop()

            if result["rc"] == 0:
                return None
            elif result["rc"] == 5:
                return "mqtt_invalid_auth"
            elif result["rc"] is None:
                return "mqtt_timeout"
            else:
                _LOGGER.debug("MQTT connection result code: %s", result["rc"])
                return "mqtt_cannot_connect"

        except Exception as e:
            _LOGGER.debug("MQTT test failed: %s", e)
            return "mqtt_cannot_connect"


class UNASProOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input):
        if user_input is not None:
            from homeassistant.components import mqtt

            new_interval = user_input[CONF_SCAN_INTERVAL]
            topics = get_mqtt_topics(self.config_entry.entry_id)
            await mqtt.async_publish(
                self.hass,
                f"{topics['control']}/monitor_interval",
                str(new_interval),
                qos=0,
                retain=True,
            )

            new_data = dict(self.config_entry.data)
            new_data[CONF_SCAN_INTERVAL] = new_interval
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        options_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): NumberSelector(
                    NumberSelectorConfig(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL, mode=NumberSelectorMode.BOX)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)
