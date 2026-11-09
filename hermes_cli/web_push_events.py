"""Small durable notification facts, written inside their source transaction.

No network, commits, subscriptions or application bootstrap belongs in this module. A sender reads
each source database independently; a sequence is local to that database, never installation-wide.
"""
from __future__ import annotations

from contextlib import closing
import json
import math
from pathlib import Path
import secrets
import sqlite3
import time

KINDS = frozenset({'reply', 'cron', 'approval', 'input'})
_EVENT_TTL = 86400
_ANY_SOURCE = object()


class SourceChangedError(RuntimeError):
    """A profile database was replaced while its notification page was being read."""


def preview(text, limit=160):
    """Force redaction before truncating, including credentials in URLs and invisible controls."""
    from agent.redact import redact_sensitive_text
    text = redact_sensitive_text(str(text or ''), force=True, redact_url_credentials=True)
    return ''.join(char for char in ' '.join(text.split()) if char.isprintable())[:limit]


def profile_for_home(home):
    from hermes_constants import profile_name_for_home
    return profile_name_for_home(home) or 'default'


def record_event(conn, *, event_key, kind, profile, target, title, body, created_at=None, expires_at=None):
    """Insert once using the caller's transaction; rolling it back also removes the event.

    A duplicate key keeps the first committed content. Events never contain credentials, source
    database paths or arbitrary URLs; ``target`` carries only coordinates resolved by the producer.
    """
    if kind not in KINDS or not isinstance(event_key, str) or not event_key or not profile:
        raise ValueError('invalid web push event')
    if not isinstance(target, dict):
        raise ValueError('invalid web push target')
    created = time.time() if created_at is None else float(created_at)
    expires = created + _EVENT_TTL if expires_at is None else float(expires_at)
    if not math.isfinite(created) or not math.isfinite(expires) or expires <= created:
        raise ValueError('invalid web push event lifetime')
    encoded = json.dumps(target, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    conn.execute('''CREATE TABLE IF NOT EXISTS web_push_events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL, profile TEXT NOT NULL, target TEXT NOT NULL,
        title TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS web_push_source (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)''')
    conn.execute('INSERT OR IGNORE INTO web_push_source(singleton,identity) VALUES(1,?)',
                 (secrets.token_hex(16),))
    conn.execute('CREATE INDEX IF NOT EXISTS web_push_expiry ON web_push_events(expires_at)')
    conn.execute('DELETE FROM web_push_events WHERE seq IN '
                 '(SELECT seq FROM web_push_events WHERE expires_at<? LIMIT 100)',
                 (time.time() - 7 * 86400,))
    conn.execute('''INSERT INTO web_push_events
        (event_key,kind,profile,target,title,body,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(event_key) DO NOTHING''',
        (event_key, kind, str(profile), encoded, preview(title, 80), preview(body), created, expires))


def _read_connection(dbpath):
    return sqlite3.connect(Path(dbpath).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)


def _source_identity(conn):
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_push_source'").fetchone() is None:
        return None
    row = conn.execute('SELECT identity FROM web_push_source WHERE singleton=1').fetchone()
    if row is None or len(row[0]) != 32 or any(char not in '0123456789abcdef' for char in row[0]):
        raise ValueError('invalid web push source identity')
    return row[0]


def source_identity(dbpath):
    """A fresh source at a reused profile path must not inherit its predecessor's cursor."""
    with closing(_read_connection(dbpath)) as conn:
        return _source_identity(conn)


def iter_events(dbpath, after_seq, limit=100, *, expected_identity=_ANY_SOURCE):
    """Read one bounded page without creating a database or migrating a source schema."""
    if not Path(dbpath).is_file():
        return []
    with closing(_read_connection(dbpath)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        # Pin identity and rows to one SQLite read snapshot, including a source
        # which had no outbox at the preceding identity probe.
        if expected_identity is not _ANY_SOURCE and _source_identity(conn) != expected_identity:
            raise SourceChangedError('notification source changed')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_push_events'").fetchone() is None:
            return []
        after_seq = max(0, int(after_seq))
        maximum = conn.execute('SELECT MAX(seq) FROM web_push_events').fetchone()[0] or 0
        # An older backup can restore the same identity with a lower sequence. Importing
        # it again is safe: the central event/device keys still prevent duplicate sends.
        if after_seq > maximum:
            after_seq = 0
        rows = conn.execute('SELECT * FROM web_push_events WHERE seq>? ORDER BY seq LIMIT ?',
                            (after_seq, max(1, min(int(limit), 500)))).fetchall()
    return [{**dict(row), 'target': json.loads(row['target'])} for row in rows]


def _group_pending_current(conn, target, principal):
    """Recheck the canonical room/task too; a member may still be unwinding after Group Stop."""
    binding = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           ('gateway.hosted.transport.v1:' + target['session_id'],)).fetchone()
    if binding is not None:
        bound = json.loads(binding[0])
        if bound['selector']['room_id'] != target['room_id'] or bound['selector']['member_id'] != target['member_id']:
            return False
        path = Path(bound['source_home']) / 'state.db'
        if not path.is_file():
            return False
        with closing(_read_connection(path)) as source:
            return _room_wait_is_current(source, target, bound['owner'])
    return _room_wait_is_current(conn, target, principal)


def _room_wait_is_current(conn, target, owner):
    from gateway.owner_identity import same_owner
    row = conn.execute('SELECT value FROM state_meta WHERE key=?',
                       ('gateway.hosted.owner.v1:' + target['room_id'],)).fetchone()
    if row is None or not same_owner(row[0], owner):
        return False
    task = conn.execute('''SELECT t.payload_json,r.members_json FROM hosted_room_driver_tasks t
        JOIN hosted_rooms r ON r.room_id=t.room_id AND r.disbanded_at IS NULL
            AND r.authority_gateway_id=t.run_gateway_id
        WHERE t.room_id=? AND t.task_id=? AND t.execution_generation=? AND t.status='running' ''',
        (target['room_id'], target['task_id'], target['execution_generation'])).fetchone()
    if task is None:
        return False
    payload, members = json.loads(task[0]), json.loads(task[1])
    if (payload.get('target_member_id', payload.get('target_profile')) != target['member_id']
            or payload.get('target_profile') != target['session_profile']):
        return False
    return any(member.get('member_id') == target['member_id'] and member.get('profile') == target['session_profile']
               for member in members)


def pending_event_is_current(event, dbpath=None):
    """Revalidate an ephemeral wait against its source DB; never trusts a path from a push target.

    The sender/click API supplies its enumerated source path, or annotates ``source_db`` locally.
    A settled admission, new runtime epoch, generation change or elapsed deadline fails closed.
    """
    path = dbpath or event.get('source_db')
    target = event.get('target') or {}
    if event.get('kind') not in {'approval', 'input'} or not path or not Path(path).is_file():
        return False
    try:
        if float(event.get('expires_at') or 0) <= time.time():
            return False
        with closing(_read_connection(path)) as conn:
            row = conn.execute('''SELECT a.principal_id FROM web_push_pending p
                JOIN runtime_epoch e ON e.singleton=1 AND e.epoch=p.owner_epoch
                JOIN sessions s ON s.id=p.session_id AND s.runtime_generation=p.generation
                JOIN session_admissions a ON a.admission_id=p.admission_id
                    AND a.target_session_id=p.session_id AND a.generation=p.generation
                    AND a.owner_epoch=p.owner_epoch AND a.status='started'
                WHERE p.event_key=? AND p.profile=? AND p.session_id=? AND p.prompt_id=?
                    AND p.generation=? AND p.admission_id=? AND p.kind=? AND p.active=1 AND p.expires_at>?''',
                (event.get('event_key'), event.get('profile'), target.get('session_id'), target.get('prompt_id'),
                 target.get('generation'), target.get('admission_id'), event['kind'], time.time())).fetchone()
            if row is None:
                return False
            return 'room_id' not in target or _group_pending_current(conn, target, row[0])
    except (sqlite3.Error, KeyError, TypeError, ValueError):
        return False
