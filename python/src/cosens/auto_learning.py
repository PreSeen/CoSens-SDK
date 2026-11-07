"""Explicit Recipe requests after external confirmation. No automatic learning."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import time
import math

from .atoms import CoSensClient
from .errors import RSICandidateFailed, RSIRecoveryRequired
from .state import TurnStore
from .workflow_atoms import _completion_tokens


@dataclass(frozen=True)
class AutoLearning:
    """Generation polling/model settings; never authorizes generation by itself."""
    timeout: float = 1800
    poll_interval: float = 2

    def __post_init__(self):
        for name in ('timeout', 'poll_interval'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be finite and positive')


class LearningCoordinator:
    def __init__(self, *, api_key, region, state_path, transport, config):
        self.client = CoSensClient(api_key=api_key, region=region, state_path=state_path, _http_transport=transport)
        self.atoms = self.client.sync_atoms
        self.store = TurnStore(state_path, principal=self.client._transport.api_key, service_id=self.client._service.identity)
        self.config = config

    @staticmethod
    def _key(turn_id):
        return 'explicit-recipe:' + turn_id

    def get(self, turn_id):
        return self.store.get(self._key(turn_id))

    def attach(self, turn):
        value = {'turn_id':turn['turn_id'], 'task_id':turn.get('task_id'), 'revision_id':turn.get('revision_id'),
                 'used_recipe_id':turn.get('used_recipe_id'), 'used_recipe_version':turn.get('used_recipe_version'),
                 'state':'awaiting_confirmation', 'action':'noop', 'reason':'external_confirmation_required'}
        self.store.insert_if_absent(self._key(turn['turn_id']), value)
        return self.get(turn['turn_id'])

    def _record(self, turn_id):
        value=self.get(turn_id)
        if not value or not value.get('task_id') or not value.get('revision_id'):
            raise RSIRecoveryRequired('Turn has no submitted Task/Revision')
        return value

    def feedback(self, turn_id, message):
        value=self._record(turn_id)
        key='feedback:'+hashlib.sha256((turn_id+'\0'+message).encode()).hexdigest()
        receipt=self.atoms.record_feedback(value['revision_id'], feedback=message, key=key)
        value['feedback']=receipt
        self.store.put(self._key(turn_id),value)
        return value

    def generate(self, turn_id, request_key=None, max_completion_tokens=None):
        """Calling this is the host's explicit confirmation signal."""
        _completion_tokens(max_completion_tokens)
        value=self._record(turn_id)
        key=request_key or value.get('request_key') or 'explicit-generation:'+turn_id
        budget = max_completion_tokens
        if key == value.get('request_key'):
            previous_budget = value.get('request_max_completion_tokens', value.get('max_completion_tokens'))
            if budget is None:
                budget = previous_budget
            elif previous_budget != budget:
                raise ValueError('request key reused with a different max_completion_tokens')
        if value.get('recipe_candidate_id') and key==value.get('request_key'):
            return value
        value.update(request_key=key,state='request_pending',reason='explicit_generation_requested',max_completion_tokens=budget,request_max_completion_tokens=budget)
        self.store.put(self._key(turn_id),value)
        options = {'max_completion_tokens': budget} if budget is not None else {}
        receipt=self.atoms.start_recipe_candidate(value['task_id'],revision_id=value['revision_id'],key=key,**options)
        value.update(recipe_candidate_id=receipt['recipe_candidate_id'],state=receipt['status'],
                     max_completion_tokens=receipt.get('max_completion_tokens',budget),
                     action='revise' if receipt.get('parent_candidate_id') else 'create')
        self.store.put(self._key(turn_id),value)
        return value

    def _refresh(self,turn_id):
        value=self._record(turn_id)
        if not value.get('recipe_candidate_id'):
            return value
        candidate=self.atoms.get_recipe_candidate(value['recipe_candidate_id'])
        if candidate.get('max_completion_tokens') is not None:
            value['max_completion_tokens'] = candidate['max_completion_tokens']
        value.update(recipe=deepcopy(candidate),state='completed' if candidate['status']=='candidate' else candidate['status'])
        self.store.put(self._key(turn_id),value)
        return value

    def resume(self,turn_id,max_completion_tokens=None):
        _completion_tokens(max_completion_tokens)
        value=self._record(turn_id)
        if value['state']=='awaiting_confirmation':
            return value
        if not value.get('recipe_candidate_id'):
            return self.generate(turn_id,value['request_key'],max_completion_tokens if max_completion_tokens is not None else value.get('request_max_completion_tokens'))
        value=self._refresh(turn_id)
        if value['state'] in {'failed','interrupted','awaiting_runner'}:
            options = {'max_completion_tokens': max_completion_tokens} if max_completion_tokens is not None else {}
            receipt = self.atoms.resume_recipe_candidate(value['recipe_candidate_id'], **options)
            value['max_completion_tokens'] = receipt.get('max_completion_tokens', max_completion_tokens if max_completion_tokens is not None else value.get('max_completion_tokens'))
            value['state']='queued'
            self.store.put(self._key(turn_id),value)
        elif max_completion_tokens is not None and max_completion_tokens != value.get('max_completion_tokens'):
            raise ValueError('cannot change the completion budget of an active or completed candidate')
        return value

    def wait(self,turn_id,timeout=None):
        timeout=self.config.timeout if timeout is None else timeout
        if isinstance(timeout,bool) or not isinstance(timeout,(float,int)) or not math.isfinite(timeout) or timeout<0:
            raise ValueError('timeout must be finite and nonnegative')
        deadline=time.monotonic()+timeout
        while True:
            value=self._refresh(turn_id)
            if value['state']=='failed':
                # The failed state is stored first, so resume_learning can retry it.
                raise RSICandidateFailed.from_candidate(value['recipe'])
            if value['state'] in {'awaiting_confirmation','completed','interrupted','awaiting_runner'}:
                return value
            if time.monotonic()>=deadline:
                raise TimeoutError('Poll or resume the same Recipe candidate; do not rerun the model')
            time.sleep(min(self.config.poll_interval,max(0,deadline-time.monotonic())))

    def close(self):
        self.client.close()
