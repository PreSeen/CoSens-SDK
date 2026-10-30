import { AsyncLocalStorage } from 'node:async_hooks';
import { randomUUID } from 'node:crypto';
import { Agent, Runner } from '@openai/agents';
import { CoSensAgentRunner, type AgentRunnerOptions } from './agent-runner.js';
import { TaskWorkflowRunner, type TaskRunnerOptions } from './task-runner.js';
import { CoSensClient } from './atoms.js';
import { resolveApiKey, resolveService } from './config.js';
import { CursorStore, type SessionLike, type SessionEvent, type Snapshot } from './cursor.js';
import { decide, type Decision } from './decision.js';
import { DeferredStream } from './deferred-stream.js';
import { RSIApprovalUnavailable, RSIDecisionUnavailable, RSIRecoveryRequired } from './errors.js';
import { inject, summary, terminal, toolContinuation, toolNames, userText } from './context.js';
import { canonical, defaultStatePath, hash, TurnStore } from './state.js';

export type CoSensMode = 'auto' | 'approval' | 'disable';
export interface CoSensOptions {
  apiKey?: string; mode?: CoSensMode; session?: SessionLike; statePath?: string; region?: string; fetch?: typeof fetch;
  decisionModel?: string; decisionTimeoutMs?: number; decisionMaxOutputTokens?: number;
}
export interface Turn {
  turnId: string; mode: CoSensMode; state: string; objective: string; taskFamily: string; runId: string | null;
  decision: Decision; requestKeys: string[]; prepared?: any; frozenContext?: any; result?: any; visibleInput?: string;
  observablePending?: SessionEvent[]; observableAckedIds?: string[]; lastError?: string; sessionBeforeCount?: number; visibleInputInSession?: boolean;
  sessionId?: string | null; sessionIncarnation?: string; conversationKey?: string; continuationKeys?: string[]; toolResponse?: any;
}
export interface CoSensProxyStatus {
  mode: CoSensMode;
  session?: SessionLike;
  lastTurnId: string | null;
  rsiCapture: string | null;
  getTurn(id: string | null): Turn | undefined;
  reconcile(id: string): Promise<Turn>;
  scope<T>(ids: { sessionId?: string; turnId?: string }, fn: () => T): T;
}
export interface CoSensProxyExtras<T> { native: T; cosens: CoSensProxyStatus; close(): Promise<void> }
export type CoSensProxy<T extends object> = Omit<T, 'close' | 'withOptions' | 'copy'> & CoSensProxyExtras<T> &
  (T extends { withOptions: (...args: infer A) => infer R } ? { withOptions(...args: A): CoSensProxy<Extract<R, object>> } : {}) &
  (T extends { copy: (...args: infer A) => infer R } ? { copy(...args: A): CoSensProxy<Extract<R, object>> } : {});
const SUPPORTED = new Set(['responses.create', 'responses.parse', 'responses.stream', 'chat.completions.create', 'chat.completions.parse', 'chat.completions.stream']);
const MODEL_PREFIXES = new Set(['responses', 'chat', 'completions', 'images', 'audio', 'embeddings', 'videos', 'realtime']);
const GENERATION_METHODS = new Set(['create', 'parse', 'stream', 'generate', 'edit', 'createVariation', 'runTools', 'runFunctions', 'connect', 'compact']);
interface ProxyEntry { wrapper: OpenAIWrapper; proxy: any; config: { apiKey: string; serviceId: string; mode: CoSensMode; session?: SessionLike; statePath: string; fetch?: typeof fetch; decisionModel?: string; decisionTimeoutMs?: number; decisionMaxOutputTokens?: number } }
const PROXIES = new WeakMap<object, ProxyEntry>();

