"""Notification eligibility belongs to the admitted input and commits with its terminal result."""
from types import SimpleNamespace
from dataclasses import asdict
import json
import queue
import threading
import time

import pytest

from hermes_cli.web_push_events import iter_events, pending_event_is_current
from hermes_state import SessionDB
from hermes_state_runtime import (
    admit_session_input, begin_runtime_epoch, claim_session_input, get_session_admission,
    resolve_unknown_session_input, settle_session_input,
)


def _claimed(tmp_path, monkeypatch, *, trusted=True, request_id='request', **payload):
    home = tmp_path / 'profiles' / 'target-bot'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    db = SessionDB(db_path=home / 'state.db')
    db.create_session('saved', source='cli')
    epoch = begin_runtime_epoch(db, instance_id='owner')
    data = {'text': 'hello', **payload}
    if trusted:
        data['web_push_origin_v1'] = {'kind': 'interactive'}
    admit_session_input(db, epoch=epoch, principal_id='human', session_id='saved', request_id=request_id, payload=data)
    row = claim_session_input(db, epoch=epoch, session_id='saved')
    return db, epoch, row


def _settle(db, epoch, row, text='final visible reply', **kwargs):
    message_id = db.append_message('saved', 'assistant', text)
    return settle_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                                outcome='completed', result={'result': {'final_response': text,
                                    'session_id': 'saved', 'persisted_turn': {'final_assistant_row_id': message_id}}}, **kwargs)


def test_reply_records_exact_profile_session_turn_and_message(tmp_path, monkeypatch):
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    with db:
        _settle(db, epoch, row)
        event, = iter_events(db.db_path, 0)
        assert event['profile'] == 'target-bot'
        message_id = db.get_messages('saved')[-1]['id']
        assert event['target'] == {'profile': 'target-bot', 'session_id': 'saved', 'message_id': str(message_id),
            'admission_id': row['admission_id'], 'turn_id': row['admission_id'], 'generation': row['generation']}


def test_canonical_bot_chat_hidden_from_sessions_still_notifies_an_interactive_reply(tmp_path, monkeypatch):
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    with db:
        db.set_session_hidden('saved', True)
        _settle(db, epoch, row)
        event, = iter_events(db.db_path, 0)
        assert event['target']['session_id'] == 'saved'


@pytest.mark.asyncio
async def test_wire_payload_cannot_supply_trusted_notification_origin(tmp_path, monkeypatch):
    from gateway.session_contract import SessionActor, SessionRef, Submission
    from hermes_state_runtime import RuntimeStoreError
    from tests.gateway.test_session_authority_cancel_settlement import _authority
    db, authority = _authority(tmp_path, monkeypatch)
    actor = SessionActor('human', 'owned', frozenset({'session:submit', 'session:operator'}), 'ws')
    ref = SessionRef('owned', 's')
    monkeypatch.setattr(authority, '_schedule', lambda _ref: None)
    with db:
        with pytest.raises(RuntimeStoreError, match='invalid_params'):
            await authority.submit(actor, Submission('spoofed', ref,
                {'text': 'hello', 'web_push_origin_v1': {'kind': 'interactive'}}, 'queue'))
        receipt = await authority.submit(actor, Submission('trusted', ref, {'text': 'hello'}, 'queue'),
                                         _web_push_origin=True)
        accepted = get_session_admission(db, admission_id=receipt.admission_id)
        assert accepted['payload']['web_push_origin_v1'] == {'kind': 'interactive'}


@pytest.mark.parametrize('payload', [{}, {'finite': True}, {'unattended': True}])
def test_native_discord_automation_and_finite_inputs_do_not_inherit_session_eligibility(tmp_path, monkeypatch, payload):
    db, epoch, row = _claimed(tmp_path, monkeypatch, trusted=bool(payload), **payload)
    with db:
        _settle(db, epoch, row)
        assert iter_events(db.db_path, 0) == []


def test_failed_event_write_rolls_back_terminal_admission(tmp_path, monkeypatch):
    from gateway import session_web_push
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    original = session_web_push.record_event
    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('write rolled back')
    monkeypatch.setattr(session_web_push, 'record_event', fail_after_insert)
    with db:
        with pytest.raises(RuntimeError, match='rolled back'):
            _settle(db, epoch, row)
        assert get_session_admission(db, admission_id=row['admission_id'])['status'] == 'started'
        assert iter_events(db.db_path, 0) == []


