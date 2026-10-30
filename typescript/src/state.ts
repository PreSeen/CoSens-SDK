import Database from 'better-sqlite3';
import { createHash } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

export function defaultStatePath() { return join(homedir(), '.cosens', 'typescript-state.sqlite3'); }
export function canonical(value: unknown): string {
  const json = JSON.stringify(value);
  if (json === undefined) throw new TypeError('State value must be JSON serializable');
  const write = (item: any): string => {
    if (Array.isArray(item)) return `[${item.map(write).join(',')}]`;
    if (item && typeof item === 'object') {
      const keys = Object.keys(item).sort((a, b) => Buffer.compare(Buffer.from(a, 'utf8'), Buffer.from(b, 'utf8')));
      return `{${keys.map(key => `${JSON.stringify(key)}:${write(item[key])}`).join(',')}}`;
    }
    return JSON.stringify(item);
  };
  return write(JSON.parse(json));
}
export function hash(value: unknown): string { return createHash('sha256').update(canonical(value)).digest('hex'); }
export function openState(path = defaultStatePath()): Database.Database {
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });
  const db = new Database(path);
  db.pragma('journal_mode = WAL');
  db.pragma('busy_timeout = 5000');
  return db;
}

export class TurnStore {
  private readonly db: Database.Database;
  private readonly prefix: string;
  constructor(path: string | undefined, principal: string, serviceId: string) {
    this.db = openState(path);
    this.db.exec('CREATE TABLE IF NOT EXISTS ts_turns (key TEXT PRIMARY KEY, value TEXT NOT NULL)');
    this.prefix = `${serviceId}:${hash(principal)}:`;
  }
  get<T extends object = Record<string, any>>(id: string): T | undefined {
    const row = this.db.prepare('SELECT value FROM ts_turns WHERE key=?').get(this.prefix + id) as { value: string } | undefined;
    return row ? JSON.parse(row.value) as T : undefined;
  }
  put(id: string, value: object): void {
    this.db.prepare('INSERT INTO ts_turns(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value').run(this.prefix + id, JSON.stringify(value));
  }
  putMany(ids: readonly string[], value: object): void {
    const stored = JSON.stringify(value);
    const statement = this.db.prepare('INSERT INTO ts_turns(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value');
    this.db.transaction(() => {
      for (const id of new Set(ids)) statement.run(this.prefix + id, stored);
    })();
  }
  claimState(id: string, expectedState: string, value: { state: string; executionId?: string; requestSha256?: string }): boolean {
    return this.db.transaction(() => {
      const key = this.prefix + id;
      const row = this.db.prepare('SELECT value FROM ts_turns WHERE key=?').get(key) as { value: string } | undefined;
      const current = row ? JSON.parse(row.value) : undefined;
      if (!current || current.state !== expectedState || current.executionId !== value.executionId || current.requestSha256 !== value.requestSha256) return false;
      this.db.prepare('UPDATE ts_turns SET value=? WHERE key=?').run(JSON.stringify(value), key);
      return true;
    }).immediate();
  }
  insertIfAbsent(id: string, value: object): boolean {
    const result = this.db.prepare('INSERT OR IGNORE INTO ts_turns(key,value) VALUES(?,?)').run(this.prefix + id, JSON.stringify(value));
    return result.changes === 1;
  }
  updatePending(id: string, value: { state: string; taskSpec?: unknown; mode?: string; taskId?: string; executionId?: string; requestSha256?: string }): boolean {
    return this.db.transaction(() => {
      const key = this.prefix + id;
      const row = this.db.prepare('SELECT value FROM ts_turns WHERE key=?').get(key) as { value: string } | undefined;
      const current = row ? JSON.parse(row.value) : undefined;
      if (!current || current.state !== 'pre_execution_pending' || canonical(current.taskSpec) !== canonical(value.taskSpec) || current.mode !== value.mode ||
        (current.taskId && current.taskId !== value.taskId) ||
        (current.executionId && current.executionId !== value.executionId) ||
        (current.requestSha256 && current.requestSha256 !== value.requestSha256)) return false;
      this.db.prepare('UPDATE ts_turns SET value=? WHERE key=?').run(JSON.stringify(value), key);
      return true;
    }).immediate();
  }
  close() { this.db.close(); }
}