export class OpenAIWrapper {
  readonly native: any;
  readonly cosens: {
    mode: CoSensMode; session?: SessionLike; lastTurnId: string | null; rsiCapture: string | null;
    getTurn: (id: string | null) => Turn | undefined; reconcile: (id: string) => Promise<Turn>;
    scope: <T>(ids: { sessionId?: string; turnId?: string }, fn: () => T) => T;
  };
  private readonly client: CoSensClient;
  private readonly cursor: CursorStore;
  private readonly turns: TurnStore;
  private readonly scopeStore = new AsyncLocalStorage<{ sessionId?: string; turnId?: string }>();
  private closed = false;
  private owners = 1;
  constructor(native: any, private readonly options: CoSensOptions, private readonly onClose?: () => void) {
    this.native = native;
    this.client = new CoSensClient(options);
    this.cursor = new CursorStore(options.statePath, this.client.transport.apiKey, this.client.transport.service.identity);
    this.turns = new TurnStore(options.statePath, this.client.transport.apiKey, this.client.transport.service.identity);
    this.cosens = {
      mode: options.mode ?? 'auto', session: options.session, lastTurnId: null, rsiCapture: null,
      getTurn: id => id ? this.turns.get<Turn>(id) : undefined,
      reconcile: id => this.reconcile(id),
      scope: (ids, fn) => this.scopeStore.run(ids, fn),
    };
  }
  retain() { if (this.closed) throw new RSIRecoveryRequired('CoSens proxy is closed'); this.owners++; }
  private async sessionId(): Promise<string | null> {
    const session = this.options.session;
    const id = this.scopeStore.getStore()?.sessionId ?? session?.sessionId ?? session?.session_id ?? session?.id ?? await session?.getSessionId?.();
    return id == null ? null : String(id);
  }
  private sessionIncarnation(): string {
    const session = this.options.session;
    return String(session?.sessionIncarnation ?? session?.incarnation ?? '0');
  }
  private requireSession(turn: Turn, sessionId: string | null): void {
    if (turn.sessionId !== sessionId || turn.sessionIncarnation !== this.sessionIncarnation()) {
      throw new RSIRecoveryRequired('Turn belongs to a different or unknown Session incarnation');
    }
  }
  private requestKey(path: string[], params: any, sessionId: string | null): string {
    const session = this.options.session;
    return `request:${hash([sessionId ?? null, session?.sessionIncarnation ?? session?.incarnation ?? '0', path, params])}`;
  }
  private conversationKey(value: any, sessionId: string | null): string | undefined {
    const id = typeof value === 'string' ? value : value?.id;
    if (typeof id !== 'string' || !id) return undefined;
    const session = this.options.session;
    return `conversation:${hash([sessionId, session?.sessionIncarnation ?? session?.incarnation ?? '0', id])}`;
  }
  private toolCallKey(callId: string, sessionId: string | null): string {
    const session = this.options.session;
    return `tool-call:${hash([sessionId, session?.sessionIncarnation ?? session?.incarnation ?? '0', callId])}`;
  }
  private save(turn: Turn) {
    this.turns.putMany([turn.turnId, ...turn.requestKeys, ...(turn.continuationKeys ?? [])], turn);
  }
  private continuation(params: any, conversationKey?: string, sessionId: string | null = null): Turn | undefined {
    const scopeId = this.scopeStore.getStore()?.turnId;
    if (scopeId) {
      const turn = this.turns.get<Turn>(scopeId);
      return turn?.state === 'awaiting_continuation' ? turn : undefined;
    }
    const ref = params?.previous_response_id;
    let alias = ref ? this.turns.get<{ turnId: string }>(`response:${ref}`) : undefined;
    if (!alias && conversationKey && Array.isArray(params?.input) && params.input.some((item: any) => item?.type === 'function_call_output')) {
      alias = this.turns.get<{ turnId: string }>(conversationKey);
    }
    if (!alias && Array.isArray(params?.messages)) {
      const refs = params.messages
        .filter((item: any) => item?.role === 'tool' && typeof item.tool_call_id === 'string')
        .map((item: any) => this.turns.get<{ turnId: string }>(this.toolCallKey(item.tool_call_id, sessionId)))
        .filter((item: any) => !!item);
      if (new Set(refs.map((item: any) => item.turnId)).size > 1) throw new RSIRecoveryRequired('Chat tool results refer to different CoSens turns');
      alias = refs[0];
    }
    const turn = alias ? this.turns.get<Turn>(alias.turnId) : undefined;
    const hasToolOutput = (Array.isArray(params?.input) && params.input.at(-1)?.type === 'function_call_output') ||
      (Array.isArray(params?.messages) && params.messages.at(-1)?.role === 'tool');
    if (turn && hasToolOutput) {
      this.requireSession(turn, sessionId);
      if (turn.state !== 'awaiting_continuation') throw new RSIRecoveryRequired(`Turn ${turn.turnId} requires reconciliation before tool continuation`);
    }
    return turn?.state === 'awaiting_continuation' ? turn : undefined;
  }
  private async classify(path: string[], params: any, previous?: Turn): Promise<Decision> {
    if (previous) {
      if (previous.mode !== this.cosens.mode) throw new RSIRecoveryRequired('Turn has a different or unknown originating mode');
      return previous.decision;
    }
    const objective = userText(path, params);
    if (this.cosens.mode === 'disable') return { activate: false, objective, task_family: 'general', reason: 'disabled', capture: 'disabled' };
    const model = this.options.decisionModel ?? params.model;
    if (typeof model !== 'string' || !model.trim()) throw new RSIDecisionUnavailable('Set model on the native request so CoSens can use it for its decision');
    try {
      const decision = await decide(this.native, model, objective, toolNames(params), this.options.decisionTimeoutMs ?? 5000, this.options.decisionMaxOutputTokens ?? 256);
      if (this.cosens.mode === 'approval' && decision.activate) throw new RSIApprovalUnavailable('The configured CoSens service has no verified /v1/invocations approval contract');
      return { ...decision, objective: decision.objective.trim() || objective, capture: decision.activate ? 'active' : 'skipped' };
    } catch (error) {
      if (error instanceof RSIApprovalUnavailable) throw error;
      if (this.cosens.mode === 'approval') throw new RSIDecisionUnavailable('Decision model could not make a required decision');
      return { activate: false, objective, task_family: 'general', reason: 'decision_failed', capture: 'decision_failed' };
    }
  }
  private async sendSession(runId: string, initialSnapshot?: Snapshot): Promise<void> {
    if (!this.options.session) return;
    const snap = initialSnapshot ?? await this.cursor.snapshot(this.options.session);
    let recovery = this.cursor.recoverBatch(snap);
    if (recovery && recovery.runId !== runId) throw new RSIRecoveryRequired(`Session has unsent events for Run ${recovery.runId}`);
    while (true) {
      const events = recovery?.events ?? this.cursor.pending(snap);
      if (!events.length) return;
      this.cursor.beginBatch(runId, snap, events);
      const receipt = await this.client.atoms.sessionEvents(runId, events);
      if (!this.cursor.ack(snap, receipt)) throw new RSIRecoveryRequired('CoSens did not durably acknowledge the first pending event');
      recovery = this.cursor.recoverBatch(snap);
    }
  }
  private async sendObservable(turn: Turn, events: SessionEvent[]): Promise<void> {
    if (!turn.runId) return;
    const acked = new Set(turn.observableAckedIds ?? []);
    const pending = turn.observablePending?.length ? turn.observablePending : events.filter(e => !acked.has(e.id));
    if (!pending.length) return;
    turn.observablePending = pending; this.save(turn);
    const receipt = await this.client.atoms.sessionEvents(turn.runId, pending);
    if (!Array.isArray(receipt?.durable_ack)) throw new RSIRecoveryRequired('Observable model events have no durable ACK list');
    const pendingIds = new Set(pending.map(event => event.id));
    const received = new Set<string>();
    for (const id of receipt.durable_ack) {
      if (typeof id !== 'string' || !pendingIds.has(id) || received.has(id)) {
        throw new RSIRecoveryRequired('Observable model event ACK contains an unsent or duplicate event ID');
      }
      received.add(id);
    }
    for (const id of receipt.durable_ack) acked.add(id);
    turn.observableAckedIds = [...acked]; turn.observablePending = pending.filter(e => !acked.has(e.id)); this.save(turn);
    if (turn.observablePending.length) throw new RSIRecoveryRequired('Observable model events received partial ACK');
  }
  private async visibleEvents(turn: Turn, response: any, includeOutput = true): Promise<SessionEvent[]> {
    const events: SessionEvent[] = [];
    const snapshot = this.options.session ? await this.cursor.snapshot(this.options.session) : undefined;
    const recent = snapshot?.items.slice(turn.sessionBeforeCount ?? 0) ?? [];
    const roles = new Set(recent.map(item => item.role));
    if (turn.visibleInput && !turn.visibleInputInSession && !roles.has('user')) events.push({ id: `visible:${turn.turnId}:input`, kind: 'user_prompt', payload: { session_id: snapshot?.sessionId ?? null, role: 'user', content: turn.visibleInput, source_revision: 'native_request' } });
    if (includeOutput && !roles.has('assistant')) events.push({ id: `visible:${turn.turnId}:output`, kind: 'agent_action', payload: { session_id: snapshot?.sessionId ?? null, role: 'assistant', content: summary(response), source_revision: 'native_response' } });
    return events;
  }
  private toolOutputEvents(turn: Turn, params: any): SessionEvent[] {
    if (Array.isArray(params?.input)) return params.input.flatMap((item: any, index: number) => {
      if (item?.type !== 'function_call_output') return [];
      const callId = String(item.call_id ?? index);
      return [{ id: `visible:${turn.turnId}:tool:${callId}`, kind: 'tool_result', payload: { role: 'tool', content: item.output, call_id: callId, source_revision: 'native_function_call_output' } }];
    });
    if (Array.isArray(params?.messages)) return params.messages.flatMap((item: any) => {
      if (item?.role !== 'tool' || typeof item.tool_call_id !== 'string') return [];
      const callId = item.tool_call_id;
      const alias = this.turns.get<{ turnId: string }>(this.toolCallKey(callId, turn.sessionId ?? null));
      if (alias?.turnId !== turn.turnId) return [];
      return [{ id: `visible:${turn.turnId}:tool:${callId}`, kind: 'tool_result', payload: { role: 'tool', content: item.content, call_id: callId, source_revision: 'native_chat_tool_message' } }];
    });
    return [];
  }
  private functionCallEvents(turn: Turn, response: any): SessionEvent[] {
    if (Array.isArray(response?.output)) return response.output.flatMap((item: any, index: number) => {
      if (item?.type !== 'function_call') return [];
      const callId = String(item.call_id ?? index);
      return [{ id: `visible:${turn.turnId}:call:${callId}`, kind: 'agent_action', payload: { role: 'assistant', content: item, call_id: callId, source_revision: 'native_function_call' } }];
    });
    if (Array.isArray(response?.choices)) return response.choices.flatMap((choice: any) =>
      (choice?.message?.tool_calls ?? []).flatMap((item: any) => {
        if (typeof item?.id !== 'string') return [];
        return [{ id: `visible:${turn.turnId}:call:${item.id}`, kind: 'agent_action', payload: { role: 'assistant', content: item, call_id: item.id, source_revision: 'native_chat_tool_call' } }];
      }));
    return [];
  }
  private async preflight(path: string[], params: any): Promise<{ turn: Turn; updated: any }> {
    const sessionId = await this.sessionId();
    const key = this.requestKey(path, params, sessionId);
    const conversationKey = this.conversationKey(params?.conversation, sessionId);
    const scopedTurnId = this.scopeStore.getStore()?.turnId;
    const previous = this.continuation(params, conversationKey, sessionId) ?? (scopedTurnId ? this.turns.get<Turn>(scopedTurnId) : undefined) ?? this.turns.get<Turn>(key);
    if (previous) this.requireSession(previous, sessionId);
    if (scopedTurnId && previous?.state === 'completed') throw new RSIRecoveryRequired(`Turn ${scopedTurnId} is already completed`);
    if (previous && !['pre_execution_pending', 'completed', 'awaiting_continuation'].includes(previous.state)) throw new RSIRecoveryRequired(`Turn ${previous.turnId} may have executed`);
    const continuing = previous?.state === 'awaiting_continuation' ? previous : undefined;
    const resumable = previous?.state === 'pre_execution_pending' ? previous : undefined;
    const decision = await this.classify(path, params, continuing ?? resumable);
    let turn: Turn = continuing ?? resumable ?? { turnId: this.scopeStore.getStore()?.turnId ?? randomUUID(), mode: this.cosens.mode, state: 'pre_execution_pending', objective: decision.objective, taskFamily: decision.task_family, runId: null, decision, requestKeys: [] };
    if (turn.mode !== this.cosens.mode) throw new RSIRecoveryRequired('Turn mode changed during recovery');
    if (turn.conversationKey && conversationKey && turn.conversationKey !== conversationKey) throw new RSIRecoveryRequired('Turn conversation changed during continuation');
    turn.sessionId = sessionId;
    turn.sessionIncarnation = this.sessionIncarnation();
    if (conversationKey) turn.conversationKey = conversationKey;
    if (!turn.requestKeys.includes(key)) turn.requestKeys.push(key);
    this.cosens.lastTurnId = turn.turnId; this.cosens.rsiCapture = decision.capture ?? null;
    this.save(turn);
    const initialSnapshot = (decision.activate || turn.runId) && this.options.session
      ? await this.cursor.snapshot(this.options.session) : undefined;
    if (!turn.frozenContext) {
      if (decision.activate) {
        if (!turn.runId) {
          turn.prepared = await this.client.atoms.prepareReady({ objective: turn.objective, taskFamily: turn.taskFamily || 'general', key: turn.turnId });
          turn.runId = turn.prepared.run_id; this.save(turn);
        }
        const run = await this.client.atoms.getRun(turn.runId!);
        const { freezeRun } = await import('./context.js');
        turn.frozenContext = freezeRun(turn.prepared, run); this.save(turn);
      } else {
        turn.frozenContext = await this.client.atoms.recallContext({ query: turn.objective, taskFamily: turn.taskFamily || 'general' }); this.save(turn);
      }
    }
    if (turn.runId) await this.sendSession(turn.runId, initialSnapshot);
    turn.visibleInput = userText(path, params);
    if (turn.runId && this.options.session) {
      const before = initialSnapshot ?? await this.cursor.snapshot(this.options.session);
      turn.sessionBeforeCount = before.items.length;
      const latest = before.items.at(-1);
      turn.visibleInputInSession = latest?.role === 'user' && latest.content === turn.visibleInput;
      this.save(turn);
    }
    if (turn.runId) await this.sendObservable(turn, this.toolOutputEvents(turn, params));
    const updated = inject(path, params, turn.frozenContext);
    turn.state = 'native_running'; this.save(turn);
    return { turn, updated };
  }
  private async complete(turn: Turn, response: any): Promise<void> {
    if (!terminal(response)) {
      if (!toolContinuation(response)) { turn.state = 'incomplete'; this.save(turn); return; }
      const continuationKeys = new Set(turn.continuationKeys ?? []);
      if (typeof response?.id === 'string' && response.id) continuationKeys.add(`response:${response.id}`);
      if (Array.isArray(response?.choices)) for (const choice of response.choices) {
        for (const call of choice?.message?.tool_calls ?? []) if (typeof call?.id === 'string') {
          continuationKeys.add(this.toolCallKey(call.id, turn.sessionId ?? null));
        }
      }
      const conversationKey = turn.conversationKey ?? this.conversationKey(response?.conversation, turn.sessionId ?? null);
      if (conversationKey) { turn.conversationKey = conversationKey; continuationKeys.add(conversationKey); }
      turn.continuationKeys = [...continuationKeys];
      turn.toolResponse = response;
      turn.state = 'observability_pending'; this.save(turn);
      if (turn.runId) {
        try {
          await this.sendObservable(turn, [
            ...(await this.visibleEvents(turn, response, false)),
            ...this.functionCallEvents(turn, response),
          ]);
        } catch (error) {
          turn.lastError = String(error); this.save(turn);
          return;
        }
      }
      turn.state = 'awaiting_continuation'; delete turn.lastError; this.save(turn);
      return;
    }
    turn.result = { summary: summary(response), outcome: 'completed' };
    turn.state = 'native_completed'; this.save(turn);
    if (!turn.runId) { turn.state = 'completed'; this.save(turn); return; }
    try {
      await this.sendSession(turn.runId);
      await this.sendObservable(turn, await this.visibleEvents(turn, response));
      const receipt = await this.client.atoms.submit(turn.runId, { data: turn.result });
      if (receipt?.status !== 'completed') throw new RSIRecoveryRequired('CoSens submit did not confirm completion');
      turn.state = 'completed';
    } catch (error) { turn.state = 'completion_pending'; turn.lastError = String(error); }
    this.save(turn);
  }
  private wrapStream(turn: Turn, nativeStream: AsyncIterable<any>, path: string[], release: () => void, callerSignal?: AbortSignal): AsyncIterable<any> {
    const self = this;
    const abort = () => {
      if (turn.state === 'native_running') { turn.state = 'incomplete'; self.save(turn); }
      release();
    };
    const signals = new Set([((nativeStream as any).controller?.signal as AbortSignal | undefined), callerSignal].filter((item): item is AbortSignal => !!item));
    for (const signal of signals) {
      if (signal.aborted) abort();
      else signal.addEventListener('abort', abort, { once: true });
    }
    const iterator = async function* () {
      let completed = false;
      let chatId: string | undefined;
      const choices = new Map<number, { content: string; finishReason: string | null; toolCalls: Map<number, { id?: string; type?: string; function: { name: string; arguments: string } }> }>();
      try {
        for await (const event of nativeStream) {
          if (event?.type === 'response.completed' && event.response && turn.state === 'native_running') { completed = true; await self.complete(turn, event.response); }
          if (path[0] === 'chat' && Array.isArray(event?.choices)) {
            chatId = event.id ?? chatId;
            for (const choice of event.choices) {
              const index = Number(choice.index);
              const previous = choices.get(index) ?? { content: '', finishReason: null, toolCalls: new Map() };
              if (typeof choice.delta?.content === 'string') previous.content += choice.delta.content;
              if (Array.isArray(choice.delta?.tool_calls)) for (const fragment of choice.delta.tool_calls) {
                const callIndex = Number(fragment.index);
                if (!Number.isInteger(callIndex) || callIndex < 0) continue;
                const call = previous.toolCalls.get(callIndex) ?? { function: { name: '', arguments: '' } };
                if (typeof fragment.id === 'string') call.id = fragment.id;
                if (typeof fragment.type === 'string') call.type = fragment.type;
                if (typeof fragment.function?.name === 'string') call.function.name += fragment.function.name;
                if (typeof fragment.function?.arguments === 'string') call.function.arguments += fragment.function.arguments;
                previous.toolCalls.set(callIndex, call);
              }
              if (choice.finish_reason) previous.finishReason = choice.finish_reason;
              choices.set(index, previous);
            }
          }
          yield event;
        }
        if (path[0] === 'chat' && turn.state === 'native_running' && choices.size && [...choices.values()].every(choice => !!choice.finishReason)) {
          const response = { id: chatId, choices: [...choices.entries()].sort(([a], [b]) => a - b).map(([index, choice]) => ({ index, finish_reason: choice.finishReason, message: {
            content: choice.content,
            tool_calls: [...choice.toolCalls.entries()].sort(([a], [b]) => a - b).map(([, call]) => call),
          } })) };
          completed = true;
          await self.complete(turn, response);
        }
      } finally {
        for (const signal of signals) signal.removeEventListener('abort', abort);
        if (!completed && turn.state === 'native_running') { turn.state = 'incomplete'; self.save(turn); }
        release();
      }
    };
    return new Proxy(nativeStream, { get(target, property) {
      if (property === Symbol.asyncIterator) return iterator;
      const member = Reflect.get(target, property, target);
      return typeof member === 'function' ? member.bind(target) : member;
    } });
  }
  private invoke(path: string[], method: Function, params: any, rest: any[]): any {
    if (rest.length > 1 || (path[0] === 'responses' && params?.prompt && !params?.input)) {
      this.cosens.rsiCapture = 'unsupported'; return method(params, ...rest);
    }
    let nativePromise: any;
    let turn: Turn | undefined;
    let started: Promise<void> | undefined;
    let parsed: Promise<any> | undefined;
    let releaseSession: (() => void) | undefined;
    const release = () => { const unlock = releaseSession; releaseSession = undefined; unlock?.(); };
    const capturedScope = this.scopeStore.getStore();
    const callerSignal = rest[0]?.signal as AbortSignal | undefined;
    const start = (signal?: AbortSignal) => started ??= (async () => {
      try {
        if (signal?.aborted || callerSignal?.aborted) throw new DOMException('CoSens request was aborted before preflight', 'AbortError');
        if (this.options.session) releaseSession = await this.cursor.acquireSession(this.options.session);
        if (signal?.aborted || callerSignal?.aborted) throw new DOMException('CoSens request was aborted before preflight', 'AbortError');
        const prepared = await (capturedScope
          ? this.scopeStore.run(capturedScope, () => this.preflight(path, params))
          : this.preflight(path, params));
        turn = prepared.turn;
        if (signal?.aborted || callerSignal?.aborted) {
          turn.state = 'incomplete'; this.save(turn);
          throw new DOMException('CoSens request was aborted before native execution', 'AbortError');
        }
        try { nativePromise = method(prepared.updated, ...rest); }
        catch (error) { turn.state = 'native_failed'; turn.lastError = String(error); this.save(turn); throw error; }
      } catch (error) { release(); throw error; }
    })();
    if (path.at(-1) === 'stream') {
      return new DeferredStream(
        async signal => { await start(signal); return nativePromise; },
        async response => { try { if (turn?.state === 'native_running') await this.complete(turn, response); } finally { release(); } },
        () => { if (turn?.state === 'native_running') { turn.state = 'incomplete'; this.save(turn); } release(); },
        callerSignal,
      );
    }
    const parse = () => parsed ??= (async () => {
      await start();
      try {
        const response = await nativePromise;
        if (response && typeof response[Symbol.asyncIterator] === 'function') return this.wrapStream(turn!, response, path, release, callerSignal);
        if (turn!.state === 'native_running') await this.complete(turn!, response);
        release();
        return response;
      } catch (error) {
        if (turn?.state === 'native_running') { turn.state = 'native_failed'; turn.lastError = String(error); this.save(turn); }
        release();
        throw error;
      }
    })();
    return {
      then: (onFulfilled: any, onRejected: any) => parse().then(onFulfilled, onRejected),
      catch: (onRejected: any) => parse().catch(onRejected),
      finally: (onFinally: any) => parse().finally(onFinally),
      asResponse: async () => {
        await start(); this.cosens.rsiCapture = 'awaiting_parse';
        try {
          const raw = await nativePromise.asResponse();
          if (turn!.state === 'native_running') { turn!.state = 'incomplete'; this.save(turn!); }
          release();
          return raw;
        } catch (error) {
          if (turn?.state === 'native_running') { turn.state = 'native_failed'; turn.lastError = String(error); this.save(turn); }
          release();
          throw error;
        }
      },
      withResponse: async () => {
        await start();
        try { const value = await nativePromise.withResponse(); if (turn!.state === 'native_running') await this.complete(turn!, value.data); return value; }
        catch (error) { if (turn?.state === 'native_running') { turn.state = 'native_failed'; turn.lastError = String(error); this.save(turn); } throw error; }
        finally { release(); }
      },
    };
  }
  private resource(value: any, path: string[] = []): any {
    if (!value || typeof value !== 'object') return value;
    return new Proxy(value, { get: (target, name) => {
      const member = Reflect.get(target, name, target);
      if (typeof name !== 'string') return member;
      const nextPath = [...path, name], signature = nextPath.join('.');
      if (typeof member === 'function') {
        const bound = member.bind(target);
        if (!path.length && (name === 'withOptions' || name === 'copy')) {
          return (...args: any[]) => cosens(bound(...args), this.options);
        }
        if (SUPPORTED.has(signature)) return (params: any, ...rest: any[]) => this.invoke(nextPath, bound, params, rest);
        return (...args: any[]) => {
          this.cosens.rsiCapture = MODEL_PREFIXES.has(nextPath[0]!) && GENERATION_METHODS.has(name) ? 'unsupported' : 'not_applicable';
          return bound(...args);
        };
      }
      return member && typeof member === 'object' && !(member instanceof Promise) ? this.resource(member, nextPath) : member;
    } });
  }
  proxy(): any {
    const root = this.resource(this.native);
    return new Proxy(root, { get: (target, name, receiver) => {
      if (name === 'native') return this.native;
      if (name === 'cosens') return this.cosens;
      if (name === 'close') return () => this.close();
      return Reflect.get(target, name, receiver);
    } });
  }
  async reconcile(id: string): Promise<Turn> {
    const release = this.options.session ? await this.cursor.acquireSession(this.options.session) : () => {};
    try {
    const turn = this.turns.get<Turn>(id);
    if (turn && turn.mode !== this.cosens.mode) throw new RSIRecoveryRequired('Turn has a different or unknown originating mode');
    if (turn) this.requireSession(turn, await this.sessionId());
    if (turn?.state === 'observability_pending') {
      try {
        if (!turn.toolResponse) throw new RSIRecoveryRequired('Tool continuation has no persisted native response');
        if (turn.runId) {
          await this.sendSession(turn.runId);
          await this.sendObservable(turn, [
            ...(await this.visibleEvents(turn, turn.toolResponse, false)),
            ...this.functionCallEvents(turn, turn.toolResponse),
          ]);
        }
        turn.state = 'awaiting_continuation'; delete turn.lastError; this.save(turn); return turn;
      } catch (error) { turn.lastError = String(error); this.save(turn); throw error; }
    }
    if (!turn?.runId || !['native_completed', 'completion_pending'].includes(turn.state)) throw new RSIRecoveryRequired('Turn has no completed native result');
    try {
      await this.sendSession(turn.runId);
      const visible = turn.observablePending?.length ? [] : await this.visibleEvents(turn, { output_text: turn.result?.summary });
      await this.sendObservable(turn, visible);
      const remote = await this.client.atoms.getRun(turn.runId);
      if (remote.status === 'completed') {
        if (canonical(remote.result) !== canonical(turn.result)) throw new RSIRecoveryRequired('Remote completed Run result differs from local result');
      } else if (remote.status === 'ready') {
        const receipt = await this.client.atoms.submit(turn.runId, { data: turn.result });
        if (receipt.status !== 'completed') throw new RSIRecoveryRequired('Recovered submit was not confirmed');
      } else throw new RSIRecoveryRequired(`Run ${turn.runId} is ${remote.status}`);
      turn.state = 'completed'; delete turn.lastError; this.save(turn); return turn;
    } catch (error) { turn.state = 'completion_pending'; turn.lastError = String(error); this.save(turn); throw error; }
    } finally { release(); }
  }
  async close() {
    if (this.closed) return;
    if (--this.owners > 0) return;
    this.closed = true;
    this.onClose?.();
    this.cursor.close(); this.turns.close(); await this.client.close();
  }
}

