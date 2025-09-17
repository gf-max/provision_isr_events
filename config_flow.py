from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_USERNAME,
    CONF_PASSWORD,
    DEFAULT_PORT,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Minimal UI flow: chiede host/porta/user/pass e crea l'entry."""
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))

            # Evita duplicati: unique_id = host (puoi cambiare in f"{host}:{port}" se preferisci)
            await self.async_set_unique_id(host)
            self._abort_if_unique_id_configured()

            # (opzionale) qui potresti fare una validazione live, es. ping o GET a /onvif/device_service
            # try:
            #     await some_async_validation(host, port, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            # except CannotConnect:
            #     errors["base"] = "cannot_connect"
            # except InvalidAuth:
            #     errors["base"] = "invalid_auth"

            if not errors:
                title_port = f":{port}" if port != DEFAULT_PORT else ""
                return self.async_create_entry(
                    title=f"Provision-ISR ({host}{title_port})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
