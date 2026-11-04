"""Explicit Agents SDK entry, backed exclusively by Task/Execution HTTP."""
import asyncio
import hashlib
import inspect
import json
from uuid import uuid4

from .task_runner import TaskWorkflowRunner
from .errors import RSIRecoveryRequired


class CoSensAgentRunner:
    def __init__(self, agent, *, mode, session, api_key, state_path, http_transport, runner):
        if runner is None:
            from agents import Runner
            runner = Runner
        self.native = agent
        self._session = session
        # An explicit run records execution even when automatic decisions are disabled.
        self._workflow = TaskWorkflowRunner(runner, mode='approval' if mode=='approval' else 'auto',
            api_key=api_key,state_path=state_path,_http_transport=http_transport)
        self._client = self._workflow._client
        self.cosens = self._workflow.cosens
        self.cosens.mode = mode
        self.cosens.session = session
        self.cosens.reconcile = self.reconcile

    async def _prepare_inputs(self, agent, user_input, *, session=None, input_artifacts=None, turn_id=None, authorize=None):
        session = session if session is not None else self._session
        session_identity = str(getattr(session,'session_id',None) or getattr(session,'id',None))
        incarnation = str(getattr(session,'session_incarnation',None) or getattr(session,'incarnation','0'))
        turn_id = turn_id or 'agent:'+hashlib.sha256(json.dumps([session_identity,incarnation,user_input],sort_keys=True,default=str).encode()).hexdigest()
        self.cosens.last_turn_id = turn_id
        store = self._workflow._store
        signature = hashlib.sha256(json.dumps([user_input,input_artifacts],sort_keys=True,default=str).encode()).hexdigest()
        key = 'agent-input:'+turn_id
        initial = store.get(key)
        if initial and initial.get('mode') != self.cosens.mode:
            raise RSIRecoveryRequired('Turn has a different originating mode')
        if initial and initial['signature'] != signature:
            raise RSIRecoveryRequired('Input changed for the same turn')
        if not initial:
            initial={'signature':signature,'refs':[],'pending':False,'mode':self.cosens.mode}
            store.put(key,initial)
        if initial['pending']:
            raise RSIRecoveryRequired('Artifact upload receipt is unknown; reconcile before retrying')
        if 'materials' not in initial:
            history=[]
            if session is not None and callable(getattr(session,'get_items',None)):
                history=session.get_items()
                if inspect.isawaitable(history):history=await history
            initial['materials']=[{'name':'task-input.json','content':json.dumps({'input':user_input,'session':history},default=str)},*(input_artifacts or [])]
            store.put(key,initial)
        for material in initial['materials'][len(initial['refs']):]:
            initial['pending']=True;store.put(key,initial)
            ref=await self._client.atoms.artifact(name=material['name'],content=material['content'])
            if not isinstance(ref.get('artifact_id'),str) or not ref['artifact_id'] or not isinstance(ref.get('content_hash'),str) or not ref['content_hash']:
                raise RSIRecoveryRequired('Artifact receipt is missing ID or content hash')
            initial['refs'].append(ref['artifact_id']);initial['pending']=False;store.put(key,initial)
        rsi={'turn_id':turn_id,'task':{'source':'new_task','objective':str(user_input),'artifacts':initial['refs']},'authorize':authorize}
        return rsi, session

    async def run(self, agent, user_input, *, session=None, input_artifacts=None, turn_id=None, authorize=None, **options):
        rsi,session=await self._prepare_inputs(agent,user_input,session=session,input_artifacts=input_artifacts,turn_id=turn_id,authorize=authorize)
        result=await self._workflow.run(agent,user_input,rsi=rsi,session=session,**options)
        state=self.cosens.get_turn(rsi['turn_id'])
        if session is not None and state and state.get('state')=='completed':
            await self._session_events(session,state)
        return result

    async def run_streamed(self, agent, user_input, *, session=None, input_artifacts=None, turn_id=None, authorize=None, **options):
        rsi,session=await self._prepare_inputs(agent,user_input,session=session,input_artifacts=input_artifacts,turn_id=turn_id,authorize=authorize)
        return self._workflow.run_streamed(agent,user_input,rsi=rsi,session=session,**options)

    def run_sync(self, agent, user_input, **options):
        return asyncio.run(self.run(agent,user_input,**options))

    async def pending_state(self, agent, turn_id):
        return await self._workflow.pending_state(agent,turn_id,session=self._session)

    async def resume(self, agent, turn_id, native_state, *, session=None, **options):
        return await self._workflow.resume(agent,turn_id,native_state,session=session or self._session,**options)

    async def _session_events(self,session,state):
        """Persist ACK only after the server accepted the exact event batch."""
        if not callable(getattr(session,'get_items',None)):return
        items=session.get_items()
        if inspect.isawaitable(items):items=await items
        identity=str(getattr(session,'session_id',None) or getattr(session,'id',None))
        incarnation=str(getattr(session,'session_incarnation','0'))
        key='agent-session:'+hashlib.sha256((identity+':'+incarnation).encode()).hexdigest()
        saved=self._workflow._store.get(key)
        if not saved:
            opened=await self._client.atoms.start_session()
            saved={'session_id':opened['session_id'],'acked':0,'prefix':hashlib.sha256(b'[]').hexdigest()}
            self._workflow._store.put(key,saved)
        encode=lambda value:json.dumps(value,sort_keys=True,default=str).encode()
        if saved['acked']>len(items) or hashlib.sha256(encode(items[:saved['acked']])).hexdigest()!=saved['prefix']:
            raise RSIRecoveryRequired('Session history changed after acknowledgement')
        for offset in range(saved['acked'],len(items),200):
            batch=items[offset:offset+200]
            events=[{'event_id':'message:'+hashlib.sha256(encode([offset+i,item])).hexdigest(),
                     'kind':'session_message','payload':item if isinstance(item,dict) else {'content':str(item)}} for i,item in enumerate(batch)]
            await self._client.atoms.append_session_events(saved['session_id'],events)
            saved.update(acked=offset+len(batch),prefix=hashlib.sha256(encode(items[:offset+len(batch)])).hexdigest())
            self._workflow._store.put(key,saved)

    async def reconcile(self,turn_id,*,session=None):
        state=await self._workflow.reconcile(turn_id,session=session or self._session)
        if session or self._session:await self._session_events(session or self._session,state)
        return state

    async def aclose(self):
        await self._workflow.aclose()

    async def __aenter__(self):return self
    async def __aexit__(self,*args):await self.aclose()
