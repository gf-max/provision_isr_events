from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import glob
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    SIGNAL_MOTION,
    SIGNAL_DISCOVERY,
    TT_NS,
    CONF_CHANNEL_FLAGS,
    # snapshot options
    CONF_SNAPSHOT_ON_MOTION,
    CONF_SNAPSHOT_DIR,
    CONF_SNAPSHOT_MIN_INTERVAL,
    CONF_SNAPSHOT_DELAY_MS,               # NEW
    CONF_SNAPSHOT_BURST,                  # NEW
    CONF_SNAPSHOT_BURST_INTERVAL_MS,      # NEW
    # retention options
    CONF_RETENTION_DAYS,
    CONF_RETENTION_MAX_FILES,
    CONF_KEEP_LATEST_ONLY,
    # defaults
    DEFAULT_SNAPSHOT_ON_MOTION,
    DEFAULT_SNAPSHOT_DIR,
    DEFAULT_SNAPSHOT_MIN_INTERVAL,
    DEFAULT_SNAPSHOT_DELAY_MS,            # NEW
    DEFAULT_SNAPSHOT_BURST,               # NEW
    DEFAULT_SNAPSHOT_BURST_INTERVAL_MS,   # NEW
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETENTION_MAX_FILES,
    DEFAULT_KEEP_LATEST_ONLY,
    PULL_INTERVAL_SEC,
    RECONNECT_BACKOFF_SEC,
    PULL_TIMEOUT_SEC,
    PULL_MESSAGE_LIMIT,
    IDLE_SLEEP_MIN_SEC,
    IDLE_SLEEP_MAX_SEC,

)

_LOGGER = logging.getLogger(__name__)




