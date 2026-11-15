/** Explicit host-coordinated start for a task-workflow phase. No Agent runs here. */
import { createHash } from 'node:crypto';
import { executionInput, modelData, taskObjective, type Atoms, type StartExecutionOptions, type WorkflowArtifactRef } from './atoms.js';
import { RSIApprovalUnavailable, RSIRecoveryRequired } from './errors.js';
import { pathPart, stableKey } from './http.js';

/** `prepare` a task execution, `correct` a completed execution, or `learn` from one. */
export type WorkflowPhase = 'prepare' | 'correct' | 'learn';
export interface WorkflowSnapshot {
  /** The execution's source and input; its objective names the task when the prepare creates one. */
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

/** startExecution's options for a task spec, checked before any task is created so a bad spec leaves none. */
export function executionOptions(task: WorkflowSnapshot['task'], { createsTask, recipeId, model, recallMemory = false }:
  { createsTask: boolean; recipeId?: string; model?: string; recallMemory?: boolean }): Omit<StartExecutionOptions, 'key'> {
  if (!task || typeof task !== 'object' || Array.isArray(task)) throw new TypeError("prepare requires task: the execution's source, objective and artifacts");
  if (task.source === undefined) throw new TypeError('task.source is required');
  if (createsTask) taskObjective(task.objective);
  executionInput(task.source, task.objective, task.artifacts, task.self_contained);
  if (recipeId !== undefined) pathPart(recipeId);
  return { source: task.source, objective: task.objective, artifacts: task.artifacts, selfContained: task.self_contained, ...modelData(model),
    ...(recipeId === undefined ? {} : { recipeId }), ...(recallMemory ? { recallMemory: true } : {}) };
}

/** The task a Recipe belongs to; its executions run there and nowhere else. */
async function recipeTask(atoms: Atoms, recipeId: string, taskId: string | undefined): Promise<string> {
  const owner = (await atoms.getRecipe(recipeId))?.task_id;
  if (!owner) throw new TypeError(`Recipe ${recipeId} belongs to no task`);
  if (taskId !== undefined && taskId !== owner) throw new TypeError(`Recipe ${recipeId} belongs to task ${owner}, not ${taskId}`);
  return owner;
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
  // Arguments are checked before the host is asked, so a refusal never follows an approval.
  let options: Omit<StartExecutionOptions, 'key'> | undefined;
  if (phase === 'prepare') {
    stableKey(key!);
    options = executionOptions(snapshot.task, { createsTask: snapshot.task_id === undefined && recipeId === undefined, recipeId, model, recallMemory });
    if (recipeId === undefined && snapshot.task_id !== undefined) pathPart(snapshot.task_id);
    // The host approves the task the Recipe's execution actually runs in.
    if (recipeId !== undefined) snapshot = { ...snapshot, task_id: await recipeTask(atoms, recipeId, snapshot.task_id) };
  } else if (phase === 'correct') {
    // A correction of a completed execution takes only the execution it corrects and a model.
    if (recipeId !== undefined || recallMemory) throw new TypeError('a correction takes only previousExecutionId, taskId and model');
    stableKey(key!);
    pathPart(snapshot.execution_id!);
    if (snapshot.task_id !== undefined) pathPart(snapshot.task_id);
    modelData(model);
  }
  if (mode === 'approval') {
    if (!authorize) throw new RSIApprovalUnavailable('approval mode requires a host authorization callback');
    if (await authorize(phase, snapshot) !== true) throw new RSIApprovalUnavailable('host authorization was not granted');
  }
  if (phase === 'prepare') {
    let task: any;
    let taskId = snapshot.task_id;
    if (taskId === undefined) {
      task = await atoms.createTask({ objective: snapshot.task!.objective!, key: derivedKey(key!, 'task') });
      taskId = task?.task_id;
      if (!taskId) throw new RSIRecoveryRequired('Task receipt has no task_id');
    } else task = await atoms.getTask(taskId);
    const execution = await atoms.startExecution(taskId, { key: derivedKey(key!, 'execution'), ...options! } as StartExecutionOptions);
    return { phase, status: execution?.status, task_id: taskId, task,
      execution_id: execution?.execution_id, request_sha256: execution?.request_sha256,
      execution_request: execution?.request, execution };
  }
  if (phase === 'correct') return atoms.startExecution(snapshot.task_id, { key: stableKey(key!), previousExecutionId: snapshot.execution_id!, ...(model === undefined ? {} : { model }) });
  if (phase === 'learn') return atoms.learn(snapshot.execution_id!, { model });
  throw new TypeError('phase must be prepare, correct or learn');
}
