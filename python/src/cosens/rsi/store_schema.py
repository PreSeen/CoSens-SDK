"""SQLite layout and mapping for RSI records."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    revision INTEGER NOT NULL,
    name TEXT,
    base_execution_id TEXT,
    base_recipe_version_id TEXT,
    recipe_id TEXT,
    recipe_version_id TEXT,
    rubrics_version_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress_percent INTEGER NOT NULL CHECK (progress_percent BETWEEN 0 AND 100),
    input_revision INTEGER NOT NULL,
    owner TEXT,
    lease_until REAL,
    fence INTEGER NOT NULL DEFAULT 0,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS executions_created ON executions(created_at,id);
CREATE INDEX IF NOT EXISTS executions_recipe ON executions(recipe_id,created_at);
CREATE INDEX IF NOT EXISTS executions_queue ON executions(status,created_at,id);
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    name TEXT,
    storage_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS recipes_created ON recipes(created_at,id);
CREATE TABLE IF NOT EXISTS evidence_snapshots (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    revision INTEGER NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id TEXT PRIMARY KEY,
    recipe_id TEXT REFERENCES recipes(id),
    rubrics_id TEXT,
    rubrics_metadata TEXT,
    object_id TEXT GENERATED ALWAYS AS (coalesce(recipe_id,rubrics_id)) VIRTUAL,
    number INTEGER NOT NULL,
    data TEXT NOT NULL,
    operation_key TEXT UNIQUE,
    status TEXT CHECK (status IS NULL OR (recipe_id IS NOT NULL AND status IN ('candidate', 'available', 'archived'))),
    CHECK ((recipe_id IS NULL) <> (rubrics_id IS NULL)),
    CHECK ((rubrics_metadata IS NULL) = (rubrics_id IS NULL)),
    UNIQUE(object_id,number)
);
CREATE INDEX IF NOT EXISTS versions_rubrics
    ON versions(rubrics_id,number DESC) WHERE rubrics_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS slots (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS slots_kind ON slots(kind,expires);
CREATE TABLE IF NOT EXISTS idempotency (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY(scope,key)
);
"""

LEGACY_RECIPES = """
CREATE TABLE recipes (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    archived_at REAL,
    revision INTEGER NOT NULL,
    name TEXT,
    active_version_id TEXT,
    latest_version_id TEXT,
    latest_qualified_version_id TEXT,
    details TEXT NOT NULL
);
"""

MIGRATE_RECIPE_VERSIONS = """
UPDATE versions AS v
SET status = CASE
        WHEN v.id = r.active_version_id AND r.archived_at IS NULL THEN 'available'
        WHEN v.number <= (
            SELECT number FROM versions WHERE id = COALESCE(
                r.latest_qualified_version_id, r.active_version_id
            )
        ) THEN 'archived'
        ELSE 'candidate'
    END
FROM recipes r WHERE r.id = v.recipe_id;
ALTER TABLE recipes DROP COLUMN revision;
ALTER TABLE recipes DROP COLUMN active_version_id;
ALTER TABLE recipes DROP COLUMN latest_version_id;
ALTER TABLE recipes DROP COLUMN latest_qualified_version_id;
ALTER TABLE recipes DROP COLUMN details;
"""

RECIPE_PROJECTION = """
CREATE INDEX IF NOT EXISTS versions_recipe_number
    ON versions(recipe_id, number DESC) WHERE recipe_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS versions_recipe_available
    ON versions(recipe_id) WHERE status = 'available';
CREATE VIEW IF NOT EXISTS recipe_records AS
SELECT r.*,
    (SELECT id FROM versions
        WHERE recipe_id = r.id ORDER BY number DESC LIMIT 1) AS latest_version_id,
    active.id AS active_version_id,
    (SELECT id FROM versions
        WHERE recipe_id = r.id AND status IN ('available', 'archived')
        ORDER BY number DESC LIMIT 1) AS latest_qualified_version_id
FROM recipes r
LEFT JOIN versions active
    ON active.recipe_id = r.id AND active.status = 'available' AND r.archived_at IS NULL;
"""

