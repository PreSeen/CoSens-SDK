import { z } from 'zod';
import { RSIClient as QueueClient, type RecoverResult } from './client.js';
import { CloudClient, type CloudOptions, type RecipeUpsertOptions, type RecipeUpsertResult } from './cloud.js';
import {
  fail,
  RecipeReferenceSchema,
  TaskInputTextSchema,
  type EvidenceFile,
  type Execution as StoredExecution,
} from './models.js';
import { uploadLocalTaskRecipe, type LocalRecipeUpsertOptions } from './local-recipe-upload.js';
export type { LocalRecipeUpsertOptions } from './local-recipe-upload.js';
import { bindModelHeaders, credentialScopeId } from './model-headers.js';
import { readTask, readTaskSummary, readTasks } from './task.js';

const text = z.string().min(1);
const revision = z.number().int().positive();
const requestHeaders = z.record(z.string(), z.string());
const connectionValue = z.string().trim().min(1);
const localCloudConfig = z.strictObject({
  headers: requestHeaders.optional(),
  rsi_base_url: connectionValue.optional(),
});
const config = localCloudConfig.extend({
  credential_scope: z.string().trim().min(1).optional(),
  auto_start: z.boolean().optional(),
  max_tokens: z.number().int().positive().optional(),
  timeout: z.number().positive().max(3600).optional(),
});
const localExecutionOptions = z.strictObject({
  data_dir: text,
  model_id: connectionValue,
  api_key: connectionValue,
  base_url: connectionValue,
  config: config.nullish(),
});
const localReadOptions = z.strictObject({
  data_dir: text,
  config: localCloudConfig.nullish(),
});
const versionArchive = z.strictObject({
  version_id: text,
  archived: z.boolean(),
});
const evidenceFile = z.strictObject({
  path: text,
  role: z.enum(['input', 'artifact', 'reference', 'feedback']),
});
const evidence = z.strictObject({
  context_path: text,
  files: z.array(evidenceFile).max(1000),
});
const executionCreate = z.strictObject({
  evidence: evidence.extend({ input_text: TaskInputTextSchema.optional() }),
  idempotency_key: text,
  recipe_reference: RecipeReferenceSchema.optional(),
});
const answer = z.strictObject({
  question_id: text,
  question_revision: revision,
  answer: z.union([z.array(text).min(1), text]),
});
const executionListQuery = z.strictObject({ execution_ids: z.array(text) });
const taskRestartOptions = z.strictObject({
  idempotency_key: text,
  evidence: evidence.optional(),
});
const recipeListQuery = z.strictObject({ recipe_ids: z.array(text) });
const recoverOptions = z.strictObject({
  recipe_id: text.optional(),
  previous_headers: requestHeaders.optional(),
  retry_key: text.optional(),
});
const recipeTest = z.strictObject({
  execution_id: text,
  recipe_id: text,
  version_id: text,
  rubrics_version_id: text,
});

export type RSIConfig = z.input<typeof config> | null;
export type ExecutionEvidence = z.input<typeof evidence>;
export type Execution = Pick<
  StoredExecution,
  | 'id'
  | 'base_execution_id'
  | 'status'
  | 'stage'
  | 'input_revision'
  | 'progress_percent'
  | 'questions'
  | 'answers'
  | 'recipe_id'
  | 'recipe_version_id'
  | 'rubrics_version_id'
  | 'test_results'
  | 'result'
  | 'error'
  | 'created_at'
  | 'updated_at'
> & {
  name: string;
  evidence: Pick<
    EvidenceFile,
    'path' | 'locator' | 'role' | 'sha256' | 'size'
  >[];
};
export type ExecutionCreate = z.input<typeof executionCreate>;
export type Page = { offset: number; archived: boolean };
export type ExecutionListQuery = z.input<typeof executionListQuery>;
export type TaskRestartOptions = z.input<typeof taskRestartOptions>;
export type RecipeListQuery = z.input<typeof recipeListQuery>;
export type RecoverOptions = z.input<typeof recoverOptions>;
export type RecipeListEntry = {
  recipe_id: string | null;
  execution_ids: string[];
  name: string;
  active_version_id: string | null;
  created_at: number;
  updated_at: number;
};
export type RecipeListRecipeEntry = RecipeListEntry & { recipe_id: string };
export type ClarificationAnswer = z.input<typeof answer>;
export type RecipeTest = z.input<typeof recipeTest>;

/** Host API for the local RSI worker. Persisted records retain their storage format. */
export type LocalOptions = z.input<typeof localExecutionOptions> | (z.input<typeof localReadOptions> & {
  model_id?: never;
  api_key?: never;
  base_url?: never;
});
export type RSIClientOptions = LocalOptions | ({ mode: 'cloud' } & CloudOptions);

/**
 * RSI atoms in cloud mode (default) or local mode.
 *
 * `new RSIClient()` uses `COSENS_API_KEY` and `COSENS_RSI_BASE_URL`.
 * `new RSIClient({ data_dir, model_id, api_key, base_url })` runs the local
 * Worker with your own model. Both modes expose the same async methods.
 * Local Recipe reads also return a storage_path relative to data_dir.
 */
