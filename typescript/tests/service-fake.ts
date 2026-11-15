/**
 * The execution API as cosens-rsi serves it, for tests: a task is created once per key, each Recipe belongs to one
 * task, fresh execution IDs per key, one learning per execution.
 */
import { canonical } from '../src/state.ts';

export const DEFAULT_BUDGET = 32_000; // the service's DEFAULT_RECIPE_MAX_COMPLETION_TOKENS

export function learningView(executionId: string, status = 'queued', fields: Record<string, unknown> = {}) {
  return { execution_id: executionId, recipe_id: 'recipe_1', version: 1, previous_version: null, current: status === 'completed',
    status, phase: status, attribution: null, recipe: status === 'completed' ? { steps: [] } : null, recipe_sha256: null,
    error: null, error_detail: null, models: null, memory_context: 'captured', initial: true, material_gap: null,
    max_completion_tokens: DEFAULT_BUDGET, ...fields } as Record<string, any>;
}

const ACTIVE = new Set(['queued', 'running', 'awaiting_previous_version', 'completed']);
const RESUMABLE = new Set(['failed', 'interrupted', 'awaiting_runner']);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const invalid = (message: string) => json({ error: { code: 'invalid_request', message } }, 422);
const notFound = (message: string) => json({ error: { code: 'resource_not_found', message } }, 404);

export interface RecordedRequest { method: string; path: string; search: URLSearchParams; body: any; headers: Headers }
interface Execution { taskId: string; kind: string; status: string; previous: string | null; requestSha256: string; input: any; instanceId: string; method: any }
interface Task { task_id: string; objective: string; artifacts: Array<{ artifact_id: string; role: string }>; context_version: number }

export class ServiceFake {
  readonly requests: RecordedRequest[] = [];
  readonly learnings = new Map<string, Record<string, any>>();
  readonly executions = new Map<string, Execution>();
  readonly tasks = new Map<string, Task>();
  private readonly taskKeys = new Map<string, { body: string; taskId: string }>();
  private readonly keys = new Map<string, { intent?: string; receipt: any }>();
  private readonly feedbackKeys = new Map<string, any>();
  private readonly taskModels = new Map<string, string>();
  generations = 0;
  private artifacts = 0;
  /**
   * Recipe version views ({recipe_id, origin, version, task_id, recipe, ...}) served by GET /recipes and
   * /recipes/{id}; a generated Recipe's task_id names the one task that owns it.
   */
  recipes: any[] = [];
  /** Called before each request; return a Response to answer it instead. */
  intercept: (request: RecordedRequest) => Response | undefined = () => undefined;

  get paths() { return this.requests.map(request => request.path); }

  readonly fetch: typeof fetch = async (input, init) => {
    const url = new URL(String(input));
    const request: RecordedRequest = { method: init?.method ?? 'GET', path: url.pathname.replace(/^\/api/, ''), search: url.searchParams,
      body: typeof init?.body === 'string' ? JSON.parse(init.body) : undefined, headers: new Headers(init?.headers) };
    this.requests.push(request);
    return this.intercept(request) ?? this.answer(request);
  };

