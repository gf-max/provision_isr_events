from __future__ import annotations
from typing import Callable, Optional

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, SIGNAL_DISCOVERY, SIGNAL_MOTION

def _parse_auto_off_map(s: str | None) -> dict[str, float]:
    """
    Esempio stringa: "5:3,7:2.5" -> {"5": 3.0, "7": 2.5}
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
            continue
    return out


async def async_setup_entry(hass, entry, async_add_entities):
    mgr = hass.data[DOMAIN][entry.entry_id]["manager"]

    known: set[str] = set()

    # Leggi opzioni
    auto_off_default = float(entry.options.get("auto_off", 3) or 3)
    auto_off_map = _parse_auto_off_map(entry.options.get("auto_off_map"))

    def _add(ch: str):
        ch = str(ch)
        if ch in known:
            return
        known.add(ch)
        auto_off = auto_off_map.get(ch, auto_off_default)
        async_add_entities([ProvisionMotionBinarySensor(hass, entry.entry_id, ch, auto_off)], True)

    # Pre-semina canali da options (se presenti)
    channels_opt = (entry.options.get("channels") or "").strip()
    if channels_opt:
        for ch in (c.strip() for c in channels_opt.split(",") if c.strip()):
            _add(ch)
    else:
        # sensore jolly per vedere subito qualcosa
        _add("unknown")

    @callback
    def _on_discovery(channel: str):
        _add(str(channel))

    unsub_disc = async_dispatcher_connect(
        hass, f"{SIGNAL_DISCOVERY}_{entry.entry_id}", _on_discovery
    )
    hass.data[DOMAIN][entry.entry_id]["unsub_disc"] = unsub_disc


class ProvisionMotionBinarySensor(BinarySensorEntity):
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(self, hass, entry_id: str, channel: str, auto_off_seconds: float = 0.0):
        self.hass = hass
        self._entry_id = entry_id
        self._channel = str(channel)
        self._attr_unique_id = f"{entry_id}_motion_{self._channel}"
        self._attr_name = f"NVR Motion {self._channel}"
        self._attr_is_on = False

        # AUTO-OFF
        self._auto_off: float = float(auto_off_seconds)  # 0 = disattivato
        self._auto_unsub: Optional[Callable[[], None]] = None

        self._unsub_motion: Optional[Callable[[], None]] = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "manufacturer": "Provision-ISR",
            "name": "Provision-ISR NVR",
        }

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_motion(channel: str, active: bool):
            # "unknown" accetta tutti i canali
            if self._channel != "unknown" and str(channel) != self._channel:
                return
            self._set_state(bool(active))

        self._unsub_motion = async_dispatcher_connect(
            self.hass, f"{SIGNAL_MOTION}_{self._entry_id}", _on_motion
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_motion:
            self._unsub_motion()
            self._unsub_motion = None
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

    # ---------------- AUTO-OFF ----------------

    @callback
    def _schedule_auto_off(self) -> None:
        # cancella eventuale timer precedente
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None
        if not self._attr_is_on or self._auto_off <= 0:
            return

        @callback
        def _off(_now) -> None:
            self._attr_is_on = False
            self._auto_unsub = None
            self.async_write_ha_state()

        self._auto_unsub = async_call_later(self.hass, self._auto_off, _off)

    @callback
    def _set_state(self, new_state: bool) -> None:
        # annulla timer se cambia stato
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

        if self._attr_is_on == new_state:
            # comunque ripianifica se resta ON (utile se arrivano ripetuti ON)
            if new_state and self._auto_off > 0:
                self._schedule_auto_off()
            return

        self._attr_is_on = new_state
        self.async_write_ha_state()

        if new_state and self._auto_off > 0:
            self._schedule_auto_off()
