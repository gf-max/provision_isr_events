DOMAIN = "provision_isr_events"
PLATFORMS = ["binary_sensor", "camera"]

CONF_HOST = "host"
CONF_PORT = "port"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

CONF_AUTO_OFF = "auto_off"
CONF_CHANNELS = "channels"

DEFAULT_PORT = 80
DEFAULT_AUTO_OFF = 5  # secondi

SIGNAL_MOTION = "provision_isr_motion"
SIGNAL_DISCOVERY = "provision_isr_discovery"

EVENT_TOPICS = {
    "motion": "tns1:VideoSource/MotionAlarm",
    # "tamper": "tns1:VideoSource/TamperAlarm",
}

TT_NS = "{http://www.onvif.org/ver10/schema}"

# RTSP di default per NVR Dahua/Provision-ISR
DEFAULT_RTSP_TEMPLATE = (
    "rtsp://{username}:{password}@{host}:554/"
    "cam/realmonitor?channel={channel}&subtype={subtype}"
)
DEFAULT_RTSP_SUBTYPE = 1  # 0=main, 1=sub