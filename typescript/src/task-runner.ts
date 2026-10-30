/** Opt-in native Runner wrapper for the task workflow protocol. */
import { Runner, RunState } from '@openai/agents';
import { CoSensClient, type WorkflowOutputRef } from './atoms.js';
import { RSIApprovalUnavailable, RSIRecoveryRequired } from './errors.js';
import type { CoSensMode } from './openai-proxy.js';
import { canonical, hash, TurnStore } from './state.js';
import type { WorkflowPhase, WorkflowSnapshot } from './workflow-start.js';

interface TaskTurn {
  turnId: string; mode: CoSensMode; state: string; taskSpec: WorkflowSnapshot['task'];
  sessionId?: string | null; sessionIncarnation?: string; inputHash?: string;
  taskId?: string; executionId?: string; requestSha256?: string; request?: any;
  materials?: Array<{ name: string; content: string }>; outputRefs?: Array<{ artifact_id: string; content_hash: string }>;
  uploadInflight?: number | null; resultBody?: { requestSha256: string; outcome: 'completed'; artifacts: WorkflowOutputRef[]; events: Array<{ id: string; kind: string; payload: Record<string, unknown> }>; traceCoverage: 'full' | 'partial' | 'unknown' };
  events?: Array<{ id: string; kind: string; payload: Record<string, unknown> }>; traceCoverage?: 'full' | 'partial' | 'unknown';
  revisionId?: string; nativeState?: string; lastError?: string;
}
export interface TaskRunnerOptions {
  mode?: CoSensMode; apiKey?: string; statePath?: string; region?: string; fetch?: typeof fetch;
  resultAdapter?: (output: unknown) => Array<{ name: string; content: string }> | Promise<Array<{ name: string; content: string }>>;
  traceAdapter?: (result: unknown) => { events: Array<{ id: string; kind: string; payload: Record<string, unknown> }>; traceCoverage: 'full' | 'partial' | 'unknown' } | Promise<{ events: Array<{ id: string; kind: string; payload: Record<string, unknown> }>; traceCoverage: 'full' | 'partial' | 'unknown' }>;
  onRsiEvent?: (name: string, state: TaskTurn) => void | Promise<void>;
}
export interface TaskRunOptions {
  rsi?: { turnId: string; task: NonNullable<WorkflowSnapshot['task']>; authorize?: (phase: WorkflowPhase, snapshot: WorkflowSnapshot) => boolean | Promise<boolean> };
  [key: string]: any;
}

function withRequest(agent: any, request: unknown): any {
  if (typeof agent?.clone !== 'function') throw new TypeError('Agent must support clone to receive the frozen execution request');
  const block = `\n\n[CoSens frozen task execution request]\n${JSON.stringify(request)}`;
  const original = agent.instructions;
  if (original == null || typeof original === 'string') return agent.clone({ instructions: (original ?? '') + block });
  if (typeof original === 'function') return agent.clone({ instructions: async (...args: any[]) => String(await original(...args)) + block });
  throw new TypeError('Agent instructions cannot safely receive a frozen execution request');
}

function observedNativeItems(result: any): { events: Array<{ id: string; kind: string; payload: Record<string, unknown> }>; traceCoverage: 'partial' | 'unknown' } {
  if (!Array.isArray(result?.newItems)) return { events: [], traceCoverage: 'unknown' };
  const events: Array<{ id: string; kind: string; payload: Record<string, unknown> }> = [];
  for (const [index, item] of result.newItems.slice(0, 200).entries()) {
    if (typeof item?.type !== 'string' || !item.type) continue;
    const payload: Record<string, unknown> = { item_type: item.type };
    try {
      const raw = JSON.stringify(item.rawItem);
      if (raw !== undefined && Buffer.byteLength(raw, 'utf8') <= 4096) payload.raw_item = JSON.parse(raw);
    } catch { /* An unserializable item still has its observed type. */ }
    events.push({ id: `native-item-${index + 1}`, kind: item.type, payload });
  }
  return { events, traceCoverage: events.length ? 'partial' : 'unknown' };
}

