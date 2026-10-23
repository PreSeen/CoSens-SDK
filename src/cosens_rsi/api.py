from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from pathlib import Path

from cosens_rsi import mcp_server
from memory.cosens_api import app as memory_app


service = mcp_server.service
mcp = mcp_server.mcp


@asynccontextmanager
async def lifespan(_app):
    # Memory remains an implementation detail behind RSI and shares the same
    # listener. Its worker lifecycle is owned by this service process.
    async with memory_app.router.lifespan_context(memory_app):
        async with mcp.session_manager.run():
            stop = threading.Event()

            def dispatch():
                while not stop.is_set():
                    try:
                        service.flush_memory_outbox(limit=10)
                        service.flush_recipe_memory_outbox(limit=20)
                    except Exception:
                        logging.getLogger(__name__).exception("Memory outbox dispatcher failed")
                    stop.wait(0.5)

            thread = threading.Thread(target=dispatch, name="rsi-memory-outbox", daemon=True)
            def improve():
                while not stop.is_set():
                    try:
                        service.process_one_improvement()
                    except Exception:
                        logging.getLogger(__name__).exception("Harness worker failed")
                    stop.wait(0.5)

            harness_thread = threading.Thread(target=improve, name="rsi-harness-worker", daemon=True)
            thread.start()
            harness_thread.start()
            try:
                yield
            finally:
                stop.set()
                thread.join(timeout=15)
                harness_thread.join(timeout=15)


app = FastAPI(title="CoSens RSI Workflow API", version="1.0.0", lifespan=lifespan)
app.state.rsi = service
_DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"


class PrepareBody(BaseModel):
    objective: str = Field(min_length=1, max_length=20000)
    task_family: str = "general"
    task_lineage_id: str | None = None
    input_artifact_ids: list[str] = Field(default_factory=list)
    deliverable: dict = Field(default_factory=dict)
    acceptance: dict = Field(default_factory=dict)
    execution_mode: str = "assist"
    requires_evidence: bool = False
    environment: dict = Field(default_factory=dict)


class JsonBody(BaseModel):
    data: dict = Field(default_factory=dict)


def principal(authorization: str | None):
    if authorization != f"Bearer {service.token}":
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "invalid bearer token"})
    return "local-codex"


def admin(authorization: str | None):
    if authorization != f"Bearer {service.admin_token}":
        raise HTTPException(401, detail={"code": "unauthenticated", "message": "admin credential required"})


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp") and request.headers.get("authorization") != f"Bearer {service.token}":
        return JSONResponse(status_code=401, content={"error": "unauthenticated"})
    return await call_next(request)


@app.exception_handler(ValueError)
async def bad_request(_request, exc):
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": str(exc)}})


@app.exception_handler(PermissionError)
async def forbidden(_request, exc):
    return JSONResponse(status_code=403, content={"error": {"code": "permission_denied", "message": str(exc)}})


@app.exception_handler(LookupError)
async def not_found(_request, exc):
    return JSONResponse(status_code=404, content={"error": {"code": "resource_not_found", "message": str(exc)}})


@app.exception_handler(RuntimeError)
async def dependency_unavailable(_request, exc):
    return JSONResponse(status_code=503, content={"error": {"code": "dependency_unavailable", "message": str(exc)}})


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(authorization: str | None = Header(default=None)):
    principal(authorization)
    try:
        return service.health()
    except RuntimeError as exc:
        raise HTTPException(503, detail={"code": "dependency_unavailable", "message": str(exc)}) from exc


@app.get("/", include_in_schema=False)
def dashboard_home():
    return RedirectResponse("/dashboard")


@app.api_route("/dashboard", methods=["GET", "HEAD"], include_in_schema=False)
def dashboard():
    return FileResponse(_DASHBOARD, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.post("/v1/trigger/decide")
def trigger_decide(body: PrepareBody, authorization: str | None = Header(default=None)):
    principal(authorization)
    return service.decide_trigger(body.model_dump(), explicit=False)


@app.post("/v1/runs/prepare")
def prepare(body: PrepareBody, authorization: str | None = Header(default=None), idempotency_key: str | None = Header(default=None)):
    owner = principal(authorization)
    if not idempotency_key:
        raise HTTPException(400, detail={"code": "idempotency_key_required"})
    # The MCP tool is a user-requested action; approval-mode execution waits for
    # the separate authenticated admin/user authorization channel.
    return service.prepare_task(body.model_dump(), owner, explicit=True, request_key=idempotency_key)


@app.get("/v1/recipes/active")
def active_recipe(task_family: str, authorization: str | None = Header(default=None)):
    principal(authorization)
    return service.get_active_recipe(task_family)


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str, authorization: str | None = Header(default=None)):
    return service.get_run(run_id, principal(authorization))


