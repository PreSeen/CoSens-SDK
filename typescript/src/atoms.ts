import { RSILearningFailedError, RSILearningStoppedError, RSIRecoveryRequired } from './errors.js';
import { pathPart, stableKey, Transport, type TransportOptions } from './http.js';
import { performance } from 'node:perf_hooks';
import { startWorkflow, type WorkflowSnapshot, type WorkflowStartOptions } from './workflow-start.js';
import { documentBytes, documentMetadata, documentStatusBody, uploadTarget, type DocumentUploadGrant, type DocumentUploadMetadata } from './document-upload.js';

function modelData(model?:string):Record<string,string>{
 if(model===undefined)return {};
 if(typeof model!=='string'||!model.trim()||model.length>256)throw new TypeError('model must be a public model ID');
 return {model:model.trim()};
}

function boundedTimeout(milliseconds: number): number {
  if (typeof milliseconds !== 'number' || !Number.isFinite(milliseconds) || milliseconds < 0 || milliseconds > Number.MAX_SAFE_INTEGER) {
    throw new TypeError('timeout must be a finite nonnegative number');
  }
  return milliseconds;
}

function boundedInteger(value: number, name: string, maximum: number, minimum = 1): number {
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) throw new TypeError(`${name} must be an integer from ${minimum} to ${maximum}`);
  return value;
}

export type WorkflowArtifactRef = string | { artifact_id: string; role: 'original_input' | 'submitted_output' | 'acceptance' | 'external_trace' };
export type WorkflowOutputRef = { artifact_id: string; role: 'submitted_output' };
export type WorkflowEvent = { id: string; kind: string; payload: Record<string, unknown> };
/** Where a feedback text comes from; the service weighs feedback by its source. */
export const FEEDBACK_SOURCES = ['human', 'evaluator', 'reference', 'automated_check', 'downstream'] as const;
export type FeedbackSource = typeof FEEDBACK_SOURCES[number];
function feedbackSource(source: FeedbackSource) {
  if (!(FEEDBACK_SOURCES as readonly string[]).includes(source)) throw new TypeError(`source must be one of ${FEEDBACK_SOURCES.join(', ')}`);
  return source;
}

function learningData(model?: string, maxCompletionTokens?: number): Record<string, unknown> {
  return { ...modelData(model), ...(maxCompletionTokens === undefined ? {} : { max_completion_tokens: boundedInteger(maxCompletionTokens, 'maxCompletionTokens', 1_000_000) }) };
}
const RECIPE_ID = /^[A-Za-z0-9_-]{1,128}$/;
/** Learning states the service moves on by itself, and states only the caller can change. */
const LEARNING_IN_FLIGHT = new Set(['queued', 'running', 'awaiting_previous_version']);
const LEARNING_STOPPED = new Set(['interrupted', 'awaiting_runner', 'awaiting_material']);
/** Time source for waitLearning; tests replace it. */
export const clock = { now: () => performance.now(), sleep: (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)) };

/** The learning when waiting is over, undefined while it is in flight; throws on failure, a stop or an unknown state. */
function waitedLearning(execution: any, executionId: string): any {
  const learning = execution?.learning;
  if (!learning || typeof learning !== 'object') throw new RSIRecoveryRequired(`Execution ${executionId} has no learning; call learn first`);
  const status = learning.status;
  if (status === 'completed') return learning;
  if (status === 'failed') throw RSILearningFailedError.fromLearning(learning, execution.requestId);
  if (LEARNING_STOPPED.has(status)) throw new RSILearningStoppedError(learning, execution.requestId);
  if (!LEARNING_IN_FLIGHT.has(status)) throw new RSIRecoveryRequired(`Unexpected learning status: ${status}`);
  return undefined;
}

