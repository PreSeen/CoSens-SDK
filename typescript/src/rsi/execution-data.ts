import type { Analysis, Execution, ExecutionResult } from './models.js';
import { stageProgress } from './progress.js';

type LegacyExecution = Omit<
  Execution,
  'result' | 'analysis' | 'progress_percent'
> & {
  parent_execution_id?: string | null;
  progress_percent?: number;
  fixed_rubrics_version_id?: string;
  source_session_id?: string | null;
  resolved_rubrics_version_id?: string | null;
  answer_children?: Record<string, string>;
  hypothesis?: string | null;
  analysis?: Analysis;
  result:
    | (Omit<ExecutionResult, 'published'> & {
        recipe_id?: string;
        recipe_version_id?: string;
        rubrics_version_id?: string;
        activated?: boolean;
        published?: boolean;
      })
    | null;
};

/** Rewrite a stored row once when opening a database with obsolete fields. */
export function upgradeExecution(row: LegacyExecution): Execution {
  const {
    parent_execution_id,
    resolved_rubrics_version_id,
    rubrics_version_id,
    answer_children,
    hypothesis: _hypothesis,
    fixed_rubrics_version_id: _fixedRubrics,
    source_session_id: _session,
    policy,
    analysis,
    result,
    ...execution
  } = row;
  const { auto_activate: _autoActivate, ...executionPolicy } =
    policy as Execution['policy'] & { auto_activate?: boolean };
  if (answer_children)
    for (const [key, id] of Object.entries(answer_children)) {
      const answer = execution.answers[key];
      if (answer) answer.continuation_execution_id = id;
    }
  return {
    ...execution,
    base_execution_id:
      execution.base_execution_id ?? parent_execution_id ?? null,
    policy: executionPolicy,
    progress_percent:
      row.status === 'completed'
        ? 100
        : (row.progress_percent ??
          stageProgress(row.error?.stage ?? row.stage, 1, 1)),
    recipe_id: execution.recipe_id ?? result?.recipe_id ?? null,
    recipe_version_id:
      execution.recipe_version_id ?? result?.recipe_version_id ?? null,
    rubrics_version_id:
      resolved_rubrics_version_id ??
      result?.rubrics_version_id ??
      (Object.hasOwn(row, 'base_execution_id') ? rubrics_version_id : null),
    analysis: analysis ? compactAnalysis(analysis) : undefined,
    result: result
      ? {
          test_status: result.test_status,
          provisional: result.provisional,
          published: result.published ?? result.activated ?? false,
        }
      : null,
  };
}

export function compactAnalysis({
  questions: _questions,
  rubrics: _rubrics,
  ...analysis
}: Analysis) {
  return analysis;
}