@app.get("/v1/runs")
def list_runs(limit: int = 100, authorization: str | None = Header(default=None)):
    return {"items": service.list_runs(principal(authorization), limit=limit)}


@app.get("/v1/runs/{run_id}/trace")
def get_run_trace(run_id: str, authorization: str | None = Header(default=None)):
    return service.get_run_trace(run_id, principal(authorization))


@app.get("/v1/cases")
def list_cases(limit: int = 100, authorization: str | None = Header(default=None)):
    return {"items": service.list_cases(principal(authorization), limit)}


@app.get("/v1/cases/{case_id}")
def get_case(case_id: str, authorization: str | None = Header(default=None)):
    return service.get_case(case_id, principal(authorization))


@app.get("/v1/cases/{case_id}/artifacts/{artifact_id}")
def get_case_artifact(case_id: str, artifact_id: str, authorization: str | None = Header(default=None)):
    return service.get_case_artifact(case_id, artifact_id, principal(authorization))


@app.get("/v1/cases/{case_id}/runs/{run_id}/recipe")
def get_case_run_recipe(case_id: str, run_id: str, authorization: str | None = Header(default=None)):
    recipe = service.get_case_run_recipe(case_id, run_id, principal(authorization))
    filename = f"{recipe['task_family']}-recipe-r{recipe['revision']}.json"
    content = json.dumps(recipe, ensure_ascii=False, indent=2)
    return Response(content=content, media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})


@app.get('/v1/cases/{case_id}/improvements/{job_id}/recipe')
def get_case_candidate_recipe(case_id: str, job_id: str, authorization: str | None = Header(default=None)):
    value=service.get_case_candidate_recipe(case_id,job_id,principal(authorization))
    filename='recipe-candidate-'+value['bundle_hash'][7:19]+'.json'
    return Response(content=json.dumps(value,ensure_ascii=False,indent=2),media_type='application/json; charset=utf-8',
        headers={'Content-Disposition':f'attachment; filename="{filename}"','Cache-Control':'no-store'})


@app.post("/v1/runs/{run_id}/guidance")
def guidance(run_id: str, body: JsonBody, authorization: str | None = Header(default=None),
             idempotency_key: str | None = Header(default=None)):
    return service.guidance(run_id, principal(authorization), body.data.get("observation", ""),
        candidates=body.data.get("candidates"), node_id=body.data.get("node_id"),
        request_key=idempotency_key or body.data.get("request_key"))


@app.get("/v1/runs/{run_id}/decisions/{decision_id}")
def get_decision(run_id: str, decision_id: str, authorization: str | None = Header(default=None)):
    return service.get_decision(run_id, principal(authorization), decision_id)


@app.post("/v1/runs/{run_id}/checkpoint")
def checkpoint(run_id: str, body: JsonBody, authorization: str | None = Header(default=None)):
    return service.checkpoint(run_id, principal(authorization), body.data)


@app.post("/v1/artifacts")
def artifact(body: dict, authorization: str | None = Header(default=None)):
    owner = principal(authorization)
    return service.store_artifact(owner, str(body.get("name", "")), str(body.get("content", "")))


@app.post("/v1/runs/{run_id}/steps/next")
def next_step(run_id: str, authorization: str | None = Header(default=None)):
    return service.next_step(run_id, principal(authorization))


@app.post("/v1/runs/{run_id}/steps/{attempt_id}/submit")
def submit_step(run_id: str, attempt_id: str, body: dict, authorization: str | None = Header(default=None)):
    return service.submit_step(run_id, principal(authorization), attempt_id,
        str(body.get("attempt_token", "")), body.get("outputs", {}),
        decision_id=body.get("decision_id"), actual_action_id=body.get("actual_action_id"))


@app.post("/v1/runs/{run_id}/complete")
def complete_guided(run_id: str, authorization: str | None = Header(default=None)):
    return service.complete_guided_run(run_id, principal(authorization))


