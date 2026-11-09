"""Cron notifications use terminal attempts, explicit silence and the actual canonical receipt."""
import pytest

from hermes_cli.web_push_events import iter_events


@pytest.mark.parametrize(('answer', 'silent'), [
    ('[SILENT]', True), ('SILENT', True), ('A useful local report', False),
    ('The documentation quotes [SILENT] as a control token.', False),
])
def test_local_delivery_is_not_confused_with_silent_and_terminal_retry_is_idempotent(tmp_path, monkeypatch, answer, silent):
    from cron import executions, jobs, scheduler
    from cron.scheduler_bookkeeping import finish_completed_run
    home = tmp_path / 'profiles' / 'scheduled-bot'
    home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', home / 'cron' / 'executions.db')
    monkeypatch.setattr(scheduler, 'save_job_output', lambda *_: None)
    monkeypatch.setattr(scheduler, '_resolve_incidents_for_recovered_job', lambda *_: None)
    job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
    attempt = executions.create_execution(job['id'], source='builtin')
    executions.mark_execution_running(attempt['id'])
    from hermes_state import SessionDB
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from cron.web_push import record_run_receipt
    with SessionDB(db_path=home / 'state.db') as db:
        db.create_session('actual-owner-receipt', source='cron')
        message_id = db.append_message('actual-owner-receipt', 'assistant', answer)
    record_run_receipt(job, {'session_id': 'actual-owner-receipt', 'final_response': answer,
        'messages': [{'role': 'assistant', 'content': answer, '_row_id': message_id, _DB_PERSISTED_MARKER: True}]})
    receipt = job['_web_push_receipt']
    executions.set_execution_session(attempt['id'], receipt['session_id'], message_id=receipt['message_id'])
    job['execution_id'] = attempt['id']
    delivery = scheduler._RunDelivery(job, True, None)
    scheduler._save_compose_deliver(delivery, scheduler._FireOwnership(job), answer, 'document',
                                   adapters=None, loop=None, verbose=False, execution_token=None)
    assert finish_completed_run(delivery, None, attempt['id'])
    row = executions.get_execution(attempt['id'])
    assert row['delivery_outcome'] == 'suppressed'
    events = iter_events(home / 'cron' / 'executions.db', 0)
    if silent:
        assert row['silence_reason'] == 'silent_marker'
        assert events == []
    else:
        event, = events
        assert event['body'] == answer
        assert event['profile'] == 'scheduled-bot'
        assert event['target']['session_id'] == 'actual-owner-receipt'
        assert event['target']['message_id'] == str(message_id)
        assert event['target']['execution_id'] == attempt['id']
    assert executions.finish_execution(attempt['id'], success=True, notification=delivery.notification) is None
    assert iter_events(home / 'cron' / 'executions.db', 0) == events


def test_failure_notice_never_contains_provider_error_or_credentials(tmp_path, monkeypatch):
    from cron import executions
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'cron' / 'executions.db')
    attempt = executions.create_execution('job', source='builtin')
    executions.mark_execution_running(attempt['id'])
    executions.finish_execution(attempt['id'], success=False, error='password=private-provider-key')
    event, = iter_events(tmp_path / 'cron' / 'executions.db', 0)
    assert event['body'] == 'A scheduled job failed. Open Hermes for details.'
    assert 'private-provider-key' not in str(event)
    assert 'session_id' not in event['target']


def test_event_storage_failure_rolls_back_execution_settlement(tmp_path, monkeypatch):
    from cron import executions, web_push
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'cron' / 'executions.db')
    attempt = executions.create_execution('job', source='builtin')
    executions.mark_execution_running(attempt['id'])
    original = web_push.record_event
    def rolled_back(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('rollback')
    monkeypatch.setattr(web_push, 'record_event', rolled_back)
    with pytest.raises(RuntimeError, match='rollback'):
        executions.finish_execution(attempt['id'], success=True, notification={'body': 'done'})
    assert executions.get_execution(attempt['id'])['status'] == 'running'
    assert iter_events(tmp_path / 'cron' / 'executions.db', 0) == []
