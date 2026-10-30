import type Database from 'better-sqlite3';
import { randomUUID } from 'node:crypto';
import { RSIRecoveryRequired, SessionHistoryChanged } from './errors.js';
import { canonical, hash, openState } from './state.js';

export interface SessionLike { sessionId?: string; session_id?: string; id?: string; getSessionId?(): Promise<string>; sessionIncarnation?: string; incarnation?: string; getItems(): Promise<readonly any[]> | readonly any[] }
export interface Snapshot { key: string; sessionId: string; items: readonly Record<string, any>[] }
export interface SessionEvent { id: string; kind: string; payload: Record<string, any> }
interface Row { acked_index: number; prefix_hash: string | null; run_id: string | null; pending_events: string | null }

export class CursorStore {
  private readonly db: Database.Database;
  private readonly principal: string;
  constructor(path: string | undefined, principal: string, private readonly serviceId: string) {
    this.db = openState(path);
    this.principal = hash(principal);
    this.db.exec('CREATE TABLE IF NOT EXISTS ts_cursors (key TEXT PRIMARY KEY, acked_index INTEGER NOT NULL, prefix_hash TEXT, run_id TEXT, pending_events TEXT, acked_message_id TEXT, acked_item_hash TEXT, source_revision TEXT)');
    this.db.exec('CREATE TABLE IF NOT EXISTS ts_session_locks (key TEXT PRIMARY KEY, owner TEXT NOT NULL, pid INTEGER NOT NULL)');
  }
  private async identity(session: SessionLike): Promise<{ key: string; sessionId: string }> {
    const id = session.sessionId ?? session.session_id ?? session.id ?? await session.getSessionId?.();
    if (!id) throw new TypeError('Session must expose a stable sessionId');
    const sessionId = String(id);
    return { key: hash([this.serviceId, this.principal, sessionId, String(session.sessionIncarnation ?? session.incarnation ?? '0')]), sessionId };
  }
  async acquireSession(session: SessionLike): Promise<() => void> {
    const { key } = await this.identity(session);
    const owner = randomUUID();
    while (true) {
      const result = this.db.prepare('INSERT OR IGNORE INTO ts_session_locks(key,owner,pid) VALUES(?,?,?)').run(key, owner, process.pid);
      if (result.changes === 1) {
        return () => { this.db.prepare('DELETE FROM ts_session_locks WHERE key=? AND owner=?').run(key, owner); };
      }
      const held = this.db.prepare('SELECT owner,pid FROM ts_session_locks WHERE key=?').get(key) as { owner: string; pid: number } | undefined;
      if (held) {
        let alive = true;
        try { process.kill(held.pid, 0); }
        catch (error) { if ((error as NodeJS.ErrnoException).code === 'ESRCH') alive = false; else throw error; }
        if (!alive) this.db.prepare('DELETE FROM ts_session_locks WHERE key=? AND owner=?').run(key, held.owner);
      }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
  }
  async snapshot(session: SessionLike): Promise<Snapshot> {
    const { key, sessionId } = await this.identity(session);
    const items = await session.getItems();
    if (!Array.isArray(items)) throw new TypeError('Session.getItems must return an array');
    const plain = items.map(item => structuredClone(typeof item?.toJSON === 'function' ? item.toJSON() : item));
    const snapshot = { key, sessionId, items: plain };
    const ids = new Set<string>();
    for (let index = 0; index < plain.length; index++) {
      const id = this.event(snapshot, index).id;
      if (ids.has(id)) throw new SessionHistoryChanged(`Session contains duplicate message ID ${id}`);
      ids.add(id);
    }
    return snapshot;
  }
  private row(key: string): Row { return (this.db.prepare('SELECT acked_index,prefix_hash,run_id,pending_events FROM ts_cursors WHERE key=?').get(key) as Row | undefined) ?? { acked_index: -1, prefix_hash: null, run_id: null, pending_events: null }; }
  private validate(snapshot: Snapshot, row: Row) {
    if (row.acked_index >= snapshot.items.length || (row.acked_index >= 0 && hash(snapshot.items.slice(0, row.acked_index + 1)) !== row.prefix_hash)) throw new SessionHistoryChanged('Previously ACKed Session history changed');
  }
  private event(snapshot: Snapshot, index: number): SessionEvent {
    const item = snapshot.items[index]!;
    const role = String(item.role ?? item.type ?? 'unknown');
    const messageId = String(item.id || hash([snapshot.key, index, item]));
    const kind = ({ user: 'user_prompt', assistant: 'agent_action', tool: 'tool_result' } as Record<string, string>)[role] ?? 'context_delivery';
    return { id: `chat:${messageId}`, kind, payload: { session_id: snapshot.sessionId, message_id: messageId, index, role, content: item.content ?? item, source_revision: item.source_revision ?? null } };
  }
  private validateBatch(snapshot: Snapshot, events: SessionEvent[], start: number) {
    for (let i = 0; i < events.length; i++) if (start + i >= snapshot.items.length || canonical(events[i]) !== canonical(this.event(snapshot, start + i))) throw new SessionHistoryChanged('Pending Session event differs from current history');
  }
  pending(snapshot: Snapshot, maxEvents = 200, maxBytes = 1_000_000): SessionEvent[] {
    const row = this.row(snapshot.key); this.validate(snapshot, row);
    const events: SessionEvent[] = []; let size = 0;
    for (let i = row.acked_index + 1; i < snapshot.items.length; i++) {
      const event = this.event(snapshot, i), bytes = Buffer.byteLength(canonical(event), 'utf8');
      if (bytes > maxBytes) throw new RangeError('One Session event exceeds the server batch size');
      if (events.length && (events.length >= maxEvents || size + bytes > maxBytes)) break;
      events.push(event); size += bytes;
    }
    return events;
  }
  beginBatch(runId: string, snapshot: Snapshot, events: SessionEvent[]): void {
    if (!events.length) return;
    this.db.transaction(() => {
      const row = this.row(snapshot.key); this.validate(snapshot, row);
      if (row.pending_events) {
        if (row.run_id !== runId || canonical(JSON.parse(row.pending_events)) !== canonical(events)) throw new RSIRecoveryRequired('Pending Session batch belongs to another Run or changed');
      } else this.validateBatch(snapshot, events, row.acked_index + 1);
      this.db.prepare('INSERT INTO ts_cursors(key,acked_index,prefix_hash,run_id,pending_events) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET run_id=excluded.run_id,pending_events=excluded.pending_events').run(snapshot.key, row.acked_index, row.prefix_hash, runId, canonical(events));
    })();
  }
  recoverBatch(snapshot: Snapshot): { runId: string; events: SessionEvent[] } | undefined {
    const row = this.row(snapshot.key); this.validate(snapshot, row);
    if (!row.pending_events) return undefined;
    const events = JSON.parse(row.pending_events) as SessionEvent[];
    this.validateBatch(snapshot, events, row.acked_index + 1);
    return { runId: row.run_id!, events };
  }
  ack(snapshot: Snapshot, receipt: { durable_ack?: string[] | { event_ids?: string[]; ids?: string[] } }): number {
    const ids = Array.isArray(receipt.durable_ack) ? receipt.durable_ack : receipt.durable_ack?.event_ids ?? receipt.durable_ack?.ids;
    if (!Array.isArray(ids)) throw new RSIRecoveryRequired('Session event receipt has no durable_ack event IDs');
    return this.db.transaction(() => {
      const row = this.row(snapshot.key); this.validate(snapshot, row);
      if (!row.pending_events) throw new RSIRecoveryRequired('No pending Session event batch to acknowledge');
      const pending = JSON.parse(row.pending_events) as SessionEvent[];
      this.validateBatch(snapshot, pending, row.acked_index + 1);
      const pendingIds = new Set(pending.map(event => event.id));
      const set = new Set<string>();
      for (const id of ids) {
        if (typeof id !== 'string' || !pendingIds.has(id) || set.has(id)) {
          throw new RSIRecoveryRequired('Session event ACK contains an unsent or duplicate event ID');
        }
        set.add(id);
      }
      let advanced = 0;
      while (advanced < pending.length && set.has(pending[advanced]!.id)) advanced++;
      const newIndex = row.acked_index + advanced, item = snapshot.items[newIndex];
      const result = this.db.prepare('UPDATE ts_cursors SET acked_index=?,prefix_hash=?,pending_events=?,acked_message_id=?,acked_item_hash=?,source_revision=? WHERE key=? AND acked_index=? AND run_id=? AND pending_events=?').run(
        newIndex, newIndex < 0 ? null : hash(snapshot.items.slice(0, newIndex + 1)), advanced === pending.length ? null : canonical(pending.slice(advanced)),
        item ? String(item.id || hash([snapshot.key, newIndex, item])) : null, item ? hash(item) : null, item?.source_revision ?? null,
        snapshot.key, row.acked_index, row.run_id, row.pending_events,
      );
      if (result.changes !== 1) throw new RSIRecoveryRequired('Session cursor changed concurrently');
      return advanced;
    })();
  }
  close() { this.db.close(); }
}
