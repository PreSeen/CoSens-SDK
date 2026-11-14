"""Explicit Recipe requests after external confirmation. No automatic learning."""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import time
import math

from .atoms import CoSensClient
from .errors import RSILearningFailed, RSILearningStopped, RSIRecoveryRequired
from .state import TurnStore
from .workflow_atoms import _LEARNING_IN_FLIGHT, _LEARNING_RESUMABLE, _LEARNING_STOPPED, _completion_tokens


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
        value = {'turn_id':turn['turn_id'], 'task_id':turn.get('task_id'), 'execution_id':turn.get('execution_id'),
                 'used_recipe_id':turn.get('used_recipe_id'), 'used_recipe_version':turn.get('used_recipe_version'),
                 'state':'awaiting_confirmation', 'action':'noop', 'reason':'external_confirmation_required'}
        self.store.insert_if_absent(self._key(turn['turn_id']), value)
        return self.get(turn['turn_id'])

    def _record(self, turn_id):
        value=self.get(turn_id)
        if not value or not value.get('execution_id'):
            raise RSIRecoveryRequired('Turn has no completed execution')
        return value

    def _save(self, turn_id, value):
        self.store.put(self._key(turn_id), value)
        return value

    def _learned(self, value, learning):
        """Store a learning view on the record; the record's state is the learning status."""
        value.update(learning=deepcopy(dict(learning)), state=learning['status'], recipe_id=learning.get('recipe_id'),
                     version=learning.get('version'), action='revise' if learning.get('previous_version') else 'create')
        if learning.get('max_completion_tokens') is not None:
            value['max_completion_tokens'] = learning['max_completion_tokens']
        return value

    def feedback(self, turn_id, message, source):
        value=self._record(turn_id)
        key='feedback:'+hashlib.sha256((turn_id+'\0'+message).encode()).hexdigest()
        value['feedback']=self.atoms.record_feedback(value['execution_id'], feedback=message, source=source, key=key)
        return self._save(turn_id, value)

    def generate(self, turn_id, max_completion_tokens=None):
        """Calling this is the host's explicit confirmation signal; an execution learns once."""
        _completion_tokens(max_completion_tokens)
        value=self._record(turn_id)
        if value.get('learning'):
            if max_completion_tokens is not None and max_completion_tokens!=value.get('max_completion_tokens'):
                raise ValueError('this execution already learned with a different completion budget')
            return value
        budget = max_completion_tokens if max_completion_tokens is not None else value.get('max_completion_tokens')
        value.update(state='request_pending',reason='explicit_generation_requested',max_completion_tokens=budget)
        self._save(turn_id, value)
        options = {'max_completion_tokens': budget} if budget is not None else {}
        return self._save(turn_id, self._learned(value, self.atoms.learn(value['execution_id'], **options)))

    def _refresh(self,turn_id):
        value=self._record(turn_id)
        if not value.get('learning'):
            return value
        learning=self.atoms.get_execution(value['execution_id']).get('learning')
        if not isinstance(learning, dict):
            raise RSIRecoveryRequired('The execution lost its learning; reconcile with the service')
        return self._save(turn_id, self._learned(value, learning))

    def resume(self,turn_id,max_completion_tokens=None):
        _completion_tokens(max_completion_tokens)
        value=self._record(turn_id)
        if value['state']=='awaiting_confirmation':
            return value
        if not value.get('learning'):
            return self.generate(turn_id, max_completion_tokens)
        value=self._refresh(turn_id)
        if value['state'] in _LEARNING_RESUMABLE:
            options = {'max_completion_tokens': max_completion_tokens} if max_completion_tokens is not None else {}
            return self._save(turn_id, self._learned(value, self.atoms.resume_learning(value['execution_id'], **options)))
        if max_completion_tokens is not None and max_completion_tokens != value.get('max_completion_tokens'):
            raise ValueError('only a failed, interrupted or awaiting_runner learning takes a new completion budget')
        return value

    def wait(self,turn_id,timeout=None):
        """Return the record once its learning completed, or at once while no learning was requested.

        ``RSILearningFailed`` (``failed``) and ``RSILearningStopped`` (``interrupted``,
        ``awaiting_runner``, ``awaiting_material``) are raised after the record is
        stored, so ``resume`` can continue a resumable learning.
        """
        timeout=self.config.timeout if timeout is None else timeout
        if isinstance(timeout,bool) or not isinstance(timeout,(float,int)) or not math.isfinite(timeout) or timeout<0:
            raise ValueError('timeout must be finite and nonnegative')
        deadline=time.monotonic()+timeout
        while True:
            value=self._refresh(turn_id)
            if value['state']=='failed':
                raise RSILearningFailed.from_learning(value['learning'])
            if value['state'] in _LEARNING_STOPPED:
                raise RSILearningStopped(value['learning'])
            if value['state'] in {'awaiting_confirmation','completed'}:
                return value
            if not value.get('learning'):
                raise RSIRecoveryRequired('Learning was requested but not confirmed; call resume_learning to request it again')
            if value['state'] not in _LEARNING_IN_FLIGHT:
                raise RSIRecoveryRequired(f"Unexpected learning status: {value['state']}")
            if time.monotonic()>=deadline:
                raise TimeoutError('Learning is still running; wait again or resume it, do not rerun the model')
            time.sleep(min(self.config.poll_interval,max(0,deadline-time.monotonic())))

    def close(self):
        self.client.close()
