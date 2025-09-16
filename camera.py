from __future__ import annotations

"""
Home Assistant camera platform for Provision‑ISR / ONVIF devices.

- Discovers ONVIF Media profiles (GetProfiles)
- Resolves stream URIs (GetStreamUri) for each profile
- Snapshot handling with multiple fallbacks (ONVIF, Provision-ISR, Dahua/Hikvision OEM)
- Creates one Camera entity per (channel, profile)

"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional
import hashlib
import os
from urllib.parse import urlsplit

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

try:
    from onvif import ONVIFCamera  # type: ignore
except Exception as exc:
    ONVIFCamera = None
    _LOGGER.debug("ONVIF import failed: %s", exc)


@dataclass
class DiscoveredProfile:
    token: str
    name: str
    channel: Optional[int]
    rtsp_uri: Optional[str]
    snapshot_uri: Optional[str]


def _inject_inline_creds(url: str, user: str, password: str) -> str:
    """Inject Basic-Auth into URL safely (URL-encode and avoid duplicates)."""
    try:
        u = URL(url)
        if u.user or u.password:
            return str(u)
        return str(u.with_user(user).with_password(password or ""))
    except Exception:
        from urllib.parse import quote
        try:
            scheme, rest = url.split("://", 1)
            return f"{scheme}://{quote(user, safe='')}:{quote(password or '', safe='')}@{rest}"
        except Exception:
            return url

def _strip_inline_creds(url: str) -> str:
    """Remove any user:pass@ from URL to avoid mixing with Authorization headers."""
    try:
        u = URL(url)
        if u.user or u.password:
            # rebuild without userinfo
            return str(URL.build(scheme=u.scheme, host=u.host, port=u.port, path=u.raw_path, query_string=u.raw_query_string))
        return str(u)
    except Exception:
        # Manual strip
        try:
            scheme, rest = url.split("://", 1)
            host_and_more = rest.split("@", 1)[-1] if "@" in rest.split("/", 1)[0] else rest
            return f"{scheme}://{host_and_more}"
        except Exception:
            return url

def _parse_www_authenticate(h: str) -> dict:
    """Parsa l'header WWW-Authenticate e ritorna i parametri della challenge Digest."""
    if not h or "digest" not in h.lower():
        return {}
    # Esempio: Digest qop="auth", realm="ONVIF Digest", nonce="ABC", opaque="...", stale="TRUE"
    parts = h.split(" ", 1)[1] if " " in h else ""
    kv = {}
    for chunk in parts.split(","):
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            kv[k.strip().strip('"').lower()] = v.strip().strip('"')
    return kv

