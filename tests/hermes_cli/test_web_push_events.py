"""Durable notification facts retain transaction ownership, bounded previews and stable cursors."""
import sqlite3
import pytest

from hermes_cli.web_push_events import iter_events, record_event, source_identity, SourceChangedError


def _record(conn, key='one', **kwargs):
    record_event(conn, event_key=key, kind='reply', profile='bot', target={'profile': 'bot', 'session_id': 'saved'},
                 title='Reply', body=kwargs.pop('body', 'visible answer'), **kwargs)


def test_read_missing_source_never_creates_database(tmp_path):
    path = tmp_path / 'missing.db'
    assert iter_events(path, 0) == []
    assert not path.exists()


def test_event_rolls_back_with_source_transaction(tmp_path):
    path = tmp_path / 'source.db'
    conn = sqlite3.connect(path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        _record(conn)
        assert conn.in_transaction
        assert conn.execute('SELECT event_key FROM web_push_events').fetchone() == ('one',)
        conn.rollback()
        assert iter_events(path, 0) == []
    finally:
        conn.close()


def test_exact_retry_keeps_first_fact_and_cursor_pages_do_not_repeat(tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_cli.web_push_events.time.time', lambda: 101)
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as conn:
        _record(conn, created_at=100, expires_at=200)
        _record(conn, body='retry must not replace original', created_at=100, expires_at=200)
        _record(conn, key='two', created_at=101, expires_at=201)
    first, = iter_events(path, 0, limit=1)
    second, = iter_events(path, first['seq'], limit=1)
    assert first['body'] == 'visible answer'
    assert first['target'] == {'profile': 'bot', 'session_id': 'saved'}
    assert second['event_key'] == 'two'
    assert iter_events(path, second['seq']) == []


def test_preview_redacts_before_truncating_even_when_user_disabled_redaction(tmp_path, monkeypatch):
    from agent import redact
    monkeypatch.setattr(redact, '_REDACT_ENABLED', False)
    path = tmp_path / 'source.db'
    secret = 'secret-value-123456789'
    with sqlite3.connect(path) as conn:
        _record(conn, body=f'password={secret}\n' + 'a' * 240)
    row, = iter_events(path, 0)
    assert secret not in row['body']
    assert '\n' not in row['body']
    assert len(row['body']) <= 160


def test_recreated_source_gets_a_distinct_identity(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as conn:
        _record(conn)
    first = source_identity(path)
    with sqlite3.connect(path) as conn:
        _record(conn, key='two')
    assert source_identity(path) == first
    path.unlink()
    with sqlite3.connect(path) as conn:
        _record(conn)
    assert source_identity(path) != first


def test_restored_sequence_below_cursor_reimports_idempotent_facts(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as conn:
        _record(conn)
    row, = iter_events(path, 100)
    assert row['seq'] == 1 and row['event_key'] == 'one'


def test_page_rejects_a_replaced_source_instead_of_using_old_cursor(tmp_path):
    path = tmp_path / 'source.db'
    with sqlite3.connect(path) as conn:
        _record(conn)
    previous = source_identity(path)
    path.unlink()
    with sqlite3.connect(path) as conn:
        _record(conn, key='new-source')
    with pytest.raises(SourceChangedError):
        iter_events(path, 0, expected_identity=previous)
    assert iter_events(path, 0, expected_identity=source_identity(path))[0]['event_key'] == 'new-source'