def test_same_owner_captured_unknown_resolution_records_reply_once(tmp_path, monkeypatch):
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    with db:
        db._execute_write(lambda conn: conn.execute("UPDATE session_admissions SET status='unknown' WHERE admission_id=?",
                                                   (row['admission_id'],)))
        for _ in range(2):
            resolve_unknown_session_input(db, epoch=epoch, admission_id=row['admission_id'], generation=row['generation'],
                _captured=('completed', {'result': {'final_response': 'saved after storage recovered'}}))
        event, = iter_events(db.db_path, 0)
        assert event['body'] == 'saved after storage recovered'


def test_pending_receipt_expires_and_is_invalidated_by_resolution_or_new_owner(tmp_path, monkeypatch):
    from gateway.session_web_push import _write_pending
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    authority = SimpleNamespace(db=db, epoch=epoch)
    prompt = {'kind': 'approval', 'prompt_id': 'actual-prompt', 'execution_generation': row['generation'],
              '_web_push_expires_at': time.time() + 60}
    with db:
        _write_pending(authority, 'saved', prompt, True)
        event, = iter_events(db.db_path, 0)
        assert event['expires_at'] - event['created_at'] <= 60
        assert pending_event_is_current(event, db.db_path)
        _write_pending(authority, 'saved', prompt, False)
        assert not pending_event_is_current(event, db.db_path)
        prompt = {**prompt, 'prompt_id': 'next-prompt'}
        _write_pending(authority, 'saved', prompt, True)
        event = iter_events(db.db_path, 0)[-1]
        assert pending_event_is_current(event, db.db_path)
        begin_runtime_epoch(db, instance_id='replacement')
        assert not pending_event_is_current(event, db.db_path)


def test_pending_event_is_not_current_after_deadline(tmp_path, monkeypatch):
    from gateway.session_web_push import _write_pending
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    with db:
        _write_pending(SimpleNamespace(db=db, epoch=epoch), 'saved', {
            'kind': 'clarify', 'prompt_id': 'question', 'execution_generation': row['generation'],
            '_web_push_expires_at': time.time() + 30}, True)
        event, = iter_events(db.db_path, 0)
        assert not pending_event_is_current({**event, 'expires_at': time.time() - 1}, db.db_path)


@pytest.mark.parametrize('different_owner_profile', [False, True])
def test_group_pending_preserves_owner_and_member_identity_and_rechecks_stop(tmp_path, monkeypatch, different_owner_profile):
    from gateway import hosted_room_driver as driver, hosted_rooms
    from gateway.session_web_push import _write_pending
    identity = driver.TaskIdentity('room', 'task', 'thread', 'turn')
    request_id = 'hosted:' + json.dumps([asdict(identity), 1], sort_keys=True, separators=(',', ':'))
    db, epoch, row = _claimed(tmp_path, monkeypatch, trusted=False, request_id=request_id)
    source = db
    if different_owner_profile:
        source_home = tmp_path / 'profiles' / 'room-owner'
        source_home.mkdir()
        source = SessionDB(db_path=source_home / 'state.db')
        binding = {'source_home': str(source_home), 'owner': 'human', 'selector': {
            'room_id': 'room', 'member_id': 'member', 'profile': 'target-bot'}}
        db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
            ('gateway.hosted.transport.v1:saved', json.dumps(binding))))
    try:
        source._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
            ('gateway.hosted.owner.v1:room', 'human')))
        hosted_rooms.create_room(source.db_path, room_id='room', name='Room', authority_gateway_id='gateway',
            members=[{'member_id': 'member', 'profile': 'target-bot', 'handle': 'bot'}])
        driver.admit_task(source.db_path, identity, payload={'target_profile': 'target-bot',
            'target_member_id': 'member', 'source_event_seq': 1, 'prompt': 'hello'}, clock=time.time)
        lease = driver.acquire_lease(source.db_path, room_id='room', gateway_id='gateway', authority_epoch=1,
                                     process_generation='test', ttl_seconds=120, clock=time.time)
        driver.start_task(source.db_path, identity, lease, expected_cancel_generation=0, clock=time.time)
        _write_pending(SimpleNamespace(db=db, epoch=epoch), 'saved', {'kind': 'approval', 'prompt_id': 'approval',
            'execution_generation': row['generation'], '_web_push_expires_at': time.time() + 60}, True)
        event, = iter_events(db.db_path, 0)
        assert event['profile'] == ('room-owner' if different_owner_profile else 'target-bot')
        assert event['target']['session_profile'] == 'target-bot'
        assert event['target']['task_id'] == 'task'
        assert event['target']['prompt_id'] == 'approval'
        assert pending_event_is_current(event, db.db_path)
        driver.begin_task_cancel(source.db_path, identity, cancel_id='stop', expected_cancel_generation=0, clock=time.time)
        assert not pending_event_is_current(event, db.db_path)
    finally:
        if source is not db:
            source.close()
        db.close()