export class RSIClient {
  readonly #queue!: QueueClient;
  readonly #cloud?: CloudClient;
  readonly #uploadHeaders?: Headers;
  readonly #rsiBaseUrl?: string;
  readonly data_dir: string | null;
  readonly mode: 'cloud' | 'local';

  constructor(options?: RSIClientOptions) {
    if (options === undefined || 'mode' in options) {
      const { mode: _mode, ...cloud } = options ?? { mode: 'cloud' as const };
      this.#cloud = new CloudClient(cloud);
      this.data_dir = null;
      this.mode = 'cloud';
      return;
    }
    this.mode = 'local';
    const hasModel = 'model_id' in options || 'api_key' in options || 'base_url' in options;
    const connection = hasModel ? localExecutionOptions.parse(options) : undefined;
    const { data_dir, config: settings } = connection ?? localReadOptions.parse(options);
    this.#rsiBaseUrl = settings?.rsi_base_url;
    try {
      this.#uploadHeaders = new Headers(settings?.headers);
    } catch {
      fail('invalid_argument', 'Invalid Recipe upload headers');
    }
    const modelSettings = connection?.config;
    const scope = modelSettings?.credential_scope === undefined
      ? undefined
      : credentialScopeId(modelSettings.credential_scope);
    const binding = connection
      ? bindModelHeaders(connection.base_url, connection.model_id, { Authorization: `Bearer ${connection.api_key}` }, scope)
      : undefined;
    this.#queue = new QueueClient({
      data_dir,
      ...(connection && binding
        ? {
            config: {
              model: {
                model: connection.model_id,
                base_url: connection.base_url,
                api_key_env: binding.name,
                ...(scope === undefined ? {} : { credential_scope: scope }),
                ...(modelSettings?.max_tokens === undefined
                  ? {}
                  : { max_tokens: modelSettings.max_tokens }),
                ...(modelSettings?.timeout === undefined
                  ? {}
                  : { timeout: modelSettings.timeout }),
              },
            },
            auto_start: modelSettings?.auto_start,
            model_header_binding: binding,
          }
        : {}),
      ...(process.env.RSI_NODE_EXECUTABLE
        ? { node_executable: process.env.RSI_NODE_EXECUTABLE }
        : {}),
    });
    this.data_dir = this.#queue.data_dir;
  }

  #presentExecution(execution: StoredExecution): Execution {
    const {
      id,
      name,
      base_execution_id,
      status,
      stage,
      input_revision,
      questions,
      answers,
      recipe_id,
      recipe_version_id,
      rubrics_version_id,
      test_results,
      result,
      error,
      created_at,
      updated_at,
    } = execution;
    return {
      id,
      name: name ?? '首次学习',
      base_execution_id,
      status,
      stage,
      progress_percent: execution.progress_percent,
      input_revision,
      evidence: this.#queue
        .execution_evidence(execution)
        .map(({ path, locator, role, sha256, size }) => ({
          path,
          locator,
          role,
          sha256,
          size,
        })),
      questions,
      answers,
      recipe_id,
      recipe_version_id,
      rubrics_version_id,
      test_results,
      result,
      error,
      created_at,
      updated_at,
    };
  }

  async execution_create(request: ExecutionCreate): Promise<Execution> {
    if (this.#cloud) return this.#cloud.execution_create(request);
    const {
      evidence: { context_path, files, input_text },
      idempotency_key,
      recipe_reference,
    } = executionCreate.parse(request);
    const recipeId = recipe_reference?.recipe_id;
    const execution = await this.#queue.execution_create_with_evidence({
      evidence: {
        context_path,
        files: recipe_reference
          ? [...files, { path: recipe_reference.path, role: 'recipe' }]
          : files,
      },
      input_text,
      idempotency_key,
      confirmation_ref: idempotency_key,
      base: recipeId ? { kind: 'recipe', id: recipeId, version_id: recipe_reference?.recipe_version_id } : null,
    }, this.#uploadHeaders, this.#rsiBaseUrl);
    return {
      ...this.#presentExecution(execution),
      recipe_id: execution.recipe_id!,
    };
  }

  async execution_get(id: string): Promise<Execution> {
    if (this.#cloud) return this.#cloud.execution_get(id);
    return this.#presentExecution(this.#queue.execution_get(id));
  }

  async execution_list(query?: ExecutionListQuery): Promise<Execution[]> {
    if (this.#cloud) return this.#cloud.execution_list(query);
    const executionIds = executionListQuery
      .optional()
      .parse(query)?.execution_ids;
    return this.#queue
      .execution_list_all(executionIds)
      .map((row) => this.#presentExecution(row));
  }

  async task_list() {
    if (this.#cloud) return fail('invalid_state', 'task_list requires local mode');
    return readTasks(this.#queue.store);
  }

  async task_get(taskId: string) {
    if (this.#cloud) return fail('invalid_state', 'task_get requires local mode');
    return readTask(this.#queue.store, text.parse(taskId));
  }

  async task_restart(taskId: string, options: TaskRestartOptions) {
    if (this.#cloud) return fail('invalid_state', 'task_restart requires local mode');
    const id = text.parse(taskId);
    const { idempotency_key, evidence } = taskRestartOptions.parse(options);
    const executionId = await this.#queue.task_restart(id, idempotency_key, evidence);
    return readTaskSummary(this.#queue.store, id, executionId);
  }

  async execution_append_evidence(
    id: string,
    submission: z.input<typeof evidence>,
  ): Promise<Execution> {
    if (this.#cloud) return this.#cloud.execution_append_evidence(id, submission);
    return this.#presentExecution(
      await this.#queue.execution_append_evidence(
        id,
        evidence.parse(submission),
      ),
    );
  }

  async execution_cancel(id: string): Promise<Execution> {
    if (this.#cloud) return this.#cloud.execution_cancel(id);
    return this.#presentExecution(this.#queue.execution_cancel(id));
  }

  async execution_clarification_answer(id: string, response: ClarificationAnswer) {
    if (this.#cloud) return this.#cloud.execution_clarification_answer(id, response);
    const { answer: value, ...question } = answer.parse(response);
    return this.#queue.execution_clarification_answer(id, {
      ...question,
      ...(typeof value === 'string'
        ? { text: value, selected_option_ids: [] }
        : { selected_option_ids: value }),
    });
  }

  async rubrics_get(recipeId: string, recipeVersionId: string): Promise<ReturnType<QueueClient['rubrics_get']>> {
    if (this.#cloud) return this.#cloud.rubrics_get(recipeId, recipeVersionId);
    return this.#queue.recipe_version_rubrics_get(
      text.parse(recipeId),
      text.parse(recipeVersionId),
    );
  }

  async recipe_list(query?: RecipeListQuery): Promise<RecipeListEntry[]> {
    if (this.#cloud) return this.#cloud.recipe_list(query);
    const recipeIds = recipeListQuery.optional().parse(query)?.recipe_ids;
    return this.#queue.recipe_list_all(recipeIds).map((row) => ({
      recipe_id: row.recipe_id,
      execution_ids: row.execution_ids,
      name: row.name ?? '',
      active_version_id: row.active_version_id ?? null,
      created_at: row.created_at,
      updated_at: row.updated_at,
    }));
  }

  recipes_upsert(options: LocalRecipeUpsertOptions): Promise<RecipeUpsertResult>;
  recipes_upsert(path: string | URL, options?: RecipeUpsertOptions): Promise<RecipeUpsertResult>;
  async recipes_upsert(request: string | URL | LocalRecipeUpsertOptions, options?: RecipeUpsertOptions) {
    if (this.#cloud) {
      if (typeof request !== 'string' && !(request instanceof URL))
        return fail('invalid_argument', 'cloud Recipe upload requires a ZIP path');
      return this.#cloud.recipes_upsert(request, options);
    }
    if (!request || typeof request !== 'object' || request instanceof URL || options !== undefined)
      return fail('invalid_argument', 'local Recipe upload requires { task_id }');
    return uploadLocalTaskRecipe(this.#queue.store, request, this.#uploadHeaders, this.#rsiBaseUrl);
  }

  async recipe_get(id: string, version = 'active') {
    if (this.#cloud) return this.#cloud.recipe_get(id, version);
    return this.#queue.recipe_get(
      id,
      version === 'active' || version === 'latest'
        ? { version_selector: version }
        : { version_id: text.parse(version) },
    );
  }

  async recipe_download(id: string, recipeVersionId: string) {
    if (!this.#cloud) return fail('invalid_state', 'recipe_download requires cloud mode');
    return this.#cloud.recipe_download(id, recipeVersionId);
  }

  async recipe_test(request: RecipeTest) {
    if (this.#cloud) return this.#cloud.recipe_test(request);
    return this.#queue.recipe_test(recipeTest.parse(request));
  }

  async recipe_version_set_archived(
    id: string,
    request: { version_id: string; archived: boolean },
  ) {
    if (this.#cloud) return this.#cloud.recipe_version_set_archived(id, request);
    const { version_id, archived } = versionArchive.parse(request);
    return this.#queue.recipe_version_set_archived(
      text.parse(id),
      version_id,
      archived,
    );
  }

  async recover(options?: RecoverOptions): Promise<RecoverResult> {
    const { recipe_id, previous_headers, retry_key } =
      recoverOptions.optional().parse(options) ?? {};
    if (this.#cloud) {
      if (retry_key !== undefined) fail('invalid_state', 'recover retry_key requires local mode');
      return { started: 0 };
    }
    return this.#queue.recover(recipe_id, { previousHeaders: previous_headers, retryKey: retry_key });
  }

  close() {
    if (!this.#cloud) this.#queue.close();
  }
}
