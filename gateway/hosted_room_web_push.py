"""Project only a canonical Group's committed, visible member reply."""
import json
from pathlib import Path

from hermes_cli.web_push_events import profile_for_home, record_event


def interactive_group_task(conn, *, room_id, task_id, thread_id, turn_id, member_id, execution_generation=None):
    """Follow the exact task's durable input, never a room's newest message or session label."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hosted_room_driver_tasks'").fetchone() is None:
        return False
    query = '''SELECT e.actor_json,t.payload_json FROM hosted_room_driver_tasks t
        JOIN hosted_room_events e ON e.room_id=t.room_id AND e.seq=t.source_event_seq AND e.kind='message.user'
        WHERE t.room_id=? AND t.task_id=? AND t.thread_id=? AND t.turn_id=?'''
    params = (room_id, task_id, thread_id, turn_id)
    if execution_generation is not None:
        query += ' AND t.execution_generation=?'
        params += (execution_generation,)
    source = conn.execute(query, params).fetchone()
    if source is None:
        return False
    actor, task_payload = json.loads(source[0]), json.loads(source[1])
    # Both Groups send routes set this actor on the server. Scheduled room posts have a
    # different server-owned actor, even when their @mentions start member turns.
    return (actor.get('kind') == 'user' and actor.get('id') == 'desktop'
            and task_payload.get('target_member_id', task_payload.get('target_profile')) == member_id)


def record_group_reply(conn, db_path, *, room_id, event_id, kind, payload, created_at):
    if kind != 'turn.settled' or payload.get('passed') is not False or not payload.get('message_event_id'):
        return
    if any(not isinstance(payload.get(key), str) or not payload[key]
           for key in ('task_id', 'turn_id', 'thread_id', 'member_id')):
        return
    # CanonicalHostedRoomService owns a room in its profile's state.db. Standalone coordination
    # stores and imported/replayed plumbing are not another source of user reply notifications.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_meta'").fetchone() is None:
        return
    if conn.execute('SELECT 1 FROM state_meta WHERE key=?',
                    ('gateway.hosted.owner.v1:' + room_id,)).fetchone() is None:
        return
    if not interactive_group_task(conn, room_id=room_id, **{key: payload[key] for key in (
            'task_id', 'thread_id', 'turn_id', 'member_id')}):
        return
    message = conn.execute('''SELECT actor_json,payload_json FROM hosted_room_events
        WHERE room_id=? AND event_id=? AND kind='message.member' ''',
        (room_id, payload['message_event_id'])).fetchone()
    if message is None:
        return
    visible, actor = json.loads(message['payload_json']), json.loads(message['actor_json'])
    if any(visible.get(key) != payload.get(key) for key in ('task_id', 'turn_id', 'thread_id', 'member_id')):
        return
    text = visible.get('text')
    if not isinstance(text, str) or not text.strip():
        return
    room = conn.execute('SELECT name FROM hosted_rooms WHERE room_id=? AND disbanded_at IS NULL',
                        (room_id,)).fetchone()
    if room is None:
        return
    profile = profile_for_home(Path(db_path).resolve().parent)
    target = {'profile': profile, 'room_id': room_id, 'message_id': payload['message_event_id'],
              **{key: payload[key] for key in ('thread_id', 'turn_id', 'task_id', 'member_id')}}
    record_event(conn, event_key=f'group:{room_id}:{event_id}', kind='reply', profile=profile,
                 target=target, title=f"{room['name'] or 'Hermes Group'}: {actor.get('display_name') or actor['id']}",
                 body=text, created_at=created_at)
