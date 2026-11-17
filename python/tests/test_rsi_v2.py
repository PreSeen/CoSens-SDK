"""Local RSI contracts, real graph, concurrency, clarification and recovery."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain")

from cosens.rsi import Conflict, InvalidState, RSIClient
from cosens.rsi.materials import atomic_json, read_snapshot
from cosens.rsi.models import LeaseLost


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
            "title": "Answer writer",
            "applicability": "Write a fixed task answer",
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
    execution = run(client, m["id"], auto_activate=True)
    runtime = FileRuntime()
    assert client.run_pending(runtime=runtime)["processed"] == 1
    result = client.execution_get(execution["id"])
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == "passed"
    assert result["result"]["activated"]
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


def test_recipe_is_inactive_without_activation_policy(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"])
    client.run_pending(runtime=FileRuntime())
    result = client.execution_get(execution["id"])
    recipe = client.recipe_get(result["recipe_id"])
    assert recipe["selected_version"] is None
    latest = client.recipe_get(recipe["id"], version_selector="latest")[
        "selected_version"
    ]
    activated = client.recipe_activate(
        recipe["id"], version_id=latest["id"], expected_revision=recipe["revision"]
    )
    assert activated["active_version_id"] == latest["id"]
    with pytest.raises(Conflict):
        client.recipe_activate(
            recipe["id"], version_id=None, expected_revision=recipe["revision"]
        )


@pytest.mark.parametrize(
    "changes,expected,rounds",
    [
        ({"test_task": None}, "unverifiable", 1),
        ({}, "failed", 2),
    ],
)
def test_missing_task_or_failed_objective_check_cannot_activate(
    client, tmp_path, changes, expected, rounds
):
    execution = run(
        client, material(client, tmp_path)["id"], auto_activate=True, **changes
    )
    runtime = FileRuntime(output="wrong")
    client.run_pending(runtime=runtime)
    result = client.execution_get(execution["id"])
    assert result["status"] == "completed", result
    assert result["result"]["test_status"] == expected
    assert len(result["test_results"]) == rounds
    assert not result["result"]["activated"]
    recipe = client.recipe_get(result["recipe_id"])
    with pytest.raises(InvalidState):
        client.recipe_activate(
            recipe["id"],
            version_id=result["recipe_version_id"],
            expected_revision=recipe["revision"],
        )


def test_late_clarification_creates_child_once(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"], auto_activate=True)
    client.run_pending(runtime=FileRuntime(questions=True))
    parent = client.execution_get(execution["id"])
    assert parent["result"]["provisional"]
    assert not parent["result"]["activated"]
    args = {
        "question_id": "format",
        "question_revision": 1,
        "selected_option_ids": ["decimal"],
        "idempotency_key": "answer-1",
    }
    answer = client.execution_clarification_answer(parent["id"], **args)
    assert client.execution_clarification_answer(parent["id"], **args) == answer
    child = client.execution_get(answer["continuation_execution_id"])
    assert child["parent_execution_id"] == parent["id"]
    assert child["base_recipe_version_id"] == parent["recipe_version_id"]
    assert child["answers"]["format:1"]["selected_options"][0]["label"] == "Decimal"
    client.run_pending(runtime=FileRuntime(questions=True))
    child = client.execution_get(child["id"])
    assert child["result"]["activated"]
    assert client.execution_get(parent["id"])["result"] == parent["result"]
    with pytest.raises(Conflict):
        client.execution_clarification_answer(
            parent["id"],
            **{**args, "selected_option_ids": ["hex"], "idempotency_key": "other"},
        )


def test_answer_during_generation_restarts_revision(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"], auto_activate=True)

    class AnsweringRuntime(FileRuntime):
        def generate(self, *args):
            if not self.generation_count:
                client.execution_clarification_answer(
                    execution["id"],
                    question_id="format",
                    question_revision=1,
                    selected_option_ids=["decimal"],
                )
            return super().generate(*args)

    runtime = AnsweringRuntime(questions=True)
    client.run_pending(runtime=runtime)
    final = client.execution_get(execution["id"])
    assert final["status"] == "completed", final
    assert final["input_revision"] == 2
    assert runtime.generation_count == 2
    assert all(t["input_revision"] == 2 for t in final["test_results"])
    assert final["result"]["activated"]


def test_lease_fencing_and_cancellation(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"])
    first = client.store.claim("old", 10, 2)
    with client.store.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?",
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
    with pytest.raises(Conflict):
        RSIClient(data_dir=client.data_dir, config=settings(max_executions=3))


def test_recipe_test_uses_existing_execution(client, tmp_path):
    recipe = client.recipe_create(
        name="Existing",
        content={"title": "Write", "applicability": "test", "instructions": "write 42"},
    )
    rubric = client.rubrics_create(
        name="Fixed",
        content={
            "criteria": [
                {
                    "id": "fixed-standard",
                    "description": "42",
                    "source": "agent_inference",
                }
            ]
        },
    )
    execution = run(client, material(client, tmp_path)["id"])
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
    runtime = FileRuntime()
    client.run_pending(runtime=runtime)
    final = client.execution_get(execution["id"])
    assert final["result"]["recipe_version_id"] == recipe["latest_version_id"]
    assert final["analysis"]["rubrics"]["criteria"][0]["id"] == "fixed-standard"
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
        material_ids=[m["id"]], policy=policy(), parent_execution_id=execution["id"]
    )
    assert child["parent_execution_id"] == execution["id"]


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
        client.execution_create(**{**args, "policy": policy(auto_activate=True)})
    with pytest.raises(ValueError):
        client.execution_create(
            material_ids=[m["id"]],
            policy={"artifact_confirmed": False, "confirmation_ref": "no"},
        )


def test_version_append_is_immutable_and_separate(client):
    recipe = client.recipe_create(name="A")
    content = {"title": "A", "applicability": "B", "instructions": "C"}
    first = client.recipe_version_create(
        recipe["id"], content=content, idempotency_key="v1"
    )
    assert (
        client.recipe_version_create(
            recipe["id"], content=content, idempotency_key="v1"
        )
        == first
    )
    second = client.recipe_version_create(
        recipe["id"], content={**content, "instructions": "D"}
    )
    assert first["number"] == 1 and second["number"] == 2
    assert (
        client.recipe_get(recipe["id"], version_id=first["id"])["selected_version"][
            "content"
        ]["instructions"]
        == "C"
    )
    assert len(client.recipe_version_list(recipe["id"])) == 2
    with pytest.raises(InvalidState):
        client.recipe_activate(
            recipe["id"], version_id=second["id"], expected_revision=3
        )


def test_crash_reclaims_expired_run_and_resumes_checkpoint(client, tmp_path):
    execution = run(client, material(client, tmp_path)["id"])

    class CrashingRuntime(FileRuntime):
        def execute(self, *args):
            super().execute(*args)
            # Simulates process exit, not a catchable model/runtime failure.
            raise SystemExit("worker crashed after an external side effect")

    with pytest.raises(SystemExit):
        client.run_pending(runtime=CrashingRuntime())
    before = client.execution_get(execution["id"])
    assert before["status"] == "running"
    with client.store.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?",
            (time.time() - 1, execution["id"]),
        )
    recovered_runtime = FileRuntime()
    client.run_pending(runtime=recovered_runtime)
    recovered = client.execution_get(execution["id"])
    assert recovered["status"] == "completed", recovered
    assert recovered_runtime.generation_count == 0
    assert len(client.recipe_version_list(recovered["recipe_id"])) == 1
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
    recipe = client.recipe_create(
        name="Base",
        content={"title": "Base", "applicability": "task", "instructions": "Original"},
    )
    m = material(client, tmp_path)
    runs = [
        client.execution_create(
            material_ids=[m["id"]],
            policy=policy(auto_activate=True),
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
    assert sum(r["result"]["activated"] for r in final) == 1
    assert sum(r["result"].get("activation_conflict", False) for r in final) == 1
    assert len(client.recipe_version_list(recipe["id"])) == 3


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
    import httpx
    from openai import APIStatusError

    class UnfundedRuntime(FileRuntime):
        def generate(self, *args):
            request = httpx.Request(
                "POST", "https://fixture.invalid/v1/chat/completions"
            )
            raise APIStatusError(
                "secret-provider-body",
                response=httpx.Response(402, request=request),
                body={"error": "secret-provider-body"},
            )

    execution = run(client, material(client, tmp_path)["id"])
    client.run_pending(runtime=UnfundedRuntime())
    result = client.execution_get(execution["id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "model_payment_required"
    assert result["error"]["upstream_status"] == 402
    assert "secret-provider-body" not in json.dumps(result)
