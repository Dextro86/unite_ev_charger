"""REST clients for the Unite web UI (used only for a soft reboot).

Modbus has no reboot register, so a restart goes over the charger's web UI. The
Unite exposes two different web UIs depending on firmware/interface:

  A) a modern JSON API over HTTPS (JWT bearer): ``https://host/api/...``
     (seen on WiFi-connected units)
  B) the legacy "webconfig" PHP portal over HTTP (session cookie + CSRF token)
     (seen on Ethernet-connected units)

Both are auto-detected (``async_build_rest_client``); each exposes the same
``test_connection()`` + ``restart_system()``. Fully isolated from Modbus: if REST
fails, charging control is unaffected. TLS verification is disabled (self-signed).
"""
from __future__ import annotations

import asyncio
import logging
import re

import aiohttp

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)

# Matches the CSRF token the webconfig portal puts in every form.
_TOKEN_RE = re.compile(r'name="token"\s+value="([0-9a-fA-F]+)"')


class UniteRestError(RuntimeError):
    """A REST call could not be completed."""


class UniteRestAuthError(UniteRestError):
    """Authentication failed (wrong web UI username/password)."""


class UniteRestEndpointMissing(UniteRestError):
    """The JSON API lacks a requested endpoint (HTTP 404).

    Some firmware serves a working JSON login on 443/4443 but does not expose
    the ``custom-actions`` restart endpoint. This lets the caller fall back to
    the legacy webconfig portal instead of failing outright.
    """