def _build_digest_header(method: str, url: str, user: str, pwd: str, chal: dict, nc: int = 1) -> str:
    """Costruisce l'header Authorization: Digest (MD5, qop=auth)."""
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
    """Set up camera entities from a config entry."""
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
        cam = await hass.async_add_executor_job(lambda: ONVIFCamera(host, port, username, password))
        media = await hass.async_add_executor_job(cam.create_media_service)
        raw_profiles = await hass.async_add_executor_job(media.GetProfiles)

        for p in raw_profiles:
            token = p.token
            name = getattr(p, "Name", None) or getattr(p, "name", None) or token
            channel: Optional[int] = None
            try:
                vsc = getattr(p, "VideoSourceConfiguration", None)
                if vsc and getattr(vsc, "SourceToken", None):
                    stoken = str(vsc.SourceToken)
                    for part in reversed(stoken.replace("_", " ").split()):
                        if part.isdigit():
                            channel = int(part)
                            break
            except Exception:
                pass

            rtsp_uri: Optional[str] = None
            snapshot_uri: Optional[str] = None

            try:
                req = await hass.async_add_executor_job(media.create_type, "GetStreamUri")
                req.ProfileToken = token
                req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
                res = await hass.async_add_executor_job(media.GetStreamUri, req)
                rtsp_uri = getattr(res, "Uri", None)
            except Exception as exc:
                _LOGGER.warning("GetStreamUri failed for profile %s: %s", token, exc)

            if rtsp_uri and username and password:
                rtsp_uri = _inject_inline_creds(rtsp_uri, username, password)

            if rtsp_uri and "://" in rtsp_uri and ":" not in rtsp_uri.split("/")[2]:
                try:
                    scheme, rest = rtsp_uri.split("://", 1)
                    hostpart, path = rest.split("/", 1)
                    if scheme.lower().startswith("rtsp") and rtsp_port != 554:
                        rtsp_uri = f"{scheme}://{hostpart}:{rtsp_port}/{path}"
                except Exception:
                    pass

            try:
                get_snap = getattr(media, "GetSnapshotUri", None)
                if get_snap is not None:
                    req = await hass.async_add_executor_job(media.create_type, "GetSnapshotUri")
                    req.ProfileToken = token
                    sres = await hass.async_add_executor_job(media.GetSnapshotUri, req)
                    snapshot_uri = getattr(sres, "Uri", None)
                    # Force inline creds because Provision-ISR requires them
                    # Keep raw URL for Digest auth (do not inject inline creds here)
                    snapshot_uri = snapshot_uri
            except Exception as exc:
                _LOGGER.debug("GetSnapshotUri not available for profile %s: %s", token, exc)

            lower_name = (name or "").lower()
            if not publish_substream and any(x in lower_name for x in ("sub", "extr", "low")):
                continue

            profiles.append(
                DiscoveredProfile(
                    token=token,
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
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        # Use a dedicated raw session for camera snapshots to avoid inherited Authorization headers
        self._raw_session = aiohttp.ClientSession()
        self._snapshot_broken = False

        channel = self._profile.channel
        base = f"{DOMAIN}"
        if channel is not None:
            base = f"CH{channel}"
        prof_name = self._profile.name or self._profile.token
        self._attr_name = f"{base} - {prof_name}"

        self._attr_unique_id = f"{entry.entry_id}_{self._profile.token}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Provision‑ISR @ {host}",
            manufacturer="Provision‑ISR / ONVIF",
            model="NVR/Camera",
            configuration_url=f"http://{host}:{port}",
        )

    @property
    def brand(self) -> str | None:
        return "Provision‑ISR"

    async def stream_source(self) -> str | None:
        return self._profile.rtsp_uri

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image from the camera with robust fallbacks."""
        from urllib.parse import quote

        if self._snapshot_broken:
            return None

        timeout_sec: int = int(self._entry.options.get("snapshot_timeout", 10))
        snapshot_mode: str = str(self._entry.options.get("snapshot_mode", "auto")).lower()

        async def _fetch(url: str, use_header_auth: bool) -> bytes | None:
            try:
                # Normalizza/encoda URL (preserva eventuali credenziali inline)
                try:
                    url_obj = URL(url)
                    url = str(url_obj)
                except Exception:
                    pass

                has_inline_creds = "@" in url.split("://", 1)[-1].split("/", 1)[0]
                auth = None
                if use_header_auth and self._username and not has_inline_creds:
                    # Primo tentativo: Basic (alcuni NVR la accettano anche se pubblicizzano Digest)
                    auth = aiohttp.BasicAuth(self._username, self._password or "")

                to = aiohttp.ClientTimeout(total=timeout_sec)
                allow_redirs = False if has_inline_creds else True
                headers = {"Accept": "image/jpeg", "Connection": "close"}

                # ---- 1) Primo colpo (Basic o inline creds) ----
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
                    # ---- 2) Fallback Digest in 2 step (challenge → Authorization: Digest) ----
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

                                # nonce scaduto → stale=TRUE → rifaccio con nuova challenge
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
                                            _LOGGER.debug("Snapshot HTTP status %s for %s after stale retry", r3.status, self.entity_id)
                                            return None

                    _LOGGER.debug(
                        "Snapshot HTTP status %s for %s via %s",
                        resp.status,
                        self.entity_id,
                        "header" if use_header_auth else "urlcreds",
                    )

            except asyncio.TimeoutError:
                _LOGGER.debug("Snapshot timeout for %s (url=%s)", self.entity_id, url)
            except Exception as exc:
                _LOGGER.debug("Snapshot error for %s (url=%s): %s", self.entity_id, url, exc)
            return None

        # 1) ONVIF-provided URL — prefer Digest auth by default
        url = self._profile.snapshot_uri
        if url and snapshot_mode != "disabled":
            # Always strip inline creds before Digest attempt
            url_no_creds = _strip_inline_creds(url)
            data = await _fetch(url_no_creds, use_header_auth=False)  # triggers Digest retry on 401
            if data:
                return data

            # Fallback: try inline credentials
            if ("@" not in url) and self._username and snapshot_mode in ("auto", "urlcreds"):
                url2 = _inject_inline_creds(url, self._username, self._password)
                data = await _fetch(url2, use_header_auth=False)
                if data:
                    return data

            # Optional: try Basic header only if URL has no creds
            if ("@" not in url) and snapshot_mode in ("auto", "basic"):
                data = await _fetch(url, use_header_auth=True)
                if data:
                    return data

        # 2) Provision-ISR generic endpoint /snapshot.cgi?user=..&pwd=..
        if snapshot_mode != "disabled" and self._username:
            u = quote(self._username, safe="")
            p = quote(self._password or "", safe="")
            prov_simple = f"http://{self._host}/snapshot.cgi?user={u}&pwd={p}"
            data = await _fetch(prov_simple, use_header_auth=False)
            if data:
                return data

        # 3) Vendor fallbacks if channel known
        ch = self._profile.channel
        if ch and snapshot_mode != "disabled":
            lower_name = (self._profile.name or "").lower()
            subtype = 1 if any(x in lower_name for x in ("sub", "extra", "low")) else 0
            u = quote(self._username or "", safe="")
            p = quote(self._password or "", safe="")
            dahua = f"http://{u}:{p}@{self._host}/cgi-bin/snapshot.cgi?channel={ch}&subtype={subtype}"
            data = await _fetch(dahua, use_header_auth=False)
            if data:
                return data

            hik_ch = f"{ch}01" if subtype == 0 else f"{ch}02"
            hik = f"http://{self._host}/ISAPI/Streaming/channels/{hik_ch}/picture"
            data = await _fetch(hik, use_header_auth=True)
            if not data and self._username and "@" not in hik:
                u = quote(self._username, safe="")
                p = quote(self._password, safe="")
                data = await _fetch(f"http://{u}:{p}@{self._host}/ISAPI/Streaming/channels/{hik_ch}/picture", use_header_auth=False)
            if data:
                return data

        self._snapshot_broken = True
        return None

    @property
    def is_streaming(self) -> bool:
        return True

    async def async_update(self) -> None:
        return

    async def async_will_remove_from_hass(self) -> None:
        # Close dedicated session
        try:
            await self._raw_session.close()
        except Exception:
            pass


async def _reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
