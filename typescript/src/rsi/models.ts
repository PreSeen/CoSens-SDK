/** Embedded RSI contracts. Secrets and model instances are never persisted. */
import { z } from 'zod';

export class RSIError extends Error {
  constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'RSIError';
  }
}
export const fail = (code: string, message: string): never => {
  throw new RSIError(code, message);
};
const text = z.string().min(1);
export const ModelConfigSchema = z.strictObject({
  model: text,
  base_url: z
    .string()
    .refine((value) => {
      try {
        const u = new URL(value);
        return (
          ['http:', 'https:'].includes(u.protocol) &&
          !u.username &&
          !u.password &&
          !u.search &&
          !u.hash
        );
      } catch {
        return false;
      }
    }, 'base_url must be an HTTP(S) endpoint without credentials, query or fragment')
    .optional(),
  api_key_env: z
    .string()
    .regex(/^[A-Za-z_][A-Za-z0-9_]*$/)
    .default('RSI_MODEL_API_KEY'),
  timeout: z.number().positive().max(3600).default(120),
  max_tokens: z.number().int().positive().default(4096),
});
export const LocalConfigSchema = z.strictObject({
  model: ModelConfigSchema,
  max_executions: z.number().int().min(1).max(32).default(2),
  max_model_requests: z.number().int().min(1).max(32).default(2),
  lease_seconds: z.number().min(3).max(3600).default(30),
  max_file_bytes: z
    .number()
    .int()
    .positive()
    .default(4 * 1024 * 1024),
  max_bundle_bytes: z
    .number()
    .int()
    .positive()
    .default(32 * 1024 * 1024),
  recursion_limit: z.number().int().min(4).max(500).default(40),
});
export type LocalConfig = z.output<typeof LocalConfigSchema>;
export type LocalConfigInput = z.input<typeof LocalConfigSchema>;
export const ManifestSchema = z.strictObject({
  context_path: text,
  files: z
    .array(
      z.strictObject({
        path: text,
        role: z
          .enum(['input', 'artifact', 'reference', 'feedback'])
          .default('input'),
        description: z.string().default(''),
      }),
    )
    .max(1000)
    .default([]),
  source_session_id: z.string().nullable().optional(),
  source_task_id: z.string().nullable().optional(),
  capture_boundary: z.string().nullable().optional(),
  feedback_context: z.record(z.string(), z.unknown()).nullable().optional(),
});
export type MaterialManifest = z.input<typeof ManifestSchema>;
export const EvidenceSchema = z
  .strictObject({
    material_id: z.string().regex(/^material_[0-9a-f]{32}$/),
    locator: text,
  })
  .refine(
    (e) => e.locator.startsWith(e.material_id + '/'),
    'locator must belong to material_id',
  );
export const FeedbackExtractionSchema = z.strictObject({
  items: z.array(
    z.strictObject({
      kind: z.enum(['requirement', 'correction']),
      rule: text,
      evidence_refs: z.array(EvidenceSchema).min(1),
    }),
  ),
});
export const RubricsSchema = z
  .strictObject({
    criteria: z
      .array(
        z.strictObject({
          id: text,
          description: text,
          source: z.enum([
            'task',
            'user_feedback',
            'trajectory',
            'agent_inference',
          ]),
          evidence: z.array(EvidenceSchema).default([]),
          required: z.boolean().default(true),
        }),
      )
      .min(1),
    pass_threshold: z.number().min(0).max(1).default(0.8),
  })
  .refine(
    (r) => new Set(r.criteria.map((c) => c.id)).size === r.criteria.length,
    'criterion IDs must be unique',
  );
export type RubricsContent = z.input<typeof RubricsSchema>;
export const QuestionSchema = z
  .strictObject({
    id: text,
    revision: z.number().int().positive().default(1),
    question: text,
    options: z
      .array(z.strictObject({ id: text, label: text }))
      .min(2)
      .max(3),
    assumption: text,
  })
  .refine(
    (q) => new Set(q.options.map((o) => o.id)).size === q.options.length,
    'option IDs must be unique',
  );
export type Question = z.output<typeof QuestionSchema>;
const AttributionSchema = z.strictObject({
  possible_causes: z
    .array(
      z.strictObject({
        observed_issue: text,
        reason: text,
        uncertainty: text,
        evidence_refs: z.array(EvidenceSchema).min(1),
      }),
    )
    .min(1),
});
const recipeName = z
  .string()
  .trim()
  .min(1)
  .max(120)
  .regex(/^[^\r\n]+$/u);
export const AnalysisSchema = z
  .strictObject({
    recipe_name: recipeName.optional(),
    sufficient_information: z.boolean(),
    hypothesis: z.string().nullable().default(null),
    questions: z.array(QuestionSchema).default([]),
    rubrics: RubricsSchema,
    attribution: AttributionSchema.nullable().default(null),
  })
  .refine(
    (a) =>
      a.sufficient_information
        ? !a.hypothesis && a.questions.length === 0
        : !!a.hypothesis && a.questions.length > 0,
    'only missing necessary information requires a hypothesis and questions',
  )
  .refine(
    (a) => new Set(a.questions.map((q) => q.id)).size === a.questions.length,
    'question IDs must be unique',
  );
