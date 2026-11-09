"""Durable per-device delivery contracts, without a real browser or push service."""
import base64
import json
import os
import stat

import pytest

from hermes_cli import web_push_store as push
from hermes_cli.web_push_keys import prepare_key


@pytest.fixture
def subscription():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"endpoint": "https://fcm.googleapis.com/wp/test-device", "keys": {
        "p256dh": encode(point), "auth": encode(b"a" * 16)}}


@pytest.fixture
def queue(tmp_path, monkeypatch, subscription):
    clock = [1000.0]
    monkeypatch.setattr(push.time, "time", lambda: clock[0])
    store = push.PushStore(tmp_path)
    identity = {"owner": "user-one", "origin": "https://hermes.test", "generation": "generation-one"}
    device = store.subscribe(None, **identity, subscription=subscription)
    return store, device, clock, identity


def event(key="execution-1", *, at=1001.0, seq=1, kind="cron", profile="radio"):
    return {"event_key": key, "seq": seq, "kind": kind, "profile": profile,
            "target": {"profile": profile, "session_id": "saved-run", "message_id": "35"},
            "title": "Radio", "body": "Published result", "created_at": at, "expires_at": at + 86400}


def test_replay_is_idempotent_and_cursor_commits_with_fanout(queue, tmp_path):
    store, device, clock, _identity = queue
    rows = [event(at=999, seq=1, key="before-subscription"), event(seq=2)]
    for _ in range(2):
        store.import_events("radio-state", tmp_path / "state.db", rows, generation="generation-one")
    assert store.cursor("radio-state") == 2
    pending = store.visible_events(device["id"])
    assert len(pending) == 1 and pending[0]["body"] == "Published result"
    clock[0] = 1003
    claimed = store.claim(generation="generation-one")
    assert claimed is not None and claimed[2]["attempts"] == 1
    assert store.claim(generation="generation-one") is None


def test_same_execution_key_in_another_profile_is_independent(queue, tmp_path):
    store, device, _clock, _identity = queue
    store.import_events("radio", tmp_path / "radio.db", [event()], generation="generation-one")
    store.import_events("fx-watch", tmp_path / "fx.db", [event(profile="fx-watch")], generation="generation-one")
    rows = store.visible_events(device["id"])
    assert len(rows) == 2
    assert {row["target"]["profile"] for row in rows} == {"radio", "fx-watch"}
    assert rows[0]["id"] != rows[1]["id"]


def test_display_acknowledgement_suppresses_only_its_device(queue, subscription, tmp_path):
    store, device, clock, identity = queue
    subscription["endpoint"] += "-second"
    second = store.subscribe(None, **identity, subscription=subscription)
    store.import_events("radio", tmp_path / "state.db", [event()], generation="generation-one")
    event_id = store.visible_events(device["id"])[0]["id"]
    store.acknowledge(device["id"], [event_id])
    assert store.visible_events(device["id"]) == []
    clock[0] = 1003
    claimed = store.claim(generation="generation-one")
    assert claimed[1]["id"] == second["id"]


def test_turning_a_kind_off_cancels_queued_deliveries_without_later_replay(queue, tmp_path):
    store, device, clock, identity = queue
    store.import_events("radio", tmp_path / "state.db", [event()], generation="generation-one")
    store.update_preferences(device, kinds={"cron": False}, preview=False)
    updated = store.device(device["id"], **identity)
    assert updated["preview"] is False and updated["kinds"]["reply"] is True
    store.update_preferences(updated, kinds={"cron": True})
    clock[0] = 1003
    assert store.claim(generation="generation-one") is None


def test_owner_origin_generation_are_all_required(queue):
    store, device, _clock, identity = queue
    for field, value in [("owner", "someone-else"), ("origin", "https://another.test"), ("generation", "replaced")]:
        assert store.device(device["id"], **{**identity, field: value}) is None


def test_transient_failure_retries_but_expired_subscription_is_revoked(queue):
    store, device, clock, identity = queue
    event_id = store.test_event(device)
    first = store.claim(generation="generation-one")
    store.settle(device["id"], event_id, result={"ok": False, "status": 429, "retry_after": 20}, attempts=first[2]["attempts"])
    assert store.claim(generation="generation-one") is None
    clock[0] += 21
    second = store.claim(generation="generation-one")
    assert second[2]["attempts"] == 2
    store.settle(device["id"], event_id, result={"ok": False, "status": 410, "error_code": "expired"}, attempts=2)
    assert store.device(device["id"], **identity) is None
    with store.connection() as conn:
        assert json.loads(conn.execute("SELECT subscription FROM devices WHERE id=?", (device["id"],)).fetchone()[0]) == {}


def test_restart_preserves_queue_and_logout_revokes_it(queue, tmp_path):
    store, device, clock, identity = queue
    event_id = store.test_event(device)
    reopened = push.PushStore(tmp_path)
    assert reopened.event_for_device(device["id"], event_id)["id"] == event_id
    assert reopened.revoke(device["id"]) == 1
    assert reopened.device(device["id"], **identity) is None
    clock[0] += 40
    assert reopened.claim(generation="generation-one") is None


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/wp/x", "https://localhost/wp/x", "https://127.0.0.1/wp/x",
    "https://fcm.googleapis.com.attacker.test/wp/x", "https://user:secret@fcm.googleapis.com/wp/x",
    "https://fcm.googleapis.com:8443/wp/x", "https://fcm.googleapis.com/wp/x?redirect=secret"])
def test_subscription_cannot_choose_arbitrary_outbound_destinations(subscription, endpoint):
    with pytest.raises(ValueError):
        push.validate_subscription({**subscription, "endpoint": endpoint})


@pytest.mark.platforms("posix")
def test_queue_and_vapid_key_are_private_and_key_survives_restart(queue):
    store, _device, _clock, _identity = queue
    path, public = prepare_key(store.directory)
    assert prepare_key(store.directory) == (path, public)
    assert len(base64.urlsafe_b64decode(public + "=" * (-len(public) % 4))) == 65
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700


@pytest.mark.platforms("posix")
def test_complete_key_with_interrupted_publication_is_recovered(queue):
    store, _device, _clock, _identity = queue
    path, public = prepare_key(store.directory)
    temporary = store.directory / '.vapid-interrupted'
    os.link(path, temporary)
    assert path.stat().st_nlink == 2
    assert prepare_key(store.directory) == (path, public)
    assert not temporary.exists() and path.stat().st_nlink == 1
