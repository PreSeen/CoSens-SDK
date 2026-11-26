import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { z } from 'zod';
import {
  CloudClient,
  matchesRecipeCloudDestination,
  openRecipeZip,
  recipeCloudBaseAliases,
  recipeCloudDestination,
  uploadRecipeZip,
  validateRecipeUpsertOptions,
  type RecipeCloudDestination,
  type RecipeUpsertResult,
} from './cloud.js';
import { fail, RSIError, type Recipe, type RecipeContent, type Version } from './models.js';
import { installRecipeVersionFiles } from './recipe-bootstrap.js';
import {
  adoptRecipeIdentity,
  assertCloudRecipeTargetAvailable,
  assertRecipeIdle,
  pendingUploadScope,
  recipeIdentityScope,
  taskUploadReceiptScope,
  uploadReceiptScope,
} from './recipe-identity.js';
import { encode, now, type Store } from './store.js';
import { assertRecipePackageMatches } from './recipe-package.js';
import { hasApprovedResult, readTaskChain, taskArtifact, taskRecipePackageScope } from './task.js';

const nonempty = z.string().min(1);
const destinationOptions = {
  idempotency_key: nonempty.optional(),
  api_key: nonempty.optional(),
  base_url: nonempty.optional(),
  virtualUserId: nonempty.optional(),
};
const localOptionsSchema = z.strictObject({
  task_id: nonempty,
  recipe_id: nonempty.optional(),
  idempotency_key: nonempty.optional(),
});
export type LocalRecipeUpsertOptions = z.input<typeof localOptionsSchema>;
const versionOptionsSchema = z.strictObject({
  local_recipe_id: nonempty,
  local_recipe_version_id: nonempty,
  recipe_id: nonempty.optional(),
  ...destinationOptions,
});
const identifier = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/);
const receiptSchema = z.object({
  recipe_id: identifier,
  recipe_version_id: identifier,
});
const terminalUploadStatuses = new Set([400, 403, 404, 409, 413, 422]);

type Destination = RecipeCloudDestination;
type UploadRequest = Destination & {
  local_recipe_id: string;
  local_recipe_version_id: string;
  zip_sha256: string;
  idempotency_key: string | undefined;
};
type TaskSelection = { task_id: string; execution_id: string };
type UploadPlan = UploadRequest & Partial<TaskSelection> & { cloud_recipe_id: string | null; idempotency_key: string };
type Receipt = { receipt: RecipeUpsertResult };
type PreparedUpload = Receipt | {
  plan: UploadPlan;
  cloud: CloudClient;
  pendingKey: string;
};
type PreparedLocalUpload = Receipt | {
  path: string;
  ids: Pick<UploadRequest, 'local_recipe_id' | 'local_recipe_version_id'>;
  task: TaskSelection;
};

function journal<Request, Result = Record<string, never>>(store: Store, scope: string, key: string) {
  const row = store.db.prepare('SELECT request,result FROM idempotency WHERE scope=? AND key=?')
    .get(scope, key) as { request: string; result: string } | undefined;
  if (!row) return;
  return { request: JSON.parse(row.request) as Request, result: JSON.parse(row.result) as Result };
}

function sameUploadSelection(store: Store, recorded: UploadPlan, request: Omit<UploadRequest, 'zip_sha256'>) {
  return store.resolveRecipeId(recorded.local_recipe_id) === store.resolveRecipeId(request.local_recipe_id)
    && store.resolveVersionId(recorded.local_recipe_version_id) === store.resolveVersionId(request.local_recipe_version_id)
    && (request.idempotency_key === undefined || recorded.idempotency_key === request.idempotency_key)
    && matchesRecipeCloudDestination(recorded, request);
}

function sameUpload(store: Store, recorded: UploadPlan, request: UploadRequest) {
  return recorded.zip_sha256 === request.zip_sha256 && sameUploadSelection(store, recorded, request);
}

function publishedUpload(store: Store, versionId: string, task?: TaskSelection) {
  const taskReceipt = task && journal<UploadPlan, RecipeUpsertResult>(store, taskUploadReceiptScope, task.task_id);
  return taskReceipt ?? journal<UploadPlan, RecipeUpsertResult>(store, uploadReceiptScope, versionId);
}

function matchesPendingTarget(plan: UploadPlan, recipeId?: string, task?: TaskSelection) {
  return (recipeId === undefined || recipeId === plan.cloud_recipe_id)
    && (plan.task_id === undefined || plan.task_id === task?.task_id)
    && (plan.execution_id === undefined || plan.execution_id === task?.execution_id);
}

