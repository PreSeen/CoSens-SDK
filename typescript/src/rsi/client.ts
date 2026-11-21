import { spawn } from 'node:child_process';
import { existsSync, rmSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { authorizedTestFiles, snapshot } from './materials.js';
import { restartExecution } from './progress.js';
import { Store, uid, now, encode, type RetryTarget } from './store.js';
import { isHeaderBinding } from './model-headers.js';
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
  type Recipe,
  type RecipeContent,
  type ClarificationAnswer,
  type Runtime,
} from './models.js';

type EvidenceExecutionCreate = {
  evidence: MaterialManifest;
  confirmation_ref: string;
  idempotency_key: string;
  base: { kind: 'recipe'; id: string } | null;
};

export type RecoverResult = {
  started: number;
  scheduled?: true;
  recipe_id?: string;
  execution_id?: string;
};

export class RSIClient {
  readonly store: Store;
  private readonly suppliedConfig?: LocalConfig;
  readonly data_dir: string;
  readonly auto_start: boolean;
  private readonly node_executable: string;
  private readonly model_header_binding?: { name: string; value: string };
  constructor({
    data_dir,
    config,
    auto_start = true,
    node_executable = process.execPath,
    model_header_binding,
  }: {
    data_dir: string;
    config?: LocalConfigInput;
    auto_start?: boolean;
    node_executable?: string;
    model_header_binding?: { name: string; value: string };
  }) {
    this.store = new Store(data_dir);
    this.data_dir = this.store.root;
    this.auto_start = auto_start;
    this.node_executable = node_executable;
    this.model_header_binding = model_header_binding;
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
  private get_versioned<C, T extends Versioned<C> | Recipe = Versioned<C>>(
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
  ): T {
    if (version_id && version_selector)
      fail(
        'invalid_argument',
        'version_id and version_selector are mutually exclusive',
      );
    const obj = this.store.get<T>(id, kind),
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
  recipe_insert(content: RecipeContent, markdown: string): Recipe {
    const { recipeId, versionId } = this.store.tx(() => {
      const recipe = this.store.createRecipe(content.name);
      const version = this.store.append(
        recipe,
        content,
        undefined,
        { source: 'external' },
        markdown,
      );
      this.store.publishRecipeVersion(recipe.id, version.id);
      return { recipeId: recipe.id, versionId: version.id };
    });
    return this.recipe_get(recipeId, { version_id: versionId });
  }
  recipe_get(
    id: string,
    options: {
      version_id?: string;
      version_selector?: 'active' | 'latest';
      include_content?: boolean;
    } = {},
  ): Recipe {
    return this.get_versioned<RecipeContent, Recipe>(id, 'recipe', options);
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
  recipe_list_all(recipe_ids?: string[]): RecipeListEntry[] {
    return readRecipeList(this.store, { recipe_ids }, true);
  }
  private execution_insert(
    request: Omit<ExecutionCreate, 'idempotency_key'> & {
      answers?: Record<string, ExecutionAnswer>;
      name?: string;
      test_request?: Execution['test_request'];
      recipe_id?: string | null;
      input_revision?: number;
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
      input_revision: request.input_revision ?? 1,
      questions: [],
      answers: request.answers ?? {},
      recipe_id: request.recipe_id ?? null,
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

  private bindRecipeOnReplay(id: string): Execution {
    const execution = this.store.get<Execution>(id, 'execution');
    if (!execution.recipe_id) {
      const baseId = execution.base_recipe_version_id;
      const recipeId =
        execution.test_request?.recipe_id ??
        (baseId
          ? this.store.version_by_id(baseId, 'recipe').object_id
          : this.store.createRecipe(execution.name).id);
      this.store.save({ ...execution, recipe_id: recipeId });
    }
    return this.execution_get(id);
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
      ? this.store.tx(() => this.bindRecipeOnReplay(previous.id))
      : this.writeExecutionEvidence(evidence, this.config, (data) => {
          const existing = replay();
          if (existing)
            return {
              execution: this.bindRecipeOnReplay(existing.id),
              attached: false,
            };
          const recipe = base
            ? this.store.get<Recipe>(base.id, 'recipe')
            : null;
          if (
            recipe &&
            (recipe.archived_at !== null || !recipe.active_version_id)
          )
            fail('invalid_state', 'base Recipe has no available version');
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
            recipe_id: recipe?.id ?? this.store.createRecipe().id,
            name: recipe?.name,
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
  execution_list_all(executionIds?: string[]): Execution[] {
    return this.store.listExecutions(executionIds);
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
      return this.cancelExecution(obj);
    });
  }
  private cancelExecution(execution: Execution): Execution {
    const result = this.store.save<Execution>({
      ...execution,
      recovery_config: undefined,
      status: 'cancelled',
      stage: 'cancelled',
    });
    this.store.db
      .prepare(
        'UPDATE executions SET fence=fence+1,owner=NULL,lease_until=NULL WHERE id=?',
      )
      .run(execution.id);
    return result;
  }
  private refreshClarificationHeaders(
    execution: Execution,
    inherited: LocalConfig,
  ): LocalConfig {
    const current = this.suppliedConfig?.model;
    const binding = this.model_header_binding;
    const original = inherited.model;
    if (
      !binding ||
      !current ||
      !execution.recipe_id ||
      !isHeaderBinding(original.api_key_env) ||
      !original.credential_scope ||
      original.credential_scope !== current.credential_scope ||
      original.api_key_env === binding.name ||
      original.model !== current.model ||
      original.base_url?.replace(/\/+$/, '') !== current.base_url?.replace(/\/+$/, '')
    ) return inherited;

    const version = execution.recipe_version_id ?? execution.base_recipe_version_id ?? null;
    const linkedRecipe = this.store.db.prepare(`
      SELECT 1 FROM recipes WHERE id=? AND archived_at IS NULL AND (? IS NULL OR EXISTS (
        SELECT 1 FROM versions WHERE recipe_id=recipes.id AND id=?
      ))
    `).get(execution.recipe_id, version, version);
    if (!linkedRecipe) return inherited;

    return { ...inherited, model: { ...original, api_key_env: binding.name } };
  }
  private repairQueuedClarification(parentId: string, childId?: string | null) {
    if (!this.model_header_binding || !childId) return;
    const child = this.store.get<Execution>(childId, 'execution');
    if (child.status !== 'queued' || !child.recipe_id || child.base_execution_id !== parentId) return;
    const inherited = child.recovery_config ?? child.runtime_config;
    if (inherited.model.api_key_env === this.model_header_binding.name) return;
    const parent = this.store.get<Execution>(parentId, 'execution');
    const version = parent.recipe_version_id ?? parent.base_recipe_version_id ?? null;
    const parentConfig = parent.recovery_config ?? parent.runtime_config;
    if (
      child.recipe_id !== parent.recipe_id ||
      (child.base_recipe_version_id ?? null) !== version ||
      inherited.model.credential_scope !== parentConfig.model.credential_scope
    ) return;
    const config = this.refreshClarificationHeaders(child, inherited);
    if (config !== inherited)
      this.store.save({ ...child, runtime_config: config, recovery_config: undefined });
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
      if (prev) {
        this.repairQueuedClarification(id, prev.continuation_execution_id);
        return prev;
      }
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
        this.repairQueuedClarification(id, continuation_execution_id);
        return {
          execution_id: id,
          continuation_execution_id: continuation_execution_id ?? null,
        };
      }
      const answers = { ...obj.answers, [key]: answer };
      let child_id: string | null = null;
      const active = !TERMINAL.has(obj.status);
      const allAnswered = obj.questions.every(
        (question) => `${question.id}:${question.revision}` in answers,
      );
      if (allAnswered && (active || obj.status === 'completed')) {
        const continuationConfig = obj.recovery_config ?? obj.runtime_config;
        const parent = active ? this.cancelExecution(obj) : obj;
        child_id = this.execution_insert(
          {
            material_ids: parent.material_ids,
            policy: parent.policy,
            base_execution_id: id,
            recipe_id: parent.recipe_id,
            base_recipe_version_id:
              parent.recipe_version_id ?? parent.base_recipe_version_id,
            // Keep newly generated questions distinct from inherited answers.
            input_revision:
              Math.max(
                parent.input_revision,
                ...Object.values(answers).map(
                  (answer) => answer.question_revision,
                ),
              ) + 1,
            answers,
            name: this.store.executionRecipeName(parent),
            ...(active && parent.test_request
              ? { test_request: parent.test_request }
              : {}),
          },
          isHeaderBinding(continuationConfig.model.api_key_env)
            ? this.refreshClarificationHeaders(obj, continuationConfig)
            : this.suppliedConfig ?? continuationConfig,
        ).id;
        this.store.save({
          ...parent,
          answers: {
            ...answers,
            [key]: { ...answer, continuation_execution_id: child_id },
          },
        });
      } else this.store.save({ ...obj, answers });
      const result = { execution_id: id, continuation_execution_id: child_id };
      this.store.remember('clarification', idempotency_key, request, result);
      return result;
    });
    if (
      this.auto_start &&
      result.continuation_execution_id &&
      !TERMINAL.has(this.execution_get(result.continuation_execution_id).status)
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
      const recipe = this.store.get<Recipe>(id, 'recipe');
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
        this.store.setRecipeVersionStatus(
          recipe,
          versionId,
          archived ? 'archived' : 'available',
        );
      }
      return { recipe_id: id, version_id: versionId, archived };
    });
  }
  private async retryTimedOutExecution(recipeId: string, retryKey: string): Promise<RecoverResult> {
    this.check_runtime();
    const { model: current } = this.config;
    const request = { recipe_id: recipeId };
    const { target, config, dispatch } = this.store.tx(() => {
      const recipe = this.store.get<Recipe>(recipeId, 'recipe');
      if (recipe.archived_at != null) fail('invalid_state', 'recipe is archived');
      const replay = this.store.replay<RetryTarget>(
        'recipe_retry', retryKey, request,
      );
      const selected = this.store.recipeExecution(recipeId, replay?.id);
      if (!selected || selected.execution.archived_at != null)
        return fail('invalid_state', 'no eligible execution for this Recipe');
      const { execution, target } = selected;
      const stored = execution.recovery_config ?? execution.runtime_config;
      const original = stored.model;
      if (
        original.credential_scope !== current.credential_scope ||
        (original.credential_scope === undefined &&
          original.api_key_env !== current.api_key_env) ||
        original.model !== current.model ||
        original.base_url?.replace(/\/+$/, '') !== current.base_url?.replace(/\/+$/, '')
      )
        fail('invalid_state', 'retry requires the original model endpoint, model and credential scope');

      const config = {
        ...stored,
        model: { ...original, api_key_env: current.api_key_env },
      };
      if (replay) {
        const queued = this.store.queuedRetry(replay);
        if (queued && original.api_key_env !== current.api_key_env) {
          this.store.save({
            ...execution,
            runtime_config: config,
            recovery_config: undefined,
          });
        }
        return { target: replay, config, dispatch: queued };
      }
      if (
        execution.status !== 'failed' ||
        execution.result?.published ||
        execution.error?.code !== 'timeout' ||
        execution.error.upstream_status !== undefined
      )
        return fail('invalid_state', 'only the latest timed-out execution can be retried');
      if (this.store.has_pending(undefined, recipeId))
        fail('invalid_state', 'a Recipe execution is already queued or running');

      this.store.save({
        ...execution,
        runtime_config: config,
        recovery_config: undefined,
        status: 'queued',
        stage: execution.error.stage,
        error: null,
      });
      this.store.remember('recipe_retry', retryKey, request, target);
      return { target, config, dispatch: true };
    });
    const receipt = { started: 0, recipe_id: recipeId, execution_id: target.id };
    if (dispatch) {
      const owner = uid('worker');
      // Keep the dispatch reservation while its Worker waits for a slot or claims this attempt.
      const slot = this.store.tx(() =>
        this.store.queuedRetry(target, config.model.api_key_env)
          ? this.store.reserve(
              `recovery:retry:${target.id}:${target.fence}:${config.model.api_key_env}`,
              owner, 1, config.lease_seconds,
            )
          : null,
      );
      if (slot) {
        await this.launchWorker(owner, slot, target.id, recipeId, true, target.fence);
        return { ...receipt, scheduled: true };
      }
    }
    return receipt;
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
        if (obj.recipe_id && obj.recipe_id !== recipe_id) {
          const time = now();
          this.store.db
            .prepare(
              `UPDATE recipes SET archived_at=?, updated_at=?
               WHERE id=? AND archived_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM versions WHERE recipe_id=recipes.id
                 )
                 AND NOT EXISTS (
                   SELECT 1 FROM executions WHERE recipe_id=recipes.id AND id<>?
                 )`,
            )
            .run(time, time, obj.recipe_id, obj.id);
          obj.recipe_id = recipe_id;
        }
        obj.test_request = request;
        restartExecution(obj);
        this.store.save(obj);
      }
      return { execution_id, status: 'queued' as const, ...request };
    });
    if (this.auto_start) await this.recover();
    return result;
  }
  credentialNames() {
    if (this.model_header_binding) return [this.model_header_binding.name];
    const name = this.suppliedConfig?.model.api_key_env;
    if (name && isHeaderBinding(name)) return process.env[name] ? [name] : [];
    return Object.keys(process.env).filter((key) => !isHeaderBinding(key) && process.env[key]);
  }
  private check_runtime() {
    if (!this.credentialNames().includes(this.config.model.api_key_env)) {
      fail('invalid_state', 'Model headers are unavailable for this execution');
    }
  }
  private async launchWorker(
    owner: string,
    slot: string,
    executionId: string,
    recipeId?: string,
    deferred = false,
    retryFence?: number,
  ) {
    try {
      const worker = fileURLToPath(new URL('./worker.js', import.meta.url));
      if (!existsSync(worker))
        fail('invalid_state', 'build the TypeScript SDK before starting detached workers');
      const args = [worker, this.data_dir, owner, slot, executionId];
      if (deferred) args.push('--wait-for-slot');
      if (recipeId !== undefined) args.push('--recipe-id', recipeId);
      if (retryFence !== undefined) args.push('--retry-fence', String(retryFence));
      const child = spawn(this.node_executable, args, {
        detached: true,
        stdio: 'ignore',
        env: {
          ...process.env,
          ...(this.model_header_binding && {
            [this.model_header_binding.name]: this.model_header_binding.value,
          }),
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
  }
  async recover(
    recipeId?: string,
    override?: { previousHeaders?: Record<string, string>; retryKey?: string },
  ): Promise<RecoverResult> {
    if (override?.retryKey !== undefined) {
      if (recipeId === undefined || override.previousHeaders !== undefined)
        return fail('invalid_argument', 'retry_key requires recipe_id and cannot be combined with previous_headers');
      return this.retryTimedOutExecution(recipeId, override.retryKey);
    }
    if (override && this.model_header_binding) {
      this.store.stage_recovery(this.config, recipeId, override.previousHeaders);
    }
    const credentials = this.credentialNames();
    const pending = this.store.pending_execution(credentials, recipeId);
    if (!pending) return { started: 0 };
    const config = LocalConfigSchema.parse(JSON.parse(pending.config));
    const workers =
      this.model_header_binding || recipeId !== undefined
        ? Math.min(
            config.max_executions,
            this.store.pending_count(credentials, recipeId),
          )
        : config.max_executions;
    let started = 0;
    for (let i = 0; i < workers; i++) {
      const owner = uid('worker');
      const slot = this.store.reserve(
        'worker',
        owner,
        config.max_executions,
        config.lease_seconds,
        true,
      );
      if (!slot) break;
      await this.launchWorker(owner, slot, pending.id, recipeId);
      started++;
    }
    if (started) return { started };

    const owner = uid('worker');
    const credential = this.model_header_binding?.name;
    const recoveryScope = recipeId === undefined
      ? credential
      : `${credential ?? 'legacy'}:recipe:${recipeId}`;
    const recoveryKind = recoveryScope ? `recovery:${recoveryScope}` as const : 'recovery';
    const deferred = this.store.tx(() =>
      this.store.has_pending(credentials, recipeId)
        ? this.store.reserve(recoveryKind, owner, 1, config.lease_seconds)
        : null,
    );
    if (!deferred) return { started: 0 };
    await this.launchWorker(owner, deferred, pending.id, recipeId, true);
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