class ProvisionOnvifEventManager:
    """Subscribe and poll ONVIF events (PullPoint) in a dedicated thread.

    Design notes:
    - ONVIF/python-onvif uses aiohttp under the hood in recent versions.
      aiohttp requires an *asyncio running loop* in the current thread.
    - Python threads do NOT have a loop by default: we must create and set it.
    - All blocking ONVIF work is executed inside this thread which *owns* its loop.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        username: str,
        password: str,
        entry_id: str,
    ) -> None:
        # HA & connection params
        self.hass = hass
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.entry_id = entry_id

        # Discovery state and lifecycle
        self._known_channels: set[str] = set()
        self._stop_ev = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # ONVIF-related objects (created inside the thread after loop is set)
        self._cam = None
        self._events = None
        self._pullpoint = None

        # Optional unsubscribe plumbing
        self._subscription_mgr = None
        self._subscription_addr = None

        # Options cache and per-channel snapshot throttling
        self._options_cache: dict | None = None
        self._last_snapshot_at: dict[str, float] = {}

    # --------------------------- LIFECYCLE ---------------------------

    def start(self) -> None:
        """Start the polling thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_ev.clear()
        self._thread = threading.Thread(
            target=self._runner_thread,
            name=f"prov_onvif_{self.entry_id}",
            daemon=True,
        )
        self._thread.start()
        _LOGGER.info("Event manager started for %s:%s", self.host, self.port)

    def stop(self) -> None:
        """Signal stop and wait the thread to finish, then cleanup."""
        self._stop_ev.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._cleanup_services()
        _LOGGER.info("Event manager stopped")

    async def async_start(self) -> None:
        self.start()

    async def async_stop(self) -> None:
        self.stop()

    # --------------------------- THREAD LOOP ---------------------------

    def _runner_thread(self) -> None:
        """Main loop: create thread-owned asyncio loop → connect → subscribe → pull → dispatch."""
        _LOGGER.debug("Run loop entered")

        # Create and set an asyncio event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while not self._stop_ev.is_set():
                try:
                    # Initialize ONVIF clients/services (requires a running loop)
                    self._create_camera_and_services()
                    # Create PullPoint subscription
                    self._subscribe_pullpoint()
                    # Start pulling messages until an error or stop is requested
                    self._poll_loop()
                except Exception as ex:
                    msg = str(ex) or ""
                    if "Reach the maximum of NotificationProducers" in msg:
                        # Device has too many concurrent subscriptions; back off longer
                        wait_s = max(120, int(PULL_TIMEOUT_SEC * 6))
                        _LOGGER.warning(
                            "ONVIF subscription limit reached; waiting %ss before retry",
                            wait_s,
                        )
                        self._cleanup_services()
                        self._stop_ev.wait(wait_s)
                    else:
                        _LOGGER.warning("ONVIF events loop ended with error: %s", ex, exc_info=True)
                        self._cleanup_services()
                        self._stop_ev.wait(RECONNECT_BACKOFF_SEC)

            # On stop signal, final cleanup
            self._cleanup_services()

        finally:
            # Best-effort aiohttp session close (if exposed by the library)
            try:
                dev = getattr(self, "_cam", None)
                sess = getattr(getattr(getattr(dev, "devicemgmt", None), "transport", None), "session", None)
                if sess and not sess.closed:
                    loop.run_until_complete(sess.close())
            except Exception:
                pass

            # Cancel any pending tasks bound to this loop
            try:
                pending = asyncio.all_tasks(loop=loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass

            # Detach and close loop
            asyncio.set_event_loop(None)
            loop.close()
            _LOGGER.debug("Thread event loop closed")

    def _create_camera_and_services(self) -> None:
        """Create ONVIFCamera and required services. Runs in the thread after loop is set.

        Note:
        - Import ONVIFCamera inside the method to avoid import-time side effects
          (some libs may touch asyncio during import).
        """
        _LOGGER.debug("Creating ONVIFCamera for %s:%s", self.host, self.port)
        from onvif import ONVIFCamera  # type: ignore

        self._cam = ONVIFCamera(self.host, self.port, self.username, self.password)

        # Check device capabilities (Events must be available)
        dev_mgmt = self._cam.create_devicemgmt_service()
        caps = dev_mgmt.GetCapabilities({"Category": "Events"})
        if not getattr(caps, "Events", None):
            raise RuntimeError("Device does not expose ONVIF Events capability")

        # Events service used for CreatePullPointSubscription / PullPoint
        self._events = self._cam.create_events_service()

    def _subscribe_pullpoint(self) -> None:
        """Create a PullPoint subscription and get the PullPoint service."""
        _LOGGER.debug("Creating PullPoint subscription")
        sub = self._events.CreatePullPointSubscription({
            "InitialTerminationTime": "PT300S"  # 5 minutes
        })

        # Try to extract the subscription address (some devices return it, some don't)
        address = None
        try:
            address = getattr(getattr(sub, "SubscriptionReference", None), "Address", None)
            if hasattr(address, "_value_1"):  # zeep AnyURI case
                address = address._value_1
        except Exception:
            address = None

        if address:
            # Use explicit PullPoint address when available
            self._pullpoint = self._cam.create_pullpoint_service(address)
            _LOGGER.debug("PullPoint with address: %s", address)
        else:
            # Fallback: some devices work without explicit address
            self._pullpoint = self._cam.create_pullpoint_service()
            _LOGGER.debug("PullPoint without address (fallback)")

        # Optional sync point (not all devices support it)
        try:
            self._events.SetSynchronizationPoint()
        except Exception as ex:
            _LOGGER.debug("SetSynchronizationPoint unsupported: %s", ex)

        # Prepare SubscriptionManager for Unsubscribe (best-effort)
        self._subscription_mgr = None
        self._subscription_addr = address
        if address:
            try:
                self._subscription_mgr = self._cam.create_subscription_service(address)
                _LOGGER.debug("SubscriptionManager created at %s", address)
            except Exception as ex:
                _LOGGER.debug("SubscriptionManager unavailable: %s", ex)

    def _poll_loop(self) -> None:
        """Continuous cycle: PullMessages → parse → dispatch with adaptive idle sleep.

        Strategy:
        - Keep a short idle sleep while events are flowing ("hot" state).
        - When no events are returned, gradually increase the idle sleep up to a cap.
        - Preserve fractional PullMessages timeout if provided (e.g. 1.2 → "PT1.2S").
        """
        _LOGGER.info("Starting ONVIF events polling (PullPoint)")

        # Start in "hot" mode for low latency
        idle_sleep = IDLE_SLEEP_MIN_SEC

        # Format ONVIF duration string keeping fractional seconds if configured
        timeout_str = f"PT{PULL_TIMEOUT_SEC}S"

        while not self._stop_ev.is_set():
            try:
                msgs = self._pullpoint.PullMessages(
                    {"Timeout": timeout_str, "MessageLimit": PULL_MESSAGE_LIMIT}
                )
                notifications = getattr(msgs, "NotificationMessage", None)

                if not notifications:
                    # No events → back off a bit (bounded exponential-ish)
                    idle_sleep = min(IDLE_SLEEP_MAX_SEC, idle_sleep * 1.5 + 0.01)
                    self._stop_ev.wait(idle_sleep)
                    continue

                # Got events → go hot immediately
                idle_sleep = IDLE_SLEEP_MIN_SEC

                if not isinstance(notifications, list):
                    notifications = [notifications]

                _LOGGER.debug("Pull returned %d notifications", len(notifications))

                # If a batch contains a State without Source, reuse the last seen channel
                last_channel: Optional[str] = None

                for idx, note in enumerate(notifications):
                    _LOGGER.debug("note[%d] raw: %s", idx, note)
                    ch, active = self._parse_motion_from_notification(note)

                    if ch is not None:
                        last_channel = ch
                    elif ch is None and active is not None and last_channel is not None:
                        ch = last_channel  # pair State with the last Source

                    _LOGGER.debug("parsed[%d]: channel=%s active=%s", idx, ch, active)
                    if ch is None or active is None:
                        continue

                    # Discovery + dispatch
                    self._maybe_discover(ch)
                    _LOGGER.debug("dispatch: entry=%s channel=%s active=%s", self.entry_id, ch, active)
                    self._dispatch_motion(ch, active)

                    # Schedule snapshot on rising edge
                    if active:
                        self._schedule_snapshot(ch, active)

            except Exception as ex:
                # Break and let the outer loop handle reconnect/backoff
                _LOGGER.debug("Error during PullMessages: %s", ex, exc_info=True)
                break

        _LOGGER.info("Exiting events polling loop")


    def _cleanup_services(self) -> None:
        """Best-effort unsubscribe and drop references to ONVIF services."""
        try:
            if self._subscription_mgr is not None:
                self._subscription_mgr.Unsubscribe()
                _LOGGER.debug("Unsubscribe issued on %s", self._subscription_addr)
        except Exception as ex:
            _LOGGER.debug("Unsubscribe failed/unsupported: %s", ex)

        self._subscription_mgr = None
        self._subscription_addr = None
        self._pullpoint = None
        self._events = None
        self._cam = None

    # --------------------------- PARSING & DISPATCH ---------------------------

    def _parse_motion_from_notification(self, note: object) -> Tuple[Optional[str], Optional[bool]]:
        """Return (channel, active) from ONVIF SimpleItem with multiple variants and fallbacks.

        Strategy:
        - Collect tt:SimpleItem Name/Value pairs and look for common keys.
        - Try to extract channel/token from several fields.
        - Fallback: parse channel from Topic string.
        """
        try:
            message = getattr(note, "Message", None)
            elem = getattr(message, "_value_1", None)  # lxml.etree._Element

            if not (elem is not None and hasattr(elem, "findall")):
                _LOGGER.debug("parse: Message element is not parsable: %r", type(elem))
                return None, None

            channel: Optional[str] = None
            active: Optional[bool] = None

            # Collect SimpleItem (Name/Value)
            items = elem.findall(f".//{TT_NS}SimpleItem")
            pairs = [((si.get("Name") or "").strip(), (si.get("Value") or "").strip()) for si in items]

            for name, val in pairs:
                lname = name.lower()
                v = val.strip()

                # Common variants for motion state
                if lname in {"state", "ismotion", "ismotiondetected", "motion", "isactive", "detected", "logicalstate"}:
                    lv = v.lower()
                    if lv in {"true", "1", "on", "active", "yes"}:
                        active = True
                    elif lv in {"false", "0", "off", "inactive", "no"}:
                        active = False

                # Source/channel variants
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

            # Fallback: extract channel from Topic
            if channel is None:
                topic_val = getattr(getattr(note, "Topic", None), "_value_1", None)
                if isinstance(topic_val, str):
                    chan = self._extract_channel_from_any(topic_val)
                    if chan:
                        channel = chan

            _LOGGER.debug("parse result: channel=%s active=%s", channel, active)
            return channel, active

        except Exception as ex:
            _LOGGER.exception("parse: exception while parsing: %s", ex)
            return None, None

    @staticmethod
    def _extract_channel_from_any(val: str | None) -> Optional[str]:
        """Extract channel number from different token/string forms."""
        if not val:
            return None
        for pat in (
            r"entities[_-]?(\d{3})",        # entities_005_0_1 -> 005
            r"[Vv]ideo[Ss]ource[_-]?(\d+)", # VideoSource_5 -> 5
            r"[Cc]hannel[_-]?(\d+)",        # channel-2 -> 2
            r"[Pp]rofile[_-]?(\d+)",        # profile_5_0 -> 5
            r"(\d{1,3})$",                  # ends with number (e.g. "..._3")
        ):
            m = re.search(pat, val)
            if m:
                try:
                    return str(int(m.group(1)))
                except Exception:
                    continue
        return None

    def _dispatch_motion(self, channel: str, active: bool) -> None:
        """Send dispatcher signal to binary_sensors (thread → HA main loop)."""
        @callback
        def _fire() -> None:
            async_dispatcher_send(
                self.hass,
                f"{SIGNAL_MOTION}_{self.entry_id}",
                channel,
                active,
            )
        # Schedule on HA loop (thread-safe)
        self.hass.loop.call_soon_threadsafe(_fire)

    def _send_discovery(self, channel: str) -> None:
        """Send discovery signal (thread → HA main loop)."""
        @callback
        def _fire() -> None:
            async_dispatcher_send(self.hass, f"{SIGNAL_DISCOVERY}_{self.entry_id}", channel)
        self.hass.loop.call_soon_threadsafe(_fire)

    def _maybe_discover(self, channel: str) -> None:
        """Avoid duplicates and persist channel in options."""
        if channel in self._known_channels:
            return
        self._known_channels.add(channel)
        _LOGGER.debug("discovery: new channel %s", channel)
        self._send_discovery(channel)
        self._persist_channel(channel)

    def _persist_channel(self, channel: str) -> None:
        """Persist discovered channel into entry.options['channels'] (format '1,2,5')."""
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
        self.hass.loop.call_soon_threadsafe(_update)

    # --------------------------- SNAPSHOT PATH ---------------------------

    def _schedule_snapshot(self, channel: str, active: bool) -> None:
        """Thread-safe schedule of the async snapshot routine on HA loop."""
        coro = self._async_snapshot_if_enabled(channel, active)
        try:
            # Use run_coroutine_threadsafe to schedule from a non-HA thread
            asyncio.run_coroutine_threadsafe(coro, self.hass.loop)
        except Exception as ex:
            _LOGGER.error("Thread-safe schedule failed (ch %s): %s", channel, ex)

    async def _async_snapshot_if_enabled(self, channel: str, active: bool) -> None:
        """If motion is active and channel is enabled, save a snapshot (with limits) and notify."""
        if not active:
            return

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        opts = (entry.options if entry else None) or (self._options_cache or {})
        self._options_cache = opts  # refresh cache

        flags = opts.get(CONF_CHANNEL_FLAGS, {}) or {}
        default_on = bool(opts.get(CONF_SNAPSHOT_ON_MOTION, DEFAULT_SNAPSHOT_ON_MOTION))
        ch_enabled = bool(flags.get(channel, default_on))
        if not ch_enabled:
            _LOGGER.debug("Snapshot skipped (channel %s disabled)", channel)
            return

        # Per-channel cooldown
        min_interval = int(opts.get(CONF_SNAPSHOT_MIN_INTERVAL, DEFAULT_SNAPSHOT_MIN_INTERVAL) or 0)
        now = time.time()
        last = self._last_snapshot_at.get(channel, 0.0)
        if min_interval > 0 and (now - last) < min_interval:
            _LOGGER.debug("Snapshot cooldown for ch %s (%.1fs < %ss)", channel, now - last, min_interval)
            return

        # Resolve camera entity automatically
        camera_entity = self._resolve_camera_entity_for_channel(channel)
        if not camera_entity:
            _LOGGER.debug("Snapshot: no camera entity resolved for channel %s", channel)
            return

        # Target directory
        rel_dir = opts.get(CONF_SNAPSHOT_DIR, DEFAULT_SNAPSHOT_DIR)
        base_dir = os.path.join(self.hass.config.path("."), rel_dir)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except Exception as ex:
            _LOGGER.error("Snapshot: unable to create dir %s: %s", base_dir, ex)
            return

        # Compute filename once per event:
        # - if keep_latest: always write "..._latest.jpg"
        # - else: write one timestamped file per event (even with burst we overwrite it)
        keep_latest = bool(opts.get(CONF_KEEP_LATEST_ONLY, DEFAULT_KEEP_LATEST_ONLY))
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = (
            f"nvr_{self.entry_id}_ch{channel}_latest.jpg"
            if keep_latest
            else f"nvr_{self.entry_id}_ch{channel}_{ts_str}.jpg"
        )
        fullpath = os.path.join(base_dir, filename)

        # Configurable delay before the first shot (to capture a more meaningful frame)
        delay_ms = int(opts.get(CONF_SNAPSHOT_DELAY_MS, DEFAULT_SNAPSHOT_DELAY_MS))
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)

        # Optional burst: take N frames separated by a short interval, overwriting the same file.
        burst = max(1, int(opts.get(CONF_SNAPSHOT_BURST, DEFAULT_SNAPSHOT_BURST)))
        burst_iv_ms = int(opts.get(CONF_SNAPSHOT_BURST_INTERVAL_MS, DEFAULT_SNAPSHOT_BURST_INTERVAL_MS))

        try:
            for i in range(burst):
                await self.hass.services.async_call(
                    "camera",
                    "snapshot",
                    {"entity_id": camera_entity, "filename": fullpath},
                    blocking=True,
                )
                if i < burst - 1 and burst_iv_ms > 0:
                    await asyncio.sleep(burst_iv_ms / 1000.0)

            self._last_snapshot_at[channel] = now

            # Build /local URL if under www
            local_url = None
            www_root = self.hass.config.path("www")
            try:
                if os.path.realpath(fullpath).startswith(os.path.realpath(www_root)):
                    rel = os.path.relpath(fullpath, www_root).replace(os.sep, "/")
                    local_url = f"/local/{rel}"
            except Exception:
                pass

            # Fire event for UI (e.g., Browser Mod popup)
            self.hass.bus.async_fire(
                f"{DOMAIN}.snapshot_saved",
                {
                    "entry_id": self.entry_id,
                    "channel": channel,
                    "camera_entity": camera_entity,
                    "file_path": fullpath,
                    "local_url": local_url,
                    "timestamp": ts_str,
                },
            )
            _LOGGER.info("Snapshot saved (ch %s): %s", channel, fullpath)

            # Cleanup (run in background to keep UI responsive)
            self.hass.async_create_task(self._async_prune_snapshots(base_dir, channel, opts))

        except Exception as ex:
            _LOGGER.error("Snapshot failed for camera %s (ch %s): %s", camera_entity, channel, ex)

    def _resolve_camera_entity_for_channel(self, channel: str) -> str | None:
        """Find the most probable camera entity for a channel using multiple patterns and scoring."""
        try:
            n = int(str(channel).strip())
        except Exception:
            _LOGGER.debug("Resolver: non-numeric channel: %r", channel)
            return None

        ch = str(n)
        ch03 = f"{n:03d}"

        def has(rx: str, s: str | None) -> bool:
            return bool(s and re.search(rx, s))

        ent_reg = er.async_get(self.hass)

        best_eid: str | None = None
        best_score = -1

        for st in self.hass.states.async_all("camera"):
            eid = st.entity_id
            eid_l = eid.lower()
            name_l = (st.name or "").lower()

            entry = ent_reg.async_get(eid)
            uid_l = ((entry.unique_id or "").lower() if entry else "")
            platform = (entry.platform if entry else "")

            score = 0

            # Strong matches (+4)
            if has(rf"profile[_-]?{ch}[_-]", eid_l) or has(rf"entities[_-]?{ch03}\b", eid_l) or has(rf"entities[_-]?{ch03}\b", uid_l):
                score += 4

            # Medium matches (+3)
            if has(rf"videosource[_-]?{ch}\b", eid_l) or has(rf"videosource[_-]?{ch}\b", uid_l):
                score += 3
            if has(rf"channel[_-]?{ch}\b", eid_l) or has(rf"\bch{ch}\b", eid_l) or has(rf"\bch{ch}\b", name_l):
                score += 3

            # Weak matches (+1/+2)
            if platform in ("onvif", "ffmpeg", "rtsp"):
                score += 1
            if has(rf"(^|[^0-9]){ch}([^0-9]|$)", eid_l):
                score += 1

            if score > best_score:
                best_score = score
                best_eid = eid

        if best_score >= 3:
            _LOGGER.debug("Resolver: channel %s → %s (score=%d)", ch, best_eid, best_score)
            return best_eid

        _LOGGER.debug("Resolver: no reliable camera for channel %s. Top=%s (score=%d)", ch, best_eid, best_score)
        return None
    async def _async_prune_snapshots(self, base_dir: str, channel: str, opts: dict) -> None:
        """Schedule snapshot pruning on the executor to avoid blocking the event loop."""
        try:
            await self.hass.async_add_executor_job(self._prune_snapshots_blocking, base_dir, channel, opts)
        except Exception as ex:
            _LOGGER.debug("Prune (async) error (ch %s): %s", channel, ex)


    def _prune_snapshots_blocking(self, base_dir: str, channel: str, opts: dict) -> None:
        """Blocking filesystem pruning (runs in executor)."""
        try:
            import os
            import time
            from pathlib import Path

            retention_days = int(opts.get(CONF_RETENTION_DAYS, DEFAULT_RETENTION_DAYS) or 0)
            max_files = int(opts.get(CONF_RETENTION_MAX_FILES, DEFAULT_RETENTION_MAX_FILES) or 0)
            keep_latest = bool(opts.get(CONF_KEEP_LATEST_ONLY, DEFAULT_KEEP_LATEST_ONLY))

            # File name patterns
            prefix = f"nvr_{self.entry_id}_ch{channel}_"
            latest_name = f"{prefix}latest.jpg"
            latest_path = Path(os.path.join(base_dir, latest_name)) if keep_latest else None

            # Fast scan with scandir (faster than glob and we are already in executor)
            def _list_snapshots() -> list[Path]:
                res: list[Path] = []
                try:
                    with os.scandir(base_dir) as it:
                        for de in it:
                            if not de.is_file():
                                continue
                            name = de.name
                            if not (name.startswith(prefix) and name.endswith(".jpg")):
                                continue
                            res.append(Path(de.path))
                except FileNotFoundError:
                    return []
                return res

            files = _list_snapshots()
            if not files:
                return

            # 1) Age-based retention
            if retention_days and retention_days > 0:
                cutoff = time.time() - retention_days * 86400
                for fp in list(files):
                    try:
                        if keep_latest and latest_path and fp.samefile(latest_path):
                            continue
                    except Exception:
                        pass
                    try:
                        if fp.stat().st_mtime < cutoff:
                            fp.unlink(missing_ok=True)
                            files.remove(fp)
                    except Exception as ex:
                        _LOGGER.debug("Prune (days): skip %s (%s)", fp.name, ex)

            # 2) Cap maximum number of files per channel
            if max_files and max_files > 0:
                files = _list_snapshots()  # refresh
                if keep_latest and latest_path and latest_path.exists():
                    try:
                        files = [f for f in files if not f.samefile(latest_path)]
                    except Exception:
                        # Fallback: keep all, rely on sort below
                        pass

                try:
                    files.sort(key=lambda p: p.stat().st_mtime)
                except Exception:
                    # If a file disappears between scan/stat, ignore
                    files = [f for f in files if f.exists()]
                    files.sort(key=lambda p: p.stat().st_mtime)

                excess = max(0, len(files) - max_files)
                for fp in files[:excess]:
                    try:
                        fp.unlink(missing_ok=True)
                    except Exception as ex:
                        _LOGGER.debug("Prune (count): skip %s (%s)", fp.name, ex)

        except Exception as ex:
            _LOGGER.debug("Prune (blocking) error (ch %s): %s", channel, ex)


__all__ = ["ProvisionOnvifEventManager"]
