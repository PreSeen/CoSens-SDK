from mcp.server.fastmcp import FastMCP
import anyio

from cosens_rsi.service import RSIService

mcp = FastMCP("cosens-rsi", stateless_http=False, json_response=True)
service = RSIService()
PRINCIPAL = "local-codex"


async def invoke(function, *args, **kwargs):
    """Run synchronous SQLite and Memory HTTP work outside FastMCP's loop.

    Memory is mounted in this same ASGI process, so blocking the event loop
    while making an HTTP request back to it would deadlock the MCP call.
    """
    return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs))


@mcp.tool()
async def cosens_prepare_task(objective: str, task_family: str = "general", execution_mode: str = "assist",
                        requires_evidence: bool = False, environment: dict | None = None,
                        input_artifact_ids: list[str] | None = None, deliverable: dict | None = None,
                        acceptance: dict | None = None, request_key: str | None = None,
                        task_lineage_id: str | None = None) -> dict:
    """Start or prepare an RSI task. RSI selects its Recipe and calls Friday Memory internally."""
    return await invoke(service.prepare_task, {"objective": objective, "task_family": task_family,
        "execution_mode": execution_mode, "requires_evidence": requires_evidence,
        "environment": environment or {}, "input_artifact_ids": input_artifact_ids or [],
        "deliverable": deliverable or {}, "acceptance": acceptance or {}, "task_lineage_id": task_lineage_id}, PRINCIPAL,
        explicit=True, request_key=request_key)


@mcp.tool()
async def cosens_request_guidance(run_id: str, observation: str = "", candidates: list[dict] | None = None,
                                  node_id: str | None = None, request_key: str | None = None) -> dict:
    """Get RSI guidance or rank 1..8 candidate strategies using verified experience.

    Candidates contain id, version, description, confidence [0,1], cost [0,1].
    Bind guided decisions to the issued node_id. Record actual actions through
    checkpoint or submit_step; an advisory recommendation is not an execution.
    """
    return await invoke(service.guidance, run_id, PRINCIPAL, observation, candidates, node_id, request_key)


@mcp.tool()
async def cosens_checkpoint(run_id: str, checkpoint: dict) -> dict:
    """Save task progress; for assist decisions include decision_id, actual_action_id and next_state.

    Actual-action experiences are written internally to Friday Memory as pending.
    Neither checkpoint fields nor user feedback can supply verified rewards.
    """
    return await invoke(service.checkpoint, run_id, PRINCIPAL, checkpoint)


@mcp.tool()
async def cosens_next_step(run_id: str) -> dict:
    """Get the next frozen hierarchical step for a guided RSI Run."""
    return await invoke(service.next_step, run_id, PRINCIPAL)


@mcp.tool()
async def cosens_store_artifact(name: str, content: str) -> dict:
    """Store a text deliverable in RSI and return an immutable artifact reference."""
    return await invoke(service.store_artifact, PRINCIPAL, name, content)


@mcp.tool()
async def cosens_submit_step(run_id: str, attempt_id: str, attempt_token: str, outputs: dict,
                             decision_id: str | None = None, actual_action_id: str | None = None) -> dict:
    """Submit a guided step's outputs against the server-issued attempt token and frozen contract."""
    return await invoke(service.submit_step, run_id, PRINCIPAL, attempt_id, attempt_token, outputs,
                        decision_id, actual_action_id)


@mcp.tool()
async def cosens_get_decision(run_id: str, decision_id: str) -> dict:
    """Read an RSI decision, actual action and internal Memory writeback status."""
    return await invoke(service.get_decision, run_id, PRINCIPAL, decision_id)


@mcp.tool()
async def cosens_complete_run(run_id: str) -> dict:
    """Complete a guided RSI Run after all DAG nodes pass their output contracts."""
    return await invoke(service.complete_guided_run, run_id, PRINCIPAL)


@mcp.tool()
async def cosens_submit_result(run_id: str, result: dict) -> dict:
    """Submit the task result. External Codex results are recorded as unverified/inconclusive."""
    return await invoke(service.submit_result, run_id, PRINCIPAL, result)


@mcp.tool()
async def cosens_get_run(run_id: str) -> dict:
    """Read an existing RSI Run without performing another Memory search."""
    return await invoke(service.get_run, run_id, PRINCIPAL)


@mcp.tool()
async def cosens_get_active_recipe(task_family: str) -> dict:
    """Read the active hierarchical RSI recipe for a task family."""
    return await invoke(service.get_active_recipe, task_family)


@mcp.tool()
async def cosens_record_feedback(run_id: str, feedback: dict) -> dict:
    """Record user feedback against an existing Run; this does not directly modify shared Memory."""
    return await invoke(service.record_feedback, run_id, PRINCIPAL, feedback)


@mcp.tool()
async def cosens_start_improvement(run_id: str, reason: str, request_key: str | None = None) -> dict:
    """Request a harness improvement job; reuse request_key for retries.

    The default key deduplicates the same run/reason. Use a new explicit key
    for a new experiment. Publication is a separate administrator action.
    """
    return await invoke(service.start_improvement, run_id, PRINCIPAL, reason, request_key)


@mcp.tool()
async def cosens_get_improvement(job_id: str) -> dict:
    """Read harness diagnosis, frozen comparison evidence and improvement status."""
    return await invoke(service.get_improvement, job_id, PRINCIPAL)


@mcp.tool()
async def cosens_capture_session_events(run_id: str, events: list[dict]) -> dict:
    """Durably append observable session events; stable IDs deduplicate retries.

    This does not attest complete action telemetry or verified expert authority.
    """
    return await invoke(service.capture_session_events, run_id, PRINCIPAL, events)


@mcp.tool()
async def cosens_supply_initial_inputs(run_id: str, artifact_ids: list[str]) -> dict:
    """Supply missing original materials by immutable artifact ID before task inputs freeze.

    Late materials are labelled supplemented; existing executed inputs cannot
    be overwritten. Store artifacts first with cosens_store_artifact.
    """
    return await invoke(service.supply_initial_inputs, run_id, PRINCIPAL, artifact_ids)


@mcp.tool()
async def cosens_resume_improvement(job_id: str) -> dict:
    """Resume waiting generic preparation after supplying inputs or a global runner.

    Running/unknown external executions require inspection and cannot be retried
    through this tool. New tasks and feedback also wake waiting jobs.
    """
    return await invoke(service.resume_improvement, job_id, PRINCIPAL)


@mcp.tool()
async def cosens_get_run_events(run_id: str) -> dict:
    """List persisted RSI lifecycle events for a Run."""
    return {"items": await invoke(service.get_events, run_id, PRINCIPAL)}
