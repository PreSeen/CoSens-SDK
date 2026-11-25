"""Local RSI contracts, real graph, concurrency, clarification and recovery."""

import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

pytest.importorskip("langgraph")

from cosens.rsi import Conflict, InvalidState, NotFound
from cosens.rsi._queue_client import RSIClient
from cosens.rsi.materials import atomic_json, read_snapshot
from cosens.rsi.models import LeaseLost, RecipeContent, RubricsContent
from cosens.rsi.store_schema import LEGACY_RECIPES, RECIPE_PROJECTION, SCHEMA


def settings(**overrides):
    return {
        "model": {"model": "local-test", "base_url": "http://127.0.0.1:1/v1"},
        **overrides,
    }


@pytest.fixture
def client(tmp_path):
    return RSIClient(data_dir=tmp_path / "db", config=settings(), auto_start=False)


def material(client, tmp_path, *, text="Write the answer 42 into answer.txt", key=None):
    path = tmp_path / ("context-" + str(time.time_ns()) + ".md")
    path.write_text(text)
    return client.material_create(
        manifest={"context_path": str(path)}, idempotency_key=key
    )


def existing_versioned_object(client, kind, *, name, content):
    if kind == "rubrics":
        content = RubricsContent.model_validate(content).model_dump()
    with client.store.transaction() as conn:
        obj = client.store.insert(
            conn,
            kind,
            {
                "name": name,
                "description": "",
                "latest_version_id": None,
                **({"active_version_id": None} if kind == "recipe" else {}),
            },
        )
        client.store.append_version(conn, obj, content)
        return client.store.get_in(conn, obj["id"], kind)


def policy(**overrides):
    return {
        "artifact_confirmed": True,
        "confirmation_ref": "host-confirmation-1",
        "test_task": {
            "instruction": "Write 42 into answer.txt",
            "checks": [{"kind": "text_equals", "path": "answer.txt", "value": "42"}],
        },
        **overrides,
    }


def run(client, material_id, **overrides):
    return client.execution_create(
        material_ids=[material_id], policy=policy(**overrides)
    )


class FileRuntime:
    """Deterministic runtime port. It really writes/reads test artifacts, no LLM."""

    def __init__(self, *, questions=False, output="42", verifiable=True, fail=False):
        self.questions, self.output, self.verifiable, self.fail = (
            questions,
            output,
            verifiable,
            fail,
        )
        self.generation_count = 0
        self.test_catalogs = []

    def analyze(self, context, files, trace_path):
        from langsmith import get_tracing_context

        assert get_tracing_context()["enabled"] is False
        read_snapshot(files[0])
        mid = files[0]["locator"].split("/")[0]
        questions = self.questions and not context["answers"]
        result = {
            "sufficient_information": not questions,
            "hypothesis": "Assume decimal" if questions else None,
            "questions": [
                {
                    "id": "format",
                    "question": "Which format?",
                    "assumption": "decimal",
                    "options": [
                        {"id": "decimal", "label": "Decimal"},
                        {"id": "hex", "label": "Hex"},
                    ],
                }
            ]
            if questions
            else [],
            "rubrics": {
                "criteria": [
                    {
                        "id": "correct",
                        "description": "Output 42",
                        "source": "task",
                        "evidence": [
                            {"material_id": mid, "locator": files[0]["locator"]}
                        ],
                    }
                ]
            },
        }
        atomic_json(trace_path, result)
        return result

    def generate(self, context, files, trace_path):
        self.generation_count += 1
        if self.fail:
            raise RuntimeError("secret-upstream-key-must-not-be-stored")
        result = {
            "name": "Answer writer",
            "description": "Write a fixed task answer",
            "instructions": "Write 42 into answer.txt",
        }
        atomic_json(trace_path, result)
        return result

    def execute(self, context, files, workspace, trace_path):
        self.test_catalogs.append(files)
        (workspace / "answer.txt").write_text(self.output)
        atomic_json(trace_path, {"tool": "write_output", "path": "answer.txt"})
        return {"summary": "Created output"}

    def assess(self, context, files, trace_path):
        assert context["recipe"]["instructions"]
        assert any(
            f["role"] == "trace" and json.loads(read_snapshot(f)["text"]) for f in files
        )
        assert any(
            read_snapshot(f)["text"] == self.output
            for f in files
            if f["role"] == "artifact"
        )
        result = {
            "verifiable": self.verifiable,
            "scores": [
                {"criterion_id": c["id"], "score": 1.0, "evidence": "test/answer.txt"}
                for c in context["rubrics"]["criteria"]
            ],
            "reason": "Read actual output",
        }
        atomic_json(trace_path, result)
        return result


def test_material_snapshot_immutable_and_deduplicated(client, tmp_path):
    path = tmp_path / "context.md"
    path.write_text("original")
    manifest = {"context_path": str(path)}
    m = client.material_create(manifest=manifest, idempotency_key="hook-1")
    path.write_text("changed")
    assert read_snapshot(m["files"][0])["text"] == "original"
    assert (
        client.material_create(manifest=manifest, idempotency_key="hook-1")["id"]
        == m["id"]
    )
    client.material_update(m["id"], archived=True)
    assert client.material_list() == []
    assert client.material_list(archived=True)[0]["id"] == m["id"]
    with pytest.raises(InvalidState):
        run(client, m["id"])
    client.material_update(m["id"], archived=False)


def test_complete_learning_test_and_activation(client, tmp_path):
    m = material(client, tmp_path)
    execution = run(client, m["id"])
    runtime = FileRuntime()
    assert client.run_pending(runtime=runtime)["processed"] == 1
    result = client.execution_get(execution["id"])
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == "passed"
    assert result["result"]["published"]
    assert result["analysis"]["attribution"] is None
    assert result["test_results"][0]["artifacts"][0]["sha256"]
    assert runtime.test_catalogs == [[]]  # host context/output is excluded
    assert (
        client.recipe_get(result["recipe_id"])["selected_version"]["id"]
        == result["recipe_version_id"]
    )
    assert (
        client.recipe_list(q="answer", active_only=True)[0]["id"] == result["recipe_id"]
    )
    assert list(
        (client.data_dir / "executions" / execution["id"]).glob("checkpoints-*.sqlite3")
    )


