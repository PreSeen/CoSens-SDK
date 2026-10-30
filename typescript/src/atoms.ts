import { RSIRecoveryRequired } from './errors.js';
import { pathPart, requestKey, Transport, type TransportOptions } from './http.js';
import { performance } from 'node:perf_hooks';
import { startWorkflow, type WorkflowSnapshot, type WorkflowStartOptions } from './workflow-start.js';
import { documentBytes, documentMetadata, documentStatusBody, uploadTarget, type DocumentUploadGrant, type DocumentUploadMetadata } from './document-upload.js';

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

export interface RecallContext { recipe: any; context: { query: string; task_family: string; source: string }; snapshot: { evidence: any[]; memory_recall: any }; run_id?: never }
export type WorkflowArtifactRef = string | { artifact_id: string; role: 'original_input' | 'submitted_output' | 'acceptance' | 'external_trace' };
export type WorkflowOutputRef = { artifact_id: string; role: 'submitted_output' };
export type WorkflowEvent = { id: string; kind: string; payload: Record<string, unknown> };

function workflowSelection(candidateSha256: string, candidateId?: string, customReason?: string) {
  if (!candidateSha256?.trim()) throw new TypeError('candidateSha256 is required');
  if (Boolean(candidateId) === Boolean(customReason)) throw new TypeError('provide exactly one candidateId or customReason');
  if (customReason !== undefined && (!customReason.trim() || customReason.length > 4_000)) throw new TypeError('customReason must be 1–4,000 characters');
  return { data: { candidate_sha256: candidateSha256, ...(candidateId ? { candidate_id: candidateId } : { custom_reason: customReason }) } };
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
  async recallContext({ query, taskFamily = 'general', tokenBudget = 1000 }: { query: string; taskFamily?: string; tokenBudget?: number }): Promise<RecallContext> {
    const recipe = await this.getActiveRecipe({ taskFamily });
    const memory = await this.memoryRecall({ query, tokenBudget });
    if (!recipe || !['matched', 'not_found'].includes(recipe.status) ||
      (recipe.status === 'matched' && (!recipe.recipe_id || !recipe.bundle || typeof recipe.bundle !== 'object' || Array.isArray(recipe.bundle)))) {
      throw new RSIRecoveryRequired('Standalone Recipe lookup returned an invalid result');
    }
    if (!Array.isArray(memory?.items) || !memory.items.every((x: unknown) => !!x && typeof x === 'object' && !Array.isArray(x))) {
      throw new RSIRecoveryRequired('Standalone Memory recall items are missing or invalid');
    }
    return structuredClone({ recipe, context: { query, task_family: taskFamily, source: 'public_recipe_and_memory' }, snapshot: { evidence: memory.items, memory_recall: memory } });
  }
  getActiveRecipe({ taskFamily = 'general' }: { taskFamily?: string } = {}) { return this.http.request('GET', `/v1/recipes/active?${new URLSearchParams({ task_family: taskFamily })}`); }
  async memoryRecall({ query, tokenBudget = 1000 }: { query: string; tokenBudget?: number }) { return this.http.request('POST', '/v1/memory/recall', { query, token_budget: boundedInteger(tokenBudget, 'tokenBudget', 8000) }); }
  memoryIngest({ content, requestKey: key, sourceKind = 'decision', durable = true, sourceId, expectedRevision }:
    { content: string; requestKey: string; sourceKind?: string; durable?: boolean; sourceId?: string; expectedRevision?: number }) {
    if (sourceId !== undefined && (!sourceId || !sourceId.trim())) throw new TypeError('sourceId must be a nonempty string');
    return this.http.request('POST', '/v1/memory/ingest', { content, request_key: requestKey(key), source_kind: sourceKind, durable,
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
    return this.http.request('POST', '/v1/artifacts', { name, content });
  }
  getArtifact(id: string) { return this.http.request('GET', `/v1/artifacts/${pathPart(id)}`); }

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
    return this.http.request('POST', '/v1/tasks', { source, artifacts, ...(objective === undefined ? {} : { objective }), ...(selfContained === undefined ? {} : { self_contained: selfContained }) }, requestKey(key));
  }
  getTask(taskId: string) { return this.http.request('GET', `/v1/tasks/${pathPart(taskId)}`); }
  supplementTaskContext(taskId: string, { expectedContextVersion, objective, artifacts, selfContained }: { expectedContextVersion: number; objective?: string; artifacts?: WorkflowArtifactRef[]; selfContained?: boolean }) {
    if (!Number.isInteger(expectedContextVersion) || expectedContextVersion < 1) throw new TypeError('expectedContextVersion must be a positive integer');
    return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/context`, { expected_context_version: expectedContextVersion, ...(objective === undefined ? {} : { objective }), ...(artifacts === undefined ? {} : { artifacts }), ...(selfContained === undefined ? {} : { self_contained: selfContained }) });
  }
  startExternalAnalysis(taskId: string, { key }: { key: string }) { return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/analysis`, undefined, requestKey(key)); }
  getExternalAnalysis(analysisId: string) { return this.http.request('GET', `/v1/task-analyses/${pathPart(analysisId)}`); }
  resumeExternalAnalysis(analysisId: string) { return this.http.request('POST', `/v1/task-analyses/${pathPart(analysisId)}/resume`); }
  startExecution(taskId: string, { key }: { key: string }) { return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/executions`, undefined, requestKey(key)); }
  startReconstruction(taskId: string, { key }: { key: string }) { return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/reconstructions`, undefined, requestKey(key)); }
  getExecution(executionId: string) { return this.http.request('GET', `/v1/executions/${pathPart(executionId)}`); }
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
    return this.http.request('POST', `/v1/executions/${pathPart(executionId)}/result`, body);
  }
  getRevision(revisionId: string) { return this.http.request('GET', `/v1/revisions/${pathPart(revisionId)}`); }
  startAttribution(revisionId: string, { key, feedback }: { key: string; feedback?: string }) {
    if (feedback !== undefined && (typeof feedback !== 'string' || !feedback.trim() || feedback.length > 4_000)) throw new TypeError('feedback must be 1–4,000 characters');
    return this.http.request('POST', `/v1/revisions/${pathPart(revisionId)}/attributions`, { data: feedback === undefined ? {} : { feedback } }, requestKey(key));
  }
  getAttribution(attributionId: string) { return this.http.request('GET', `/v1/attributions/${pathPart(attributionId)}`); }
  resumeAttribution(attributionId: string) { return this.http.request('POST', `/v1/attributions/${pathPart(attributionId)}/resume`); }
  selectCause(attributionId: string, { candidateSha256, candidateId, customReason }: { candidateSha256: string; candidateId?: string; customReason?: string }) {
    return this.http.request('POST', `/v1/attributions/${pathPart(attributionId)}/selection`, workflowSelection(candidateSha256, candidateId, customReason));
  }
  startRevision(revisionId: string, { attributionId, key }: { attributionId: string; key: string }) {
    return this.http.request('POST', `/v1/revisions/${pathPart(revisionId)}/revisions`, { data: { attribution_id: attributionId } }, requestKey(key));
  }
  acceptRevision(revisionId: string, { artifactHashes }: { artifactHashes: Record<string, string> }) {
    return this.http.request('POST', `/v1/revisions/${pathPart(revisionId)}/acceptance`, { data: { artifact_hashes: artifactHashes } });
  }
  startRecipeCandidate(taskId: string, { revisionId, key, includeMemory = false }: { revisionId: string; key: string; includeMemory?: boolean }) {
    return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/recipe-candidates`, { data: { revision_id: revisionId, include_memory: includeMemory } }, requestKey(key));
  }
  getRecipeCandidate(recipeId: string) { return this.http.request('GET', `/v1/recipe-candidates/${pathPart(recipeId)}`); }
  resumeRecipeCandidate(recipeId: string) { return this.http.request('POST', `/v1/recipe-candidates/${pathPart(recipeId)}/resume`); }
  startVerification(recipeId: string, { key }: { key: string }) { return this.http.request('POST', `/v1/recipe-candidates/${pathPart(recipeId)}/verifications`, undefined, requestKey(key)); }
  getVerification(verificationId: string) { return this.http.request('GET', `/v1/recipe-verifications/${pathPart(verificationId)}`); }
  async waitWorkflowJob(kind: 'analysis' | 'attribution' | 'recipe_candidate', id: string, { timeout = 300 }: { timeout?: number } = {}) {
    const allowed: Record<string, readonly string[]> = {
      analysis: ['queued', 'running', 'awaiting_runner', 'completed', 'failed'],
      attribution: ['queued', 'running', 'awaiting_runner', 'awaiting_selection', 'inconclusive', 'selected', 'failed'],
      recipe_candidate: ['queued', 'running', 'awaiting_runner', 'candidate', 'failed'],
    };
    if (!(kind in allowed)) throw new TypeError('job kind must be analysis, attribution or recipe_candidate');
    const deadline = performance.now() + boundedTimeout(boundedTimeout(timeout) * 1000);
    let delay = 2_000;
    while (true) {
      const job = kind === 'analysis' ? await this.getExternalAnalysis(id) : kind === 'attribution' ? await this.getAttribution(id) : await this.getRecipeCandidate(id);
      if (!allowed[kind]!.includes(job?.status)) throw new RSIRecoveryRequired(`Unexpected workflow ${kind} status: ${job?.status}`);
      if (!['queued', 'running'].includes(job.status)) return job;
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error(`Workflow ${kind} ${id} is still ${job.status}; resume polling the same ID`);
      await new Promise(resolve => setTimeout(resolve, Math.min(delay, remaining)));
      delay = Math.min(delay * 2, 30_000);
    }
  }
  prepare({ objective, key, taskFamily = 'general', inputArtifactIds = [], executionMode, ...options }: { objective: string; key: string; taskFamily?: string; inputArtifactIds?: string[]; executionMode?: 'assist' | 'guided'; [key: string]: unknown }) {
    const mode = executionMode ?? options.execution_mode ?? 'assist';
    if (mode !== 'assist' && mode !== 'guided') throw new TypeError('executionMode must be assist or guided');
    if (executionMode !== undefined && options.execution_mode !== undefined) throw new TypeError('set only executionMode');
    return this.http.request('POST', '/v1/runs/prepare', { objective, task_family: taskFamily, input_artifact_ids: inputArtifactIds,
      ...options, ...(executionMode === undefined ? {} : { execution_mode: executionMode }) }, requestKey(key));
  }
  async prepareReady(options: Parameters<Atoms['prepare']>[0], timeoutMs = 300_000) {
    const deadline = performance.now() + boundedTimeout(timeoutMs);
    while (true) {
      const prepared = await this.prepare(options);
      if (prepared?.status === 'ready' && prepared.run_id) return prepared;
      if (prepared?.status !== 'processing') throw new RSIRecoveryRequired(`Unexpected CoSens prepare status: ${prepared?.status}`);
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new RSIRecoveryRequired('CoSens prepare did not become ready before timeout');
      const retryAfter = prepared.retry_after_seconds;
      const delay = retryAfter == null || retryAfter === '' || !Number.isFinite(Number(retryAfter)) ? 2 : Number(retryAfter);
      await new Promise(resolve => setTimeout(resolve, Math.min(Math.max(delay, 0) * 1000, 30_000, remaining)));
    }
  }
  getRun(runId: string) { return this.http.request('GET', `/v1/runs/${pathPart(runId)}`); }
  getRecipeStep(runId: string, nodeId: string) { return this.http.request('GET', `/v1/runs/${pathPart(runId)}/recipe/steps/${pathPart(nodeId)}`); }
  sessionEvents(runId: string, events: any[]) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/session-events`, { events }); }
  initialInputs(runId: string, artifactIds: string[]) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/initial-inputs`, { artifact_ids: artifactIds }); }
  guidance(runId: string, { data }: { data: unknown }) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/guidance`, { data }); }
  checkpoint(runId: string, { data }: { data: unknown }) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/checkpoint`, { data }); }
  getDecision(runId: string, decisionId: string) { return this.http.request('GET', `/v1/runs/${pathPart(runId)}/decisions/${pathPart(decisionId)}`); }
  getTrace(runId: string) { return this.http.request('GET', `/v1/runs/${pathPart(runId)}/trace`); }
  nextStep(runId: string) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/steps/next`); }
  submitStep(runId: string, attemptId: string, { attemptToken, outputs, decisionId, actualActionId }:
    { attemptToken: string; outputs: Record<string, unknown>; decisionId?: string; actualActionId?: string }) {
    if (typeof attemptToken !== 'string' || !attemptToken) throw new TypeError('attemptToken is required');
    if (!outputs || typeof outputs !== 'object' || Array.isArray(outputs)) throw new TypeError('outputs must be an object');
    return this.http.request('POST', `/v1/runs/${pathPart(runId)}/steps/${pathPart(attemptId)}/submit`,
      { attempt_token: attemptToken, outputs, ...(decisionId === undefined ? {} : { decision_id: decisionId }),
        ...(actualActionId === undefined ? {} : { actual_action_id: actualActionId }) });
  }
  completeGuided(runId: string) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/complete`); }
  getEvents(runId: string) { return this.http.request('GET', `/v1/runs/${pathPart(runId)}/events`); }
  submit(runId: string, { data = {}, key }: { data?: unknown; key?: string } = {}) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/submit`, { data }, key === undefined ? undefined : requestKey(key)); }
  feedback(runId: string, { data, key }: { data: unknown; key?: string }) { return this.http.request('POST', `/v1/runs/${pathPart(runId)}/feedback`, { data }, key === undefined ? undefined : requestKey(key)); }
  improve(runId: string, { reason, key }: { reason: string; key?: string }) {
    if (!reason?.trim()) throw new TypeError('reason is required');
    return this.http.request('POST', `/v1/runs/${pathPart(runId)}/improvements`, { data: { reason } }, key === undefined ? undefined : requestKey(key));
  }
  getImprovement(jobId: string) { return this.http.request('GET', `/v1/improvements/${pathPart(jobId)}`); }
  resumeImprovement(jobId: string) { return this.http.request('POST', `/v1/improvements/${pathPart(jobId)}/resume`); }
  async waitImprovement(jobId: string, { timeout = 300 }: { timeout?: number } = {}) {
    const deadline = performance.now() + boundedTimeout(boundedTimeout(timeout) * 1000);
    let delay = 2_000;
    while (true) {
      const job = await this.getImprovement(jobId);
      if (!['queued', 'running'].includes(job.status)) return job;
      const remaining = deadline - performance.now();
      if (remaining <= 0) throw new Error(`improvement ${jobId} did not finish within ${timeout} seconds`);
      await new Promise(resolve => setTimeout(resolve, Math.min(delay, remaining)));
      delay = Math.min(delay * 2, 30_000);
    }
  }
}