function resolveUploadTarget(
  store: Store, recipeId: string, destination: Destination, requested?: string, pending?: UploadPlan, task?: TaskSelection,
) {
  const identity = journal<Destination>(store, recipeIdentityScope, recipeId);
  if (identity && !matchesRecipeCloudDestination(identity.request, destination))
    return fail('conflict', 'local Recipe was synchronized to a different cloud endpoint or Virtual User');
  if (identity && requested !== undefined && requested !== recipeId)
    return fail('conflict', 'local Recipe is already linked to a different cloud Recipe');
  const id = pending ? pending.cloud_recipe_id : (requested ?? (identity ? recipeId : null));
  const existing = task && id && id !== recipeId
    ? store.db.prepare('SELECT * FROM recipe_records WHERE id=?').get(id) as Recipe | undefined
    : undefined;
  if (pending && Boolean(existing) !== Boolean(pending.task_id))
    return fail('conflict', 'local Recipe upload target changed while pending');
  assertCloudRecipeTargetAvailable(store, recipeId, recipeId);
  if (id !== null) assertCloudRecipeTargetAvailable(store, id, recipeId, Boolean(existing));
  if (existing) {
    const linked = journal<Destination>(store, recipeIdentityScope, existing.id);
    if (linked && !matchesRecipeCloudDestination(linked.request, destination))
      return fail('conflict', 'target Recipe was synchronized to a different cloud endpoint or Virtual User');
    if (existing.archived_at !== null) return fail('invalid_state', 'target Recipe is archived');
    assertRecipeIdle(store, existing.id);
  }
  return { id, link: existing && task ? { recipe: existing, task } : undefined };
}

function assertAvailableTaskUploadKey(store: Store, taskId: string, request: UploadRequest) {
  if (request.idempotency_key === undefined) return;
  const used = store.db.prepare(`
    SELECT 1 FROM idempotency WHERE scope IN (?,?,?)
      AND json_extract(request, '$.base_url') IN (?,?) AND json_extract(request, '$.virtual_user_id') IS ?
      AND json_extract(request, '$.idempotency_key')=? AND json_extract(request, '$.task_id') IS NOT ?
    LIMIT 1
  `).get(pendingUploadScope, taskUploadReceiptScope, uploadReceiptScope,
    ...recipeCloudBaseAliases(request.base_url), request.virtual_user_id, request.idempotency_key, taskId);
  if (used) fail('conflict', 'idempotency key is already used by another Recipe upload');
}

function persistTaskAssociation(
  store: Store, target: Recipe, taskId: string, source: Version<RecipeContent>, plan: UploadPlan,
  receipt: RecipeUpsertResult, bytes: Uint8Array,
) {
  const id = receipt.recipe_version_id;
  if (store.resolveVersionId(id) !== id || store.db.prepare('SELECT 1 FROM versions WHERE id=?').get(id))
    return fail('conflict', 'cloud Recipe version ID is already used locally');
  const { number } = store.db.prepare('SELECT COALESCE(MAX(number),0)+1 AS number FROM versions WHERE recipe_id=?')
    .get(target.id) as { number: number };
  const version: Version<RecipeContent> = {
    id, object_id: target.id, number, content: source.content, created_at: now(),
    provenance: { source: 'cloud', package_sha256: plan.zip_sha256 },
  };
  installRecipeVersionFiles(store, target, id, bytes);
  // A published target head replaces the previous available version. Draft candidates stay independent.
  store.db.prepare("UPDATE versions SET status='archived' WHERE recipe_id=? AND status='available'").run(target.id);
  store.db.prepare("INSERT INTO versions(id,recipe_id,number,data,status) VALUES(?,?,?,?,'available')")
    .run(id, target.id, number, encode(version));
  store.save(target);
  store.remember(taskUploadReceiptScope, taskId, plan, receipt);
  store.db.prepare('UPDATE recipes SET recipe_id=?,recipe_version_id=? WHERE id=?')
    .run(target.id, id, taskId);
  store.remember(uploadReceiptScope, id, { ...plan, local_recipe_id: target.id, local_recipe_version_id: id }, receipt);
}

function clearPending(store: Store, pendingKey: string, plan: UploadPlan) {
  const pending = journal<UploadPlan>(store, pendingUploadScope, pendingKey);
  if (pending && encode(pending.request) === encode(plan))
    store.db.prepare('DELETE FROM idempotency WHERE scope=? AND key=?')
      .run(pendingUploadScope, pendingKey);
}

function uploadFailedPermanently(error: unknown) {
  const status = error instanceof RSIError
    ? error.status
    : error instanceof TypeError && 'status' in error ? error.status : undefined;
  return typeof status === 'number' && terminalUploadStatuses.has(status);
}