@pytest.mark.parametrize("cite_test_input", [False, True])
def test_test_and_evaluation_catalogs_require_explicit_file_authorization(
    client, tmp_path, cite_test_input
):
    context = tmp_path / "context.md"
    context.write_text("Create answer.txt with the number in the current input.")
    files = []
    for name, role in [
        ("old-orders.csv", "reference"),
        ("historical-input.csv", "input"),
        ("current-orders.csv", "input"),
        ("format.md", "reference"),
        ("confirmed-output.csv", "artifact"),
    ]:
        path = tmp_path / name
        path.write_text(
            "order_id,ordered,received\nA-12,4,7\n"
            if name == "old-orders.csv"
            else name
        )
        files.append({"path": str(path), "role": role})
    m = client.material_create(manifest={"context_path": str(context), "files": files})
    old_reference, old_input, current, shared, historical_output = [
        f["locator"] for f in m["files"][1:]
    ]
    task = policy()["test_task"] | {
        "input_locators": [current],
        "shared_reference_locators": [shared],
    }
    for invalid in (historical_output, "material_unknown/0"):
        with pytest.raises(ValueError, match="input_locators"):
            run(client, m["id"], test_task=task | {"input_locators": [invalid]})

    attribution = {
        "possible_causes": [
            {
                "observed_issue": "The old order was reported with the wrong quantity",
                "reason": "The quantity columns may have been swapped",
                "uncertainty": "The historical input does not include the calculation",
                "evidence_refs": [
                    {
                        "material_id": m["id"],
                        "locator": current if cite_test_input else old_input,
                    }
                ],
            }
        ]
    }

    class VerifyCatalogs(FileRuntime):
        def analyze(self, context, files, trace_path):
            result = super().analyze(context, files, trace_path) | {
                "attribution": attribution
            }
            if not cite_test_input and not context.get("portability_feedback"):
                result["rubrics"]["criteria"][0]["description"] = (
                    "Historical A-12 must appear as OVER"
                )
            return result

        def generate(self, context, files, trace_path):
            locators = {file["locator"] for file in files}
            assert {old_reference, old_input, shared, historical_output} <= locators
            assert current not in locators
            assert "test_task" not in context["policy"]
            assert context["analysis"]["attribution"] == attribution
            assert (
                context["analysis"]["rubrics"]["criteria"][0]["description"]
                == "Output 42"
            )
            result = super().generate(context, files, trace_path)
            if not context.get("portability_feedback"):
                result["instructions"] += "\nWorked example: A-12,4,7"
            return result

        def execute(self, context, files, workspace, trace_path):
            assert "A-12" not in json.dumps(context)
            return super().execute(context, files, workspace, trace_path)

        def assess(self, context, files, trace_path):
            assert "A-12" not in json.dumps(context)
            assert set(context["rubrics"]["criteria"][0]) == {
                "id",
                "description",
                "required",
            }
            locators = {file["locator"] for file in files}
            assert {old_reference, old_input, historical_output}.isdisjoint(locators)
            assert {current, shared, "test/answer.txt"} <= locators
            return super().assess(context, files, trace_path)

    execution = run(client, m["id"], test_task=task)
    runtime = VerifyCatalogs()
    client.run_pending(runtime=runtime)
    result = client.execution_get(execution["id"])
    if cite_test_input:
        assert result["status"] == "failed", result
        assert result["error"]["code"] == "invalid_model_output"
        assert runtime.generation_count == 0
        return
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == "passed"
    assert result["analysis"]["attribution"] == attribution
    assert runtime.generation_count == 2
    assert [[f["locator"] for f in files] for files in runtime.test_catalogs] == [
        [current, shared]
    ]


