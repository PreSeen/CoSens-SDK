import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { RSIClient } from './client.js';
import { Engine } from './engine.js';
import { LangChainRuntime } from './runtime.js';
import { RSIError, type Runtime } from './models.js';
import { now, uid } from './store.js';

export async function drain(
  client: RSIClient,
  runtime?: Runtime,
  owner = uid('worker'),
  reservedSlot?: string,
) {
  const { store, config } = client;
  const slot =
    reservedSlot ??
    store.reserve('worker', owner, config.max_executions, config.lease_seconds);
  if (!slot) return { processed: 0 };
  let leaseError: unknown;
  let active: { id: string; fence: number } | null = null;
  const renew = () => store.heartbeat(owner, config.lease_seconds, slot);
  const heartbeat = setInterval(
    () => {
      try {
        renew();
      } catch (error) {
        leaseError = error;
      }
    },
    (config.lease_seconds * 1000) / 3,
  );
  const agent =
    runtime ??
    new LangChainRuntime(client, owner, () => {
      if (leaseError) throw leaseError;
      if (active) store.guard(active.id, owner, active.fence);
    });
  let processed = 0;
  try {
    renew();
    let idleSince = Date.now();
    while (!leaseError) {
      active = store.claim(owner, config.lease_seconds, config.max_executions);
      if (!active) {
        if (Date.now() - idleSince >= 1000) {
          const emptied = store.tx(() => {
            const pending = store.db
              .prepare(
                "SELECT 1 FROM jobs WHERE status='queued' OR (status='running' AND lease_until<?) LIMIT 1",
              )
              .get(now());
            if (!pending) store.release(slot);
            return !pending;
          });
          if (emptied) break;
          idleSince = Date.now();
        }
        await sleep(50);
        continue;
      }
      const { id, fence } = active;
      try {
        await new Engine(client, agent, owner, fence, id).run();
      } catch (error) {
        if (!(error instanceof RSIError && error.code === 'lease_lost')) {
          try {
            store.tx(() => {
              const execution = store.guard(id, owner, fence);
              store.save({
                ...execution,
                status: 'failed',
                stage: 'failed',
                error: failureDetails(error, execution.stage),
              });
            });
          } catch (persistError) {
            if (
              !(
                persistError instanceof RSIError &&
                persistError.code === 'lease_lost'
              )
            )
              throw persistError;
          }
        }
      } finally {
        active = null;
      }
      processed++;
      idleSince = Date.now();
    }
    return { processed };
  } finally {
    clearInterval(heartbeat);
    store.release(slot);
  }
}

function failureDetails(error: unknown, stage: string) {
  const status =
    error && typeof error === 'object' && 'status' in error
      ? error.status
      : undefined;
  const info = { stage, type: error instanceof Error ? error.name : 'Error' };
  if (typeof status !== 'number')
    return {
      ...info,
      code: error instanceof RSIError ? error.code : 'execution_failed',
    };
  const code =
    status === 402
      ? 'model_payment_required'
      : status === 429
        ? 'model_rate_limited'
        : status >= 500
          ? 'model_unavailable'
          : 'model_request_rejected';
  return { ...info, code, upstream_status: status };
}

if (
  process.argv[1] &&
  pathToFileURL(process.argv[1]).href === import.meta.url
) {
  const [data_dir, owner, slot] = process.argv.slice(2);
  try {
    if (!data_dir || Boolean(owner) !== Boolean(slot))
      throw new Error('worker expects a data directory and optional lease');
    const client = new RSIClient({ data_dir, auto_start: false });
    try {
      await drain(client, undefined, owner, slot);
    } finally {
      client.close();
    }
  } catch {
    // The host can recover a job after lease expiry. Avoid exposing model data in process logs.
    process.exitCode = 1;
  }
}
