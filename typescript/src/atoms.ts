import { RSIRecoveryRequired } from './errors.js';
import { pathPart, requestKey, Transport, type TransportOptions } from './http.js';
import { performance } from 'node:perf_hooks';
import { startWorkflow, type WorkflowSnapshot, type WorkflowStartOptions } from './workflow-start.js';
import { documentBytes, documentMetadata, documentStatusBody, uploadTarget, type DocumentUploadGrant, type DocumentUploadMetadata } from './document-upload.js';

function modelData(model?:string):Record<string,string>{
 if(model===undefined)return {};
 if(typeof model!=='string'||!model.trim()||model.length>256)throw new TypeError('model must be a public Friday model ID');
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

export interface RecallContext { recipe: any; context: { query: string; task_family: string; source: string }; snapshot: { evidence: any[]; memory_recall: any }; run_id?: never }
export type WorkflowArtifactRef = string | { artifact_id: string; role: 'original_input' | 'submitted_output' | 'acceptance' | 'external_trace' };
export type WorkflowOutputRef = { artifact_id: string; role: 'submitted_output' };
export type WorkflowEvent = { id: string; kind: string; payload: Record<string, unknown> };

function workflowSelection(candidateSha256?: string, candidateId?: string, customReason?: string) {
  if (Boolean(candidateId) === Boolean(customReason)) throw new TypeError('provide exactly one candidateId or customReason');
  if (candidateId !== undefined && (typeof candidateSha256 !== 'string' || !candidateSha256.trim())) throw new TypeError('candidateSha256 is required');
  if (candidateId !== undefined && (typeof candidateId !== 'string' || !candidateId.trim())) throw new TypeError('candidateId must be nonempty');
  if (customReason !== undefined && (!customReason.trim() || customReason.length > 4_000)) throw new TypeError('customReason must be 1–4,000 characters');
  return { data: { ...(candidateSha256 === undefined ? {} : { candidate_sha256: candidateSha256 }),
    ...(candidateId ? { candidate_id: candidateId } : { custom_reason: customReason }) } };
}
export class Atoms {
  listRecipeCandidates({limit=20,cursor,taskId,status}:{limit?:number;cursor?:string;taskId?:string;status?:string}={}) {
    boundedInteger(limit,'limit',100);return this.http.request('GET','/v1/recipe-candidates?'+new URLSearchParams({limit:String(limit),...(cursor?{cursor}:{}),...(taskId?{task_id:taskId}:{}),...(status?{status}:{})}));
  }
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
  async recallContext({query,taskFamily='general',tokenBudget=1000}:{query:string;taskFamily?:string;tokenBudget?:number}):Promise<RecallContext>{
    const page=await this.listRecipeCandidates({status:'candidate',limit:100});
    const candidates=[];for(const item of page.items.filter((x:any)=>x.current).slice(0,20)){candidates.push(await this.getRecipeCandidate(item.recipe_candidate_id));}
    const memory=await this.memoryRecall({query,tokenBudget});
    return {recipe:{status:'not_found',candidates},context:{query,task_family:taskFamily,source:'workflow_recipes'},snapshot:{evidence:memory.items,memory_recall:memory}};
  }
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
  startExecution(taskId: string, { key, model, recipeId, includeMemory = false }: { key: string; model?: string; recipeId?: string; includeMemory?: boolean }) {
    if (typeof includeMemory !== 'boolean') throw new TypeError('includeMemory must be boolean');
    if (recipeId !== undefined) pathPart(recipeId);
    const connection = modelData(model);
    const body = { ...connection,
      ...(recipeId === undefined ? {} : { recipe_id: recipeId }), ...(includeMemory ? { include_memory: true } : {}) };
    return this.http.request('POST', `/v1/tasks/${pathPart(taskId)}/executions`,
      Object.keys(body).length ? body : undefined, requestKey(key), undefined);
  }
  getTaskRecipe(taskId: string) { return this.http.request('GET', `/v1/tasks/${pathPart(taskId)}/recipe`); }
  getRecipeVersions(recipeId: string) { return this.http.request('GET', `/v1/recipe-chains/${pathPart(recipeId)}/versions`); }
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
  startRevision(revisionId:string,{key}:{key:string}) {return this.http.request('POST',`/v1/revisions/${pathPart(revisionId)}/revisions`,{data:{}},requestKey(key));}
  startRecipeCandidate(taskId:string,{revisionId,key,model}:{revisionId:string;key:string;model?:string}) {
    return this.http.request('POST',`/v1/tasks/${pathPart(taskId)}/recipe-candidates`,{data:{revision_id:revisionId,...modelData(model)}},requestKey(key));
  }
  getRecipeCandidate(recipeId:string){return this.http.request('GET',`/v1/recipe-candidates/${pathPart(recipeId)}`);}
  resumeRecipeCandidate(recipeId:string,model?:string){const data=modelData(model);return this.http.request('POST',`/v1/recipe-candidates/${pathPart(recipeId)}/resume`,Object.keys(data).length?{data}:undefined);}

  listTasks({limit=20,cursor}:{limit?:number;cursor?:string}={}) {return this.http.request('GET','/v1/tasks?'+new URLSearchParams({limit:String(limit),...(cursor?{cursor}:{})}));}
  recordFeedback(revisionId:string,{feedback,key}:{feedback:string;key:string}) {return this.http.request('POST',`/v1/revisions/${pathPart(revisionId)}/feedback`,{feedback},requestKey(key));}
  startSession(sessionId?:string) {return this.http.request('POST','/v1/sessions/start',sessionId?{session_id:sessionId}:{});}
  appendSessionEvents(sessionId:string,events:Array<{event_id:string;kind:string;payload:Record<string,unknown>;task_id?:string;revision_id?:string}>) {return this.http.request('POST',`/v1/sessions/${pathPart(sessionId)}/events`,{events});}
  getSessionContext(sessionId:string,after=0) {return this.http.request('GET',`/v1/sessions/${pathPart(sessionId)}/context?after=${after}`);}
  endSession(sessionId:string) {return this.http.request('POST',`/v1/sessions/${pathPart(sessionId)}/end`);}
  async waitWorkflowJob(kind:'recipe_candidate',id:string,{timeout=300}:{timeout?:number}={}) {
    if(kind!=='recipe_candidate')throw new TypeError('job kind must be recipe_candidate');
    const deadline=performance.now()+boundedTimeout(timeout*1000);
    while(true){const value=await this.getRecipeCandidate(id);if(!['queued','running','awaiting_parent'].includes(value.status))return value;
    if(performance.now()>=deadline)throw new Error('Poll or resume the same candidate');await new Promise(r=>setTimeout(r,100));}
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
