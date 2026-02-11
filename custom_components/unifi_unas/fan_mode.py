from __future__ import annotations

import logging

from homeassistant.components import mqtt
from homeassistant.core import callback

_LOGGER = logging.getLogger(__name__)


class FanModeMixin:
    _current_mode: str | None = None
    _unsubscribe_mode = None

    async def _subscribe_fan_mode(self) -> None:
        @callback
        def mode_message_received(msg):
            payload = msg.payload
            if payload == "unas_managed":
                self._current_mode = "unas_managed"
            elif payload == "auto":
                self._current_mode = "auto"
            elif payload == "target_temp":
                self._current_mode = "target_temp"
            elif payload.isdigit():
                self._current_mode = "set_speed"
            else:
                self._current_mode = None
            self.async_write_ha_state()

        self._unsubscribe_mode = await mqtt.async_subscribe(
            self.hass, f"{self._topics['control']}/fan/mode", mode_message_received, qos=0
        )

    def _unsubscribe_fan_mode(self) -> None:
        if self._unsubscribe_mode:
            try:
                self._unsubscribe_mode()
            except Exception as err:
                _LOGGER.debug("Error unsubscribing from fan mode: %s", err)
