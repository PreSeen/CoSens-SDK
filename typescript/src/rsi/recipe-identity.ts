import { randomUUID } from 'node:crypto';
import {
  cpSync, lstatSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import { fail, type Execution, type Recipe, type RecipeContent } from './models.js';
import { formatRecipeMarkdown } from './recipe-file.js';
import type { Store } from './store.js';
import type { RecipeUpsertResult } from './cloud.js';

export const recipeIdAlias = 'recipe_cloud_id_alias';
export const versionIdAlias = 'recipe_cloud_version_alias';
export const recipeIdentityScope = 'recipe_cloud_identity';
export const pendingUploadScope = 'recipe_cloud_upload_pending';
export const uploadReceiptScope = 'recipe_cloud_upload_receipt';
export const taskUploadReceiptScope = 'task_recipe_upload_receipt';

export function assertCloudRecipeTargetAvailable(
  store: Store, targetId: string, localRecipeId?: string, allowExisting = false,
) {
  if (localRecipeId !== undefined && targetId !== localRecipeId && (
    store.resolveRecipeId(targetId) !== targetId
    || (!allowExisting && store.db.prepare('SELECT 1 FROM recipe_objects WHERE id=?').get(targetId))
  )) fail('conflict', 'cloud Recipe ID is already used locally');

  const pending = store.db.prepare(`
    SELECT 1 FROM idempotency
    WHERE scope=? AND (? IS NULL OR key<>?) AND (key=? OR json_extract(request, '$.cloud_recipe_id')=?)
    LIMIT 1
  `).get(pendingUploadScope, localRecipeId ?? null, localRecipeId ?? null, targetId, targetId);
  if (pending) fail('conflict', 'the cloud Recipe has a pending local upload');
}

const recipeExecutions = `
  recipe_id = @recipeId OR json_extract(details, '$.test_request.recipe_id') = @recipeId
  OR json_extract(details, '$.result.recipe_id') = @recipeId
  OR EXISTS (
    SELECT 1 FROM versions
    WHERE recipe_id = @recipeId AND id IN (
      executions.recipe_version_id, executions.base_recipe_version_id,
      json_extract(executions.details, '$.publication_base_version_id'),
      json_extract(executions.details, '$.test_request.version_id'),
      json_extract(executions.details, '$.result.recipe_version_id')
    )
  )
  OR EXISTS (
    SELECT 1 FROM json_each(executions.details, '$.test_results') AS t
    WHERE json_extract(t.value, '$.recipe_id') = @recipeId
      OR json_extract(t.value, '$.version_id') IN (SELECT id FROM versions WHERE recipe_id=@recipeId)
  )
`;

export function assertRecipeIdle(store: Store, recipeId: string) {
  const active = store.db.prepare(`
    SELECT 1 FROM executions WHERE status IN ('queued', 'running') AND (${recipeExecutions}) LIMIT 1
  `).get({ recipeId });
  if (active) fail('conflict', 'finish the pending Recipe execution before synchronizing its IDs');
}

function checkDirectory(path: string) {
  const entry = lstatSync(path, { throwIfNoEntry: false });
  if (entry && !entry.isDirectory())
    fail('conflict', 'Recipe version path already exists and is not a directory');
  return entry !== undefined;
}

function recipeMarkdown(path: string) {
  const entry = lstatSync(path, { throwIfNoEntry: false });
  if (!entry) return;
  if (!entry.isFile()) fail('conflict', 'Recipe Markdown path is not a regular file');
  return readFileSync(path);
}

function copyVersionDirectory(store: Store, recipe: Recipe, oldId: string, newId: string) {
  if (oldId === newId) return;
  const validId = /^[A-Za-z0-9_-]+$/u;
  if (!/^recipes\/[A-Za-z0-9_-]+$/u.test(recipe.storage_path)
      || !validId.test(oldId) || !validId.test(newId))
    fail('invalid_state', 'invalid Recipe version storage path');
  checkDirectory(join(store.root, 'recipes'));
  const parent = join(store.root, recipe.storage_path);
  checkDirectory(parent);
  const source = join(parent, oldId);
  const sourceExists = checkDirectory(source);
  const markdown = (sourceExists ? recipeMarkdown(join(source, 'RECIPE.md')) : undefined)
    ?? Buffer.from(formatRecipeMarkdown(store.version<RecipeContent>(recipe.id, oldId).content));
  const target = join(parent, newId);
  if (checkDirectory(target)) {
    if (!recipeMarkdown(join(target, 'RECIPE.md'))?.equals(markdown))
      fail('conflict', 'Recipe version path already contains another Recipe');
    return;
  }

  mkdirSync(parent, { recursive: true, mode: 0o700 });
  const temporary = join(parent, `.recipe-sync-${randomUUID()}`);
  try {
    if (sourceExists) cpSync(source, temporary, { recursive: true, verbatimSymlinks: true });
    else mkdirSync(temporary, { mode: 0o700 });
    const entrypoint = join(temporary, 'RECIPE.md');
    if (!recipeMarkdown(entrypoint))
      writeFileSync(entrypoint, markdown, { flag: 'wx', mode: 0o600, flush: true });
    renameSync(temporary, target);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

export function adoptRecipeIdentity(
  store: Store,
  recipe: Recipe,
  versionId: string,
  receipt: RecipeUpsertResult,
  legacy = false,
) {
  const recipeId = recipe.id;
  const objects = legacy ? 'recipes' : 'recipe_objects';
  const { recipe_id: formalRecipeId, recipe_version_id: formalVersionId } = receipt;
  if ((recipeId !== formalRecipeId && (
    store.db.prepare(`SELECT 1 FROM ${objects} WHERE id=?`).get(formalRecipeId)
    || store.resolveRecipeId(formalRecipeId) !== formalRecipeId
  )) || (versionId !== formalVersionId && (
    store.db.prepare('SELECT 1 FROM versions WHERE id=?').get(formalVersionId)
    || store.resolveVersionId(formalVersionId) !== formalVersionId
  ))) fail('conflict', 'cloud Recipe or version ID is already used locally');

  copyVersionDirectory(store, recipe, versionId, formalVersionId);
  const executions = store.db.prepare(`SELECT id,details FROM executions WHERE ${recipeExecutions}`)
    .all({ recipeId }) as { id: string; details: string }[];
  const canonicalVersion = (id: string | null | undefined) => id === versionId ? formalVersionId : id;
  const writeDetails = store.db.prepare('UPDATE executions SET details=? WHERE id=?');
  for (const row of executions) {
    const details = JSON.parse(row.details) as Pick<Execution, 'test_request' | 'test_results'> & {
      publication_base_version_id?: string | null;
      result?: { recipe_id?: string; recipe_version_id?: string | null } | null;
    };
    for (const entry of [details.test_request, ...(details.test_results ?? [])]) {
      if (!entry) continue;
      if (entry.recipe_id === recipeId) entry.recipe_id = formalRecipeId;
      entry.version_id = canonicalVersion(entry.version_id)!;
    }
    details.publication_base_version_id = canonicalVersion(details.publication_base_version_id);
    if (details.result) {
      if (details.result.recipe_id === recipeId) details.result.recipe_id = formalRecipeId;
      details.result.recipe_version_id = canonicalVersion(details.result.recipe_version_id);
    }
    writeDetails.run(JSON.stringify(details), row.id);
  }

  // Existing version rows still reference the Recipe ID until both writes finish.
  store.db.pragma('defer_foreign_keys = ON');
  if (recipeId !== formalRecipeId) {
    store.db.prepare(`UPDATE ${objects} SET id=? WHERE id=?`).run(formalRecipeId, recipeId);
    store.db.prepare(`
      UPDATE versions SET recipe_id=?, data=json_set(data, '$.object_id', ?) WHERE recipe_id=?
    `).run(formalRecipeId, formalRecipeId, recipeId);
    store.db.prepare('UPDATE executions SET recipe_id=? WHERE recipe_id=?').run(formalRecipeId, recipeId);
    if (!legacy) store.db.prepare('UPDATE recipes SET recipe_id=? WHERE recipe_id=?').run(formalRecipeId, recipeId);
    store.remember(recipeIdAlias, recipeId, {}, { id: formalRecipeId });
  }
  if (versionId !== formalVersionId) {
    store.db.prepare(`
      UPDATE versions SET id=?, data=json_set(data, '$.id', ?) WHERE id=? AND recipe_id=?
    `).run(formalVersionId, formalVersionId, versionId, formalRecipeId);
    store.db.prepare('UPDATE executions SET recipe_version_id=? WHERE recipe_version_id=?')
      .run(formalVersionId, versionId);
    store.db.prepare('UPDATE executions SET base_recipe_version_id=? WHERE base_recipe_version_id=?')
      .run(formalVersionId, versionId);
    if (!legacy) {
      store.db.prepare('UPDATE recipes SET recipe_version_id=? WHERE recipe_id=? AND recipe_version_id=?')
        .run(formalVersionId, formalRecipeId, versionId);
    }
    store.remember(versionIdAlias, versionId, {}, { id: formalVersionId });
  }
}

export function migrateMappedRecipes(store: Store) {
  const rows = store.db.prepare(`
    SELECT r.id AS local_recipe_id, v.id AS local_version_id,
      r.cloud_recipe_id AS recipe_id, v.cloud_recipe_version_id AS recipe_version_id
    FROM recipes r JOIN versions v ON v.recipe_id=r.id
    WHERE r.cloud_recipe_id IS NOT NULL AND v.cloud_recipe_version_id IS NOT NULL
    ORDER BY r.id, v.number
  `).all() as (RecipeUpsertResult & { local_recipe_id: string; local_version_id: string })[];
  for (const { local_recipe_id, local_version_id, ...receipt } of rows) {
    const validId = /^[A-Za-z0-9_-]{1,64}$/u;
    if (!validId.test(receipt.recipe_id) || !validId.test(receipt.recipe_version_id))
      fail('invalid_state', 'invalid mapped Recipe ID');
    const recipe = store.get<Recipe>(local_recipe_id, 'recipe');
    const entry = store.db.prepare('SELECT request FROM idempotency WHERE scope=? AND key=?')
      .get(uploadReceiptScope, local_version_id) as { request: string } | undefined;
    if (!entry) return fail('invalid_state', 'mapped Recipe version lost its upload receipt');
    const { base_url, virtual_user_id } = JSON.parse(entry.request);
    const destination = { base_url, virtual_user_id };
    adoptRecipeIdentity(store, recipe, local_version_id, receipt, true);
    if (!store.replay(recipeIdentityScope, receipt.recipe_id, destination))
      store.remember(recipeIdentityScope, receipt.recipe_id, destination, {});
    const move = store.db.prepare('UPDATE idempotency SET key=? WHERE scope=? AND key=?');
    move.run(receipt.recipe_version_id, uploadReceiptScope, local_version_id);
    if (recipe.id !== receipt.recipe_id) move.run(receipt.recipe_id, pendingUploadScope, recipe.id);
  }
  store.db.exec(`
    ALTER TABLE recipes DROP COLUMN cloud_recipe_id;
    ALTER TABLE recipes DROP COLUMN cloud_recipe_version_id;
    ALTER TABLE versions DROP COLUMN cloud_recipe_version_id;
  `);
}
