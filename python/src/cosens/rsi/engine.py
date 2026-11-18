"""Shared learning core. LangGraph checkpoints use Execution IDs as thread IDs."""

from __future__ import annotations

from typing import TypedDict

from .materials import atomic_json, authorized_test_files, digest
from .models import Analysis, Assessment, InputChanged, ModelOutputError, RecipeContent
from .store import uid


class State(TypedDict, total=False):
    execution_id: str
    input_revision: int
    round: int
    analysis: dict
    recipe_id: str
    version_id: str
    rubrics_version_id: str
    test: dict
    activation_revision: int


class Engine:
    def __init__(self, client, runtime, owner, fence, execution_id):
        self.client, self.store, self.runtime = client, client.store, runtime
        self.owner, self.fence, self.execution_id = owner, fence, execution_id

    def guard(self, revision=None):
        with self.store.transaction() as conn:
            return self.store.guard(
                conn, self.execution_id, self.owner, self.fence, revision
            )

    def begin(self, state, stage):
        return self.store.progress(
            self.execution_id,
            self.owner,
            self.fence,
            state["input_revision"],
            stage=stage,
        )

    def files(self, execution, *, test=False):
        files = [
            f
            for material_id in execution["material_ids"]
            for f in self.client.material_get(material_id)["files"]
        ]
        task = execution["policy"].get("test_task")
        if test:
            return authorized_test_files(files, task)
        held_out = set((task or {}).get("input_locators") or [])
        return [file for file in files if file["locator"] not in held_out]

    def directory(self, state):
        directory = (
            self.client.data_dir
            / "executions"
            / self.execution_id
            / f"input-{state['input_revision']}-lease-{self.fence}"
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory

    def context(self, execution):
        return {
            "answers": execution.get("answers"),
            "policy": {
                k: v for k, v in execution["policy"].items() if k != "test_task"
            },
            "source_session_id": execution.get("source_session_id"),
        }

    def analyze(self, state):
        execution = self.begin(state, "analyze")
        if execution.get("analyzed_input_revision") == state["input_revision"]:
            return {
                "analysis": execution["analysis"],
                "rubrics_version_id": execution["resolved_rubrics_version_id"],
            }
        context = self.context(execution)
        fixed_rubrics_id = (execution.get("test_request") or {}).get(
            "rubrics_version_id"
        ) or execution.get("rubrics_version_id")
        if fixed_rubrics_id:
            context["fixed_rubrics"] = self.store.version_by_id(
                fixed_rubrics_id, "rubrics"
            )["content"]
        files = self.files(execution)
        analysis = Analysis.model_validate(
            self.runtime.analyze(
                context, files, self.directory(state) / "analysis.json"
            )
        ).model_dump()
        locators = {file["locator"] for file in files}
        for cause in (analysis["attribution"] or {}).get("possible_causes", []):
            if any(ref["locator"] not in locators for ref in cause["evidence_refs"]):
                raise ModelOutputError(
                    "Attribution contains unauthorized evidence references"
                )
        for criterion in (
            analysis["rubrics"]["criteria"] if not fixed_rubrics_id else []
        ):
            if criterion["source"] != "agent_inference" and not criterion["evidence"]:
                raise ValueError(
                    "evidence-based rubric criterion requires source references"
                )
            for evidence in criterion["evidence"]:
                if evidence["locator"] not in locators or not evidence[
                    "locator"
                ].startswith(evidence["material_id"] + "/"):
                    raise ValueError(
                        "rubric evidence reference is outside the Material catalog"
                    )
        if fixed_rubrics_id:
            analysis["rubrics"] = context["fixed_rubrics"]
        for question in analysis["questions"]:
            question["revision"] = state["input_revision"]
        rubrics_version_id = self.store.save_analysis(
            self.execution_id, self.owner, self.fence, state["input_revision"], analysis
        )
        return {"analysis": analysis, "rubrics_version_id": rubrics_version_id}

    def generate(self, state):
        execution = self.begin(state, "generate")
        round_number = state.get("round", 0) + 1
        context = {
            **self.context(execution),
            "analysis": state["analysis"],
            "previous_test": state.get("test"),
        }
        base_id = state.get("version_id") or execution.get("base_recipe_version_id")
        if base_id:
            context["base_recipe"] = self.store.version_by_id(base_id, "recipe")[
                "content"
            ]
        request = execution.get("test_request")
        if request:
            recipe = self.store.get(request["recipe_id"], "recipe")
            return {
                "recipe_id": recipe["id"],
                "version_id": request["version_id"],
                "round": round_number,
                "activation_revision": recipe["revision"],
            }
        key = f"recipe:{self.execution_id}:{state['input_revision']}:{round_number}"
        version = self.store.version_by_operation(key)
        if version:
            return {
                "recipe_id": version["object_id"],
                "version_id": version["id"],
                "round": round_number,
                "activation_revision": version["provenance"]["activation_revision"],
            }
        content = RecipeContent.model_validate(
            self.runtime.generate(
                context,
                self.files(execution),
                self.directory(state) / f"generate-{round_number}.json",
            )
        ).model_dump()
        version = self.store.publish_candidate(
            self.execution_id,
            self.owner,
            self.fence,
            state["input_revision"],
            content=content,
            recipe_id=state.get("recipe_id"),
            base_id=base_id,
            operation_key=key,
        )
        return {
            "recipe_id": version["object_id"],
            "version_id": version["id"],
            "round": round_number,
            "activation_revision": version["provenance"]["activation_revision"],
        }

    def test(self, state):
        execution = self.begin(state, "test")
        # Test IDs are deterministic for graph-node replay; each attempt gets a fresh workspace.
        test_id = f"test:{self.execution_id}:{state['input_revision']}:{state['round']}:{state['version_id']}"
        previous = next(
            (t for t in execution["test_results"] if t["id"] == test_id), None
        )
        if previous:
            return {"test": previous}
        recipe = self.store.version(state["recipe_id"], state["version_id"])
        rubric = self.store.version_by_id(state["rubrics_version_id"], "rubrics")
        task = execution["policy"].get("test_task")
        result = {
            "id": test_id,
            "execution_id": self.execution_id,
            "recipe_id": state["recipe_id"],
            "version_id": state["version_id"],
            "rubrics_version_id": rubric["id"],
            "input_revision": state["input_revision"],
            "round": state["round"],
            "status": "unverifiable",
            "scores": [],
            "score": None,
            "checks": [],
            "artifacts": [],
        }
        if task is None:
            result["reason"] = (
                "Host did not supply a reproducible test task; Recipe is not validated"
            )
        else:
            test_files = self.files(execution, test=True)
            attempt = self.directory(state) / ("test-" + uid("attempt"))
            workspace = attempt / "outputs"
            workspace.mkdir(parents=True, mode=0o700)
            atomic_json(
                attempt / "test-input.json",
                {
                    "test_task": task,
                    "recipe": recipe,
                    "rubrics_version_id": rubric["id"],
                },
            )
            executor_result = self.runtime.execute(
                {
                    "test_task": {"instruction": task["instruction"]},
                    "recipe": recipe["content"],
                },
                test_files,
                workspace,
                attempt / "executor-trace.json",
            )
            atomic_json(attempt / "executor-result.json", executor_result)
            self.guard(state["input_revision"])
            artifacts = []
            for path in sorted(workspace.rglob("*")):
                if path.is_file():
                    if path.is_symlink() or not path.resolve().is_relative_to(
                        workspace.resolve()
                    ):
                        raise ValueError("test artifact escaped workspace")
                    data = path.read_bytes()
                    if len(data) > self.client.config.max_file_bytes:
                        raise ValueError("test artifact exceeds size limit")
                    artifacts.append(
                        {
                            "path": str(path),
                            "snapshot": str(path),
                            "locator": "test/" + str(path.relative_to(workspace)),
                            "role": "artifact",
                            "description": "Actual Recipe test output",
                            "sha256": digest(data),
                            "size": len(data),
                        }
                    )
            result.update(
                workspace=str(workspace),
                artifacts=artifacts,
                trace_path=str(attempt / "executor-trace.json"),
            )
            if not artifacts:
                result["reason"] = "Recipe executor produced no verifiable output files"
            else:
                checks = self.objective_checks(workspace, task["checks"])
                evaluator_files = artifacts + test_files
                executor_trace = attempt / "executor-trace.json"
                if executor_trace.is_file():
                    trace_bytes = executor_trace.read_bytes()
                    evaluator_files.append(
                        {
                            "path": str(executor_trace),
                            "snapshot": str(executor_trace),
                            "locator": "test/executor-trace.json",
                            "role": "trace",
                            "description": "Actual executor tool calls and results",
                            "sha256": digest(trace_bytes),
                            "size": len(trace_bytes),
                        }
                    )
                self.begin(state, "evaluate")
                assessment = Assessment.model_validate(
                    self.runtime.assess(
                        {
                            "test_task": task,
                            "rubrics": rubric["content"],
                            "objective_checks": checks,
                            "recipe": recipe["content"],
                        },
                        evaluator_files,
                        attempt / "evaluator-trace.json",
                    )
                ).model_dump()
                criteria = rubric["content"]["criteria"]
                scores = {s["criterion_id"]: s for s in assessment["scores"]}
                if len(scores) != len(assessment["scores"]) or set(scores) != {
                    c["id"] for c in criteria
                }:
                    raise ValueError(
                        "evaluator must score each fixed criterion exactly once"
                    )
                score = sum(s["score"] for s in scores.values()) / len(scores)
                passed = (
                    score >= rubric["content"]["pass_threshold"]
                    and all(c["passed"] for c in checks)
                    and all(
                        scores[c["id"]]["score"] >= rubric["content"]["pass_threshold"]
                        for c in criteria
                        if c["required"]
                    )
                )
                result.update(
                    status=("passed" if passed else "failed")
                    if assessment["verifiable"]
                    else "unverifiable",
                    scores=assessment["scores"],
                    score=score,
                    checks=checks,
                    reason=assessment["reason"],
                    evaluator_trace_path=str(attempt / "evaluator-trace.json"),
                )
            atomic_json(attempt / "result.json", result)
        self.store.save_test(
            self.execution_id, self.owner, self.fence, state["input_revision"], result
        )
        return {"test": result}

    @staticmethod
    def objective_checks(workspace, checks):
        results = []
        for check in checks:
            if set(check) - {"kind", "path", "value"}:
                raise ValueError("unsupported objective check fields")
            path = (workspace / check["path"]).resolve()
            if (
                not path.is_relative_to(workspace.resolve())
                or path == workspace.resolve()
            ):
                raise ValueError("check path escaped workspace")
            kind = check["kind"]
            if kind == "exists":
                passed = path.is_file()
            elif kind in {"text_equals", "text_contains", "sha256"}:
                if not path.is_file():
                    passed = False
                else:
                    content = path.read_bytes()
                    actual = (
                        digest(content) if kind == "sha256" else content.decode("utf-8")
                    )
                    passed = (
                        check["value"] in actual
                        if kind == "text_contains"
                        else actual == check["value"]
                    )
            else:
                raise ValueError("unsupported objective check kind")
            results.append({**check, "passed": passed})
        return results

    def route(self, state):
        execution = self.guard(state["input_revision"])
        if (
            state["test"]["status"] == "failed"
            and state["round"] < execution["policy"]["max_rounds"]
            and not execution.get("test_request")
        ):
            return "generate"
        return "commit"

    def commit(self, state):
        self.store.complete(self.execution_id, self.owner, self.fence, state)
        return {}

    def run(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph
        from langsmith import tracing_context

        # Separate checkpoint DB avoids long checkpoint writes contending with queue leases.
        path = (
            self.client.data_dir
            / "executions"
            / self.execution_id
            / f"checkpoints-{self.fence}.sqlite3"
        )
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            # Each lease writes its own checkpoint DB. An expired worker finishing
            # a model call can never overwrite the replacement worker's state.
            previous = [
                p
                for p in path.parent.glob("checkpoints-*.sqlite3")
                if int(p.stem.split("-")[-1]) < self.fence
            ]
            if previous:
                import sqlite3

                latest = max(previous, key=lambda p: int(p.stem.split("-")[-1]))
                with sqlite3.connect(latest) as source, sqlite3.connect(path) as target:
                    source.backup(target)
        with SqliteSaver.from_conn_string(str(path)) as saver:
            builder = StateGraph(State)
            for name in ("analyze", "generate", "test", "commit"):
                builder.add_node(name, getattr(self, name))
            builder.add_edge(START, "analyze")
            builder.add_edge("analyze", "generate")
            builder.add_edge("generate", "test")
            builder.add_conditional_edges(
                "test", self.route, {"generate": "generate", "commit": "commit"}
            )
            builder.add_edge("commit", END)
            graph = builder.compile(checkpointer=saver)
            config = {
                "configurable": {"thread_id": self.execution_id},
                "recursion_limit": 100,
            }
            while True:
                execution = self.guard()
                revision = execution["input_revision"]
                snapshot = graph.get_state(config)
                start = (
                    None
                    if snapshot.values.get("input_revision") == revision
                    and snapshot.next
                    else {
                        "execution_id": self.execution_id,
                        "input_revision": revision,
                        "round": 0,
                        "analysis": {},
                        "recipe_id": execution.get("recipe_id"),
                        "version_id": None,
                        "test": None,
                    }
                )
                try:
                    # Do not inherit host-enabled remote LangSmith tracing.
                    with tracing_context(enabled=False):
                        graph.invoke(start, config)
                    return
                except InputChanged:
                    # An accepted answer or new Material arrived during an LLM/tool call.
                    # Supersede that revision; never commit or activate its stale output.
                    continue
