from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD,
    DEFAULT_PORT,
)

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Minimal UI flow: chiede host/porta/user/pass, crea l'entry e basta."""
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"Provision-ISR ({user_input[CONF_HOST]}:{user_input[CONF_PORT]})",
                data=user_input,   # i dati vengono letti in __init__.py
            )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)
