import { compactAnalysis } from './execution-data.js';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';
import Database from 'better-sqlite3';
import { Annotation, END, START, StateGraph } from '@langchain/langgraph';
import { SqliteSaver } from '@langchain/langgraph-checkpoint-sqlite';
import { traceable } from 'langsmith/traceable';
import type { RSIClient } from './client.js';
import {
  atomic_json,
  authorizedTestFiles,
  digest,
  inside,
  read_snapshot,
  workspace_files,
} from './materials.js';
import { LearningLiterals, rubricRuleText } from './portability.js';
import { uid } from './store.js';
import { stageProgress } from './progress.js';
import type { PythonRun } from './test-python.js';
import {
  AnalysisSchema,
  FeedbackExtractionSchema,
  AssessmentSchema,
  RecipeSchema,
  RubricsSchema,
  RSIError,
  fail,
  type Analysis,
  type EvidenceFile,
  type Execution,
  type ExecutionResult,
  type RecipeContent,
  type Runtime,
  type TestResult,
  type Recipe,
} from './models.js';

interface Candidate {
  recipe_id: string;
  version_id: string;
  round: number;
}

const State = Annotation.Root({
  revision: Annotation<number>(),
  analysis: Annotation<Analysis | null>(),
  rubric: Annotation<string>(),
  candidate: Annotation<Candidate | null>(),
  test: Annotation<TestResult | null>(),
});
type State = typeof State.State;

export class Engine {
  constructor(
    private client: RSIClient,
    private runtime: Runtime,
    private owner: string,
    private fence: number,
    private id: string,
  ) {}

  private get store() {
    return this.client.store;
  }
  private guard(revision?: number) {
    return this.store.guard(this.id, this.owner, this.fence, revision);
  }

  private inStage(revision: number, stage: string, round = 1): Execution {
    return this.store.tx(() => {
      const execution = this.guard(revision);
      return this.store.save({
        ...execution,
        stage,
        progress_percent: Math.max(
          execution.progress_percent,
          stageProgress(stage, round, execution.policy.max_rounds),
        ),
      });
    });
  }

  private evidence(execution: Execution, forTest = false): EvidenceFile[] {
    const files = execution.material_ids.flatMap(
      (id) => this.client.material_get(id).files,
    );
    const task = execution.policy.test_task;
    if (forTest) return authorizedTestFiles(files, task);
    if (task?.replay_current_case) return files;
    const heldOut = new Set(task?.input_locators);
    return files.filter((file) => !heldOut.has(file.locator));
  }

  private learningLiterals(
    execution: Execution,
    ...sourceExecutionIds: unknown[]
  ) {
    const sources = new Map([[execution.id, execution]]);
    for (const id of sourceExecutionIds) {
      if (typeof id === 'string' && !sources.has(id))
        sources.set(id, this.store.get<Execution>(id, 'execution'));
    }
    const files = new Map<string, EvidenceFile>();
    for (const source of sources.values()) {
      const authorized = new Set(
        this.evidence(source, true).map((f) => f.locator),
      );
      const replaysCurrentCase = source.policy.test_task?.replay_current_case;
      for (const file of this.evidence(source))
        if (replaysCurrentCase || !authorized.has(file.locator))
          files.set(file.locator, file);
    }
    const allowedPaths = [
      ...this.evidence(execution, true).map((file) => file.path),
      ...(execution.policy.test_task?.checks ?? []).map((check) => check.path),
    ];
    return new LearningLiterals([...files.values()], allowedPaths);
  }

  private directory(revision: number) {
    return join(
      this.client.data_dir,
      'executions',
      this.id,
      `input-${revision}-lease-${this.fence}`,
    );
  }

  private context(execution: Execution) {
    const { test_task: _testTask, ...policy } = execution.policy;
    return {
      answers: Object.fromEntries(
        Object.entries(execution.answers).map(
          ([key, { continuation_execution_id: _child, ...answer }]) => [
            key,
            answer,
          ],
        ),
      ),
      policy,
    };
  }

