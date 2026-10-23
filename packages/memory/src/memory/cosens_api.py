"""Private HTTP adapter used only by the CoSens RSI service.

It deliberately does not expose a Memory MCP server. The RSI service owns the
only credential and constructs AuthContext from deployment configuration.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from memory import (AccessDenied, AuthContext, Conflict, ExperienceService, ExtractiveCompiler,
                           IngestRoute, InvalidEvidence, MemoryService, Store, Worker)
from .recipe_experiences import RecipeExperienceService


class IngestBody(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    request_key: str = Field(min_length=1, max_length=256)
    source_id: str | None = None
    expected_revision: int = 0
    durable: bool = False
    source_kind: str = "message"


class ExperienceBody(BaseModel):
    record: dict
    request_key: str = Field(min_length=1, max_length=256)
    experience_id: str | None = None
    expected_revision: int = Field(default=0, ge=0)


class OutcomeBody(BaseModel):
    outcome: dict
    request_key: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)


def make_app() -> FastAPI:
    db_path = os.environ.get("MEMORY_DB", "./var/memory.sqlite3")
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    secret = os.environ["COSENS_MEMORY_INTERNAL_TOKEN"]
    actor = AuthContext(
        os.environ.get("COSENS_MEMORY_TENANT", "local"),
        os.environ.get("COSENS_MEMORY_PRINCIPAL", "cosens-rsi"),
        administrator=True,
    )
    project = os.environ.get("COSENS_MEMORY_PROJECT", "default")
    store = Store.sqlite(db_path)
    store.migrate()
    service = MemoryService(store)
    experiences = ExperienceService(service)
    recipe_experiences = RecipeExperienceService(service)
    try:
        service.create_project(actor, project, "CoSens local project")
    except Exception as exc:
        if "project exists" not in str(exc):
            raise
    worker = Worker(service, ExtractiveCompiler())
    stop = threading.Event()

    def work():
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(0.4)

    @asynccontextmanager
    async def lifespan(_app):
        thread = threading.Thread(target=work, name="memory-worker", daemon=True)
        thread.start()
        yield
        stop.set()
        thread.join(timeout=3)
        store.close()

    app = FastAPI(title="Memory internal RSI adapter", version="1.0.0", lifespan=lifespan)
    app.state.memory_service = service
    app.state.experiences = experiences
    app.state.recipe_experiences = recipe_experiences
    app.state.memory_actor = actor
    app.state.memory_project = project

    @app.exception_handler(AccessDenied)
    async def denied(_request, _exc):
        return JSONResponse(status_code=403, content={"error": "resource unavailable"})

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(InvalidEvidence)
    async def invalid_evidence(_request, exc):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @app.exception_handler(ValueError)
    async def invalid_request(_request, exc):
        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(LookupError)
    async def missing(_request, _exc):
        return JSONResponse(status_code=404, content={"error": "resource unavailable"})

    def authorize(value: str | None):
        if value != f"Bearer {secret}":
            raise HTTPException(401, "unauthorized")

    @app.get("/internal/v1/health")
    def health(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"status": "ok", "database": "sqlite-local", "project": project, "worker": "running"}

    @app.post("/internal/v1/recipe-experiences/observe")
    def observe_recipe(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return recipe_experiences.observe(actor, project, **body)

    @app.post("/internal/v1/recipe-experiences/learning-view")
    def recipe_learning_view(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return recipe_experiences.learning_view(actor, project, **body)

    @app.post("/internal/v1/recipe-experiences/recall")
    def recall_recipe(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return recipe_experiences.recall(actor, project, **body)

    @app.get("/internal/v1/experiences/watermark")
    def experience_watermark(authorization: str | None = Header(default=None)):
        authorize(authorization)
        return {"watermark": experiences.watermark(actor, project)}

    @app.post("/internal/v1/experiences/upsert")
    def save_experience(body: ExperienceBody, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return experiences.upsert(actor, project, **body.model_dump())

    @app.post("/internal/v1/experiences/search")
    def search_experience(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        # Normal runtime callers cannot widen retrieval into evaluation sets.
        allowed = {"task_family", "phase", "environment", "action_schema", "reward_spec",
                   "query", "watermark", "limit", "exclude_run_id", "contract_hash"}
        if set(body) - allowed:
            raise ValueError("unsupported experience search options")
        required = {"task_family", "phase", "environment", "action_schema", "reward_spec", "query"}
        if not required <= set(body):
            raise ValueError("experience search scope is incomplete")
        return experiences.search(actor, project, **body)

    @app.get("/internal/v1/experiences/{experience_id}")
    def read_experience(experience_id: str, revision: int | None = None,
                        authorization: str | None = Header(default=None)):
        authorize(authorization)
        return experiences.get(actor, project, experience_id, revision=revision)

    @app.post("/internal/v1/experiences/{experience_id}/outcomes")
    def outcome(experience_id: str, body: OutcomeBody, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return experiences.append_outcome(actor, project, experience_id, **body.model_dump())

    @app.post("/internal/v1/experiences/{experience_id}/withdraw")
    def withdraw_experience(experience_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        experiences.withdraw(actor, project, experience_id)
        return {"experience_id": experience_id, "status": "withdrawn"}

    @app.post("/internal/v1/wiki/search")
    def wiki_search(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        query = str(body.get("query", "")).strip()
        if not query:
            raise HTTPException(422, "query required")
        data = service.read_wiki(actor, project, level=1)
        import re
        query_terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", query.lower()))
        scored = []
        for i, block in enumerate(data["blocks"]):
            text_terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", block["text"].lower()))
            scored.append((len(query_terms & text_terms), i))
        order = [i for score, i in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
        items = [data["blocks"][i] for i in order[:8]]
        return {"status": "matched" if items else "empty", "items": items,
                "coverage": data["coverage"], "source_count_covered": data["source_count_covered"],
                "profile": "wiki-first-lexical-dev"}

    @app.post("/internal/v1/recall")
    def recall(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        query = str(body.get("query", "")).strip()
        result = service.recall(actor, project, query, token_budget=min(int(body.get("token_budget", 1500)), 8000))
        return {"query": result.query, "items": [
            {"id": x.id, "kind": x.kind, "text": x.text, "refs": [r.__dict__ for r in x.refs],
             "revision": x.revision, "score": x.score} for x in result.items
        ], "estimated_tokens": result.estimated_tokens, "diagnostics": result.diagnostics}

    @app.post("/internal/v1/ingest")
    def ingest(body: IngestBody, authorization: str | None = Header(default=None)):
        authorize(authorization)
        receipt = service.ingest(actor, project, body.content, request_key=body.request_key,
            source_id=body.source_id, expected_revision=body.expected_revision,
            route=IngestRoute(source_kind=body.source_kind, durable=body.durable))
        return {"source_id": receipt.source_id, "revision": receipt.revision,
                "job_id": receipt.job_id, "content_hash": receipt.content_hash, "state": receipt.state}

    @app.get("/internal/v1/jobs/{job_id}")
    def job_status(job_id: str, authorization: str | None = Header(default=None)):
        authorize(authorization)
        return service.job_status(actor, project, job_id)

    @app.post("/internal/v1/evidence/expand")
    def expand(body: dict, authorization: str | None = Header(default=None)):
        authorize(authorization)
        refs = body.get("refs", [])
        items = []
        for ref in refs[:12]:
            source = service.read_source(actor, project, ref["source_id"])
            if source["revision"] != int(ref["revision"]) or source["content_hash"] != ref["content_hash"]:
                raise HTTPException(409, "source version changed")
            start, end = int(ref["start"]), int(ref["end"])
            if not 0 <= start < end <= len(source["content"]):
                raise HTTPException(422, "invalid source span")
            items.append({"ref": ref, "text": source["content"][start:end]})
        return {"items": items, "validated_live": True}

    return app


app = make_app()
