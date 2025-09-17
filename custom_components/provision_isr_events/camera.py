from __future__ import annotations

"""
Home Assistant camera platform for Provision-ISR / ONVIF devices.

- Discovers ONVIF Media profiles (GetProfiles) in HA's executor (non-blocking).
- Resolves stream URIs (GetStreamUri) and injects credentials when needed.
- Snapshot handling with robust fallbacks:
    1) ONVIF GetSnapshotUri (Digest/Basic or inline creds)
    2) Provision-ISR generic /snapshot.cgi?user=..&pwd=..
    3) Dahua/Hikvision OEM endpoints if channel is known
- Creates one Camera entity per (channel, profile).

Notes:
- python-onvif uses aiohttp internally in recent versions; any ONVIF calls must
  happen in a thread that has a running event loop *or* via HA executor.
  Here we always call ONVIF via `hass.async_add_executor_job`.
- We use a dedicated `aiohttp.ClientSession` (`_raw_session`) for snapshots
  to avoid inheriting global auth headers that could break Digest negotiations.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional
import hashlib
import os
from urllib.parse import urlsplit, quote

import aiohttp
from yarl import URL

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

DOMAIN = "provision_isr_events"
DEFAULT_RTSP_PORT = 554

# Import ONVIF lazily; keep module presence explicit for clearer errors
try:
    from onvif import ONVIFCamera  # type: ignore
except Exception as exc:
    ONVIFCamera = None
    _LOGGER.debug("ONVIF import failed: %s", exc)


@dataclass
class DiscoveredProfile:
    """Normalized view of an ONVIF media profile."""
    token: str
    name: str
    channel: Optional[int]
    rtsp_uri: Optional[str]
    snapshot_uri: Optional[str]


def _inject_inline_creds(url: str, user: str, password: str) -> str:
    """Inject URL-encoded user:pass into URL userinfo if not already present."""
    try:
        u = URL(url)
        if u.user or u.password:
            return str(u)
        return str(u.with_user(user).with_password(password or ""))
    except Exception:
        # Fallback manual injection
        try:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{quote(user, safe='')}:{quote(password or '', safe='')}@{rest}"
        except Exception:
            return url


def _strip_inline_creds(url: str) -> str:
    """Remove userinfo (user:pass@) from URL to avoid mixing with header auth."""
    try:
        u = URL(url)
        if u.user or u.password:
            # Rebuild without userinfo; keep query untouched
            return str(
                URL.build(
                    scheme=u.scheme,
                    host=u.host,
                    port=u.port,
                    path=u.raw_path,
                    query_string=u.raw_query_string,
                )
            )
        return str(u)
    except Exception:
        # Manual strip as last resort
        try:
            scheme, rest = url.split("://", 1)
            host_and_more = rest.split("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
            return f"{scheme}://{host_and_more}"
        except Exception:
            return url


def _parse_www_authenticate(h: str) -> dict:
    """Parse WWW-Authenticate header (Digest) into a dict of challenge params."""
    if not h or "digest" not in h.lower():
        return {}
    # Example: Digest qop="auth", realm="ONVIF Digest", nonce="...", opaque="...", stale="TRUE"
    parts = h.split(" ", 1)[1] if " " in h else ""
    kv = {}
    for chunk in parts.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            kv[k.strip().strip('"').lower()] = v.strip().strip('"')
    return kv


def _build_digest_header(method: str, url: str, user: str, pwd: str, chal: dict, nc: int = 1) -> str:
    """Build Authorization: Digest header (MD5, qop=auth)."""
    realm = chal.get("realm", "")
    nonce = chal.get("nonce", "")
    qop = chal.get("qop", "")
    opaque = chal.get("opaque", None)

    parsed = urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    cnonce = os.urandom(8).hex()
    nc_value = f"{nc:08x}"

    def md5(s: str) -> str:
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    ha1 = md5(f"{user}:{realm}:{pwd}")
    ha2 = md5(f"{method}:{path}")
    if "auth" in (qop or ""):
        response = md5(f"{ha1}:{nonce}:{nc_value}:{cnonce}:auth:{ha2}")
    else:
        response = md5(f"{ha1}:{nonce}:{ha2}")

    parts = [
        f'Digest username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{path}"',
        f'response="{response}"',
    ]
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if "auth" in (qop or ""):
        parts += ['qop=auth', f'nc={nc_value}', f'cnonce="{cnonce}"']
    return ", ".join(parts)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Provision-ISR camera entities from a config entry.

    Implementation:
    - All ONVIF calls are performed via HA executor to avoid blocking the loop.
    - Profiles are filtered (optional) to skip obvious low/sub streams if requested.
    """
    entry.async_on_unload(entry.add_update_listener(_reload_on_update))

    host: str = entry.data[CONF_HOST]
    port: int = entry.data.get(CONF_PORT, 80)
    username: str = entry.data.get(CONF_USERNAME, "")
    password: str = entry.data.get(CONF_PASSWORD, "")

    rtsp_port: int = entry.options.get("rtsp_port", DEFAULT_RTSP_PORT)
    publish_substream: bool = entry.options.get("publish_substream", True)

    if ONVIFCamera is None:
        _LOGGER.error("onvif package not available; cannot create camera entities for %s", host)
        return

    profiles: list[DiscoveredProfile] = []

    try:
        # Create ONVIF camera and media service in executor (non-blocking)
        cam = await hass.async_add_executor_job(lambda: ONVIFCamera(host, port, username, password))
        media = await hass.async_add_executor_job(cam.create_media_service)
        raw_profiles = await hass.async_add_executor_job(media.GetProfiles)

        for p in raw_profiles:
            token = getattr(p, "token", None) or getattr(p, "_token", None)
            if not token:
                _LOGGER.debug("Profile without token skipped: %r", p)
                continue

            name = getattr(p, "Name", None) or getattr(p, "name", None) or token

            # Try to infer channel from VideoSourceConfiguration.SourceToken
            channel: Optional[int] = None
            try:
                vsc = getattr(p, "VideoSourceConfiguration", None)
                if vsc and getattr(vsc, "SourceToken", None):
                    stoken = str(vsc.SourceToken)
                    # Extract last numeric segment (e.g., "..._5" -> 5)
                    for part in reversed(stoken.replace("_", " ").split()):
                        if part.isdigit():
                            channel = int(part)
                            break
            except Exception:
                pass

            rtsp_uri: Optional[str] = None
            snapshot_uri: Optional[str] = None

            # GetStreamUri
            try:
                req = await hass.async_add_executor_job(media.create_type, "GetStreamUri")
                req.ProfileToken = token
                req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
                res = await hass.async_add_executor_job(media.GetStreamUri, req)
                rtsp_uri = getattr(res, "Uri", None)
            except Exception as exc:
                _LOGGER.warning("GetStreamUri failed for profile %s: %s", token, exc)

            # Inject credentials into RTSP if needed and adjust port
            if rtsp_uri and username and password:
                rtsp_uri = _inject_inline_creds(rtsp_uri, username, password)
            if rtsp_uri and "://" in rtsp_uri and ":" not in rtsp_uri.split("/")[2]:
                # Add explicit port if missing and different from default 554
                try:
                    scheme, rest = rtsp_uri.split("://", 1)
                    hostpart, path = rest.split("/", 1)
                    if scheme.lower().startswith("rtsp") and rtsp_port != 554:
                        rtsp_uri = f"{scheme}://{hostpart}:{rtsp_port}/{path}"
                except Exception:
                    pass

            # GetSnapshotUri (may be absent on some devices)
            try:
                get_snap = getattr(media, "GetSnapshotUri", None)
                if get_snap is not None:
                    req = await hass.async_add_executor_job(media.create_type, "GetSnapshotUri")
                    req.ProfileToken = token
                    sres = await hass.async_add_executor_job(media.GetSnapshotUri, req)
                    snapshot_uri = getattr(sres, "Uri", None)
                    # Keep original URL; auth is negotiated at request time (Digest/Basic/urlcreds)
            except Exception as exc:
                _LOGGER.debug("GetSnapshotUri not available for profile %s: %s", token, exc)

            # Optionally filter substreams
            lower_name = (str(name)).lower()
            if not publish_substream and any(x in lower_name for x in ("sub", "extr", "low")):
                continue

            profiles.append(
                DiscoveredProfile(
                    token=str(token),
                    name=str(name),
                    channel=channel,
                    rtsp_uri=rtsp_uri,
                    snapshot_uri=snapshot_uri,
                )
            )

    except Exception as exc:
        _LOGGER.error("Failed to initialize ONVIF media for %s:%s: %s", host, port, exc)
        return

    if not profiles:
        _LOGGER.warning("No ONVIF media profiles discovered for %s", host)
        return

    entities: list[ProvisionOnvifCamera] = []
    for idx, prof in enumerate(profiles, start=1):
        entities.append(
            ProvisionOnvifCamera(
                hass=hass,
                entry=entry,
                host=host,
                port=port,
                username=username,
                password=password,
                profile=prof,
                index=idx,
            )
        )

    async_add_entities(entities, update_before_add=False)
    _LOGGER.info("[camera] Added %d Provision-ISR camera entities for %s", len(entities), host)