  private async analyze(state: State) {
    const execution = this.inStage(state.revision, 'analyze');
    if (execution.analyzed_input_revision === state.revision) {
      return {
        analysis: {
          ...execution.analysis!,
          questions: execution.questions,
          rubrics: RubricsSchema.parse(
            this.store.version_by_id(execution.rubrics_version_id!, 'rubrics')
              .content,
          ),
        },
        rubric: execution.rubrics_version_id!,
      };
    }
    const fixed = execution.test_request?.rubrics_version_id;
    const fixedRubrics = fixed
      ? RubricsSchema.parse(this.store.version_by_id(fixed, 'rubrics').content)
      : undefined;
    const files = this.evidence(execution);
    const literals = this.learningLiterals(execution);
    let feedback: Analysis['feedback'];
    if (!fixed && this.runtime.extract_feedback) {
      const sources = files.filter(
        ({ role }) => role === 'context' || role === 'feedback',
      );
      feedback = sources.length
        ? FeedbackExtractionSchema.parse(
            await this.runtime.extract_feedback(
              sources,
              join(this.directory(state.revision), 'feedback-extraction.json'),
            ),
          ).items
        : [];
      const sourceLocators = new Set(sources.map(({ locator }) => locator));
      if (
        feedback.some(({ evidence_refs }) =>
          evidence_refs.some(({ locator }) => !sourceLocators.has(locator)),
        ) ||
        literals.foundIn(feedback.map(({ rule }) => rule))
      )
        fail(
          'invalid_model_output',
          'Extracted feedback contains learning-only details or unauthorized evidence references',
        );
    }
    const feedbackLocators = new Set(
      feedback?.flatMap(({ evidence_refs }) =>
        evidence_refs.map(({ locator }) => locator),
      ),
    );
    const recipeName = this.store.executionRecipeName(execution);
    const context: Record<string, unknown> = {
      ...this.context(execution),
      recipe_name: recipeName,
      fixed_rubrics: fixedRubrics,
      user_feedback: feedback,
    };
    let analysis: Analysis;
    for (let attempt = 0; ; attempt++) {
      analysis = AnalysisSchema.parse(
        await this.runtime.analyze(
          context,
          files,
          join(this.directory(state.revision), 'analysis.json'),
        ),
      );
      const ungroundedFeedback =
        feedback !== undefined &&
        analysis.rubrics.criteria.some(
          ({ source, evidence }) =>
            source === 'user_feedback' &&
            !evidence.some(({ locator }) => feedbackLocators.has(locator)),
        );
      if (
        (recipeName ||
          !analysis.recipe_name ||
          !literals.foundIn([analysis.recipe_name])) &&
        (fixedRubrics ||
          (!literals.foundIn(rubricRuleText(analysis.rubrics)) &&
            !ungroundedFeedback))
      )
        break;
      if (attempt === 2)
        fail(
          'invalid_model_output',
          'Recipe name or Rubric rules contain learning-only details or unsupported feedback citations',
        );
      context.portability_feedback =
        'Rewrite the Rubric as reusable rules for any future input. Express the requirements without copying details or references specific to past executions. Refer to input roles instead of naming learning source files. Use an evidence reference from an extracted rule for every user_feedback criterion. If asked to propose a Recipe name, make it reusable and omit learning-only details.';
    }
    const locators = new Set(files.map((file) => file.locator));
    for (const cause of analysis.attribution?.possible_causes ?? []) {
      if (cause.evidence_refs.some((ref) => !locators.has(ref.locator))) {
        fail(
          'invalid_model_output',
          'Attribution contains unauthorized evidence references',
        );
      }
    }
    if (fixedRubrics) analysis.rubrics = fixedRubrics;
    else {
      for (const criterion of analysis.rubrics.criteria) {
        if (
          (criterion.source !== 'agent_inference' &&
            !criterion.evidence.length) ||
          criterion.evidence.some((ref) => !locators.has(ref.locator))
        ) {
          fail(
            'invalid_model_output',
            'Rubrics contain missing or unauthorized evidence references',
          );
        }
      }
    }
    if (recipeName) delete analysis.recipe_name;
    if (feedback !== undefined) analysis.feedback = feedback;
    for (const question of analysis.questions)
      question.revision = state.revision;
    return this.store.tx(() => {
      const current = this.guard(state.revision);
      let rubric = fixed;
      if (!rubric) {
        const object = this.store.insert('rubrics', {
          name: 'Rubrics for ' + this.id,
          description: '',
          latest_version_id: null,
        });
        rubric = this.store.append(object, analysis.rubrics, undefined, {
          execution_id: this.id,
          input_revision: state.revision,
        }).id;
      }
      this.store.save({
        ...current,
        name: this.store.executionRecipeName(current) ?? analysis.recipe_name,
        questions: analysis.questions,
        analysis: compactAnalysis(analysis),
        analyzed_input_revision: state.revision,
        rubrics_version_id: rubric,
      });
      return { analysis, rubric };
    });
  }

