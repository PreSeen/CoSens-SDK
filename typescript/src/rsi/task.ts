import { createHash, randomUUID } from 'node:crypto';
import {
  closeSync, constants, fstatSync, lstatSync, mkdirSync, openSync, readSync, renameSync, rmSync, writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
import { MAX_RECIPE_ZIP_BYTES, type RecipeUpsertResult } from './cloud.js';
import { fail, RSIError, type Execution, type ExecutionAnswer, type Question, type Recipe } from './models.js';
import { recipeIdentityScope, uploadReceiptScope } from './recipe-identity.js';
import { buildRecipePackage, readRecipePackage } from './recipe-package.js';
import type { Store } from './store.js';

export type TaskSummary = {
  task_id: string;
  execution_id: string;
  name: string;
  status: Execution['status'];
  stage: string;
  progress_percent: number;
  created_at: number;
  updated_at: number;
  pending_question_count: number;
  test_status: NonNullable<Execution['result']>['test_status'] | null;
};

export type TaskQuestion = {
  execution_id: string;
  question_id: string;
  question_revision: number;
  question: string;
  options: Question['options'];
};

export type TaskClarification = TaskQuestion & {
  answer: Omit<ExecutionAnswer, 'continuation_execution_id'> | null;
};

export type TaskArtifact = {
  zip_path: string;
  local_recipe_id: string;
  local_recipe_version_id: string;
};

export type Task = TaskSummary & {
  execution_ids: string[];
  pending_questions: TaskQuestion[];
  clarification_history: TaskClarification[];
  result: Execution['result'];
  error: Execution['error'];
  artifact: TaskArtifact | null;
  recipe_id: string | null;
  recipe_version_id: string | null;
};

function continuationIds(execution: Execution) {
  return [execution.continuation_execution_id,
    ...Object.values(execution.answers).map((answer) => answer.continuation_execution_id)];
}

function executionLinks(store: Store) {
  const executions = store.listExecutions(undefined, true);
  const byId = new Map(executions.map((execution) => [execution.id, execution]));
  const next = new Map<string, Execution>();
  const successors = new Set<string>();
  for (const execution of executions) {
    for (const id of continuationIds(execution)) {
      if (!id) continue;
      const child = byId.get(id);
      if (child?.base_execution_id !== execution.id) continue;
      next.set(execution.id, child);
      successors.add(id);
      break;
    }
  }
  const roots = executions.filter((execution) => !successors.has(execution.id));
  return { roots, next };
}

function chain(root: Execution, next: Map<string, Execution>) {
  const run = [root];
  const seen = new Set([root.id]);
  for (let child = next.get(root.id); child; child = next.get(child.id)) {
    if (seen.has(child.id)) return fail('invalid_state', 'Task execution chain contains a cycle');
    run.push(child);
    seen.add(child.id);
  }
  return run;
}

export function readTaskChain(store: Store, taskId: string) {
  const { roots, next } = executionLinks(store);
  const root = roots.find(({ id }) => id === taskId);
  if (!root) return fail('not_found', 'task not found');
  return chain(root, next);
}

const questionKey = (question: Question) => `${question.id}:${question.revision}`;

function pendingQuestions(execution: Execution) {
  if (execution.status === 'failed' || execution.status === 'cancelled') return [];
  return execution.questions.filter((question) => question.revision === execution.input_revision
    && !execution.answers[questionKey(question)]);
}

function taskQuestion(execution: Execution, question: Question): TaskQuestion {
  return {
    execution_id: execution.id,
    question_id: question.id,
    question_revision: question.revision,
    question: question.question,
    options: question.options,
  };
}

function summarizeTask(store: Store, root: Execution, current: Execution): TaskSummary {
  return {
    task_id: root.id,
    execution_id: current.id,
    name: store.executionRecipeName(current) ?? '首次学习',
    status: current.status,
    stage: current.stage,
    progress_percent: current.progress_percent,
    created_at: root.created_at,
    updated_at: current.updated_at,
    pending_question_count: pendingQuestions(current).length,
    test_status: current.result?.test_status ?? null,
  };
}

export function readTasks(store: Store): TaskSummary[] {
  const { roots, next } = executionLinks(store);
  return roots.reverse().filter((root) => root.archived_at == null)
    .map((root) => summarizeTask(store, root, chain(root, next).at(-1)!));
}

export function readTaskSummary(store: Store, taskId: string, executionId: string): TaskSummary {
  const executions = readTaskChain(store, taskId);
  const execution = executions.find(({ id }) => id === executionId);
  if (!execution) return fail('not_found', 'task execution not found');
  return summarizeTask(store, executions[0], execution);
}

export function hasApprovedResult(execution: Execution): boolean {
  return execution.status === 'completed' && execution.result?.test_status === 'passed'
    && execution.result.published === true && execution.result.provisional === false
    && execution.recipe_id != null && execution.recipe_version_id != null;
}

export const taskRecipePackageScope = 'task_recipe_package';

type UploadedVersion = { zipSha256: string; result: RecipeUpsertResult };

function uploadedVersion(store: Store, execution: Execution): UploadedVersion | undefined {
  if (!hasApprovedResult(execution) || !execution.recipe_version_id) return;
  const receipt = store.db.prepare('SELECT request,result FROM idempotency WHERE scope=? AND key=?')
    .get(uploadReceiptScope, store.resolveVersionId(execution.recipe_version_id)) as
      { request: string; result: string } | undefined;
  if (receipt) return { zipSha256: JSON.parse(receipt.request).zip_sha256, result: JSON.parse(receipt.result) };
}

const digest = (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');

function readFrozenPackage(path: string): Buffer | null {
  const directory = lstatSync(dirname(path), { throwIfNoEntry: false });
  if (!directory) return null;
  if (!directory.isDirectory()) return fail('invalid_state', 'invalid Task package directory');
  const stat = lstatSync(path, { throwIfNoEntry: false });
  if (!stat) return null;
  if (!stat.isFile() || stat.size > MAX_RECIPE_ZIP_BYTES)
    return fail('invalid_state', 'invalid Task Recipe ZIP file');
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const opened = fstatSync(descriptor);
    if (!opened.isFile() || opened.dev !== stat.dev || opened.ino !== stat.ino || opened.size !== stat.size)
      return fail('invalid_state', 'the frozen Task Recipe ZIP has changed');
    const bytes = Buffer.alloc(stat.size + 1);
    let offset = 0;
    while (offset < bytes.length) {
      const count = readSync(descriptor, bytes, offset, bytes.length - offset, offset);
      if (!count) break;
      offset += count;
    }
    if (offset !== stat.size) return fail('invalid_state', 'the frozen Task Recipe ZIP has changed');
    return bytes.subarray(0, offset);
  } finally {
    closeSync(descriptor);
  }
}

function taskArtifact(store: Store, taskId: string, execution: Execution, upload?: UploadedVersion): TaskArtifact | null {
  if (!hasApprovedResult(execution) || !execution.recipe_id || !execution.recipe_version_id) return null;
  const recipe = store.get<Recipe>(execution.recipe_id, 'recipe');
  const version = store.version(recipe.id, execution.recipe_version_id);
  const directory = join(store.root, recipe.storage_path, version.id);
  if (!/^[A-Za-z0-9_-]+$/u.test(taskId)) return fail('invalid_state', 'invalid Task package identity');
  const packages = join(store.root, 'task-packages');
  const packagesStat = lstatSync(packages, { throwIfNoEntry: false });
  if (packagesStat && !packagesStat.isDirectory()) return fail('invalid_state', 'invalid Task package directory');
  const zip_path = join(packages, `${taskId}.zip`);
  const request = { execution_id: execution.id };
  const saved = store.replay<{ sha256: string; version_id: string }>(taskRecipePackageScope, taskId, request);
  const cached = saved || upload ? readFrozenPackage(zip_path) : null;
  const frozen = cached && (saved || digest(cached) === upload?.zipSha256) ? cached : null;
  const bytes = frozen ?? buildRecipePackage(directory);
  const sha256 = digest(bytes);
  if (saved && (saved.sha256 !== sha256 || store.resolveVersionId(saved.version_id) !== version.id))
    return fail('invalid_state', 'the frozen Task Recipe ZIP has changed');
  if (upload && upload.zipSha256 !== sha256) return null;
  if (!saved && frozen) readRecipePackage(bytes);
  if (!frozen) {
    mkdirSync(packages, { recursive: true, mode: 0o700 });
    const temporary = `${zip_path}.${randomUUID()}.tmp`;
    try {
      writeFileSync(temporary, bytes, { flag: 'wx', mode: 0o600, flush: true });
      renameSync(temporary, zip_path);
    } finally {
      rmSync(temporary, { force: true });
    }
  }
  if (!saved) store.remember(taskRecipePackageScope, taskId, request, { sha256, version_id: version.id });
  return { zip_path, local_recipe_id: recipe.id, local_recipe_version_id: version.id };
}

function availableArtifact(store: Store, taskId: string, execution: Execution, upload?: UploadedVersion) {
  try {
    return taskArtifact(store, taskId, execution, upload);
  } catch (error) {
    // Older uploads may have no readable local copy of the exact published archive.
    if (!upload) throw error;
    return null;
  }
}

export function freezeTaskArtifact(store: Store, execution: Execution) {
  if (!hasApprovedResult(execution)) return;
  let root = execution;
  const seen = new Set([root.id]);
  while (root.base_execution_id) {
    let parent: Execution;
    try {
      parent = store.get<Execution>(root.base_execution_id, 'execution');
    } catch (error) {
      if (error instanceof RSIError && error.code === 'not_found') break;
      throw error;
    }
    if (!continuationIds(parent).includes(root.id)) break;
    if (seen.has(parent.id)) return fail('invalid_state', 'Task execution chain contains a cycle');
    seen.add(parent.id);
    root = parent;
  }
  availableArtifact(store, root.id, execution, uploadedVersion(store, execution));
}

function presentAnswer(recorded: ExecutionAnswer) {
  const { continuation_execution_id: _next, ...answer } = recorded;
  return answer;
}

export function readTask(store: Store, taskId: string): Task {
  return store.tx(() => {
    const executions = readTaskChain(store, taskId);
    const current = executions.at(-1)!;
    const history = new Map<string, TaskClarification>();
    for (const execution of executions) {
      for (const question of [...(execution.question_history ?? []), ...execution.questions]) {
        const key = questionKey(question);
        if (history.has(key)) continue;
        const recorded = current.answers[key] ?? execution.answers[key];
        history.set(key, {
          ...taskQuestion(execution, question),
          answer: recorded ? presentAnswer(recorded) : null,
        });
      }
      for (const [key, answer] of Object.entries(execution.answers)) {
        if (history.has(key)) continue;
        const suffix = `:${answer.question_revision}`;
        if (!key.endsWith(suffix)) continue;
        history.set(key, {
          execution_id: execution.id,
          question_id: key.slice(0, -suffix.length),
          question_revision: answer.question_revision,
          question: answer.question,
          options: answer.selected_options,
          answer: presentAnswer(answer),
        });
      }
    }
    const upload = uploadedVersion(store, current);
    const artifact = availableArtifact(store, taskId, current, upload);
    const formal = upload?.result;
    const knownId = current.recipe_id && store.resolveRecipeId(current.recipe_id);
    const linked = knownId && store.db.prepare('SELECT 1 FROM idempotency WHERE scope=? AND key=?')
      .get(recipeIdentityScope, knownId);
    return {
      ...summarizeTask(store, executions[0], current),
      execution_ids: executions.map(({ id }) => id),
      pending_questions: pendingQuestions(current).map((question) => taskQuestion(current, question)),
      clarification_history: [...history.values()],
      result: current.result,
      error: current.error,
      artifact,
      recipe_id: formal?.recipe_id ?? (linked ? knownId : null),
      recipe_version_id: formal?.recipe_version_id ?? null,
    };
  });
}