export class TaskWorkflowRunner {
  readonly native: Runner;
  readonly cosens: { mode: CoSensMode; lastTurnId: string | null; atoms?: CoSensClient['atoms']; getTurn: (id: string) => TaskTurn | undefined; reconcile: (id: string, options?: { session?: any }) => Promise<TaskTurn> };
  private readonly client?: CoSensClient;
  private readonly turns?: TurnStore;
  constructor(nativeRunner: Runner | typeof Runner, private readonly options: TaskRunnerOptions = {}) {
    this.native = typeof nativeRunner === 'function' ? new nativeRunner() : nativeRunner;
    if ((options.mode ?? 'disable') !== 'disable') {
      this.client = new CoSensClient(options);
      this.turns = new TurnStore(options.statePath, this.client.transport.apiKey, this.client.transport.service.identity);
    }
    this.cosens = { mode: options.mode ?? 'disable', lastTurnId: null, atoms: this.client?.atoms,
      getTurn: id => this.turns?.get<TaskTurn>(id), reconcile: (id, opts) => this.reconcile(id, opts) };
  }
  private save(state: TaskTurn) { this.turns!.put(state.turnId, state); }
  private claimNative(state: TaskTurn, expected: string) {
    state.state = 'native_running';
    if (!this.turns!.claimState(state.turnId, expected, state)) throw new RSIRecoveryRequired('Workflow turn was claimed by another runner; reconcile before retrying');
  }
  private savePending(state: TaskTurn) {
    if (!this.turns!.updatePending(state.turnId, state)) throw new RSIRecoveryRequired('Workflow turn changed during preparation; reconcile before retrying');
  }
  private async sessionIdentity(session: any): Promise<[string | null, string]> {
    if (session == null) return [null, '0'];
    const id = session.sessionId ?? session.session_id ?? session.id ?? await session.getSessionId?.();
    if (id == null) throw new TypeError('Workflow Session requires a stable sessionId, session_id, id or getSessionId');
    return [String(id), String(session.sessionIncarnation ?? session.session_incarnation ?? session.incarnation ?? '0')];
  }
  private async requireSession(state: TaskTurn, session: any) {
    const [id, incarnation] = await this.sessionIdentity(session);
    if (state.sessionId !== id || state.sessionIncarnation !== incarnation) {
      throw new RSIRecoveryRequired('Workflow turn belongs to a different Session incarnation');
    }
  }
  private async emit(name: string, state: TaskTurn) {
    try { await this.options.onRsiEvent?.(name, structuredClone(state)); }
    catch (error) {
      (state as TaskTurn & { eventCallbackError?: string }).eventCallbackError = String(error);
      if (state.state === 'pre_execution_pending') {
        try { this.savePending(state); } catch (error) { if (!(error instanceof RSIRecoveryRequired)) throw error; }
      } else this.save(state);
    }
  }
  private async prepare(rsi: NonNullable<TaskRunOptions['rsi']>, agent: any, input: unknown, session: any): Promise<TaskTurn> {
    if (!rsi || typeof rsi.turnId !== 'string' || !rsi.turnId.trim()) throw new TypeError('rsi.turnId is required for a recoverable workflow turn');
    if (!rsi.task || typeof rsi.task !== 'object' || Array.isArray(rsi.task)) throw new TypeError('rsi.task is required for a workflow turn');
    const [sessionId, sessionIncarnation] = await this.sessionIdentity(session);
    const inputHash = hash({ agentName: agent?.name ?? null, input });
    let state = this.turns!.get<TaskTurn>(rsi.turnId);
    if (state && state.state !== 'pre_execution_pending') throw new RSIRecoveryRequired(`Workflow turn ${rsi.turnId} may have executed; reconcile without rerunning Agent`);
    if (state && canonical(state.taskSpec) !== canonical(rsi.task)) throw new RSIRecoveryRequired('Workflow task changed while recovering the same turn');
    if (state && (state.sessionId !== sessionId || state.sessionIncarnation !== sessionIncarnation || state.inputHash !== inputHash)) {
      throw new RSIRecoveryRequired('Workflow turn input or Session identity changed during recovery');
    }
    if (!state) {
      state = { turnId: rsi.turnId, taskSpec: rsi.task, state: 'pre_execution_pending', mode: this.cosens.mode,
        sessionId, sessionIncarnation, inputHash };
      if (!this.turns!.insertIfAbsent(state.turnId, state)) {
        state = this.turns!.get<TaskTurn>(rsi.turnId);
        if (state?.state !== 'pre_execution_pending' || canonical(state.taskSpec) !== canonical(rsi.task) ||
          state.sessionId !== sessionId || state.sessionIncarnation !== sessionIncarnation || state.inputHash !== inputHash) {
          throw new RSIRecoveryRequired('Workflow turn was already claimed or configured differently');
        }
      }
    }
    if (state.mode !== this.cosens.mode) throw new RSIRecoveryRequired('Workflow turn originated in a different mode');
    this.cosens.lastTurnId = state.turnId;
    if (!state.executionId) {
      const started = await this.client!.start(state.taskId ? { task_id: state.taskId } : { task: state.taskSpec }, { phase: 'prepare', mode: state.mode, requestKey: state.turnId, authorize: rsi.authorize });
      if (started?.task_id || started?.task?.task_id) {
        state.taskId = started.task_id ?? started.task.task_id; this.savePending(state);
      }
      if (started?.status !== 'awaiting_execution' || !started?.execution_id || !started?.request_sha256 || !started?.execution_request || typeof started.execution_request !== 'object') {
        throw new RSIRecoveryRequired(`Workflow task is ${started?.status}; provide context before executing`);
      }
      state.taskId = started.task_id; state.executionId = started.execution_id;
      state.requestSha256 = started.request_sha256; state.request = started.execution_request; this.savePending(state);
    }
    await this.emit('awaiting_execution', state);
    return state;
  }
  private async materials(output: unknown): Promise<Array<{ name: string; content: string }>> {
    if (output == null && !this.options.resultAdapter) throw new TypeError('native result has no output; supply resultAdapter before execution');
    const adapted = this.options.resultAdapter ? await this.options.resultAdapter(output) :
      (output && typeof output === 'object' && Array.isArray((output as any).files) ? (output as any).files :
        [{ name: 'result.txt', content: typeof output === 'string' ? output : JSON.stringify(output) }]);
    if (!Array.isArray(adapted) || !adapted.length || adapted.some(x => typeof x?.name !== 'string' || typeof x?.content !== 'string')) {
      throw new TypeError('completed workflow output requires text artifact objects with name/content');
    }
    return adapted;
  }
  private async sendResult(state: TaskTurn) {
    if (state.uploadInflight != null) throw new RSIRecoveryRequired('Artifact upload receipt is unknown; reconcile with the service before retrying');
    const refs = state.outputRefs ??= [];
    for (const material of state.materials!.slice(refs.length)) {
      state.uploadInflight = refs.length; this.save(state);
      const receipt = await this.client!.atoms.artifact(material);
      if (typeof receipt?.artifact_id !== 'string' || !receipt.artifact_id || typeof receipt?.content_hash !== 'string' || !receipt.content_hash) {
        throw new RSIRecoveryRequired('Artifact receipt lacks ID or server content hash');
      }
      refs.push({ artifact_id: receipt.artifact_id, content_hash: receipt.content_hash });
      state.uploadInflight = null; this.save(state);
    }
    if (!state.resultBody) {
      state.resultBody = { requestSha256: state.requestSha256!, outcome: 'completed',
        artifacts: refs.map(ref => ({ artifact_id: ref.artifact_id, role: 'submitted_output' })),
        events: state.events ?? [], traceCoverage: state.traceCoverage ?? 'unknown' };
      this.save(state);
    }
    const receipt = await this.client!.atoms.submitExecution(state.executionId!, state.resultBody);
    if (['failed', 'cancelled'].includes(receipt?.status) || typeof receipt?.revision_id !== 'string' || !receipt.revision_id) throw new RSIRecoveryRequired('Workflow result was not confirmed as completed');
    state.revisionId = receipt.revision_id; state.state = 'completed'; delete state.lastError; this.save(state);
    await this.emit('completed', state);
  }
  private async complete(state: TaskTurn, nativeResult: any) {
    state.state = 'native_completed'; this.save(state);
    try {
      state.materials = await this.materials(nativeResult.finalOutput);
      if (this.options.traceAdapter) {
        const observed = await this.options.traceAdapter(nativeResult);
        state.events = observed.events; state.traceCoverage = observed.traceCoverage;
      } else {
        const observed = observedNativeItems(nativeResult);
        state.events = observed.events; state.traceCoverage = observed.traceCoverage;
      }
      this.save(state); await this.sendResult(state);
    } catch (error) {
      state.state = 'completion_pending'; state.lastError = String(error); this.save(state);
    }
  }
  private interrupted(state: TaskTurn, result: any) {
    state.nativeState = result.state?.toString(); state.state = 'awaiting_native_approval'; this.save(state);
  }
  private streamed(state: TaskTurn, nativeResult: any): any {
    const owner = this;
    let finalized: Promise<void> | undefined;
    const finalize = () => finalized ??= (async () => {
      try {
        if (state.state === 'incomplete') return;
        await nativeResult.completed;
        if (state.state === 'incomplete') return;
        if (nativeResult.cancelled) { state.state = 'incomplete'; this.save(state); return; }
        if (nativeResult.interruptions?.length) this.interrupted(state, nativeResult);
        else await this.complete(state, nativeResult);
      } catch (error) {
        if (state.state === 'native_running') {
          state.state = 'incomplete'; state.lastError = String(error); this.save(state);
        }
        throw error;
      }
    })();
    return new Proxy(nativeResult, { get: (target, property) => {
      if (property === 'abort' || property === 'cancel') return (...args: any[]) => {
        try { return target[property]?.apply(target, args); }
        finally { if (state.state === 'native_running') { state.state = 'incomplete'; owner.save(state); } }
      };
      if (property === 'completed') return finalize();
      if (property === 'toStream' || property === 'toTextStream') return (...args: any[]) => {
        const value = target[property](...args); void finalize().catch(() => {}); return value;
      };
      if (property === Symbol.asyncIterator) return async function* () {
        let finished = false;
        try { for await (const event of nativeResult) yield event; finished = true; await finalize(); }
        finally { if (!finished && state.state === 'native_running') { state.state = 'incomplete'; owner.save(state); } }
      };
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    } });
  }
  async run(agent: any, input: any, { rsi, ...nativeOptions }: TaskRunOptions = {}): Promise<any> {
    if (this.cosens.mode === 'disable' || !rsi) return this.native.run(agent, input, nativeOptions);
    const state = await this.prepare(rsi, agent, input, nativeOptions.session);
    const activeAgent = withRequest(agent, state.request);
    this.claimNative(state, 'pre_execution_pending');
    let result: any;
    try { result = await this.native.run(activeAgent, input, nativeOptions); }
    catch (error) { state.state = 'native_failed'; state.lastError = String(error); this.save(state); throw error; }
    if (nativeOptions.stream && typeof result?.[Symbol.asyncIterator] === 'function') return this.streamed(state, result);
    if (result.interruptions?.length) this.interrupted(state, result);
    else await this.complete(state, result);
    return result;
  }
  runStreamed(agent: any, input: any, options: TaskRunOptions = {}) { return this.run(agent, input, { ...options, stream: true }); }
  async pendingState(agent: any, turnId: string, options: { session?: any } = {}) {
    const state = this.turns?.get<TaskTurn>(turnId);
    if (state?.state !== 'awaiting_native_approval' || !state.nativeState) throw new RSIRecoveryRequired('Turn has no pending native approval');
    await this.requireSession(state, options.session);
    return RunState.fromString(withRequest(agent, state.request), state.nativeState);
  }
  async resume(agent: any, turnId: string, nativeState: any, options: Record<string, any> = {}) {
    const state = this.turns?.get<TaskTurn>(turnId);
    if (state?.state !== 'awaiting_native_approval') throw new RSIRecoveryRequired('Turn is not awaiting native approval');
    await this.requireSession(state, options.session);
    this.claimNative(state, 'awaiting_native_approval');
    let result: any;
    try { result = await this.native.run(withRequest(agent, state.request), nativeState, options); }
    catch (error) { state.state = 'native_failed'; state.lastError = String(error); this.save(state); throw error; }
    if (options.stream && typeof result?.[Symbol.asyncIterator] === 'function') return this.streamed(state, result);
    if (result.interruptions?.length) this.interrupted(state, result);
    else await this.complete(state, result);
    return result;
  }
  async reconcile(turnId: string, options: { session?: any } = {}): Promise<TaskTurn> {
    const state = this.turns?.get<TaskTurn>(turnId);
    if (!state || !['native_completed', 'completion_pending'].includes(state.state) || !state.materials) {
      throw new RSIRecoveryRequired('Turn has no completed native result available for reconciliation');
    }
    await this.requireSession(state, options.session);
    try { await this.sendResult(state); }
    catch (error) { state.state = 'completion_pending'; state.lastError = String(error); this.save(state); throw error; }
    return state;
  }
  async close() { this.turns?.close(); await this.client?.close(); }
}
