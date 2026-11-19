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
    revision INTEGER NOT NULL,
    name TEXT,
    active_version_id TEXT,
    latest_version_id TEXT,
    latest_qualified_version_id TEXT,
    details TEXT NOT NULL
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

_COMMON = ("id", "created_at", "updated_at", "archived_at", "revision")

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
    "recipe": (
        "recipes",
        _COMMON
        + (
            "name",
            "active_version_id",
            "latest_version_id",
            "latest_qualified_version_id",
        ),
    ),
    "material": ("evidence_snapshots", _COMMON),
}
