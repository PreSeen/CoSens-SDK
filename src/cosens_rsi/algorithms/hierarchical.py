"""Versioned FlowRecipe/StepRecipe bundles and their frozen DAG interpreter.

Only declarative Recipe data evolves. The driver, port checks, budgets and
end-task evaluator are supplied by the runner and cannot be edited by a learner.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Protocol


SLOTS = frozenset({"preconditions", "steps", "checks", "recovery"})
PORT_TYPES = frozenset({"text", "artifacts"})


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _fields(value: Any, required: set[str], optional: set[str] = frozenset()) -> None:
    if not isinstance(value, dict) or set(value) - required - optional or required - set(value):
        raise ValueError(f"expected fields {sorted(required)}; optional {sorted(optional)}")


def _strings(value: Any, *, empty: bool = False) -> bool:
    return isinstance(value, list) and (empty or bool(value)) and all(isinstance(v, str) and v.strip() for v in value)


def validate_bundle(bundle: dict) -> dict:
    """Validate complete bindings, DAG, port compatibility and immutable versions."""
    _fields(bundle, {"schema", "flow", "bindings", "steps"})
    if bundle["schema"] != "hierarchical-recipe-1":
        raise ValueError("unsupported hierarchical Recipe schema")
    flow, steps, bindings = bundle["flow"], bundle["steps"], bundle["bindings"]
    _fields(flow, {"id", "version", "nodes", "final", "checks"})
    if not _strings(flow["checks"], empty=True):
        raise ValueError("flow checks must be strings")
    if not isinstance(steps, dict) or not steps or not isinstance(bindings, dict):
        raise ValueError("steps and bindings must be objects")
    for ref, step in steps.items():
        _fields(step, {"id", "version", "inputs", "outputs", "body"})
        if not all(isinstance(step[k], str) and step[k].strip() for k in ("id", "version")):
            raise ValueError("step identity must be non-empty")
        if ref != f"{step['id']}@{step['version']}":
            raise ValueError("step reference does not pin its version")
        for ports in (step["inputs"], step["outputs"]):
            if not isinstance(ports, dict) or not ports or any(
                not isinstance(k, str) or not k or t not in PORT_TYPES for k, t in ports.items()
            ):
                raise ValueError("invalid step ports")
        _fields(step["body"], set(SLOTS))
        if any(not _strings(v, empty=(k != "steps")) for k, v in step["body"].items()):
            raise ValueError("step body slots must contain strings")
    if any(not isinstance(k, str) or not k or ref not in steps for k, ref in bindings.items()):
        raise ValueError("dangling step binding")
    if not isinstance(flow["nodes"], list) or not flow["nodes"] or len(flow["nodes"]) > 12:
        raise ValueError("flow needs 1..12 nodes")
    nodes: dict[str, dict] = {}
    for node in flow["nodes"]:
        _fields(node, {"id", "goal", "skill", "inputs", "after", "max_repairs"})
        if not isinstance(node["id"], str) or not node["id"] or node["id"] in nodes:
            raise ValueError("duplicate or empty node id")
        if not isinstance(node["goal"], str) or not node["goal"].strip():
            raise ValueError("node goal is required")
        if node["skill"] not in bindings:
            raise ValueError("unbound node skill")
        if type(node["max_repairs"]) is not int or node["max_repairs"] not in (0, 1):
            raise ValueError("at most one local repair is allowed")
        if not _strings(node["after"], empty=True) or not isinstance(node["inputs"], dict):
            raise ValueError("invalid node dependencies")
        step = steps[bindings[node["skill"]]]
        if set(node["inputs"]) != set(step["inputs"]):
            raise ValueError("node input ports do not match step contract")
        nodes[node["id"]] = node
    dependencies: dict[str, set[str]] = {}
    for name, node in nodes.items():
        deps = set(node["after"])
        step = steps[bindings[node["skill"]]]
        for port, source in node["inputs"].items():
            if source == "$task":
                kind = "text"
            else:
                if not isinstance(source, str) or "." not in source:
                    raise ValueError("invalid input source")
                producer, output = source.split(".", 1)
                if producer not in nodes:
                    raise ValueError("missing input producer")
                other = steps[bindings[nodes[producer]["skill"]]]
                if output not in other["outputs"]:
                    raise ValueError("missing output port")
                kind = other["outputs"][output]
                deps.add(producer)
            if kind != step["inputs"][port]:
                raise ValueError("incompatible port types")
        if name in deps or deps - nodes.keys():
            raise ValueError("invalid or self dependency")
        dependencies[name] = deps
    pending = dict(dependencies)
    order: list[str] = []
    while pending:
        ready = [n for n in nodes if n in pending and not pending[n] - set(order)]
        if not ready:
            raise ValueError("cycle in flow")
        for n in ready:
            order.append(n)
            del pending[n]
    source = flow["final"]
    if not isinstance(source, str) or "." not in source:
        raise ValueError("final must name a node output")
    final_node, final_port = source.split(".", 1)
    if final_node not in nodes or final_port not in steps[bindings[nodes[final_node]["skill"]]]["outputs"]:
        raise ValueError("invalid final output")
    if not all(isinstance(flow[k], str) and flow[k].strip() for k in ("id", "version")):
        raise ValueError("flow identity is required")
    # Disconnected nodes can otherwise run without contributing to the result.
    ancestors = {final_node}
    while True:
        expanded = ancestors | set().union(*(dependencies[n] for n in ancestors))
        if expanded == ancestors:
            break
        ancestors = expanded
    if ancestors != nodes.keys():
        raise ValueError("every node must contribute to the final output")
    canonical = deepcopy(bundle)
    canonical["flow"]["nodes"] = [deepcopy(nodes[n]) for n in order]
    return canonical


def freeze_bundle(bundle: dict) -> dict:
    canonical = validate_bundle(bundle)
    return {"bundle": canonical, "sha256": fingerprint(canonical),
            "flow_sha256": fingerprint(canonical["flow"]),
            "step_sha256": {k: fingerprint(v) for k, v in canonical["steps"].items()}}


def mutate(bundle: dict, change: dict) -> dict:
    """One step slot or one typed graph edit; I/O contracts remain fixed."""
    result = validate_bundle(bundle)
    if change.get("layer") == "step":
        _fields(change, {"layer", "target", "slot", "value"})
        target, slot = change["target"], change["slot"]
        if target not in result["bindings"] or slot not in SLOTS or not _strings(change["value"]):
            raise ValueError("invalid step mutation")
        old_ref = result["bindings"][target]
        step = deepcopy(result["steps"][old_ref])
        if step["body"][slot] == change["value"]:
            raise ValueError("no-op mutation")
        step["body"][slot] = deepcopy(change["value"])
        step["version"] = fingerprint({k: v for k, v in step.items() if k != "version"})[:16]
        ref = f"{step['id']}@{step['version']}"
        result["steps"][ref] = step
        result["bindings"][target] = ref
    elif change.get("layer") == "flow":
        _fields(change, {"layer", "op", "node"}, {"value"})
        nodes = result["flow"]["nodes"]
        previous_nodes = deepcopy(nodes)
        node = next((n for n in nodes if n["id"] == change["node"]), None)
        op = change["op"]
        if op == "insert":
            spec = change.get("value")
            _fields(spec, {"node", "consumer", "input_port", "output_port"})
            new = spec["node"]
            if node is not None or not isinstance(new, dict) or new.get("id") != change["node"]:
                raise ValueError("invalid node insertion")
            consumer = next((n for n in nodes if n["id"] == spec["consumer"]), None)
            if consumer is None or spec["input_port"] not in consumer["inputs"]:
                raise ValueError("insertion needs an existing consumer input")
            nodes.append(deepcopy(new))
            consumer["inputs"][spec["input_port"]] = f"{new['id']}.{spec['output_port']}"
        elif node is None:
            raise ValueError("unknown flow node")
        elif op == "remove":
            spec = change.get("value")
            _fields(spec, {"redirects"})
            if not isinstance(spec["redirects"], dict):
                raise ValueError("removal needs explicit replacement output sources")
            # A splice is one structural operation: remove the node and
            # reconnect its consumers, including pure ordering dependencies.
            parents = set(node["after"]) | {s.split(".", 1)[0] for s in node["inputs"].values() if s != "$task"}
            for consumer in nodes:
                for port, source in list(consumer["inputs"].items()):
                    if source.startswith(change["node"] + "."):
                        output = source.split(".", 1)[1]
                        if output not in spec["redirects"]:
                            raise ValueError("missing removal redirect")
                        consumer["inputs"][port] = spec["redirects"][output]
                if change["node"] in consumer["after"]:
                    consumer["after"] = sorted((set(consumer["after"]) - {change["node"]}) | parents)
            if result["flow"]["final"].startswith(change["node"] + "."):
                result["flow"]["final"] = spec["redirects"].get(result["flow"]["final"].split(".", 1)[1], "")
            nodes.remove(node)
        elif op == "bind":
            node["skill"] = change["value"]
        elif op == "rewire":
            _fields(change.get("value"), {"inputs", "after"})
            node.update(deepcopy(change["value"]))
        else:
            raise ValueError("unknown flow edit")
        if nodes == previous_nodes:
            raise ValueError("no-op mutation")
        result["flow"]["version"] = fingerprint(nodes)[:16]
    else:
        raise ValueError("mutation layer must be step or flow")
    return validate_bundle(result)


def seed_bundle() -> dict:
    """Domain-neutral bootstrap, explicitly not a learned or validated method."""
    bodies = {
        "prepare": {"preconditions": [], "steps": [
            "Read the request and accessible source inputs; identify required outputs and hard constraints.",
            "Prepare a concise execution brief with necessary evidence, dependencies and unresolved limitations."],
            "checks": ["The brief covers the requested deliverables and constraints."], "recovery": []},
        "produce": {"preconditions": ["A preparation brief is available."],
            "steps": ["Use the supplied brief and authorized inputs to produce and save the requested deliverables."],
            "checks": ["Outputs exist in the requested formats."], "recovery": ["Repair a missing deliverable."]},
        "verify": {"preconditions": ["Saved candidate deliverables are available."],
            "steps": ["Inspect the saved deliverables against the task; correct any remaining gap and submit the final files."],
            "checks": ["Every submitted file is readable and relevant to the request."],
            "recovery": ["Make a focused correction and inspect the saved file again."]},
    }
    steps = {f"{name}@seed": {"id": name, "version": "seed", "inputs": inputs,
                             "outputs": outputs, "body": bodies[name]}
             for name, inputs, outputs in (
                 ("prepare", {"task": "text"}, {"brief": "text"}),
                 ("produce", {"task": "text", "brief": "text"}, {"files": "artifacts"}),
                 ("verify", {"task": "text", "files": "artifacts"}, {"files": "artifacts"}))}
    return validate_bundle({"schema": "hierarchical-recipe-1", "steps": steps,
        "bindings": {name: f"{name}@seed" for name in bodies},
        "flow": {"id": "task-delivery", "version": "seed", "checks": ["Satisfy the task's explicit requirements."],
                 "nodes": [
                     {"id": "prepare", "goal": "Prepare the task and evidence", "skill": "prepare",
                      "inputs": {"task": "$task"}, "after": [], "max_repairs": 1},
                     {"id": "produce", "goal": "Create requested deliverables", "skill": "produce",
                      "inputs": {"task": "$task", "brief": "prepare.brief"}, "after": [], "max_repairs": 1},
                     {"id": "verify", "goal": "Inspect and finalize saved deliverables", "skill": "verify",
                      "inputs": {"task": "$task", "files": "produce.files"}, "after": [], "max_repairs": 1}],
                 "final": "verify.files"}})


def bundle_from_flat_recipe(recipe: dict) -> dict:
    """Lossless instruction migration; a one-node baseline, not learned splitting."""
    body = recipe.get("body", {})
    normalized = {slot: deepcopy(body.get(slot, [])) for slot in SLOTS}
    if not normalized["steps"]:
        normalized["steps"] = ["Complete the task using its explicit requirements."]
    for name in ("strategy", "execution"):
        if body.get(name):
            normalized["steps"].append(f"Follow the frozen {name} instructions: " + json.dumps(body[name], ensure_ascii=False))
    scope = recipe.get("scope", {})
    for name in ("prerequisites", "exclusions"):
        if scope.get(name):
            normalized["preconditions"].append(f"Recipe {name}: " + json.dumps(scope[name], ensure_ascii=False))
    if body.get("task_profile"):
        normalized["preconditions"].append("Recipe task profile: " + json.dumps(body["task_profile"], ensure_ascii=False))
    identity = "migrated-" + str(recipe.get("id", "recipe"))
    version = fingerprint(normalized)[:16]
    step = {"id": identity, "version": version, "inputs": {"task": "text"},
            "outputs": {"files": "artifacts"}, "body": normalized}
    return validate_bundle({"schema": "hierarchical-recipe-1", "steps": {f"{identity}@{version}": step},
        "bindings": {"legacy": f"{identity}@{version}"}, "flow": {"id": identity, "version": version,
        "checks": [], "final": "complete.files", "nodes": [{"id": "complete", "skill": "legacy",
        "goal": str(recipe.get("description") or "Complete the requested task"), "inputs": {"task": "$task"},
        "after": [], "max_repairs": 1}]}})


class StepDriver(Protocol):
    def execute(self, instruction: str, *, timeout: float) -> dict:
        """Return completed, outputs, elapsed_sec, tokens, error (if any)."""


@dataclass(frozen=True)
class ExecutionBudget:
    max_calls: int = 6
    max_seconds: float = 1800
    per_call_seconds: float = 600


def _check_outputs(outputs: Any, ports: dict, workspace: Path, protected: dict) -> list[str]:
    if not isinstance(outputs, dict) or set(outputs) != set(ports):
        return ["output ports do not match the frozen step contract"]
    errors = []
    for name, digest in protected.items():
        try:
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                errors.append("a protected task input was modified")
        except OSError:
            errors.append("a protected task input became unavailable")
    for port, kind in ports.items():
        value = outputs[port]
        if kind == "text":
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{port}: expected non-empty text")
        elif not _strings(value):
            errors.append(f"{port}: expected non-empty artifact path list")
        else:
            for name in value:
                path = (workspace / name).resolve()
                if not path.is_relative_to(workspace) or not path.is_file():
                    errors.append(f"{port}: artifact missing or outside workspace")
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    errors.append(f"{port}: artifact unreadable")
                    continue
                if not data or protected.get(str(path)) == hashlib.sha256(data).hexdigest():
                    errors.append(f"{port}: artifact empty or unchanged task input")
    return errors


def _canonicalize_artifact_port_alias(outputs: Any, ports: dict) -> tuple[Any, Any | None]:
    """Normalize the common files/artifacts label mismatch for artifact ports.

    The schema remains the source of truth: this accepts only a one-port,
    one-to-one alias where both labels denote artifact lists. The raw value is
    returned separately so traces retain exactly what the actor emitted.
    """
    if (isinstance(outputs, dict) and len(outputs) == 1 and len(ports) == 1
            and ports.get("files") == "artifacts" and "files" not in outputs
            and "artifacts" in outputs):
        return {"files": outputs["artifacts"]}, outputs
    if (isinstance(outputs, dict) and len(outputs) == 1 and len(ports) == 1
            and ports.get("artifacts") == "artifacts" and "artifacts" not in outputs
            and "files" in outputs):
        return {"artifacts": outputs["files"]}, outputs
    return outputs, None


class HierarchicalExecutor:
    """All task runs start fresh; only passed predecessor outputs are bound."""

    def run(self, bundle: dict, task: str, workspace: Path, driver: StepDriver,
            budget: ExecutionBudget | None = None, *, evidence_context: dict | None = None,
            editable_inputs: tuple[str, ...] = ()) -> dict:
        snapshot = freeze_bundle(bundle)
        frozen = snapshot["bundle"]
        context = json.loads(json.dumps(evidence_context or {}, allow_nan=False))
        budget = budget or ExecutionBudget()
        if budget.max_calls < 1 or budget.max_seconds <= 0 or budget.per_call_seconds <= 0:
            raise ValueError("execution budgets must be positive")
        workspace = Path(workspace).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        original_inputs = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in workspace.rglob("*") if p.is_file() and p.resolve().is_relative_to(workspace)}
        editable = set()
        for name in editable_inputs:
            if not isinstance(name, str) or not name:
                raise ValueError('editable input must be an explicit source file')
            path = (workspace / name).resolve()
            if not path.is_relative_to(workspace) or str(path) not in original_inputs:
                raise ValueError('editable input is outside the frozen workspace')
            editable.add(str(path))
        # This allowlist comes from operator-owned execution configuration,
        # never from generated Recipe text. Source documents stay immutable.
        protected = {name: sha for name, sha in original_inputs.items() if name not in editable}
        outputs: dict[str, dict] = {}
        trace: list[dict] = []
        action_events: list[dict] = []
        actions_complete = True
        started = time.monotonic()
        calls = 0
        status = "completed"
        failure = None
        for node in frozen["flow"]["nodes"]:
            ref = frozen["bindings"][node["skill"]]
            step = frozen["steps"][ref]
            inputs = {port: task if src == "$task" else outputs[src.split(".", 1)[0]][src.split(".", 1)[1]]
                      for port, src in node["inputs"].items()}
            errors: list[str] = []
            accepted = False
            for attempt in range(node["max_repairs"] + 1):
                remaining = budget.max_seconds - (time.monotonic() - started)
                if calls >= budget.max_calls or remaining <= 0:
                    status, failure = "over_budget", {"layer": "infrastructure", "node": node["id"], "reason": "execution budget exhausted"}
                    break
                prompt = ("Execute ONLY the current workflow node using the authorized inputs. "
                          "The same agent will execute subsequent nodes. Preserve source inputs. "
                          "Use available tools; do not claim a file exists without saving it. "
                          "Finish with one JSON object {\"outputs\": {...}} matching the output contract. "
                          "Artifact paths must refer to saved files in this workspace.\n" +
                          json.dumps({"goal": node["goal"], "inputs": inputs, "method": step["body"],
                                      "output_contract": step["outputs"], "global_checks": frozen["flow"]["checks"],
                                      "repair_errors": errors, "evidence_context": context}, ensure_ascii=False))
                calls += 1
                try:
                    result = driver.execute(prompt, timeout=min(budget.per_call_seconds, remaining))
                except Exception as exc:
                    result = {"completed": False, "error": type(exc).__name__, "outputs": {}}
                raw_outputs = result.get("outputs")
                normalized_outputs, aliased_outputs = _canonicalize_artifact_port_alias(raw_outputs, step["outputs"])
                errors = _check_outputs(normalized_outputs, step["outputs"], workspace, protected)
                if not result.get("completed"):
                    errors = ["agent execution did not complete", *errors]
                if time.monotonic() - started > budget.max_seconds:
                    errors.append("execution deadline exceeded")
                actions = result.get("action_trace")
                # A node driver must expose its actual tool operations. Node
                # completion or a short prose summary cannot imply coverage.
                from .trajectory import measure_trace
                try:
                    measure_trace(actions)
                    if not actions["events"]:
                        raise ValueError("empty node telemetry")
                except ValueError:
                    actions_complete = False
                else:
                    for event in actions["events"]:
                        copied = {**event, "id": f"{calls}:{event['id']}", "node": node["id"],
                                  "sequence": len(action_events) + 1, "repair": attempt > 0 or event.get("repair", False)}
                        action_events.append(copied)
                trace.append({"node": node["id"], "step_ref": ref, "step_sha256": snapshot["step_sha256"][ref],
                              "input_sha256": fingerprint(inputs), "output_sha256": fingerprint(normalized_outputs),
                              "outputs": normalized_outputs, "raw_outputs": aliased_outputs,
                              "attempt": attempt, "passed": not errors,
                              "errors": errors, "elapsed_sec": result.get("elapsed_sec", 0),
                              "tokens": result.get("tokens"), "agent_error": result.get("error"),
                              "response": result.get("response", "")})
                if result.get("audit_files"):
                    trace[-1]["audit_files"] = result["audit_files"]
                if not result.get("completed"):
                    status, failure = "infrastructure_error", {"layer": "infrastructure", "node": node["id"], "reason": result.get("error", "agent incomplete")}
                    break
                if "execution deadline exceeded" in errors:
                    status, failure = "over_budget", {"layer": "infrastructure", "node": node["id"],
                                                        "reason": "execution deadline exceeded"}
                    break
                if not errors:
                    outputs[node["id"]] = normalized_outputs
                    accepted = True
                    break
            if not accepted:
                if failure is None:
                    status, failure = "failed", {"layer": "step", "node": node["id"], "skill": node["skill"], "reason": errors}
                break
        final = None
        if status == "completed":
            name, port = frozen["flow"]["final"].split(".", 1)
            final = outputs[name][port]
        return {"bundle_sha256": snapshot["sha256"], "task_sha256": fingerprint(task), "status": status,
                "editable_inputs": [str(Path(p).relative_to(workspace)) for p in sorted(editable)],
                "final": final, "trace": trace, "failure": failure, "calls": calls,
                "context_sha256": fingerprint(context),
                "action_trace": {"schema": "action-trace-v1", "complete": actions_complete and status in {"completed", "failed"},
                                 "events": action_events},
                "elapsed_sec": time.monotonic() - started,
                "tokens": sum(t["tokens"] for t in trace) if all(isinstance(t["tokens"], int) for t in trace) else None}
