from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    # credentials
    CONF_HOST, CONF_PORT, CONF_USERNAME, CONF_PASSWORD, DEFAULT_PORT,
    # base options
    CONF_AUTO_OFF, CONF_CHANNELS,
    # snapshots (global)
    CONF_SNAPSHOT_ON_MOTION, DEFAULT_SNAPSHOT_ON_MOTION,
    CONF_SNAPSHOT_DIR, DEFAULT_SNAPSHOT_DIR,
    # limits / retention
    CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL,
    CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS,
    CONF_RETENTION_MAX_FILES, DEFAULT_RETENTION_MAX_FILES,
    CONF_KEEP_LATEST_ONLY, DEFAULT_KEEP_LATEST_ONLY,
    # per-channel flags
    CONF_CHANNEL_FLAGS,
)

# ----------------------------- USER STEP (entry creation) ---------------------

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): str,
})

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Minimal install flow; all runtime options live in the Options Flow."""
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Ask for NVR credentials only, then create the entry."""
        if user_input is not None:
            return self.async_create_entry(
                title=f"Provision-ISR ({user_input[CONF_HOST]}:{user_input[CONF_PORT]})",
                data=user_input,
            )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Create the options flow (do NOT pass config_entry to the handler; deprecated)."""
        return OptionsFlowHandler()


# ------------------------------ OPTIONS FLOW ---------------------------------

class OptionsFlowHandler(OptionsFlow):
    """Two steps: 'basic' (global settings) → 'channels' (per-channel toggles)."""

    def __init__(self) -> None:
        # Do NOT assign self.config_entry here (deprecated in HA 2025.12).
        self._snapshot_on_motion: bool = DEFAULT_SNAPSHOT_ON_MOTION
        self._snapshot_dir: str = DEFAULT_SNAPSHOT_DIR
        self._channels_str: str = ""
        self._channels: list[str] = []
        # global extra limits
        self._min_interval: int = DEFAULT_SNAPSHOT_MIN_INTERVAL
        self._ret_days: int = DEFAULT_RETENTION_DAYS
        self._ret_max_files: int = DEFAULT_RETENTION_MAX_FILES
        self._keep_latest_only: bool = DEFAULT_KEEP_LATEST_ONLY

    async def async_step_init(self, user_input=None):
        """Show 'basic' step with global options and channel list."""
        opts = self.config_entry.options
        schema = vol.Schema({
            vol.Optional(
                CONF_AUTO_OFF,
                default=opts.get(CONF_AUTO_OFF, 0),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),

            vol.Optional(
                CONF_CHANNELS,
                default=opts.get(CONF_CHANNELS, ""),
            ): str,  # e.g. "1,2,6"

            vol.Optional(
                CONF_SNAPSHOT_ON_MOTION,
                default=opts.get(CONF_SNAPSHOT_ON_MOTION, DEFAULT_SNAPSHOT_ON_MOTION),
            ): bool,

            vol.Optional(
                CONF_SNAPSHOT_DIR,
                default=opts.get(CONF_SNAPSHOT_DIR, DEFAULT_SNAPSHOT_DIR),
            ): str,

            # Limits / retention
            vol.Optional(
                CONF_SNAPSHOT_MIN_INTERVAL,
                default=opts.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),

            vol.Optional(
                CONF_RETENTION_DAYS,
                default=opts.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),

            vol.Optional(
                CONF_RETENTION_MAX_FILES,
                default=opts.get(CONF_RETENTION_MAX_FILES, DEFAULT_RETENTION_MAX_FILES),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),

            vol.Optional(
                CONF_KEEP_LATEST_ONLY,
                default=opts.get(CONF_KEEP_LATEST_ONLY, DEFAULT_KEEP_LATEST_ONLY),
            ): bool,
        })
        return self.async_show_form(step_id="basic", data_schema=schema)

    async def async_step_basic(self, user_input=None):
        """Persist global options and, if channels are provided, continue with per-channel toggles."""
        if user_input is None:
            return await self.async_step_init()

        # cache global options and normalize channel list
        self._snapshot_on_motion = bool(user_input.get(CONF_SNAPSHOT_ON_MOTION, DEFAULT_SNAPSHOT_ON_MOTION))
        self._snapshot_dir = (user_input.get(CONF_SNAPSHOT_DIR, DEFAULT_SNAPSHOT_DIR) or "").strip()
        self._channels_str = (user_input.get(CONF_CHANNELS, "") or "").strip()
        self._channels = [p.strip() for p in self._channels_str.split(",") if p.strip().isdigit()]

        self._min_interval = int(user_input.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL) or 0)
        self._ret_days = int(user_input.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) or 0)
        self._ret_max_files = int(user_input.get(CONF_RETENTION_MAX_FILES, DEFAULT_RETENTION_MAX_FILES) or 0)
        self._keep_latest_only = bool(user_input.get(CONF_KEEP_LATEST_ONLY, DEFAULT_KEEP_LATEST_ONLY))

        # No channels? Save global options and finish.
        if not self._channels:
            new_opts = {
                **self.config_entry.options,
                CONF_AUTO_OFF: int(user_input.get(CONF_AUTO_OFF, 0) or 0),
                CONF_CHANNELS: self._channels_str,
                CONF_SNAPSHOT_ON_MOTION: self._snapshot_on_motion,
                CONF_SNAPSHOT_DIR: self._snapshot_dir,
                CONF_SNAPSHOT_MIN_INTERVAL: self._min_interval,
                CONF_RETENTION_DAYS: self._ret_days,
                CONF_RETENTION_MAX_FILES: self._ret_max_files,
                CONF_KEEP_LATEST_ONLY: self._keep_latest_only,
            }
            return self.async_create_entry(title="", data=new_opts)

        # Otherwise go to per-channel toggles
        return await self.async_step_channels()

    async def async_step_channels(self, user_input=None):
        """Show per-channel toggles, then persist flags along with the global options."""
        opts = self.config_entry.options
        existing_flags: dict = opts.get(CONF_CHANNEL_FLAGS, {}) or {}

        if user_input is None:
            schema_dict: dict = {}
            for ch in self._channels:
                schema_dict[vol.Optional(
                    f"snapshot_{ch}",
                    default=existing_flags.get(ch, self._snapshot_on_motion),
                )] = bool

            return self.async_show_form(
                step_id="channels",
                data_schema=vol.Schema(schema_dict),
                description_placeholders={"channels": ", ".join(self._channels)},
            )

        # Collect flags
        flags: dict[str, bool] = {}
        for ch in self._channels:
            flags[ch] = bool(user_input.get(f"snapshot_{ch}", self._snapshot_on_motion))

        new_opts = {
            **self.config_entry.options,
            # global
            CONF_CHANNELS: self._channels_str,
            CONF_SNAPSHOT_ON_MOTION: self._snapshot_on_motion,
            CONF_SNAPSHOT_DIR: self._snapshot_dir,
            CONF_SNAPSHOT_MIN_INTERVAL: self._min_interval,
            CONF_RETENTION_DAYS: self._ret_days,
            CONF_RETENTION_MAX_FILES: self._ret_max_files,
            CONF_KEEP_LATEST_ONLY: self._keep_latest_only,
            # per-channel
            CONF_CHANNEL_FLAGS: flags,
        }
        return self.async_create_entry(title="", data=new_opts)
