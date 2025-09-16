from __future__ import annotations

import logging
import threading
import re
from typing import Optional, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import SIGNAL_MOTION, SIGNAL_DISCOVERY, TT_NS

_LOGGER = logging.getLogger(__name__)

# Timings
PULL_INTERVAL_SEC = 1.0          # pausa tra pull quando non ci sono eventi
RECONNECT_BACKOFF_SEC = 5.0      # attesa prima di un nuovo tentativo standard
PULL_TIMEOUT_SEC = 10            # timeout PullMessages (PT10S)


class ProvisionOnvifEventManager:
    """Sottoscrizione e polling eventi ONVIF (PullPoint) in un thread dedicato."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        entry_id: str,
    ) -> None:
        self.hass = hass
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.entry_id = entry_id

        self._known_channels: set[str] = set()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Oggetti ONVIF creati/gestiti nel thread
        self._cam = None
        self._events = None
        self._pullpoint = None

        # (facoltativo) dati subscription per unsubscribe
        self._subscription_mgr = None
        self._subscription_addr = None

    # --------------------------- LIFECYCLE ---------------------------

    def start(self) -> None:
        """Avvia il thread di polling."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(
            target=self._runner_thread,
            name=f"prov_onvif_{self.entry_id}",
            daemon=True,
        )
        self._thread.start()
        _LOGGER.info("Manager started for %s:%s", self.host, self.port)

    def stop(self) -> None:
        """Ferma il thread e fa cleanup."""
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._cleanup_services()
        _LOGGER.info("Manager stopped")

    async def async_start(self) -> None:
        self.start()

    async def async_stop(self) -> None:
        self.stop()

    # --------------------------- THREAD LOOP ---------------------------

    def _runner_thread(self) -> None:
        """Loop principale: connect → subscribe → pull → dispatch, con retry/backoff."""
        _LOGGER.debug("Run loop entered")
        while not self._stop_ev.is_set():
            try:
                self._create_camera_and_services()
                self._subscribe_pullpoint()
                self._poll_loop()
            except Exception as ex:
                msg = str(ex) or ""
                if "Reach the maximum of NotificationProducers" in msg:
                    wait_s = max(120, int(PULL_TIMEOUT_SEC * 6))
                    _LOGGER.warning(
                        "Limite subscription ONVIF raggiunto; attendo %ss prima del retry",
                        wait_s,
                    )
                    self._cleanup_services()
                    self._stop_ev.wait(wait_s)
                else:
                    _LOGGER.warning("ONVIF events loop terminato: %s", ex, exc_info=True)
                    self._cleanup_services()
                    self._stop_ev.wait(RECONNECT_BACKOFF_SEC)

        self._cleanup_services()

    def _create_camera_and_services(self) -> None:
        """Crea ONVIFCamera e servizi. Tutto sincrono (siamo in un thread)."""
        _LOGGER.debug("Creazione ONVIFCamera per %s:%s", self.host, self.port)
        from onvif import ONVIFCamera  # type: ignore

        self._cam = ONVIFCamera(self.host, self.port, self.username, self.password)

        dev_mgmt = self._cam.create_devicemgmt_service()
        caps = dev_mgmt.GetCapabilities({"Category": "Events"})
        if not getattr(caps, "Events", None):
            raise RuntimeError("Il dispositivo non espone capacità ONVIF Events")

        self._events = self._cam.create_events_service()

    def _subscribe_pullpoint(self) -> None:
        """Crea una sottoscrizione PullPoint e ottieni il servizio PullPoint."""
        _LOGGER.debug("Creazione sottoscrizione PullPoint")
        sub = self._events.CreatePullPointSubscription({
            "InitialTerminationTime": "PT300S"  # 5 minuti
        })

        address = None
        try:
            address = getattr(getattr(sub, "SubscriptionReference", None), "Address", None)
            if hasattr(address, "_value_1"):  # zeep AnyURI
                address = address._value_1
        except Exception:
            address = None

        if address:
            self._pullpoint = self._cam.create_pullpoint_service(address)
            _LOGGER.debug("PullPoint con address: %s", address)
        else:
            self._pullpoint = self._cam.create_pullpoint_service()
            _LOGGER.debug("PullPoint senza address (fallback)")

        try:
            self._events.SetSynchronizationPoint()
        except Exception as ex:
            _LOGGER.debug("SetSynchronizationPoint non supportato: %s", ex)

        self._subscription_mgr = None
        self._subscription_addr = address
        if address:
            try:
                self._subscription_mgr = self._cam.create_subscription_service(address)
                _LOGGER.debug("SubscriptionManager creato su %s", address)
            except Exception as ex:
                _LOGGER.debug("SubscriptionManager non disponibile: %s", ex)

    def _poll_loop(self) -> None:
        """Ciclo continuo: PullMessages → parse → dispatch (con pairing Source→State)."""
        _LOGGER.info("Avvio polling eventi ONVIF (PullPoint)")
        while not self._stop_ev.is_set():
            try:
                msgs = self._pullpoint.PullMessages(
                    {"Timeout": f"PT{PULL_TIMEOUT_SEC}S", "MessageLimit": 10}
                )
                notifications = getattr(msgs, "NotificationMessage", None)
                if not notifications:
                    self._stop_ev.wait(PULL_INTERVAL_SEC)
                    continue
                if not isinstance(notifications, list):
                    notifications = [notifications]

                _LOGGER.debug("pull: %d note", len(notifications))

                # pairing: se nel batch arriva uno State senza Source, usa l'ultimo canale del batch
                last_channel: Optional[str] = None

                for idx, note in enumerate(notifications):
                    _LOGGER.debug("note[%d] raw: %s", idx, note)
                    ch, active = self._parse_motion_from_notification(note)

                    if ch is not None:
                        last_channel = ch
                    elif ch is None and active is not None and last_channel is not None:
                        ch = last_channel  # associa allo stesso canale del batch

                    _LOGGER.debug("parsed[%d]: ch=%s act=%s", idx, ch, active)
                    if ch is None or active is None:
                        continue

                    self._maybe_discover(ch)
                    _LOGGER.debug("dispatch: entry=%s ch=%s active=%s", self.entry_id, ch, active)
                    self._dispatch_motion(ch, active)

            except Exception as ex:
                _LOGGER.debug("Errore durante PullMessages: %s", ex, exc_info=True)
                break

        _LOGGER.info("Uscita dal polling eventi")

    def _cleanup_services(self) -> None:
        try:
            if self._subscription_mgr is not None:
                self._subscription_mgr.Unsubscribe()
                _LOGGER.debug("Unsubscribe eseguito su %s", self._subscription_addr)
        except Exception as ex:
            _LOGGER.debug("Unsubscribe fallito/unsupported: %s", ex)

        self._subscription_mgr = None
        self._subscription_addr = None
        self._pullpoint = None
        self._events = None
        self._cam = None

    # --------------------------- PARSING & DISPATCH ---------------------------

    def _parse_motion_from_notification(self, note: object) -> Tuple[Optional[str], Optional[bool]]:
        """Ritorna (channel, active) dai SimpleItem ONVIF con più varianti e fallback."""
        try:
            message = getattr(note, "Message", None)
            elem = getattr(message, "_value_1", None)  # lxml.etree._Element

            if not (elem is not None and hasattr(elem, "findall")):
                _LOGGER.debug("parse: elemento Message non analizzabile: %r", type(elem))
                return None, None

            channel: Optional[str] = None
            active: Optional[bool] = None

            # raccogli SimpleItem (Name/Value)
            items = elem.findall(f".//{TT_NS}SimpleItem")
            pairs = [((si.get("Name") or "").strip(), (si.get("Value") or "").strip()) for si in items]

            for name, val in pairs:
                lname = name.lower()
                v = val.strip()

                # Varianti comuni per lo stato di motion
                if lname in {"state", "ismotion", "ismotiondetected", "motion", "isactive", "detected"}:
                    lv = v.lower()
                    if lv in {"true", "1", "on", "active", "yes"}:
                        active = True
                    elif lv in {"false", "0", "off", "inactive", "no"}:
                        active = False

                # Varianti per sorgente/canale
                if channel is None and lname in {
                    "source",
                    "videosource",
                    "videosourceconfigurationtoken",
                    "videosourcetoken",
                    "token",
                    "channel",
                    "inputtoken",
                }:
                    chan = self._extract_channel_from_any(v)
                    if chan:
                        channel = chan

            # Fallback: estrai canale dal Topic (alcuni device lo mettono lì)
            if channel is None:
                topic_val = getattr(getattr(note, "Topic", None), "_value_1", None)
                if isinstance(topic_val, str):
                    chan = self._extract_channel_from_any(topic_val)
                    if chan:
                        channel = chan

            _LOGGER.debug("parse: channel=%s active=%s", channel, active)
            return channel, active

        except Exception as ex:
            _LOGGER.exception("parse: eccezione durante il parsing: %s", ex)
            return None, None

    @staticmethod
    def _extract_channel_from_any(val: str | None) -> Optional[str]:
        """Estrai numero canale da diverse forme di token/stringa."""
        if not val:
            return None
        for pat in (
            r"entities[_-]?(\d{3})",        # entities_005_0_1 -> 005
            r"[Vv]ideo[Ss]ource[_-]?(\d+)", # VideoSource_5 -> 5
            r"[Cc]hannel[_-]?(\d+)",        # channel-2 -> 2
            r"(\d{1,3})$",                  # termina con numero (es. "..._3")
        ):
            m = re.search(pat, val)
            if m:
                try:
                    return str(int(m.group(1)))
                except Exception:
                    continue
        return None

    def _dispatch_motion(self, channel: str, active: bool) -> None:
        """Invia il segnale verso i binary_sensor (thread → loop principale)."""
        @callback
        def _fire() -> None:
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_MOTION}_{self.entry_id}",
                channel,
                active,
            )
        self.hass.loop.call_soon_threadsafe(_fire)

    def _send_discovery(self, channel: str) -> None:
        """Invia il segnale di discovery (thread → loop principale)."""
        @callback
        def _fire() -> None:
            async_dispatcher_send(self.hass, f"{SIGNAL_DISCOVERY}_{self.entry_id}", channel)
        self.hass.loop.call_soon_threadsafe(_fire)

    def _maybe_discover(self, channel: str) -> None:
        """Evita duplicati e persiste il canale tra le opzioni."""
        if channel in self._known_channels:
            return
        self._known_channels.add(channel)
        _LOGGER.debug("discovery: nuovo canale %s", channel)
        self._send_discovery(channel)
        self._persist_channel(channel)

    def _persist_channel(self, channel: str) -> None:
        """Salva il canale scoperto in entry.options['channels'] (formato '1,2,5')."""
        @callback
        def _update() -> None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if not entry:
                return
            raw = (entry.options.get("channels") or "").strip()
            curr = {c.strip() for c in raw.split(",") if c.strip()}
            if channel in curr:
                return
            curr.add(str(channel))
            new = ",".join(sorted(curr, key=lambda x: int(x) if x.isdigit() else x))
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, "channels": new}
            )
        # esegui nel loop principale (thread-safe)
        self.hass.loop.call_soon_threadsafe(_update)


__all__ = ["ProvisionOnvifEventManager"]
