import { pagination, type Store } from './store.js';
import type { Pagination, Recipe } from './models.js';

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
  execution_ids: string[];
  activity_updated_at: number | null;
  created_at: number;
  updated_at: number;
  active_version_number: number | null;
  latest_version_number: number | null;
  candidate_version_id: string | null;
  candidate_version_number: number | null;
  pending_question_count: number;
  available_version_id: string | null;
  archived: boolean;
}

export type RecipeListRecipeEntry = Recipe &
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
  'available_version_id' | 'archived' | 'execution_ids'
> & {
  id: string;
  kind: RecipeListEntry['kind'];
  name: string;
  metadata: string | null;
  execution_ids: string;
};

export function readRecipeList(
  store: Store,
  options: Pagination & {
    q?: string;
    active_only?: boolean;
    recipe_ids?: string[];
  },
  all = false,
): RecipeListEntry[] {
  const rows = store.db.prepare(recipeListQuery).all({
    ...(all ? { limit: -1, offset: 0 } : pagination(options)),
    q: options.q ?? null,
    recipe_ids:
      options.recipe_ids === undefined
        ? null
        : JSON.stringify(options.recipe_ids),
    archived: Number(options.archived ?? false),
    active_only: Number(options.active_only ?? false),
  }) as RecipeListRow[];
  return rows.map(
    ({
      metadata,
      id,
      name,
      kind,
      execution_id,
      execution_ids,
      ...progress
    }) => {
      if (kind === 'learning') {
        return {
          ...progress,
          id,
          name,
          kind,
          recipe_id: null,
          execution_id: id,
          execution_ids: [id],
          active_version_id: null,
          available_version_id: null,
          archived: false,
          latest_version_id: null,
        };
      }
      const recipe = JSON.parse(metadata!) as Recipe;
      return {
        ...recipe,
        ...progress,
        name,
        kind,
        recipe_id: id,
        execution_id,
        execution_ids: JSON.parse(execution_ids) as string[],
        available_version_id: recipe.active_version_id ?? null,
        archived:
          !!recipe.latest_qualified_version_id && !recipe.active_version_id,
      };
    },
  );
}

const recipeListQuery = `
WITH requested_recipes AS (
    SELECT value AS id FROM json_each(:recipe_ids)
),
execution_activity AS (
    SELECT e.id AS execution_id, e.created_at AS created,
        COALESCE(
            e.recipe_id,
            json_extract(e.details, '$.test_request.recipe_id'),
            base.recipe_id
        ) AS recipe_id,
        COALESCE(
            NULLIF(e.name, ''),
            '首次学习'
        ) AS name,
        e.status AS execution_status,
        e.stage AS stage,
        json_extract(e.details, '$.result.test_status') AS test_status,
        e.updated_at AS activity_updated_at,
        e.archived_at AS archived_at,
        COALESCE(
            e.recipe_version_id,
            json_extract(e.details, '$.test_request.version_id')
        ) AS candidate_version_id,
        (
            SELECT count(*) FROM json_each(e.details, '$.questions') q
            WHERE NOT EXISTS (
                SELECT 1 FROM json_each(e.details, '$.answers') a
                WHERE a.key = json_extract(q.value, '$.id') || ':' ||
                    json_extract(q.value, '$.revision')
            )
        ) AS unanswered
    FROM executions e
    LEFT JOIN versions base ON base.id = e.base_recipe_version_id
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
        AND (:recipe_ids IS NULL OR recipe_id IN (SELECT id FROM requested_recipes))
),
execution_ids_by_recipe AS (
    SELECT recipe_id, json_group_array(execution_id) AS execution_ids
    FROM (SELECT recipe_id, execution_id FROM latest_by_recipe ORDER BY recipe_id, position)
    GROUP BY recipe_id
),
list_rows AS (
    SELECT r.id, 'recipe' AS kind, r.id AS recipe_id,
        COALESCE(r.name, a.name, '首次学习') AS name,
        json_object('id', r.id, 'name', r.name, 'storage_path', r.storage_path,
            'created_at', r.created_at, 'updated_at', r.updated_at, 'archived_at', r.archived_at,
            'active_version_id', r.active_version_id,
            'latest_version_id', r.latest_version_id,
            'latest_qualified_version_id', r.latest_qualified_version_id) AS metadata,
        r.created_at AS created, r.updated_at AS updated_at,
        r.archived_at AS archived_at,
        active.number AS active_version_number, latest.number AS latest_version_number,
        a.execution_id,
        COALESCE(ids.execution_ids, '[]') AS execution_ids,
        COALESCE(a.status, CASE WHEN active.id IS NULL THEN 'inactive' ELSE 'active' END) AS status,
        a.candidate_version_id, candidate.number AS candidate_version_number,
        CASE WHEN a.status = 'needs_clarification' THEN a.unanswered ELSE 0 END AS pending_question_count,
        a.activity_updated_at,
        COALESCE(r.name, '') || ' ' ||
        COALESCE(active.data, '') AS search_text,
        active.id IS NOT NULL AS is_active
    FROM recipe_records r
    LEFT JOIN latest_by_recipe a ON a.recipe_id = r.id AND a.position = 1
    LEFT JOIN execution_ids_by_recipe ids ON ids.recipe_id = r.id
    LEFT JOIN versions active ON active.id = r.active_version_id
    LEFT JOIN versions latest ON latest.id = r.latest_version_id
    LEFT JOIN versions candidate ON candidate.id = a.candidate_version_id
    WHERE :recipe_ids IS NULL OR r.id IN (SELECT id FROM requested_recipes)

    UNION ALL

    SELECT execution_id, 'learning', NULL, name, NULL, created, activity_updated_at, archived_at,
        NULL, NULL, execution_id, json_array(execution_id), status, NULL, NULL,
        CASE WHEN status = 'needs_clarification' THEN unanswered ELSE 0 END,
        activity_updated_at, name, 0
    FROM activities
    WHERE recipe_id IS NULL AND :active_only = 0 AND :recipe_ids IS NULL
)
SELECT id, kind, recipe_id, name, metadata, status, execution_id, execution_ids, activity_updated_at,
    created AS created_at, updated_at,
    active_version_number, latest_version_number, candidate_version_id,
    candidate_version_number, pending_question_count
FROM list_rows
WHERE (archived_at IS NOT NULL) = :archived
    AND (:active_only = 0 OR is_active)
    AND (:q IS NULL OR instr(lower(search_text), lower(:q)) > 0)
ORDER BY created, id LIMIT :limit OFFSET :offset
`;