@pytest.mark.parametrize('ending', ['answer', 'interrupt'])
def test_real_webapp_batch_clarify_uses_actual_request_id_and_retires_it(tmp_path, monkeypatch, ending):
    from gateway import session_web_push
    from gateway.session_clarify_batch import ask_clarify_batch
    from gateway.session_events import SessionEvents
    from gateway.session_server_requests import resolve
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    events = SessionEvents()
    events.execution = {'admission_id': row['admission_id'], 'execution_generation': row['generation']}
    frames = queue.Queue()
    class Peer:
        def write(self, frame):
            if frame.get('method') == 'clarify':
                frames.put(frame)
            return True
        def close(self):
            pass
    events.fanout.attach(Peer())
    authority = SimpleNamespace(db=db, epoch=epoch, sessions={'saved': SimpleNamespace(event_stream=events)})
    monkeypatch.setattr('gateway.session_cron.owner_for_home', lambda _home: (authority, None))
    monkeypatch.setattr(session_web_push, 'queue_pending', session_web_push._write_pending)
    agent = SimpleNamespace(_interrupt_requested=False)
    turn = SimpleNamespace(_approval_owner=(authority, 'saved', row['generation']),
                           _ctx=SimpleNamespace(agent_holder=[agent]))
    answers = {}
    questions = [{'qid': 'q1', 'question': 'Choose', 'choices': ['one', 'two'], 'multi_select': False}]
    worker = threading.Thread(target=lambda: answers.update(ask_clarify_batch(turn, questions)))
    try:
        worker.start()
        frame = frames.get(timeout=5)
        event, = iter_events(db.db_path, 0)
        assert event['kind'] == 'input'
        assert event['target']['prompt_id'] == frame['id']
        assert frame['id'].startswith('srq-')
        assert pending_event_is_current(event, db.db_path)
        if ending == 'answer':
            assert resolve({'id': frame['id'], 'result': {'answers': {'q1': 'one'}, 'outcome': 'submitted'}})
        else:
            agent._interrupt_requested = True
        worker.join(5)
        assert not worker.is_alive()
        assert answers['outcome'] == ('submitted' if ending == 'answer' else 'cancelled')
        assert not pending_event_is_current(event, db.db_path)
    finally:
        agent._interrupt_requested = True
        worker.join(5)
        db.close()


@pytest.mark.parametrize('remote', [False, True])
def test_late_native_approval_registration_is_inactive_but_remote_waiter_is_retained(remote):
    from gateway.session_events import SessionEvents
    from gateway.session_pending_controls import PendingControls
    controls = PendingControls(SessionEvents())
    changes = []
    controls.on_change = lambda prompt, active: changes.append(active)
    if remote:
        controls.remote_responders['already-resolved'] = lambda *_args: None
    controls.register('saved', 'web-push-test-no-native-waiter', 1,
                      {'request_id': 'already-resolved', 'command': 'safe command'})
    assert ('already-resolved' in controls.pending) is remote
    assert changes == ([True] if remote else [True, False])


def test_pending_housekeeping_is_bounded_and_keeps_unexpired_requests(tmp_path, monkeypatch):
    from gateway.session_web_push import _write_pending
    db, epoch, row = _claimed(tmp_path, monkeypatch)
    authority = SimpleNamespace(db=db, epoch=epoch)
    prompt = {'kind': 'approval', 'prompt_id': 'current', 'execution_generation': row['generation'],
              '_web_push_expires_at': time.time() + 60}
    with db:
        _write_pending(authority, 'saved', prompt, True)
        old = time.time() - 8 * 86400
        db._execute_write(lambda conn: conn.executemany('INSERT INTO web_push_pending VALUES(?,?,?,?,?,?,?,?,?,?)',
            [(f'old-{index}', 'target-bot', 'saved', row['admission_id'], epoch, row['generation'],
              f'old-{index}', 'approval', index % 2, old) for index in range(105)]))
        _write_pending(authority, 'saved', prompt, False)
        with db._read_ctx() as conn:
            assert conn.execute("SELECT COUNT(*) FROM web_push_pending WHERE event_key LIKE 'old-%'").fetchone()[0] == 5
            current = conn.execute("SELECT active FROM web_push_pending WHERE prompt_id='current'").fetchone()
            assert current is not None and current[0] == 0