export class Atoms {
  constructor(private readonly http: Transport) {}
  createDocumentUploadGrant(metadata: DocumentUploadMetadata) {
    return this.http.request('POST', '/v1/document/upload-grants', documentMetadata(metadata));
  }
  async uploadDocumentToGrant(grant: DocumentUploadGrant, content: Uint8Array) {
    const { path, token, bytes } = uploadTarget(grant, content, this.http.service.url);
    return this.http.requestBytes('PUT', path, bytes, [token]);
  }
  async uploadDocument({ content, ...metadata }: DocumentUploadMetadata & { content: Uint8Array }) {
    const body = documentMetadata(metadata);
    const bytes = documentBytes(content);
    const { filename, mime_type, ...fields } = body;
    return this.http.requestMultipart('POST', '/v1/document/upload', String(filename), String(mime_type), bytes, fields);
  }
  getDocumentStatus(documentIds: string[]) {
    return this.http.request('POST', '/v1/document/status', documentStatusBody(documentIds));
  }
  async memoryRecall({ query, tokenBudget = 1000 }: { query: string; tokenBudget?: number }) { return this.http.request('POST', '/v1/memory/recall', { query, token_budget: boundedInteger(tokenBudget, 'tokenBudget', 8000) }); }
  memoryIngest({ content, requestKey: key, sourceKind = 'decision', durable = true, sourceId, expectedRevision }:
    { content: string; requestKey: string; sourceKind?: string; durable?: boolean; sourceId?: string; expectedRevision?: number }) {
    if (sourceId !== undefined && (!sourceId || !sourceId.trim())) throw new TypeError('sourceId must be a nonempty string');
    return this.http.request('POST', '/v1/memory/ingest', { content, request_key: stableKey(key), source_kind: sourceKind, durable,
      ...(sourceId === undefined ? {} : { source_id: sourceId }),
      ...(expectedRevision === undefined ? {} : { expected_revision: boundedInteger(expectedRevision, 'expectedRevision', Number.MAX_SAFE_INTEGER, 0) }) });
  }
  async getMemoryItems({ limit = 200 }: { limit?: number } = {}) { return this.http.request('GET', `/v1/memory/items?limit=${boundedInteger(limit, 'limit', 500)}`); }
  getMemoryJob(jobId: string) { return this.http.request('GET', `/v1/memory/jobs/${pathPart(jobId)}`); }
  getMemoryExperience(id: string, options: { revision?: number } = {}) { return this.http.request('GET', `/v1/memory/experiences/${pathPart(id)}${options.revision === undefined ? '' : `?${new URLSearchParams({ revision: String(options.revision) })}`}`); }
  getMemory() { return this.http.request('GET', '/v1/memory'); }
  artifact({ name, content }: { name: string; content: string }) {
    if (typeof name !== 'string' || !name.trim()) throw new TypeError('artifact name is required');
    if (typeof content !== 'string' || !content || Buffer.byteLength(content, 'utf8') > 1_000_000) throw new TypeError('artifact content must be nonempty UTF-8 text of at most 1,000,000 bytes');
    return this.http.request('POST', '/artifacts', { name, content });
  }
  getArtifact(id: string) { return this.http.request('GET', `/artifacts/${pathPart(id)}`); }

