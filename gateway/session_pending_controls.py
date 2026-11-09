"""Generation-bound projection/control of the existing blocking approval queue.

Ordinary approvals and clarify prompts share the existing native waiters.
Secret/sudo prompts remain native; no response is published or journaled here.
"""
from copy import deepcopy
import logging
import time

from hermes_state_runtime import RuntimeStoreError
from tools.approval import list_gateway_approvals, resolve_gateway_approval


class PendingControls:
    def __init__(self, events):
        self.events = events
        self.pending = {}
        self.remote_responders = {}
        self.on_change = None
        self.deadlines = {}

    def _changed(self, prompt, active):
        if self.on_change is not None:
            try:
                self.on_change({**prompt, '_web_push_expires_at': self.deadlines.get(prompt['prompt_id'])}, active)
            except Exception:
                logging.getLogger(__name__).warning('Pending notification projection failed', exc_info=True)

    def forget(self, prompt_id):
        saved = self.pending.pop(prompt_id, None)
        if saved is not None:
            self._changed(saved[1], False)
        self.deadlines.pop(prompt_id, None)
        return saved

    def _settled_elsewhere(self, session_id, prompt_id, generation):
        with self.events.lock:
            saved = self.pending.get(prompt_id)
            if saved is None or saved[1]['execution_generation'] != generation:
                return
            self.forget(prompt_id)
            self.remote_responders.pop(prompt_id, None)
            self.events.publish(session_id, {'prompt_id': prompt_id, 'execution_generation': generation},
                                event_type=saved[1]['kind'] + '.settled')

    def register(self, session_id, route, generation, data, *, timeout_s=None):
        from gateway.run import _redact_approval_command
        with self.events.lock:
            prompt_id = data['request_id']
            choices = ['once', 'deny']
            if data.get('allow_session', True) and not data.get('smart_denied', False):
                choices.append('session')
            if data.get('allow_permanent', True) and not data.get('smart_denied', False):
                choices.append('always')
            prompt = {'kind': 'approval', 'prompt_id': prompt_id,
                      'execution_generation': generation,
                      'command': _redact_approval_command(data.get('command', '')),
                      'description': _redact_approval_command(data.get('description', '')),
                      'choices': choices}
            if 'edit' in data:
                prompt['edit'] = deepcopy(data['edit'])
                prompt['choices'] = ['once', 'deny']
            if timeout_s is not None:
                prompt['timeout_s'] = timeout_s
            self.pending[prompt_id] = (route, prompt)
            self.deadlines[prompt_id] = time.time() + min(120, max(0, timeout_s if timeout_s is not None else 120))
            self.events.publish(session_id, prompt, event_type='approval.request')
            self._changed(prompt, True)
            from tools.approval import observe_gateway_settle
            observed = observe_gateway_settle(route, prompt_id,
                                              lambda _reason: self._settled_elsewhere(session_id, prompt_id, generation))
            if not observed and prompt_id not in self.remote_responders:
                # The native waiter may have resolved between rendering and observer registration.
                # Managed-worker prompts live in the worker, so their explicit responder is the proof.
                self.snapshot(session_id, generation)

    def register_clarify(self, session_id, generation, entry, *, timeout_s=None):
        from gateway.run import _redact_approval_command
        with self.events.lock:
            prompt = {"kind": "clarify", "prompt_id": entry.clarify_id,
                      "execution_generation": generation,
                      "question": _redact_approval_command(entry.question),
                      "choices": [_redact_approval_command(c) for c in entry.choices or []],
                      "multi_select": entry.multi_select}
            if timeout_s is not None:
                prompt["timeout_s"] = timeout_s
            self.pending[entry.clarify_id] = (entry, prompt)
            self.deadlines[entry.clarify_id] = time.time() + min(120, max(0, timeout_s if timeout_s is not None else 120))
            self.events.publish(session_id, prompt, event_type="clarify.request")
            self._changed(prompt, True)

    def snapshot(self, session_id, generation):
        with self.events.lock:
            for prompt_id, (route, prompt) in list(self.pending.items()):
                active = (prompt_id in self.remote_responders or
                          (not route.event.is_set() if prompt['kind'] == 'clarify' else
                           any(p['request_id'] == prompt_id for p in list_gateway_approvals(route))))
                if prompt['execution_generation'] != generation or not active:
                    self.forget(prompt_id)
                    self.remote_responders.pop(prompt_id, None)
                    self.events.publish(session_id, {'prompt_id': prompt_id,
                        'execution_generation': prompt['execution_generation']}, event_type=prompt['kind'] + '.settled')
            return tuple(deepcopy(prompt) for _, prompt in self.pending.values())

    def respond(self, session_id, generation, prompt_id, response, *, kind="approval"):
        with self.events.lock:
            self.snapshot(session_id, generation)
            entry = self.pending.get(prompt_id)
            if entry is None:
                return {'status': 'already_resolved', 'prompt_id': prompt_id}
            route, prompt = entry
            if prompt['kind'] != kind:
                raise RuntimeStoreError('invalid_params')
            if prompt_id in self.remote_responders:
                field = 'answer' if kind == 'clarify' else 'choice'
                if (not isinstance(response, dict) or set(response) != {field}
                        or not isinstance(response[field], str) or len(response[field]) > 16384
                        or (kind == 'approval' and response[field] not in prompt['choices'])):
                    raise RuntimeStoreError('invalid_params')
                self.remote_responders[prompt_id](kind, prompt_id, response[field])
                del self.remote_responders[prompt_id]
                self.forget(prompt_id)
                self.events.publish(session_id, {'prompt_id': prompt_id, 'execution_generation': generation},
                                    event_type=kind + '.settled')
                return {'status': 'resolved', 'prompt_id': prompt_id}
            if kind == 'clarify':
                from tools.clarify_gateway import resolve_gateway_clarify
                if (not isinstance(response, dict) or set(response) != {'answer'}
                        or not isinstance(response['answer'], str)):
                    raise RuntimeStoreError('invalid_params')
                resolved = resolve_gateway_clarify(prompt_id, response['answer'])
                self.snapshot(session_id, generation)
                return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}
            if not isinstance(response, dict) or set(response) != {'choice'} or response['choice'] not in prompt['choices']:
                raise RuntimeStoreError('invalid_params')
            # The tools lock arbitrates native taps versus attached responders;
            # an exact request ID can never consume the next FIFO approval.
            resolved = resolve_gateway_approval(route, response['choice'], request_id=prompt_id)
            self.snapshot(session_id, generation)
            return {'status': 'resolved' if resolved else 'already_resolved', 'prompt_id': prompt_id}