  private async generate(state: State): Promise<{ candidate: Candidate }> {
    const round = (state.candidate?.round ?? 0) + 1;
    const execution = this.inStage(state.revision, 'generate', round);
    if (execution.test_request) {
      const request = execution.test_request;
      return {
        candidate: {
          recipe_id: request.recipe_id,
          version_id: request.version_id,
          round,
        },
      };
    }
    const key = `recipe:${this.id}:${state.revision}:${round}`;
    const cached = this.store.operation<RecipeContent>(key);
    if (cached) {
      return {
        candidate: {
          recipe_id: cached.object_id,
          version_id: cached.id,
          round,
        },
      };
    }
    const baseId =
      state.candidate?.version_id ?? execution.base_recipe_version_id;
    const base = baseId
      ? this.store.version_by_id<RecipeContent>(baseId, 'recipe')
      : undefined;
    const files = this.evidence(execution);
    const recipeFile = files.find((file) => file.role === 'recipe');
    const literals = this.learningLiterals(
      execution,
      base?.provenance.execution_id,
    );
    const context: Record<string, unknown> = {
      ...this.context(execution),
      recipe_name: this.store.executionRecipeName(execution),
      analysis: state.analysis,
      previous_test: state.test,
      base_recipe:
        base?.content ??
        (recipeFile ? read_snapshot(recipeFile).text : undefined),
    };
    let content: RecipeContent;
    for (let attempt = 0; ; attempt++) {
      content = RecipeSchema.parse(
        await this.runtime.generate(
          context,
          files,
          join(this.directory(state.revision), `generate-${round}.json`),
        ),
      );
      if (typeof context.recipe_name === 'string')
        content.name = context.recipe_name;
      if (!literals.foundIn(Object.values(content))) break;
      if (attempt === 2)
        fail(
          'invalid_model_output',
          'Recipe rules contain learning-only literals',
        );
      context.portability_feedback =
        'Rewrite the Recipe as reusable rules for any future input. Express the procedure without copying details or references specific to past executions. Refer to input roles instead of naming learning source files.';
    }

    return this.store.tx(() => {
      const current = this.guard(state.revision);
      const recipeId =
        state.candidate?.recipe_id ?? current.recipe_id ?? base?.object_id;
      const recipe = recipeId
        ? this.store.get<Recipe>(recipeId, 'recipe')
        : this.store.createRecipe(current.name ?? content.name);
      if (recipe.archived_at)
        fail('invalid_state', 'recipe was archived during learning');
      const name = recipe.name ?? current.name ?? content.name;
      const version = this.store.append(
        { ...recipe, name },
        { ...content, name },
        key,
        {
          execution_id: this.id,
          input_revision: state.revision,
        },
      );
      this.store.save({
        ...current,
        name,
        recipe_id: recipe.id,
        recipe_version_id: version.id,
      });
      return {
        candidate: {
          recipe_id: recipe.id,
          version_id: version.id,
          round,
        },
      };
    });
  }

  private async test(state: State): Promise<{ test: TestResult }> {
    const { recipe_id, version_id, round } = state.candidate!;
    const execution = this.inStage(state.revision, 'test', round);
    const id = `test:${this.id}:${state.revision}:${round}:${version_id}`;
    const previous = execution.test_results.find((test) => test.id === id);
    if (previous) return { test: previous };
    const result: TestResult = {
      id,
      execution_id: this.id,
      recipe_id,
      version_id,
      rubrics_version_id: state.rubric,
      input_revision: state.revision,
      round,
      status: 'unverifiable',
      score: null,
      scores: [],
      checks: [],
      artifacts: [],
    };
    const task = execution.policy.test_task;
    if (task) await this.runTest(state, execution, result, task);
    else result.reason = 'Host did not supply a reproducible test task';

    this.store.tx(() => {
      const current = this.guard(state.revision);
      this.store.save({
        ...current,
        test_results: [...current.test_results, result],
      });
    });
    return { test: result };
  }

