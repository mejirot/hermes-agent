"""The Webapp auth boundary also owns device Push grants and notification navigation."""
import base64
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth.base import Session
from hermes_cli.web_routers import web_push as api
from hermes_cli.web_push_store import PushStore


@pytest.fixture
def application(tmp_path, monkeypatch):
    app = FastAPI()
    app.state.ui_surface = "webapp"
    app.state.auth_required = True
    app.state.listener_public_url = "https://hermes.test"
    app.state.webapp_home = tmp_path
    store = PushStore(tmp_path)
    app.state.web_push_runtime = SimpleNamespace(available=True, store=store, public_key="vapid-public")
    monkeypatch.setattr(api.browser_credential, "is_current", lambda value: value == "generation")

    @app.middleware("http")
    async def verified_browser(request: Request, call_next):
        if request.headers.get("x-test-auth") != "absent":
            request.state.session = Session(request.headers.get("x-test-user", "owner"), "", "", "", "basic", 9999999999, "", "")
            request.state.webapp_sign_in = "generation"
        return await call_next(request)

    app.include_router(api.router)
    return app, store


@pytest.fixture
def subscription():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"endpoint": "https://fcm.googleapis.com/wp/api-test", "keys": {"p256dh": encode(point), "auth": encode(b"a" * 16)}}


def test_auth_and_same_origin_are_required(application, subscription):
    app, _store = application
    with TestClient(app, base_url="https://hermes.test") as client:
        assert client.get("/api/webapp/push/status", headers={"x-test-auth": "absent"}).status_code == 401
        response = client.put("/api/webapp/push/subscription", json={"subscription": subscription},
                              headers={"Origin": "https://attacker.test", "Sec-Fetch-Site": "cross-site"})
        assert response.status_code == 403
        assert client.get("/api/webapp/push/status").json()["subscription"] is None


def test_subscription_status_and_test_do_not_disclose_encryption_keys(application, subscription):
    app, store = application
    with TestClient(app, base_url="https://hermes.test", headers={"Origin": "https://hermes.test"}) as client:
        response = client.put("/api/webapp/push/subscription", json={"subscription": subscription})
        assert response.status_code == 200
        assert response.json()["subscription"] == {"endpoint": subscription["endpoint"]}
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        response = client.post("/api/webapp/push/test")
        assert response.status_code == 200
        event_id = response.json()["event_id"]
        viewed = client.get(f"/api/webapp/push/notifications/{event_id}")
        assert viewed.status_code == 200 and viewed.json()["target"] == {}
        assert "source_db" not in viewed.json()
        assert client.post("/api/webapp/push/test").status_code == 429
        assert client.get(f"/api/webapp/push/notifications/{event_id}", headers={"x-test-user": "other"}).status_code == 404
        assert client.delete("/api/webapp/push/subscription").status_code == 200
        assert client.get("/api/webapp/push/status").json()["subscription"] is None
        assert store.claim(generation="generation") is None


def test_preferences_preserve_other_kinds_and_reject_invalid_values(application, subscription):
    app, _store = application
    with TestClient(app, base_url="https://hermes.test", headers={"Origin": "https://hermes.test"}) as client:
        client.put("/api/webapp/push/subscription", json={"subscription": subscription})
        response = client.patch("/api/webapp/push/preferences", json={"kinds": {"cron": False}, "preview": False})
        assert response.json()["kinds"]["reply"] is True
        assert response.json()["kinds"]["cron"] is False
        assert response.json()["preview"] is False
        assert client.patch("/api/webapp/push/preferences", json={"kinds": {"unknown": True}}).status_code == 400
        assert client.patch("/api/webapp/push/preferences", json={"preview": "false"}).status_code == 400


def test_pwa_public_paths_do_not_allow_auth_or_api_neighbors():
    from hermes_cli.dashboard_auth.middleware import _path_is_public

    assert _path_is_public("/sw.js")
    assert _path_is_public("/manifest.webmanifest")
    assert not _path_is_public("/sw.js/private")
    assert not _path_is_public("/pwa/private.json")
    assert not _path_is_public("/api/webapp/push/status")


def test_existing_logout_revokes_push_even_after_auth_cookie_expires(application, subscription, monkeypatch):
    from hermes_cli.dashboard_auth import routes as auth_routes

    app, store = application
    app.include_router(auth_routes.router)
    monkeypatch.setattr(auth_routes, "_audit", lambda *_args, **_kwargs: None)
    with TestClient(app, base_url="https://hermes.test", headers={"Origin": "https://hermes.test"}) as client:
        client.put("/api/webapp/push/subscription", json={"subscription": subscription})
        event_id = client.post("/api/webapp/push/test").json()["event_id"]
        response = client.post("/auth/logout", headers={"x-test-auth": "absent"}, follow_redirects=False)
        assert response.status_code == 302
        assert "hermes_push_device" in response.headers["set-cookie"]
        assert store.claim(generation="generation") is None
        assert client.get(f"/api/webapp/push/notifications/{event_id}").status_code == 404
