"""Project only a canonical Group's committed, visible member reply."""
import json
from pathlib import Path

from hermes_cli.web_push_events import profile_for_home, record_event


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