export async function uploadLocalTaskRecipe(
  store: Store,
  options: LocalRecipeUpsertOptions,
  uploadHeaders?: Headers,
): Promise<RecipeUpsertResult> {
  const { task_id, recipe_id, idempotency_key } = localOptionsSchema.parse(options);
  validateRecipeUpsertOptions({ recipe_id, idempotency_key });
  const destination = recipeCloudDestination({}, uploadHeaders);
  const prepared = store.tx<PreparedLocalUpload>(() => {
    const execution = readTaskChain(store, task_id).at(-1)!;
    if (!hasApprovedResult(execution))
      return fail('invalid_state', 'Task has no approved Recipe version to upload');
    const ids = {
      local_recipe_id: store.resolveRecipeId(execution.recipe_id),
      local_recipe_version_id: store.resolveVersionId(execution.recipe_version_id),
    };
    const task = { task_id, execution_id: execution.id };
    const selection = { ...ids, ...destination, idempotency_key };
    const saved = publishedUpload(store, ids.local_recipe_version_id, task);
    if (saved) {
      if (!sameUploadSelection(store, saved.request, selection)
          || (recipe_id !== undefined && recipe_id !== saved.result.recipe_id))
        return fail('conflict', 'local Recipe version was uploaded with different arguments');
      return { receipt: saved.result };
    }
    if (!uploadHeaders || uploadHeaders.keys().next().done)
      return fail('invalid_state', 'configure CoSens authentication in RSIClient config.headers to upload a local Recipe');
    const pending = journal<UploadPlan>(store, pendingUploadScope, ids.local_recipe_id);
    if (pending && (!sameUploadSelection(store, pending.request, selection)
        || !matchesPendingTarget(pending.request, recipe_id, task)))
      return fail('conflict', 'another upload is pending for this local Recipe');
    resolveUploadTarget(store, ids.local_recipe_id, destination, recipe_id, pending?.request, task);
    const artifact = taskArtifact(store, task_id, execution, pending?.request.zip_sha256);
    if (!artifact && pending) return fail('conflict', 'pending Recipe upload requires the original frozen ZIP');
    if (!artifact) return fail('invalid_state', 'Task has no frozen Recipe ZIP to upload');
    return { path: artifact.zip_path, ids, task };
  });
  if ('receipt' in prepared) return prepared.receipt;
  return uploadLocalRecipe(store, prepared.path, {
    ...prepared.ids, recipe_id, idempotency_key, base_url: destination.base_url,
  }, uploadHeaders, prepared.task);
}

