/** Explicit host-coordinated start for a task-workflow phase. No Agent runs here. */
import { createHash } from 'node:crypto';
import type { Atoms, StartExecutionOptions, WorkflowArtifactRef } from './atoms.js';
import { RSIApprovalUnavailable, RSIRecoveryRequired } from './errors.js';
import { stableKey } from './http.js';

/** `prepare` a task execution, `correct` a completed execution, or `learn` from one. */
export type WorkflowPhase = 'prepare' | 'correct' | 'learn';
export interface WorkflowSnapshot {
  task?: { source: 'new_task' | 'external_artifact'; objective?: string; artifacts?: WorkflowArtifactRef[]; self_contained?: boolean };
  task_id?: string; execution_id?: string;
  feedback?: string;
}
export interface WorkflowStartOptions {
  phase?: WorkflowPhase; mode?: 'auto' | 'approval' | 'disable'; requestKey?: string;
  recipeId?: string; model?: string; recallMemory?: boolean;
  authorize?: (phase: WorkflowPhase, snapshot: WorkflowSnapshot) => boolean | Promise<boolean>;
}

function derivedKey(key: string, operation: string) {
  return createHash('sha256').update(`workflow-start:${operation}:${stableKey(key)}`).digest('hex');
}

export async function startWorkflow(atoms: Atoms, snapshot: WorkflowSnapshot | string, { phase = 'prepare', mode = 'auto', requestKey: key, authorize, recipeId, model, recallMemory = false }: WorkflowStartOptions = {}): Promise<any> {
  if (!['auto', 'approval', 'disable'].includes(mode)) throw new TypeError('mode must be auto, approval or disable');
  if (mode === 'disable') return { status: 'disabled', phase };
  if (typeof recallMemory !== 'boolean') throw new TypeError('recallMemory must be boolean');
  if (typeof snapshot === 'string' && phase === 'prepare') {
    if (!snapshot.trim()) throw new TypeError('task Prompt must be nonempty');
    snapshot = { task: { source: 'new_task', objective: snapshot } };
  }
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) throw new TypeError('session context must be an authorized snapshot object');
  if (mode === 'approval') {
    if (!authorize) throw new RSIApprovalUnavailable('approval mode requires a host authorization callback');
    if (await authorize(phase, snapshot) !== true) throw new RSIApprovalUnavailable('host authorization was not granted');
  }
  if (phase === 'prepare') {
    const stable = stableKey(key!);
    let task: any;
    let taskId = snapshot.task_id;
    if (taskId) task = await atoms.getTask(taskId);
    else {
      if (!snapshot.task) throw new TypeError('prepare requires task or task_id');
      task = await atoms.createTask({ source: snapshot.task.source, objective: snapshot.task.objective,
        artifacts: snapshot.task.artifacts, selfContained: snapshot.task.self_contained, key: derivedKey(stable, 'task') });
      taskId = task?.task_id;
    }
    if (task?.status === 'ready' && !taskId) throw new RSIRecoveryRequired('Ready task receipt has no task_id');
    if (task?.status !== 'ready') return { phase, task, status: task?.status };
    const execution = await atoms.startExecution(taskId!, { key: derivedKey(stable, 'execution'), recipeId, model, recallMemory });
    return { phase, status: execution?.status, task_id: taskId, task,
      execution_id: execution?.execution_id, request_sha256: execution?.request_sha256,
      execution_request: execution?.request, execution };
  }
  if (phase === 'correct') {
    // Forward recipeId and recallMemory as given, typed or not, so the atom rejects them before sending.
    const options = { key: stableKey(key!), previousExecutionId: snapshot.execution_id!, recipeId, model, recallMemory } as StartExecutionOptions;
    return atoms.startExecution(snapshot.task_id!, options);
  }
  if (phase === 'learn') return atoms.learn(snapshot.execution_id!, { model });
  throw new TypeError('phase must be prepare, correct or learn');
}
