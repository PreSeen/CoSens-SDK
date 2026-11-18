import { spawn } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { authorizedTestFiles, snapshot } from './materials.js';
import { Store, uid, now, encode, pagination } from './store.js';
import {
  LocalConfigSchema,
  PolicySchema,
  RecipeSchema,
  RubricsSchema,
  TERMINAL,
  fail,
  type LocalConfig,
  type LocalConfigInput,
  type Material,
  type MaterialManifest,
  type Execution,
  type ExecutionCreate,
  type MetadataUpdate,
  type Pagination,
  type Kind,
  type ObjectMetadata,
  type Versioned,
  type Version,
  type RecipeContent,
  type RubricsContent,
  type ClarificationAnswer,
  type Runtime,
} from './models.js';

export class RSIClient {
  readonly store: Store;
  readonly config: LocalConfig;
  readonly data_dir: string;
  readonly auto_start: boolean;
  private readonly node_executable: string;
  constructor({
    data_dir,
    config,
    auto_start = true,
    node_executable = process.execPath,
  }: {
    data_dir: string;
    config?: LocalConfigInput;
    auto_start?: boolean;
    node_executable?: string;
  }) {
    this.store = new Store(data_dir);
    this.data_dir = this.store.root;
    this.auto_start = auto_start;
    this.node_executable = node_executable;
    try {
      this.config = LocalConfigSchema.parse(
        this.store.configure(
          config ? LocalConfigSchema.parse(config) : undefined,
        ),
      );
    } catch (e) {
      this.store.close();
      throw e;
    }
  }
  material_create({
    manifest,
    idempotency_key,
  }: {
    manifest: MaterialManifest;
    idempotency_key?: string;
  }): Material {
    const request = { manifest };
    const prev = this.store.replay<{ id: string }>(
      'material_create',
      idempotency_key,
      request,
    );
    if (prev) return this.material_get(prev.id);
    const data = snapshot(this.data_dir, manifest, this.config);
    try {
      return this.store.tx(() => {
        const replay = this.store.replay<{ id: string }>(
          'material_create',
          idempotency_key,
          request,
        );
        if (replay) {
          rmSync(join(this.data_dir, 'materials', data.id), {
            recursive: true,
            force: true,
          });
          return this.material_get(replay.id);
        }
        const obj = this.store.insert<Material>('material', data);
        this.store.remember('material_create', idempotency_key, request, {
          id: obj.id,
        });
        return obj;
      });
    } catch (e) {
      rmSync(join(this.data_dir, 'materials', data.id), {
        recursive: true,
        force: true,
      });
      throw e;
    }
  }
  material_get(id: string): Material {
    return this.store.get(id, 'material');
  }
  material_list(options: Pagination = {}): Material[] {
    return this.store.list('material', options);
  }
  private update<T extends ObjectMetadata>(
    id: string,
    kind: Kind,
    changes: MetadataUpdate,
  ): T {
    return this.store.tx(() => {
      const obj = this.store.get<T>(id, kind);
      this.check_revision(obj, changes.expected_revision);
      if (changes.name !== undefined) obj.name = changes.name;
      if (changes.description !== undefined)
        obj.description = changes.description;
      if (changes.archived !== undefined)
        obj.archived_at = changes.archived ? (obj.archived_at ?? now()) : null;
      return this.store.save(obj);
    });
  }
  private check_revision(obj: ObjectMetadata, revision?: number) {
    if (revision !== undefined && obj.revision !== revision)
      fail('conflict', 'object revision changed');
  }
  material_update(id: string, changes: MetadataUpdate): Material {
    return this.update(id, 'material', changes);
  }
  private create_versioned<C>(
    kind: 'recipe' | 'rubrics',
    name: string,
    content?: C,
  ): Versioned<C> {
    if (!name.trim()) fail('invalid_argument', 'name is required');
    return this.store.tx(() => {
      const parsed =
        content === undefined
          ? undefined
          : (kind === 'recipe' ? RecipeSchema : RubricsSchema).parse(content);
      const obj = this.store.insert<Versioned<C>>(kind, {
        name,
        description: '',
        latest_version_id: null,
        ...(kind === 'recipe' ? { active_version_id: null } : {}),
      });
      if (parsed) this.store.append(obj, parsed);
      return this.store.get(obj.id, kind);
    });
  }
  rubrics_create({
    name,
    content,
  }: {
    name: string;
    content?: RubricsContent;
  }): Versioned<ResolvedRubrics> {
    return this.create_versioned(
      'rubrics',
      name,
      content,
    ) as Versioned<ResolvedRubrics>;
  }
  recipe_create({
    name,
    content,
  }: {
    name: string;
    content?: RecipeContent;
  }): Versioned<RecipeContent> {
    return this.create_versioned('recipe', name, content);
  }
  private get_versioned<C>(
    id: string,
    kind: 'recipe' | 'rubrics',
    {
      version_id,
      version_selector,
      include_content = true,
    }: {
      version_id?: string;
      version_selector?: 'active' | 'latest';
      include_content?: boolean;
    } = {},
  ): Versioned<C> {
    if (version_id && version_selector)
      fail(
        'invalid_argument',
        'version_id and version_selector are mutually exclusive',
      );
    const obj = this.store.get<Versioned<C>>(id, kind),
      selected =
        version_id ??
        (kind === 'recipe' && version_selector !== 'latest'
          ? obj.active_version_id
          : obj.latest_version_id);
    const version = selected ? this.store.version<C>(id, selected) : null;
    if (version && !include_content)
      delete (version as Partial<Version<C>>).content;
    return { ...obj, selected_version: version };
  }
  recipe_get(
    id: string,
    options: {
      version_id?: string;
      version_selector?: 'active' | 'latest';
      include_content?: boolean;
    } = {},
  ): Versioned<RecipeContent> {
    return this.get_versioned(id, 'recipe', options);
  }
  rubrics_get(
    id: string,
    options: { version_id?: string; include_content?: boolean } = {},
  ): Versioned<ResolvedRubrics> {
    return this.get_versioned(id, 'rubrics', options);
  }
  rubrics_list(options: Pagination = {}): Versioned<ResolvedRubrics>[] {
    return this.store.list('rubrics', options);
  }
  recipe_list(
    options: Pagination & { q?: string; active_only?: boolean } = {},
  ): Versioned<RecipeContent>[] {
    const { limit, offset } = pagination(options),
      q = options.q ?? null;
    return (
      this.store.db
        .prepare(
          `SELECT o.data FROM objects o LEFT JOIN versions v ON v.id=json_extract(o.data,'$.active_version_id')
      WHERE o.kind='recipe' AND ((json_extract(o.data,'$.archived_at') IS NOT NULL)=?) AND (?=0 OR v.id IS NOT NULL)
      AND (? IS NULL OR instr(lower(COALESCE(json_extract(o.data,'$.name'),'')||' '||COALESCE(json_extract(o.data,'$.description'),'')||' '||COALESCE(v.data,'')),lower(?))>0)
      ORDER BY o.created,o.id LIMIT ? OFFSET ?`,
        )
        .all(
          Number(options.archived ?? false),
          Number(options.active_only ?? false),
          q,
          q,
          limit,
          offset,
        ) as { data: string }[]
    ).map((row) => JSON.parse(row.data));
  }
  rubrics_update(
    id: string,
    changes: MetadataUpdate,
  ): Versioned<ResolvedRubrics> {
    return this.update(id, 'rubrics', changes);
  }
  recipe_update(id: string, changes: MetadataUpdate): Versioned<RecipeContent> {
    return this.update(id, 'recipe', changes);
  }
  private version_create<C>(
    id: string,
    kind: 'recipe' | 'rubrics',
    options: {
      content: C;
      expected_revision?: number;
      idempotency_key?: string;
    },
  ): Version<C> {
    const content = (kind === 'recipe' ? RecipeSchema : RubricsSchema).parse(
      options.content,
    ) as C;
    const request = {
      object_id: id,
      content,
      expected_revision: options.expected_revision,
    };
    return this.store.tx(() => {
      const prev = this.store.replay<Version<C>>(
        kind + '_version_create',
        options.idempotency_key,
        request,
      );
      if (prev) return prev;
      const obj = this.store.get(id, kind);
      this.check_revision(obj, options.expected_revision);
      if (obj.archived_at)
        fail('invalid_state', 'restore the object before creating a version');
      const version = this.store.append(obj, content);
      this.store.remember(
        kind + '_version_create',
        options.idempotency_key,
        request,
        version,
      );
      return version;
    });
  }
  recipe_version_create(
    id: string,
    options: {
      content: RecipeContent;
      expected_revision?: number;
      idempotency_key?: string;
    },
  ): Version<RecipeContent> {
    return this.version_create(id, 'recipe', options);
  }
  rubrics_version_create(
    id: string,
    options: {
      content: RubricsContent;
      expected_revision?: number;
      idempotency_key?: string;
    },
  ): Version<ResolvedRubrics> {
    return this.version_create(
      id,
      'rubrics',
      options,
    ) as Version<ResolvedRubrics>;
  }
  private version_list<C>(
    id: string,
    kind: 'recipe' | 'rubrics',
    options: Pagination = {},
  ): Version<C>[] {
    const { limit, offset } = pagination(options);
    this.store.get(id, kind);
    return (
      this.store.db
        .prepare(
          'SELECT data FROM versions WHERE object_id=? ORDER BY number LIMIT ? OFFSET ?',
        )
        .all(id, limit, offset) as { data: string }[]
    ).map((r) => JSON.parse(r.data));
  }
  recipe_version_list(
    id: string,
    options: Pagination = {},
  ): Version<RecipeContent>[] {
    return this.version_list(id, 'recipe', options);
  }
  rubrics_version_list(
    id: string,
    options: Pagination = {},
  ): Version<ResolvedRubrics>[] {
    return this.version_list(id, 'rubrics', options);
  }
  private execution_insert(
    request: Omit<ExecutionCreate, 'idempotency_key'> & {
      answers?: Record<string, unknown>;
    },
  ): Execution {
    if (!request.material_ids.length)
      fail('invalid_argument', 'at least one Material is required');
    const materials = request.material_ids.map((id) => this.material_get(id));
    for (const material of materials)
      if (material.archived_at)
        fail('invalid_state', 'archived material cannot be attached');
    authorizedTestFiles(
      materials.flatMap((material) => material.files),
      request.policy.test_task,
    );
    for (const [id, kind] of [
      [request.rubrics_version_id, 'rubrics'],
      [request.base_recipe_version_id, 'recipe'],
    ] as const) {
      if (id) {
        const v = this.store.version_by_id(id, kind);
        if (this.store.get(v.object_id, kind).archived_at)
          fail('invalid_state', 'archived version owner cannot be used');
      }
    }
    if (
      request.parent_execution_id &&
      !TERMINAL.has(this.execution_get(request.parent_execution_id).status)
    )
      fail('invalid_state', 'continue active execution with execution_update');
    return this.store.insert<Execution>('execution', {
      ...request,
      policy: PolicySchema.parse(request.policy),
      status: 'queued',
      stage: 'queued',
      input_revision: 1,
      questions: [],
      answers: request.answers ?? {},
      recipe_id: null,
      recipe_version_id: null,
      test_results: [],
      result: null,
      error: null,
    });
  }
  async execution_create({
    idempotency_key,
    ...input
  }: ExecutionCreate): Promise<Execution> {
    if (this.auto_start) this.check_runtime();
    const request = {
      ...input,
      policy: PolicySchema.parse(input.policy),
      material_ids: [...new Set(input.material_ids)],
    };
    const obj = this.store.tx(() => {
      const prev = this.store.replay<{ id: string }>(
        'execution_create',
        idempotency_key,
        request,
      );
      if (prev) return this.execution_get(prev.id);
      const created = this.execution_insert(request);
      this.store.remember('execution_create', idempotency_key, request, {
        id: created.id,
      });
      return created;
    });
    if (this.auto_start) await this.recover();
    return obj;
  }
  execution_get(id: string): Execution {
    return this.store.get(id, 'execution');
  }
  execution_list(options: Pagination = {}): Execution[] {
    return this.store.list('execution', options);
  }
  async execution_update(
    id: string,
    {
      material_ids,
      archived,
      expected_revision,
    }: {
      material_ids?: string[];
      archived?: boolean;
      expected_revision?: number;
    },
  ): Promise<Execution> {
    const result = this.store.tx(() => {
      const obj = this.execution_get(id);
      this.check_revision(obj, expected_revision);
      if (material_ids !== undefined) {
        if (TERMINAL.has(obj.status))
          fail(
            'invalid_state',
            'terminal inputs are immutable; create a child execution',
          );
        for (const material of material_ids)
          if (this.material_get(material).archived_at)
            fail('invalid_state', 'archived material cannot be attached');
        const updated = [...new Set([...obj.material_ids, ...material_ids])];
        if (updated.length !== obj.material_ids.length) {
          obj.material_ids = updated;
          obj.input_revision++;
        }
      }
      if (archived !== undefined)
        obj.archived_at = archived ? (obj.archived_at ?? now()) : null;
      return this.store.save(obj);
    });
    if (this.auto_start && !TERMINAL.has(result.status)) await this.recover();
    return result;
  }
  execution_cancel(id: string): Execution {
    return this.store.tx(() => {
      const obj = this.execution_get(id);
      if (obj.status === 'completed')
        fail('invalid_state', 'completed execution cannot be cancelled');
      if (TERMINAL.has(obj.status)) return obj;
      obj.status = 'cancelled';
      obj.stage = 'cancelled';
      const result = this.store.save(obj);
      this.store.db
        .prepare(
          'UPDATE jobs SET fence=fence+1,owner=NULL,lease_until=NULL WHERE id=?',
        )
        .run(id);
      return result;
    });
  }
  async execution_clarification_answer(
    id: string,
    options: ClarificationAnswer,
  ): Promise<{
    execution_id: string;
    continuation_execution_id: string | null;
  }> {
    const { idempotency_key, ...args } = options,
      request = {
        execution_id: id,
        ...args,
        selected_option_ids: [...new Set(args.selected_option_ids)].sort(),
      };
    const result = this.store.tx(() => {
      const prev = this.store.replay<{
        execution_id: string;
        continuation_execution_id: string | null;
      }>('clarification', idempotency_key, request);
      if (prev) return prev;
      const obj = this.execution_get(id),
        q = obj.questions.find(
          (q) =>
            q.id === args.question_id && q.revision === args.question_revision,
        );
      if (!q) fail('conflict', 'question is missing or stale');
      if (!request.selected_option_ids.length && !args.text)
        fail('invalid_argument', 'a selected option or free text is required');
      if (
        request.selected_option_ids.some(
          (id) => !q!.options.some((o) => o.id === id),
        )
      )
        fail('invalid_argument', 'unknown option');
      const key = `${q!.id}:${q!.revision}`,
        answer = {
          selected_option_ids: request.selected_option_ids,
          question: q!.question,
          question_revision: q!.revision,
          selected_options: q!.options.filter((o) =>
            request.selected_option_ids.includes(o.id),
          ),
          text: args.text ?? null,
        };
      if (obj.answers[key]) {
        if (encode(obj.answers[key]) !== encode(answer))
          fail(
            'conflict',
            'answer already accepted; submit correction as feedback Material',
          );
        return {
          execution_id: id,
          continuation_execution_id: obj.answer_children?.[key] ?? null,
        };
      }
      obj.answers[key] = answer;
      let child_id: string | null = null;
      if (obj.status === 'completed') {
        child_id = this.execution_insert({
          material_ids: obj.material_ids,
          policy: obj.policy,
          rubrics_version_id: obj.rubrics_version_id,
          source_session_id: obj.source_session_id,
          parent_execution_id: id,
          base_recipe_version_id: obj.recipe_version_id,
          answers: obj.answers,
        }).id;
        obj.answer_children = { ...obj.answer_children, [key]: child_id };
      } else if (!TERMINAL.has(obj.status)) obj.input_revision++;
      this.store.save(obj);
      const result = { execution_id: id, continuation_execution_id: child_id };
      this.store.remember('clarification', idempotency_key, request, result);
      return result;
    });
    if (
      this.auto_start &&
      !TERMINAL.has(
        this.execution_get(result.continuation_execution_id ?? id).status,
      )
    )
      await this.recover();
    return result;
  }
  recipe_activate(
    id: string,
    {
      version_id,
      expected_revision,
    }: { version_id: string | null; expected_revision: number },
  ): Versioned<RecipeContent> {
    return this.store.tx(() => {
      const obj = this.store.get<Versioned<RecipeContent>>(id, 'recipe');
      this.check_revision(obj, expected_revision);
      if (version_id) {
        this.store.version(id, version_id);
        if (obj.archived_at)
          fail('invalid_state', 'restore recipe before activation');
        const rows = this.store.db
          .prepare(
            "SELECT data FROM objects WHERE kind='execution' AND json_extract(data,'$.status')='completed'",
          )
          .all() as { data: string }[];
        const eligible = rows.some((row) => {
          const e = JSON.parse(row.data) as Execution;
          return (
            e.result &&
            !e.result.provisional &&
            e.test_results.some(
              (t) =>
                t.version_id === version_id &&
                t.status === 'passed' &&
                t.input_revision === e.input_revision,
            )
          );
        });
        if (!eligible)
          fail(
            'invalid_state',
            'version has no completed non-provisional passing test',
          );
      }
      obj.active_version_id = version_id;
      return this.store.save(obj);
    });
  }
  async recipe_test({
    execution_id,
    recipe_id,
    version_id,
    rubrics_version_id,
    material_ids,
  }: {
    execution_id: string;
    recipe_id: string;
    version_id: string;
    rubrics_version_id: string;
    material_ids?: string[];
  }) {
    const result = this.store.tx(() => {
      const obj = this.execution_get(execution_id);
      if (TERMINAL.has(obj.status))
        fail(
          'invalid_state',
          'test within active execution; create a child for a terminal run',
        );
      if (this.store.get(recipe_id, 'recipe').archived_at)
        fail('invalid_state', 'recipe is archived');
      this.store.version(recipe_id, version_id);
      this.store.version_by_id(rubrics_version_id, 'rubrics');
      if (
        material_ids &&
        encode([...new Set(material_ids)].sort()) !==
          encode([...obj.material_ids].sort())
      )
        fail('conflict', 'test materials must match execution snapshot');
      const request = { recipe_id, version_id, rubrics_version_id };
      if (encode(obj.test_request) !== encode(request)) {
        obj.test_request = request;
        obj.input_revision++;
        this.store.save(obj);
      }
      return { execution_id, status: 'queued' as const, ...request };
    });
    if (this.auto_start) await this.recover();
    return result;
  }
  private check_runtime() {
    if (!process.env[this.config.model.api_key_env])
      fail(
        'invalid_state',
        'configured model credential environment variable is missing',
      );
  }
  async recover(): Promise<{ started: number; scheduled?: true }> {
    this.check_runtime();
    if (!this.store.has_pending()) return { started: 0 };
    const worker = fileURLToPath(new URL('./worker.js', import.meta.url));
    if (!existsSync(worker))
      fail(
        'invalid_state',
        'build the TypeScript SDK before starting detached workers',
      );

    const launch = async (owner: string, slot: string, deferred = false) => {
      try {
        const args = [worker, this.data_dir, owner, slot];
        if (deferred) args.push('--wait-for-slot');
        const child = spawn(this.node_executable, args, {
          detached: true,
          stdio: 'ignore',
          env: {
            ...process.env,
            LANGSMITH_TRACING: 'false',
            LANGCHAIN_TRACING_V2: 'false',
            LANGCHAIN_VERBOSE: 'false',
          },
        });
        await new Promise<void>((resolve, reject) => {
          child.once('spawn', resolve);
          child.once('error', reject);
        });
        child.unref();
      } catch (error) {
        this.store.release(slot);
        throw error;
      }
    };

    let started = 0;
    for (let i = 0; i < this.config.max_executions; i++) {
      const owner = uid('worker');
      const slot = this.store.reserve(
        'worker',
        owner,
        this.config.max_executions,
        this.config.lease_seconds,
        true,
      );
      if (!slot) break;
      await launch(owner, slot);
      started++;
    }
    if (started) return { started };

    const owner = uid('worker');
    const deferred = this.store.tx(() =>
      this.store.has_pending()
        ? this.store.reserve('recovery', owner, 1, this.config.lease_seconds)
        : null,
    );
    if (!deferred) return { started: 0 };
    await launch(owner, deferred, true);
    return { started: 0, scheduled: true };
  }
  async run_pending({ runtime }: { runtime?: Runtime } = {}): Promise<{
    processed: number;
  }> {
    const { drain } = await import('./worker.js');
    return drain(this, runtime);
  }
  async wait(
    id: string,
    {
      timeout = 300,
      poll_interval = 0.2,
    }: { timeout?: number; poll_interval?: number } = {},
  ): Promise<Execution> {
    if (timeout < 0 || poll_interval <= 0)
      fail('invalid_argument', 'invalid wait timing');
    const deadline = Date.now() + timeout * 1000;
    for (;;) {
      const result = this.execution_get(id);
      if (TERMINAL.has(result.status)) return result;
      if (Date.now() >= deadline)
        fail('timeout', 'execution still pending; work has not been cancelled');
      await sleep(poll_interval * 1000);
    }
  }
  close() {
    this.store.close();
  }
}
type ResolvedRubrics = ReturnType<typeof RubricsSchema.parse>;