export async function uploadLocalRecipe(
  store: Store,
  path: string | URL,
  options: z.input<typeof versionOptionsSchema>,
  uploadHeaders?: Headers,
  task?: TaskSelection,
): Promise<RecipeUpsertResult> {
  const { local_recipe_id, local_recipe_version_id, recipe_id, idempotency_key, ...cloudOptions } = versionOptionsSchema.parse(options);
  validateRecipeUpsertOptions({ recipe_id, idempotency_key });
  const destination = recipeCloudDestination(cloudOptions, uploadHeaders);
  const { file, filename } = await openRecipeZip(path);
  // Send the same immutable bytes used to derive the idempotency key.
  const bytes = await file.arrayBuffer();
  const zip_sha256 = createHash('sha256').update(new Uint8Array(bytes)).digest('hex');
  const request: UploadRequest = {
    local_recipe_id, local_recipe_version_id, zip_sha256, idempotency_key, ...destination,
  };

  const prepared = store.tx<PreparedUpload>(() => {
    const recipe = store.get<Recipe>(local_recipe_id, 'recipe');
    const version = store.version(recipe.id, local_recipe_version_id);
    const synced = publishedUpload(store, version.id, task);
    if (synced) {
      if (!sameUpload(store, synced.request, request)
          || (recipe_id !== undefined && recipe_id !== synced.result.recipe_id))
        return fail('conflict', 'local Recipe version was uploaded with different arguments');
      return { receipt: synced.result };
    }
    if (version.provenance?.source === 'cloud')
      return fail('invalid_state', 'the imported base Recipe version is already published in the cloud');
    const pending = journal<UploadPlan>(store, pendingUploadScope, recipe.id);
    if (pending && (!sameUpload(store, pending.request, request)
        || !matchesPendingTarget(pending.request, recipe_id, task)))
      return fail('conflict', 'another upload is pending for this local Recipe');
    const target = resolveUploadTarget(store, recipe.id, destination, recipe_id, pending?.request, task);
    const linkedTaskId = target.link?.task.task_id;
    if (linkedTaskId !== undefined) assertAvailableTaskUploadKey(store, linkedTaskId, request);
    if (typeof version.provenance?.execution_id === 'string') {
      const approved = store.db.prepare(`
        SELECT 1 FROM executions
        WHERE recipe_id=? AND recipe_version_id=? AND status='completed'
          AND json_extract(details, '$.result.test_status')='passed'
          AND json_extract(details, '$.result.published')=1
          AND json_extract(details, '$.result.provisional')=0
        LIMIT 1
      `).get(recipe.id, version.id);
      const origin = store.db.prepare('SELECT 1 FROM executions WHERE id=?').get(version.provenance.execution_id);
      if (!approved && (origin || recipe.active_version_id !== version.id))
        return fail('invalid_state', 'a Task Recipe version must be approved before upload');
    }
    assertRecipePackageMatches(join(store.root, recipe.storage_path, version.id), new Uint8Array(bytes));
    const mismatchedArtifact = store.db.prepare(`
      SELECT 1 FROM idempotency p JOIN executions e ON e.id=json_extract(p.request, '$.execution_id')
      WHERE p.scope=? AND e.recipe_version_id=? AND json_extract(p.result, '$.sha256')<>?
        AND (? IS NULL OR p.key=?)
      LIMIT 1
    `).get(taskRecipePackageScope, version.id, zip_sha256, linkedTaskId ?? null, linkedTaskId ?? null);
    if (mismatchedArtifact) return fail('conflict', 'upload must use the frozen Task Recipe ZIP');
    assertRecipeIdle(store, recipe.id);
    const cloud = new CloudClient({ ...cloudOptions, base_url: destination.base_url }, uploadHeaders);
    const keyParts = [recipe.id, version.id, zip_sha256];
    if (target.id !== null) keyParts.push(target.id);
    if (linkedTaskId !== undefined) keyParts.push(linkedTaskId);
    const plan: UploadPlan = pending?.request ?? {
      ...request,
      ...target.link?.task,
      local_recipe_id: recipe.id,
      local_recipe_version_id: version.id,
      idempotency_key: idempotency_key ?? `local-recipe-upload-${createHash('sha256')
        .update(keyParts.join('\0')).digest('hex')}`,
      cloud_recipe_id: target.id,
    };
    if (!pending) store.remember(pendingUploadScope, recipe.id, plan, {});
    return { plan, cloud, pendingKey: recipe.id };
  });
  if ('receipt' in prepared) return prepared.receipt;

  const { cloud, plan, pendingKey } = prepared;
  let response: unknown;
  try {
    response = await uploadRecipeZip(
      cloud,
      { file: new Blob([bytes], { type: 'application/zip' }), filename },
      { recipe_id: plan.cloud_recipe_id ?? undefined, idempotency_key: plan.idempotency_key },
    );
  } catch (error) {
    const terminal = uploadFailedPermanently(error);
    if (plan.task_id || terminal) {
      const committed = store.tx(() => {
        const saved = plan.task_id && journal<UploadPlan, RecipeUpsertResult>(store, taskUploadReceiptScope, plan.task_id);
        if (saved && encode(saved.request) === encode(plan)) return saved.result;
        if (terminal) clearPending(store, pendingKey, plan);
      });
      if (committed) return committed;
    }
    throw error;
  }
  const parsed = receiptSchema.safeParse(response);
  if (!parsed.success || (plan.cloud_recipe_id !== null && parsed.data.recipe_id !== plan.cloud_recipe_id))
    return fail('invalid_response', 'cloud Recipe upload returned mismatched or invalid IDs');
  const receipt = parsed.data;

  return store.tx(() => {
    const versionId = store.resolveVersionId(local_recipe_version_id);
    const replay = publishedUpload(store, versionId, task);
    if (replay) {
      if (!sameUpload(store, replay.request, request)
          || replay.result.recipe_id !== receipt.recipe_id
          || replay.result.recipe_version_id !== receipt.recipe_version_id)
        return fail('conflict', 'cloud Recipe upload returned a different receipt');
      return replay.result;
    }
    const pending = journal<UploadPlan>(store, pendingUploadScope, pendingKey);
    if (!pending || encode(pending.request) !== encode(plan))
      return fail('conflict', 'local Recipe upload plan changed before its receipt was saved');
    const recipe = store.get<Recipe>(local_recipe_id, 'recipe');
    assertRecipeIdle(store, recipe.id);
    const target = resolveUploadTarget(store, recipe.id, destination, recipe_id, plan, task);
    if (target.link) {
      persistTaskAssociation(store, target.link.recipe, target.link.task.task_id,
        store.version(recipe.id, versionId), plan, receipt, new Uint8Array(bytes));
    } else {
      assertCloudRecipeTargetAvailable(store, receipt.recipe_id, recipe.id);
      adoptRecipeIdentity(store, recipe, versionId, receipt);
      store.remember(uploadReceiptScope, receipt.recipe_version_id, plan, receipt);
    }
    const previouslyLinked = journal<Destination>(store, recipeIdentityScope, receipt.recipe_id);
    if (!previouslyLinked) {
      store.remember(recipeIdentityScope, receipt.recipe_id, destination, {});
    }
    clearPending(store, pendingKey, plan);
    return receipt;
  });
}
