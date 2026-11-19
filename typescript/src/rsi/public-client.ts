import { z } from 'zod';
import { RSIClient as QueueClient } from './client.js';
import type { EvidenceFile, Execution as StoredExecution } from './models.js';

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
    auto_start: z.boolean(),
  })
  .nullable();
const clientOptions = z.strictObject({ data_dir: text, config });
const versionArchive = z.strictObject({
  version_id: text,
  archived: z.boolean(),
});
const evidence = z.strictObject({
  context_path: text,
  files: z
    .array(
      z.strictObject({
        path: text,
        role: z.enum(['input', 'artifact', 'reference', 'feedback']),
      }),
    )
    .max(1000),
});
const executionCreate = z.strictObject({
  evidence,
  idempotency_key: text,
  base_execution_id: text.nullable(),
});
const answer = z.strictObject({
  question_id: text,
  question_revision: revision,
  answer: z.union([z.array(text).min(1), text]),
});
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
export type ClarificationAnswer = z.input<typeof answer>;
export type RecipeTest = z.input<typeof recipeTest>;

/** Host API for the local RSI worker. Persisted records retain their storage format. */
export class RSIClient {
  readonly #queue: QueueClient;
  readonly data_dir: string;

  constructor(options: { data_dir: string; config: RSIConfig }) {
    const { data_dir, config: settings } = clientOptions.parse(options);
    this.#queue = new QueueClient({
      data_dir,
      ...(settings
        ? {
            config: {
              model: { model: settings.model, base_url: settings.base_url },
            },
            auto_start: settings.auto_start,
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

  async execution_create(request: ExecutionCreate) {
    const { base_execution_id, ...submission } = executionCreate.parse(request);
    return this.#presentExecution(
      await this.#queue.execution_create_with_evidence({
        ...submission,
        confirmation_ref: submission.idempotency_key,
        base: base_execution_id
          ? { kind: 'execution', id: base_execution_id }
          : null,
      }),
    );
  }

  execution_get(id: string) {
    return this.#presentExecution(this.#queue.execution_get(id));
  }

  execution_list(query: Page) {
    return this.#queue
      .execution_list(page.parse(query))
      .map((row) => this.#presentExecution(row));
  }

  async execution_append_evidence(id: string, submission: ExecutionEvidence) {
    return this.#presentExecution(
      await this.#queue.execution_append_evidence(
        id,
        evidence.parse(submission),
      ),
    );
  }

  execution_cancel(id: string) {
    return this.#presentExecution(this.#queue.execution_cancel(id));
  }

  execution_clarification_answer(id: string, response: ClarificationAnswer) {
    const { answer: value, ...question } = answer.parse(response);
    return this.#queue.execution_clarification_answer(id, {
      ...question,
      ...(typeof value === 'string'
        ? { text: value, selected_option_ids: [] }
        : { selected_option_ids: value }),
    });
  }

  rubrics_list(query: Page) {
    return this.#queue.rubrics_list(page.parse(query));
  }

  rubrics_get(id: string, version: string) {
    return this.#queue.rubrics_get(
      id,
      version === 'latest' ? {} : { version_id: text.parse(version) },
    );
  }

  recipe_list() {
    return this.#queue.recipe_list_all();
  }

  recipe_get(id: string, version: string) {
    return this.#queue.recipe_get(
      id,
      version === 'active' || version === 'latest'
        ? { version_selector: version }
        : { version_id: text.parse(version) },
    );
  }

  recipe_test(request: RecipeTest) {
    return this.#queue.recipe_test(recipeTest.parse(request));
  }

  recipe_version_set_archived(
    id: string,
    request: { version_id: string; archived: boolean },
  ) {
    const { version_id, archived } = versionArchive.parse(request);
    return this.#queue.recipe_version_set_archived(
      text.parse(id),
      version_id,
      archived,
    );
  }

  recover() {
    return this.#queue.recover();
  }

  close() {
    this.#queue.close();
  }
}
