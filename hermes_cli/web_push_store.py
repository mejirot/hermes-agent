"""Private browser subscriptions and the durable, per-device Web Push delivery queue.

Source transactions own their event rows. This installation-owned database imports those
rows idempotently; a cursor only advances in the transaction that records every delivery.
It never contains provider access/refresh tokens or the VAPID private key.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

KINDS = ("cron", "reply", "approval", "input")
DEFAULT_KINDS = dict.fromkeys(KINDS, True)
COOKIE_NAME = "__Host-hermes_push_device"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, origin TEXT NOT NULL, generation TEXT NOT NULL,
 subscription TEXT NOT NULL, kinds TEXT NOT NULL, preview INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 last_status INTEGER, last_error TEXT, last_sent_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS push_endpoint ON devices(json_extract(subscription,'$.endpoint'));
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, source_key TEXT NOT NULL, event_key TEXT NOT NULL,
 source_db TEXT, kind TEXT NOT NULL, profile TEXT NOT NULL, target TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 UNIQUE(source_key,event_key)
);
CREATE TABLE IF NOT EXISTS deliveries (
 event_id TEXT NOT NULL REFERENCES events(id), device_id TEXT NOT NULL REFERENCES devices(id),
 status TEXT NOT NULL DEFAULT 'pending', due_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 lease_until REAL, http_status INTEGER, error_code TEXT,
 PRIMARY KEY(event_id,device_id)
);
CREATE INDEX IF NOT EXISTS push_due ON deliveries(status,due_at,lease_until);
CREATE TABLE IF NOT EXISTS cursors (source_key TEXT PRIMARY KEY, seq INTEGER NOT NULL);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _decode_key(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) > 128:
        raise ValueError("Invalid subscription key")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid subscription key") from exc
    if len(raw) != size:
        raise ValueError("Invalid subscription key")
    return raw


def validate_subscription(value: object) -> dict:
    """Only Chromium's Android Web Push service is an outbound destination in v1."""
    if not isinstance(value, dict) or not isinstance(value.get("endpoint"), str):
        raise ValueError("Invalid subscription")
    endpoint = value["endpoint"]
    if len(endpoint) > 4096 or any(ord(char) < 33 for char in endpoint):
        raise ValueError("Invalid subscription endpoint")
    try:
        url = urlsplit(endpoint)
        valid = (url.scheme == "https" and url.hostname == "fcm.googleapis.com"
                 and url.port in (None, 443) and not url.username and not url.password
                 and not url.query and not url.fragment and url.path.startswith(("/fcm/send/", "/wp/")))
    except ValueError as exc:
        raise ValueError("Invalid subscription endpoint") from exc
    if not valid:
        raise ValueError("Unsupported push service; use Android Brave or Chrome")
    keys = value.get("keys")
    if not isinstance(keys, dict):
        raise ValueError("Invalid subscription keys")
    point = _decode_key(keys.get("p256dh"), 65)
    _decode_key(keys.get("auth"), 16)
    from cryptography.hazmat.primitives.asymmetric import ec
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    return {"endpoint": endpoint, "keys": {key: keys[key] for key in ("p256dh", "auth")}}


def preferences(kinds=None, preview=None, *, previous=None) -> tuple[dict, bool]:
    current = dict(DEFAULT_KINDS if previous is None else previous["kinds"])
    if kinds is not None:
        if not isinstance(kinds, dict) or set(kinds) - set(KINDS) or any(type(v) is not bool for v in kinds.values()):
            raise ValueError("Invalid notification kinds")
        current.update(kinds)
    if preview is not None and type(preview) is not bool:
        raise ValueError("Invalid preview setting")
    return current, (previous["preview"] if previous is not None else True) if preview is None else preview


