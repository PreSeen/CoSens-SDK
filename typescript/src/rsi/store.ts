import { upgradeExecution } from './execution-data.js';
import {
  legacyRecipes,
  migrateRecipeVersions,
  recipeProjection,
  schema,
  storage,
} from './store-schema.js';
import Database from 'better-sqlite3';
import { randomUUID } from 'node:crypto';
import {
  chmodSync,
  mkdirSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { resolve, join } from 'node:path';
import {
  fail,
  type Kind,
  type ObjectMetadata,
  type Version,
  type Pagination,
  type Execution,
  type Versioned,
  type Recipe,
  type RecipeContent,
} from './models.js';

export const uid = (prefix: string) =>
  `${prefix}_${randomUUID().replaceAll('-', '')}`;
export const now = () => Date.now() / 1000;
/** Stable JSON for idempotency comparison, including nested object key order. */
export function encode(value: unknown): string {
  return JSON.stringify(value, (_k, v: unknown) =>
    v && typeof v === 'object' && !Array.isArray(v)
      ? Object.fromEntries(
          Object.entries(v)
            .filter(([, x]) => x !== undefined)
            .sort(([a], [b]) => a.localeCompare(b)),
        )
      : v,
  );
}
export function pagination({ limit = 50, offset = 0 }: Pagination = {}) {
  if (
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 200 ||
    !Number.isInteger(offset) ||
    offset < 0
  )
    fail(
      'invalid_argument',
      'limit must be 1..200; offset must be nonnegative',
    );
  return { limit, offset };
}
const initializeRecipeAvailability = `
WITH legacy AS MATERIALIZED (
    SELECT id, data FROM objects WHERE kind='recipe'
        AND json_type(data, '$.latest_qualified_version_id') IS NULL
),
eligible AS (
    SELECT id AS recipe_id, json_extract(data, '$.active_version_id') AS version_id
    FROM legacy
    UNION
    SELECT r.id,
        json_extract(t.value, '$.version_id')
    FROM objects e JOIN legacy r ON r.id=json_extract(e.data, '$.recipe_id')
    JOIN json_each(e.data, '$.test_results') t
    WHERE e.kind='execution' AND json_extract(e.data, '$.status')='completed'
        AND json_extract(e.data, '$.result.provisional')=0
        AND json_extract(t.value, '$.status')='passed'
        AND json_extract(t.value, '$.input_revision')=json_extract(e.data, '$.input_revision')
),
ranked AS (
    SELECT v.id, v.object_id, row_number() OVER (
        PARTITION BY v.object_id ORDER BY v.number DESC
    ) AS position
    FROM eligible e JOIN versions v ON v.object_id=e.recipe_id AND v.id=e.version_id
),
target AS MATERIALIZED (
    SELECT r.id, v.id AS version_id
    FROM legacy r LEFT JOIN ranked v ON v.object_id=r.id AND v.position=1
)
UPDATE objects
SET data=json_set(objects.data, '$.latest_qualified_version_id', target.version_id,
    '$.active_version_id', CASE WHEN json_extract(objects.data, '$.archived_at') IS NULL
        THEN target.version_id END)
FROM target WHERE objects.id=target.id
`;

export class Store {
  readonly root: string;
  readonly db: Database.Database;
  constructor(root: string) {
    this.root = resolve(root);
    mkdirSync(this.root, { recursive: true, mode: 0o700 });
    // A different filename and runtime marker prevent accidental Python worker sharing.
    const path = join(this.root, 'rsi-ts.sqlite3');
    this.db = new Database(path);
    this.db.pragma('journal_mode=WAL');
    this.db.pragma('busy_timeout=20000');
    this.db.pragma('foreign_keys=ON');
    this.initialize();
    chmodSync(path, 0o600);
  }
  private initialize() {
    if (this.db.pragma('user_version', { simple: true }) === 7) return;
    this.tx(() => {
      const version = this.db.pragma('user_version', { simple: true });
      if (version === 7) return;
      if (
        version !== 0 &&
        version !== 1 &&
        version !== 2 &&
        version !== 3 &&
        version !== 4 &&
        version !== 5 &&
        version !== 6
      )
        fail('invalid_state', 'unsupported RSI SQLite schema version');
      const legacy = this.db
        .prepare(
          "SELECT 1 FROM sqlite_master WHERE type='table' AND name='objects'",
        )
        .get();
      if (legacy) {
        const rows = this.db
          .prepare("SELECT id,data FROM objects WHERE kind='execution'")
          .all() as { id: string; data: string }[];
        const settings = this.db
          .prepare(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'",
          )
          .get();
        const previousConfig = settings
          ? (this.db.prepare('SELECT data FROM settings WHERE id=1').get() as
              | { data: string }
              | undefined)
          : undefined;
        const write = this.db.prepare('UPDATE objects SET data=? WHERE id=?');
        for (const { id, data } of rows) {
          const execution = upgradeExecution(JSON.parse(data));
          if (!execution.runtime_config && previousConfig)
            execution.runtime_config = JSON.parse(previousConfig.data).config;
          write.run(encode(execution), id);
        }
        this.db.exec(initializeRecipeAvailability);
        this.db.exec('ALTER TABLE versions RENAME TO previous_versions;');
        this.db.exec(legacyRecipes);
      }
      if (version === 2)
        this.db.exec(`
          ALTER TABLE executions ADD COLUMN owner TEXT;
          ALTER TABLE executions ADD COLUMN lease_until REAL;
          ALTER TABLE executions ADD COLUMN fence INTEGER NOT NULL DEFAULT 0;
          ALTER TABLE versions RENAME TO previous_versions;
        `);
      if (version === 3)
        this.db.exec(`
          ALTER TABLE versions ADD COLUMN status TEXT
          CHECK (status IS NULL OR (recipe_id IS NOT NULL AND status IN ('candidate', 'available', 'archived')));
        `);
      this.db.exec(schema);
      if (legacy) {
        const rows = this.db
          .prepare(
            "SELECT kind,data FROM objects WHERE kind IN ('execution','material')",
          )
          .all() as {
          kind: 'execution' | 'material';
          data: string;
        }[];
        for (const { kind, data } of rows)
          this.writeRecord(kind, JSON.parse(data), false);
        this.db.exec(`
          INSERT INTO recipes (id,created_at,updated_at,archived_at,name,revision,
              active_version_id,latest_version_id,latest_qualified_version_id,details)
          SELECT id, json_extract(data,'$.created_at'), json_extract(data,'$.updated_at'),
              json_extract(data,'$.archived_at'), json_extract(data,'$.name'),
              1, json_extract(data,'$.active_version_id'),
              json_extract(data,'$.latest_version_id'), json_extract(data,'$.latest_qualified_version_id'),
              '{}'
          FROM objects WHERE kind='recipe';
          INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key)
          SELECT v.id, CASE WHEN o.kind='recipe' THEN o.id END,
              CASE WHEN o.kind='rubrics' THEN o.id END,
              CASE WHEN o.kind='rubrics' THEN json_remove(o.data, '$.latest_version_id') END,
              v.number,v.data,v.operation_key
          FROM previous_versions v JOIN objects o ON o.id=v.object_id;
        `);
      }
      if (version === 2)
        this.db.exec(`
          INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key)
          SELECT v.id, v.recipe_id, v.rubrics_id,
              CASE WHEN v.rubrics_id IS NOT NULL THEN json_set(
                json_remove(r.details, '$.latest_version_id'),
                '$.id', r.id, '$.created_at', r.created_at, '$.updated_at', r.updated_at,
                '$.archived_at', r.archived_at, '$.revision', r.revision
              ) END,
              v.number,v.data,v.operation_key
          FROM previous_versions v LEFT JOIN rubrics r ON r.id=v.rubrics_id;
        `);
      if (legacy || version === 2)
        this.db.exec(`
          UPDATE executions SET owner=j.owner, lease_until=j.lease_until, fence=j.fence
          FROM jobs j WHERE j.id=executions.id;
          DROP TABLE jobs;
          DROP TABLE previous_versions;
          DROP TABLE IF EXISTS rubrics;
          DROP TABLE IF EXISTS objects;
          DROP TABLE IF EXISTS settings;
        `);
      if (legacy || version === 2 || version === 3)
        this.db.exec(migrateRecipeVersions);
      if (version === 4)
        this.db.exec(`
          DROP VIEW IF EXISTS recipe_records;
          DROP TABLE IF EXISTS recipe_api_metadata;
        `);
      if (
        legacy ||
        version === 2 ||
        version === 3 ||
        version === 4 ||
        version === 5
      ) {
        this.db.exec(`
          ALTER TABLE recipes ADD COLUMN storage_path TEXT NOT NULL DEFAULT '';
          UPDATE recipes SET storage_path = 'recipes/' || id;
        `);
      }
      this.migrateRecipeContent();
      this.db.exec(recipeProjection);
      this.db.pragma('user_version = 7');
    });
  }

  private migrateRecipeContent() {
    const rows = this.db
      .prepare(`
        SELECT r.storage_path, v.data FROM versions v JOIN recipes r ON r.id=v.recipe_id
      `)
      .all() as { storage_path: string; data: string }[];
    const update = this.db.prepare('UPDATE versions SET data=? WHERE id=?');
    for (const row of rows) {
      const version = JSON.parse(row.data) as Version<{
        title: string;
        applicability: string;
        instructions: string;
      }>;
      const { title, applicability, instructions } = version.content;
      const migrated = {
        ...version,
        content: { name: title, description: applicability, instructions },
      };
      this.writeRecipeFile(row.storage_path, migrated);
      update.run(encode(migrated), migrated.id);
    }
  }
  tx<T>(fn: () => T): T {
    return this.db.transaction(fn).immediate();
  }
  private kind(id: string): Kind {
    const kind = id.split('_', 1)[0];
    if (kind !== 'rubrics' && !Object.hasOwn(storage, kind))
      fail('not_found', 'object not found');
    return kind as Kind;
  }
  private record<T>({
    details,
    owner: _owner,
    lease_until: _lease,
    fence: _fence,
    ...fields
  }: Record<string, unknown>): T {
    return { ...JSON.parse(details as string), ...fields } as T;
  }
  private writeRecord(
    kind: Exclude<Kind, 'rubrics'>,
    record: ObjectMetadata | Recipe,
    update: boolean,
  ) {
    const { table, columns } = storage[kind];
    const details: Record<string, unknown> = { ...record };
    const fields = columns.map((column) => {
      const value = details[column] ?? null;
      delete details[column];
      return value;
    });
    const names = [...columns];
    if (kind !== 'recipe') {
      names.push('details');
      fields.push(encode(details));
    }
    if (update)
      this.db
        .prepare(
          `UPDATE ${table} SET ${names
            .slice(1)
            .map((column) => column + '=?')
            .join(',')} WHERE id=?`,
        )
        .run(...fields.slice(1), record.id);
    else
      this.db
        .prepare(
          `INSERT INTO ${table} (${names.join(',')}) VALUES (${names.map(() => '?').join(',')})`,
        )
        .run(...fields);
  }
  private rubricMetadata<T>(row: { id: string; rubrics_metadata: string }): T {
    return {
      ...JSON.parse(row.rubrics_metadata),
      latest_version_id: row.id,
    } as T;
  }
  get<T extends ObjectMetadata | Recipe>(
    id: string,
    kind: Kind = this.kind(id),
  ): T {
    if (kind === 'rubrics') {
      const row = this.db
        .prepare(
          'SELECT id,rubrics_metadata FROM versions WHERE rubrics_id=? ORDER BY number DESC LIMIT 1',
        )
        .get(id) as { id: string; rubrics_metadata: string } | undefined;
      if (!row) return fail('not_found', 'rubrics not found');
      return this.rubricMetadata<T>(row);
    }
    const table = kind === 'recipe' ? 'recipe_records' : storage[kind].table;
    const row = this.db.prepare(`SELECT * FROM ${table} WHERE id=?`).get(id) as
      | Record<string, unknown>
      | undefined;
    if (!row) fail('not_found', `${kind} not found`);
    return kind === 'recipe' ? (row as T) : this.record<T>(row!);
  }
  executionRecipeName(execution: Execution): string | undefined {
    const recipeId =
      execution.recipe_id ?? execution.test_request?.recipe_id ?? null;
    const baseId = execution.base_recipe_version_id ?? null;
    if (!recipeId && !baseId) return execution.name;
    const recipe = this.db
      .prepare(
        `SELECT name FROM recipes
       WHERE id=COALESCE(?, (SELECT object_id FROM versions WHERE id=?))`,
      )
      .get(recipeId, baseId) as { name: string | null } | undefined;
    return recipe?.name ?? execution.name;
  }
  createRecipe(name?: string) {
    return this.insert<Recipe>('recipe', {
      name,
      latest_version_id: null,
      active_version_id: null,
      latest_qualified_version_id: null,
    });
  }
  insert<T extends ObjectMetadata | Recipe>(
    kind: Kind,
    data: Record<string, unknown>,
  ): T {
    const obj = {
      ...data,
      id: data.id ?? uid(kind),
      ...(kind === 'recipe' ? {} : { revision: 1 }),
      created_at: now(),
      updated_at: now(),
      archived_at: null,
    } as unknown as T;
    if (kind === 'recipe')
      Object.assign(obj, { storage_path: `recipes/${obj.id}` });
    if (kind !== 'rubrics') this.writeRecord(kind, { ...obj }, false);
    return obj;
  }
  save<T extends ObjectMetadata | Recipe>(obj: T): T {
    const kind = this.kind(obj.id);
    if (kind === 'rubrics')
      return fail(
        'invalid_state',
        'rubrics metadata is written with its version',
      );
    const saved = { ...obj, updated_at: now() };
    if (kind !== 'recipe' && 'revision' in saved) saved.revision++;
    this.writeRecord(kind, saved, true);
    return saved;
  }
  list<T extends ObjectMetadata | Recipe>(
    kind: Kind,
    options: Pagination = {},
  ): T[] {
    const { limit, offset } = pagination(options);
    if (kind === 'rubrics') {
      const rows = this.db
        .prepare(
          `
        WITH current AS (
          SELECT id,rubrics_id,rubrics_metadata,
                 row_number() OVER (PARTITION BY rubrics_id ORDER BY number DESC) AS position
          FROM versions WHERE rubrics_id IS NOT NULL
        )
        SELECT id,rubrics_metadata FROM current
        WHERE position=1 AND (json_extract(rubrics_metadata,'$.archived_at') IS NOT NULL)=?
        ORDER BY json_extract(rubrics_metadata,'$.created_at'),rubrics_id LIMIT ? OFFSET ?
      `,
        )
        .all(Number(options.archived ?? false), limit, offset) as {
        id: string;
        rubrics_metadata: string;
      }[];
      return rows.map((row) => this.rubricMetadata<T>(row));
    }
    const table = kind === 'recipe' ? 'recipe_records' : storage[kind].table;
    const rows = this.db
      .prepare(
        `SELECT * FROM ${table} WHERE (archived_at IS NOT NULL)=? ORDER BY created_at,id LIMIT ? OFFSET ?`,
      )
      .all(Number(options.archived ?? false), limit, offset) as Record<
      string,
      unknown
    >[];
    return rows.map((row) =>
      kind === 'recipe' ? (row as T) : this.record<T>(row),
    );
  }
  listExecutions(executionIds?: string[]): Execution[] {
    const rows = this.db
      .prepare(
        `SELECT * FROM executions
       WHERE (@ids IS NULL AND archived_at IS NULL)
          OR id IN (SELECT value FROM json_each(@ids))
       ORDER BY created_at, id`,
      )
      .all({
        ids: executionIds === undefined ? null : encode(executionIds),
      }) as Record<string, unknown>[];
    return rows.map((row) => this.record<Execution>(row));
  }
  version<C = unknown>(object_id: string, version_id: string): Version<C> {
    const row = this.db
      .prepare('SELECT data FROM versions WHERE object_id=? AND id=?')
      .get(object_id, version_id) as { data: string } | undefined;
    if (!row)
      fail(
        'not_found',
        'version does not belong to this object or does not exist',
      );
    return JSON.parse(row!.data);
  }
  publishRecipeVersion(recipeId: string, versionId: string): boolean {
    const recipe = this.get<Recipe>(recipeId, 'recipe');
    if (recipe.archived_at) return false;
    const version = this.version(recipeId, versionId);
    const latest = recipe.latest_qualified_version_id
      ? this.version(recipeId, recipe.latest_qualified_version_id)
      : null;
    if (latest && version.number <= latest.number)
      return recipe.active_version_id === versionId;
    this.db
      .prepare(
        "UPDATE versions SET status='archived' WHERE recipe_id=? AND number<? AND status<>'archived'",
      )
      .run(recipeId, version.number);
    this.setRecipeVersionStatus(recipe, versionId, 'available');
    return true;
  }
  setRecipeVersionStatus(
    recipe: Recipe,
    versionId: string,
    status: 'available' | 'archived',
  ) {
    const result = this.db
      .prepare(
        'UPDATE versions SET status=? WHERE recipe_id=? AND id=? AND status<>?',
      )
      .run(status, recipe.id, versionId, status);
    if (result.changes) this.save(recipe);
  }
  version_by_id<C = unknown>(id: string, kind: Kind): Version<C> {
    const row = this.db
      .prepare('SELECT object_id FROM versions WHERE id=?')
      .get(id) as { object_id: string } | undefined;
    if (!row) fail('not_found', 'version not found');
    this.get(row!.object_id, kind);
    return this.version(row!.object_id, id);
  }
  operation<C = unknown>(key: string): Version<C> | undefined {
    const row = this.db
      .prepare('SELECT data FROM versions WHERE operation_key=?')
      .get(key) as { data: string } | undefined;
    return row ? JSON.parse(row.data) : undefined;
  }
  append<C>(
    obj: ObjectMetadata | Recipe,
    content: C,
    operation_key?: string,
    provenance: Record<string, unknown> = {},
    recipeMarkdown?: string,
  ): Version<C> {
    if (operation_key) {
      const prev = this.operation<C>(operation_key);
      if (prev) return prev;
    }
    const { number } = this.db
      .prepare(
        'SELECT COALESCE(MAX(number),0)+1 AS number FROM versions WHERE object_id=?',
      )
      .get(obj.id) as { number: number };
    const version = {
      id: uid('version'),
      object_id: obj.id,
      number,
      content,
      provenance,
      created_at: now(),
    };
    const kind = this.kind(obj.id);
    const { latest_version_id: _latest, ...metadata } = obj as Versioned;
    this.db
      .prepare(
        'INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key,status) VALUES (?,?,?,?,?,?,?,?)',
      )
      .run(
        version.id,
        kind === 'recipe' ? obj.id : null,
        kind === 'rubrics' ? obj.id : null,
        kind === 'rubrics'
          ? encode({
              ...metadata,
              revision: metadata.revision + 1,
              updated_at: now(),
            })
          : null,
        number,
        encode(version),
        operation_key ?? null,
        kind === 'recipe' ? 'candidate' : null,
      );
    if (kind === 'recipe') {
      const recipe = obj as Recipe;
      this.writeRecipeFile(
        recipe.storage_path,
        version as Version<RecipeContent>,
        recipeMarkdown,
      );
      this.save(recipe);
    }
    return version;
  }
  private writeRecipeFile(
    storagePath: string,
    version: Version<RecipeContent>,
    markdown?: string,
  ) {
    if (
      !/^[A-Za-z0-9_-]+$/u.test(version.object_id) ||
      !/^[A-Za-z0-9_-]+$/u.test(version.id) ||
      storagePath !== `recipes/${version.object_id}`
    )
      fail('invalid_state', 'invalid Recipe version storage path');
    const directory = join(this.root, storagePath, version.id);
    const target = join(directory, 'RECIPE.md');
    const temporary = join(directory, `.${randomUUID()}.tmp`);
    const { name, description, instructions } = version.content;
    const frontmatter = `---\nname: ${JSON.stringify(name)}\ndescription: ${JSON.stringify(description)}\n---\n`;
    const body =
      instructions && !instructions.endsWith('\n')
        ? `${instructions}\n`
        : instructions;
    const source = markdown ?? (body ? `${frontmatter}\n${body}` : frontmatter);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    try {
      writeFileSync(temporary, source, {
        flag: 'wx',
        mode: 0o600,
        flush: true,
      });
      renameSync(temporary, target);
    } finally {
      rmSync(temporary, { force: true });
    }
  }
  replay<T>(
    scope: string,
    key: string | undefined,
    request: unknown,
  ): T | undefined {
    if (!key) return;
    const row = this.db
      .prepare('SELECT request,result FROM idempotency WHERE scope=? AND key=?')
      .get(scope, key) as { request: string; result: string } | undefined;
    if (row && row.request !== encode(request))
      fail('conflict', 'idempotency key reused with different arguments');
    return row ? JSON.parse(row.result) : undefined;
  }
  remember(
    scope: string,
    key: string | undefined,
    request: unknown,
    result: unknown,
  ) {
    if (key)
      this.db
        .prepare('INSERT INTO idempotency VALUES (?,?,?,?)')
        .run(scope, key, encode(request), encode(result));
  }
  reserve(
    kind: 'model' | 'worker' | 'recovery' | `recovery:${string}`,
    owner: string,
    limit: number,
    ttl: number,
    onlyIfPending = false,
  ): string | null {
    return this.tx(() => {
      this.db.prepare('DELETE FROM slots WHERE expires<?').run(now());
      const { count } = this.db
        .prepare('SELECT COUNT(*) AS count FROM slots WHERE kind=?')
        .get(kind) as { count: number };
      if (count >= limit) return null;
      if (kind === 'worker' && onlyIfPending) {
        const { pending } = this.db
          .prepare(
            "SELECT COUNT(*) AS pending FROM executions WHERE status IN ('queued','running')",
          )
          .get() as { pending: number };
        if (count >= pending) return null;
      }
      const token = uid('slot');
      this.db
        .prepare('INSERT INTO slots VALUES (?,?,?,?)')
        .run(token, kind, owner, now() + ttl);
      return token;
    });
  }
  release(token: string) {
    this.db.prepare('DELETE FROM slots WHERE token=?').run(token);
  }
  heartbeat(owner: string, ttl: number, slot: string) {
    this.tx(() => {
      const time = now();
      if (
        this.db
          .prepare(
            'UPDATE slots SET expires=? WHERE token=? AND owner=? AND expires>=?',
          )
          .run(time + ttl, slot, owner, time).changes !== 1
      )
        fail('lease_lost', 'worker slot expired');
      this.db
        .prepare('UPDATE slots SET expires=? WHERE owner=? AND expires>=?')
        .run(time + ttl, owner, time);
      this.db
        .prepare(
          "UPDATE executions SET lease_until=? WHERE owner=? AND lease_until>=? AND status='running'",
        )
        .run(time + ttl, owner, time);
    });
  }
  claim(
    owner: string,
    ttl: number,
    limit: number,
    credentials?: string[],
    recipeId?: string,
  ): { id: string; fence: number } | null {
    return this.tx(() => {
      const time = now();
      const { count } = this.db
        .prepare(
          "SELECT COUNT(*) AS count FROM executions WHERE status='running' AND lease_until>=?",
        )
        .get(time) as { count: number };
      if (count >= limit) return null;
      const row = this.db
        .prepare(
          `SELECT executions.id, executions.fence FROM executions
           WHERE (executions.status='queued' OR (executions.status='running' AND executions.lease_until<?))
           AND COALESCE(json_extract(executions.details, '$.runtime_config.max_executions'), ?) > ?
           AND (? IS NULL OR json_extract(executions.details, '$.runtime_config.model.api_key_env') IN (SELECT value FROM json_each(?)))
           AND (? IS NULL OR executions.recipe_id = ?)
           ORDER BY executions.created_at,executions.id LIMIT 1`,
        )
        .get(
          time,
          limit,
          count,
          credentials ? encode(credentials) : null,
          credentials ? encode(credentials) : null,
          recipeId ?? null,
          recipeId ?? null,
        ) as { id: string; fence: number } | undefined;
      if (!row) return null;
      this.db
        .prepare(
          'UPDATE executions SET owner=?,lease_until=?,fence=fence+1 WHERE id=?',
        )
        .run(owner, time + ttl, row.id);
      this.save({
        ...this.get<Execution>(row.id, 'execution'),
        status: 'running',
      });
      return { id: row.id, fence: row.fence + 1 };
    });
  }
  guard(
    id: string,
    owner: string,
    fence: number,
    revision?: number,
  ): Execution {
    const job = this.db
      .prepare(
        'SELECT owner,lease_until,fence,status FROM executions WHERE id=?',
      )
      .get(id) as
      | { owner: string; fence: number; lease_until: number; status: string }
      | undefined;
    if (
      !job ||
      job.owner !== owner ||
      job.fence !== fence ||
      job.lease_until == null ||
      job.lease_until < now() ||
      job.status !== 'running'
    )
      fail('lease_lost', 'execution cancelled or worker lease expired');
    const execution = this.get<Execution>(id, 'execution');
    if (revision !== undefined && execution.input_revision !== revision)
      fail('input_changed', 'execution inputs changed');
    return execution;
  }
  pending_execution(credentials?: string[], recipeId?: string) {
    return this.db
      .prepare(
        `SELECT executions.id AS id, json_extract(executions.details, '$.runtime_config') AS config
       FROM executions
       WHERE executions.status IN ('queued','running')
       AND (? IS NULL OR json_extract(executions.details, '$.runtime_config.model.api_key_env') IN (SELECT value FROM json_each(?)))
       AND (? IS NULL OR executions.recipe_id = ?)
       ORDER BY executions.created_at, executions.id LIMIT 1`,
      )
      .get(
        credentials ? encode(credentials) : null,
        credentials ? encode(credentials) : null,
        recipeId ?? null,
        recipeId ?? null,
      ) as { id: string; config: string } | undefined;
  }
  has_pending(credentials?: string[], recipeId?: string): boolean {
    return !!this.pending_execution(credentials, recipeId);
  }
  pending_count(credentials: string[], recipeId?: string): number {
    return this.db
      .prepare(`
        SELECT COUNT(*) FROM executions WHERE status IN ('queued','running')
        AND json_extract(details, '$.runtime_config.model.api_key_env') IN (SELECT value FROM json_each(?))
        AND (? IS NULL OR recipe_id = ?)
      `)
      .pluck()
      .get(encode(credentials), recipeId ?? null, recipeId ?? null) as number;
  }
  close() {
    this.db.close();
  }
}
