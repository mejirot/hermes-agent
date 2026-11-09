"""Notification projections of trusted admissions and generation-bound pending controls."""
from __future__ import annotations

import json
from pathlib import Path
import time

from hermes_cli.web_push_events import profile_for_home, record_event


def record_reply(conn, db, row, outcome, result):
    """Called inside terminal settlement, never from stream deltas or a session's launch source."""
    payload = json.loads(row['payload_json'])
    origin = payload.get('web_push_origin_v1')
    if not isinstance(origin, dict) or origin.get('kind') != 'interactive':
        return
    if payload.get('finite') or payload.get('unattended') or payload.get('kanban_card_v1'):
        return
    value = (result or {}).get('result') or {}
    if outcome not in {'completed', 'failed'}:
        return
    text = value.get('final_response') or ''
    if not isinstance(text, str):
        return
    if outcome == 'completed' and (not text.strip() or text.strip().upper() in {
            '[SILENT]', 'SILENT', 'NO_REPLY', '[NO_REPLY]', 'NO REPLY'}):
        return
    profile = profile_for_home(Path(db.db_path).resolve().parent)
    session_id = value.get('session_id') or row['target_session_id']
    session = conn.execute('SELECT source FROM sessions WHERE id=?', (session_id,)).fetchone()
    # The canonical Bot Chat itself is hidden in Sessions, so hidden alone is not a plumbing test.
    if session is None or session['source'] in {'cron', 'bot_room'}:
        return
    target = {'profile': profile, 'session_id': session_id, 'admission_id': row['admission_id'],
              'generation': row['generation'], 'turn_id': row['admission_id']}
    receipt = value.get('persisted_turn') or {}
    message_id = receipt.get('final_assistant_row_id')
    if (type(message_id) is int and message_id > 0 and conn.execute(
            "SELECT 1 FROM messages WHERE id=? AND session_id=? AND role='assistant'",
            (message_id, session_id)).fetchone() is not None):
        target['message_id'] = str(message_id)
    record_event(conn, event_key='reply:' + row['admission_id'], kind='reply', profile=profile,
                 target=target, title=f"{profile}: {'reply' if outcome == 'completed' else 'reply failed'}",
                 body=text if outcome == 'completed' else 'A reply could not be completed. Open Hermes for details.')


def _pending_key(session_id, prompt):
    return f"pending:{session_id}:{prompt['execution_generation']}:{prompt['kind']}:{prompt['prompt_id']}"


def _hosted_origin(conn, row, profile):
    """A started hosted admission passed the owner's attestation before its claim (not just a label).

    Preserve its exact task/generation. In particular, never add fields to its payload: that payload
    is compared byte-for-byte against the source's attested prompt on every recovery.
    """
    if not row['request_id'].startswith('hosted:'):
        return None
    try:
        session_profile = profile
        task, generation = json.loads(row['request_id'][7:])
        if type(generation) is not int or generation < 1 or set(task) != {'room_id', 'task_id', 'thread_id', 'turn_id'}:
            return None
        bound = conn.execute('SELECT value FROM state_meta WHERE key=?',
                             ('gateway.hosted.transport.v1:' + row['target_session_id'],)).fetchone()
        if bound is not None:
            binding = json.loads(bound[0])
            selector = binding['selector']
            if selector['room_id'] != task['room_id']:
                return None
            member_id, profile = selector['member_id'], profile_for_home(binding['source_home'])
        else:
            owner = conn.execute('SELECT value FROM state_meta WHERE key=?',
                                 ('gateway.hosted.owner.v1:' + task['room_id'],)).fetchone()
            if owner is None:
                return None
            from gateway.owner_identity import same_owner
            if not same_owner(owner[0], row['principal_id']):
                return None
            task_row = conn.execute('''SELECT payload_json FROM hosted_room_driver_tasks
                WHERE room_id=? AND task_id=? AND thread_id=? AND turn_id=?
                    AND execution_generation=? AND status='running' ''',
                (task['room_id'], task['task_id'], task['thread_id'], task['turn_id'], generation)).fetchone()
            if task_row is None:
                return None
            payload = json.loads(task_row[0])
            member_id = payload.get('target_member_id') or payload['target_profile']
        origin = {'kind': 'group', 'profile': profile, **task, 'member_id': member_id,
                  'execution_generation': generation}
        from hermes_cli.web_push_events import _group_pending_current
        if not _group_pending_current(conn, {'session_id': row['target_session_id'],
                'session_profile': session_profile, **origin}, row['principal_id']):
            return None
        return origin
    except (KeyError, TypeError, ValueError):
        return None