class ProvisionOnvifCamera(Camera):
    """Camera entity representing a single ONVIF media profile."""

    # Advertise streaming capability (HA will use RTSP source)
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        host: str,
        port: int,
        username: str,
        password: str,
        profile: DiscoveredProfile,
        index: int,
    ) -> None:
        super().__init__()
        self.hass = hass
        self._entry = entry
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._profile = profile
        self._index = index

        # HA shared session (not used for snapshots that require raw auth handling)
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        # Dedicated raw session for snapshots (no inherited Authorization)
        self._raw_session = aiohttp.ClientSession()
        self._snapshot_broken = False  # Avoid repeated retries if definitively failing

        # Friendly name: CH{n} - {profile_name} if channel detected
        channel = self._profile.channel
        base = f"{DOMAIN}" if channel is None else f"CH{channel}"
        prof_name = self._profile.name or self._profile.token
        self._attr_name = f"{base} - {prof_name}"

        # Unique ID: config entry + profile token
        self._attr_unique_id = f"{entry.entry_id}_{self._profile.token}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Provision-ISR @ {host}",
            manufacturer="Provision-ISR / ONVIF",
            model="NVR/Camera",
            configuration_url=f"http://{host}:{port}",
        )

    @property
    def brand(self) -> str | None:
        return "Provision-ISR"

    async def stream_source(self) -> str | None:
        """Return RTSP URL for HA stream worker."""
        return self._profile.rtsp_uri

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image from the camera with robust fallbacks.

        Order:
        1) ONVIF GetSnapshotUri:
           - First attempt: no inline creds (let server challenge with Digest)
           - If 401 Digest: compute Authorization: Digest and retry
           - Fallback: inline creds or Basic header
        2) Provision-ISR /snapshot.cgi?user=&pwd=
        3) Dahua /cgi-bin/snapshot.cgi and Hikvision /ISAPI endpoints (if channel known)
        """
        if self._snapshot_broken:
            return None

        timeout_sec: int = int(self._entry.options.get("snapshot_timeout", 10))
        snapshot_mode: str = str(self._entry.options.get("snapshot_mode", "auto")).lower()

        async def _fetch(url: str, use_header_auth: bool) -> bytes | None:
            """Fetch JPEG with optional Basic header; auto-retry Digest challenge."""
            try:
                # Normalize URL (preserve inline creds if present)
                try:
                    url_obj = URL(url)
                    url = str(url_obj)
                except Exception:
                    pass

                has_inline_creds = "@" in url.split("://", 1)[-1].split("/", 1)[0]
                auth = None
                if use_header_auth and self._username and not has_inline_creds:
                    # First try Basic (some devices accept Basic even if they announce Digest)
                    auth = aiohttp.BasicAuth(self._username, self._password or "")

                to = aiohttp.ClientTimeout(total=timeout_sec)
                allow_redirs = False if has_inline_creds else True
                headers = {"Accept": "image/jpeg", "Connection": "close"}

                # ---- 1) Initial request (Basic header or inline creds or none) ----
                async with self._raw_session.get(
                    url,
                    auth=auth,
                    timeout=to,
                    allow_redirects=allow_redirs,
                    headers=headers,
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()

                    wa = resp.headers.get("WWW-Authenticate", "")
                    # ---- 2) Digest 2-step: challenge → Authorization: Digest ----
                    if resp.status == 401 and "digest" in wa.lower() and self._username:
                        chal = _parse_www_authenticate(wa)
                        if chal:
                            auth_header = _build_digest_header("GET", url, self._username, self._password or "", chal, nc=1)
                            headers2 = {**headers, "Authorization": auth_header}
                            async with self._raw_session.get(
                                url,
                                timeout=to,
                                allow_redirects=allow_redirs,
                                headers=headers2,
                            ) as r2:
                                if r2.status == 200:
                                    return await r2.read()

                                # Handle stale nonce (server asks to retry with new nonce)
                                wa2 = r2.headers.get("WWW-Authenticate", "")
                                if r2.status == 401 and "stale=\"true\"" in wa2.lower():
                                    chal2 = _parse_www_authenticate(wa2)
                                    if chal2:
                                        auth_header2 = _build_digest_header("GET", url, self._username, self._password or "", chal2, nc=2)
                                        headers3 = {**headers, "Authorization": auth_header2}
                                        async with self._raw_session.get(
                                            url,
                                            timeout=to,
                                            allow_redirects=allow_redirs,
                                            headers=headers3,
                                        ) as r3:
                                            if r3.status == 200:
                                                return await r3.read()
                                            _LOGGER.debug(
                                                "Snapshot HTTP status %s for %s after stale retry",
                                                r3.status, self.entity_id
                                            )
                                            return None

                    _LOGGER.debug(
                        "Snapshot HTTP status %s for %s via %s",
                        resp.status, self.entity_id,
                        "header" if use_header_auth else "urlcreds",
                    )

            except asyncio.TimeoutError:
                _LOGGER.debug("Snapshot timeout for %s (url=%s)", self.entity_id, url)
            except Exception as exc:
                _LOGGER.debug("Snapshot error for %s (url=%s): %s", self.entity_id, url, exc)
            return None

        # 1) ONVIF-provided URL — prefer Digest path by default
        url = self._profile.snapshot_uri
        if url and snapshot_mode != "disabled":
            # First try WITHOUT inline creds to trigger proper WWW-Authenticate
            url_no_creds = _strip_inline_creds(url)
            data = await _fetch(url_no_creds, use_header_auth=False)  # will handle Digest on 401
            if data:
                return data

            # Fallback: try inline credentials
            if ("@" not in url) and self._username and snapshot_mode in ("auto", "urlcreds"):
                url2 = _inject_inline_creds(url, self._username, self._password)
                data = await _fetch(url2, use_header_auth=False)
                if data:
                    return data

            # Optional: try Basic header (only if URL has no creds)
            if ("@" not in url) and snapshot_mode in ("auto", "basic"):
                data = await _fetch(url, use_header_auth=True)
                if data:
                    return data

        # 2) Provision-ISR generic endpoint
        if snapshot_mode != "disabled" and self._username:
            u = quote(self._username, safe="")
            p = quote(self._password or "", safe="")
            prov_simple = f"http://{self._host}/snapshot.cgi?user={u}&pwd={p}"
            data = await _fetch(prov_simple, use_header_auth=False)
            if data:
                return data

        # 3) Vendor fallbacks if channel is known
        ch = self._profile.channel
        if ch and snapshot_mode != "disabled":
            lower_name = (self._profile.name or "").lower()
            subtype = 1 if any(x in lower_name for x in ("sub", "extra", "low")) else 0

            # Dahua-style
            u = quote(self._username or "", safe="")
            p = quote(self._password or "", safe="")
            dahua = f"http://{u}:{p}@{self._host}/cgi-bin/snapshot.cgi?channel={ch}&subtype={subtype}"
            data = await _fetch(dahua, use_header_auth=False)
            if data:
                return data

            # Hikvision-style
            hik_ch = f"{ch}01" if subtype == 0 else f"{ch}02"
            hik = f"http://{self._host}/ISAPI/Streaming/channels/{hik_ch}/picture"
            data = await _fetch(hik, use_header_auth=True)
            if not data and self._username and "@" not in hik:
                data = await _fetch(f"http://{u}:{p}@{self._host}/ISAPI/Streaming/channels/{hik_ch}/picture", use_header_auth=False)
            if data:
                return data

        # Mark as broken to avoid spamming retries each UI refresh
        self._snapshot_broken = True
        return None

    @property
    def is_streaming(self) -> bool:
        """Advertise camera as streaming-capable."""
        return True

    async def async_update(self) -> None:
        """No periodic polling required; stream/snapshot on demand."""
        return

    async def async_will_remove_from_hass(self) -> None:
        """Close dedicated raw session on entity removal."""
        try:
            await self._raw_session.close()
        except Exception:
            pass


async def _reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entities when options or config change."""
    await hass.config_entries.async_reload(entry.entry_id)