def test_feedback_extraction_rejects_refs_outside_context_and_feedback(
    client, tmp_path
):
    context = tmp_path / "context.md"
    context.write_text("Write the answer into answer.txt")
    reference = tmp_path / "reference.md"
    reference.write_text("Example output")
    source = client.material_create(
        manifest={
            "context_path": str(context),
            "files": [{"path": str(reference), "role": "reference"}],
        }
    )

    class MisattributedFeedback(FileRuntime):
        def extract_feedback(self, files, trace_path):
            assert [f["role"] for f in files] == ["context"]
            return {
                "items": [
                    {
                        "kind": "requirement",
                        "rule": "Write only the answer",
                        "evidence_refs": [
                            {
                                "material_id": source["id"],
                                "locator": source["files"][1]["locator"],
                            }
                        ],
                    }
                ]
            }

    execution = run(client, source["id"])
    client.run_pending(runtime=MisattributedFeedback())
    result = client.execution_get(execution["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "invalid_model_output"
    assert client.rubrics_list() == []


def test_version_availability_archive_and_restore(client, tmp_path):
    from cosens.rsi import RSIClient as PublicClient

    source = material(client, tmp_path)
    execution = run(client, source["id"])
    client.run_pending(runtime=FileRuntime())
    result = client.execution_get(execution["id"])
    recipe_id, version_id = result["recipe_id"], result["recipe_version_id"]
    host = PublicClient(data_dir=client.data_dir, config=None)
    assert not hasattr(host, "recipe_activate")
    assert host.recipe_list()[0]["active_version_id"] == version_id
    assert not host.recipe_get(recipe_id, version=version_id)["selected_version"][
        "archived"
    ]
    assert host.recipe_version_set_archived(
        recipe_id, version_id=version_id, archived=True
    ) == {"recipe_id": recipe_id, "version_id": version_id, "archived": True}
    reopened = PublicClient(data_dir=client.data_dir, config=None)
    [row] = reopened.recipe_list()
    assert row["active_version_id"] is None
    assert (
        reopened.recipe_get(recipe_id, version="latest")["latest_qualified_version_id"]
        == version_id
    )
    assert reopened.recipe_get(recipe_id, version=version_id)["selected_version"][
        "archived"
    ]
    assert reopened.recipe_get(recipe_id, version="active")["selected_version"] is None

    reopened.recipe_version_set_archived(
        recipe_id, version_id=version_id, archived=False
    )
    assert reopened.recipe_list()[0]["active_version_id"] == version_id

    revision = client.execution_create(
        material_ids=[source["id"]], policy=policy(), base_recipe_version_id=version_id
    )
    assert reopened.recipe_list()[0]["active_version_id"] == version_id
    client.run_pending(runtime=FileRuntime())
    new_id = client.execution_get(revision["id"])["recipe_version_id"]
    assert reopened.recipe_list()[0]["active_version_id"] == new_id
    recipe = reopened.recipe_get(recipe_id, version="active")
    assert recipe["storage_path"] == f"recipes/{recipe_id}"
    for exported_id in (version_id, new_id):
        path = reopened.data_dir / recipe["storage_path"] / exported_id / "RECIPE.md"
        assert "---\n\nWrite 42" in path.read_text(encoding="utf-8")

    assert reopened.recipe_get(recipe_id, version=version_id)["selected_version"][
        "archived"
    ]
    reopened.recipe_version_set_archived(
        recipe_id, version_id=version_id, archived=True
    )
    assert reopened.recipe_list()[0]["active_version_id"] == new_id
    with pytest.raises(InvalidState, match="only the latest qualified"):
        reopened.recipe_version_set_archived(
            recipe_id, version_id=version_id, archived=False
        )


def test_seeded_recipe_requires_skill_metadata_and_creates_available_version(tmp_path):
    from cosens.rsi import RSIClient as PublicClient
    from cosens.rsi.recipe_file import read_recipe_file

    host = PublicClient(data_dir=tmp_path)

    def seed(source):
        return host._queue.recipe_insert(*read_recipe_file(source))

    path = tmp_path / "SKILL.md"
    metadata = {
        "name": "order-export",
        "description": 'Export orders: "CSV"\nUse when requested.',
    }
    instructions = "    existing Markdown code block\r\n"
    generated = {**metadata, "instructions": instructions}
    for field in ("name", "description"):
        missing = {key: value for key, value in generated.items() if key != field}
        with pytest.raises(ValidationError):
            RecipeContent.model_validate(missing)
        with pytest.raises(ValidationError):
            RecipeContent.model_validate({**generated, field: " \t "})
    for source, error in (
        ("# Recipe\n\nNo frontmatter", "delimited by ---"),
        ("---\nname: order-export\ndescription: Export orders\n", "delimited by ---"),
        ("---\nname: [broken\ndescription: Export orders\n---", "valid YAML mapping"),
        (
            "---\nname: first\nname: duplicate\ndescription: Export orders\n---",
            "valid YAML mapping",
        ),
        ("---\nname: order-export\n---", "name and description"),
        ('---\nname: " "\ndescription: Export orders\n---', "name and description"),
        ("---\nname: order-export\ndescription: 42\n---", "name and description"),
    ):
        path.write_text(source, encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            seed(path)
    path.write_bytes(b"\x80")
    with pytest.raises(ValueError, match="readable UTF-8 Markdown file"):
        seed(path)
    with pytest.raises(ValueError, match="readable UTF-8 Markdown file"):
        seed(tmp_path)
    assert host.recipe_list() == []

    source = (
        "\ufeff---\r\nname: order-export\r\ndescription: |-\r\n"
        '  Export orders: "CSV"\r\n  Use when requested.\r\n'
        'metadata:\r\n  version: "1.0"\r\n---\r\n\r\n' + instructions
    )
    path.write_bytes(source.encode("utf-8"))
    first = seed(path)
    second = seed(str(path))
    assert first["id"] != second["id"]
    assert first["active_version_id"] != second["active_version_id"]
    assert host.recipe_get(first["id"], version=first["active_version_id"]) == first
    assert host.recipe_get(first["id"]) == first
    version = first["selected_version"]
    assert version["number"] == 1
    assert version["content"] == generated
    assert version["provenance"] == {"source": "external"}
    assert first["active_version_id"] == version["id"]
    assert host.recipe_get(first["id"], version="active") == first
    assert host.execution_list() == []
    [row] = host.recipe_list(query={"recipe_ids": [first["id"]]})
    assert "status" not in row
    assert row["execution_ids"] == []
    stored = tmp_path / first["storage_path"] / version["id"] / "RECIPE.md"
    assert stored.read_bytes() == source.encode("utf-8")

    path.write_text(
        "---\nname: minimal\ndescription: Metadata only\n---", encoding="utf-8"
    )
    minimal = seed(path)
    assert minimal["selected_version"]["content"]["instructions"] == ""


@pytest.mark.parametrize("schema_version", [3, 4, 5, 6])
def test_recipe_migration_preserves_archived_target_and_candidate(
    tmp_path, schema_version
):
    from cosens.rsi import RSIClient as PublicClient

    recipe_id = "recipe_legacy"
    path = tmp_path / "rsi.sqlite3"
    conn = sqlite3.connect(path)
    try:
        if schema_version == 3:
            conn.executescript(
                LEGACY_RECIPES
                + """
                CREATE TABLE versions (
                  id TEXT PRIMARY KEY, recipe_id TEXT REFERENCES recipes(id), rubrics_id TEXT,
                  rubrics_metadata TEXT,
                  object_id TEXT GENERATED ALWAYS AS (coalesce(recipe_id,rubrics_id)) VIRTUAL,
                  number INTEGER NOT NULL, data TEXT NOT NULL, operation_key TEXT UNIQUE,
                  UNIQUE(object_id, number)
                );
                INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 8, 'Legacy Recipe',
                  NULL, 'version_second', 'version_first', '{"description":"Retained description"}');
                INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 5, 'Waiting Recipe',
                  NULL, NULL, NULL, '{"description":"Description without a version"}');
                """
            )
        elif schema_version == 6:
            conn.executescript(
                SCHEMA
                + """
                INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 'Legacy Recipe', 'recipes/recipe_legacy');
                INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 'Waiting Recipe', 'recipes/recipe_unversioned');
                """
            )
        else:
            conn.executescript(
                SCHEMA.replace(",\n    storage_path TEXT NOT NULL", "")
                + """
                INSERT INTO recipes VALUES ('recipe_legacy', 1, 2, NULL, 'Legacy Recipe');
                INSERT INTO recipes VALUES ('recipe_unversioned', 2, 3, NULL, 'Waiting Recipe');
                """
            )
        if schema_version == 4:
            conn.executescript(
                """
                CREATE TABLE recipe_api_metadata (
                    recipe_id TEXT PRIMARY KEY REFERENCES recipes(id),
                    revision INTEGER NOT NULL, description TEXT NOT NULL
                );
                INSERT INTO recipe_api_metadata VALUES ('recipe_legacy', 8, 'Retired description');
                INSERT INTO recipe_api_metadata VALUES ('recipe_unversioned', 5, 'Retired description');
                CREATE VIEW recipe_records AS SELECT r.*, m.revision, m.description
                    FROM recipes r LEFT JOIN recipe_api_metadata m ON m.recipe_id = r.id;
                """
            )
        if schema_version >= 5:
            conn.executescript(RECIPE_PROJECTION)
        conn.execute(f"PRAGMA user_version = {schema_version}")
        for version_id, number in (("version_first", 1), ("version_second", 2)):
            version = {
                "id": version_id,
                "object_id": recipe_id,
                "number": number,
                "content": {
                    "title": "Legacy Recipe",
                    "applicability": "Fixture",
                    "instructions": "Produce output",
                },
                "provenance": {},
                "created_at": 1,
            }
            conn.execute(
                "INSERT INTO versions (id,recipe_id,number,data) VALUES (?,?,?,?)",
                (version_id, recipe_id, number, json.dumps(version)),
            )
        if schema_version >= 4:
            conn.execute(
                "UPDATE versions SET status=CASE WHEN number=1 THEN 'archived' ELSE 'candidate' END"
            )
        conn.commit()
    finally:
        conn.close()

    host = PublicClient(data_dir=tmp_path, config=None)
    recipe = host.recipe_get(recipe_id, version="latest")
    assert recipe["latest_version_id"] == "version_second"
    assert recipe["latest_qualified_version_id"] == "version_first"
    assert recipe["active_version_id"] is None
    assert recipe["storage_path"] == f"recipes/{recipe_id}"
    for version_id in ("version_first", "version_second"):
        path = tmp_path / recipe["storage_path"] / version_id / "RECIPE.md"
        assert path.read_text(encoding="utf-8") == (
            '---\nname: "Legacy Recipe"\ndescription: "Fixture"\n---\n\nProduce output\n'
        )
    assert "revision" not in recipe
    assert "description" not in recipe
    assert recipe["selected_version"]["id"] == "version_second"
    assert recipe["selected_version"]["content"] == {
        "name": "Legacy Recipe",
        "description": "Fixture",
        "instructions": "Produce output",
    }
    unversioned = host.recipe_get("recipe_unversioned", version="latest")
    assert "revision" not in unversioned
    assert "description" not in unversioned
    assert unversioned["selected_version"] is None
    host.recipe_version_set_archived(
        recipe_id, version_id="version_first", archived=False
    )
    assert host.recipe_list()[0]["active_version_id"] == "version_first"
    assert (
        host.recipe_get(recipe_id, version="active")["selected_version"]["id"]
        == "version_first"
    )
    with host._queue.store.connect() as migrated:
        assert {
            row["name"] for row in migrated.execute("PRAGMA table_info(recipes)")
        } == {"id", "recipe_id", "recipe_version_id", "recipe_zip_path"}
        assert {
            row["name"] for row in migrated.execute("PRAGMA table_info(recipe_objects)")
        } == {
            "id",
            "name",
            "created_at",
            "updated_at",
            "archived_at",
            "storage_path",
        }
        assert [
            row[0]
            for row in migrated.execute("SELECT status FROM versions ORDER BY number")
        ] == [
            "available",
            "candidate",
        ]
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 10
        assert (
            migrated.execute(
                "SELECT name FROM sqlite_schema WHERE name='recipe_api_metadata'"
            ).fetchone()
            is None
        )
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "changes,expected,rounds",
    [
        ({"test_task": None}, "unverifiable", 1),
        ({}, "failed", 2),
    ],
)
def test_missing_task_or_failed_objective_check_cannot_publish(
    client, tmp_path, changes, expected, rounds
):
    execution = run(client, material(client, tmp_path)["id"], **changes)
    runtime = FileRuntime(output="wrong")
    client.run_pending(runtime=runtime)
    result = client.execution_get(execution["id"])
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == expected
    assert len(result["test_results"]) == rounds
    assert not result["result"]["published"]
    recipe = client.recipe_get(result["recipe_id"])
    with pytest.raises(InvalidState):
        client.recipe_version_set_archived(
            recipe["id"], version_id=result["recipe_version_id"], archived=False
        )


@pytest.mark.parametrize(
    "covered_score, expected_status, expected_rounds",
    [(1, "passed", 1), (None, "unverifiable", 1), (0, "failed", 2)],
)
def test_uncovered_criteria_excluded_from_scoring_and_iteration(
    client, tmp_path, covered_score, expected_status, expected_rounds
):
    milestones = []

    def record_progress():
        milestones.append(client.execution_get(execution["id"])["progress_percent"])

    class CoverageRuntime(FileRuntime):
        def analyze(self, context, files, trace_path):
            record_progress()
            analysis = super().analyze(context, files, trace_path)
            analysis["recipe_name"] = "Format signed answers"
            analysis["rubrics"]["criteria"].append(
                {
                    "id": "negative-answer-format",
                    "description": "If the answer is negative, preserve its sign",
                    "source": "agent_inference",
                    "required": True,
                }
            )
            return analysis

        def generate(self, context, files, trace_path):
            record_progress()
            assert context["recipe_name"] == "Format signed answers"
            result = super().generate(context, files, trace_path)
            return {**result, "name": f"Alternative name {self.generation_count}"}

        def execute(self, *args):
            record_progress()
            return super().execute(*args)

        def assess(self, context, files, trace_path):
            record_progress()
            result = super().assess(context, files, trace_path)
            result["scores"][1].update(
                status="unverified", score=None, evidence="No negative input"
            )
            result["scores"][0].update(
                status="unverified" if covered_score is None else "verified",
                score=covered_score,
            )
            return result

    execution = run(client, material(client, tmp_path)["id"])
    runtime = CoverageRuntime()
    client.run_pending(runtime=runtime)
    result = client.execution_get(execution["id"])
    assert result["status"] == "completed", result
    expected_progress = [10, 25, 40, 55]
    if expected_rounds == 2:
        expected_progress += [65, 75, 90]
    assert milestones == expected_progress
    assert result["progress_percent"] == 100
    assert result["result"]["test_status"] == expected_status
    assert result["result"]["published"] == (expected_status == "passed")
    assert runtime.generation_count == expected_rounds
    assert len(result["test_results"]) == expected_rounds
    assert result["name"] == client.recipe_list()[0]["name"] == "Format signed answers"
    recipe_id = result["recipe_id"]
    latest = client.recipe_get(recipe_id, version_selector="latest")["selected_version"]
    assert latest["number"] == expected_rounds
    for test in result["test_results"]:
        version = client.recipe_get(recipe_id, version_id=test["version_id"])[
            "selected_version"
        ]
        assert version["content"]["name"] == result["name"]
        assert test["score"] == covered_score
        assert test["scores"][1]["status"] == "unverified"
        assert test["scores"][1]["score"] is None


class TwoQuestionRuntime(FileRuntime):
    def analyze(self, context, files, trace_path):
        result = super().analyze(context, files, trace_path)
        if result["questions"]:
            result["questions"].append(
                {
                    "id": "precision",
                    "question": "Which precision?",
                    "assumption": "exact",
                    "options": [
                        {"id": "exact", "label": "Exact"},
                        {"id": "rounded", "label": "Rounded"},
                    ],
                }
            )
        return result


def test_late_clarification_waits_for_all_answers_and_creates_child_once(
    client, tmp_path
):
    execution = run(client, material(client, tmp_path)["id"])
    client.run_pending(runtime=TwoQuestionRuntime(questions=True))
    parent = client.execution_get(execution["id"])
    assert parent["result"]["provisional"]
    assert not parent["result"]["published"]
    first = {
        "question_id": "format",
        "question_revision": 1,
        "selected_option_ids": ["decimal"],
        "idempotency_key": "answer-1",
    }
    partial = {"execution_id": parent["id"], "continuation_execution_id": None}
    assert client.execution_clarification_answer(parent["id"], **first) == partial
    assert client.execution_clarification_answer(parent["id"], **first) == partial
    assert len(client.execution_list()) == 1
    assert (
        client.execution_get(parent["id"])["input_revision"] == parent["input_revision"]
    )
    last = {
        **first,
        "question_id": "precision",
        "selected_option_ids": ["exact"],
        "idempotency_key": "answer-2",
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(client.execution_clarification_answer, parent["id"], **last)
            for _ in range(2)
        ]
        answer, replay = [call.result() for call in calls]
    assert answer == replay
    assert len(client.execution_list()) == 2
    saved = client.execution_get(parent["id"])
    recorded = saved["answers"]["precision:1"]
    assert recorded["continuation_execution_id"] == answer["continuation_execution_id"]
    child = client.execution_get(answer["continuation_execution_id"])
    assert child["base_execution_id"] == parent["id"]
    assert child["input_revision"] == parent["input_revision"] + 1
    assert child["base_recipe_version_id"] == parent["recipe_version_id"]
    assert child["answers"]["format:1"]["selected_options"][0]["label"] == "Decimal"
    assert child["answers"]["precision:1"]["selected_options"][0]["label"] == "Exact"
    client.run_pending(runtime=TwoQuestionRuntime(questions=True))
    child = client.execution_get(child["id"])
    assert child["recipe_id"] == parent["recipe_id"]
    assert child["result"]["published"]
    assert client.execution_get(parent["id"])["result"] == parent["result"]
    with pytest.raises(Conflict):
        client.execution_clarification_answer(
            parent["id"],
            **{**first, "selected_option_ids": ["hex"], "idempotency_key": "other"},
        )


def test_last_answer_during_generation_creates_one_child(client, tmp_path):
    from cosens.rsi import RSIClient as HostClient

    execution = run(client, material(client, tmp_path)["id"])
    latest = HostClient(
        data_dir=client.data_dir,
        model_id="fixture-replacement",
        api_key="fixture-refreshed",
        base_url="http://127.0.0.1:1/v1",
        config={
            "credential_scope": "account-primary",
            "auto_start": False,
        },
    )._queue.config.model_dump()

    class AnsweringRuntime(TwoQuestionRuntime):
        child_id = None

        def analyze(self, context, *args):
            return {
                **super().analyze(context, *args),
                "recipe_name": "Attempted rename"
                if context["answers"]
                else "Format numbers",
            }

        def generate(self, context, *args):
            assert context["recipe_name"] == "Format numbers"
            if not self.generation_count:
                with client.store.transaction() as conn:
                    running = client.store.get_in(conn, execution["id"], "execution")
                    assert running["status"] == "running"
                    client.store.save(conn, {**running, "recovery_config": latest})
                first = client.execution_clarification_answer(
                    execution["id"],
                    question_id="format",
                    question_revision=1,
                    selected_option_ids=["decimal"],
                )
                assert first["continuation_execution_id"] is None
                assert client.execution_get(execution["id"])["status"] == "running"
                assert client.execution_get(execution["id"])["input_revision"] == 1
                assert len(client.execution_list()) == 1
                last = client.execution_clarification_answer(
                    execution["id"],
                    question_id="precision",
                    question_revision=1,
                    selected_option_ids=["exact"],
                )
                self.child_id = last["continuation_execution_id"]
            else:
                assert set(context["answers"]) == {"format:1", "precision:1"}
            return super().generate(context, *args)

    runtime = AnsweringRuntime(questions=True)
    client.run_pending(runtime=runtime)
    parent = client.execution_get(execution["id"])
    assert parent["status"] == "cancelled"
    assert "recovery_config" not in parent
    assert parent["input_revision"] == 1
    assert parent["test_results"] == []
    final = client.execution_get(runtime.child_id)
    assert final["status"] == "completed", final
    assert final["runtime_config"] == latest
    assert final["base_execution_id"] == parent["id"]
    assert final["input_revision"] == parent["input_revision"] + 1
    assert runtime.generation_count == 2
    recipe = client.recipe_get(final["recipe_id"])
    assert final["name"] == recipe["name"] == "Format numbers"
    assert recipe["selected_version"]["content"]["name"] == final["name"]
    assert len(final["test_results"]) == 1
    assert final["result"]["published"]


def test_lease_fencing_and_cancellation(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"])
    first = client.store.claim("old", 10, 2)
    with client.store.transaction() as conn:
        conn.execute(
            "UPDATE executions SET lease_until=? WHERE id=?",
            (time.time() - 1, execution["id"]),
        )
    second = client.store.claim("new", 10, 2)
    assert second[1] > first[1]
    with pytest.raises(LeaseLost):
        client.store.progress(execution["id"], "old", first[1], 1, stage="commit")
    client.execution_cancel(execution["id"])
    with pytest.raises(LeaseLost):
        client.store.progress(execution["id"], "new", second[1], 1, stage="commit")
    assert client.execution_get(execution["id"])["status"] == "cancelled"


def test_atomic_claim_shared_across_independent_clients(client, tmp_path):
    material_id = material(client, tmp_path)["id"]
    for _ in range(8):
        run(client, material_id)

    def claim(index):
        peer = RSIClient(data_dir=client.data_dir, auto_start=False)
        return peer.store.claim(f"session-{index}", 30, 2)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = [c for c in pool.map(claim, range(8)) if c]
    assert len(claims) == 2
    assert len({c[0] for c in claims}) == 2
    different = RSIClient(
        data_dir=client.data_dir, config=settings(max_executions=3), auto_start=False
    )
    execution = run(different, material_id)
    assert execution["runtime_config"]["max_executions"] == 3
    assert client.execution_get(claims[0][0])["runtime_config"]["max_executions"] == 2


@pytest.mark.parametrize(
    "leaked_from",
    [None, "recipe", "rubric", "filename_in_recipe", "filename_in_rubric"],
)
def test_recipe_test_uses_existing_execution(client, tmp_path, leaked_from):
    recipe = existing_versioned_object(
        client,
        "recipe",
        name="Existing",
        content={
            "name": "Write",
            "description": "test",
            "instructions": {
                "recipe": "copy A-12",
                "filename_in_recipe": "read history.csv",
            }.get(leaked_from, "write 42"),
        },
    )
    rubric = existing_versioned_object(
        client,
        "rubrics",
        name="Fixed",
        content={
            "criteria": [
                {
                    "id": "fixed-standard",
                    "description": {
                        "rubric": "replay A-12",
                        "filename_in_rubric": "compare with history.csv",
                    }.get(leaked_from, "42"),
                    "source": "agent_inference",
                }
            ]
        },
    )
    context = tmp_path / "context.md"
    context.write_text("Write 42 into answer.txt")
    history = tmp_path / "history.csv"
    history.write_text("order_id,qty\nA-12,7\n")
    source = client.material_create(
        manifest={
            "context_path": str(context),
            "files": [{"path": str(history), "role": "reference"}],
        }
    )
    execution = run(client, source["id"])
    args = {
        "execution_id": execution["id"],
        "recipe_id": recipe["id"],
        "version_id": recipe["latest_version_id"],
        "rubrics_version_id": rubric["latest_version_id"],
    }
    client.recipe_test(**args)
    revision = client.execution_get(execution["id"])["input_revision"]
    client.recipe_test(**args)
    assert client.execution_get(execution["id"])["input_revision"] == revision

    class FixedRubricRuntime(FileRuntime):
        def extract_feedback(self, files, trace_path):
            pytest.fail("An explicitly selected rubric must be reused")

    runtime = FixedRubricRuntime()
    client.run_pending(runtime=runtime)
    final = client.execution_get(execution["id"])
    if leaked_from:
        assert final["status"] == "failed"
        assert final["error"]["code"] == "invalid_model_output"
        assert runtime.test_catalogs == []
        return
    assert final["recipe_version_id"] == recipe["latest_version_id"]
    assert (
        client.store.version_by_id(final["rubrics_version_id"], "rubrics")["content"][
            "criteria"
        ][0]["id"]
        == "fixed-standard"
    )
    assert runtime.generation_count == 0
    assert len(client.execution_list()) == 1


def test_failed_run_redacts_provider_errors_and_terminal_updates(client, tmp_path):
    m = material(client, tmp_path)
    execution = run(client, m["id"])
    client.run_pending(runtime=FileRuntime(fail=True))
    final = client.execution_get(execution["id"])
    assert final["status"] == "failed"
    assert "secret-upstream" not in json.dumps(final)
    with pytest.raises(InvalidState):
        client.execution_update(execution["id"], material_ids=[m["id"]])
    child = client.execution_create(
        material_ids=[m["id"]], policy=policy(), base_execution_id=execution["id"]
    )
    assert child["base_execution_id"] == execution["id"]


def test_execution_idempotency_and_unapproved_generation(client, tmp_path):
    m = material(client, tmp_path)
    args = {
        "material_ids": [m["id"]],
        "policy": policy(),
        "idempotency_key": "stop-hook-session-1-turn-1",
    }
    first = client.execution_create(**args)
    assert client.execution_create(**args)["id"] == first["id"]
    with pytest.raises(Conflict):
        client.execution_create(**{**args, "policy": policy(max_rounds=3)})
    with pytest.raises(ValueError):
        client.execution_create(
            material_ids=[m["id"]],
            policy={"artifact_confirmed": False, "confirmation_ref": "no"},
        )


def test_learning_appends_an_immutable_recipe_version(client, tmp_path):
    source = material(client, tmp_path)
    first = run(client, source["id"])
    client.run_pending(runtime=FileRuntime())
    first = client.execution_get(first["id"])
    recipe_id = first["recipe_id"]
    first_version = client.recipe_get(recipe_id, version_selector="latest")[
        "selected_version"
    ]
    assert first_version["number"] == 1

    revision = client.execution_create(
        material_ids=[source["id"]],
        policy=policy(),
        base_recipe_version_id=first_version["id"],
    )
    client.run_pending(runtime=FileRuntime())
    revised = client.execution_get(revision["id"])
    assert revised["recipe_id"] == recipe_id
    retained = client.recipe_get(recipe_id, version_id=first_version["id"])[
        "selected_version"
    ]
    latest = client.recipe_get(recipe_id, version_selector="latest")["selected_version"]
    assert retained == {**first_version, "archived": True}
    assert latest["number"] == 2
    assert revised["recipe_version_id"] == latest["id"]
    assert client.recipe_get(recipe_id)["selected_version"] == latest

    rubrics = client.rubrics_list()
    assert len(rubrics) == 2
    for rubric in rubrics:
        version = client.rubrics_get(rubric["id"])["selected_version"]
        assert version["object_id"] == rubric["id"]
        assert version["id"] == rubric["latest_version_id"]
        assert version["content"]["criteria"]


def test_crash_reclaims_expired_run_and_resumes_checkpoint(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"])

    class CrashingRuntime(FileRuntime):
        def analyze(self, *args):
            return {**super().analyze(*args), "recipe_name": "Write an answer file"}

        def execute(self, *args):
            super().execute(*args)
            # Simulates process exit, not a catchable model/runtime failure.
            raise SystemExit("worker crashed after an external side effect")

    with pytest.raises(SystemExit):
        client.run_pending(runtime=CrashingRuntime())
    before = client.execution_get(execution["id"])
    assert before["status"] == "running"
    assert before["progress_percent"] == 40
    with client.store.transaction() as conn:
        conn.execute(
            "UPDATE executions SET lease_until=? WHERE id=?",
            (time.time() - 1, execution["id"]),
        )
    recovered_runtime = FileRuntime()
    client.run_pending(runtime=recovered_runtime)
    recovered = client.execution_get(execution["id"])
    assert recovered["status"] == "completed", recovered
    assert recovered_runtime.generation_count == 0
    assert recovered["progress_percent"] == 100
    assert (
        recovered["name"]
        == before["name"]
        == client.recipe_list()[0]["name"]
        == "Write an answer file"
    )
    latest = client.recipe_get(recovered["recipe_id"], version_selector="latest")[
        "selected_version"
    ]
    assert latest["number"] == 1
    assert len(client.rubrics_list()) == 1
    assert (
        len(
            list(
                (client.data_dir / "executions" / execution["id"]).glob(
                    "checkpoints-*.sqlite3"
                )
            )
        )
        == 2
    )


def test_concurrent_revisions_cannot_overwrite_active_pointer(client, tmp_path):
    recipe = existing_versioned_object(
        client,
        "recipe",
        name="Base",
        content={"name": "Base", "description": "task", "instructions": "Original"},
    )
    m = material(client, tmp_path)
    runs = [
        client.execution_create(
            material_ids=[m["id"]],
            policy=policy(),
            base_recipe_version_id=recipe["latest_version_id"],
        )
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)

    class ConcurrentRuntime(FileRuntime):
        def execute(self, *args):
            result = super().execute(*args)
            barrier.wait(timeout=10)  # both candidates exist before either commits
            return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(client.run_pending, runtime=ConcurrentRuntime())
            for _ in range(2)
        ]
        for future in futures:
            future.result(timeout=20)
    final = [client.execution_get(r["id"]) for r in runs]
    assert all(r["status"] == "completed" for r in final), final
    latest = client.recipe_get(recipe["id"], version_selector="latest")[
        "selected_version"
    ]
    assert latest["number"] == 3
    assert client.recipe_get(recipe["id"])["selected_version"]["id"] == latest["id"]


def test_input_and_capability_boundaries(client, tmp_path):
    with pytest.raises(ValueError):
        client.material_create(manifest={"context_path": str(tmp_path)})
    m = material(client, tmp_path)
    with pytest.raises(ValueError):
        run(
            client,
            m["id"],
            test_task={
                "instruction": "escape",
                "checks": [{"kind": "exists", "path": "../host.txt"}],
            },
        )
    bad = {
        "model": {"model": "local", "base_url": "http://secret:password@localhost/v1"}
    }
    with pytest.raises(ValueError):
        RSIClient(data_dir=tmp_path / "other", config=bad)


def test_clarification_retry_repairs_dispatch_without_duplicate_child(
    client, tmp_path, monkeypatch
):
    execution = run(client, material(client, tmp_path)["id"])
    client.run_pending(runtime=FileRuntime(questions=True))
    client.auto_start = True
    calls = []

    def dispatch():
        calls.append(True)
        if len(calls) == 1:
            raise OSError("temporary process launch failure")
        return {"started": 1}

    monkeypatch.setattr(client, "recover", dispatch)
    args = {
        "question_id": "format",
        "question_revision": 1,
        "selected_option_ids": ["decimal"],
        "idempotency_key": "callback-retry",
    }
    with pytest.raises(OSError):
        client.execution_clarification_answer(execution["id"], **args)
    result = client.execution_clarification_answer(execution["id"], **args)
    assert result["continuation_execution_id"]
    assert len(calls) == 2
    assert len(client.execution_list()) == 2


def test_model_payment_error_is_actionable_without_persisting_body(client, tmp_path):
    from cosens.rsi.chat_completions import ModelStatusError

    class UnfundedRuntime(FileRuntime):
        def generate(self, *args):
            raise ModelStatusError(402) from RuntimeError("secret-provider-body")

    execution = run(client, material(client, tmp_path)["id"])
    client.run_pending(runtime=UnfundedRuntime())
    result = client.execution_get(execution["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_payment_required"
    assert result["error"]["upstream_status"] == 402
    assert "secret-provider-body" not in json.dumps(result)


def test_recipe_list_includes_learning_and_revision_status(client, tmp_path):
    for method in (
        "recipe_create",
        "recipe_version_create",
        "rubrics_create",
        "rubrics_version_create",
        "rubrics_version_list",
    ):
        assert not hasattr(client, method)
    source = material(client, tmp_path)
    learning = run(client, source["id"])
    [row] = client.recipe_list()
    assert (row["kind"], row["recipe_id"], row["status"]) == (
        "learning",
        None,
        "queued",
    )
    assert row["execution_id"] == learning["id"]
    assert row["name"] == "首次学习"
    assert client.recipe_list(q=source["name"]) == []
    assert client.recipe_list(active_only=True) == []
    with client.store.connect() as conn:
        assert conn.execute("SELECT id FROM recipe_objects").fetchall() == []
        assert tuple(conn.execute("SELECT * FROM recipes").fetchone()) == (learning["id"], None, None, None)

    client.run_pending(runtime=FileRuntime())
    [row] = client.recipe_list()
    assert (row["kind"], row["status"]) == ("recipe", "passed")
    with client.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rubrics_versions").fetchone()[0] == 1
    assert row["active_version_number"] == 1
    assert row["execution_id"] == learning["id"]
    assert "runtime_config" not in row

    revision = client.execution_create(
        material_ids=[source["id"]],
        policy=policy(),
        base_recipe_version_id=row["active_version_id"],
    )
    [working] = client.recipe_list(active_only=True, q="answer", limit=1)
    assert working["recipe_id"] == row["recipe_id"]
    assert working["active_version_id"] == row["active_version_id"]
    assert working["execution_id"] == revision["id"]
    assert working["status"] == "queued"
    assert working["candidate_version_id"] is None
    assert client.recipe_list(offset=1) == []

    client.execution_cancel(revision["id"])
    [cancelled] = client.recipe_list()
    assert cancelled["status"] == "cancelled"
    assert cancelled["active_version_id"] == row["active_version_id"]
    client.execution_update(revision["id"], archived=True)
    [restored] = client.recipe_list()
    assert restored["status"] == "passed"


def test_recipe_list_uses_latest_created_execution_for_status(client, tmp_path):
    from cosens.rsi import RSIClient as PublicClient

    source = material(client, tmp_path)
    learning = run(client, source["id"])
    client.run_pending(runtime=FileRuntime())
    first = client.execution_get(learning["id"])
    recipe_id, active_id = first["recipe_id"], first["recipe_version_id"]
    older, newer = [
        client.execution_create(
            material_ids=[source["id"]], policy=policy(), base_recipe_version_id=active_id
        )
        for _ in range(2)
    ]
    client.execution_cancel(newer["id"])
    with client.store.transaction() as conn:
        older = client.store.get_in(conn, older["id"], "execution")
        client.store.save(conn, {**older, "status": "running", "stage": "test"})

    host = PublicClient(data_dir=client.data_dir)
    [row] = host.recipe_list(query={"recipe_ids": [recipe_id]})
    assert row["execution_ids"] == [newer["id"], older["id"], learning["id"]]
    assert "status" not in row
    assert row["active_version_id"] == active_id

    client.execution_update(newer["id"], archived=True)
    [row] = host.recipe_list(query={"recipe_ids": [recipe_id]})
    assert row["execution_ids"] == [older["id"], learning["id"]]
    assert "status" not in row


def test_public_required_inputs_preserve_test_and_revision(tmp_path):
    from cosens.rsi import RSIClient as PublicClient

    client = PublicClient(
        data_dir=tmp_path / "sdk",
        model_id="local-test",
        api_key="fixture-key",
        base_url="http://127.0.0.1:1/v1",
        config={"auto_start": False},
    )
    context = tmp_path / "context.md"
    context.write_text("Write the answer to answer.txt")
    source = tmp_path / "input.txt"
    source.write_text("42")
    reference = tmp_path / "reference.md"
    reference.write_text("Use plain text")
    original = tmp_path / "confirmed.txt"
    original.write_text("42")
    manifest = {
        "context_path": str(context),
        "files": [
            {"path": str(source), "role": "input"},
            {"path": str(reference), "role": "reference"},
            {"path": str(original), "role": "artifact"},
        ],
    }
    assert not hasattr(client, "run_pending")
    assert not hasattr(client, "wait")
    assert not any(
        hasattr(client, method)
        for method in (
            "material_create",
            "material_get",
            "material_list",
            "material_update",
        )
    )
    payload = {
        "evidence": manifest,
        "idempotency_key": "delivery",
    }
    queue = RSIClient(data_dir=client.data_dir, config=settings(), auto_start=False)
    with pytest.raises(ValueError, match="learning-only role"):
        client.execution_create(
            **{
                **payload,
                "evidence": {
                    **manifest,
                    "files": manifest["files"]
                    + [{"path": str(original), "role": "input"}],
                },
            }
        )
    assert queue.material_list() == []
    assert list((client.data_dir / "materials").iterdir()) == []
    created = client.execution_create(**payload)
    assert "material_ids" not in created
    assert created["progress_percent"] == 0
    assert created["recipe_id"]
    assert created["recipe_version_id"] is None
    draft = client.recipe_get(created["recipe_id"], version="latest")
    assert draft["selected_version"] is None
    assert client.execution_create(**payload)["recipe_id"] == created["recipe_id"]
    assert len(queue.material_list()) == 1
    assert client.recipe_list() == [
        {
            "recipe_id": created["recipe_id"],
            "execution_ids": [created["id"]],
            "name": "首次学习",
            "active_version_id": None,
            "created_at": draft["created_at"],
            "updated_at": draft["updated_at"],
        }
    ]
    assert (
        client.recipe_list(
            query={
                "recipe_ids": [
                    created["recipe_id"],
                    "recipe_missing",
                    created["recipe_id"],
                ]
            }
        )
        == client.recipe_list()
    )
    assert client.recipe_list(query={"recipe_ids": []}) == []
    assert client.recipe_list(query={"recipe_ids": ["recipe_missing"]}) == []
    assert not hasattr(client, "recipe_search")
    files = {f["role"]: f["locator"] for f in created["evidence"]}
    stored = queue.execution_get(created["id"])
    assert stored["policy"]["confirmation_ref"] == payload["idempotency_key"]
    assert "policy" not in created
    assert "snapshot" not in created["evidence"][0]
    task = stored["policy"]["test_task"]
    assert task["input_locators"] == [files["input"]]
    assert task["replay_current_case"]
    assert task["shared_reference_locators"] is None
    assert task["checks"] == []
    feedback = tmp_path / "feedback.md"
    feedback.write_text("Keep the answer concise.")
    extra = {"context_path": str(feedback), "files": []}
    assert not hasattr(client, "execution_update")
    assert not hasattr(client, "rubrics_update")
    assert not hasattr(client, "recipe_update")
    assert not hasattr(client, "recipe_version_list")
    updated = client.execution_append_evidence(created["id"], evidence=extra)
    assert client.execution_append_evidence(created["id"], evidence=extra) == updated
    assert updated["input_revision"] == 2
    assert len(updated["evidence"]) == len(created["evidence"]) + 1
    assert queue.execution_get(updated["id"])["policy"]["test_task"] == task
    runtime = FileRuntime()
    queue.run_pending(runtime=runtime)
    assert [[f["locator"] for f in catalog] for catalog in runtime.test_catalogs] == [
        [files["input"]]
    ]
    finished = client.execution_get(created["id"])
    assert finished["recipe_id"] == created["recipe_id"]
    assert finished["result"]["test_status"] == "passed"
    assert finished["progress_percent"] == 100
    final_zip = client.data_dir / "task-packages" / f"{created['id']}.zip"
    assert final_zip.is_file()
    assert client.task_get(created["id"])["artifact"]["zip_path"] == str(final_zip)
    assert (
        client.execution_list(query={"execution_ids": [created["id"]]})[0][
            "progress_percent"
        ]
        == 100
    )
    with pytest.raises(InvalidState, match="terminal inputs are immutable"):
        client.execution_append_evidence(created["id"], evidence=extra)
    recipe = client.recipe_get(finished["recipe_id"], version="latest")
    assert any(row["recipe_id"] == recipe["id"] for row in client.recipe_list())
    assert (
        client.recipe_get(recipe["id"], version=recipe["latest_version_id"])[
            "selected_version"
        ]
        == recipe["selected_version"]
    )
    [available] = [row for row in client.recipe_list() if row["active_version_id"]]
    assert available == {
        "recipe_id": recipe["id"],
        "execution_ids": [finished["id"]],
        "name": recipe["name"],
        "active_version_id": recipe["latest_version_id"],
        "created_at": recipe["created_at"],
        "updated_at": recipe["updated_at"],
    }
    assert (
        client.recipe_get(
            available["recipe_id"], version=available["active_version_id"]
        )["selected_version"]
        == recipe["selected_version"]
    )

    recipe_path = tmp_path / "prior-recipe.md"
    recipe_text = "Write the response as a plain text file."
    recipe_path.write_text(recipe_text)
    recipe_reference = {
        "path": str(recipe_path),
        "recipe_id": recipe["id"],
    }
    submission = {
        "evidence": manifest,
        "idempotency_key": "same-delivery-revision",
        "recipe_reference": recipe_reference,
    }
    with pytest.raises(NotFound, match="recipe not found"):
        client.execution_create(
            **{
                **submission,
                "recipe_reference": {**recipe_reference, "recipe_id": finished["id"]},
            }
        )
    with pytest.raises(ValueError, match="recipe"):
        client.execution_create(
            **{
                **submission,
                "evidence": {
                    **manifest,
                    "files": [{"path": str(recipe_path), "role": "recipe"}],
                },
            }
        )
    with queue.store.transaction() as conn:
        stored_recipe = queue.store.get_in(conn, recipe["id"], "recipe")
        candidate = queue.store.append_version(
            conn, stored_recipe, recipe["selected_version"]["content"]
        )
    assert (
        client.recipe_get(recipe["id"], version="latest")["selected_version"]["id"]
        == candidate["id"]
    )

    revised = client.execution_create(**submission)
    assert revised["recipe_id"] == recipe["id"]
    assert revised["recipe_version_id"] is None
    assert any(file["role"] == "recipe" for file in revised["evidence"])
    recipe_path.write_text("Modified after submission.")
    detail = client.recipe_get(recipe["id"], version="latest")
    assert client.recipe_list() == [
        {
            "recipe_id": recipe["id"],
            "execution_ids": [revised["id"], finished["id"]],
            "name": recipe["name"],
            "active_version_id": finished["recipe_version_id"],
            "created_at": detail["created_at"],
            "updated_at": detail["updated_at"],
        }
    ]
    assert revised["base_execution_id"] is None
    assert queue.execution_get(revised["id"]).get("base_recipe_version_id") is None

    class RevisionRuntime(FileRuntime):
        def generate(self, context, files, trace_path):
            assert context["base_recipe"] == recipe_text
            return super().generate(context, files, trace_path)

        def execute(self, context, files, workspace, trace_path):
            assert not any(file["role"] == "recipe" for file in files)
            assert recipe_text not in json.dumps(context)
            return super().execute(context, files, workspace, trace_path)

        def assess(self, context, files, trace_path):
            assert not any(file["role"] == "recipe" for file in files)
            assert recipe_text not in json.dumps(context)
            return super().assess(context, files, trace_path)

    queue.run_pending(runtime=RevisionRuntime())
    revision_result = client.execution_get(revised["id"])
    assert revision_result["result"]["published"]
    assert revision_result["recipe_id"] == recipe["id"]
    assert revision_result["recipe_version_id"] != finished["recipe_version_id"]
    queue.execution_update(finished["id"], archived=True)
    assert [row["id"] for row in client.execution_list()] == [revised["id"]]
    selected = client.execution_list(
        query={
            "execution_ids": [
                revised["id"],
                finished["id"],
                "execution_missing",
                finished["id"],
            ]
        }
    )
    assert [row["id"] for row in selected] == [finished["id"], revised["id"]]
    assert client.execution_list(query={"execution_ids": []}) == []
    assert client.recipe_list()[0]["execution_ids"] == [revised["id"]]

    client.recipe_version_set_archived(
        recipe["id"], version_id=revision_result["recipe_version_id"], archived=True
    )
    with pytest.raises(InvalidState, match="base Recipe has no available version"):
        client.execution_create(**{**submission, "idempotency_key": "unavailable"})
    assert client.execution_create(**submission)["id"] == revised["id"]
    assert queue.execution_get(revised["id"]).get("base_recipe_version_id") is None

    context.unlink()
    replayed = client.execution_create(**payload)
    assert replayed["id"] == created["id"]
    assert replayed["evidence"][0]["sha256"] == created["evidence"][0]["sha256"]

    legacy_material = queue.material_create(manifest={"context_path": str(feedback)})
    legacy = queue.execution_create(
        material_ids=[legacy_material["id"]], policy=policy(), idempotency_key="legacy"
    )
    with pytest.raises(Conflict):
        client.execution_create(**{**payload, "idempotency_key": "legacy"})
    assert [
        file["locator"] for file in client.execution_get(legacy["id"])["evidence"]
    ] == [file["locator"] for file in legacy_material["files"]]
    with queue.store.transaction() as conn:
        another = queue.store.create_recipe(conn, name="Unrelated Recipe")
    all_entries = client.recipe_list()
    assert any(row["recipe_id"] is None for row in all_entries)
    selected_ids = {created["recipe_id"], another["id"]}
    expected = [row for row in all_entries if row["recipe_id"] in selected_ids]
    recipe_ids = [row["recipe_id"] for row in reversed(expected)]
    assert client.recipe_list(query={"recipe_ids": recipe_ids}) == expected


def test_opt_in_recipe_package_resources_are_staged_and_kept_out_of_artifacts(client, tmp_path):
    from cosens.rsi.engine import Engine
    from cosens.rsi.worker import drain

    resource = {"path": "templates/answer.txt", "content": "42"}
    publication = []

    class PackageEngine(Engine):
        package_recipes = True

        def prepare_test_workspace(self, recipe, workspace, state):
            assert state["version_id"] == recipe["id"]
            for item in recipe["content"]["resources"]:
                path = workspace / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item["content"], encoding="utf-8")

        def commit(self, state):
            def on_published(conn):
                publication.append(conn.execute(
                    "SELECT status FROM versions WHERE id=?", (state["version_id"],)
                ).fetchone()[0])

            self.store.complete(
                self.execution_id, self.owner, self.fence, state, on_published=on_published
            )
            return {}

    class PackageRuntime(FileRuntime):
        def generate(self, context, files, trace_path):
            assert context["package_recipes"] is True
            return {**super().generate(context, files, trace_path), "resources": [resource]}

        def execute(self, context, files, workspace, trace_path):
            assert context["package_files"] == [resource["path"]]
            assert (workspace / resource["path"]).read_text(encoding="utf-8") == "42"
            return super().execute(context, files, workspace, trace_path)

    execution = run(client, material(client, tmp_path)["id"])
    drain(client, runtime=PackageRuntime(), engine_class=PackageEngine)

    completed = client.execution_get(execution["id"])
    assert completed["result"]["published"]
    assert publication == ["available"]
    assert client.store.version(
        completed["recipe_id"], completed["recipe_version_id"]
    )["content"]["resources"] == [resource]
    assert [Path(item["path"]).name for item in completed["test_results"][0]["artifacts"]] == ["answer.txt"]


def test_package_recipe_resource_schema_is_opt_in():
    from cosens.rsi.models import BundledRecipeContent
    from cosens.rsi.runtime import ChatRuntime, parse_structured

    runtime = ChatRuntime(None, None, None)
    runtime._call = lambda **options: options
    assert runtime.generate({}, [], None)["schema"] is RecipeContent
    packaged = runtime.generate({"package_recipes": True}, [], None)
    assert packaged["schema"] is BundledRecipeContent
    assert "resources" in packaged["instruction"]
    content = {"name": "Name", "description": "Description", "instructions": "Instruction"}
    assert BundledRecipeContent.model_validate(content).model_dump(exclude_defaults=True) == content
    resource = {"path": "templates/data.json", "content": '{"value": 42}'}
    parsed, error = parse_structured(
        BundledRecipeContent, json.dumps({**content, "resources": json.dumps([resource])})
    )
    assert error is None
    assert parsed.resources[0].model_dump() == resource
    with pytest.raises(ValidationError, match="relative file"):
        BundledRecipeContent.model_validate({
            **content, "resources": [{"path": "scripts/../escape.py", "content": ""}]
        })


def test_python_restore_rejects_overwritten_package_files(tmp_path):
    import base64

    from cosens.rsi.models import LocalConfig
    from cosens.rsi.python_runner import _restore_output

    workspace = tmp_path / "workspace"
    template = workspace / "templates" / "answer.txt"
    template.parent.mkdir(parents=True)
    template.write_text("42", encoding="utf-8")

    with pytest.raises(ValueError, match="changed a Recipe package file"):
        _restore_output(
            workspace,
            {"templates/answer.txt": base64.b64encode(b"0").decode()},
            LocalConfig.model_validate(settings()),
            protected_files={"templates/answer.txt"},
        )

    assert template.read_text(encoding="utf-8") == "42"
