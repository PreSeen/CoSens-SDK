import { pagination, type Store } from './store.js';
import type { Pagination, RecipeContent, Versioned } from './models.js';

export type RecipeListStatus =
  | 'active'
  | 'inactive'
  | 'queued'
  | 'analyzing'
  | 'generating'
  | 'testing'
  | 'needs_clarification'
  | 'passed'
  | 'test_failed'
  | 'unverifiable'
  | 'completed'
  | 'failed'
  | 'cancelled';

interface RecipeListProgress {
  status: RecipeListStatus;
  execution_id: string | null;
  activity_updated_at: number | null;
  active_version_number: number | null;
  latest_version_number: number | null;
  candidate_version_id: string | null;
  candidate_version_number: number | null;
  pending_question_count: number;
  available_version_id: string | null;
  archived: boolean;
}

export type RecipeListRecipeEntry = Versioned<RecipeContent> &
  RecipeListProgress & { kind: 'recipe'; recipe_id: string };

export type RecipeListEntry =
  | RecipeListRecipeEntry
  | (RecipeListProgress & {
      kind: 'learning';
      id: string;
      name: string;
      recipe_id: null;
      execution_id: string;
      active_version_id: null;
      latest_version_id: null;
    });

type RecipeListRow = Omit<
  RecipeListProgress,
  'available_version_id' | 'archived'
> & {
  id: string;
  kind: RecipeListEntry['kind'];
  name: string;
  metadata: string | null;
};

export function readRecipeList(
  store: Store,
  options: Pagination & { q?: string; active_only?: boolean },
  all = false,
): RecipeListEntry[] {
  const rows = store.db.prepare(recipeListQuery).all({
    ...(all ? { limit: -1, offset: 0 } : pagination(options)),
    q: options.q ?? null,
    archived: Number(options.archived ?? false),
    active_only: Number(options.active_only ?? false),
  }) as RecipeListRow[];
  return rows.map(({ metadata, id, name, kind, execution_id, ...progress }) => {
    if (kind === 'learning') {
      return {
        ...progress,
        id,
        name,
        kind,
        recipe_id: null,
        execution_id: id,
        active_version_id: null,
        available_version_id: null,
        archived: false,
        latest_version_id: null,
      };
    }
    const recipe = JSON.parse(metadata!) as Versioned<RecipeContent>;
    return {
      ...recipe,
      ...progress,
      kind,
      recipe_id: id,
      execution_id,
      available_version_id: recipe.active_version_id ?? null,
      archived:
        !!recipe.latest_qualified_version_id && !recipe.active_version_id,
    };
  });
}

const recipeListQuery = `
WITH execution_activity AS (
    SELECT e.id AS execution_id, e.created,
        COALESCE(
            json_extract(e.data, '$.recipe_id'),
            json_extract(e.data, '$.result.recipe_id'),
            json_extract(e.data, '$.test_request.recipe_id'),
            base.object_id
        ) AS recipe_id,
        COALESCE(
            NULLIF(json_extract(e.data, '$.name'), ''),
            '首次学习'
        ) AS name,
        json_extract(e.data, '$.status') AS execution_status,
        json_extract(e.data, '$.stage') AS stage,
        json_extract(e.data, '$.result.test_status') AS test_status,
        json_extract(e.data, '$.updated_at') AS activity_updated_at,
        json_extract(e.data, '$.archived_at') AS archived_at,
        COALESCE(
            json_extract(e.data, '$.recipe_version_id'),
            json_extract(e.data, '$.result.recipe_version_id'),
            json_extract(e.data, '$.test_request.version_id')
        ) AS candidate_version_id,
        (
            SELECT count(*) FROM json_each(e.data, '$.questions') q
            WHERE NOT EXISTS (
                SELECT 1 FROM json_each(e.data, '$.answers') a
                WHERE a.key = json_extract(q.value, '$.id') || ':' ||
                    json_extract(q.value, '$.revision')
            )
        ) AS unanswered
    FROM objects e
    LEFT JOIN versions base ON base.id = json_extract(e.data, '$.base_recipe_version_id')
    WHERE e.kind = 'execution'
),
activities AS (
    SELECT *,
        CASE
            WHEN execution_status IN ('failed', 'cancelled') THEN execution_status
            WHEN unanswered > 0 THEN 'needs_clarification'
            WHEN execution_status = 'queued' THEN 'queued'
            WHEN execution_status = 'completed' THEN
                CASE test_status
                    WHEN 'passed' THEN 'passed'
                    WHEN 'failed' THEN 'test_failed'
                    WHEN 'unverifiable' THEN 'unverifiable'
                    ELSE 'completed'
                END
            WHEN stage = 'generate' THEN 'generating'
            WHEN stage IN ('test', 'evaluate') THEN 'testing'
            ELSE 'analyzing'
        END AS status
    FROM execution_activity
),
latest_by_recipe AS (
    SELECT *, row_number() OVER (
        PARTITION BY recipe_id ORDER BY
            (status IN ('needs_clarification', 'queued', 'analyzing', 'generating', 'testing')) DESC,
            created DESC, execution_id DESC
    ) AS position
    FROM activities
    WHERE recipe_id IS NOT NULL AND archived_at IS NULL
),
list_rows AS (
    SELECT r.id, 'recipe' AS kind, r.id AS recipe_id,
        json_extract(r.data, '$.name') AS name,
        r.data AS metadata, r.created,
        json_extract(r.data, '$.archived_at') AS archived_at,
        active.number AS active_version_number, latest.number AS latest_version_number,
        a.execution_id,
        COALESCE(a.status, CASE WHEN active.id IS NULL THEN 'inactive' ELSE 'active' END) AS status,
        a.candidate_version_id, candidate.number AS candidate_version_number,
        CASE WHEN a.status = 'needs_clarification' THEN a.unanswered ELSE 0 END AS pending_question_count,
        a.activity_updated_at,
        COALESCE(json_extract(r.data, '$.name'), '') || ' ' ||
        COALESCE(json_extract(r.data, '$.description'), '') || ' ' ||
        COALESCE(active.data, '') AS search_text,
        active.id IS NOT NULL AS is_active
    FROM objects r
    LEFT JOIN latest_by_recipe a ON a.recipe_id = r.id AND a.position = 1
    LEFT JOIN versions active ON active.id = json_extract(r.data, '$.active_version_id')
    LEFT JOIN versions latest ON latest.id = json_extract(r.data, '$.latest_version_id')
    LEFT JOIN versions candidate ON candidate.id = a.candidate_version_id
    WHERE r.kind = 'recipe'

    UNION ALL

    SELECT execution_id, 'learning', NULL, name, NULL, created, archived_at,
        NULL, NULL, execution_id, status, NULL, NULL,
        CASE WHEN status = 'needs_clarification' THEN unanswered ELSE 0 END,
        activity_updated_at, name, 0
    FROM activities
    WHERE recipe_id IS NULL AND :active_only = 0
)
SELECT id, kind, recipe_id, name, metadata, status, execution_id, activity_updated_at,
    active_version_number, latest_version_number, candidate_version_id,
    candidate_version_number, pending_question_count
FROM list_rows
WHERE (archived_at IS NOT NULL) = :archived
    AND (:active_only = 0 OR is_active)
    AND (:q IS NULL OR instr(lower(search_text), lower(:q)) > 0)
ORDER BY created, id LIMIT :limit OFFSET :offset
`;
