"""Durable learning coordination for the transparent auto wrapper.

The host still owns feedback and business execution. A learning completion means
that a candidate Recipe is available, never acceptance or production publication.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
import queue
import threading
import time
from uuid import uuid4

from .atoms import CoSensClient
from .auto_policy import normalize
from .errors import RSIRecoveryRequired
from .model_api import improvement_data
from .state import TurnStore


@dataclass(frozen=True)
class AutoLearning:
    """Optional pure callbacks run on the learning worker, not the model caller.

    feedback(turn) returns actual corrective text or None. select_cause(portfolio)
    returns an observed candidate ID or None to defer selection. Async callbacks
    are supported on the worker's event loop; do not share loop-bound clients.
    model_api is explicit and is never copied from the native OpenAI credentials.
    """
    feedback: object = None
    select_cause: object = None
    model_api: dict | None = field(default=None, repr=False)
    timeout: float = 1800
    poll_interval: float = 2
    max_job_retries: int = 1

    def __post_init__(self):
        for value in (self.timeout, self.poll_interval):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Learning timeout and poll interval must be positive finite numbers')
        if type(self.max_job_retries) is not int or self.max_job_retries < 0:
            raise ValueError('max_job_retries must be a nonnegative integer')
        for callback in (self.feedback, self.select_cause):
            if callback is not None and not callable(callback):
                raise TypeError('Learning callbacks must be callable')
        improvement_data(model_api=self.model_api)
        object.__setattr__(self, 'model_api', deepcopy(self.model_api))


def _callback(callback, value):
    result = callback(deepcopy(value))
    if inspect.isawaitable(result):
        async def wait():
            return await result
        return asyncio.run(wait())
    return result


class _Stopped(Exception):
    pass


class LearningCoordinator:
    TERMINAL = {'completed', 'awaiting_feedback', 'awaiting_selection', 'paused', 'failed', 'unavailable', 'noop'}

    def __init__(self, *, api_key, region, state_path, transport, config, planner=None):
        self.config = config
        self.planner = planner
        self.client = CoSensClient(api_key=api_key, region=region, state_path=state_path, _http_transport=transport)
        self.atoms = self.client.sync_atoms
        self.store = TurnStore(state_path, principal=api_key, service_id=self.client._service.identity)
        self.owner = uuid4().hex
        self.stop = threading.Event()
        self.queue = queue.Queue()
        self.lock = threading.RLock()
        self.thread = None
        self.closed = False

    @staticmethod
    def _key(turn_id):
        return 'auto-learning:' + turn_id

    def get(self, turn_id):
        return self.store.get(self._key(turn_id))

    def attach(self, turn):
        value = {'turn_id': turn['turn_id'], 'run_id': turn.get('run_id'), 'task_id': turn.get('task_id'),
                 'revision_id': turn.get('revision_id'), 'v1_candidate_id': turn.get('recipe_candidate_id'),
                 'used_recipe_id': turn.get('used_recipe_id'), 'used_recipe_version': turn.get('used_recipe_version'),
                 'used_recipe_candidate_id': turn.get('used_recipe_candidate_id'),
                 'feedback_turn_id': (turn.get('decision') or {}).get('feedback_turn_id'),
                 'lookup_complete': turn.get('frozen_context', {}).get('context', {}).get('lookup_complete', True),
                 'action': 'noop', 'reason': 'decision_pending', 'phase': 'plan', 'state': 'queued',
                 'feedback': None, 'feedback_evaluated': False,
                 'source': {'objective': turn.get('objective'), 'result': deepcopy(turn.get('result')),
                            'messages': deepcopy(turn.get('agent_context', [])),
                            'events': deepcopy(turn.get('execution_events', []))},
                 'job_retries': {}, 'published': False, 'accepted': False}
        if self.store.insert_if_absent(self._key(turn['turn_id']), value):
            self.schedule(turn['turn_id'])
        return self.get(turn['turn_id'])

    def schedule(self, turn_id):
        with self.lock:
            if self.closed:
                raise RuntimeError('Learning coordinator is closed')
            self.queue.put(turn_id)
            if self.thread is None:
                self.thread = threading.Thread(target=self._worker, name='cosens-auto-learning', daemon=True)
                self.thread.start()

    def _update(self, turn_id, **updates):
        def update(value):
            if value is None:
                raise RSIRecoveryRequired('Unknown learning turn')
            if value.get('owner') not in (None, self.owner):
                raise _Stopped()
            value.update(deepcopy(updates))
            value['updated_at'] = time.time()
            if value.get('state') == 'running':
                value['lease_until'] = time.time() + self.config.timeout + 90
            return value
        return self.store.mutate(self._key(turn_id), update)

    def feedback(self, turn_id, message):
        improvement_data(feedback=message)
        if not isinstance(message, str) or not message.strip():
            raise ValueError('feedback must be nonempty text')
        def freeze(value):
            if value is None:
                raise RSIRecoveryRequired('Turn has no submitted Task/Revision for learning')
            if value.get('feedback') not in (None, message):
                raise RSIRecoveryRequired('Feedback for this turn is already frozen; target the next executed turn')
            value['feedback'] = message
            value['feedback_source'] = 'provided'
            if value['state'] in {'noop', 'completed', 'awaiting_feedback'} and not value.get('attribution_id'):
                value.update(state='queued', phase='plan', owner=None)
                # A newly created candidate is the parent for subsequent feedback.
                if value.get('recipe_candidate_id'):
                    value['v1_candidate_id'] = value['recipe_candidate_id']
                    value['used_recipe_candidate_id'] = value['recipe_candidate_id']
                    value['used_recipe_id'] = value.get('recipe', {}).get('recipe_id') or value['recipe_candidate_id']
                    value['used_recipe_version'] = value.get('recipe', {}).get('version')

            return value
        value = self.store.mutate(self._key(turn_id), freeze)
        if value['state'] != 'completed':
            self.schedule(turn_id)
        return value

    def resume(self, turn_id):
        def resume(value):
            if value is None:
                raise RSIRecoveryRequired('Unknown learning turn')
            if value['state'] in {'completed', 'noop'}:
                return value
            if value['state'] == 'running' and value.get('lease_until', 0) > time.time():
                return value
            value.update(state='queued', owner=None, job_retries={})
            value.pop('error_type', None)
            return value
        value = self.store.mutate(self._key(turn_id), resume)
        if value['state'] == 'queued':
            self.schedule(turn_id)
        return value

    def wait(self, turn_id, timeout):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('timeout must be finite and nonnegative')
        deadline = time.monotonic() + timeout
        while True:
            value = self.get(turn_id)
            if value is None:
                raise RSIRecoveryRequired('Turn has no submitted Task/Revision for learning')
            if value['state'] in self.TERMINAL:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Learning is still running; keep the same turn and job IDs')
            time.sleep(min(0.02, remaining))

    def _claim(self, turn_id):
        claimed = False
        def claim(value):
            nonlocal claimed
            if value is None or value['state'] in self.TERMINAL:
                return value
            if value['state'] == 'running' and value.get('lease_until', 0) > time.time():
                return value
            value.update(state='running', owner=self.owner, lease_until=time.time() + self.config.timeout + 90)
            claimed = True
            return value
        self.store.mutate(self._key(turn_id), claim)
        return claimed

    def _worker(self):
        try:
            while not self.stop.is_set():
                try:
                    turn_id = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if not self._claim(turn_id):
                    continue
                try:
                    self._run(turn_id)
                except _Stopped:
                    value = self.get(turn_id)
                    if value and value.get('owner') == self.owner:
                        self._update(turn_id, state='paused', owner=None)
                except TimeoutError:
                    self._update(turn_id, state='paused', error_type='TimeoutError', owner=None)
                except Exception as error:
                    self._update(turn_id, state='paused' if self.stop.is_set() else 'failed',
                                 error_type=type(error).__name__, owner=None)
        finally:
            self.client.close()
            self.store.close()

    def _check(self, deadline):
        if self.stop.is_set():
            raise _Stopped()
        if time.monotonic() >= deadline:
            raise TimeoutError('Learning worker deadline exceeded')

    def _poll(self, turn_id, kind, job_id, deadline):
        getter = self.atoms.get_recipe_candidate if kind == 'recipe' else self.atoms.get_attribution
        while True:
            self._check(deadline)
            value = getter(job_id)
            self._update(turn_id, remote_status=value.get('status'))
            status = value.get('status')
            if status == 'failed':
                record = self.get(turn_id)
                retries = record['job_retries'].get(job_id, 0)
                if retries >= self.config.max_job_retries:
                    raise RSIRecoveryRequired('Remote learning job failed; resume the same turn')
                retries_by_id = {**record['job_retries'], job_id: retries + 1}
                # Persist before a resume: after an uncertain reply, GET the same
                # job on recovery rather than creating a replacement job.
                self._update(turn_id, job_retries=retries_by_id)
                self._check(deadline)
                if kind == 'recipe':
                    self.atoms.resume_recipe_candidate(job_id)
                else:
                    self.atoms.resume_attribution(job_id)
            elif status in {'candidate', 'awaiting_selection', 'selected', 'inconclusive'}:
                return value
            elif status not in {'queued', 'running', 'awaiting_parent'}:
                self._update(turn_id, state='paused', error_type='RemoteJobNeedsInput', owner=None)
                raise _Stopped()
            self.stop.wait(min(self.config.poll_interval, max(0, deadline - time.monotonic())))

    def _run(self, turn_id):
        deadline = time.monotonic() + self.config.timeout
        value = self.get(turn_id)
        if value['phase'] == 'plan':
            search_incomplete = (not value.get('lookup_complete', True) and
                                 not (value.get('used_recipe_id') or value.get('v1_candidate_id')))
            if value.get('feedback_turn_id') or search_incomplete:
                self._update(turn_id, action='noop', reason='feedback_routed' if value.get('feedback_turn_id') else 'recipe_search_incomplete', state='noop', owner=None)
                return
            if not value.get('feedback') and not value['feedback_evaluated'] and self.config.feedback:
                self._update(turn_id, feedback_evaluated=True)
                message = _callback(self.config.feedback, value['source'])
                self._check(deadline)
                if message is not None and not self.get(turn_id).get('feedback'):
                    self.feedback(turn_id, message)
                    self._update(turn_id, feedback_source='evaluator')
            value = self.get(turn_id)
            has_recipe = bool(value.get('used_recipe_id') or value.get('v1_candidate_id'))
            recorded = bool(value.get('task_id') and value.get('revision_id'))
            if has_recipe and not value.get('feedback'):
                plan = {'action': 'noop', 'reason': 'reuse_without_feedback', 'context_sufficient': True}
            elif not recorded:
                plan = {'action': 'noop', 'reason': 'execution_not_recorded', 'context_sufficient': False}
            elif self.planner is None:
                plan = {'action': 'noop', 'reason': 'policy_unavailable', 'context_sufficient': False}
            else:
                plan = _callback(self.planner, value)
            self._check(deadline)
            # Re-evaluate if feedback arrived during the policy call.
            latest = self.get(turn_id)
            if latest.get('feedback') != value.get('feedback'):
                return self._run(turn_id)
            plan = normalize(plan, has_recipe=has_recipe, feedback=value.get('feedback'), recorded=recorded)
            if plan['action'] == 'noop':
                def finish(current):
                    if current.get('feedback') != value.get('feedback'):
                        return current
                    current.update(plan, state='noop', owner=None)
                    return current
                saved = self.store.mutate(self._key(turn_id), finish)
                if saved['state'] != 'noop':
                    return self._run(turn_id)
                return
            if plan['action'] == 'create':
                digest = hashlib.sha256(json.dumps(
                    [value['source'].get('objective'), value['source'].get('messages')],
                    sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
                key = 'auto-create-claim:' + digest
                self.store.insert_if_absent(key, {'turn_id': turn_id})
                existing = self.store.get(key)['turn_id']
                if existing != turn_id:
                    self._update(turn_id, action='noop', reason='recipe_creation_already_requested',
                                 existing_turn_id=existing, state='noop', owner=None)
                    return
            self._update(turn_id, **plan, phase='create' if plan['action'] == 'create' else 'feedback')
        value = self.get(turn_id)
        if value['phase'] == 'create':
            if not value.get('recipe_candidate_id'):
                self._check(deadline)
                options = {'model_api': self.config.model_api} if self.config.model_api is not None else {}
                job = self.atoms.start_recipe_candidate(value['task_id'], revision_id=value['revision_id'], key='auto-create:' + turn_id, **options)
                self._update(turn_id, recipe_candidate_id=job['recipe_candidate_id'])
            self._update(turn_id, phase='recipe')
        value = self.get(turn_id)
        if value['phase'] == 'feedback':
            self._check_parent(value)
            digest = hashlib.sha256((turn_id + '\0' + value['feedback']).encode()).hexdigest()
            self._check(deadline)
            job = self.atoms.improve_task(value['task_id'], revision_id=value['revision_id'],
                feedback=value['feedback'], key='auto-learning:' + digest, model_api=self.config.model_api)
            aid = job.get('attribution_id') or job.get('job_id')
            if not aid:
                raise RSIRecoveryRequired('Improvement receipt has no job ID')
            self._update(turn_id, attribution_id=aid, phase='attribution')
        value = self.get(turn_id)
        if value['phase'] == 'attribution':
            job = self._poll(turn_id, 'attribution', value['attribution_id'], deadline)
            portfolio = job.get('portfolio') or {}
            selection = job.get('selection') or {}
            recovered_id = job.get('recipe_candidate_id') or selection.get('recipe_candidate_id')
            if recovered_id:
                self._update(turn_id, recipe_candidate_id=recovered_id, phase='recipe')
            else:
                if value.get('selection_inflight'):
                    raise RSIRecoveryRequired('Selection outcome is unknown; inspect the existing attribution before retrying')
                candidates = portfolio.get('diagnostic_candidates') or []
                if self.config.select_cause:
                    chosen = _callback(self.config.select_cause, portfolio)
                else:
                    chosen = next((c.get('candidate_id') or c.get('id') for c in candidates if c.get('evidence_refs')), None)
                self._check(deadline)
                if chosen is None:
                    self._update(turn_id, state='awaiting_selection', portfolio=portfolio, owner=None)
                    return
                if chosen not in {c.get('candidate_id') or c.get('id') for c in candidates}:
                    raise ValueError('Cause selector must return an observed candidate ID')
                self._check_parent(value)
                self._update(turn_id, selection_inflight=True, selected_cause_id=chosen, candidate_sha256=portfolio['candidate_sha256'])
                selected = self.atoms.select_cause(value['attribution_id'], candidate_id=chosen, candidate_sha256=portfolio['candidate_sha256'])
                self._update(turn_id, recipe_candidate_id=selected['recipe_candidate_id'], phase='recipe', selection_inflight=False,
                             selection_source='auto_policy', human_confirmed=False)
        value = self.get(turn_id)
        if value['phase'] == 'recipe':
            recipe = self._poll(turn_id, 'recipe', value['recipe_candidate_id'], deadline)
            def finish(current):
                if current.get('owner') != self.owner:
                    raise _Stopped()
                current.update(recipe=recipe, state='completed', phase='completed', owner=None)
                if current.get('feedback') and current['action'] == 'create' and not current.get('attribution_id'):
                    current.update(used_recipe_id=recipe['recipe_id'], used_recipe_version=recipe['version'],
                                   used_recipe_candidate_id=current['recipe_candidate_id'],
                                   state='queued', phase='plan')
                return current
            saved = self.store.mutate(self._key(turn_id), finish)
            if saved['state'] == 'queued':
                self.schedule(turn_id)

    def _check_parent(self, value):
        expected = value.get('used_recipe_candidate_id')
        if expected:
            current = self.atoms.get_task_recipe(value['task_id']).get('current') or {}
            if current.get('recipe_candidate_id') != expected:
                raise RSIRecoveryRequired('Recipe version changed since the recorded execution; inspect the linked version')

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.stop.set()
            if self.thread is None:
                self.client.close()
                self.store.close()
