"""Push delivery is independent of model execution and respects durable grant state."""
import base64
import json

import pytest

from hermes_cli import web_push_store
from hermes_cli import web_push_worker as worker


def configure_push(home, subject, *, enabled=True):
    push = {"enabled": enabled}
    if subject is not None:
        push["vapid_subject"] = subject
    (home / "config.yaml").write_text(json.dumps({"webapp": {"push": push}}), encoding="utf-8")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    configure_push(tmp_path, "https://example.com")
    store = web_push_store.PushStore(tmp_path)
    device = store.subscribe(None, owner="owner", origin="https://hermes.test", generation="generation",
        subscription={"endpoint": "https://fcm.googleapis.com/wp/worker-test", "keys": {
            "p256dh": encode(point), "auth": encode(b"a" * 16)}}, preview=False)
    value = worker.PushRuntime(tmp_path)
    value.store, value.key_path, value.public_key = store, tmp_path / "key.pem", "public"
    from hermes_cli import browser_credential
    monkeypatch.setattr(browser_credential, "generation", lambda: "generation")
    monkeypatch.setattr(browser_credential, "is_current", lambda value: value == "generation")
    return value, device


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["https://example.com", "mailto:push@example.com"])
async def test_payload_uses_private_sender_and_preview_preference(runtime, monkeypatch, subject):
    value, device = runtime
    assert worker.configured(value.home)
    configure_push(value.home, subject)
    event_id = value.store.test_event(device)
    sent = []

    async def send(request):
        sent.append(request)
        return {"ok": True, "status": 201, "error_code": None}

    monkeypatch.setattr(worker, "run_sender", send)
    assert await value.deliver(set()) is True
    assert await value.deliver(set()) is False
    request = sent[0]
    assert request["payload"]["url"] == f"https://hermes.test/#/push/open/{event_id}"
    assert request["payload"]["body"] == "新しい返信があります。"
    assert "source_db" not in request["payload"]
    assert request["vapid_private_key_path"] == str(value.key_path)
    assert request["vapid_subject"] == subject
    assert request["ttl"] <= 120


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", [None, "", "https://example.com/contact", False])
async def test_missing_or_invalid_contact_preserves_existing_delivery_state(runtime, monkeypatch, subject):
    value, device = runtime
    assert worker.configured(value.home)
    event_id = value.store.test_event(device)
    value.key_path.write_text("existing private key fixture", encoding="utf-8")
    original_key = value.key_path.read_bytes()
    if subject is None:
        (value.home / "config.yaml").unlink()
    else:
        configure_push(value.home, subject)
    sent = []

    async def send(request):
        sent.append(request)
        return {"ok": True, "status": 201}

    monkeypatch.setattr(worker, "run_sender", send)
    assert await value.deliver(set()) is False
    assert sent == []
    assert value.key_path.read_bytes() == original_key
    assert value.store.device(device["id"], owner="owner", origin="https://hermes.test",
                              generation="generation") is not None
    claimed = value.store.claim(generation="generation")
    assert claimed is not None and claimed[0]["id"] == event_id


@pytest.mark.parametrize("enabled,subject", [
    (True, None), (True, ""), (True, "https://example.com/contact"), (True, False),
    (False, "https://example.com"),
])
def test_prepare_without_valid_contact_does_not_create_push_state(tmp_path, monkeypatch, enabled, subject):
    configure_push(tmp_path, subject, enabled=enabled)
    sender = tmp_path / "sender"
    sender.write_text("prepared image executable", encoding="utf-8")
    monkeypatch.setattr(worker, "SENDER_PYTHON", sender)
    monkeypatch.setattr(worker, "SENDER_SCRIPT", sender)
    value = worker.PushRuntime(tmp_path)
    value.prepare()
    assert not value.available
    assert value.store is None and value.key_path is None
    assert not (tmp_path / "webapp").exists()


@pytest.mark.asyncio
async def test_expired_or_resolved_request_never_reaches_transport(runtime, monkeypatch):
    value, device = runtime
    value.store.test_event(device)
    sent = []

    async def send(request):
        sent.append(request)
        return {"ok": True, "status": 201}

    monkeypatch.setattr(worker, "event_is_current", lambda *_: False)
    monkeypatch.setattr(worker, "run_sender", send)
    assert await value.deliver(set()) is True
    assert sent == []


@pytest.mark.asyncio
async def test_revoked_grant_is_rechecked_immediately_before_transport(runtime, monkeypatch):
    value, device = runtime
    value.store.test_event(device)
    sent = []

    def resolve(_event, _sources):
        value.store.revoke(device["id"])
        return True

    async def send(request):
        sent.append(request)
        return {"ok": True, "status": 201}

    monkeypatch.setattr(worker, "event_is_current", resolve)
    monkeypatch.setattr(worker, "run_sender", send)
    assert await value.deliver(set()) is True
    assert sent == []


def test_runtime_registers_global_signout_revocation(runtime, tmp_path, monkeypatch):
    from hermes_cli import browser_credential

    sender = tmp_path / "sender"
    sender.write_text("prepared image executable")
    monkeypatch.setattr(worker, "SENDER_PYTHON", sender)
    monkeypatch.setattr(worker, "SENDER_SCRIPT", sender)
    monkeypatch.setattr(worker, "configured", lambda _home: True)
    revokers = {}
    monkeypatch.setattr(browser_credential, "on_rotate", lambda name, callback: revokers.update({name: callback}))
    value, device = runtime
    value.store.test_event(device)
    value.prepare()
    assert value.available
    assert list(revokers) == [f"web-push:{tmp_path}"]
    # The callback is the same grant invalidation seam used by `hermes webapp --sign-out`.
    assert revokers[f"web-push:{tmp_path}"]() == 1
    assert value.store.device(device["id"], owner="owner", origin="https://hermes.test", generation="generation") is None
    assert value.store.claim(generation="replaced") is None
