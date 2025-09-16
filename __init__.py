from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from .const import DOMAIN, PLATFORMS
from .manager import ProvisionOnvifEventManager

async def async_setup_entry(hass, entry):
    data = entry.data
    mgr = ProvisionOnvifEventManager(
        hass=hass,
        host=data["host"],
        port=data["port"],
        username=data["username"],
        password=data["password"],
        entry_id=entry.entry_id,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"manager": mgr}
    # imposta default una volta, se mancante
    if "auto_off" not in entry.options:
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "auto_off": 3}  # 3 secondi
        )
    # 🔧 AVVIO MANAGER (se è sync usa executor)
    await hass.async_add_executor_job(mgr.start)

    # stop pulito quando HA si spegne
    def _on_ha_stop(_):
        try:
            mgr.stop()
        except Exception:
            pass
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True
# ricarica entità quando cambiano le opzioni
async def _update_listener(hass, updated_entry):
    await hass.config_entries.async_reload(updated_entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        # ferma eventuale thread/polling del manager se hai uno stop()
        try:
            hass.data[DOMAIN][entry.entry_id]["manager"].stop()
        except Exception:
            pass
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
