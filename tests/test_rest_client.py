"""Tests for the REST clients (JSON API + webconfig) and the detection factory.

rest_client only imports aiohttp/asyncio/re, so it loads under the `uec` shim
with no Home Assistant. We drive it with fake aiohttp-style sessions.
"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from uec.rest_client import (
    UniteJsonRestClient,
    UnitePhpRestClient,
    UniteRestAuthError,
    UniteRestEndpointMissing,
    UniteRestError,
    async_build_rest_client,
    async_restart_charger,
    async_restore_three_phase,
)


class FakeResp:
    def __init__(self, status: int, json_data: dict | None = None, text: str = "") -> None:
        self.status = status
        self._json = json_data or {}
        self._text = text

    async def json(self, **kwargs):
        return self._json

    async def text(self):
        return self._text

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _RaisingCtx:
    async def __aenter__(self):
        raise aiohttp.ClientError("connection refused")

    async def __aexit__(self, *exc):
        return False


# --- JSON API client (variant A) --------------------------------------------
class FakeSession:
    """Returns queued POST responses; records (url, headers)."""

    def __init__(self, responses: list[FakeResp]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs.get("headers", {})))
        return self._responses.pop(0)


def _json_client(session: FakeSession) -> UniteJsonRestClient:
    return UniteJsonRestClient(session, "10.0.0.5", "admin", "secret")


def test_json_login_then_restart_posts_expected_paths():
    session = FakeSession([FakeResp(201, {"access_token": "tok123"}), FakeResp(202)])
    asyncio.run(_json_client(session).restart_system())
    assert session.calls[0][0] == "https://10.0.0.5:443/api/login"
    assert session.calls[1][0] == "https://10.0.0.5:443/api/custom-actions/restart-system"
    assert session.calls[1][1]["Authorization"] == "Bearer tok123"


def test_json_invalid_credentials_raise_auth_error():
    session = FakeSession([FakeResp(401)])
    with pytest.raises(UniteRestAuthError):
        asyncio.run(_json_client(session).test_connection())


def test_json_expired_token_triggers_relogin():
    session = FakeSession([
        FakeResp(201, {"access_token": "old"}),
        FakeResp(401),
        FakeResp(201, {"access_token": "new"}),
        FakeResp(204),
    ])
    asyncio.run(_json_client(session).restart_system())
    assert session.calls[-1][1]["Authorization"] == "Bearer new"


def test_json_parses_body_despite_wrong_content_type():
    class WrongCT(FakeResp):
        async def json(self, content_type="application/json"):
            return self._json if content_type is None else None

    session = FakeSession([WrongCT(201, {"access_token": "tok"})])
    asyncio.run(_json_client(session).test_connection())  # must not raise


# --- webconfig PHP client (variant B) pure helpers --------------------------
def test_php_is_login_page():
    assert UnitePhpRestClient.is_login_page('<input name="button_login">') is True
    assert UnitePhpRestClient.is_login_page('<a href="logout.php">out</a>') is False


def test_php_extract_token():
    html = '<input type="hidden" name="token" value="478965a133aa">'
    assert UnitePhpRestClient.extract_token(html) == "478965a133aa"
    assert UnitePhpRestClient.extract_token("<html>no token</html>") is None


# --- detection factory ------------------------------------------------------
class DetectSession:
    """Fake: /api/login answers on the given HTTPS ports; GET / returns the body."""

    def __init__(self, api_ports, webconfig_body: str = "") -> None:
        self._api_ports = set(api_ports)
        self._webconfig_body = webconfig_body

    def post(self, url, **kwargs):  # JSON-API probe: https://host:PORT/api/login
        for p in self._api_ports:
            if f":{p}/" in url:
                return FakeResp(403)  # endpoint exists, rejected the probe creds
        return _RaisingCtx()  # port closed / not the API

    def get(self, url, **kwargs):  # webconfig probe: http://host/
        return FakeResp(200, text=self._webconfig_body)


_LOGIN_FORM = '<input name="username"><input type="password" name="pass"><input name="button_login">'


def test_detect_json_api_on_443():
    client = asyncio.run(async_build_rest_client(DetectSession({443}), "10.0.0.5", "admin", "x"))
    assert isinstance(client, UniteJsonRestClient)
    assert ":443/api" in client._base


def test_detect_json_api_on_4443_when_443_closed():
    # charger 2 pattern: 443 closed, API on 4443
    client = asyncio.run(async_build_rest_client(DetectSession({4443}), "10.0.0.5", "admin", "x"))
    assert isinstance(client, UniteJsonRestClient)
    assert ":4443/api" in client._base


def test_detect_webconfig_when_no_api():
    session = DetectSession(set(), webconfig_body=_LOGIN_FORM)
    client = asyncio.run(async_build_rest_client(session, "10.0.0.5", "admin", "x"))
    assert isinstance(client, UnitePhpRestClient)


def test_detect_none_raises():
    session = DetectSession(set(), webconfig_body="<html>something else</html>")
    with pytest.raises(UniteRestError):
        asyncio.run(async_build_rest_client(session, "10.0.0.5", "admin", "x"))


# --- restart orchestration + 404 fallback -----------------------------------
def test_json_restart_404_raises_endpoint_missing():
    # a 404 on the restart endpoint is a distinct error, not a generic failure
    session = FakeSession([FakeResp(201, {"access_token": "tok"}), FakeResp(404)])
    with pytest.raises(UniteRestEndpointMissing):
        asyncio.run(_json_client(session).restart_system())


class RestartSession:
    """Drives async_restart_charger: JSON login on given ports, restart status,
    plus a webconfig GET body. Records the JSON restart URLs it was asked for."""

    def __init__(self, json_ports, *, restart_status=404, login_status=201, webconfig_body=""):
        self.json_ports = set(json_ports)
        self.restart_status = restart_status
        self.login_status = login_status
        self.webconfig_body = webconfig_body
        self.restart_urls: list[str] = []

    def post(self, url, **kwargs):
        if url.endswith("/api/login"):
            if not any(f":{p}/" in url for p in self.json_ports):
                return _RaisingCtx()  # port closed
            creds = kwargs.get("json") or {}
            if creds.get("username") == "__probe__":
                return FakeResp(403)  # probe: login endpoint exists
            return FakeResp(self.login_status, {"access_token": "tok"})
        if "restart-system" in url:
            self.restart_urls.append(url)
            return FakeResp(self.restart_status)
        return _RaisingCtx()

    def get(self, url, **kwargs):
        return FakeResp(200, text=self.webconfig_body)


def test_restart_uses_json_when_endpoint_present():
    session = RestartSession({443}, restart_status=204)
    route = asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert route == "json:443"


def test_restart_falls_back_to_webconfig_on_json_404(monkeypatch):
    called: list[str] = []

    async def fake_php_restart(self):
        called.append(self._host)

    monkeypatch.setattr(UnitePhpRestClient, "restart_system", fake_php_restart)
    # charger-2 pattern: JSON API on 4443, restart endpoint 404s, webconfig on 80
    session = RestartSession({4443}, restart_status=404, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert route == "webconfig"
    assert called == ["10.0.0.5"]  # webconfig reset actually fired
    assert session.restart_urls  # and only after the JSON restart was tried


def test_restart_auth_error_not_masked_by_fallback(monkeypatch):
    called: list[str] = []

    async def fake_php_restart(self):
        called.append(self._host)

    monkeypatch.setattr(UnitePhpRestClient, "restart_system", fake_php_restart)
    # bad credentials: the real login returns 403 -> must surface as auth error,
    # not silently retried against webconfig
    session = RestartSession({4443}, login_status=403, webconfig_body=_LOGIN_FORM)
    with pytest.raises(UniteRestAuthError):
        asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))
    assert called == []  # webconfig was never touched


def test_restart_json_404_and_no_webconfig_raises():
    session = RestartSession({4443}, restart_status=404, webconfig_body="<html>nope</html>")
    with pytest.raises(UniteRestError):
        asyncio.run(async_restart_charger(session, "10.0.0.5", "admin", "x"))


# --- phase-config restore (currentLimiterPhase 0->1) ------------------------
_PHASE_FIELD = "installationSettings.currentLimiterPhase"


class ConfigSession:
    """JSON session: login ok, then queued statuses for /configuration-updates.
    Records the posted JSON bodies so we can assert the payload shape."""

    def __init__(self, statuses):
        self._statuses = list(statuses)
        self.bodies: list = []

    def post(self, url, **kwargs):
        if url.endswith("/api/login"):
            return FakeResp(201, {"access_token": "tok"})
        self.bodies.append(kwargs.get("json"))
        return FakeResp(self._statuses.pop(0))


def test_json_set_phase_accepts_plain_int():
    s = ConfigSession([200])
    asyncio.run(_json_client(s).set_current_limiter_phase(0))
    assert s.bodies == [[{"fieldKey": _PHASE_FIELD, "value": 0}]]


def test_json_set_phase_falls_back_to_nested_on_422():
    # firmware that rejects the plain int -> retry with the nested selection form
    s = ConfigSession([422, 200])
    asyncio.run(_json_client(s).set_current_limiter_phase(1))
    assert s.bodies[0] == [{"fieldKey": _PHASE_FIELD, "value": 1}]
    assert s.bodies[1] == [
        {"fieldKey": _PHASE_FIELD, "value": {"value": 1, "valueType": "selection"}}
    ]


def test_php_selected_option_reads_current_limiter_value():
    html = (
        '<select name="currentLimiterValue">'
        '<option value="6">6</option>'
        '<option value="16" selected>16</option>'
        '</select>'
    )
    assert UnitePhpRestClient._selected_option(html, "currentLimiterValue") == "16"
    assert UnitePhpRestClient._selected_option(html, "nope") is None


class RestoreSession:
    """Drives async_restore_three_phase: JSON login on given ports + config
    writes; records config bodies; serves a webconfig GET body."""

    def __init__(self, json_ports, *, config_status=200, webconfig_body=""):
        self.json_ports = set(json_ports)
        self.config_status = config_status
        self.webconfig_body = webconfig_body
        self.config_posts: list = []

    def post(self, url, **kwargs):
        if url.endswith("/api/login"):
            if not any(f":{p}/" in url for p in self.json_ports):
                return _RaisingCtx()
            return FakeResp(201, {"access_token": "tok"})
        if "configuration-updates" in url:
            self.config_posts.append(kwargs.get("json"))
            return FakeResp(self.config_status)
        return _RaisingCtx()

    def get(self, url, **kwargs):
        return FakeResp(200, text=self.webconfig_body)


def test_restore_three_phase_toggles_via_json():
    session = RestoreSession({443})
    route = asyncio.run(
        async_restore_three_phase(session, "10.0.0.5", "admin", "x", settle_s=0)
    )
    assert route == "json:443"
    # 0 then 1, both plain int
    assert session.config_posts == [
        [{"fieldKey": _PHASE_FIELD, "value": 0}],
        [{"fieldKey": _PHASE_FIELD, "value": 1}],
    ]


def test_restore_three_phase_falls_back_to_webconfig(monkeypatch):
    calls: list[int] = []

    async def fake_php_set(self, value):
        calls.append(value)

    monkeypatch.setattr(UnitePhpRestClient, "set_current_limiter_phase", fake_php_set)
    # JSON login works but the config endpoint 404s -> webconfig
    session = RestoreSession({4443}, config_status=404, webconfig_body=_LOGIN_FORM)
    route = asyncio.run(
        async_restore_three_phase(session, "10.0.0.5", "admin", "x", settle_s=0)
    )
    assert route == "webconfig"
    assert calls == [0, 1]  # toggle ran on the webconfig client
