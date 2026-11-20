"""Shared learning core. LangGraph checkpoints use Execution IDs as thread IDs."""

from __future__ import annotations

from typing import TypedDict

from .materials import atomic_json, authorized_test_files, digest, read_snapshot
from .models import (
    Analysis,
    Assessment,
    FeedbackExtraction,
    InputChanged,
    ModelOutputError,
    RecipeContent,
)
from .portability import LearningLiterals, rubric_rule_text
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


class Engine:
    def __init__(self, client, runtime, owner, fence, execution_id):
        self.client, self.store, self.runtime = client, client.store, runtime
        self.owner, self.fence, self.execution_id = owner, fence, execution_id

    def guard(self, revision=None):
        with self.store.transaction() as conn:
            return self.store.guard(
                conn, self.execution_id, self.owner, self.fence, revision
            )

    def begin(self, state, stage, *, round_number=1):
        return self.store.progress(
            self.execution_id,
            self.owner,
            self.fence,
            state["input_revision"],
            stage=stage,
            round_number=round_number,
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
        if task and task.get("replay_current_case"):
            return files
        held_out = set((task or {}).get("input_locators") or [])
        return [file for file in files if file["locator"] not in held_out]

    def learning_literals(self, execution, *source_execution_ids):
        sources = {execution["id"]: execution}
        for source_id in source_execution_ids:
            if source_id and source_id not in sources:
                sources[source_id] = self.store.get(source_id, "execution")
        files = {}
        for source in sources.values():
            authorized = {f["locator"] for f in self.files(source, test=True)}
            replays_current_case = (source["policy"].get("test_task") or {}).get(
                "replay_current_case"
            )
            files.update(
                (f["locator"], f)
                for f in self.files(source)
                if replays_current_case or f["locator"] not in authorized
            )
        task = execution["policy"].get("test_task") or {}
        allowed_paths = [f["path"] for f in self.files(execution, test=True)]
        allowed_paths.extend(check["path"] for check in task.get("checks", []))
        return LearningLiterals(files.values(), allowed_paths)

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
            "answers": {
                key: {
                    field: value
                    for field, value in answer.items()
                    if field != "continuation_execution_id"
                }
                for key, answer in execution["answers"].items()
            },
            "policy": {
                k: v for k, v in execution["policy"].items() if k != "test_task"
            },
        }

    def analyze(self, state):
        execution = self.begin(state, "analyze")
        if execution.get("analyzed_input_revision") == state["input_revision"]:
            rubrics_version_id = execution["rubrics_version_id"]
            return {
                "analysis": {
                    **execution["analysis"],
                    "questions": execution["questions"],
                    "rubrics": self.store.version_by_id(rubrics_version_id, "rubrics")[
                        "content"
                    ],
                },
                "rubrics_version_id": rubrics_version_id,
            }
        context = self.context(execution)
        with self.store.connect() as conn:
            recipe_name = self.store.execution_recipe_name(conn, execution)
        context["recipe_name"] = recipe_name
        fixed_rubrics_id = (execution.get("test_request") or {}).get(
            "rubrics_version_id"
        )
        if fixed_rubrics_id:
            context["fixed_rubrics"] = self.store.version_by_id(
                fixed_rubrics_id, "rubrics"
            )["content"]
        files = self.files(execution)
        literals = self.learning_literals(execution)
        feedback = None
        extract_feedback = getattr(self.runtime, "extract_feedback", None)
        if not fixed_rubrics_id and extract_feedback is not None:
            sources = [f for f in files if f["role"] in {"context", "feedback"}]
            feedback = (
                FeedbackExtraction.model_validate(
                    extract_feedback(
                        sources, self.directory(state) / "feedback-extraction.json"
                    )
                ).model_dump()["items"]
                if sources
                else []
            )
            source_locators = {f["locator"] for f in sources}
            if any(
                ref["locator"] not in source_locators
                for item in feedback
                for ref in item["evidence_refs"]
            ) or literals.found_in(item["rule"] for item in feedback):
                raise ModelOutputError(
                    "Extracted feedback contains learning-only details or unauthorized evidence references"
                )
            context["user_feedback"] = feedback
        feedback_locators = {
            ref["locator"] for item in (feedback or []) for ref in item["evidence_refs"]
        }
        for attempt in range(3):
            analysis = Analysis.model_validate(
                self.runtime.analyze(
                    context, files, self.directory(state) / "analysis.json"
                )
            ).model_dump()
            ungrounded_feedback = feedback is not None and any(
                criterion["source"] == "user_feedback"
                and not any(
                    ref["locator"] in feedback_locators for ref in criterion["evidence"]
                )
                for criterion in analysis["rubrics"]["criteria"]
            )
            proposed_name = analysis.get("recipe_name")
            if (
                recipe_name
                or not proposed_name
                or not literals.found_in([proposed_name])
            ) and (
                fixed_rubrics_id
                or (
                    not literals.found_in(rubric_rule_text(analysis["rubrics"]))
                    and not ungrounded_feedback
                )
            ):
                break
            if attempt == 2:
                raise ModelOutputError(
                    "Recipe name or Rubric rules contain learning-only details or unsupported feedback citations"
                )
            context["portability_feedback"] = (
                "Rewrite the Rubric as reusable rules for any future input. "
                "Express the requirements without copying details or references specific to past executions. Refer to input roles instead of naming learning source files. "
                "Use an evidence reference from an extracted rule for every user_feedback criterion. If asked to propose a Recipe name, make it reusable and omit learning-only details."
            )
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
        if recipe_name:
            analysis.pop("recipe_name", None)
        if feedback is not None:
            analysis["feedback"] = feedback
        for question in analysis["questions"]:
            question["revision"] = state["input_revision"]
        rubrics_version_id = self.store.save_analysis(
            self.execution_id, self.owner, self.fence, state["input_revision"], analysis
        )
        return {"analysis": analysis, "rubrics_version_id": rubrics_version_id}

    def generate(self, state):
        round_number = state.get("round", 0) + 1
        execution = self.begin(state, "generate", round_number=round_number)
        with self.store.connect() as conn:
            recipe_name = self.store.execution_recipe_name(conn, execution)
        context = {
            **self.context(execution),
            "recipe_name": recipe_name,
            "analysis": state["analysis"],
            "previous_test": state.get("test"),
        }
        base_id = state.get("version_id") or execution.get("base_recipe_version_id")
        base = self.store.version_by_id(base_id, "recipe") if base_id else None
        request = execution.get("test_request")
        if request:
            return {
                "recipe_id": request["recipe_id"],
                "version_id": request["version_id"],
                "round": round_number,
            }
        key = f"recipe:{self.execution_id}:{state['input_revision']}:{round_number}"
        version = self.store.version_by_operation(key)
        if version:
            return {
                "recipe_id": version["object_id"],
                "version_id": version["id"],
                "round": round_number,
            }
        files = self.files(execution)
        recipe_file = next((file for file in files if file["role"] == "recipe"), None)
        if base:
            context["base_recipe"] = base["content"]
        elif recipe_file:
            context["base_recipe"] = read_snapshot(recipe_file)["text"]
        literals = self.learning_literals(
            execution, base["provenance"].get("execution_id") if base else None
        )
        for attempt in range(3):
            content = RecipeContent.model_validate(
                self.runtime.generate(
                    context,
                    files,
                    self.directory(state) / f"generate-{round_number}.json",
                )
            ).model_dump()
            if recipe_name:
                content["name"] = recipe_name
            if not literals.found_in(content.values()):
                break
            if attempt == 2:
                raise ModelOutputError("Recipe rules contain learning-only literals")
            context["portability_feedback"] = (
                "Rewrite the Recipe as reusable rules for any future input. "
                "Express the procedure without copying details or references specific to past executions. Refer to input roles instead of naming learning source files."
            )
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
        }

    def test(self, state):
        execution = self.begin(state, "test", round_number=state["round"])
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
            literals = self.learning_literals(
                execution,
                recipe["provenance"].get("execution_id"),
                rubric["provenance"].get("execution_id"),
            )
            # Existing versions must pass the same boundary without rewriting their rules.
            if literals.found_in(recipe["content"].values()) or literals.found_in(
                rubric_rule_text(rubric["content"])
            ):
                raise ModelOutputError("Test rules contain learning-only literals")
            requires_python = rubric["content"].get("requires_python", False)
            python_requirement = {"requires_python": True} if requires_python else {}
            test_rubrics = {
                "pass_threshold": rubric["content"]["pass_threshold"],
                **python_requirement,
                "criteria": [
                    {field: c[field] for field in ("id", "description", "required")}
                    for c in rubric["content"]["criteria"]
                ],
            }
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
                    **python_requirement,
                },
                test_files,
                workspace,
                attempt / "executor-trace.json",
            )
            atomic_json(attempt / "executor-result.json", executor_result)
            self.guard(state["input_revision"])
            artifacts = []
            for path in sorted(workspace.rglob("*")):
                if path.relative_to(workspace).parts[0] == "scripts":
                    continue
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

            python_runs = {}
            if isinstance(executor_result, dict):
                receipts = executor_result.get("python_runs")
                if isinstance(receipts, list):
                    python_runs = {run["path"]: run for run in receipts}
            python_passed = bool(python_runs) and all(
                run["status"] == "completed" and run["exit_code"] == 0
                for run in python_runs.values()
            )
            if (requires_python or python_runs) and not python_passed:
                unavailable = (
                    requires_python and not self.client.config.test_python_image
                ) or any(run["status"] == "unavailable" for run in python_runs.values())
                result.update(
                    status="unverifiable" if unavailable else "failed",
                    reason=(
                        "Python sandbox is unavailable for Recipe validation"
                        if unavailable
                        else "Recipe executor did not complete required Python execution"
                    ),
                )
            elif not artifacts:
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
                self.begin(state, "evaluate", round_number=state["round"])
                assessment = Assessment.model_validate(
                    self.runtime.assess(
                        {
                            "test_task": task,
                            "rubrics": test_rubrics,
                            "objective_checks": checks,
                            "recipe": recipe["content"],
                        },
                        evaluator_files,
                        attempt / "evaluator-trace.json",
                    )
                ).model_dump()
                result.update(
                    scores=assessment["scores"],
                    checks=checks,
                    reason=assessment["reason"],
                    evaluator_trace_path=str(attempt / "evaluator-trace.json"),
                )
                if assessment["verifiable"]:
                    criteria = rubric["content"]["criteria"]
                    scores = {
                        s["criterion_id"]: s["score"] for s in assessment["scores"]
                    }
                    if len(scores) != len(assessment["scores"]) or set(scores) != {
                        c["id"] for c in criteria
                    }:
                        raise ModelOutputError(
                            "evaluator must report every fixed criterion exactly once"
                        )
                    verified_scores = {
                        key: score for key, score in scores.items() if score is not None
                    }
                    if verified_scores:
                        result["score"] = sum(verified_scores.values()) / len(
                            verified_scores
                        )
                    threshold = rubric["content"]["pass_threshold"]
                    if not all(c["passed"] for c in checks):
                        result["status"] = "failed"
                    elif result["score"] is not None:
                        passed = result["score"] >= threshold and all(
                            verified_scores[c["id"]] >= threshold
                            for c in criteria
                            if c["required"] and c["id"] in verified_scores
                        )
                        result["status"] = "passed" if passed else "failed"
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
