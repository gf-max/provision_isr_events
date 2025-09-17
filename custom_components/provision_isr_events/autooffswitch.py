from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.event import async_call_later

_LOGGER = logging.getLogger(__name__)


class AutoOffSwitch(SwitchEntity):
    """A switch entity that automatically turns OFF after a configurable delay."""

    _attr_has_entity_name = True
    _attr_is_on = False

    def __init__(self, auto_off_seconds: float | int):
        """
        Initialize the switch.

        Args:
            auto_off_seconds: Delay before turning OFF automatically.
                              If <= 0, auto-off is disabled.
        """
        self._auto_off = float(auto_off_seconds)
        self._auto_unsub: CALLBACK_TYPE | None = None

        _LOGGER.debug(
            "AutoOffSwitch created (auto_off=%s seconds, enabled=%s)",
            self._auto_off,
            self._auto_off > 0,
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Handle turning the switch ON."""
        _LOGGER.info("Switch turned ON manually.")
        self._set_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Handle turning the switch OFF."""
        _LOGGER.info("Switch turned OFF manually.")
        self._set_state(False)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any running timer when the entity is removed."""
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
            _LOGGER.debug("Auto-off timer cancelled on entity removal.")

    @callback
    def _schedule_auto_off(self) -> None:
        """(Re)schedule the auto-off timer if required."""
        # Cancel previous timer if present
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
            _LOGGER.debug("Previous auto-off timer cancelled.")

        # Schedule only if ON and delay > 0
        if not self._attr_is_on or self._auto_off <= 0:
            return

        @callback
        def _off(_now) -> None:
            _LOGGER.info("Auto-off timer expired (%.3f s) → turning OFF.", self._auto_off)
            self._attr_is_on = False
            self._auto_unsub = None
            self.async_write_ha_state()

        # Accept both float (seconds) or timedelta
        delay: float | timedelta = self._auto_off
        self._auto_unsub = async_call_later(self.hass, delay, _off)
        _LOGGER.debug("Auto-off scheduled in %.3f seconds.", self._auto_off)

    @callback
    def _set_state(self, new_state: bool) -> None:
        """Update the switch state and handle auto-off."""
        new_state = bool(new_state)

        # Idempotency: exit if state does not change
        if self._attr_is_on == new_state:
            _LOGGER.debug("State unchanged (%s). Nothing to do.", new_state)
            return

        # Always cancel pending auto-off on state change
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
            _LOGGER.debug("Cancelled previous auto-off timer on state change.")

        # Update state and notify HA
        self._attr_is_on = new_state
        self.async_write_ha_state()
        _LOGGER.info("State updated: %s", "ON" if new_state else "OFF")

        # If ON, schedule auto-off
        if self._attr_is_on:
            self._schedule_auto_off()
