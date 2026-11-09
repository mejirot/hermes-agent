"""Cron notification facts are settled by the attempt owner, independently of delivery targets."""
import json

from hermes_cli.web_push_events import preview, profile_for_home, record_event
from hermes_constants import get_hermes_home


def record_run_receipt(job, result):
    """Keep row coordinates from the agent's committed persistence markers, including compression."""
    if not isinstance(result, dict) or not isinstance(result.get('session_id'), str) or not result['session_id']:
        return
    receipt = {'session_id': result['session_id']}
    messages = result.get('messages')
    if isinstance(messages, list) and messages and isinstance(messages[-1], dict):
        last = messages[-1]
        from agent.persisted_turn_receipt import committed_row_id
        row_id = committed_row_id(last)
        if (row_id is not None and last.get('role') == 'assistant' and not last.get('tool_calls')
                and last.get('content') == result.get('final_response')):
            receipt['message_id'] = str(row_id)
    job['_web_push_receipt'] = receipt


def record_finished_execution(conn, record, notification=None):
    if not record or record['status'] not in {'completed', 'failed'}:
        return
    saved = json.loads(record.get('notification_json') or '{}') if notification is None else notification
    notice = {key: preview(value, 80 if key != 'body' else 160)
              for key, value in saved.items() if key in {'job_name', 'body', 'silence_reason'}}
    # Persist an explicit reason: delivery_outcome='suppressed' also means deliver=local and
    # cannot distinguish a useful report from [SILENT], an acked incident or a scheduled retry.
    if notification is not None:
        encoded = json.dumps(notice, ensure_ascii=False)
        conn.execute('UPDATE executions SET notification_json=?, silence_reason=? WHERE id=?',
                     (encoded, notice.get('silence_reason'), record['id']))
        record.update(notification_json=encoded, silence_reason=notice.get('silence_reason'))
    if notice.get('silence_reason'):
        return
    success = record['status'] == 'completed'
    body = notice.get('body') if success else 'A scheduled job failed. Open Hermes for details.'
    if not body:
        return
    profile = profile_for_home(get_hermes_home())
    target = {'profile': profile, 'job_id': record['job_id'], 'execution_id': record['id']}
    if record.get('session_id'):
        target['session_id'] = record['session_id']
        if record.get('message_id'):
            target['message_id'] = record['message_id']
    record_event(conn, event_key='cron:' + record['id'], kind='cron', profile=profile, target=target,
                 title=f"{profile}: {notice.get('job_name') or ('Cron completed' if success else 'Cron failed')}", body=body)