  private answer({ method, path, search, body, headers }: RecordedRequest): Response {
    for (const forbidden of ['/runs', '/attributions', '/improvements', '/sessions', '/revisions', '/recipe-candidates']) {
      if (path.includes(forbidden)) throw new Error(`removed route ${path}`);
    }
    if (path === '/v1/memory/recall') return json({ items: [] });
    if (path === '/artifacts') return json({ artifact_id: `artifact_${++this.artifacts}`, content_hash: 'sha256:test' });
    if (path === '/tasks' && method === 'POST') return this.createTask(body ?? {}, headers.get('idempotency-key')!);
    if (path.startsWith('/tasks/') && method === 'GET') return this.getTask(decodeURIComponent(path.split('/')[2]!));
    if (path === '/recipes' && method === 'GET') return this.listRecipes(search);
    if (path.startsWith('/recipes/') && method === 'GET') return this.getRecipe(decodeURIComponent(path.split('/')[2]!), search.get('version'));
    if (path === '/executions' && method === 'POST') return this.start(body ?? {}, headers.get('idempotency-key')!);
    const executionId = path.startsWith('/executions/') ? path.split('/')[2]! : undefined;
    const execution = executionId ? this.executions.get(executionId) : undefined;
    if (!executionId || !execution) return notFound('execution not found');
    if (path.endsWith('/result')) {
      if (body.request_sha256 !== execution.requestSha256) return invalid('execution request hash mismatch');
      if (execution.status === 'awaiting_execution') execution.status = body.outcome;
      return json({ task_id: execution.taskId, execution_id: executionId, status: execution.status, trace_origin: execution.kind });
    }
    if (path.endsWith('/feedback')) return this.feedback(executionId, execution, body, headers.get('idempotency-key')!);
    if (path.endsWith('/learn') || path.endsWith('/resume')) {
      const frozen = this.freezeModel(execution.taskId, body?.model);
      if (frozen) return frozen;
      return path.endsWith('/learn') ? this.learn(executionId, execution, body) : this.resume(executionId, body);
    }
    if (method === 'GET') {
      let learning = this.learnings.get(executionId);
      if (learning && ['queued', 'running'].includes(learning.status)) {
        learning = { ...learning, status: 'completed', current: true, recipe: { steps: [] } };
        this.learnings.set(executionId, learning);
      }
      return json({ execution_id: executionId, task_id: execution.taskId, instance_id: execution.instanceId, kind: execution.kind,
        status: execution.status, previous_execution_id: execution.previous, learning: learning ?? null });
    }
    return notFound(`no route ${method} ${path}`);
  }

  /** The task's model is frozen once set; a different one is rejected. */
  private freezeModel(taskId: string, model: string | undefined): Response | undefined {
    if (model === undefined) return undefined;
    const frozen = this.taskModels.get(taskId);
    if (frozen !== undefined && frozen !== model) return invalid('task model is frozen; only its credential and auth type may rotate');
    this.taskModels.set(taskId, model);
    return undefined;
  }

  /** A task as POST /tasks stores it; tests also call it for a task made before the test, such as a Recipe's. */
  addTask(objective: string, artifacts: Array<string | { artifact_id: string; role: string }> = []): string {
    const taskId = `task_${this.tasks.size + 1}`;
    this.tasks.set(taskId, { task_id: taskId, objective, context_version: 1,
      artifacts: artifacts.map(artifact => typeof artifact === 'string' ? { artifact_id: artifact, role: 'reference' } : artifact) });
    return taskId;
  }

  /** POST /tasks: one task per idempotency key; the same key with another body is refused. */
  private createTask(body: any, key: string): Response {
    if (Object.keys(body).some(field => !['objective', 'artifacts'].includes(field)) || !body.objective) return invalid('unknown task fields');
    const saved = this.taskKeys.get(key);
    if (saved) return saved.body === canonical(body) ? json(this.tasks.get(saved.taskId)) : invalid('idempotency key reused with different task');
    const taskId = this.addTask(body.objective, body.artifacts ?? []);
    this.taskKeys.set(key, { body: canonical(body), taskId });
    return json(this.tasks.get(taskId));
  }

  private getTask(taskId: string): Response {
    const task = this.tasks.get(taskId);
    if (!task) return notFound('task not found');
    const executions = [...this.executions].filter(([, execution]) => execution.taskId === taskId).map(([executionId, execution]) => ({
      execution_id: executionId, instance_id: execution.instanceId, kind: execution.kind, status: execution.status, request_sha256: execution.requestSha256 }));
    const recipes = this.current().filter(recipe => recipe.task_id === taskId).map(recipe => ({
      recipe_id: recipe.recipe_id, current_version: recipe.version, latest_version: recipe.version, learning: null }));
    return json({ ...task, executions, recipes });
  }

  private method(recipeId: string) {
    const recipe = this.current().find(item => item.recipe_id === recipeId);
    return { recipe_id: recipeId, version: recipe.version, recipe_sha256: 'f'.repeat(64), recipe: recipe.recipe };
  }