export const NamedAnalysisSchema = AnalysisSchema.safeExtend({
  recipe_name: recipeName,
});
export type Analysis = z.output<typeof AnalysisSchema> & {
  feedback?: z.output<typeof FeedbackExtractionSchema>['items'];
};
export const RecipeSchema = z.strictObject({
  title: text,
  applicability: text,
  instructions: text,
});
export type RecipeContent = z.output<typeof RecipeSchema>;
export const CheckSchema = z
  .strictObject({
    kind: z.enum(['exists', 'text_equals', 'text_contains', 'sha256']),
    path: text.refine(
      (p) =>
        !p.startsWith('/') &&
        !p.split('/').includes('..') &&
        !/[\\:]/.test(p) &&
        p !== '.' &&
        !p.endsWith('/'),
      'check path must be a relative workspace file',
    ),
    value: z.string().nullable().optional(),
  })
  .refine(
    (c) => c.kind === 'exists' || c.value != null,
    'check requires a value',
  );
export const PolicySchema = z.strictObject({
  artifact_confirmed: z.literal(true),
  confirmation_ref: text,
  max_rounds: z.number().int().min(1).max(10).default(2),
  auto_activate: z.boolean().default(false),
  test_task: z
    .strictObject({
      instruction: text,
      input_locators: z.array(text).nullable().optional(),
      shared_reference_locators: z.array(text).nullable().optional(),
      checks: z.array(CheckSchema).default([]),
    })
    .nullable()
    .optional(),
});
export type ExecutionPolicy = z.input<typeof PolicySchema>;
export const AssessmentSchema = z.strictObject({
  verifiable: z.boolean(),
  scores: z.array(
    z
      .strictObject({
        criterion_id: text,
        status: z.enum(['verified', 'unverified']).default('verified'),
        score: z.number().min(0).max(1).nullable(),
        evidence: text,
      })
      .refine(
        ({ status, score }) => (status === 'unverified') === (score === null),
        'unverified criteria require null scores; verified criteria require numeric scores',
      ),
  ),
  reason: z.string(),
});
export const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
export type Kind = 'material' | 'execution' | 'rubrics' | 'recipe';
export interface ObjectMetadata {
  id: string;
  revision: number;
  created_at: number;
  updated_at: number;
  archived_at: number | null;
  name?: string;
  description?: string;
}
export interface EvidenceFile {
  path: string;
  snapshot: string;
  locator: string;
  role: string;
  description: string;
  sha256: string;
  size: number;
}
export interface Material extends ObjectMetadata {
  manifest: z.output<typeof ManifestSchema>;
  files: EvidenceFile[];
  status: 'ready';
}
export interface Version<C = unknown> {
  id: string;
  object_id: string;
  number: number;
  content: C;
  provenance: Record<string, unknown>;
  created_at: number;
}
export interface Versioned<C = unknown> extends ObjectMetadata {
  latest_version_id: string | null;
  active_version_id?: string | null;
  selected_version?: Version<C> | null;
}
export interface TestResult {
  id: string;
  execution_id: string;
  recipe_id: string;
  version_id: string;
  rubrics_version_id: string;
  input_revision: number;
  round: number;
  status: 'passed' | 'failed' | 'unverifiable';
  score: number | null;
  scores: z.input<typeof AssessmentSchema>['scores'];
  checks: Array<z.output<typeof CheckSchema> & { passed: boolean }>;
  artifacts: EvidenceFile[];
  reason?: string;
  workspace?: string;
  trace_path?: string;
  evaluator_trace_path?: string;
}
export interface ExecutionResult {
  recipe_id: string;
  recipe_version_id: string;
  rubrics_version_id: string;
  test_status: TestResult['status'];
  provisional: boolean;
  activated: boolean;
  activation_conflict?: boolean;
}
export interface Execution extends ObjectMetadata {
  runtime_config: LocalConfig;
  material_ids: string[];
  policy: z.output<typeof PolicySchema>;
  source_session_id?: string | null;
  parent_execution_id?: string | null;
  rubrics_version_id?: string | null;
  base_recipe_version_id?: string | null;
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';
  stage: string;
  input_revision: number;
  questions: Question[];
  answers: Record<string, unknown>;
  answer_children?: Record<string, string>;
  recipe_id: string | null;
  recipe_version_id: string | null;
  result: ExecutionResult | null;
  test_results: TestResult[];
  error: {
    code: string;
    type: string;
    stage: string;
    upstream_status?: number;
  } | null;
  hypothesis?: string | null;
  analysis?: Analysis;
  analyzed_input_revision?: number;
  resolved_rubrics_version_id?: string;
  test_request?: {
    recipe_id: string;
    version_id: string;
    rubrics_version_id: string;
  };
}
export interface Pagination {
  archived?: boolean;
  limit?: number;
  offset?: number;
}
export interface MetadataUpdate {
  name?: string;
  description?: string;
  archived?: boolean;
  expected_revision?: number;
}
export interface ExecutionCreate {
  material_ids: string[];
  policy: ExecutionPolicy;
  rubrics_version_id?: string | null;
  base_recipe_version_id?: string | null;
  parent_execution_id?: string | null;
  source_session_id?: string | null;
  idempotency_key?: string;
}
export interface ClarificationAnswer {
  question_id: string;
  question_revision: number;
  selected_option_ids: string[];
  text?: string | null;
  idempotency_key?: string;
}
export interface Runtime {
  extract_feedback?(
    files: EvidenceFile[],
    trace_path: string,
  ): Promise<unknown>;
  analyze(
    context: Record<string, unknown>,
    files: EvidenceFile[],
    trace_path: string,
  ): Promise<unknown>;
  generate(
    context: Record<string, unknown>,
    files: EvidenceFile[],
    trace_path: string,
  ): Promise<unknown>;
  execute(
    context: Record<string, unknown>,
    files: EvidenceFile[],
    workspace: string,
    trace_path: string,
  ): Promise<unknown>;
  assess(
    context: Record<string, unknown>,
    files: EvidenceFile[],
    trace_path: string,
  ): Promise<unknown>;
}