export function cosens(native: Agent, options?: AgentRunnerOptions): CoSensAgentRunner;
export function cosens(native: Runner | typeof Runner, options?: TaskRunnerOptions): TaskWorkflowRunner;
export function cosens<T extends object>(native: T, options?: CoSensOptions): CoSensProxy<T>;
export function cosens(native: any, options: CoSensOptions = {}): any {
  if (options.mode && !['auto', 'approval', 'disable'].includes(options.mode)) throw new TypeError('mode must be auto, approval, or disable');
  if (native instanceof TaskWorkflowRunner) {
    if (Object.keys(options).length) throw new TypeError('Task workflow Runner is already configured; use its current instance');
    return native;
  }
  if (native instanceof Agent) return new CoSensAgentRunner(native, options as AgentRunnerOptions);
  if (native === Runner || native instanceof Runner) return new TaskWorkflowRunner(native, options as TaskRunnerOptions);
  if (!native || typeof native !== 'object') throw new TypeError('CoSens requires an OpenAI client or Agent');
  const existing = PROXIES.get(native);
  if (existing) {
    const original = existing.config;
    if ((options.apiKey !== undefined && resolveApiKey(options.apiKey) !== original.apiKey) ||
      (options.region !== undefined && resolveService(options.region).identity !== original.serviceId) ||
      (options.mode !== undefined && options.mode !== original.mode) ||
      (options.session !== undefined && options.session !== original.session) ||
      (options.statePath !== undefined && options.statePath !== original.statePath) ||
      (options.fetch !== undefined && options.fetch !== original.fetch) ||
      (options.decisionModel !== undefined && options.decisionModel !== original.decisionModel) ||
      (options.decisionTimeoutMs !== undefined && options.decisionTimeoutMs !== original.decisionTimeoutMs) ||
      (options.decisionMaxOutputTokens !== undefined && options.decisionMaxOutputTokens !== original.decisionMaxOutputTokens)) {
      throw new TypeError('CoSens proxy is already configured differently');
    }
    existing.wrapper.retain();
    return existing.proxy;
  }
  if (native?.cosens && native?.native) throw new TypeError('CoSens proxy is already configured differently');
  let proxy: any;
  const wrapper = new OpenAIWrapper(native, options, () => { PROXIES.delete(native); PROXIES.delete(proxy); });
  proxy = wrapper.proxy();
  const config = { apiKey: resolveApiKey(options.apiKey, resolveService(options.region).region), serviceId: resolveService(options.region).identity, mode: options.mode ?? 'auto', session: options.session, statePath: options.statePath ?? defaultStatePath(), fetch: options.fetch, decisionModel: options.decisionModel, decisionTimeoutMs: options.decisionTimeoutMs, decisionMaxOutputTokens: options.decisionMaxOutputTokens };
  const entry = { wrapper, proxy, config };
  PROXIES.set(native, entry); PROXIES.set(proxy, entry);
  return proxy;
}
