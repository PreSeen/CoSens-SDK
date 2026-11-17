import type Database from 'better-sqlite3';
import { randomUUID } from 'node:crypto';
import { SessionHistoryChanged } from './errors.js';
import { hash, openState } from './state.js';

export interface SessionLike { sessionId?: string; session_id?: string; id?: string; getSessionId?(): Promise<string>; sessionIncarnation?: string; incarnation?: string; getItems(): Promise<readonly any[]> | readonly any[] }
export interface Snapshot { key: string; sessionId: string; items: readonly Record<string, any>[] }
export interface SessionEvent { id: string; kind: string; payload: Record<string, any> }

export class CursorStore {
  private readonly db: Database.Database;
  private readonly principal: string;
  constructor(path: string | undefined, principal: string, private readonly serviceId: string) {
    this.db = openState(path);
    this.principal = hash(principal);
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
  private event(snapshot: Snapshot, index: number): SessionEvent {
    const item = snapshot.items[index]!;
    const role = String(item.role ?? item.type ?? 'unknown');
    const messageId = String(item.id || hash([snapshot.key, index, item]));
    const kind = ({ user: 'user_prompt', assistant: 'agent_action', tool: 'tool_result' } as Record<string, string>)[role] ?? 'context_delivery';
    return { id: `chat:${messageId}`, kind, payload: { session_id: snapshot.sessionId, message_id: messageId, index, role, content: item.content ?? item, source_revision: item.source_revision ?? null } };
  }
  close() { this.db.close(); }
}
