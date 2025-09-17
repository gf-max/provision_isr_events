from __future__ import annotations
import logging
from typing import Callable, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, SIGNAL_DISCOVERY, SIGNAL_MOTION

_LOGGER = logging.getLogger(__name__)


def _parse_auto_off_map(s: str | None) -> dict[str, float]:
    """
    Parse a per-channel auto-off map.

    Example input string:
      "5:3,7:2.5"  ->  {"5": 3.0, "7": 2.5}

    Returns:
      dict[channel_as_str] = seconds_as_float
    """
    if not s:
        return {}
    out: dict[str, float] = {}
    for part in s.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        ch, sec = part.split(":", 1)
        ch = ch.strip()
        try:
            out[ch] = float(sec.strip())
        except ValueError:
            _LOGGER.debug("Ignoring invalid auto_off_map fragment: %r", part)
            continue
    return out


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up motion binary sensors for a config entry."""
    mgr = hass.data[DOMAIN][entry.entry_id]["manager"]  # kept for symmetry, if needed

    known: set[str] = set()

    # Read options
    auto_off_default = float(entry.options.get("auto_off", 3) or 3)
    auto_off_map = _parse_auto_off_map(entry.options.get("auto_off_map"))

    _LOGGER.debug(
        "[%s] Options loaded: auto_off_default=%.3f, auto_off_map=%s",
        entry.entry_id,
        auto_off_default,
        auto_off_map,
    )

    def _add(ch: str):
        ch = str(ch)
        if ch in known:
            _LOGGER.debug("[%s] Channel %s already added, skipping.", entry.entry_id, ch)
            return
        known.add(ch)
        auto_off = auto_off_map.get(ch, auto_off_default)
        _LOGGER.info(
            "[%s] Adding motion binary_sensor for channel=%s (auto_off=%s s)",
            entry.entry_id,
            ch,
            auto_off,
        )
        async_add_entities(
            [ProvisionMotionBinarySensor(hass, entry.entry_id, ch, auto_off)], True
        )

    # Pre-seed channels from options (if provided)
    channels_opt = (entry.options.get("channels") or "").strip()
    if channels_opt:
        _LOGGER.debug("[%s] Pre-seeding channels from options: %s", entry.entry_id, channels_opt)
        for ch in (c.strip() for c in channels_opt.split(",") if c.strip()):
            _add(ch)
    else:
        # Fallback "wildcard" sensor so users can see something immediately
        _LOGGER.info(
            "[%s] No channels specified; creating wildcard sensor 'unknown'.",
            entry.entry_id,
        )
        _add("unknown")

    @callback
    def _on_discovery(channel: str):
        _LOGGER.info(
            "[%s] Discovery: new channel detected -> %s",
            entry.entry_id,
            channel,
        )
        _add(str(channel))

    unsub_disc = async_dispatcher_connect(
        hass, f"{SIGNAL_DISCOVERY}_{entry.entry_id}", _on_discovery
    )
    hass.data[DOMAIN][entry.entry_id]["unsub_disc"] = unsub_disc


class ProvisionMotionBinarySensor(BinarySensorEntity):
    """Binary sensor that reflects ONVIF motion events from the NVR."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        hass,
        entry_id: str,
        channel: str,
        auto_off_seconds: float = 0.0,
    ):
        self.hass = hass
        self._entry_id = entry_id
        self._channel = str(channel)
        self._attr_unique_id = f"{entry_id}_motion_{self._channel}"
        self._attr_name = f"NVR Motion {self._channel}"
        self._attr_is_on = False

        # AUTO-OFF
        self._auto_off: float = float(auto_off_seconds)  # 0 = disabled
        self._auto_unsub: Optional[Callable[[], None]] = None

        self._unsub_motion: Optional[Callable[[], None]] = None

        _LOGGER.debug(
            "[%s][ch=%s] Sensor created (auto_off=%s s, unique_id=%s)",
            self._entry_id,
            self._channel,
            self._auto_off,
            self._attr_unique_id,
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "manufacturer": "Provision-ISR",
            "name": "Provision-ISR NVR",
        }

    async def async_added_to_hass(self) -> None:
        """Register dispatcher callback on add."""
        @callback
        def _on_motion(channel: str, active: bool):
            # The "unknown" sensor accepts all channels
            if self._channel != "unknown" and str(channel) != self._channel:
                return
            _LOGGER.debug(
                "[%s][ch=%s] Motion event received: active=%s (raw_channel=%s)",
                self._entry_id,
                self._channel,
                active,
                channel,
            )
            self._set_state(bool(active))

        self._unsub_motion = async_dispatcher_connect(
            self.hass, f"{SIGNAL_MOTION}_{self._entry_id}", _on_motion
        )
        _LOGGER.debug(
            "[%s][ch=%s] Subscribed to motion signal.",
            self._entry_id,
            self._channel,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Clean up callbacks and timers."""
        if self._unsub_motion:
            self._unsub_motion()
            self._unsub_motion = None
            _LOGGER.debug("[%s][ch=%s] Unsubscribed from motion signal.", self._entry_id, self._channel)
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
            _LOGGER.debug("[%s][ch=%s] Auto-off timer cancelled on removal.", self._entry_id, self._channel)

    # ---------------- AUTO-OFF ----------------

    @callback
    def _schedule_auto_off(self) -> None:
        """Schedule automatic OFF after the configured delay."""
        # Cancel any previous timer
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

        if not self._attr_is_on or self._auto_off <= 0:
            return

        @callback
        def _off(_now) -> None:
            _LOGGER.info(
                "[%s][ch=%s] Auto-off elapsed (%.3f s) → turning OFF.",
                self._entry_id,
                self._channel,
                self._auto_off,
            )
            self._attr_is_on = False
            self._auto_unsub = None
            self.async_write_ha_state()

        _LOGGER.debug(
            "[%s][ch=%s] Scheduling auto-off in %.3f s.",
            self._entry_id,
            self._channel,
            self._auto_off,
        )
        self._auto_unsub = async_call_later(self.hass, self._auto_off, _off)

    @callback
    def _set_state(self, new_state: bool) -> None:
        """Set the sensor state and handle auto-off timers."""
        # Cancel any pending timer when a new state comes in
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
            _LOGGER.debug("[%s][ch=%s] Previous auto-off timer cancelled.", self._entry_id, self._channel)

        if self._attr_is_on == new_state:
            # Re-schedule auto-off if we remain ON (useful on repeated ON events)
            if new_state and self._auto_off > 0:
                _LOGGER.debug(
                    "[%s][ch=%s] State unchanged (ON) → re-scheduling auto-off.",
                    self._entry_id,
                    self._channel,
                )
                self._schedule_auto_off()
            return

        _LOGGER.info(
            "[%s][ch=%s] State change: %s → %s",
            self._entry_id,
            self._channel,
            "ON" if self._attr_is_on else "OFF",
            "ON" if new_state else "OFF",
        )

        self._attr_is_on = new_state
        self.async_write_ha_state()

        if new_state and self._auto_off > 0:
            self._schedule_auto_off()
