import { createHash } from 'node:crypto';
import { lstatSync, mkdirSync, mkdtempSync, renameSync, rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { z } from 'zod';
import { CloudClient, recipeCloudDestination, type RecipeCloudDestination } from './cloud.js';
import { fail, type Recipe, type RecipeContent, type Version } from './models.js';
import { parseRecipeMarkdown } from './recipe-file.js';
import { assertCloudRecipeTargetAvailable, recipeIdentityScope, uploadReceiptScope } from './recipe-identity.js';
import { assertRecipePackageMatches, readRecipePackage, stageRecipePackageFiles } from './recipe-package.js';
import { encode, now, type Store } from './store.js';

const bootstrapScope = 'recipe_cloud_bootstrap';
const identifier = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/u);
const cloudRecipe = z.object({
  id: identifier,
  archived_at: z.number().nullable(),
  active_version_id: identifier.nullable(),
  selected_version: z.object({
    id: identifier,
    object_id: identifier,
    publication_state: z.string().nullable(),
    package_sha256: z.string().nullable(),
  }).nullable(),
});

export type RecipeBase = { kind: 'recipe'; id: string; version_id?: string };
type LocalBase = { recipe: Recipe; versionId?: string };
type Bootstrap = {
  recipeId: string;
  versionId: string;
  destination: RecipeCloudDestination;
  sha256: string;
  archive: Uint8Array;
  files: Map<string, Uint8Array>;
  content: RecipeContent;
};

export function findLocalRecipeBase(store: Store, base: RecipeBase): LocalBase | undefined {
  const recipeId = store.resolveRecipeId(base.id);
  const recipe = store.db.prepare('SELECT * FROM recipe_records WHERE id=?').get(recipeId) as Recipe | undefined;
  if (!recipe) return;
  if (recipe.archived_at !== null) return fail('invalid_state', 'base Recipe is archived');
  const versionId = base.version_id ? store.resolveVersionId(base.version_id) : recipe.active_version_id;
  if (!versionId) return fail('invalid_state', 'base Recipe has no available version');
  const row = store.db.prepare("SELECT recipe_id,status,json_extract(data, '$.provenance.source') AS source FROM versions WHERE id=?")
    .get(versionId) as { recipe_id: string | null; status: string | null; source: string | null } | undefined;
  if (!row) {
    const linked = store.db.prepare('SELECT 1 FROM idempotency WHERE scope=? AND key=?')
      .get(recipeIdentityScope, recipeId);
    if (!linked) return fail('not_found', 'local base Recipe version not found');
    return;
  }
  if (row.recipe_id !== recipe.id) return fail('conflict', 'base Recipe version belongs to another Recipe');
  const imported = store.db.prepare('SELECT 1 FROM idempotency WHERE scope=? AND key=?')
    .get(bootstrapScope, versionId);
  if (!base.version_id) return { recipe, versionId: imported || row.source === 'cloud' ? versionId : undefined };
  if (row.status !== 'available' && !imported) {
    const upload = store.db.prepare('SELECT 1 FROM idempotency WHERE scope=? AND key=?').get(uploadReceiptScope, versionId);
    const accepted = store.db.prepare(`
      SELECT 1 FROM executions WHERE recipe_id=? AND recipe_version_id=? AND status='completed'
      AND json_extract(details, '$.result.test_status')='passed'
      AND json_extract(details, '$.result.published')=1
      AND json_extract(details, '$.result.provisional')=0 LIMIT 1
    `).get(recipe.id, versionId);
    if (!upload && !accepted) return fail('invalid_state', 'base Recipe version has not been approved');
  }
  return { recipe, versionId };
}

export async function fetchCloudRecipeBase(store: Store, base: RecipeBase, headers?: Headers): Promise<Bootstrap> {
  if ((!headers || headers.keys().next().done) && !process.env.COSENS_API_KEY)
    return fail('not_found', 'recipe not found in local SQLite; configure CoSens credentials to fetch it');
  const recipeId = identifier.parse(store.resolveRecipeId(base.id));
  const requested = base.version_id ? identifier.parse(store.resolveVersionId(base.version_id)) : undefined;
  const destination = recipeCloudDestination({}, headers);
  store.replay(recipeIdentityScope, recipeId, destination);
  const cloud = new CloudClient({ base_url: destination.base_url }, headers);
  const response = cloudRecipe.safeParse(await cloud.recipe_get(recipeId, requested ?? 'active'));
  if (!response.success || response.data.id !== recipeId)
    return fail('invalid_response', 'CoSens returned invalid cloud Recipe metadata');
  const recipe = response.data;
  if (recipe.archived_at !== null || !recipe.active_version_id)
    return fail('invalid_state', 'cloud Recipe has no available version');
  if (requested && requested !== recipe.active_version_id)
    return fail('conflict', 'the requested Recipe version is no longer active in the cloud');
  const selected = recipe.selected_version;
  if (!selected || selected.id !== recipe.active_version_id || selected.object_id !== recipeId)
    return fail('invalid_response', 'CoSens returned inconsistent cloud Recipe version IDs');
  if (selected.publication_state !== 'published' || !selected.package_sha256 || !/^[a-f0-9]{64}$/u.test(selected.package_sha256))
    return fail('invalid_state', 'cloud Recipe version has no verified published ZIP');
  const archive = await cloud.recipe_download(recipeId, selected.id);
  const sha256 = createHash('sha256').update(archive).digest('hex');
  if (sha256 !== selected.package_sha256)
    return fail('invalid_response', 'cloud Recipe ZIP does not match its published SHA-256');
  const files = readRecipePackage(archive);
  const markdown = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(files.get('RECIPE.md')!);
  return { recipeId, versionId: selected.id, destination, sha256, archive, files, content: parseRecipeMarkdown(markdown).content };
}