  private async runTest(
    state: State,
    execution: Execution,
    result: TestResult,
    task: NonNullable<Execution['policy']['test_task']>,
  ) {
    const attempt = join(this.directory(state.revision), uid('test-attempt'));
    const workspace = join(attempt, 'outputs');
    mkdirSync(workspace, { recursive: true, mode: 0o700 });
    const recipe = this.store.version<RecipeContent>(
      result.recipe_id,
      result.version_id,
    );
    const rubricVersion = this.store.version_by_id(state.rubric, 'rubrics');
    const rubrics = RubricsSchema.parse(rubricVersion.content);
    const literals = this.learningLiterals(
      execution,
      recipe.provenance.execution_id,
      rubricVersion.provenance.execution_id,
    );
    // Validate existing versions without rewriting the rules being tested.
    if (
      literals.foundIn(Object.values(recipe.content)) ||
      literals.foundIn(rubricRuleText(rubrics))
    )
      fail('invalid_model_output', 'Test rules contain learning-only literals');
    const testRubrics = {
      pass_threshold: rubrics.pass_threshold,
      ...(rubrics.requires_python ? { requires_python: true } : {}),
      criteria: rubrics.criteria.map(({ id, description, required }) => ({
        id,
        description,
        required,
      })),
    };
    const inputs = this.evidence(execution, true);
    const trace = join(attempt, 'executor-trace.json');
    atomic_json(join(attempt, 'test-input.json'), {
      test_task: task,
      recipe,
      rubrics_version_id: state.rubric,
    });
    const summary = await this.runtime.execute(
      {
        recipe: recipe.content,
        test_task: { instruction: task.instruction },
        ...(rubrics.requires_python ? { requires_python: true } : {}),
      },
      inputs,
      workspace,
      trace,
    );
    atomic_json(join(attempt, 'executor-result.json'), summary ?? null);
    this.guard(state.revision);
    result.workspace = workspace;
    result.trace_path = trace;
    result.artifacts = workspace_files(workspace)
      .sort()
      .map((path) =>
        this.file(
          path,
          'test/' + relative(workspace, path).split(sep).join('/'),
          'artifact',
          'Actual Recipe test output',
        ),
      );
    const latestRuns = new Map<string, PythonRun>();
    if (summary && typeof summary === 'object' && 'python_runs' in summary &&
      Array.isArray(summary.python_runs)) {
      for (const run of summary.python_runs as PythonRun[]) latestRuns.set(run.path, run);
    }
    const runs = [...latestRuns.values()];
    const unavailable = runs.find((run) => run.status === 'unavailable');
    const failed = runs.find((run) => run.exit_code !== 0);
    if (unavailable || (rubrics.requires_python && !this.client.config.test_python_image)) {
      result.reason = unavailable?.stderr ?? 'Required Python execution is not configured';
      atomic_json(join(attempt, 'result.json'), result);
      return;
    }
    if (failed || (rubrics.requires_python && !runs.length)) {
      result.status = 'failed';
      result.reason = failed?.status === 'rejected'
        ? failed.stderr
        : failed
          ? `Python script ${failed.path} exited with code ${failed.exit_code}`
          : 'Required Python execution was skipped';
      atomic_json(join(attempt, 'result.json'), result);
      return;
    }
    if (!result.artifacts.length) {
      result.reason = 'Recipe executor produced no verifiable output files';
      return;
    }

    result.checks = task.checks.map((check) => {
      const path = inside(workspace, check.path);
      const bytes = result.artifacts.some((file) => file.path === path)
        ? readFileSync(path)
        : null;
      let passed = false;
      if (bytes) {
        switch (check.kind) {
          case 'exists':
            passed = true;
            break;
          case 'text_equals':
            passed = bytes.toString('utf8') === check.value;
            break;
          case 'text_contains':
            passed = bytes.toString('utf8').includes(check.value!);
            break;
          case 'sha256':
            passed = digest(bytes) === check.value;
            break;
        }
      }
      return { ...check, passed };
    });
    const files = [...result.artifacts, ...inputs];
    if (existsSync(trace))
      files.push(
        this.file(
          trace,
          'trace/executor.json',
          'trace',
          'Actual executor tool calls and results',
        ),
      );
    this.inStage(state.revision, 'evaluate', result.round);
    const judgeTrace = join(attempt, 'evaluator-trace.json');
    const assessment = AssessmentSchema.parse(
      await this.runtime.assess(
        {
          test_task: task,
          rubrics: testRubrics,
          objective_checks: result.checks,
          recipe: recipe.content,
        },
        files,
        judgeTrace,
      ),
    );
    result.evaluator_trace_path = judgeTrace;
    result.reason = assessment.reason;
    result.scores = assessment.scores;
    if (assessment.verifiable) {
      const scores = new Map(
        assessment.scores.map((score) => [score.criterion_id, score.score]),
      );
      if (
        scores.size !== assessment.scores.length ||
        scores.size !== rubrics.criteria.length ||
        rubrics.criteria.some((criterion) => !scores.has(criterion.id))
      ) {
        fail(
          'invalid_model_output',
          'evaluator must report every fixed criterion exactly once',
        );
      }
      const verifiedScores = [...scores.values()].filter(
        (score) => score !== null,
      );
      if (verifiedScores.length) {
        result.score =
          verifiedScores.reduce((sum, score) => sum + score, 0) /
          verifiedScores.length;
      }
      if (!result.checks.every((check) => check.passed))
        result.status = 'failed';
      else if (result.score !== null) {
        const failedRequired = rubrics.criteria.some(({ id, required }) => {
          const score = scores.get(id);
          return required && score != null && score < rubrics.pass_threshold;
        });
        result.status =
          result.score >= rubrics.pass_threshold && !failedRequired
            ? 'passed'
            : 'failed';
      }
    }
    atomic_json(join(attempt, 'result.json'), result);
  }