export class AdminAtoms {
  constructor(private readonly http: Transport) {}
  bootstrapBundle({ taskFamily, bundle, reason }: { taskFamily: string; bundle: unknown; reason: string }) { return this.http.request('POST', '/internal/admin/v1/recipes/bootstrap-bundle', { task_family: taskFamily, bundle, reason }); }
  evaluateDecision(runId: string, decisionId: string, { evaluation }: { evaluation: unknown }) { return this.http.request('POST', `/internal/admin/v1/runs/${pathPart(runId)}/decisions/${pathPart(decisionId)}/evaluations`, evaluation); }
  evaluateRecipe(runId: string, { record, artifactId, contentHash }: { record: unknown; artifactId: string; contentHash: string }) { return this.http.request('POST', `/internal/admin/v1/runs/${pathPart(runId)}/recipe-evaluation`, { record, artifact_id: artifactId, content_hash: contentHash }); }
  publish(jobId: string, { expectedRevision, reason }: { expectedRevision: number; reason: string }) {
    if (!reason?.trim()) throw new TypeError('reason is required');
    return this.http.request('POST', `/internal/admin/v1/improvements/${pathPart(jobId)}/publish`, { expected_revision: expectedRevision, reason });
  }
}

export class CoSensClient {
  readonly transport: Transport;
  readonly atoms: Atoms;
  readonly admin: { atoms: AdminAtoms };
  constructor(options: TransportOptions & { statePath?: string } = {}) {
    this.transport = new Transport(options);
    this.atoms = new Atoms(this.transport);
    this.admin = { atoms: new AdminAtoms(this.transport) };
  }
  start(snapshot: WorkflowSnapshot, options: WorkflowStartOptions = {}) { return startWorkflow(this.atoms, snapshot, options); }
  async close() { /* fetch has no owned socket lifecycle */ }
}
