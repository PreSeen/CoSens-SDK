import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';
import { parseArgs } from 'node:util';
import { RSIClient } from './client.js';
import { Engine } from './engine.js';
import { ChatRuntime } from './runtime.js';
import {
  LocalConfigSchema,
  RSIError,
  type Execution,
  type Runtime,
} from './models.js';
import { now, uid } from './store.js';

export async function drain(
  client: RSIClient,
  runtime?: Runtime,
  owner = uid('worker'),
  reservedSlot?: string,
  recipeId?: string,
) {
  const { store, config } = client;
  const credentials = runtime ? undefined : client.credentialNames();
  const slot =
    reservedSlot ??
    store.reserve('worker', owner, config.max_executions, config.lease_seconds);
  if (!slot) return { processed: 0 };
  let leaseError: unknown;
  let active: { id: string; fence: number } | null = null;
  let leaseSeconds = config.lease_seconds;
  const renew = () => store.heartbeat(owner, leaseSeconds, slot);
  const heartbeat = setInterval(
    () => {
      try {
        renew();
      } catch (error) {
        leaseError = error;
      }
    },
    1000, // One third of the minimum supported lease.
  );
  let processed = 0;
  try {
    renew();
    let idleSince = Date.now();
    while (!leaseError) {
      active = store.claim(
        owner,
        config.lease_seconds,
        config.max_executions,
        credentials,
        recipeId,
      );
      if (!active) {
        if (Date.now() - idleSince >= 1000) {
          const emptied = store.tx(() => {
            // A detached worker can pick up an interrupted job once its lease expires.
            const pending = reservedSlot || credentials || recipeId !== undefined
              ? store.has_pending(credentials, recipeId)
              : store.db
                  .prepare(
                    "SELECT 1 FROM executions WHERE status='queued' OR (status='running' AND lease_until<?) LIMIT 1",
                  )
                  .get(now());
            if (!pending) store.release(slot);
            return !pending;
          });
          if (emptied) break;
          idleSince = Date.now();
        }
        await sleep(reservedSlot ? 200 : 50);
        continue;
      }
      const { id, fence } = active;
      let executionClient: RSIClient | undefined;
      try {
        executionClient = new RSIClient({
          data_dir: client.data_dir,
          config: store.get<Execution>(id, 'execution').runtime_config,
          auto_start: false,
        });
        leaseSeconds = executionClient.config.lease_seconds;
        renew();
        const agent =
          runtime ??
          new ChatRuntime(executionClient, owner, () => {
            if (leaseError) throw leaseError;
            if (active) store.guard(active.id, owner, active.fence);
          });
        await new Engine(executionClient, agent, owner, fence, id).run();
      } catch (error) {
        if (!(error instanceof RSIError && error.code === 'lease_lost')) {
          try {
            store.tx(() => {
              const execution = store.guard(id, owner, fence);
              const failure = failureDetails(error, execution.stage);
              const pending = execution.recovery_config;
              const useRefreshedCredentials = pending &&
                pending.model.api_key_env !== execution.runtime_config.model.api_key_env &&
                'upstream_status' in failure &&
                (failure.upstream_status === 401 || failure.upstream_status === 403);
              store.save({
                ...execution,
                runtime_config: useRefreshedCredentials ? pending : execution.runtime_config,
                recovery_config: undefined,
                status: useRefreshedCredentials ? 'queued' : 'failed',
                stage: useRefreshedCredentials ? 'queued' : 'failed',
                error: useRefreshedCredentials ? null : failure,
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
        executionClient?.close();
        active = null;
        leaseSeconds = config.lease_seconds;
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

async function waitForWorkerSlot(
  client: RSIClient,
  owner: string,
  waitingSlot: string,
  recipeId?: string,
) {
  const { store, config } = client;
  const credentials = client.credentialNames();
  let nextHeartbeat = 0;
  try {
    for (;;) {
      if (now() >= nextHeartbeat) {
        store.heartbeat(owner, config.lease_seconds, waitingSlot);
        nextHeartbeat = now() + config.lease_seconds / 3;
      }
      const pending = store.tx(() => {
        if (store.has_pending(credentials, recipeId)) return true;
        store.release(waitingSlot);
        return false;
      });
      if (!pending) return null;

      const slot = store.reserve(
        'worker',
        owner,
        config.max_executions,
        config.lease_seconds,
        true,
      );
      if (slot) return slot;
      await sleep(250);
    }
  } finally {
    store.release(waitingSlot);
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
  try {
    const { positionals, values } = parseArgs({
      options: {
        'wait-for-slot': { type: 'boolean' },
        'recipe-id': { type: 'string' },
      },
      allowPositionals: true,
    });
    const [data_dir, owner, slot, executionId] = positionals;
    const waiting = values['wait-for-slot'];
    const recipeId = values['recipe-id'];
    if (
      !data_dir ||
      positionals.length > 4 ||
      Boolean(owner) !== Boolean(slot) ||
      (waiting && !slot)
    )
      throw new Error('worker expects a data directory and optional lease');
    const reader = new RSIClient({ data_dir, auto_start: false });
    try {
      const pending = reader.store.pending_execution(undefined, recipeId);
      const requested = executionId ? reader.execution_get(executionId) : undefined;
      const config = requested
        ? requested.recovery_config ?? requested.runtime_config
        : pending && LocalConfigSchema.parse(JSON.parse(pending.config));
      if (config) {
        const client = new RSIClient({ data_dir, config, auto_start: false });
        try {
          const workerSlot = waiting
            ? await waitForWorkerSlot(client, owner, slot, recipeId)
            : slot;
          if (workerSlot !== null)
            await drain(client, undefined, owner, workerSlot, recipeId);
        } finally {
          client.close();
        }
      }
    } finally {
      reader.close();
    }
  } catch {
    // The host can recover a job after lease expiry. Avoid exposing model data in process logs.
    process.exitCode = 1;
  }
}