function versionDirectory(store: Store, recipe: Recipe, versionId: string) {
  if (!/^recipes\/[A-Za-z0-9_-]+$/u.test(recipe.storage_path) || !identifier.safeParse(versionId).success)
    return fail('invalid_state', 'invalid local Recipe version storage path');
  return join(store.root, recipe.storage_path, versionId);
}

export function baseRecipePath(store: Store, recipe: Recipe, versionId: string) {
  const directory = versionDirectory(store, recipe, versionId);
  for (const path of [dirname(dirname(directory)), dirname(directory), directory]) {
    if (!lstatSync(path, { throwIfNoEntry: false })?.isDirectory())
      return fail('invalid_state', 'local base Recipe version directory is unavailable');
  }
  const path = join(directory, 'RECIPE.md');
  if (!lstatSync(path, { throwIfNoEntry: false })?.isFile())
    return fail('invalid_state', 'local base Recipe Markdown is unavailable');
  return path;
}

export function installRecipeVersionFiles(
  store: Store, recipe: Recipe, versionId: string, archive: Uint8Array, files?: Map<string, Uint8Array>,
) {
  const target = versionDirectory(store, recipe, versionId);
  const parent = dirname(target);
  for (const path of [dirname(parent), parent, target]) {
    const stat = lstatSync(path, { throwIfNoEntry: false });
    if (stat && !stat.isDirectory()) return fail('conflict', 'local Recipe version directory is not usable');
  }
  if (lstatSync(target, { throwIfNoEntry: false })) {
    assertRecipePackageMatches(target, archive);
    return;
  }
  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const staging = mkdtempSync(join(parent, '.cloud-recipe-'));
  try {
    stageRecipePackageFiles(staging, files ?? readRecipePackage(archive));
    renameSync(staging, target);
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
}

// The caller holds the SQLite write transaction, while cloud I/O takes place before it.
export function installCloudRecipeBase(store: Store, bootstrap: Bootstrap): LocalBase {
  const { recipeId, versionId, destination, sha256, content } = bootstrap;
  assertCloudRecipeTargetAvailable(store, recipeId);
  if (store.resolveRecipeId(recipeId) !== recipeId || store.resolveVersionId(versionId) !== versionId)
    return fail('conflict', 'cloud Recipe or version ID is already used as a local alias');
  const identity = store.replay(recipeIdentityScope, recipeId, destination);
  const existing = store.db.prepare('SELECT * FROM recipe_records WHERE id=?').get(recipeId) as Recipe | undefined;
  if (existing && (!identity || existing.archived_at !== null))
    return fail('conflict', 'existing local Recipe cannot be linked to this cloud Recipe');
  const row = store.db.prepare('SELECT recipe_id FROM versions WHERE id=?').get(versionId) as
    { recipe_id: string | null } | undefined;
  const request = { recipe_id: recipeId, ...destination, sha256 };
  const imported = store.replay(bootstrapScope, versionId, request);
  const uploaded = row && !imported && store.db.prepare(`
    SELECT 1 FROM idempotency WHERE scope=? AND key=?
      AND json_extract(request, '$.base_url')=? AND json_extract(request, '$.virtual_user_id') IS ?
      AND json_extract(request, '$.zip_sha256')=? AND json_extract(result, '$.recipe_id')=?
  `).get(uploadReceiptScope, versionId, destination.base_url, destination.virtual_user_id, sha256, recipeId);
  if (row && (row.recipe_id !== recipeId || (!imported && !uploaded)))
    return fail('conflict', 'cloud Recipe version ID already belongs to a local version');
  const recipe = existing ?? store.insert<Recipe>('recipe', { id: recipeId, name: content.name });
  installRecipeVersionFiles(store, recipe, versionId, bootstrap.archive, bootstrap.files);
  if (!row) {
    const { number } = store.db.prepare('SELECT COALESCE(MAX(number),0)+1 AS number FROM versions WHERE recipe_id=?')
      .get(recipeId) as { number: number };
    const version: Version<RecipeContent> = {
      id: versionId, object_id: recipeId, number, content, created_at: now(),
      provenance: { source: 'cloud', package_sha256: sha256 },
    };
    // Preserve a local active version and unpublished drafts. The journal makes a
    // verified extra version selectable explicitly without advancing the local head.
    store.db.prepare('INSERT INTO versions(id,recipe_id,number,data,status) VALUES(?,?,?,?,?)')
      .run(versionId, recipeId, number, encode(version), recipe.active_version_id ? 'candidate' : 'available');
    store.save(recipe);
    store.remember(bootstrapScope, versionId, request, {});
  }
  if (!identity) store.remember(recipeIdentityScope, recipeId, destination, {});
  return { recipe, versionId };
}
