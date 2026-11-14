/** The execution API as cosens-rsi serves it, for tests: fresh execution IDs per key, one learning per execution. */
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
interface Execution { taskId: string; kind: string; status: string; previous: string | null; requestSha256: string }

export class ServiceFake {
  readonly requests: RecordedRequest[] = [];
  readonly learnings = new Map<string, Record<string, any>>();
  readonly executions = new Map<string, Execution>();
  private readonly keys = new Map<string, { intent?: string; receipt: any }>();
  private readonly feedbackKeys = new Map<string, any>();
  private readonly taskModels = new Map<string, string>();
  generations = 0;
  private artifacts = 0;
  /** Recipe version views ({recipe_id, origin, version, recipe, ...}) served by GET /recipes and /recipes/{id}. */
  recipes: any[] = [];
  /** The task's current Recipe method; the service binds it to every new execution. */
  head: any = null;
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
    if (path === '/tasks' && method === 'POST') return json({ task_id: 'task_1', status: 'ready' });
    if (path === '/recipes' && method === 'GET') return this.listRecipes(search);
    if (path.startsWith('/recipes/') && method === 'GET') return this.getRecipe(decodeURIComponent(path.split('/')[2]!), search.get('version'));
    if (path.endsWith('/executions') && method === 'POST') return this.start(path.split('/')[2]!, body ?? {}, headers.get('idempotency-key')!);
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
      return json({ execution_id: executionId, task_id: execution.taskId, kind: execution.kind, status: execution.status,
        previous_execution_id: execution.previous, learning: learning ?? null });
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

  private start(taskId: string, body: any, key: string): Response {
    const previous = body.previous_execution_id ?? null;
    let scope: string; let intent: string | undefined;
    if (previous !== null) {
      if (body.recipe_id !== undefined || body.recall_memory) return invalid('a correction takes only previous_execution_id and model');
      const before = this.executions.get(previous);
      if (!before) return notFound('execution not found');
      if (before.status !== 'completed') return invalid('execution has no completed output');
      if (before.taskId !== taskId) return notFound('previous execution not found in this task');
      scope = JSON.stringify(['correction', previous, key]);
    } else {
      scope = JSON.stringify(['original', taskId, key]);
      intent = JSON.stringify([body.recipe_id ?? null, Boolean(body.recall_memory)]);
    }
    const frozen = this.freezeModel(taskId, body.model);
    if (frozen) return frozen;
    const saved = this.keys.get(scope);
    if (saved) return saved.intent === intent ? json(saved.receipt) : invalid('idempotency key reused with different context');
    const executionId = `execution_${this.executions.size + 1}`;
    const kind = previous ? 'correction' : 'original';
    this.executions.set(executionId, { taskId, kind, status: 'awaiting_execution', previous, requestSha256: 'sha-' + executionId });
    const receipt = { task_id: taskId, execution_id: executionId, kind, request_sha256: 'sha-' + executionId, status: 'awaiting_execution',
      request: { context: { objective: 'Produce output' }, method: this.head } };
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
    const summary = (recipe: any) => ({ recipe_id: recipe.recipe_id, origin: recipe.origin, version: recipe.version, title: recipe.recipe.title, task_id: 'task_0', updated_at: 't' });
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
    return recipe ? json(recipe) : notFound('Recipe not found');
  }
}