# --- Variant A: modern JSON API over HTTPS ----------------------------------
class UniteJsonRestClient:
    """login -> JWT bearer -> POST an action. Uses the shared HA session."""

    variant = "json_api"

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        port: int = 443,
    ) -> None:
        self._session = session
        self._base = f"https://{host}:{port}/api"
        self._username = username
        self._password = password
        self._token: str | None = None

    async def _login(self) -> None:
        try:
            async with self._session.post(
                f"{self._base}/login",
                json={"username": self._username, "password": self._password},
                ssl=False,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise UniteRestAuthError("Invalid web UI username or password")
                if resp.status not in (200, 201):
                    raise UniteRestError(f"Unexpected login status {resp.status}")
                data = await resp.json(content_type=None)  # tolerate wrong Content-Type
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UniteRestError(f"Cannot reach the charger web API: {err}") from err
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token:
            raise UniteRestError("Login response did not contain an access token")
        self._token = token

    async def _post(self, path: str) -> None:
        if self._token is None:
            await self._login()
        for attempt in range(2):
            try:
                async with self._session.post(
                    f"{self._base}{path}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    ssl=False,
                    timeout=_TIMEOUT,
                ) as resp:
                    if resp.status in (401, 403) and attempt == 0:
                        self._token = None
                        await self._login()
                        continue
                    if resp.status == 404:
                        raise UniteRestEndpointMissing(
                            f"REST endpoint {path} not present on this firmware (HTTP 404)"
                        )
                    if resp.status not in (200, 201, 202, 204):
                        raise UniteRestError(f"Unexpected status {resp.status} for {path}")
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UniteRestError(f"Cannot reach the charger web API: {err}") from err

    async def test_connection(self) -> None:
        self._token = None
        await self._login()

    async def restart_system(self) -> None:
        await self._post("/custom-actions/restart-system")


# --- Variant B: legacy webconfig PHP portal over HTTP ------------------------
class UnitePhpRestClient:
    """Cookie login -> scrape CSRF token -> POST the Soft Reset.

    Uses its own short-lived session with an *unsafe* cookie jar, because the
    charger is an IP host and aiohttp's default jar drops cookies from IPs.
    """

    variant = "webconfig"

    def __init__(self, host: str, username: str, password: str) -> None:
        self._host = host
        self._base = f"http://{host}"
        self._username = username
        self._password = password

    @staticmethod
    def is_login_page(html: str) -> bool:
        """The login form's submit is ``button_login``; absent once logged in."""
        return "button_login" in html

    @staticmethod
    def extract_token(html: str) -> str | None:
        m = _TOKEN_RE.search(html)
        return m.group(1) if m else None

    def _new_session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=_TIMEOUT
        )

    async def _login_and_token(self, session: aiohttp.ClientSession) -> str:
        try:
            async with session.get(f"{self._base}/") as r:
                await r.read()  # seed the PHPSESSID cookie
            form = {"username": self._username, "pass": self._password, "button_login": "Login"}
            async with session.post(f"{self._base}/", data=form, allow_redirects=False) as r:
                await r.read()
            async with session.get(f"{self._base}/") as r:
                html = await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UniteRestError(f"Cannot reach the charger web UI: {err}") from err
        if self.is_login_page(html):  # still the login form -> bad credentials
            raise UniteRestAuthError("Invalid web UI username or password")
        token = self.extract_token(html)
        if not token:
            raise UniteRestError("Could not read the CSRF token after login")
        return token

    async def test_connection(self) -> None:
        async with self._new_session() as session:
            await self._login_and_token(session)

    async def restart_system(self) -> None:
        async with self._new_session() as session:
            token = await self._login_and_token(session)
            # Verified against a real "Soft Reset -> Confirm" capture (HAR): a
            # form POST to index_main.php with the CSRF token and the submit
            # button's default value "Submit Query" (an empty value is ignored).
            form = {"token": token, "button_soft_reset": "Submit Query"}
            try:
                async with session.post(f"{self._base}/index_main.php", data=form, allow_redirects=False) as r:
                    if r.status not in (200, 302, 303):
                        raise UniteRestError(f"Soft reset returned status {r.status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise UniteRestError(f"Cannot reach the charger web UI: {err}") from err


# --- detection + factory -----------------------------------------------------
# The JSON API is served over HTTPS on 443 and/or 4443 (varies per firmware /
# interface); the legacy webconfig portal is on HTTP/80. A single charger can
# expose both - we prefer the clean JSON API and fall back to webconfig.
JSON_API_PORTS = (443, 4443)
_API_STATUSES = {200, 201, 400, 401, 403}


async def _probe_json_api(session: aiohttp.ClientSession, host: str, port: int) -> bool:
    """True if the JSON API's /api/login answers on this HTTPS port."""
    try:
        async with session.post(
            f"https://{host}:{port}/api/login",
            json={"username": "__probe__", "password": "__probe__"},
            ssl=False,
            timeout=_PROBE_TIMEOUT,
        ) as r:
            await r.read()
            return r.status in _API_STATUSES  # 401/403/etc = the login endpoint exists
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def _has_webconfig(session: aiohttp.ClientSession, host: str) -> bool:
    try:
        async with session.get(f"http://{host}/", timeout=_PROBE_TIMEOUT) as r:
            text = await r.text()
        return 'name="pass"' in text and "button_login" in text
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return False


async def async_build_rest_client(
    session: aiohttp.ClientSession, host: str, username: str, password: str
):
    """Detect which web UI the charger exposes and return the matching client."""
    for port in JSON_API_PORTS:
        if await _probe_json_api(session, host, port):
            _LOGGER.debug("Charger %s exposes the JSON API on port %s", host, port)
            return UniteJsonRestClient(session, host, username, password, port=port)
    if await _has_webconfig(session, host):
        _LOGGER.debug("Charger %s exposes the webconfig (PHP) web UI", host)
        return UnitePhpRestClient(host, username, password)
    raise UniteRestError(
        "No reachable web UI found (tried the JSON API on 443/4443 and the HTTP webconfig portal)"
    )


async def async_restart_charger(
    session: aiohttp.ClientSession, host: str, username: str, password: str
) -> str:
    """Restart the charger over whichever web UI actually supports it.

    Prefers the clean JSON API, but some firmware serves a working JSON login
    without the restart endpoint (it 404s). In that case we fall back to the
    legacy webconfig soft-reset. Returns a short label of the route used.
    Authentication failures are *not* swallowed -- they propagate so the caller
    can report ``auth_failed`` rather than masking bad credentials.
    """
    json_endpoint_missing = False
    for port in JSON_API_PORTS:
        if not await _probe_json_api(session, host, port):
            continue
        client = UniteJsonRestClient(session, host, username, password, port=port)
        try:
            await client.restart_system()
            _LOGGER.debug("Restarted charger %s via JSON API on port %s", host, port)
            return f"json:{port}"
        except UniteRestEndpointMissing:
            json_endpoint_missing = True  # login works here but restart 404s
            _LOGGER.debug(
                "JSON API on %s:%s has no restart endpoint, trying webconfig", host, port
            )

    if await _has_webconfig(session, host):
        await UnitePhpRestClient(host, username, password).restart_system()
        _LOGGER.debug("Restarted charger %s via webconfig soft-reset", host)
        return "webconfig"

    if json_endpoint_missing:
        raise UniteRestError(
            "The JSON API has no restart endpoint on this firmware and no "
            "webconfig portal was found to fall back to"
        )
    raise UniteRestError(
        "No reachable web UI found (tried the JSON API on 443/4443 and the HTTP webconfig portal)"
    )
