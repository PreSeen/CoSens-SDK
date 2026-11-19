import { spawn } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { authorizedTestFiles, snapshot } from './materials.js';
import { restartExecution } from './progress.js';
import { Store, uid, now, encode } from './store.js';
import {
  readRecipeList,
  type RecipeListEntry,
  type RecipeListRecipeEntry,
} from './recipe-list.js';
import {
  LocalConfigSchema,
  PolicySchema,
  type RubricsSchema,
  TERMINAL,
  fail,
  type LocalConfig,
  type LocalConfigInput,
  type Material,
  type MaterialManifest,
  type Execution,
  type ExecutionCreate,
  type ExecutionAnswer,
  type MetadataUpdate,
  type Pagination,
  type Kind,
  type ObjectMetadata,
  type Versioned,
  type Version,
  type RecipeContent,
  type ClarificationAnswer,
  type Runtime,
} from './models.js';

type EvidenceExecutionCreate = {
  evidence: MaterialManifest;
  confirmation_ref: string;
  idempotency_key: string;
  base: { kind: 'execution'; id: string } | null;
};

export class RSIClient {
  readonly store: Store;
  private readonly suppliedConfig?: LocalConfig;
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
      this.suppliedConfig =
        config === undefined ? undefined : LocalConfigSchema.parse(config);
    } catch (e) {
      this.store.close();
      throw e;
    }
  }
  get config(): LocalConfig {
    return (
      this.suppliedConfig ??
      fail('invalid_state', 'config is required to create work')
    );
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
    if (version && kind === 'recipe') {
      const qualified = obj.latest_qualified_version_id
        ? this.store.version(id, obj.latest_qualified_version_id)
        : null;
      version.archived =
        !!qualified &&
        version.number <= qualified.number &&
        version.id !== obj.active_version_id;
    }
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
    options: Pagination & { q?: string; active_only: true },
  ): RecipeListRecipeEntry[];
  recipe_list(
    options?: Pagination & { q?: string; active_only?: boolean },
  ): RecipeListEntry[];
  recipe_list(
    options: Pagination & { q?: string; active_only?: boolean } = {},
  ): RecipeListEntry[] {
    return readRecipeList(this.store, options);
  }
  recipe_list_all(): RecipeListEntry[] {
    return readRecipeList(this.store, {}, true);
  }
  private execution_insert(
    request: Omit<ExecutionCreate, 'idempotency_key'> & {
      answers?: Record<string, ExecutionAnswer>;
      name?: string;
    },
    config = this.config,
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
    if (request.base_recipe_version_id) {
      const base = this.store.version_by_id(
        request.base_recipe_version_id,
        'recipe',
      );
      if (this.store.get(base.object_id, 'recipe').archived_at)
        fail('invalid_state', 'archived recipe cannot be used');
    }
    if (
      request.base_execution_id &&
      !TERMINAL.has(this.execution_get(request.base_execution_id).status)
    )
      fail(
        'invalid_state',
        'continue active execution with execution_append_evidence',
      );
    return this.store.insert<Execution>('execution', {
      ...request,
      base_execution_id: request.base_execution_id ?? null,
      rubrics_version_id: null,
      runtime_config: config,
      policy: PolicySchema.parse(request.policy),
      status: 'queued',
      stage: 'queued',
      progress_percent: 0,
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
  private writeExecutionEvidence(
    evidence: MaterialManifest,
    config: LocalConfig,
    write: (data: ReturnType<typeof snapshot>) => {
      execution: Execution;
      attached: boolean;
    },
  ): Execution {
    const data = snapshot(this.data_dir, evidence, config);
    let attached = false;
    try {
      const result = this.store.tx(() => write(data));
      attached = result.attached;
      return result.execution;
    } finally {
      if (!attached)
        rmSync(join(this.data_dir, 'materials', data.id), {
          recursive: true,
          force: true,
        });
    }
  }

  async execution_create_with_evidence(
    request: EvidenceExecutionCreate,
  ): Promise<Execution> {
    const { evidence, confirmation_ref, idempotency_key, base } = request;
    const scope = 'execution_create';
    const replay = () =>
      this.store.replay<{ id: string }>(scope, idempotency_key, request);
    const previous = replay();
    if (!previous && this.auto_start) this.check_runtime();
    const execution = previous
      ? this.execution_get(previous.id)
      : this.writeExecutionEvidence(evidence, this.config, (data) => {
          const existing = replay();
          if (existing)
            return {
              execution: this.execution_get(existing.id),
              attached: false,
            };
          const parent = base ? this.execution_get(base.id) : null;
          const material = this.store.insert<Material>('material', data);
          const learningPaths = new Set(
            data.files
              .filter((file) => file.role !== 'input')
              .map((file) => file.path),
          );
          const inputs = data.files.filter((file) => file.role === 'input');
          if (inputs.some((file) => learningPaths.has(file.path)))
            fail(
              'invalid_argument',
              'current case inputs cannot also have a learning-only role',
            );
          const created = this.execution_insert({
            material_ids: [material.id],
            base_recipe_version_id: parent?.recipe_version_id,
            base_execution_id: parent?.id,
            policy: {
              artifact_confirmed: true,
              confirmation_ref,
              test_task: {
                instruction:
                  'Repeat the current task using the Recipe and the supplied current inputs. Produce the expected deliverables from scratch.',
                replay_current_case: true,
                input_locators: inputs.map((file) => file.locator),
                checks: [],
              },
            },
          });
          this.store.remember(scope, idempotency_key, request, {
            id: created.id,
          });
          return { execution: created, attached: true };
        });
    if (this.auto_start) await this.recover();
    return execution;
  }

  async execution_append_evidence(
    id: string,
    evidence: MaterialManifest,
  ): Promise<Execution> {
    const current = this.store.get<Execution>(id, 'execution');
    const validate = (execution: Execution) => {
      if (TERMINAL.has(execution.status))
        fail(
          'invalid_state',
          'terminal inputs are immutable; create a child execution',
        );
    };
    validate(current);
    const result = this.writeExecutionEvidence(
      evidence,
      current.runtime_config,
      (data) => {
        const execution = this.store.get<Execution>(id, 'execution');
        validate(execution);
        const alreadyAttached = execution.material_ids.some((materialId) => {
          const existing = this.material_get(materialId).files;
          return (
            existing.length === data.files.length &&
            existing.every((file, index) => {
              const incoming = data.files[index];
              return (
                file.path === incoming.path &&
                file.role === incoming.role &&
                file.sha256 === incoming.sha256
              );
            })
          );
        });
        if (alreadyAttached) return { execution, attached: false };
        const material = this.store.insert<Material>('material', data);
        execution.material_ids.push(material.id);
        restartExecution(execution);
        return { execution: this.store.save(execution), attached: true };
      },
    );
    if (this.auto_start) await this.recover();
    return result;
  }

  execution_evidence(execution: Execution) {
    return execution.material_ids.flatMap((id) => this.material_get(id).files);
  }

  execution_get(id: string): Execution {
    return this.store.db.transaction(() => {
      const execution = this.store.get<Execution>(id, 'execution');
      return {
        ...execution,
        name: this.store.executionRecipeName(execution) ?? '首次学习',
      };
    })();
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
      const obj = this.store.get<Execution>(id, 'execution');
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
          restartExecution(obj);
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
      const obj = this.store.get<Execution>(id, 'execution');
      if (obj.status === 'completed')
        fail('invalid_state', 'completed execution cannot be cancelled');
      if (TERMINAL.has(obj.status)) return obj;
      obj.status = 'cancelled';
      obj.stage = 'cancelled';
      const result = this.store.save(obj);
      this.store.db
        .prepare(
          'UPDATE executions SET fence=fence+1,owner=NULL,lease_until=NULL WHERE id=?',
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
      const obj = this.store.get<Execution>(id, 'execution'),
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
        const { continuation_execution_id, ...recorded } = obj.answers[key];
        if (encode(recorded) !== encode(answer))
          fail(
            'conflict',
            'answer already accepted; submit correction as feedback Material',
          );
        return {
          execution_id: id,
          continuation_execution_id: continuation_execution_id ?? null,
        };
      }
      obj.answers[key] = answer;
      let child_id: string | null = null;
      if (obj.status === 'completed') {
        child_id = this.execution_insert(
          {
            material_ids: obj.material_ids,
            policy: obj.policy,
            base_execution_id: id,
            base_recipe_version_id: obj.recipe_version_id,
            answers: obj.answers,
            name: this.store.executionRecipeName(obj),
          },
          this.suppliedConfig ?? obj.runtime_config,
        ).id;
        obj.answers[key] = { ...answer, continuation_execution_id: child_id };
      } else if (!TERMINAL.has(obj.status)) restartExecution(obj);
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
  recipe_version_set_archived(
    id: string,
    versionId: string,
    archived: boolean,
  ) {
    return this.store.tx(() => {
      const recipe = this.store.get<Versioned<RecipeContent>>(id, 'recipe');
      const version = this.store.version(id, versionId);
      const latest = recipe.latest_qualified_version_id
        ? this.store.version(id, recipe.latest_qualified_version_id)
        : null;
      if (!latest || version.number > latest.number)
        fail(
          'invalid_state',
          'version is not eligible for archival or restoration',
        );
      if (versionId !== latest!.id) {
        if (!archived)
          fail(
            'invalid_state',
            'only the latest qualified version can be restored',
          );
      } else {
        if (!archived && recipe.archived_at)
          fail('invalid_state', 'recipe is archived');
        const active = archived ? null : versionId;
        if (recipe.active_version_id !== active)
          this.store.save({ ...recipe, active_version_id: active });
      }
      return { recipe_id: id, version_id: versionId, archived };
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
      const obj = this.store.get<Execution>(execution_id, 'execution');
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
        restartExecution(obj);
        this.store.save(obj);
      }
      return { execution_id, status: 'queued' as const, ...request };
    });
    if (this.auto_start) await this.recover();
    return result;
  }
  private check_runtime(config = this.config) {
    if (!process.env[config.model.api_key_env])
      fail(
        'invalid_state',
        'configured model credential environment variable is missing',
      );
  }
  async recover(): Promise<{ started: number; scheduled?: true }> {
    const first = this.store.pending_execution();
    if (!first) return { started: 0 };
    const credentials = Object.keys(process.env).filter(
      (name) => process.env[name],
    );
    const pending = this.store.pending_execution(credentials);
    const config = LocalConfigSchema.parse(
      JSON.parse((pending ?? first).config),
    );
    this.check_runtime(config);
    const worker = fileURLToPath(new URL('./worker.js', import.meta.url));
    if (!existsSync(worker))
      fail(
        'invalid_state',
        'build the TypeScript SDK before starting detached workers',
      );

    const launch = async (owner: string, slot: string, deferred = false) => {
      try {
        const args = [
          worker,
          this.data_dir,
          owner,
          slot,
          (pending ?? first).id,
        ];
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
    for (let i = 0; i < config.max_executions; i++) {
      const owner = uid('worker');
      const slot = this.store.reserve(
        'worker',
        owner,
        config.max_executions,
        config.lease_seconds,
        true,
      );
      if (!slot) break;
      await launch(owner, slot);
      started++;
    }
    if (started) return { started };

    const owner = uid('worker');
    const deferred = this.store.tx(() =>
      this.store.has_pending(credentials)
        ? this.store.reserve('recovery', owner, 1, config.lease_seconds)
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
    if (this.suppliedConfig) return drain(this, runtime);
    const pending = this.store.pending_execution();
    if (!pending) return { processed: 0 };
    const client = new RSIClient({
      data_dir: this.data_dir,
      config: LocalConfigSchema.parse(JSON.parse(pending.config)),
      auto_start: false,
    });
    try {
      return await drain(client, runtime);
    } finally {
      client.close();
    }
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
