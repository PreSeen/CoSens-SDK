import { randomUUID } from 'node:crypto';
import { Runner, RunState } from '@openai/agents';
import { CoSensClient } from './atoms.js';
import { freezeRun } from './context.js';
import { CursorStore, type SessionLike, type Snapshot } from './cursor.js';
import { RSIRecoveryRequired } from './errors.js';
import type { CoSensMode, CoSensOptions } from './openai-proxy.js';
import { canonical, hash, TurnStore } from './state.js';

interface AgentTurn {
  turnId: string; mode: CoSensMode; state: string; objective: string; runId?: string; prepared?: any; frozenContext?: any;
  sessionId?: string | null; sessionIncarnation?: string;
  inputArtifacts?: Array<{ name: string; content: string }>; inputRefs?: any[]; inputUploadInflight?: number | null;
  outputFiles?: unknown; outputRefs?: any[]; outputUploadInflight?: number | null;
  result?: any; nativeState?: string; lastError?: string; logicalKey?: string;
}
export interface AgentRunnerOptions extends CoSensOptions { runner?: { run(agent: any, input: any, options?: any): Promise<any> }; fetcher?: typeof fetch }

export class CoSensAgentRunner {
  readonly native: any;
  readonly cosens: { mode: CoSensMode; session?: SessionLike; lastTurnId: string | null; getTurn: (id: string | null) => AgentTurn | undefined; reconcile: (id: string, options?: { session?: SessionLike }) => Promise<AgentTurn> };
  private readonly client: CoSensClient;
  private readonly cursor: CursorStore;
  private readonly turns: TurnStore;
  private readonly runner: { run(agent: any, input: any, options?: any): Promise<any> };
  private readonly locks = new Map<string, Promise<void>>();
  constructor(agent: any, private readonly options: AgentRunnerOptions = {}) {
    this.native = agent;
    this.client = new CoSensClient({ ...options, fetch: options.fetcher ?? options.fetch });
    this.cursor = new CursorStore(options.statePath, this.client.transport.apiKey, this.client.transport.service.identity);
    this.turns = new TurnStore(options.statePath, this.client.transport.apiKey, this.client.transport.service.identity);
    this.runner = options.runner ?? new Runner();
    this.cosens = { mode: options.mode ?? 'auto', session: options.session, lastTurnId: null, getTurn: id => id ? this.turns.get<AgentTurn>(id) : undefined, reconcile: (id, opts) => this.reconcile(id, opts) };
  }
  private save(key: string, state: AgentTurn) { state.logicalKey = key; this.turns.putMany([state.turnId, key], state); }
  private requireMode(state: AgentTurn) {
    if (state.mode !== this.cosens.mode) throw new RSIRecoveryRequired('Turn has a different or unknown originating mode');
  }
  private async requireSession(state: AgentTurn, session?: SessionLike) {
    const id = session ? (session.sessionId ?? session.session_id ?? session.id ?? await session.getSessionId?.()) : null;
    const incarnation = String(session?.sessionIncarnation ?? session?.incarnation ?? '0');
    if (state.sessionId === undefined || state.sessionIncarnation === undefined ||
      state.sessionId !== (id == null ? null : String(id)) || state.sessionIncarnation !== incarnation) {
      throw new RSIRecoveryRequired('Turn belongs to a different or unknown Session incarnation');
    }
  }
  private async withLock<T>(session: SessionLike | undefined, fn: (hold: (until: Promise<unknown>) => void) => Promise<T>): Promise<T> {
    const id = session ? (session.sessionId ?? session.session_id ?? session.id ?? await session.getSessionId?.()) : `none:${randomUUID()}`;
    const previous = this.locks.get(id!) ?? Promise.resolve();
    let release!: () => void; const current = new Promise<void>(resolve => release = resolve);
    const lane = previous.then(() => current);
    this.locks.set(id!, lane);
    await previous;
    let releaseSession = () => {};
    const unlock = () => { try { releaseSession(); } finally { release(); if (this.locks.get(id!) === lane) this.locks.delete(id!); } };
    let held: Promise<unknown> | undefined;
    try {
      if (session) releaseSession = await this.cursor.acquireSession(session);
      const value = await fn(until => { held = until; });
      if (held) void held.then(unlock, unlock);
      else unlock();
      return value;
    } catch (error) { unlock(); throw error; }
  }
  private async sendPending(runId: string, session?: SessionLike, initialSnapshot?: Snapshot) {
    if (!session) return;
    const snap = initialSnapshot ?? await this.cursor.snapshot(session);
    let recovery = this.cursor.recoverBatch(snap);
    if (recovery && recovery.runId !== runId) throw new RSIRecoveryRequired(`Session has unsent events for Run ${recovery.runId}`);
    while (true) {
      const events = recovery?.events ?? this.cursor.pending(snap);
      if (!events.length) break;
      this.cursor.beginBatch(runId, snap, events);
      const receipt = await this.client.atoms.sessionEvents(runId, events);
      if (!this.cursor.ack(snap, receipt)) throw new RSIRecoveryRequired('CoSens did not durably acknowledge the first pending event');
      recovery = this.cursor.recoverBatch(snap);
    }
  }
  private async upload(key: string, state: AgentTurn, kind: 'input' | 'output') {
    const materials = kind === 'input' ? state.inputArtifacts ?? [] : state.outputFiles ?? [];
    if (!Array.isArray(materials)) throw new TypeError('finalOutput.files must be an array of text artifacts');
    const refs = kind === 'input' ? state.inputRefs ??= [] : state.outputRefs ??= [];
    const inflight = kind === 'input' ? 'inputUploadInflight' : 'outputUploadInflight';
    if (state[inflight] != null) throw new RSIRecoveryRequired('Artifact upload receipt is unknown; reconcile before retrying');
    for (const material of materials.slice(refs.length)) {
      if (typeof material?.name !== 'string' || typeof material?.content !== 'string') throw new TypeError('Artifacts must contain name/content text');
      state[inflight] = refs.length; this.save(key, state);
      const receipt = await this.client.atoms.artifact(material);
      if (typeof receipt?.artifact_id !== 'string' || !receipt.artifact_id.trim() ||
        typeof receipt?.content_hash !== 'string' || !receipt.content_hash.trim()) {
        throw new RSIRecoveryRequired('Artifact upload receipt is missing artifact_id/content_hash; reconcile before retrying');
      }
      refs.push({ artifact_id: receipt.artifact_id, content_hash: receipt.content_hash });
      state[inflight] = null; this.save(key, state);
    }
    if (kind === 'output' && refs.length) { state.result.files = refs; this.save(key, state); }
  }
  private withContext(agent: any, state: AgentTurn) {
    const run = state.frozenContext;
    const block = `\n\n[CoSens frozen context; source: RSI Run ${run.run_id}]\n${JSON.stringify({ recipe: run.recipe, context: run.context, snapshot: run.snapshot })}`;
    if (!agent?.clone || typeof agent.clone !== 'function') throw new TypeError('Agent must support immutable clone');
    const instructions = agent.instructions;
    if (instructions == null || typeof instructions === 'string') return agent.clone({ instructions: (instructions ?? '') + block });
    if (typeof instructions === 'function') return agent.clone({ instructions: async (...args: any[]) => String(await instructions(...args)) + block });
    throw new TypeError('Agent instructions cannot be safely extended');
  }
  private resultData(output: any) {
    const data = output && typeof output === 'object' && !Array.isArray(output) ? { ...output } : { summary: String(output ?? '') };
    delete data.files; data.summary = String(data.summary ?? ''); data.outcome = 'completed';
    return data;
  }
  private async complete(key: string, state: AgentTurn, nativeResult: any, session?: SessionLike) {
    state.state = 'native_completed'; state.result = this.resultData(nativeResult.finalOutput);
    state.outputFiles = nativeResult.finalOutput?.files ?? [];
    this.save(key, state);
    try {
      await this.sendPending(state.runId!, session);
      await this.upload(key, state, 'output');
      const receipt = await this.client.atoms.submit(state.runId!, { data: state.result });
      if (receipt.status !== 'completed') throw new RSIRecoveryRequired('CoSens submit did not confirm completion');
      state.state = 'completed'; delete state.lastError;
    } catch (error) { state.state = 'completion_pending'; state.lastError = String(error); }
    this.save(key, state);
  }
  private streamed(key: string, state: AgentTurn, nativeResult: any, session: SessionLike | undefined, hold: (until: Promise<unknown>) => void): any {
    const self = this;
    let releaseStream!: () => void;
    hold(new Promise<void>(resolve => { releaseStream = resolve; }));
    let finalized: Promise<void> | undefined;
    const markIncomplete = () => {
      if (state.state === 'native_running') { state.state = 'incomplete'; self.save(key, state); }
      releaseStream();
    };
    const finalize = () => finalized ??= (async () => {
      try {
        if (nativeResult.completed) await nativeResult.completed;
        if (nativeResult.cancelled) { markIncomplete(); return; }
        if (nativeResult.interruptions?.length) {
          state.nativeState = nativeResult.state?.toString(); state.state = 'awaiting_native_approval'; self.save(key, state);
        } else await self.complete(key, state, nativeResult, session);
      } catch (error) { markIncomplete(); throw error; }
      finally { releaseStream(); }
    })();
    return new Proxy(nativeResult, { get(target, property) {
      if (property === 'abort') return (...args: any[]) => {
        try { return target.abort?.apply(target, args); } finally { markIncomplete(); }
      };
      if (property === 'completed') return finalize();
      if (property === 'toStream' || property === 'toTextStream') return (...args: any[]) => {
        const result = target[property](...args);
        void finalize().catch(() => {});
        return result;
      };
      if (property !== Symbol.asyncIterator) {
        const member = Reflect.get(target, property, target);
        return typeof member === 'function' ? member.bind(target) : member;
      }
      return async function* () {
        let finished = false;
        try {
          for await (const event of nativeResult) yield event;
          finished = true;
          await finalize();
        } finally {
          if (!finished) markIncomplete();
        }
      };
    } });
  }
  async run(agent: any, input: any, options: { session?: SessionLike; inputArtifacts?: Array<{ name: string; content: string }>; [key: string]: any } = {}): Promise<any> {
    const session = options.session ?? this.cosens.session;
    return this.withLock(session, async hold => {
      const objective = typeof input === 'string' ? input : JSON.stringify(input);
      const sessionId = session ? (session.sessionId ?? session.session_id ?? session.id ?? await session.getSessionId?.()) : null;
      const sessionIncarnation = String(session?.sessionIncarnation ?? session?.incarnation ?? '0');
      const key = `logical:${hash([sessionId, sessionIncarnation, agent.name, objective])}`;
      const previous = this.turns.get<AgentTurn>(key);
      if (previous) this.requireMode(previous);
      if (previous && !['pre_execution_pending', 'completed'].includes(previous.state)) throw new RSIRecoveryRequired(`Turn ${previous.turnId} may have executed; reconcile without rerunning Agent`);
      if (previous?.state === 'pre_execution_pending' && options.inputArtifacts !== undefined &&
        hash(options.inputArtifacts) !== hash(previous.inputArtifacts ?? [])) {
        throw new RSIRecoveryRequired('Input artifacts changed during Agent pre-execution recovery');
      }
      const state: AgentTurn = previous?.state === 'pre_execution_pending' ? previous : { turnId: randomUUID(), mode: this.cosens.mode, state: 'pre_execution_pending', objective, sessionId: sessionId == null ? null : String(sessionId), sessionIncarnation, inputArtifacts: options.inputArtifacts ?? [] };
      this.cosens.lastTurnId = state.turnId; this.save(key, state);
      try {
        const initialSnapshot = session ? await this.cursor.snapshot(session) : undefined;
        if (!state.runId) {
          await this.upload(key, state, 'input');
          state.prepared = await this.client.atoms.prepareReady({ objective, key: state.turnId, inputArtifactIds: state.inputRefs?.map(x => x.artifact_id) ?? [] });
          state.runId = state.prepared.run_id; this.save(key, state);
        }
        if (!state.frozenContext) { state.frozenContext = freezeRun(state.prepared, await this.client.atoms.getRun(state.runId!)); this.save(key, state); }
        await this.sendPending(state.runId!, session, initialSnapshot);
      } catch (error) { state.lastError = String(error); this.save(key, state); throw error; }
      const activeAgent = this.withContext(agent, state);
      state.state = 'native_running'; this.save(key, state);
      const { inputArtifacts: _artifacts, ...nativeOptions } = options;
      let nativeResult: any;
      try { nativeResult = await this.runner.run(activeAgent, input, { ...nativeOptions, session }); }
      catch (error) { state.state = 'native_failed'; state.lastError = String(error); this.save(key, state); throw error; }
      if (options.stream && typeof nativeResult?.[Symbol.asyncIterator] === 'function') return this.streamed(key, state, nativeResult, session, hold);
      if (nativeResult.interruptions?.length) {
        state.nativeState = nativeResult.state?.toString(); state.state = 'awaiting_native_approval'; this.save(key, state); return nativeResult;
      }
      await this.complete(key, state, nativeResult, session);
      return nativeResult;
    });
  }
  runStreamed(agent: any, input: any, options: { session?: SessionLike; inputArtifacts?: Array<{ name: string; content: string }>; [key: string]: any } = {}) {
    return this.run(agent, input, { ...options, stream: true });
  }
  async pendingState(agent: any, turnId: string) {
    const state = this.turns.get<AgentTurn>(turnId);
    if (state) this.requireMode(state);
    if (state?.state !== 'awaiting_native_approval' || !state.nativeState || !state.frozenContext) throw new RSIRecoveryRequired('Turn has no pending native tool approval');
    return RunState.fromString(this.withContext(agent, state), state.nativeState);
  }
  async resume(agent: any, turnId: string, nativeState: any, options: { session?: SessionLike; [key: string]: any } = {}) {
    const session = options.session ?? this.cosens.session;
    return this.withLock(session, async hold => {
      const state = this.turns.get<AgentTurn>(turnId);
      if (state) this.requireMode(state);
      if (state) await this.requireSession(state, session);
      if (state?.state !== 'awaiting_native_approval' || !state.frozenContext) throw new RSIRecoveryRequired('Turn is not awaiting native tool approval');
      state.state = 'native_running'; this.save(state.logicalKey!, state);
      let result: any;
      try { result = await this.runner.run(this.withContext(agent, state), nativeState, { ...options, session }); }
      catch (error) { state.state = 'native_failed'; state.lastError = String(error); this.save(state.logicalKey!, state); throw error; }
      if (options.stream && typeof result?.[Symbol.asyncIterator] === 'function') return this.streamed(state.logicalKey!, state, result, session, hold);
      if (result.interruptions?.length) { state.nativeState = result.state?.toString(); state.state = 'awaiting_native_approval'; this.save(state.logicalKey!, state); return result; }
      await this.complete(state.logicalKey!, state, result, session); return result;
    });
  }
  async reconcile(id: string, options: { session?: SessionLike } = {}): Promise<AgentTurn> {
    return this.withLock(options.session ?? this.cosens.session, async () => {
    const state = this.turns.get<AgentTurn>(id);
    if (state) this.requireMode(state);
    if (state) await this.requireSession(state, options.session ?? this.cosens.session);
    if (!state?.runId || !['native_completed', 'completion_pending'].includes(state.state)) throw new RSIRecoveryRequired('Turn has no completed native result available for reconciliation');
    try {
      await this.sendPending(state.runId, options.session ?? this.cosens.session);
      const remote = await this.client.atoms.getRun(state.runId);
      if (remote.status === 'completed') {
        const files = state.outputFiles ?? [];
        if (!Array.isArray(files)) throw new TypeError('finalOutput.files must be an array of text artifacts');
        if (state.outputUploadInflight != null || (state.outputRefs?.length ?? 0) !== files.length) {
          throw new RSIRecoveryRequired('Output Artifact receipts remain unresolved; cannot accept remote completion');
        }
        if (canonical(remote.result) !== canonical(state.result)) throw new RSIRecoveryRequired('Remote completed Run result differs from locally saved result');
      } else if (remote.status === 'ready') {
        await this.upload(state.logicalKey!, state, 'output');
        const receipt = await this.client.atoms.submit(state.runId, { data: state.result });
        if (receipt.status !== 'completed') throw new RSIRecoveryRequired('Recovered submit was not confirmed');
      } else throw new RSIRecoveryRequired(`Run ${state.runId} is ${remote.status}`);
      state.state = 'completed'; delete state.lastError; this.save(state.logicalKey!, state); return state;
    } catch (error) { state.state = 'completion_pending'; state.lastError = String(error); this.save(state.logicalKey!, state); throw error; }
    });
  }
  async close() { this.cursor.close(); this.turns.close(); await this.client.close(); }
}
