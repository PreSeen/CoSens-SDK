import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import Database from 'better-sqlite3';
import { RSIClient as QueueClient } from '../src/rsi/client.js';
import type { Recipe } from '../src/rsi/models.js';
import { parseRecipeMarkdown, readRecipeFile } from '../src/rsi/recipe-file.js';
import { recipeProjection, schema } from '../src/rsi/store-schema.js';

export async function seedRecipe(data_dir: string, path: string) {
  const { content, markdown } = await readRecipeFile(path);
  const queue = new QueueClient({ data_dir, auto_start: false });
  try {
    return queue.recipe_insert(content, markdown);
  } finally {
    queue.close();
  }
}

export function seedLegacyRecipe(data_dir: string, markdown: string, schemaVersion: 7 | 8 | 9): Recipe {
  const id = 'recipe_legacy';
  const versionId = 'version_legacy';
  const storage_path = `recipes/${id}`;
  const { content } = parseRecipeMarkdown(markdown);
  const selected_version = {
    id: versionId,
    object_id: id,
    number: 1,
    content,
    provenance: { source: 'external' },
    created_at: 1,
  };
  const db = new Database(join(data_dir, 'rsi-ts.sqlite3'));
  try {
    db.exec(schema);
    db.exec(recipeProjection);
    db.prepare('INSERT INTO recipes VALUES (?, 1, 2, NULL, ?, ?)').run(id, content.name, storage_path);
    db.prepare('INSERT INTO versions (id,recipe_id,number,data,status) VALUES (?,?,1,?,?)')
      .run(versionId, id, JSON.stringify(selected_version), 'available');
    db.pragma(`user_version = ${schemaVersion}`);
  } finally {
    db.close();
  }
  const dir = join(data_dir, storage_path, versionId);
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'RECIPE.md'), markdown);
  return {
    id, storage_path, created_at: 1, updated_at: 2, archived_at: null, name: content.name,
    active_version_id: versionId, latest_version_id: versionId, latest_qualified_version_id: versionId,
    selected_version: { ...selected_version, archived: false },
  };
}
