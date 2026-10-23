"""Leased, fenced, bounded retry worker. No hidden unbounded retry loops."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

from .models import AccessDenied, AuthContext, CompileInput, Conflict, GraphDelta, InvalidEvidence, timestamp
from .service import MemoryService, digest, refs_from
from .verification import prepare_verification


class Worker:
    def __init__(
        self,
        service: MemoryService,
        compiler,
        *,
        graph_sink=None,
        verifier=None,
        embedder=None,
        vector_index=None,
        lease_seconds=300,
        max_attempts=3,
        token_budget=3000,
    ):
        if lease_seconds < 1 or max_attempts < 1 or not 1 <= token_budget <= 16000:
            raise ValueError("invalid worker limits")
        self.service = service
        self.db = service.db
        self.compiler = compiler
        self.graph_sink = graph_sink
        self.verifier = verifier
        self.embedder = embedder
        self.vector_index = vector_index
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.token_budget = token_budget

    def claim(self):
        now = self.service.clock()
        with self.db.transaction():
            # Exhausted workers that crashed at the attempt limit must not stay running forever.
            self.db.execute(
                """UPDATE jobs SET state='failed',error_code='LeaseExpired' WHERE state='running'
                AND lease_until<=? AND attempts>=?""",
                (now, self.max_attempts),
            )
            suffix = " FOR UPDATE SKIP LOCKED" if self.db.dialect == "postgres" else ""
            row = self.db.one(
                """SELECT * FROM jobs WHERE attempts<? AND
                (state IN ('queued','retryable') OR (state='running' AND lease_until<=?))
                ORDER BY created_at,id LIMIT 1"""
                + suffix,
                (self.max_attempts, now),
            )
            if not row:
                return None
            token = str(uuid4())
            until = timestamp(datetime.fromisoformat(now) + timedelta(seconds=self.lease_seconds))
            self.db.execute(
                """UPDATE jobs SET state='running',attempts=attempts+1,lease_token=?,lease_until=?,error_code=NULL
                WHERE tenant=? AND project=? AND id=?""",
                (token, until, row["tenant"], row["project"], row["id"]),
            )
            return {**row, "lease_token": token, "lease_until": until, "attempts": row["attempts"] + 1}

    def _fence(self, job):
        row = self.db.one(
            "SELECT * FROM jobs WHERE tenant=? AND project=? AND id=?", (job["tenant"], job["project"], job["id"])
        )
        if (
            not row
            or row["state"] != "running"
            or row["lease_token"] != job["lease_token"]
            or row["lease_until"] <= self.service.clock()
        ):
            raise Conflict("worker lease expired or cancelled")

    def _finish(self, job, state, error=None):
        self.db.execute(
            """UPDATE jobs SET state=?,error_code=?,lease_token=NULL,lease_until=NULL
            WHERE tenant=? AND project=? AND id=? AND lease_token=? AND state='running'""",
            (state, error, job["tenant"], job["project"], job["id"], job["lease_token"]),
        )

    def heartbeat(self, job):
        with self.db.transaction():
            self._fence(job)
            until = timestamp(datetime.fromisoformat(self.service.clock()) + timedelta(seconds=self.lease_seconds))
            self.db.execute(
                "UPDATE jobs SET lease_until=? WHERE tenant=? AND project=? AND id=? AND lease_token=?",
                (until, job["tenant"], job["project"], job["id"], job["lease_token"]),
            )

    def process(self, job):
        actor = AuthContext(job["tenant"], job["principal"])
        project = job["project"]
        payload = json.loads(job["payload"])
        try:
            if job["kind"] == "compile":
                with self.db.transaction():
                    self._fence(job)
                    self.service._scope(actor, project, write=True)
                    source = self.service._source(actor, project, payload["source_id"], revision=payload["revision"])
                manifest = CompileInput(
                    source["id"], source["revision"], source["content"], source["content_hash"], self.token_budget
                )
                drafts = deepcopy(self.compiler.compile(manifest))  # Freeze nested candidate metadata too.
                if sum(len(d.text) for d in drafts) > self.token_budget * 4:
                    raise InvalidEvidence("compiler output exceeds budget")
                if any(r.source_id != source["id"] or r.revision != source["revision"] for d in drafts for r in d.refs):
                    raise InvalidEvidence("compiler crossed source manifest")
                prepared_verifier = None
                if self.verifier is not None:
                    with self.db.transaction():
                        self._fence(job)
                        self.service._scope(actor, project, write=True)
                        evidence_texts = [self.service._validate_refs(actor, project, d.refs) for d in drafts]

                    # External verification may block or call a model; never hold the DB transaction.
                    def verification_checkpoint():
                        self.heartbeat(job)
                        with self.db.transaction():
                            self._fence(job)
                            self.service._scope(actor, project, write=True)
                            self.service._source(actor, project, source["id"], revision=source["revision"])

                    prepared_verifier = prepare_verification(
                        self.verifier, drafts, evidence_texts, checkpoint=verification_checkpoint
                    )
                # Revoke or overwrite while the LLM was running: commit must fail closed.
                with self.db.transaction():
                    self.db.lock_project(actor.tenant, project)
                    self._fence(job)
                    self.service._scope(actor, project, write=True)
                    self.service._source(actor, project, source["id"], revision=source["revision"])
                    self.service._commit(
                        actor,
                        project,
                        drafts,
                        compiler_version=f"compiler:{self.compiler.version}",
                        verifier=prepared_verifier,
                    )
                    self._finish(job, "committed")
            elif job["kind"] == "graph":
                with self.db.transaction():
                    self.db.lock_project(actor.tenant, project)
                    self._fence(job)
                    head = self.db.one(
                        "SELECT * FROM assertions WHERE tenant=? AND project=? AND id=?",
                        (actor.tenant, project, payload["assertion_id"]),
                    )
                    if not head:
                        raise InvalidEvidence("projection assertion missing")
                    row = self.db.one(
                        "SELECT * FROM assertion_versions WHERE tenant=? AND project=? AND id=? AND revision=?",
                        (actor.tenant, project, head["id"], head["revision"]),
                    )
                    # Projection is internal and not a grant to readers. Public reads recheck ACL.
                    active = not head["revoked"] and row["status"] == "supported"
                    for ref in refs_from(row["refs"]):
                        source = self.db.one(
                            "SELECT revision,state FROM sources WHERE tenant=? AND project=? AND id=?",
                            (actor.tenant, project, ref.source_id),
                        )
                        active = active and bool(
                            source and source["state"] == "active" and source["revision"] == ref.revision
                        )
                    delta = GraphDelta(
                        actor.tenant,
                        project,
                        head["id"],
                        head["sequence"],
                        row["subject"] if active else "",
                        row["predicate"] if active else "",
                        row["value"] if active else "",
                        row["kind"],
                        bool(active),
                    )
                if self.graph_sink:
                    self.graph_sink.apply(delta)  # Idempotent version-fenced side effect; retried after crash.
                with self.db.transaction():
                    self._fence(job)
                    self.db.execute(
                        """INSERT INTO graph_edges VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(tenant,project,assertion_id) DO UPDATE SET version=excluded.version,
                        subject=excluded.subject,predicate=excluded.predicate,value=excluded.value,
                        kind=excluded.kind,active=excluded.active WHERE graph_edges.version<excluded.version""",
                        (
                            delta.tenant,
                            delta.project,
                            delta.assertion_id,
                            delta.version,
                            delta.subject,
                            delta.predicate,
                            delta.value,
                            delta.kind,
                            int(delta.active),
                        ),
                    )
                    self._finish(job, "committed")
            elif job["kind"] == "embed":
                if self.embedder is None or self.vector_index is None:
                    raise InvalidEvidence("embedding adapter not configured")
                if getattr(self.embedder, "model", None) != self.vector_index.model:
                    raise InvalidEvidence("embedding model does not match index space")
                with self.db.transaction():
                    self._fence(job)
                    self.service._scope(actor, project, write=True)
                    self.service._source(actor, project, payload["source_id"], revision=payload["revision"])
                    chunks = self.db.all(
                        """SELECT * FROM chunks WHERE tenant=? AND project=? AND source_id=?
                        AND source_revision=? ORDER BY start_offset""",
                        (actor.tenant, project, payload["source_id"], payload["revision"]),
                    )
                    pending = [
                        c
                        for c in chunks
                        if not self.vector_index.contains(actor.tenant, project, c["id"], digest(c["text"]))
                    ]
                for start in range(0, len(pending), 16):
                    self.heartbeat(job)
                    batch = pending[start : start + 16]
                    vectors = self.embedder.embed([c["text"] for c in batch])
                    if len(vectors) != len(batch):
                        raise InvalidEvidence("embedding response count mismatch")
                    with self.db.transaction():
                        self._fence(job)
                        self.service._scope(actor, project, write=True)
                        self.service._source(actor, project, payload["source_id"], revision=payload["revision"])
                    for chunk, vector in zip(batch, vectors):
                        self.vector_index.upsert(actor.tenant, project, chunk["id"], digest(chunk["text"]), vector)
                with self.db.transaction():
                    self._fence(job)
                    self.service._scope(actor, project, write=True)
                    self.service._source(actor, project, payload["source_id"], revision=payload["revision"])
                    self._finish(job, "committed")
            else:
                raise InvalidEvidence("unknown job kind")
        except (AccessDenied, InvalidEvidence) as error:
            with self.db.transaction():
                self._finish(job, "quarantined", type(error).__name__)
        except Conflict as error:
            with self.db.transaction():
                self._finish(job, "quarantined", type(error).__name__)
        except Exception as error:
            # Persist only exception class; provider bodies can contain secrets or private text.
            state = "failed" if job["attempts"] >= self.max_attempts else "retryable"
            with self.db.transaction():
                self._finish(job, state, type(error).__name__)

    def run_once(self) -> bool:
        job = self.claim()
        if not job:
            return False
        self.process(job)
        return True

    def drain(self, *, max_jobs=100):
        count = 0
        while count < max_jobs and self.run_once():
            count += 1
        return count

    def cancel(self, actor, project, job_id):
        with self.db.transaction():
            self.service._scope(actor, project, write=True)
            job = self.db.one(
                "SELECT principal FROM jobs WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, job_id)
            )
            if not job or job["principal"] != actor.principal:
                raise AccessDenied("job unavailable")
            self.db.execute(
                """UPDATE jobs SET state='cancelled',lease_token=NULL,lease_until=NULL
                WHERE tenant=? AND project=? AND id=? AND state IN ('queued','running','retryable')""",
                (actor.tenant, project, job_id),
            )