@app.post("/v1/runs/{run_id}/submit")
def submit(run_id: str, body: JsonBody, authorization: str | None = Header(default=None)):
    return service.submit_result(run_id, principal(authorization), body.data)


@app.get("/v1/runs/{run_id}/events")
def events(run_id: str, authorization: str | None = Header(default=None)):
    return {"items": service.get_events(run_id, principal(authorization))}


@app.post("/v1/runs/{run_id}/feedback")
def feedback(run_id: str, body: JsonBody, authorization: str | None = Header(default=None)):
    return service.record_feedback(run_id, principal(authorization), body.data)


@app.post("/v1/runs/{run_id}/session-events")
def session_events(run_id: str, body: dict, authorization: str | None = Header(default=None)):
    return service.capture_session_events(run_id, principal(authorization), body.get("events"))


@app.post("/v1/runs/{run_id}/initial-inputs")
def initial_inputs(run_id: str, body: dict, authorization: str | None = Header(default=None)):
    return service.supply_initial_inputs(run_id, principal(authorization), body.get("artifact_ids"))


@app.post("/v1/improvements/{job_id}/resume")
def resume_improvement(job_id: str, authorization: str | None = Header(default=None)):
    return service.resume_improvement(job_id, principal(authorization))


@app.post("/v1/runs/{run_id}/improvements")
def improve(run_id: str, body: JsonBody, authorization: str | None = Header(default=None),
            idempotency_key: str | None = Header(default=None)):
    return service.start_improvement(run_id, principal(authorization), body.data.get("reason", ""),
                                     idempotency_key or body.data.get("request_key"))


@app.get("/v1/recipes/{task_family}")
def recipe(task_family: str, authorization: str | None = Header(default=None)):
    principal(authorization)
    return service.get_active_recipe(task_family)


@app.get("/v1/improvements/{job_id}")
def get_improvement(job_id: str, authorization: str | None = Header(default=None)):
    return service.get_improvement(job_id, principal(authorization))


@app.post("/internal/admin/v1/improvements/{job_id}/publish")
def publish_improvement(job_id: str, body: dict, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.publish_improvement(job_id, body.get("expected_revision"), body.get("reason", ""),
                                       admin_token=service.admin_token)


@app.post("/internal/admin/v1/recipes/bootstrap")
def bootstrap(body: dict, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.bootstrap_seed(str(body.get("task_family", "general")), str(body.get("reason", "")))


@app.post("/internal/admin/v1/recipes/bootstrap-bundle")
def bootstrap_bundle(body: dict, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.bootstrap_recipe_bundle(str(body.get("task_family", "")), body.get("bundle", {}),
        str(body.get("reason", "")), admin_token=service.admin_token)


@app.post('/internal/admin/v1/runs/{run_id}/recipe-evaluation')
def evaluate_recipe_run(run_id: str, body: dict, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.evaluate_recipe_run(run_id, 'local-codex', body['record'], body['artifact_id'],
                                       body['content_hash'], admin_token=service.admin_token)


@app.post("/internal/admin/v1/runs/{run_id}/decisions/{decision_id}/evaluations")
def evaluate_decision(run_id: str, decision_id: str, body: dict,
                      authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.evaluate_decision(run_id, decision_id, body, admin_token=service.admin_token)


@app.get("/internal/admin/v1/memory-outbox")
def memory_outbox(authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.outbox_status()


@app.post("/internal/admin/v1/memory-outbox/{outbox_id}/retry")
def retry_memory_write(outbox_id: str, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.retry_memory_write(outbox_id, admin_token=service.admin_token)


@app.post("/internal/admin/v1/approvals/{approval_id}/approve")
def approve(approval_id: str, authorization: str | None = Header(default=None)):
    admin(authorization)
    return service.approve(approval_id, admin_token=service.admin_token)


@app.get("/internal/admin/v1/approvals/{approval_id}")
def approval(approval_id: str, authorization: str | None = Header(default=None)):
    admin(authorization)
    with service._db() as db:
        row = db.execute("SELECT id,status,expires,created_at,run_id FROM approvals WHERE id=?", (approval_id,)).fetchone()
    if not row:
        raise HTTPException(404, "approval not found")
    return dict(row)


# FastMCP's ASGI app already owns the /mcp route; mount it at the root so the
# public endpoint remains exactly /mcp instead of becoming /mcp/mcp.
app.mount("/memory", memory_app)
app.mount("/", mcp.streamable_http_app())