  private file(
    path: string,
    locator: string,
    role: string,
    description: string,
  ): EvidenceFile {
    const data = readFileSync(path);
    return {
      path,
      snapshot: path,
      locator,
      role,
      description,
      sha256: digest(data),
      size: data.length,
    };
  }

  private route(state: State) {
    const execution = this.guard(state.revision);
    return state.test?.status === 'failed' &&
      state.candidate!.round < execution.policy.max_rounds &&
      !execution.test_request
      ? 'generate'
      : 'commit';
  }

  private commit(state: State) {
    this.store.tx(() => {
      const execution = this.guard(state.revision);
      const candidate = state.candidate!;
      const provisional = execution.questions.some(
        (question) =>
          !(`${question.id}:${question.revision}` in execution.answers),
      );
      const result: ExecutionResult = {
        test_status: state.test!.status,
        provisional,
        published: false,
      };
      if (result.test_status === 'passed' && !provisional)
        result.published = this.store.publishRecipeVersion(
          candidate.recipe_id,
          candidate.version_id,
        );
      this.store.save({
        ...execution,
        recovery_config: undefined,
        status: 'completed',
        stage: 'done',
        progress_percent: 100,
        result,
        rubrics_version_id: state.rubric,
        recipe_id: candidate.recipe_id,
        recipe_version_id: candidate.version_id,
      });
    });
    return {};
  }

  async run(): Promise<void> {
    const directory = join(this.client.data_dir, 'executions', this.id);
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    const checkpointPath = join(directory, `checkpoints-${this.fence}.sqlite3`);
    // A fresh lease copies the preceding checkpoint store so a stale worker cannot overwrite it.
    if (!existsSync(checkpointPath) && this.fence > 1) {
      const previous = join(directory, `checkpoints-${this.fence - 1}.sqlite3`);
      if (existsSync(previous)) {
        const db = new Database(previous, { readonly: true });
        try {
          await db.backup(checkpointPath);
        } finally {
          db.close();
        }
      }
    }
    // Reuse the SDK SQLite driver so checkpoints and the job queue use the same Node ABI.
    const saver = new SqliteSaver(new Database(checkpointPath));
    try {
      const graph = new StateGraph(State)
        .addNode('analyze', (state) => this.analyze(state))
        .addNode('generate', (state) => this.generate(state))
        .addNode('test_recipe', (state) => this.test(state))
        .addNode('commit', (state) => this.commit(state))
        .addEdge(START, 'analyze')
        .addEdge('analyze', 'generate')
        .addEdge('generate', 'test_recipe')
        .addConditionalEdges('test_recipe', (state) => this.route(state), [
          'generate',
          'commit',
        ])
        .addEdge('commit', END)
        .compile({ checkpointer: saver });
      const config = {
        configurable: { thread_id: this.id },
        recursionLimit: 100,
        callbacks: [],
      };
      for (;;) {
        const revision = this.guard().input_revision;
        const saved = await graph.getState(config);
        const state = saved.values as Partial<State>;
        try {
          const input =
            state.revision === revision && saved.next.length
              ? null
              : {
                  revision,
                  analysis: null,
                  rubric: '',
                  candidate: null,
                  test: null,
                };
          await traceable(() => graph.invoke(input, config), {
            tracingEnabled: false,
          })();
          return;
        } catch (error) {
          if (error instanceof RSIError && error.code === 'input_changed')
            continue;
          // If the host supplied revised inputs mid-call, abandon errors from that stale run.
          if (this.guard().input_revision === revision) throw error;
        }
      }
    } finally {
      saver.db.close();
    }
  }
}
