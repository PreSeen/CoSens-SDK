import { RSIError, type RecipeContent, type Version } from './models.js';
import { taskUploadReceiptScope } from './recipe-identity.js';
import type { Store } from './store.js';
import { hasApprovedResult } from './task.js';

function generatingRubricsVersionId(store: Store, version: Version<RecipeContent>): string | undefined {
  const { rubrics_version_id, execution_id, input_revision } = version.provenance ?? {};
  if (typeof rubrics_version_id === 'string' && rubrics_version_id) return rubrics_version_id;
  if (typeof execution_id !== 'string' || typeof input_revision !== 'number') return;

  const analysis = store.operation(`rubrics:${execution_id}:${input_revision}`);
  if (analysis) return analysis.id;
  const [origin] = store.listExecutions([execution_id]);
  const matches = origin?.test_results?.filter((result) =>
    result.execution_id === execution_id
    && result.input_revision === input_revision
    && store.resolveRecipeId(result.recipe_id) === version.object_id
    && store.resolveVersionId(result.version_id) === version.id);
  const testRubrics = new Set(matches?.map((result) => result.rubrics_version_id));
  if (testRubrics.size === 1) return testRubrics.values().next().value;
  if (testRubrics.size === 0 && origin?.input_revision === input_revision
      && (origin.analyzed_input_revision ?? input_revision) === input_revision)
    return origin.rubrics_version_id ?? undefined;
}

export function approvedRubricsVersionId(
  store: Store, version: Version<RecipeContent>, executionId?: string,
): string | undefined {
  const [execution] = executionId ? store.listExecutions([executionId]) : [];
  if (execution && hasApprovedResult(execution)
      && store.resolveRecipeId(execution.recipe_id) === version.object_id
      && store.resolveVersionId(execution.recipe_version_id) === version.id
      && execution.rubrics_version_id)
    return execution.rubrics_version_id;
  return generatingRubricsVersionId(store, version);
}

export function recipeRubricsVersionId(store: Store, version: Version<RecipeContent>): string | undefined {
  const rubricId = generatingRubricsVersionId(store, version);
  if (rubricId || version.provenance?.source !== 'cloud') return rubricId;

  // Older Task uploads retained the generated version only in the exact upload receipt.
  const receipts = store.db.prepare(`
    SELECT json_extract(request, '$.local_recipe_id') AS recipeId,
           json_extract(request, '$.local_recipe_version_id') AS versionId,
           json_extract(request, '$.execution_id') AS executionId
    FROM idempotency
    WHERE scope=? AND json_extract(result, '$.recipe_id')=?
      AND json_extract(result, '$.recipe_version_id')=?
      AND json_extract(request, '$.cloud_recipe_id')=?
    LIMIT 2
  `).all(taskUploadReceiptScope, version.object_id, version.id, version.object_id) as {
    recipeId: string | null; versionId: string | null; executionId: string | null;
  }[];
  if (receipts.length !== 1) return;
  const [{ recipeId, versionId, executionId }] = receipts;
  if (!recipeId || !versionId || !executionId) return;
  try {
    const original = store.version<RecipeContent>(recipeId, versionId);
    if (original.id !== version.id) return approvedRubricsVersionId(store, original, executionId);
  } catch (error) {
    if (!(error instanceof RSIError) || error.code !== 'not_found') throw error;
  }
}
