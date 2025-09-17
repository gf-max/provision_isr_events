from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, PLATFORMS
from .manager import ProvisionOnvifEventManager

_LOGGER = logging.getLogger(__name__)

# hass.data layout:
# hass.data[DOMAIN] = { entry_id: { "manager": ProvisionOnvifEventManager } }

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (YAML not supported)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: start the ONVIF event manager and forward platforms."""
    _LOGGER.info("Setting up entry %s", entry.entry_id)

    mgr = ProvisionOnvifEventManager(
        hass,
        host=entry.data.get("host"),
        port=entry.data.get("port"),
        username=entry.data.get("username"),
        password=entry.data.get("password"),
        entry_id=entry.entry_id,
    )
    await mgr.async_start()
    hass.data[DOMAIN][entry.entry_id] = {"manager": mgr}

    async def _options_updated(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        _LOGGER.debug("Options updated for %s — reloading entry", updated_entry.entry_id)
        await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: stop the manager and unload platforms."""
    _LOGGER.info("Unloading entry %s", entry.entry_id)

    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, {})
    mgr: ProvisionOnvifEventManager | None = data.get("manager")  # type: ignore[assignment]
    if mgr:
        await mgr.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.warning("Some platforms failed to unload for %s", entry.entry_id)
    return unload_ok
