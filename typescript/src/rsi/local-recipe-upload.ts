import { createHash } from 'node:crypto';
import { z } from 'zod';
import { selectedVirtualUser } from '../virtual-user.js';
import {
  CloudClient,
  DEFAULT_CLOUD_BASE_URL,
  openRecipeZip,
  validateRecipeUpsertOptions,
  type RecipeUpsertResult,
} from './cloud.js';
import { fail, RSIError, type Recipe } from './models.js';
import {
  adoptRecipeIdentity,
  assertRecipeIdle,
  pendingUploadScope,
  recipeIdentityScope,
  uploadReceiptScope,
} from './recipe-identity.js';
import { encode, type Store } from './store.js';

const nonempty = z.string().min(1);
const optionsSchema = z.strictObject({
  local_recipe_id: nonempty,
  local_recipe_version_id: nonempty,
  idempotency_key: nonempty.optional(),
  api_key: nonempty.optional(),
  base_url: nonempty.optional(),
  virtualUserId: nonempty.optional(),
});
export type LocalRecipeUpsertOptions = z.input<typeof optionsSchema>;
const identifier = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/);
const receiptSchema = z.object({
  recipe_id: identifier,
  recipe_version_id: identifier,
});
const terminalUploadStatuses = new Set([400, 403, 404, 409, 413, 422]);

type Destination = { base_url: string; virtual_user_id: string | null };
type UploadRequest = Destination & {
  local_recipe_id: string;
  local_recipe_version_id: string;
  zip_sha256: string;
  idempotency_key: string | undefined;
};
type UploadPlan = UploadRequest & { cloud_recipe_id: string | null; idempotency_key: string };
type PreparedUpload = { receipt: RecipeUpsertResult } | {
  plan: UploadPlan;
  cloud: CloudClient;
  pendingKey: string;
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

function sameUpload(store: Store, recorded: UploadPlan, request: UploadRequest) {
  return store.resolveRecipeId(recorded.local_recipe_id) === store.resolveRecipeId(request.local_recipe_id)
    && store.resolveVersionId(recorded.local_recipe_version_id) === store.resolveVersionId(request.local_recipe_version_id)
    && recorded.zip_sha256 === request.zip_sha256
    && (request.idempotency_key === undefined || recorded.idempotency_key === request.idempotency_key)
    && sameDestination(recorded, request);
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

export async function uploadLocalRecipe(
  store: Store,
  path: string | URL,
  options: LocalRecipeUpsertOptions,
  modelHeaders?: Record<string, string>,
): Promise<RecipeUpsertResult> {
  const { local_recipe_id, local_recipe_version_id, idempotency_key, ...cloudOptions } = optionsSchema.parse(options);
  validateRecipeUpsertOptions({ idempotency_key });
  const base = new URL(cloudOptions.base_url ?? process.env.COSENS_RSI_BASE_URL ?? DEFAULT_CLOUD_BASE_URL);
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw new TypeError('base_url must be an HTTP(S) address without embedded credentials, query or fragment');
  const base_url = base.href.replace(/\/+$/, '');
  const headers = new Headers(modelHeaders);
  const virtual_user_id = selectedVirtualUser(cloudOptions) ?? headers.get('X-Virtual-User-Id');
  const destination = { base_url, virtual_user_id };
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
      if (!sameUpload(store, synced.request, request))
        return fail('conflict', 'local Recipe version was uploaded with different arguments');
      return { receipt: synced.result };
    }
    const pending = journal<UploadPlan>(store, pendingUploadScope, recipe.id);
    if (pending && !sameUpload(store, pending.request, request))
      return fail('conflict', 'another upload is pending for this local Recipe');
    assertRecipeIdle(store, recipe.id);
    const identity = journal<Destination>(store, recipeIdentityScope, recipe.id);
    if (identity && !sameDestination(identity.request, destination))
      return fail('conflict', 'local Recipe was synchronized to a different cloud endpoint or Virtual User');
    const cloud = new CloudClient({ ...cloudOptions, base_url }, headers);
    const plan: UploadPlan = pending?.request ?? {
      ...request,
      local_recipe_id: recipe.id,
      local_recipe_version_id: version.id,
      idempotency_key: idempotency_key ?? `local-recipe-upload-${createHash('sha256')
        .update([recipe.id, version.id, zip_sha256].join('\0')).digest('hex')}`,
      cloud_recipe_id: identity ? recipe.id : null,
    };
    if (!pending) store.remember(pendingUploadScope, recipe.id, plan, {});
    return { plan, cloud, pendingKey: recipe.id };
  });
  if ('receipt' in prepared) return prepared.receipt;

  const { cloud, plan, pendingKey } = prepared;
  let response: unknown;
  try {
    response = await cloud.uploadRecipeZip(
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
    adoptRecipeIdentity(store, recipe, versionId, receipt);
    if (plan.cloud_recipe_id === null) {
      store.remember(recipeIdentityScope, receipt.recipe_id, destination, {});
    }
    store.remember(uploadReceiptScope, receipt.recipe_version_id, plan, receipt);
    clearPending(store, pendingKey, plan);
    return receipt;
  });
}
