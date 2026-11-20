import { z } from 'zod';
import { RSIClient as QueueClient } from './client.js';
import { CloudClient, type CloudOptions } from './cloud.js';
import {
  RecipeReferenceSchema,
  type EvidenceFile,
  type Execution as StoredExecution,
} from './models.js';
import { readRecipeFile } from './recipe-file.js';
import { bindModelHeaders } from './model-headers.js';
import type { RecipeListStatus } from './recipe-list.js';

const text = z.string().min(1);
const page = z.strictObject({
  offset: z.number().int().nonnegative(),
  archived: z.boolean(),
});
const revision = z.number().int().positive();
const config = z
  .strictObject({
    model: text,
    base_url: text,
    headers: z.record(z.string(), z.string()).optional(),
    auto_start: z.boolean(),
    max_tokens: z.number().int().positive().optional(),
    timeout: z.number().positive().max(3600).optional(),
  })
  .nullable();
const clientOptions = z.strictObject({ data_dir: text, config });
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
  evidence,
  idempotency_key: text,
  recipe_reference: RecipeReferenceSchema.optional(),
});
const answer = z.strictObject({
  question_id: text,
  question_revision: revision,
  answer: z.union([z.array(text).min(1), text]),
});
const executionListQuery = z.strictObject({ execution_ids: z.array(text) });
const recipeListQuery = z.strictObject({ recipe_ids: z.array(text) });
const recipeTest = z.strictObject({
  execution_id: text,
  recipe_id: text,
  version_id: text,
  rubrics_version_id: text,
});

export type RSIConfig = z.input<typeof config>;
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
export type Page = z.input<typeof page>;
export type ExecutionListQuery = z.input<typeof executionListQuery>;
export type RecipeListQuery = z.input<typeof recipeListQuery>;
export type RecipeListEntry = {
  recipe_id: string | null;
  execution_ids: string[];
  name: string;
  status: RecipeListStatus;
  available_version_id: string | null;
  created_at: number;
  updated_at: number;
};
export type RecipeListRecipeEntry = RecipeListEntry & { recipe_id: string };
export type ClarificationAnswer = z.input<typeof answer>;
export type RecipeTest = z.input<typeof recipeTest>;

/** Host API for the local RSI worker. Persisted records retain their storage format. */
export type LocalOptions = { data_dir: string; config: RSIConfig };
export type RSIClientOptions = LocalOptions | ({ mode: 'cloud' } & CloudOptions);

/**
 * RSI atoms in cloud mode (default) or local mode.
 *
 * `new RSIClient()` uses the CoSens service with `COSENS_API_KEY`.
 * `new RSIClient({ data_dir, config })` runs the local Worker with your own
 * model. Both modes expose the same async methods. Local Recipe reads also
 * return a storage_path relative to data_dir.
 */
export class RSIClient {
  readonly #queue!: QueueClient;
  readonly #cloud?: CloudClient;
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
    const { data_dir, config: settings } = clientOptions.parse(options);
    const binding = settings && bindModelHeaders(
      settings.base_url, settings.model, settings.headers ?? {},
    );
    this.#queue = new QueueClient({
      data_dir,
      ...(settings && binding
        ? {
            config: {
              model: {
                model: settings.model,
                base_url: settings.base_url,
                api_key_env: binding.name,
                ...(settings.max_tokens === undefined
                  ? {}
                  : { max_tokens: settings.max_tokens }),
                ...(settings.timeout === undefined
                  ? {}
                  : { timeout: settings.timeout }),
              },
            },
            auto_start: settings.auto_start,
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
      evidence: { context_path, files },
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
      idempotency_key,
      confirmation_ref: idempotency_key,
      base: recipeId ? { kind: 'recipe', id: recipeId } : null,
    });
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

  async rubrics_list(query: Page) {
    if (this.#cloud) return this.#cloud.rubrics_list(query);
    return this.#queue.rubrics_list(page.parse(query));
  }

  async rubrics_get(id: string, version: string) {
    if (this.#cloud) return this.#cloud.rubrics_get(id, version);
    return this.#queue.rubrics_get(
      id,
      version === 'latest' ? {} : { version_id: text.parse(version) },
    );
  }

  async recipe_list(query?: RecipeListQuery): Promise<RecipeListEntry[]> {
    if (this.#cloud) return this.#cloud.recipe_list(query);
    const recipeIds = recipeListQuery.optional().parse(query)?.recipe_ids;
    return this.#queue.recipe_list_all(recipeIds).map((row) => ({
      recipe_id: row.recipe_id,
      execution_ids: row.execution_ids,
      name: row.name ?? '',
      status: row.status,
      available_version_id: row.available_version_id,
      created_at: row.created_at,
      updated_at: row.updated_at,
    }));
  }

  async recipe_insert(path: string) {
    if (this.#cloud) return this.#cloud.recipe_insert(path);
    const { content, markdown } = await readRecipeFile(path);
    return this.#queue.recipe_insert(content, markdown);
  }

  async recipe_get(id: string, version: string) {
    if (this.#cloud) return this.#cloud.recipe_get(id, version);
    return this.#queue.recipe_get(
      id,
      version === 'active' || version === 'latest'
        ? { version_selector: version }
        : { version_id: text.parse(version) },
    );
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

  async recover() {
    if (this.#cloud) return { started: 0 };
    return this.#queue.recover();
  }

  close() {
    if (!this.#cloud) this.#queue.close();
  }
}
