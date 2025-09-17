from __future__ import annotations

# Integration domain
DOMAIN = "provision_isr_events"

# User step (config entry data)
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
DEFAULT_PORT = 80

# ONVIF namespace used when parsing SimpleItem
TT_NS = "{http://www.onvif.org/ver10/schema}"

# Internal dispatcher signals
SIGNAL_MOTION = "provision_isr_events_motion"
SIGNAL_DISCOVERY = "provision_isr_events_discovery"

# Base options
CONF_AUTO_OFF = "auto_off"
CONF_CHANNELS = "channels"  # e.g. "1,2,6"

# Snapshot: global options and per-channel flags
CONF_SNAPSHOT_ON_MOTION = "snapshot_on_motion"   # bool
CONF_SNAPSHOT_DIR = "snapshot_dir"               # path relative to /config (e.g. "www/snapshots")

# Per-channel flags (camera mapping no longer required)
CONF_CHANNEL_FLAGS = "channel_flags"             # dict: { "1": true, "2": false, ... }

# Defaults
DEFAULT_SNAPSHOT_ON_MOTION = True
DEFAULT_SNAPSHOT_DIR = "www/snapshots"           # → URL /local/snapshots/...

# Limits / retention
CONF_SNAPSHOT_MIN_INTERVAL = "snapshot_min_interval"   # seconds
CONF_RETENTION_DAYS = "retention_days"                 # days
CONF_RETENTION_MAX_FILES = "retention_max_files"       # per channel
CONF_KEEP_LATEST_ONLY = "keep_latest_only"             # overwrite ..._latest.jpg

DEFAULT_SNAPSHOT_MIN_INTERVAL = 30
DEFAULT_RETENTION_DAYS = 7
DEFAULT_RETENTION_MAX_FILES = 200
DEFAULT_KEEP_LATEST_ONLY = False

# Platforms exposed by this integration
PLATFORMS: list[str] = ["binary_sensor", "camera"]