  /** The method a new execution freezes: the named Recipe of this task, else the task's only Recipe. */
  private taskMethod(taskId: string, recipeId: string | undefined): any {
    const owned = [...new Set(this.recipes.filter(recipe => recipe.task_id === taskId).map(recipe => recipe.recipe_id as string))].sort();
    if (recipeId !== undefined) return owned.includes(recipeId) ? this.method(recipeId) : notFound('Recipe not found in this task');
    if (owned.length > 1) return json({ error: { code: 'recipe_choice_required', recipe_ids: owned, message: 'the task has several Recipes; name one with recipe_id' } }, 422);
    return owned.length ? this.method(owned[0]!) : null;
  }

  /** The frozen input and what it lacks; a reconstruction also needs what it started from and what it produced. */
  static input(source: string, requestInput: any): [any, string[]] {
    const role = source === 'new_task' ? 'original_input' : 'submitted_output';
    const artifacts = (requestInput.artifacts ?? []).map((artifact: any) => typeof artifact === 'string' ? { artifact_id: artifact, role } : artifact);
    const value = { objective: requestInput.objective ?? null, artifacts, self_contained: Boolean(requestInput.self_contained) };
    const missing = value.objective ? [] : ['objective'];
    if (source === 'external_artifact') {
      const roles = new Set(artifacts.map((artifact: any) => artifact.role));
      if (!roles.has('original_input') && !value.self_contained) missing.push('original_input_or_self_contained_task');
      if (!roles.has('submitted_output')) missing.push('submitted_output');
    }
    return [value, missing];
  }

  /** POST /executions: a new execution names task_id, source and input; a correction names previous_execution_id. */
  private start(body: any, key: string): Response {
    const previous = body.previous_execution_id ?? null;
    let taskId: string; let requestInput: any; let instanceId: string | undefined; let method: any;
    let scope: string; let intent: string | undefined;
    if (previous !== null) {
      if (Object.keys(body).some(field => !['previous_execution_id', 'task_id', 'model'].includes(field))) return invalid('a correction takes only previous_execution_id, task_id and model');
      const before = this.executions.get(previous);
      if (!before) return notFound('execution not found');
      if (before.status !== 'completed') return invalid('execution has no completed output');
      if (body.task_id !== undefined && body.task_id !== before.taskId) return invalid('previous execution belongs to another task');
      taskId = before.taskId; requestInput = before.input; instanceId = before.instanceId;
      scope = JSON.stringify(['correction', previous, key]);
      // A correction follows the current version of the Recipe the previous execution followed.
      method = before.method ? this.method(before.method.recipe_id) : this.taskMethod(taskId, undefined);
    } else {
      if (!body.task_id || !['new_task', 'external_artifact'].includes(body.source) || !body.input || typeof body.input !== 'object') {
        return invalid('a new execution needs task_id, source and input');
      }
      taskId = body.task_id;
      if (!this.tasks.has(taskId)) return notFound('task not found');
      const [value, missing] = ServiceFake.input(body.source, body.input);
      if (missing.length) return json({ error: { code: 'input_incomplete', message: 'the execution input is incomplete', missing } }, 422);
      requestInput = value;
      scope = JSON.stringify(['original', taskId, key]);
      intent = canonical([body.source, value, body.recipe_id ?? null, Boolean(body.recall_memory)]);
      method = this.taskMethod(taskId, body.recipe_id);
    }
    if (method instanceof Response) return method;
    const frozen = this.freezeModel(taskId, body.model);
    if (frozen) return frozen;
    const saved = this.keys.get(scope);
    if (saved) return saved.intent === intent ? json(saved.receipt) : invalid('idempotency key reused with different context');
    const executionId = `execution_${this.executions.size + 1}`;
    const kind = previous ? 'correction' : 'original';
    instanceId ??= executionId;
    this.executions.set(executionId, { taskId, kind, status: 'awaiting_execution', previous, requestSha256: 'sha-' + executionId,
      input: requestInput, instanceId, method });
    const task = this.tasks.get(taskId)!;
    const receipt = { task_id: taskId, execution_id: executionId, instance_id: instanceId, kind, request_sha256: 'sha-' + executionId, status: 'awaiting_execution',
      request: { task_context: { objective: task.objective, artifacts: task.artifacts, context_version: task.context_version }, input: requestInput, method } };
    this.keys.set(scope, { intent, receipt });
    return json(receipt);
  }

