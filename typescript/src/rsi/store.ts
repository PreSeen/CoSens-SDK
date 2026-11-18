import Database from 'better-sqlite3';
import { randomUUID } from 'node:crypto';
import { mkdirSync, chmodSync } from 'node:fs';
import { resolve, join } from 'node:path';
import {
  fail,
  type Kind,
  type ObjectMetadata,
  type Version,
  type Pagination,
  type Execution,
  type LocalConfig,
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
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY,kind TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL,revision INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS objects_kind ON objects(kind,created,id);
      CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY,object_id TEXT NOT NULL REFERENCES objects(id),number INTEGER NOT NULL,data TEXT NOT NULL,operation_key TEXT UNIQUE,UNIQUE(object_id,number));
      CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY REFERENCES objects(id),status TEXT NOT NULL,owner TEXT,lease_until REAL,fence INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created,id);
      CREATE TABLE IF NOT EXISTS slots (token TEXT PRIMARY KEY,kind TEXT NOT NULL,owner TEXT NOT NULL,expires REAL NOT NULL);
      CREATE INDEX IF NOT EXISTS slots_kind ON slots(kind,expires);
      CREATE TABLE IF NOT EXISTS idempotency (scope TEXT NOT NULL,key TEXT NOT NULL,request TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(scope,key));
    `);
    chmodSync(path, 0o600);
  }
  tx<T>(fn: () => T): T {
    return this.db.transaction(fn).immediate();
  }
  configure(config?: LocalConfig): LocalConfig {
    return this.tx(() => {
      const row = this.db
        .prepare('SELECT data FROM settings WHERE id=1')
        .get() as { data: string } | undefined;
      if (row) {
        const saved = JSON.parse(row.data);
        if (saved.runtime !== 'typescript-v2')
          fail('conflict', 'incompatible RSI runtime');
        if (config && encode(config) !== encode(saved.config))
          fail('conflict', 'data directory has different runtime settings');
        return saved.config;
      }
      if (!config)
        fail('invalid_state', 'initialize this directory with config first');
      this.db
        .prepare('INSERT INTO settings VALUES (1,?)')
        .run(encode({ runtime: 'typescript-v2', config }));
      return config!;
    });
  }
  get<T extends ObjectMetadata>(id: string, kind?: Kind): T {
    const row = this.db
      .prepare('SELECT kind,data FROM objects WHERE id=?')
      .get(id) as { kind: string; data: string } | undefined;
    if (!row || (kind && row.kind !== kind))
      fail('not_found', `${kind ?? 'object'} not found`);
    return JSON.parse(row!.data) as T;
  }
  insert<T extends ObjectMetadata>(
    kind: Kind,
    data: Record<string, unknown>,
  ): T {
    const obj = {
      ...data,
      id: data.id ?? uid(kind),
      revision: 1,
      created_at: now(),
      updated_at: now(),
      archived_at: null,
    } as unknown as T;
    this.db
      .prepare('INSERT INTO objects VALUES (?,?,?,?,?)')
      .run(obj.id, kind, encode(obj), obj.created_at, 1);
    if (kind === 'execution')
      this.db
        .prepare('INSERT INTO jobs(id,status,created) VALUES (?,?,?)')
        .run(obj.id, (obj as unknown as Execution).status, obj.created_at);
    return obj;
  }
  save<T extends ObjectMetadata>(obj: T): T {
    const saved = { ...obj, revision: obj.revision + 1, updated_at: now() };
    this.db
      .prepare('UPDATE objects SET data=?,revision=? WHERE id=?')
      .run(encode(saved), saved.revision, obj.id);
    if (obj.id.startsWith('execution_'))
      this.db
        .prepare('UPDATE jobs SET status=? WHERE id=?')
        .run((saved as unknown as Execution).status, obj.id);
    return saved;
  }
  list<T extends ObjectMetadata>(kind: Kind, options: Pagination = {}): T[] {
    const { limit, offset } = pagination(options);
    return (
      this.db
        .prepare(
          "SELECT data FROM objects WHERE kind=? AND ((json_extract(data,'$.archived_at') IS NOT NULL)=?) ORDER BY created,id LIMIT ? OFFSET ?",
        )
        .all(kind, Number(options.archived ?? false), limit, offset) as {
        data: string;
      }[]
    ).map((r) => JSON.parse(r.data));
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
    obj: ObjectMetadata,
    content: C,
    operation_key?: string,
    provenance: Record<string, unknown> = {},
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
    this.db
      .prepare('INSERT INTO versions VALUES (?,?,?,?,?)')
      .run(version.id, obj.id, number, encode(version), operation_key ?? null);
    this.save({ ...obj, latest_version_id: version.id });
    return version;
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
    kind: 'model' | 'worker' | 'recovery',
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
            "SELECT COUNT(*) AS pending FROM jobs WHERE status IN ('queued','running')",
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
          "UPDATE jobs SET lease_until=? WHERE owner=? AND lease_until>=? AND status='running'",
        )
        .run(time + ttl, owner, time);
    });
  }
  claim(
    owner: string,
    ttl: number,
    limit: number,
  ): { id: string; fence: number } | null {
    return this.tx(() => {
      const time = now();
      const { count } = this.db
        .prepare(
          "SELECT COUNT(*) AS count FROM jobs WHERE status='running' AND lease_until>=?",
        )
        .get(time) as { count: number };
      if (count >= limit) return null;
      const row = this.db
        .prepare(
          "SELECT id,fence FROM jobs WHERE status='queued' OR (status='running' AND lease_until<?) ORDER BY created,id LIMIT 1",
        )
        .get(time) as { id: string; fence: number } | undefined;
      if (!row) return null;
      this.db
        .prepare(
          "UPDATE jobs SET owner=?,lease_until=?,fence=fence+1,status='running' WHERE id=?",
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
    const job = this.db.prepare('SELECT * FROM jobs WHERE id=?').get(id) as
      | { owner: string; fence: number; lease_until: number; status: string }
      | undefined;
    if (
      !job ||
      job.owner !== owner ||
      job.fence !== fence ||
      job.lease_until < now() ||
      job.status !== 'running'
    )
      fail('lease_lost', 'execution cancelled or worker lease expired');
    const execution = this.get<Execution>(id, 'execution');
    if (revision !== undefined && execution.input_revision !== revision)
      fail('input_changed', 'execution inputs changed');
    return execution;
  }
  has_pending(): boolean {
    return !!this.db
      .prepare(
        "SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1",
      )
      .get();
  }
  close() {
    this.db.close();
  }
}
