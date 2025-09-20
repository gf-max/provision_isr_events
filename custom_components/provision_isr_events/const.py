# custom_components/provision_isr_events/const.py
from __future__ import annotations

from homeassistant.const import Platform

# --- Domain / basic ---
DOMAIN = "provision_isr_events"

# HA platforms exposed by this integration
# (we create binary_sensors for motion; cameras are existing HA entities used only for snapshots)
PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CAMERA]

# --- Credentials / connection ---
CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
DEFAULT_PORT = 80  # ONVIF SOAP typically listens on 80 (adjust if your NVR uses a different port)

# --- Core options ---
CONF_CHANNELS = "channels"            # e.g. "1,2,3"
CONF_CHANNEL_FLAGS = "channel_flags"  # per-channel feature flags (dict)
CONF_AUTO_OFF = "auto_off"            # auto-clear motion (seconds) on binary_sensors
DEFAULT_AUTO_OFF = 2

# --- Snapshot options (global) ---
CONF_SNAPSHOT_ON_MOTION = "snapshot_on_motion"
DEFAULT_SNAPSHOT_ON_MOTION = True

CONF_SNAPSHOT_DIR = "snapshot_dir"  # relative to HA config dir
DEFAULT_SNAPSHOT_DIR = "www/provision_isr_snapshots"

CONF_SNAPSHOT_MIN_INTERVAL = "snapshot_min_interval"  # seconds, per-channel cooldown
DEFAULT_SNAPSHOT_MIN_INTERVAL = 3

# Optional delay before taking the first snapshot after motion (milliseconds)
CONF_SNAPSHOT_DELAY_MS = "snapshot_delay_ms"
DEFAULT_SNAPSHOT_DELAY_MS = 0

# Optional burst (multiple snapshots per motion)
CONF_SNAPSHOT_BURST = "snapshot_burst"
DEFAULT_SNAPSHOT_BURST = 1

# Interval between burst snapshots (milliseconds)
CONF_SNAPSHOT_BURST_INTERVAL_MS = "snapshot_burst_interval_ms"
DEFAULT_SNAPSHOT_BURST_INTERVAL_MS = 300

# --- Retention / limits ---
CONF_RETENTION_DAYS = "retention_days"
DEFAULT_RETENTION_DAYS = 7

CONF_RETENTION_MAX_FILES = "retention_max_files"
DEFAULT_RETENTION_MAX_FILES = 500

CONF_KEEP_LATEST_ONLY = "keep_latest_only"
DEFAULT_KEEP_LATEST_ONLY = False

# --- Dispatcher signals (prefixed in code with entry_id for scoping) ---
SIGNAL_MOTION = f"{DOMAIN}_motion"
SIGNAL_DISCOVERY = f"{DOMAIN}_discovery"

# --- Namespaces / parsing helpers ---
# ONVIF "tt" namespace (used in SimpleItem lookups)
TT_NS = "{http://www.onvif.org/ver10/schema}"

# Event topics considered “motion-like” (used to decide when to snapshot)
# NOTE: Vendors vary; extend this map if your device emits different topics.
TOPIC_MAP: dict[str, str] = {
    "tns1:RuleEngine/CellMotionDetector/Motion": "motion",
    "tns1:VideoSource/MotionAlarm": "motion",
    "tns1:VideoAnalytics/MotionDetection": "motion",
    "tns1:RuleEngine/TamperDetection/Tamper": "tamper",
    "tns1:Device/Triggers/AlarmLocal": "alarm",
}

# --- Polling / adaptive loop timings ---
# Long-poll timeout used in PullMessages (some firmwares dislike fractional seconds).
PULL_TIMEOUT_SEC = 1.2  # if you see SOAP errors, set this to 1

# Return as soon as a single message is available (minimizes latency).
PULL_MESSAGE_LIMIT = 1

# Idle backoff between pulls when there are no events (adaptive loop).
IDLE_SLEEP_MIN_SEC = 0.05   # minimum idle sleep when the loop is "hot"
IDLE_SLEEP_MAX_SEC = 0.8    # maximum idle sleep when the loop is "cold"

# Keep the loop “hot” for a short window after any activity (reduces post-event latency).
HOT_WINDOW_SEC = 5.0

# Legacy param kept for compatibility (not used by the adaptive loop).
PULL_INTERVAL_SEC = 0.2

# Backoff before reconnect attempts on errors.
RECONNECT_BACKOFF_SEC = 5.0

__all__ = [
    "DOMAIN",
    "PLATFORMS",
    # credentials
    "CONF_HOST", "CONF_PORT", "CONF_USERNAME", "CONF_PASSWORD", "DEFAULT_PORT",
    # base options
    "CONF_AUTO_OFF", "DEFAULT_AUTO_OFF", "CONF_CHANNELS", "CONF_CHANNEL_FLAGS",
    # snapshots (global)
    "CONF_SNAPSHOT_ON_MOTION", "DEFAULT_SNAPSHOT_ON_MOTION",
    "CONF_SNAPSHOT_DIR", "DEFAULT_SNAPSHOT_DIR",
    "CONF_SNAPSHOT_MIN_INTERVAL", "DEFAULT_SNAPSHOT_MIN_INTERVAL",
    "CONF_SNAPSHOT_DELAY_MS", "DEFAULT_SNAPSHOT_DELAY_MS",
    "CONF_SNAPSHOT_BURST", "DEFAULT_SNAPSHOT_BURST",
    "CONF_SNAPSHOT_BURST_INTERVAL_MS", "DEFAULT_SNAPSHOT_BURST_INTERVAL_MS",
    # limits / retention
    "CONF_RETENTION_DAYS", "DEFAULT_RETENTION_DAYS",
    "CONF_RETENTION_MAX_FILES", "DEFAULT_RETENTION_MAX_FILES",
    "CONF_KEEP_LATEST_ONLY", "DEFAULT_KEEP_LATEST_ONLY",
    # signals / parsing
    "SIGNAL_MOTION", "SIGNAL_DISCOVERY", "TT_NS", "TOPIC_MAP",
    # timings
    "PULL_INTERVAL_SEC", "RECONNECT_BACKOFF_SEC",
    "PULL_TIMEOUT_SEC", "PULL_MESSAGE_LIMIT",
    "IDLE_SLEEP_MIN_SEC", "IDLE_SLEEP_MAX_SEC", "HOT_WINDOW_SEC",
]
