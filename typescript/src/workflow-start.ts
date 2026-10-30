/** Explicit host-coordinated start for a task-workflow phase. No Agent runs here. */
import { createHash } from 'node:crypto';
import type { Atoms, WorkflowArtifactRef } from './atoms.js';
import { RSIApprovalUnavailable, RSIRecoveryRequired } from './errors.js';
import { requestKey } from './http.js';

export type WorkflowPhase = 'prepare' | 'analyze' | 'attribute' | 'revise' | 'recipe' | 'verify';
export interface WorkflowSnapshot {
  task?: { source: 'new_task' | 'external_artifact'; objective?: string; artifacts?: WorkflowArtifactRef[]; self_contained?: boolean };
  task_id?: string; revision_id?: string; attribution_id?: string; recipe_candidate_id?: string;
  feedback?: string; include_memory?: boolean;
}
export interface WorkflowStartOptions {
  phase?: WorkflowPhase; mode?: 'auto' | 'approval' | 'disable'; requestKey?: string;
  authorize?: (phase: WorkflowPhase, snapshot: WorkflowSnapshot) => boolean | Promise<boolean>;
}

function derivedKey(key: string, operation: string) {
  return createHash('sha256').update(`workflow-start:${operation}:${requestKey(key)}`).digest('hex');
}

export async function startWorkflow(atoms: Atoms, snapshot: WorkflowSnapshot, { phase = 'prepare', mode = 'auto', requestKey: key, authorize }: WorkflowStartOptions = {}): Promise<any> {
  if (!['auto', 'approval', 'disable'].includes(mode)) throw new TypeError('mode must be auto, approval or disable');
  if (mode === 'disable') return { status: 'disabled', phase };
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) throw new TypeError('session context must be an authorized snapshot object');
  if (mode === 'approval') {
    if (!authorize) throw new RSIApprovalUnavailable('approval mode requires a host authorization callback');
    if (await authorize(phase, snapshot) !== true) throw new RSIApprovalUnavailable('host authorization was not granted');
  }
  const stable = requestKey(key!);
  if (phase === 'prepare') {
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
    const execution = await atoms.startExecution(taskId!, { key: derivedKey(stable, 'execution') });
    return { phase, status: execution?.status, task_id: taskId, task,
      execution_id: execution?.execution_id, request_sha256: execution?.request_sha256,
      execution_request: execution?.request, execution };
  }
  if (phase === 'analyze') return atoms.startExternalAnalysis(snapshot.task_id!, { key: stable });
  if (phase === 'attribute') return atoms.startAttribution(snapshot.revision_id!, { feedback: snapshot.feedback, key: stable });
  if (phase === 'revise') return atoms.startRevision(snapshot.revision_id!, { attributionId: snapshot.attribution_id!, key: stable });
  if (phase === 'recipe') return atoms.startRecipeCandidate(snapshot.task_id!, { revisionId: snapshot.revision_id!, includeMemory: snapshot.include_memory, key: stable });
  if (phase === 'verify') return atoms.startVerification(snapshot.recipe_candidate_id!, { key: stable });
  throw new RSIRecoveryRequired('phase must be prepare, analyze, attribute, revise, recipe or verify');
}
