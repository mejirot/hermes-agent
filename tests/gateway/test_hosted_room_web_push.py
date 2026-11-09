"""Only a native Group's committed visible reply produces a notification."""
import time

import pytest

from gateway import hosted_room_driver as driver, hosted_rooms
from hermes_cli.web_push_events import iter_events
from hermes_state import SessionDB


COORDINATES = {'task_id': 'task', 'turn_id': 'turn', 'thread_id': 'thread', 'member_id': 'member'}


def _room(tmp_path, monkeypatch, *, native=True, source_actor=None):
    home = tmp_path / 'profiles' / 'room-owner'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(db_path=home / 'state.db')
    if native:
        db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                              ('gateway.hosted.owner.v1:room', 'human')))
    hosted_rooms.create_room(db.db_path, room_id='room', name='Project room',
        members=[{'member_id': 'member', 'profile': 'member-bot', 'handle': 'bot'}],
        authority_gateway_id='gateway')
    source = hosted_rooms.append_event(db.db_path, room_id='room', event_id='input', kind='message.user',
        actor=source_actor or {'kind': 'user', 'id': 'desktop'},
        payload={'text': '@bot hello', 'thread_id': 'thread'}, authority_gateway_id='gateway', authority_epoch=1)
    driver.admit_task(db.db_path, driver.TaskIdentity('room', 'task', 'thread', 'turn'),
        payload={'target_profile': 'member-bot', 'target_member_id': 'member',
                 'source_event_seq': source['seq'], 'prompt': 'hello'}, clock=time.time)
    return db


def _append(db, event_id, kind, payload):
    actor = {'kind': 'member', 'id': 'member', 'profile': 'member-bot'} if kind == 'message.member' else {
        'kind': 'gateway', 'id': 'gateway'}
    return hosted_rooms.append_event(db.db_path, room_id='room', event_id=event_id, kind=kind, actor=actor,
        payload=payload, authority_gateway_id='gateway', authority_epoch=1)


def test_member_message_is_not_notified_until_canonical_settlement_and_replay_is_once(tmp_path, monkeypatch):
    db = _room(tmp_path, monkeypatch)
    with db:
        _append(db, 'visible-message', 'message.member', {**COORDINATES, 'text': 'Final room reply'})
        assert iter_events(db.db_path, 0) == []
        terminal = {**COORDINATES, 'passed': False, 'message_event_id': 'visible-message'}
        for _ in range(2):
            _append(db, 'settled', 'turn.settled', terminal)
        event, = iter_events(db.db_path, 0)
        assert event['profile'] == 'room-owner'
        assert event['target'] == {'profile': 'room-owner', 'room_id': 'room',
                                   'message_id': 'visible-message', **COORDINATES}
        assert event['body'] == 'Final room reply'


@pytest.mark.parametrize(('kind', 'payload'), [
    ('turn.settled', {'passed': True, 'message_event_id': None}),
    ('turn.failed', {'error': 'private raw provider cause'}),
    ('turn.cancelled', {'reason': 'superseded_by_newer_user_event'}),
])
def test_pass_failure_and_superseded_plumbing_do_not_notify(tmp_path, monkeypatch, kind, payload):
    db = _room(tmp_path, monkeypatch)
    with db:
        _append(db, 'terminal', kind, {**COORDINATES, **payload})
        assert iter_events(db.db_path, 0) == []


def test_standalone_room_is_not_a_second_notification_source(tmp_path, monkeypatch):
    db = _room(tmp_path, monkeypatch, native=False)
    with db:
        _append(db, 'message', 'message.member', {**COORDINATES, 'text': 'reply'})
        _append(db, 'settled', 'turn.settled', {**COORDINATES, 'passed': False, 'message_event_id': 'message'})
        assert iter_events(db.db_path, 0) == []


@pytest.mark.parametrize('source_id', ['cron:scheduled-job', 'cron-notice:scheduled-job', 'discord'])
def test_noninteractive_source_does_not_inherit_a_later_desktop_message(tmp_path, monkeypatch, source_id):
    db = _room(tmp_path, monkeypatch, source_actor={'kind': 'user', 'id': source_id})
    with db:
        hosted_rooms.append_event(db.db_path, room_id='room', event_id='newer-input', kind='message.user',
            actor={'kind': 'user', 'id': 'desktop'}, payload={'text': 'Later human message', 'thread_id': 'other'},
            authority_gateway_id='gateway', authority_epoch=1)
        _append(db, 'message', 'message.member', {**COORDINATES, 'text': 'Scheduled reply'})
        _append(db, 'settled', 'turn.settled', {**COORDINATES, 'passed': False, 'message_event_id': 'message'})
        assert iter_events(db.db_path, 0) == []


@pytest.mark.parametrize('coordinate', ['task_id', 'thread_id', 'turn_id', 'member_id'])
def test_reply_must_match_the_exact_source_task(tmp_path, monkeypatch, coordinate):
    db = _room(tmp_path, monkeypatch)
    with db:
        other = {**COORDINATES, coordinate: 'other'}
        _append(db, 'message', 'message.member', {**other, 'text': 'Unrelated reply'})
        _append(db, 'settled', 'turn.settled', {**other, 'passed': False, 'message_event_id': 'message'})
        assert iter_events(db.db_path, 0) == []


def test_failed_event_write_rolls_back_canonical_terminal_append(tmp_path, monkeypatch):
    from gateway import hosted_room_web_push
    db = _room(tmp_path, monkeypatch)
    with db:
        _append(db, 'message', 'message.member', {**COORDINATES, 'text': 'reply'})
        original = hosted_room_web_push.record_event
        def rolled_back(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('rollback canonical event')
        monkeypatch.setattr(hosted_room_web_push, 'record_event', rolled_back)
        terminal = {**COORDINATES, 'passed': False, 'message_event_id': 'message'}
        with pytest.raises(RuntimeError, match='rollback canonical'):
            _append(db, 'settled', 'turn.settled', terminal)
        assert hosted_rooms.room_state(db.db_path, room_id='room')['latest_seq'] == 2
        assert iter_events(db.db_path, 0) == []
        monkeypatch.setattr(hosted_room_web_push, 'record_event', original)
        _append(db, 'settled', 'turn.settled', terminal)
        assert len(iter_events(db.db_path, 0)) == 1
