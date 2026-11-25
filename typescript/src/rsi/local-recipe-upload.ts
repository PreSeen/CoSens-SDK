import { createHash } from 'node:crypto';
import { join } from 'node:path';
import { z } from 'zod';
import {
  CloudClient,
  openRecipeZip,
  recipeCloudDestination,
  uploadRecipeZip,
  validateRecipeUpsertOptions,
  type RecipeCloudDestination,
  type RecipeUpsertResult,
} from './cloud.js';
import { fail, RSIError, type Recipe } from './models.js';
import {
  adoptRecipeIdentity,
  assertCloudRecipeTargetAvailable,
  assertRecipeIdle,
  pendingUploadScope,
  recipeIdentityScope,
  uploadReceiptScope,
} from './recipe-identity.js';
import { encode, type Store } from './store.js';
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
type UploadPlan = UploadRequest & { cloud_recipe_id: string | null; idempotency_key: string };
type Receipt = { receipt: RecipeUpsertResult };
type PreparedUpload = Receipt | {
  plan: UploadPlan;
  cloud: CloudClient;
  pendingKey: string;
};
type PreparedLocalUpload = Receipt | {
  path: string;
  ids: Pick<UploadRequest, 'local_recipe_id' | 'local_recipe_version_id'>;
};

function journal<Request, Result = Record<string, never>>(store: Store, scope: string, key: string) {
  const row = store.db.prepare('SELECT request,result FROM idempotency WHERE scope=? AND key=?')
    .get(scope, key) as { request: string; result: string } | undefined;
  if (!row) return;
  return { request: JSON.parse(row.request) as Request, result: JSON.parse(row.result) as Result };
}

function sameDestination(left: Destination, right: Destination) {
  return left.base_url === right.base_url && left.virtual_user_id === right.virtual_user_id;
}

function sameUploadSelection(store: Store, recorded: UploadPlan, request: Omit<UploadRequest, 'zip_sha256'>) {
  return store.resolveRecipeId(recorded.local_recipe_id) === store.resolveRecipeId(request.local_recipe_id)
    && store.resolveVersionId(recorded.local_recipe_version_id) === store.resolveVersionId(request.local_recipe_version_id)
    && (request.idempotency_key === undefined || recorded.idempotency_key === request.idempotency_key)
    && sameDestination(recorded, request);
}

function sameUpload(store: Store, recorded: UploadPlan, request: UploadRequest) {
  return recorded.zip_sha256 === request.zip_sha256 && sameUploadSelection(store, recorded, request);
}

function resolveUploadTarget(
  store: Store, recipeId: string, destination: Destination, requested?: string, pending?: UploadPlan,
) {
  const identity = journal<Destination>(store, recipeIdentityScope, recipeId);
  if (identity && !sameDestination(identity.request, destination))
    return fail('conflict', 'local Recipe was synchronized to a different cloud endpoint or Virtual User');
  if (identity && requested !== undefined && requested !== recipeId)
    return fail('conflict', 'local Recipe is already linked to a different cloud Recipe');
  const target = pending ? pending.cloud_recipe_id : (requested ?? (identity ? recipeId : null));
  if (target !== null) assertCloudRecipeTargetAvailable(store, target, recipeId);
  return target;
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
    const selection = { ...ids, ...destination, idempotency_key };
    const saved = journal<UploadPlan, RecipeUpsertResult>(store, uploadReceiptScope, ids.local_recipe_version_id);
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
        || (recipe_id !== undefined && recipe_id !== pending.request.cloud_recipe_id)))
      return fail('conflict', 'another upload is pending for this local Recipe');
    resolveUploadTarget(store, ids.local_recipe_id, destination, recipe_id, pending?.request);
    const artifact = taskArtifact(store, task_id, execution, pending?.request.zip_sha256);
    if (!artifact && pending) return fail('conflict', 'pending Recipe upload requires the original frozen ZIP');
    if (!artifact) return fail('invalid_state', 'Task has no frozen Recipe ZIP to upload');
    return { path: artifact.zip_path, ids };
  });
  if ('receipt' in prepared) return prepared.receipt;
  return uploadLocalRecipe(store, prepared.path, {
    ...prepared.ids, recipe_id, idempotency_key, base_url: destination.base_url,
  }, uploadHeaders);
}

export async function uploadLocalRecipe(
  store: Store,
  path: string | URL,
  options: z.input<typeof versionOptionsSchema>,
  uploadHeaders?: Headers,
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
    const synced = journal<UploadPlan, RecipeUpsertResult>(store, uploadReceiptScope, version.id);
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
        || (recipe_id !== undefined && recipe_id !== pending.request.cloud_recipe_id)))
      return fail('conflict', 'another upload is pending for this local Recipe');
    const cloudRecipeId = resolveUploadTarget(store, recipe.id, destination, recipe_id, pending?.request);
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
      LIMIT 1
    `).get(taskRecipePackageScope, version.id, zip_sha256);
    if (mismatchedArtifact) return fail('conflict', 'upload must use the frozen Task Recipe ZIP');
    assertRecipeIdle(store, recipe.id);
    const cloud = new CloudClient({ ...cloudOptions, base_url: destination.base_url }, uploadHeaders);
    const keyParts = [recipe.id, version.id, zip_sha256];
    if (cloudRecipeId !== null) keyParts.push(cloudRecipeId);
    const plan: UploadPlan = pending?.request ?? {
      ...request,
      local_recipe_id: recipe.id,
      local_recipe_version_id: version.id,
      idempotency_key: idempotency_key ?? `local-recipe-upload-${createHash('sha256')
        .update(keyParts.join('\0')).digest('hex')}`,
      cloud_recipe_id: cloudRecipeId,
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
    if (uploadFailedPermanently(error)) store.tx(() => clearPending(store, pendingKey, plan));
    throw error;
  }
  const parsed = receiptSchema.safeParse(response);
  if (!parsed.success || (plan.cloud_recipe_id !== null && parsed.data.recipe_id !== plan.cloud_recipe_id))
    return fail('invalid_response', 'cloud Recipe upload returned mismatched or invalid IDs');
  const receipt = parsed.data;

  return store.tx(() => {
    const versionId = store.resolveVersionId(local_recipe_version_id);
    const replay = journal<UploadPlan, RecipeUpsertResult>(store, uploadReceiptScope, versionId);
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
    assertCloudRecipeTargetAvailable(store, receipt.recipe_id, recipe.id);
    const previouslyLinked = journal<Destination>(store, recipeIdentityScope, recipe.id);
    adoptRecipeIdentity(store, recipe, versionId, receipt);
    if (!previouslyLinked) {
      store.remember(recipeIdentityScope, receipt.recipe_id, destination, {});
    }
    store.remember(uploadReceiptScope, receipt.recipe_version_id, plan, receipt);
    clearPending(store, pendingKey, plan);
    return receipt;
  });
}
