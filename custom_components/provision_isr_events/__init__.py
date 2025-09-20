from __future__ import annotations

import logging
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# hass.data layout:
# hass.data[DOMAIN] = { entry_id: { "manager": ProvisionOnvifEventManager } }

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Prepare domain namespace (no YAML support)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry: load platforms first, then start the event manager."""
    # Ensure domain bucket exists even on reload paths
    domain_data = hass.data.setdefault(DOMAIN, {})

    _LOGGER.info("Setting up entry %s", entry.entry_id)

    # Local import to avoid side effects at module import time
    from .manager import ProvisionOnvifEventManager

    # Best-effort cleanup if something left a stale manager
    old = domain_data.get(entry.entry_id, {}).get("manager")
    if old:
        try:
            await old.async_stop()
        except Exception:  # pragma: no cover
            _LOGGER.debug("Old manager stop failed (ignored)", exc_info=True)

    mgr = ProvisionOnvifEventManager(
        hass=hass,
        host=entry.data.get("host"),
        port=entry.data.get("port"),
        username=entry.data.get("username"),
        password=entry.data.get("password"),
        entry_id=entry.entry_id,
    )
    domain_data[entry.entry_id] = {"manager": mgr}

    # Reload entry if options change
    async def _options_updated(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        _LOGGER.debug("Options updated for %s — reloading entry", updated_entry.entry_id)
        await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    # 1) Load platforms first so entities are ready to receive signals
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 2) Start manager as a background task (non-blocking, we are on HA loop)
    start_task = hass.async_create_task(
        mgr.async_start(), name=f"{DOMAIN}_start_{entry.entry_id}"
    )
    # Cancel the start task if the entry is unloaded during startup
    entry.async_on_unload(start_task.cancel)

    # Stop manager on HA shutdown — async listener (runs on HA loop)
    async def _on_ha_stop(_event) -> None:
        await mgr.async_stop()

    unsub_stop = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    entry.async_on_unload(unsub_stop)

    # Remove data bucket for this entry when it is unloaded
    def _cleanup_bucket() -> None:
        domain_data.pop(entry.entry_id, None)
        if not domain_data:
            hass.data.pop(DOMAIN, None)

    entry.async_on_unload(_cleanup_bucket)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry: stop the manager and unload platforms."""
    _LOGGER.info("Unloading entry %s", entry.entry_id)

    domain_data = hass.data.get(DOMAIN, {})
    data = domain_data.pop(entry.entry_id, {})
    mgr = data.get("manager")

    if mgr:
        await mgr.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        _LOGGER.warning("Some platforms failed to unload for %s", entry.entry_id)

    if not domain_data:
        hass.data.pop(DOMAIN, None)

    return unload_ok
