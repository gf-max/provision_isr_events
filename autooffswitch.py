from __future__ import annotations

from datetime import timedelta
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.event import async_call_later

class AutoOffSwitch(SwitchEntity):
    _attr_has_entity_name = True
    _attr_is_on = False

    def __init__(self, auto_off_seconds: float | int):
        # seconds > 0 per abilitare l’auto-off
        self._auto_off = float(auto_off_seconds)
        self._auto_unsub: CALLBACK_TYPE | None = None

    async def async_turn_on(self, **kwargs) -> None:
        self._set_state(True)

    async def async_turn_off(self, **kwargs) -> None:
        self._set_state(False)

    async def async_will_remove_from_hass(self) -> None:
        # cancella timer alla rimozione dell’entità
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

    @callback
    def _schedule_auto_off(self) -> None:
        """(Ri)programma l'auto-off se serve."""
        # cancella eventuale timer pendente
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

        # programma solo se acceso e delay valido
        if not self._attr_is_on or self._auto_off <= 0:
            return

        @callback
        def _off(_now) -> None:
            self._attr_is_on = False
            self._auto_unsub = None
            self.async_write_ha_state()

        # accetta float (secondi) o timedelta: usa quello che preferisci
        delay: float | timedelta = self._auto_off
        # delay = timedelta(seconds=self._auto_off)  # alternativa equivalente
        self._auto_unsub = async_call_later(self.hass, delay, _off)

    @callback
    def _set_state(self, new_state: bool) -> None:
        """Aggiorna lo stato e gestisce l'auto-off."""
        new_state = bool(new_state)

        # idempotenza: se non cambia nulla, esci
        if self._attr_is_on == new_state:
            return

        # annulla sempre eventuale auto-off pendente
        if self._auto_unsub:
            self._auto_unsub()
            self._auto_unsub = None

        # imposta stato e scrivi
        self._attr_is_on = new_state
        self.async_write_ha_state()

        # se è ON, (ri)programma l'auto-off
        if self._attr_is_on:
            self._schedule_auto_off()
