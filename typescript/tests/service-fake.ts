/** The execution API as cosens-rsi serves it, for tests: fresh execution IDs per key, one learning per execution. */
export function learningView(executionId: string, status = 'queued', fields: Record<string, unknown> = {}) {
  return { execution_id: executionId, recipe_id: 'recipe_1', version: 1, previous_version: null, current: status === 'completed',
    status, phase: status, attribution: null, recipe: status === 'completed' ? { steps: [] } : null, recipe_sha256: null,
    error: null, error_detail: null, models: null, memory_context: 'captured', initial: true, material_gap: null,
    max_completion_tokens: 200_000, ...fields } as Record<string, any>;
}

const ACTIVE = new Set(['queued', 'running', 'awaiting_previous_version', 'completed']);
const RESUMABLE = new Set(['failed', 'interrupted', 'awaiting_runner']);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
const invalid = (message: string) => json({ error: { code: 'invalid_request', message } }, 422);

export interface RecordedRequest { method: string; path: string; search: URLSearchParams; body: any; headers: Headers }

export class ServiceFake {
  readonly requests: RecordedRequest[] = [];
  readonly learnings = new Map<string, Record<string, any>>();
  readonly executions = new Map<string, { kind: string; status: string; previous: string | null }>();
  private readonly keys = new Map<string, any>();
  generations = 0;
  private artifacts = 0;
  /** Full Recipe views served by GET /recipes and /recipes/{id}. */
  recipes: any[] = [];
  /** The Recipe the service binds to a new execution; none unless a test sets it. */
  method: (kind: string, body: any) => any = () => null;
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
    if (path === '/recipes') {
      const origin = search.get('origin');
      const items = this.recipes.filter(recipe => !origin || recipe.origin === origin)
        .map(recipe => ({ recipe_id: recipe.recipe_id, origin: recipe.origin, version: recipe.version, title: recipe.recipe.title, task_id: 'task_0', updated_at: 't' }));
      return json({ items, has_more: false, next_cursor: null });
    }
    if (path.startsWith('/recipes/') && method === 'GET') return json(this.recipes.find(recipe => path === `/recipes/${recipe.recipe_id}`));
    if (path.endsWith('/executions') && method === 'POST') return this.start(body ?? {}, headers.get('idempotency-key')!);
    const executionId = path.startsWith('/executions/') ? path.split('/')[2]! : undefined;
    const execution = executionId ? this.executions.get(executionId) : undefined;
    if (!executionId || !execution) return json({ error: { code: 'not_found', message: `no route ${method} ${path}` } }, 404);
    if (path.endsWith('/result')) {
      if (execution.status === 'awaiting_execution') execution.status = body.outcome;
      return json({ task_id: 'task_1', execution_id: executionId, status: execution.status, trace_origin: execution.kind });
    }
    if (path.endsWith('/feedback')) {
      if (execution.status !== 'completed') return invalid('feedback needs a completed execution');
      return json({ feedback_id: 'feedback_1', execution_id: executionId, task_id: 'task_1', message: body.feedback, source: body.source });
    }
    if (path.endsWith('/learn')) return this.learn(executionId, execution, body);
    if (path.endsWith('/resume')) return this.resume(executionId, body);
    if (method === 'GET') {
      let learning = this.learnings.get(executionId);
      if (learning && ['queued', 'running'].includes(learning.status)) {
        learning = { ...learning, status: 'completed', current: true, recipe: { steps: [] } };
        this.learnings.set(executionId, learning);
      }
      return json({ execution_id: executionId, task_id: 'task_1', kind: execution.kind, status: execution.status,
        previous_execution_id: execution.previous, learning: learning ?? null });
    }
    return json({ error: { code: 'not_found', message: `no route ${method} ${path}` } }, 404);
  }

  private start(body: any, key: string): Response {
    if (this.keys.has(key)) return json(this.keys.get(key));
    const previous = body.previous_execution_id ?? null;
    if (previous !== null) {
      if (body.recipe_id !== undefined || body.recall_memory) return invalid('a correction takes only previous_execution_id and model');
      if (this.executions.get(previous)?.status !== 'completed') return invalid('previous execution has not completed');
    }
    const executionId = `execution_${this.executions.size + 1}`;
    const kind = previous ? 'correction' : 'original';
    this.executions.set(executionId, { kind, status: 'awaiting_execution', previous });
    const receipt = { task_id: 'task_1', execution_id: executionId, kind, request_sha256: 'request-hash', status: 'awaiting_execution',
      request: { context: { objective: 'Produce output' }, method: this.method(kind, body) } };
    this.keys.set(key, receipt);
    return json(receipt);
  }

  private learn(executionId: string, execution: { status: string; kind: string }, body: any): Response {
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
      attribution: version > 1 ? { possible_causes: [{ reason: 'format' }] } : null, max_completion_tokens: budget ?? 200_000 });
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
}