def _write_pending(authority, session_id, prompt, active):
    """A pending-state receipt and its event commit together, off the shared gateway loop."""
    profile = profile_for_home(Path(authority.db.db_path).resolve().parent)
    generation = prompt['execution_generation']
    key = _pending_key(session_id, prompt)
    now = time.time()
    expires = min(float(prompt.get('_web_push_expires_at') or now), now + 120)
    kind = 'approval' if prompt['kind'] == 'approval' else 'input'

    def write(conn):
        conn.execute('''CREATE TABLE IF NOT EXISTS web_push_pending (
            event_key TEXT PRIMARY KEY, profile TEXT NOT NULL, session_id TEXT NOT NULL,
            admission_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL, generation INTEGER NOT NULL,
            prompt_id TEXT NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL, expires_at REAL NOT NULL)''')
        conn.execute('CREATE INDEX IF NOT EXISTS web_push_pending_expiry ON web_push_pending(expires_at)')
        # Bounded housekeeping never removes a request whose deadline is still in the future.
        conn.execute('''DELETE FROM web_push_pending WHERE event_key IN (
            SELECT event_key FROM web_push_pending WHERE expires_at<? ORDER BY expires_at LIMIT 100)''',
            (now - 7 * 86400,))
        if not active:
            conn.execute('UPDATE web_push_pending SET active=0 WHERE event_key=?', (key,))
            return
        row = conn.execute('''SELECT a.* FROM session_admissions a JOIN runtime_epoch e
            ON e.singleton=1 AND e.epoch=a.owner_epoch WHERE a.target_session_id=? AND a.generation=?
                AND a.owner_epoch=? AND a.status='started' ''',
            (session_id, generation, authority.epoch)).fetchone()
        if row is None or expires <= now:
            return
        origin = json.loads(row['payload_json']).get('web_push_origin_v1') or _hosted_origin(conn, row, profile)
        if not isinstance(origin, dict) or origin.get('kind') not in {'interactive', 'group'}:
            return
        target = {'profile': profile, 'session_id': session_id, 'admission_id': row['admission_id'],
                  'generation': generation, 'prompt_id': prompt['prompt_id'], 'turn_id': row['admission_id']}
        if origin['kind'] == 'group':
            target['session_profile'] = profile
            target['profile'] = origin['profile']
            target.update({key: origin[key] for key in (
                'room_id', 'thread_id', 'turn_id', 'task_id', 'member_id', 'execution_generation')})
        event_profile = target['profile']
        label = f"{event_profile} / {origin['member_id']}" if origin['kind'] == 'group' else event_profile
        conn.execute('''INSERT INTO web_push_pending VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_key) DO UPDATE SET active=excluded.active''',
            (key, event_profile, session_id, row['admission_id'], authority.epoch, generation,
             prompt['prompt_id'], kind, 1, expires))
        record_event(conn, event_key=key, kind=kind, profile=event_profile, target=target,
                     title=f"{label}: {'Approval needed' if kind == 'approval' else 'Input needed'}",
                     body='Hermes is waiting for your approval.' if kind == 'approval'
                     else 'Hermes is waiting for your input.', created_at=now, expires_at=expires)
    authority.db._execute_write(write)


def queue_pending(authority, session_id, prompt, active):
    """Schedule an ordered, tracked write; a blocking source database cannot freeze Stop or a prompt."""
    from gateway.session_cron import owner_for_home
    owner = owner_for_home(Path(authority.db.db_path).resolve().parent)
    if owner is None or owner[0] is not authority:
        return
    loop = owner[1]
    snapshot = dict(prompt)

    async def persist():
        from gateway.session_runtime_workers import tracked_write
        await tracked_write(authority, lambda: _write_pending(authority, session_id, snapshot, active), ordered=True)

    def schedule():
        from gateway.session_runtime_workers import track_mutation
        track_mutation(authority, persist())
    loop.call_soon_threadsafe(schedule)


def bind_pending(authority, session_id, controls):
    controls.on_change = lambda prompt, active: queue_pending(authority, session_id, prompt, active)


def viewer_wait(events, session_id, method, request_id, timeout):
    """Observe the canonical batch clarify request, whose srq id bypasses PendingControls.

    Sudo/secret/vault requests deliberately do not create Push facts. No prompt text or answer
    crosses this projection; the existing request/response registry continues to own the wait.
    """
    if method != 'clarify':
        return None
    from gateway.session_cron import owner_for_home
    from hermes_constants import get_hermes_home
    owner = owner_for_home(get_hermes_home())
    if owner is None:
        return None
    authority = owner[0]
    live = authority.sessions.get(session_id)
    generation = events.execution.get('execution_generation')
    if live is None or live.event_stream is not events or type(generation) is not int:
        return None
    prompt = {'kind': 'clarify', 'prompt_id': request_id, 'execution_generation': generation,
              '_web_push_expires_at': time.time() + min(120, max(0, timeout if timeout is not None else 120))}
    queue_pending(authority, session_id, prompt, True)
    return lambda: queue_pending(authority, session_id, prompt, False)