  // Task workflow atoms; each call maps to one HTTP request and never runs an Agent.
  createTask({ source, key, objective, artifacts = [], selfContained }: { source: 'new_task' | 'external_artifact'; key: string; objective?: string; artifacts?: WorkflowArtifactRef[]; selfContained?: boolean }) {
    if (!['new_task', 'external_artifact'].includes(source)) throw new TypeError('source must be new_task or external_artifact');
    if (objective !== undefined && (typeof objective !== 'string' || !objective.trim() || objective.length > 20_000)) throw new TypeError('objective must be 1–20,000 characters');
    if (!Array.isArray(artifacts) || artifacts.length > 100) throw new TypeError('artifacts must contain at most 100 references');
    const roles = artifacts.map(a => typeof a === 'string' ? (a ? source === 'new_task' ? 'original_input' : 'submitted_output' : undefined) :
      a && typeof a.artifact_id === 'string' && a.artifact_id && ['original_input', 'submitted_output', 'acceptance', 'external_trace'].includes(a.role) ? a.role : undefined);
    if (roles.includes(undefined)) throw new TypeError('invalid artifact reference or role');
    if (source === 'external_artifact' && !roles.includes('submitted_output')) throw new TypeError('external_artifact requires submitted_output');
    const rolesById = new Map<string, Set<string>>();
    artifacts.forEach((artifact, index) => {
      const id = typeof artifact === 'string' ? artifact : artifact.artifact_id;
      const itemRoles = rolesById.get(id) ?? new Set<string>();
      itemRoles.add(roles[index]!); rolesById.set(id, itemRoles);
    });
    if ([...rolesById.values()].some(itemRoles => itemRoles.has('original_input') && itemRoles.has('submitted_output'))) throw new TypeError('one artifact cannot be both original_input and submitted_output');
    if (selfContained !== undefined && typeof selfContained !== 'boolean') throw new TypeError('selfContained must be a boolean');
    return this.http.request('POST', '/tasks', { source, artifacts, ...(objective === undefined ? {} : { objective }), ...(selfContained === undefined ? {} : { self_contained: selfContained }) }, stableKey(key));
  }
  getTask(taskId: string) { return this.http.request('GET', `/tasks/${pathPart(taskId)}`); }
  supplementTaskContext(taskId: string, { expectedContextVersion, objective, artifacts, selfContained }: { expectedContextVersion: number; objective?: string; artifacts?: WorkflowArtifactRef[]; selfContained?: boolean }) {
    if (!Number.isInteger(expectedContextVersion) || expectedContextVersion < 1) throw new TypeError('expectedContextVersion must be a positive integer');
    return this.http.request('POST', `/tasks/${pathPart(taskId)}/context`, { expected_context_version: expectedContextVersion, ...(objective === undefined ? {} : { objective }), ...(artifacts === undefined ? {} : { artifacts }), ...(selfContained === undefined ? {} : { self_contained: selfContained }) });
  }
  /** Start an execution; with `previousExecutionId` it is a correction of that execution and takes only `model`. */
  startExecution(taskId: string, { key, model, recipeId, recallMemory = false, previousExecutionId }: { key: string; model?: string; recipeId?: string; recallMemory?: boolean; previousExecutionId?: string }) {
    if (typeof recallMemory !== 'boolean') throw new TypeError('recallMemory must be boolean');
    if (previousExecutionId !== undefined) {
      if (recipeId !== undefined || recallMemory) throw new TypeError('a correction takes only previousExecutionId and model');
      pathPart(previousExecutionId);
    }
    if (recipeId !== undefined) pathPart(recipeId);
    const body = { ...modelData(model), ...(previousExecutionId === undefined ? {} : { previous_execution_id: previousExecutionId }),
      ...(recipeId === undefined ? {} : { recipe_id: recipeId }), ...(recallMemory ? { recall_memory: true } : {}) };
    return this.http.request('POST', `/tasks/${pathPart(taskId)}/executions`,
      Object.keys(body).length ? body : undefined, stableKey(key), undefined);
  }
  getRecipeVersions(recipeId: string) { return this.http.request('GET', `/recipes/${pathPart(recipeId)}/versions`); }
  getExecution(executionId: string) { return this.http.request('GET', `/executions/${pathPart(executionId)}`); }
  submitExecution(executionId: string, { requestSha256, outcome, artifacts, events = [], traceCoverage = 'unknown' }: { requestSha256: string; outcome: 'completed' | 'failed' | 'cancelled'; artifacts: WorkflowOutputRef[]; events?: WorkflowEvent[]; traceCoverage?: 'full' | 'partial' | 'unknown' }) {
    if (!requestSha256?.trim()) throw new TypeError('requestSha256 is required');
    if (!['completed', 'failed', 'cancelled'].includes(outcome)) throw new TypeError('invalid execution outcome');
    if (!['full', 'partial', 'unknown'].includes(traceCoverage)) throw new TypeError('invalid traceCoverage');
    if (!Array.isArray(artifacts) || !Array.isArray(events) || events.length > 200) throw new TypeError('invalid execution artifacts or events');
    if (artifacts.some(a => !a || typeof a.artifact_id !== 'string' || !a.artifact_id || a.role !== 'submitted_output')) throw new TypeError('execution artifacts must be submitted_output references');
    const ids = events.map(e => e?.id);
    if (ids.some(id => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length || events.some(e => !e || typeof e.kind !== 'string' || !e.kind || typeof e.payload !== 'object' || !e.payload || Array.isArray(e.payload))) throw new TypeError('event IDs must be unique, kind nonempty and payloads must be objects');
    if (outcome === 'completed' && !artifacts?.some(a => a.role === 'submitted_output')) throw new TypeError('completed execution requires a submitted_output artifact');
    const body = { request_sha256: requestSha256, outcome, artifacts, events, trace_coverage: traceCoverage };
    if (Buffer.byteLength(JSON.stringify(body), 'utf8') > 1_000_000) throw new TypeError('execution result JSON must be at most 1,000,000 bytes');
    return this.http.request('POST', `/executions/${pathPart(executionId)}/result`, body);
  }
  recordFeedback(executionId: string, { feedback, source, key }: { feedback: string; source: FeedbackSource; key: string }) {
    return this.http.request('POST', `/executions/${pathPart(executionId)}/feedback`, { feedback, source: feedbackSource(source) }, stableKey(key));
  }
  /** Learn a Recipe version from a confirmed execution; repeat calls return the same learning. */
  learn(executionId: string, { model, maxCompletionTokens }: { model?: string; maxCompletionTokens?: number } = {}) {
    return this.http.request('POST', `/executions/${pathPart(executionId)}/learn`, learningData(model, maxCompletionTokens));
  }
  /**
   * Retry a `failed`, `interrupted` or `awaiting_runner` learning, optionally with a new budget.
   * A learning that is active or `completed` comes back unchanged; a different `maxCompletionTokens`
   * then gets a 422. An execution that has not learned yet, or whose learning is `awaiting_material`, also gets a 422.
   */
  resumeLearning(executionId: string, { model, maxCompletionTokens }: { model?: string; maxCompletionTokens?: number } = {}) {
    const data = learningData(model, maxCompletionTokens);
    return this.http.request('POST', `/executions/${pathPart(executionId)}/resume`, Object.keys(data).length ? data : undefined);
  }
  /**
   * Poll the execution (2 s, doubling to 30 s) until its learning is `completed` and return the learning.
   * Throws `RSILearningFailedError` on `failed`, `RSILearningStoppedError` on `interrupted`, `awaiting_runner`
   * or `awaiting_material`, and a `TimeoutError` while it is still in flight. `timeout` is in seconds.
   */
  async waitLearning(executionId: string, { timeout = 300 }: { timeout?: number } = {}) {
    const deadline = clock.now() + boundedTimeout(timeout * 1000);
    let delay = 2000;
    while (true) {
      const execution = await this.getExecution(executionId);
      const learning = waitedLearning(execution, executionId);
      if (learning !== undefined) return learning;
      const remaining = deadline - clock.now();
      if (remaining <= 0) {
        throw Object.assign(new Error(`Learning for execution ${executionId} is still ${execution.learning.status}; call waitLearning again`), { name: 'TimeoutError' });
      }
      await clock.sleep(Math.min(delay, remaining));
      delay = Math.min(delay * 2, 30_000);
    }
  }
  listTasks({limit=20,cursor}:{limit?:number;cursor?:string}={}) {return this.http.request('GET','/tasks?'+new URLSearchParams({limit:String(limit),...(cursor?{cursor}:{})}));}

  /** Store a Recipe version learned outside the service; `previousVersion` is the version it was built on. */
  uploadRecipe(recipeId: string, { version, recipe, previousVersion }: { version: number; recipe: Record<string, unknown>; previousVersion?: number }) {
    if (typeof recipeId !== 'string' || !RECIPE_ID.test(recipeId)) throw new TypeError("recipeId must be 1-128 letters, digits, '_' or '-'");
    if (!recipe || typeof recipe !== 'object' || Array.isArray(recipe)) throw new TypeError('recipe must be an object');
    boundedInteger(version, 'version', Number.MAX_SAFE_INTEGER);
    const body = { ...(previousVersion === undefined ? {} : { previous_version: boundedInteger(previousVersion, 'previousVersion', Number.MAX_SAFE_INTEGER) }), recipe };
    return this.http.request('PUT', `/recipes/${pathPart(recipeId)}/versions/${version}`, body);
  }
  getRecipe(recipeId: string, { version }: { version?: number } = {}) {
    const query = version === undefined ? '' : '?' + new URLSearchParams({ version: String(boundedInteger(version, 'version', Number.MAX_SAFE_INTEGER)) });
    return this.http.request('GET', `/recipes/${pathPart(recipeId)}${query}`);
  }
  listRecipes({ limit = 20, cursor, origin }: { limit?: number; cursor?: string; origin?: 'uploaded' | 'generated' } = {}) {
    boundedInteger(limit, 'limit', 100);
    if (cursor !== undefined && (typeof cursor !== 'string' || !cursor.trim())) throw new TypeError('cursor must be a nonempty string');
    if (origin !== undefined && !['uploaded', 'generated'].includes(origin)) throw new TypeError('origin must be uploaded or generated');
    return this.http.request('GET', '/recipes?' + new URLSearchParams({ limit: String(limit), ...(cursor === undefined ? {} : { cursor }), ...(origin === undefined ? {} : { origin }) }));
  }
  searchRecipes(query: string, { limit = 10 }: { limit?: number } = {}) {
    if (typeof query !== 'string' || query.length < 1 || query.length > 1000) throw new TypeError('query must be 1-1000 characters');
    boundedInteger(limit, 'limit', 50);
    return this.http.request('GET', '/recipes?' + new URLSearchParams({ q: query, limit: String(limit) }));
  }
}
export class CoSensClient {
  readonly transport: Transport;
  readonly atoms: Atoms;
  constructor(options: TransportOptions & { statePath?: string } = {}) {
    this.transport = new Transport(options);
    this.atoms = new Atoms(this.transport);
  }
  start(snapshot: WorkflowSnapshot | string, options: WorkflowStartOptions = {}) { return startWorkflow(this.atoms, snapshot, options); }
  async close() { /* fetch has no owned socket lifecycle */ }
}
