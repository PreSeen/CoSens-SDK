import type { Execution } from './models.js';

const firstRound: Record<string, number> = {
  analyze: 10,
  generate: 25,
  test: 40,
  evaluate: 55,
};

const retryOffsets: Record<string, number> = {
  generate: 10,
  test: 20,
  evaluate: 35,
};

export function stageProgress(
  stage: string,
  round: number,
  maxRounds: number,
): number {
  const offset = retryOffsets[stage];
  if (round < 2 || maxRounds < 2 || offset === undefined)
    return firstRound[stage] ?? 0;
  return 55 + Math.round(((round - 2) * 35 + offset) / (maxRounds - 1));
}

export function restartExecution(execution: Execution) {
  execution.input_revision++;
  execution.rubrics_version_id = null;
  execution.stage = execution.status === 'running' ? 'analyze' : 'queued';
  execution.progress_percent = stageProgress(execution.stage, 1, 1);
}