class PushStore:
    def __init__(self, home: Path):
        self.directory = Path(home) / "webapp" / "push"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "delivery.sqlite3"
        with self.connection() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def connection(self):
        # umask is process-global; never change it in a multiplexed server thread.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def device(self, device_id: str | None, *, owner: str, origin: str, generation: str):
        if not device_id:
            return None
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id=? AND owner=? AND origin=? AND generation=? AND active=1",
                               (device_id, owner, origin, generation)).fetchone()
        return self._device(row) if row else None

    @staticmethod
    def _device(row) -> dict:
        result = dict(row)
        result["subscription"] = json.loads(result["subscription"])
        result["kinds"] = json.loads(result["kinds"])
        result["preview"] = bool(result["preview"])
        return result

    def subscribe(self, device_id, *, owner, origin, generation, subscription, kinds=None, preview=None):
        subscription = validate_subscription(subscription)
        previous = self.device(device_id, owner=owner, origin=origin, generation=generation)
        chosen, show_preview = preferences(kinds, preview, previous=previous)
        device_id = previous["id"] if previous else secrets.token_urlsafe(24)
        now = time.time()
        with self.connection() as conn:
            # An endpoint cannot be silently taken from a different signed-in browser identity.
            conflict = conn.execute("SELECT id,owner FROM devices WHERE json_extract(subscription,'$.endpoint')=?",
                                    (subscription["endpoint"],)).fetchone()
            if conflict and conflict["id"] != device_id:
                raise ValueError("This subscription is already registered; disable it before registering again")
            conn.execute("""INSERT INTO devices(id,owner,origin,generation,subscription,kinds,preview,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET subscription=excluded.subscription,
                kinds=excluded.kinds,preview=excluded.preview,updated_at=excluded.updated_at,active=1,
                last_error=NULL,last_status=NULL""",
                         (device_id, owner, origin, generation, _json(subscription), _json(chosen), show_preview, now, now))
        return self.device(device_id, owner=owner, origin=origin, generation=generation)

    def update_preferences(self, device, *, kinds=None, preview=None):
        chosen, show_preview = preferences(kinds, preview, previous=device)
        with self.connection() as conn:
            conn.execute("UPDATE devices SET kinds=?,preview=?,updated_at=? WHERE id=? AND active=1",
                         (_json(chosen), show_preview, time.time(), device["id"]))
            for kind, enabled in chosen.items():
                if not enabled:
                    conn.execute("UPDATE deliveries SET status='disabled' WHERE device_id=? AND status IN ('pending','sending') "
                                 "AND event_id IN (SELECT id FROM events WHERE kind=? AND source_key!='test')",
                                 (device["id"], kind))

    def revoke(self, device_id: str) -> int:
        with self.connection() as conn:
            changed = conn.execute("UPDATE devices SET active=0,subscription='{}' WHERE id=? AND active=1",
                                   (device_id,)).rowcount
            conn.execute("UPDATE deliveries SET status='revoked' WHERE device_id=? AND status IN ('pending','sending')",
                         (device_id,))
        return changed

    def revoke_all(self) -> int:
        with self.connection() as conn:
            changed = conn.execute("UPDATE devices SET active=0,subscription='{}' WHERE active=1").rowcount
            conn.execute("UPDATE deliveries SET status='revoked' WHERE status IN ('pending','sending')")
        return changed

    def cursor(self, source_key: str) -> int:
        with self.connection() as conn:
            row = conn.execute("SELECT seq FROM cursors WHERE source_key=?", (source_key,)).fetchone()
        return row[0] if row else 0

    def import_events(self, source_key, source_db, rows, *, generation):
        if not rows:
            return
        now = time.time()
        with self.connection() as conn:
            devices = [self._device(row) for row in conn.execute(
                "SELECT * FROM devices WHERE active=1 AND generation=?", (generation,))]
            for row in rows:
                event_id = self._insert_event(conn, source_key, source_db, row)
                if row["expires_at"] <= now:
                    continue
                for device in devices:
                    if row["created_at"] >= device["created_at"] and device["kinds"].get(row["kind"], False):
                        conn.execute("INSERT OR IGNORE INTO deliveries(event_id,device_id,due_at) VALUES(?,?,?)",
                                     (event_id, device["id"], now + 2))
            conn.execute("INSERT INTO cursors(source_key,seq) VALUES(?,?) ON CONFLICT(source_key) DO UPDATE SET seq=excluded.seq",
                         (source_key, rows[-1]["seq"]))

    @staticmethod
    def _insert_event(conn, source_key, source_db, row):
        event_id = hashlib.sha256(_json([source_key, row["event_key"]]).encode()).hexdigest()
        conn.execute("""INSERT OR IGNORE INTO events(id,source_key,event_key,source_db,kind,profile,target,title,body,created_at,expires_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (event_id, source_key, row["event_key"], str(source_db) if source_db else None,
            row["kind"], row["profile"], _json(row["target"]), row["title"], row["body"], row["created_at"], row["expires_at"]))
        return event_id

    def test_event(self, device) -> str:
        now = time.time()
        row = {"event_key": secrets.token_hex(16), "kind": "reply", "profile": "", "target": {},
               "title": "Hermes", "body": "スマートフォン通知の接続を確認できました。", "created_at": now, "expires_at": now + 120}
        with self.connection() as conn:
            recent = conn.execute("SELECT 1 FROM deliveries d JOIN events e ON e.id=d.event_id WHERE d.device_id=? "
                                  "AND e.source_key='test' AND e.created_at>?", (device["id"], now - 5)).fetchone()
            if recent:
                raise ValueError("Wait a few seconds before sending another test")
            event_id = self._insert_event(conn, "test", None, row)
            conn.execute("INSERT INTO deliveries(event_id,device_id,due_at) VALUES(?,?,?)", (event_id, device["id"], now))
        return event_id

    @staticmethod
    def _event(row):
        result = dict(row)
        result["target"] = json.loads(result["target"])
        return result

    def visible_events(self, device_id):
        with self.connection() as conn:
            rows = conn.execute("SELECT e.* FROM events e JOIN deliveries d ON e.id=d.event_id "
                "WHERE d.device_id=? AND d.status='pending' AND e.expires_at>? ORDER BY e.created_at DESC LIMIT 100",
                (device_id, time.time())).fetchall()
        return [self._event(row) for row in rows]

    def acknowledge(self, device_id, event_ids):
        with self.connection() as conn:
            conn.executemany("UPDATE deliveries SET status='acknowledged' WHERE device_id=? AND event_id=? AND status='pending'",
                             ((device_id, event_id) for event_id in event_ids))

    def event_for_device(self, device_id, event_id):
        with self.connection() as conn:
            row = conn.execute("SELECT e.* FROM events e JOIN deliveries d ON e.id=d.event_id WHERE d.device_id=? AND e.id=?",
                               (device_id, event_id)).fetchone()
        return self._event(row) if row else None

    def claim(self, *, generation):
        now = time.time()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE deliveries SET status='expired' WHERE status IN ('pending','sending') "
                         "AND event_id IN (SELECT id FROM events WHERE expires_at<=?)", (now,))
            row = conn.execute("""SELECT d.event_id,d.device_id FROM deliveries d JOIN events e ON e.id=d.event_id
                JOIN devices b ON b.id=d.device_id WHERE b.active=1 AND b.generation=? AND e.expires_at>?
                AND ((d.status='pending' AND d.due_at<=?) OR (d.status='sending' AND d.lease_until<=?))
                ORDER BY CASE WHEN e.kind IN ('approval','input') THEN 0 ELSE 1 END,d.due_at LIMIT 1""",
                (generation, now, now, now)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE deliveries SET status='sending',lease_until=?,attempts=attempts+1 WHERE event_id=? AND device_id=?",
                         (now + 30, row["event_id"], row["device_id"]))
            event = self._event(conn.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone())
            device = self._device(conn.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone())
            delivery = dict(conn.execute("SELECT * FROM deliveries WHERE event_id=? AND device_id=?",
                                        (row["event_id"], row["device_id"])).fetchone())
        return event, device, delivery

    def can_send(self, device_id, event_id, generation):
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM deliveries d JOIN devices b ON b.id=d.device_id "
                "WHERE d.event_id=? AND d.device_id=? AND d.status='sending' AND b.active=1 AND b.generation=?",
                (event_id, device_id, generation)).fetchone() is not None

    def settle(self, device_id, event_id, *, result, attempts):
        now = time.time()
        http_status = result.get("status")
        http_status = http_status if type(http_status) is int else None
        transient = http_status is None or http_status == 429 or 500 <= http_status <= 599
        state = "sent" if result.get("ok") else "pending" if transient and attempts < 6 else "failed"
        retry = result.get("retry_after")
        delay = min(3600, max(5, retry)) if type(retry) in (int, float) and math.isfinite(retry) else min(600, 5 * 3 ** attempts)
        error = result.get("error_code")
        error = error if isinstance(error, str) and error.replace("_", "").isalnum() and len(error) <= 64 else None
        with self.connection() as conn:
            conn.execute("UPDATE deliveries SET status=?,due_at=?,lease_until=NULL,http_status=?,error_code=? "
                         "WHERE device_id=? AND event_id=? AND status='sending'",
                         (state, now + delay, http_status, error, device_id, event_id))
            conn.execute("UPDATE devices SET last_status=?,last_error=?,last_sent_at=CASE WHEN ? THEN ? ELSE last_sent_at END WHERE id=?",
                         (http_status, error, state == "sent", now, device_id))
        if http_status in (404, 410):
            self.revoke(device_id)

    def discard(self, device_id, event_id, reason="expired"):
        with self.connection() as conn:
            conn.execute("UPDATE deliveries SET status=?,lease_until=NULL WHERE device_id=? AND event_id=? AND status='sending'",
                         (reason, device_id, event_id))

    def prune(self):
        cutoff = time.time() - 7 * 86400
        with self.connection() as conn:
            conn.execute("DELETE FROM deliveries WHERE event_id IN (SELECT id FROM events WHERE expires_at<?)", (cutoff,))
            conn.execute("DELETE FROM events WHERE expires_at<?", (cutoff,))