  private feedback(executionId: string, execution: Execution, body: any, key: string): Response {
    if (execution.status !== 'completed') return invalid('feedback needs a completed execution');
    const scope = JSON.stringify([executionId, key]);
    const saved = this.feedbackKeys.get(scope);
    if (saved) {
      if (saved.message !== body.feedback || saved.source !== body.source) return invalid('idempotency key reused with different feedback');
      return json(saved);
    }
    const value = { feedback_id: `feedback_${this.feedbackKeys.size + 1}`, execution_id: executionId, task_id: execution.taskId, message: body.feedback, source: body.source };
    this.feedbackKeys.set(scope, value);
    return json(value);
  }

  private learn(executionId: string, execution: Execution, body: any): Response {
    const budget = body?.max_completion_tokens;
    const current = this.learnings.get(executionId);
    if (current) {
      if (budget !== undefined && budget !== current.max_completion_tokens) return invalid('this execution already learned with a different completion budget');
      return json(current);
    }
    if (execution.status !== 'completed') return invalid('execution has not completed');
    const version = this.learnings.size + 1;
    this.generations++;
    const learning = learningView(executionId, 'queued', { version, previous_version: version > 1 ? version - 1 : null,
      attribution: version > 1 ? { possible_causes: [{ reason: 'format' }] } : null, max_completion_tokens: budget ?? DEFAULT_BUDGET });
    this.learnings.set(executionId, learning);
    return json(learning);
  }

  private resume(executionId: string, body: any): Response {
    const budget = body?.max_completion_tokens;
    const current = this.learnings.get(executionId);
    if (!current) return invalid('this execution has not learned yet');
    if (ACTIVE.has(current.status)) {
      if (budget !== undefined && budget !== current.max_completion_tokens) return invalid('cannot change the completion budget of an active or completed learning');
      return json(current);
    }
    if (!RESUMABLE.has(current.status)) return invalid(`a learning in ${current.status} cannot be resumed`);
    const learning = { ...current, status: 'queued', error: null, error_detail: null, ...(budget === undefined ? {} : { max_completion_tokens: budget }) };
    this.learnings.set(executionId, learning);
    return json(learning);
  }

  /** Each Recipe's current (highest) version. */
  private current(): any[] {
    const latest = new Map<string, any>();
    for (const recipe of this.recipes) if ((latest.get(recipe.recipe_id)?.version ?? 0) < recipe.version) latest.set(recipe.recipe_id, recipe);
    return [...latest.values()];
  }

  private listRecipes(search: URLSearchParams): Response {
    const summary = (recipe: any) => ({ recipe_id: recipe.recipe_id, origin: recipe.origin, version: recipe.version, title: recipe.recipe.title, task_id: recipe.task_id ?? null, updated_at: 't' });
    const q = search.get('q');
    if (q !== null) {
      if (search.has('cursor') || search.has('origin')) return invalid('search takes no cursor or origin');
      const found = this.current().filter(recipe => String(recipe.recipe.title).toLowerCase().includes(q.toLowerCase()));
      return json({ items: found.slice(0, Number(search.get('limit') ?? 10)).map(summary) });
    }
    const origin = search.get('origin');
    const items = this.current().filter(recipe => !origin || recipe.origin === origin);
    const limit = Number(search.get('limit') ?? 20);
    return json({ items: items.slice(0, limit).map(summary), has_more: items.length > limit, next_cursor: null });
  }

  private getRecipe(recipeId: string, version: string | null): Response {
    const recipe = version === null ? this.current().find(item => item.recipe_id === recipeId)
      : this.recipes.find(item => item.recipe_id === recipeId && item.version === Number(version));
    return recipe ? json({ task_id: null, ...recipe }) : notFound('Recipe not found');
  }
}