MIGRATE_TASK_RECIPES = """
DROP VIEW recipe_records;

INSERT INTO idempotency(scope, key, request, result)
SELECT 'recipe_state', id, '{}',
    json_object(
        'created_at', json(printf('%!.17g', created_at)),
        'updated_at', json(printf('%!.17g', updated_at)),
        'archived_at', json(CASE WHEN archived_at IS NULL THEN 'null' ELSE printf('%!.17g', archived_at) END),
        'name', name, 'storage_path', storage_path
    )
FROM recipes;

INSERT INTO idempotency(scope, key, request, result)
SELECT 'recipe_version_state', id, '{}',
    json_object(
        'recipe_id', recipe_id, 'number', number, 'data', json(data),
        'operation_key', operation_key, 'status', status
    )
FROM versions WHERE recipe_id IS NOT NULL;

CREATE TABLE rubrics_versions (
    id TEXT PRIMARY KEY NOT NULL,
    rubrics_id TEXT NOT NULL,
    rubrics_metadata TEXT NOT NULL,
    number INTEGER NOT NULL,
    data TEXT NOT NULL,
    operation_key TEXT UNIQUE,
    UNIQUE(rubrics_id,number)
);

INSERT INTO rubrics_versions (id,rubrics_id,rubrics_metadata,number,data,operation_key)
SELECT id,rubrics_id,rubrics_metadata,number,data,operation_key
FROM versions WHERE rubrics_id IS NOT NULL;

DROP TABLE versions;
DROP TABLE recipes;

CREATE TABLE recipes (
    id TEXT PRIMARY KEY NOT NULL REFERENCES executions(id),
    recipe_id TEXT,
    recipe_version_id TEXT,
    recipe_zip_path TEXT
);
CREATE INDEX recipes_recipe_version ON recipes(recipe_id,recipe_version_id);

CREATE UNIQUE INDEX recipe_version_number
    ON idempotency(json_extract(result,'$.recipe_id'),json_extract(result,'$.number'))
    WHERE scope='recipe_version_state';
CREATE UNIQUE INDEX recipe_version_operation
    ON idempotency(json_extract(result,'$.operation_key'))
    WHERE scope='recipe_version_state';
CREATE UNIQUE INDEX recipe_version_available
    ON idempotency(json_extract(result,'$.recipe_id'))
    WHERE scope='recipe_version_state' AND json_extract(result,'$.status')='available';

CREATE VIEW recipe_objects AS
SELECT key AS id, json_extract(result,'$.created_at') AS created_at,
    json_extract(result,'$.updated_at') AS updated_at,
    json_extract(result,'$.archived_at') AS archived_at,
    json_extract(result,'$.name') AS name,
    json_extract(result,'$.storage_path') AS storage_path
FROM idempotency WHERE scope='recipe_state';

CREATE TRIGGER recipe_objects_insert INSTEAD OF INSERT ON recipe_objects
BEGIN
    SELECT RAISE(ABORT, 'Recipe metadata fields are required')
    WHERE NEW.created_at IS NULL OR NEW.updated_at IS NULL OR NEW.storage_path IS NULL;
    INSERT INTO idempotency(scope,key,request,result)
    VALUES (
        'recipe_state', NEW.id, '{}',
        json_object(
            'created_at', json(printf('%!.17g', NEW.created_at)),
            'updated_at', json(printf('%!.17g', NEW.updated_at)),
            'archived_at', json(CASE WHEN NEW.archived_at IS NULL THEN 'null'
                ELSE printf('%!.17g', NEW.archived_at) END),
            'name', NEW.name, 'storage_path', NEW.storage_path
        )
    );
END;

CREATE TRIGGER recipe_objects_update INSTEAD OF UPDATE ON recipe_objects
BEGIN
    DELETE FROM idempotency WHERE scope='recipe_state' AND key=OLD.id;
    INSERT INTO recipe_objects(id,created_at,updated_at,archived_at,name,storage_path)
    VALUES (NEW.id,NEW.created_at,NEW.updated_at,NEW.archived_at,NEW.name,NEW.storage_path);
END;

CREATE TRIGGER recipe_objects_delete INSTEAD OF DELETE ON recipe_objects
BEGIN
    SELECT RAISE(ABORT, 'Recipe still has versions')
    WHERE EXISTS (
        SELECT 1 FROM idempotency
        WHERE scope='recipe_version_state' AND json_extract(result,'$.recipe_id')=OLD.id
    );
    DELETE FROM idempotency WHERE scope='recipe_state' AND key=OLD.id;
END;

CREATE VIEW versions AS
SELECT key AS id, json_extract(result,'$.recipe_id') AS recipe_id,
    NULL AS rubrics_id, NULL AS rubrics_metadata,
    json_extract(result,'$.recipe_id') AS object_id,
    json_extract(result,'$.number') AS number,
    json_extract(result,'$.data') AS data,
    json_extract(result,'$.operation_key') AS operation_key,
    json_extract(result,'$.status') AS status
FROM idempotency WHERE scope='recipe_version_state'
UNION ALL
SELECT id, NULL, rubrics_id, rubrics_metadata, rubrics_id, number, data, operation_key, NULL
FROM rubrics_versions;

CREATE TRIGGER versions_insert INSTEAD OF INSERT ON versions
BEGIN
    SELECT RAISE(ABORT, 'invalid version fields')
    WHERE NEW.id IS NULL OR NEW.number IS NULL OR NEW.data IS NULL
        OR (NEW.recipe_id IS NULL) = (NEW.rubrics_id IS NULL)
        OR (NEW.rubrics_metadata IS NULL) <> (NEW.rubrics_id IS NULL)
        OR (NEW.status IS NOT NULL AND (
            NEW.recipe_id IS NULL OR NEW.status NOT IN ('candidate','available','archived')
        ));
    SELECT RAISE(ABORT, 'version Recipe does not exist')
    WHERE NEW.recipe_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM idempotency WHERE scope='recipe_state' AND key=NEW.recipe_id
    );
    SELECT RAISE(ABORT, 'version ID is already used')
    WHERE EXISTS (SELECT 1 FROM versions WHERE id=NEW.id);
    SELECT RAISE(ABORT, 'version operation key is already used')
    WHERE NEW.operation_key IS NOT NULL AND EXISTS (
        SELECT 1 FROM versions WHERE operation_key=NEW.operation_key
    );

    INSERT INTO idempotency(scope,key,request,result)
    SELECT 'recipe_version_state', NEW.id, '{}',
        json_object(
            'recipe_id', NEW.recipe_id, 'number', NEW.number, 'data', json(NEW.data),
            'operation_key', NEW.operation_key, 'status', NEW.status
        )
    WHERE NEW.recipe_id IS NOT NULL;

    INSERT INTO rubrics_versions (id,rubrics_id,rubrics_metadata,number,data,operation_key)
    SELECT NEW.id,NEW.rubrics_id,NEW.rubrics_metadata,NEW.number,NEW.data,NEW.operation_key
    WHERE NEW.rubrics_id IS NOT NULL;
END;

CREATE TRIGGER versions_delete INSTEAD OF DELETE ON versions
BEGIN
    DELETE FROM idempotency WHERE scope='recipe_version_state' AND key=OLD.id;
    DELETE FROM rubrics_versions WHERE id=OLD.id;
END;

CREATE TRIGGER versions_update INSTEAD OF UPDATE ON versions
BEGIN
    DELETE FROM versions WHERE id=OLD.id;
    INSERT INTO versions (id,recipe_id,rubrics_id,rubrics_metadata,number,data,operation_key,status)
    VALUES (NEW.id,NEW.recipe_id,NEW.rubrics_id,NEW.rubrics_metadata,NEW.number,NEW.data,NEW.operation_key,NEW.status);
END;

CREATE VIEW recipe_records AS
SELECT r.*,
    (SELECT id FROM versions
        WHERE recipe_id = r.id ORDER BY number DESC LIMIT 1) AS latest_version_id,
    active.id AS active_version_id,
    (SELECT id FROM versions
        WHERE recipe_id = r.id AND status IN ('available', 'archived')
        ORDER BY number DESC LIMIT 1) AS latest_qualified_version_id
FROM recipe_objects r
LEFT JOIN versions active
    ON active.recipe_id = r.id AND active.status = 'available' AND r.archived_at IS NULL;
"""

_IDENTITY = ("id", "created_at", "updated_at", "archived_at")
_COMMON = (*_IDENTITY, "revision")

STORAGE = {
    "execution": (
        "executions",
        _COMMON
        + (
            "name",
            "base_execution_id",
            "base_recipe_version_id",
            "recipe_id",
            "recipe_version_id",
            "rubrics_version_id",
            "status",
            "stage",
            "progress_percent",
            "input_revision",
        ),
    ),
    "recipe": ("recipes", (*_IDENTITY, "name", "storage_path")),
    "material": ("evidence_snapshots", _COMMON),
}
