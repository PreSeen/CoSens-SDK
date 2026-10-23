"""Authoritative memory operations and live access/validity enforcement."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from uuid import uuid4

from .models import (
    AccessDenied,
    AuthContext,
    ClaimDraft,
    Conflict,
    ContextAnchor,
    Evidence,
    EvidenceBundle,
    InvalidEvidence,
    Receipt,
    SourceRef,
    timestamp,
)
from .retrieval import RetrievalConfig, lexemes, passage, rank, shortlist_fingerprint, rerank_shortlist
from .pool_retrieval import source_preserving_shortlist, candidate_pool_fingerprints
from .store import Store
from .segmentation import index_spans
from .routing import IngestRoute, route_ingest


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def refs_from(value: str) -> tuple[SourceRef, ...]:
    return tuple(SourceRef(**item) for item in json.loads(value))


def terms(text: str) -> set[str]:
    result = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        result.update(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return result - {"the", "is", "a", "an", "of", "to", "was", "what", "why", "who", "how"}


def tokens(text: str) -> int:
    # Conservative estimate, not the provider's billing tokenizer.
    return max(len(text.split()), (len(text) + 2) // 3)


def evidence_cost(group, *, preceding=False):
    """One exact rendered-group cost for feasibility and final packing."""
    rendered = [f'[{entry.kind}:{entry.id}@{entry.revision}] {entry.text}' for entry in group]
    separators = max(0,len(group)-1) + bool(preceding and group)
    return (sum(tokens(text) for text in rendered)+separators,
            sum(len(text) for text in rendered)+2*separators)


class MemoryService:
    def __init__(
        self,
        store: Store,
        *,
        clock=timestamp,
        single_valued_predicates=("OWNER", "RESPONSIBLE", "STATUS", "DEADLINE"),
        enable_embedding_jobs=False,
    ):
        self.db = store
        self.clock = clock
        self.single_valued_predicates = frozenset(single_valued_predicates)
        self.enable_embedding_jobs = enable_embedding_jobs

    def _scope(self, actor: AuthContext, project: str, *, write=False, owner=False):
        # Serialize project ACL changes with in-flight public operations. This is
        # deliberately conservative; future read replicas need an ACL epoch gate.
        self.db.lock_project(actor.tenant, project)
        row = self.db.one(
            "SELECT role FROM members WHERE tenant=? AND project=? AND principal=?",
            (actor.tenant, project, actor.principal),
        )
        role = row["role"] if row else None
        if role not in {"reader", "editor", "owner"}:
            raise AccessDenied("project unavailable")
        if (write and role not in {"editor", "owner"}) or (owner and role != "owner"):
            raise AccessDenied("insufficient project role")
        return role

    def create_project(self, actor: AuthContext, project: str, title: str) -> None:
        if not actor.administrator:
            raise AccessDenied("project provisioning requires trusted administrator")
        if not all((actor.tenant, actor.principal, project, title)):
            raise ValueError("empty identity or project")
        with self.db.transaction():
            if self.db.one("SELECT id FROM projects WHERE tenant=? AND id=?", (actor.tenant, project)):
                raise Conflict("project exists")
            self.db.execute("INSERT INTO projects(tenant,id,title) VALUES(?,?,?)", (actor.tenant, project, title))
            self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (actor.tenant, project, actor.principal, "owner"))

    def set_member(self, actor: AuthContext, project: str, principal: str, role: str | None) -> None:
        if role not in {None, "reader", "editor", "owner"}:
            raise ValueError("invalid role")
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, owner=True)
            if principal == actor.principal and role != "owner":
                raise Conflict("owner cannot remove own authority")
            self.db.execute(
                "DELETE FROM members WHERE tenant=? AND project=? AND principal=?", (actor.tenant, project, principal)
            )
            if role:
                self.db.execute("INSERT INTO members VALUES(?,?,?,?)", (actor.tenant, project, principal, role))

    def _sequence(self, tenant: str, project: str) -> int:
        self.db.execute("UPDATE projects SET sequence=sequence+1 WHERE tenant=? AND id=?", (tenant, project))
        return self.db.one("SELECT sequence FROM projects WHERE tenant=? AND id=?", (tenant, project))["sequence"]

    def _job(self, actor: AuthContext, project: str, kind: str, payload: dict) -> str:
        job = str(uuid4())
        self.db.execute(
            """INSERT INTO jobs(tenant,project,id,kind,payload,principal,state,created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (actor.tenant, project, job, kind, dump(payload), actor.principal, "queued", self.clock()),
        )
        return job

    def ingest(
        self,
        actor: AuthContext,
        project: str,
        content: str,
        *,
        request_key: str,
        source_id: str | None = None,
        expected_revision: int = 0,
        readers: tuple[str, ...] = (),
        context_anchors: tuple[ContextAnchor, ...] = (),
        chunk_policy: str = 'fixed',
        route: IngestRoute | None = None,
    ) -> Receipt:
        if not content.strip() or len(content) > 2_000_000 or not request_key:
            raise ValueError("content must be nonempty, <=2M characters; request_key required")
        request_key = digest(request_key)  # Keys can contain private caller text; never retain it verbatim.
        acl = sorted(set(readers))
        if acl and actor.principal not in acl:
            raise AccessDenied("writer must be able to read source")
        if len(context_anchors) > 10000:
            raise ValueError("too many context anchors")
        for anchor in context_anchors:
            if not isinstance(anchor, ContextAnchor):
                raise ValueError("invalid context anchor")
            anchor.validate(len(content))
        anchors = sorted(set(context_anchors), key=lambda a: (a.start, a.end, a.scope_start, a.scope_end))
        spans = index_spans(content, anchors, policy=chunk_policy)
        # Preserve the historical API contract for callers that have not
        # opted into layered routing yet.  New hosts pass an explicit
        # ``IngestRoute``; an ordinary ``IngestRoute()`` is the hot/raw path.
        route = route if route is not None else IngestRoute(durable=True)
        decision = route_ingest(route)
        fingerprint_data = [content, source_id, expected_revision, acl, route.payload()]
        if anchors:
            fingerprint_data.append([asdict(a) for a in anchors])
        if chunk_policy != 'fixed':
            fingerprint_data.append({'chunk_policy':chunk_policy})
        fingerprint = digest(dump(fingerprint_data))
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            previous = self.db.one(
                "SELECT * FROM receipts WHERE tenant=? AND project=? AND principal=? AND request_key=?",
                (actor.tenant, project, actor.principal, request_key),
            )
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("idempotency key reused with different request")
                receipt = Receipt(**json.loads(previous["result"]))
                self._source(actor, project, receipt.source_id)
                return receipt
            source_id = source_id or str(uuid4())
            head = self.db.one(
                "SELECT * FROM sources WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, source_id)
            )
            if head:
                self._source(actor, project, source_id)
                if head["owner"] != actor.principal:
                    raise AccessDenied("only source owner may revise source")
                if head["revision"] != expected_revision:
                    raise Conflict("source revision changed")
                if json.loads(head["readers"]) != acl:
                    raise Conflict("use set_source_readers for access changes")
            elif expected_revision:
                raise Conflict("source does not exist")
            revision = expected_revision + 1
            content_hash = digest(content)
            if head:
                self.db.execute(
                    "UPDATE sources SET revision=? WHERE tenant=? AND project=? AND id=?",
                    (revision, actor.tenant, project, source_id),
                )
                self._invalidate_source(actor, project, source_id)
            else:
                self.db.execute(
                    "INSERT INTO sources VALUES(?,?,?,?,?,?,?)",
                    (actor.tenant, project, source_id, revision, "active", dump(acl), actor.principal),
                )
            self.db.execute(
                "INSERT INTO source_versions VALUES(?,?,?,?,?,?,?)",
                (actor.tenant, project, source_id, revision, content, content_hash, self.clock()),
            )
            # Cheap raw indexing does not wait for LLM-based knowledge compilation.
            if anchors:
                self.db.execute("INSERT INTO source_context VALUES(?,?,?,?,?)",
                                (actor.tenant, project, source_id, revision, dump([asdict(a) for a in anchors])))
            for start, end in spans:
                chunk_key = f"{source_id}:{revision}:{start}"
                if chunk_policy == 'units' or (chunk_policy != 'fixed' and anchors):
                    chunk_key += f":{chunk_policy}:{end}"
                chunk_id = digest(chunk_key)
                self.db.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)",
                    (actor.tenant, project, chunk_id, source_id, revision, start, end, content[start:end]),
                )
            self.db.execute(
                "INSERT INTO source_routes VALUES(?,?,?,?,?,?)",
                (actor.tenant, project, source_id, revision, decision.layer.value, dump(decision.payload())),
            )
            self._sequence(actor.tenant, project)
            job = (
                self._job(actor, project, "compile", {"source_id": source_id, "revision": revision})
                if decision.enqueue_compile
                else None
            )
            if self.enable_embedding_jobs:
                self._job(actor, project, "embed", {"source_id": source_id, "revision": revision})
            receipt = Receipt(source_id, revision, job, content_hash, state=decision.layer.value)
            self.db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?,?)",
                (actor.tenant, project, actor.principal, request_key, fingerprint, dump(asdict(receipt))),
            )
            return receipt

    def _source(
        self,
        actor: AuthContext,
        project: str,
        source_id: str,
        *,
        revision: int | None = None,
        known_at: str | None = None,
        historical=False,
    ) -> dict:
        head = self.db.one(
            "SELECT * FROM sources WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, source_id)
        )
        if not head or head["state"] != "active":
            raise AccessDenied("source unavailable")
        readers = json.loads(head["readers"])
        if readers and actor.principal not in readers:
            raise AccessDenied("source unavailable")
        if historical and revision is not None:
            row = self.db.one(
                """SELECT * FROM source_versions WHERE tenant=? AND project=? AND id=?
                AND revision=? AND recorded_at<=?""",
                (actor.tenant, project, source_id, revision, known_at or self.clock()),
            )
        elif known_at:
            row = self.db.one(
                """SELECT * FROM source_versions WHERE tenant=? AND project=? AND id=?
                AND recorded_at<=? ORDER BY revision DESC LIMIT 1""",
                (actor.tenant, project, source_id, known_at),
            )
        else:
            row = self.db.one(
                "SELECT * FROM source_versions WHERE tenant=? AND project=? AND id=? AND revision=?",
                (actor.tenant, project, source_id, head["revision"]),
            )
        if not row or (revision is not None and row["revision"] != revision):
            raise InvalidEvidence("source revision no longer eligible")
        return row

    def read_source(self, actor: AuthContext, project: str, source_id: str, *, known_at: str | None = None) -> dict:
        with self.db.transaction():
            self._scope(actor, project)
            return self._source(actor, project, source_id, known_at=timestamp(known_at) if known_at else None)

    def _validate_refs(
        self, actor: AuthContext, project: str, refs: tuple[SourceRef, ...], *, known_at=None, historical=False
    ) -> list[str]:
        if not refs:
            raise InvalidEvidence("at least one source reference required")
        texts = []
        for ref in refs:
            source = self._source(
                actor, project, ref.source_id, revision=ref.revision, known_at=known_at, historical=historical
            )
            if source["content_hash"] != ref.content_hash or not (0 <= ref.start < ref.end <= len(source["content"])):
                raise InvalidEvidence("source hash or span mismatch")
            texts.append(source["content"][ref.start : ref.end])
        return texts

    def commit(
        self,
        actor: AuthContext,
        project: str,
        drafts: list[ClaimDraft],
        *,
        compiler_version: str = "human-reviewed",
        verifier=None,
    ) -> list[dict]:
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            return self._commit(actor, project, drafts, compiler_version=compiler_version, verifier=verifier)

    def _commit(self, actor, project, drafts, *, compiler_version, verifier=None):
        if len(drafts) > 100:
            raise ValueError("changeset limited to 100 assertions")
        prepared = []
        for draft in drafts:
            if draft.kind not in {"fact", "decision", "risk", "procedure", "hypothesis"} or draft.status not in {
                "proposed",
                "supported",
                "disputed",
            }:
                raise ValueError("unknown kind or status")
            if not all((draft.subject.strip(), draft.predicate.strip(), draft.value.strip(), draft.text.strip())):
                raise InvalidEvidence("empty assertion")
            if (
                len(draft.text) > 20000
                or any(len(value) > 2048 for value in (draft.subject, draft.predicate, draft.value))
                or len(draft.refs) > 32
            ):
                raise InvalidEvidence("assertion exceeds size limits")
            if len(dump(draft.metadata)) > 64000:
                raise InvalidEvidence("assertion metadata exceeds size limit")
            texts = self._validate_refs(actor, project, draft.refs)
            if compiler_version != "human-reviewed":
                if (
                    draft.kind != "hypothesis"
                    and draft.status != "disputed"
                    and verifier is not None
                    and verifier.supports(draft, texts)
                ):
                    draft = replace(draft, status="supported")
                elif draft.status == "supported":
                    draft = replace(draft, status="proposed")
            if draft.kind == "hypothesis" and draft.status == "supported":
                raise InvalidEvidence("hypothesis cannot silently become supported fact")
            if draft.kind == "procedure" and not all(
                draft.metadata.get(k) for k in ("preconditions", "steps", "validation")
            ):
                raise InvalidEvidence("procedures require preconditions, steps and validation record")
            start = timestamp(draft.valid_from) if draft.valid_from else self.clock()
            end = timestamp(draft.valid_to) if draft.valid_to else None
            if end and end <= start:
                raise ValueError("invalid validity interval")
            assertion_id = draft.assertion_id or str(uuid4())
            if any(item[1] == assertion_id for item in prepared):
                raise Conflict("duplicate assertion in changeset")
            head = self.db.one(
                "SELECT * FROM assertions WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, assertion_id)
            )
            if head:
                if head["revoked"] or head["revision"] != draft.expected_revision:
                    raise Conflict("assertion revision changed or revoked")
                previous = self.db.one(
                    "SELECT * FROM assertion_versions WHERE tenant=? AND project=? AND id=? AND revision=?",
                    (actor.tenant, project, assertion_id, head["revision"]),
                )
                # Authorization checks old sources, but old revisions may be stale:
                # repairing a stale assertion must remain possible.
                for ref in refs_from(previous["refs"]):
                    self._source(actor, project, ref.source_id)
                if (draft.subject, draft.predicate) != (previous["subject"], previous["predicate"]):
                    raise Conflict("assertion identity cannot change semantic slot")
            elif draft.expected_revision:
                raise Conflict("assertion does not exist")
            elif draft.status == "supported" and draft.predicate in self.single_valued_predicates:
                conflicts = self.db.all(
                    """SELECT v.* FROM assertion_versions v JOIN assertions a
                    ON a.tenant=v.tenant AND a.project=v.project AND a.id=v.id AND a.revision=v.revision
                    WHERE v.tenant=? AND v.project=? AND a.revoked=0 AND v.status='supported'
                    AND v.subject=? AND v.predicate=? AND v.value<>?""",
                    (actor.tenant, project, draft.subject, draft.predicate, draft.value),
                )
                overlapping = any(
                    (not end or r["valid_from"] < end) and (not r["valid_to"] or start < r["valid_to"])
                    for r in conflicts
                )
                overlapping = overlapping or any(
                    d.status == "supported"
                    and (d.subject, d.predicate) == (draft.subject, draft.predicate)
                    and d.value != draft.value
                    and (not end or s < end)
                    and (not e or start < e)
                    for d, _, s, e, _ in prepared
                )
                if overlapping:
                    draft = replace(draft, status="disputed")
            prepared.append((draft, assertion_id, start, end, head))
        if not prepared:
            return []
        sequence = self._sequence(actor.tenant, project)
        results = []
        for draft, assertion_id, start, end, head in prepared:
            revision = draft.expected_revision + 1
            if head:
                self.db.execute(
                    "UPDATE assertions SET revision=?,sequence=? WHERE tenant=? AND project=? AND id=?",
                    (revision, sequence, actor.tenant, project, assertion_id),
                )
            else:
                self.db.execute(
                    "INSERT INTO assertions VALUES(?,?,?,?,?,?)",
                    (actor.tenant, project, assertion_id, revision, 0, sequence),
                )
            self.db.execute(
                "INSERT INTO assertion_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    actor.tenant,
                    project,
                    assertion_id,
                    revision,
                    draft.subject,
                    draft.predicate,
                    draft.value,
                    draft.text,
                    draft.kind,
                    draft.status,
                    dump([asdict(r) for r in draft.refs]),
                    dump(draft.metadata),
                    start,
                    end,
                    self.clock(),
                    actor.principal,
                ),
            )
            for ref in draft.refs:
                self.db.execute(
                    "INSERT INTO dependencies VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (actor.tenant, project, assertion_id, ref.source_id, ref.revision),
                )
            block_id = f"assertion:{assertion_id}"
            existing = self.db.one(
                "SELECT * FROM wiki_blocks WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, block_id)
            )
            if draft.status == "supported" and (not existing or existing["managed_by"] == "compiler"):
                self._patch(
                    actor,
                    project,
                    block_id,
                    page=draft.kind,
                    text=draft.text,
                    refs=draft.refs,
                    dependencies={assertion_id: revision},
                    expected_revision=existing["revision"] if existing else 0,
                    managed_by="compiler",
                    watermark=sequence,
                )
            elif existing:
                self.db.execute(
                    "UPDATE wiki_blocks SET state='stale' WHERE tenant=? AND project=? AND id=?",
                    (actor.tenant, project, block_id),
                )
            self._job(actor, project, "graph", {"assertion_id": assertion_id})
            results.append({"id": assertion_id, "revision": revision, "status": draft.status})
        # Manifest deliberately excludes text: privacy deletion need not chase copies of source content.
        self.db.execute(
            "INSERT INTO changesets VALUES(?,?,?,?,?,?,?)",
            (
                actor.tenant,
                project,
                str(uuid4()),
                sequence,
                "committed",
                dump({"compiler": compiler_version, "assertions": results}),
                self.clock(),
            ),
        )
        return results

    def _patch(
        self, actor, project, block_id, *, page, text, refs, dependencies, expected_revision, managed_by, watermark
    ):
        self._validate_refs(actor, project, refs)
        old = self.db.one(
            "SELECT * FROM wiki_blocks WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, block_id)
        )
        if (old["revision"] if old else 0) != expected_revision:
            raise Conflict("wiki revision changed")
        if old and old["managed_by"] == "human" and managed_by != "human":
            raise Conflict("human block is protected")
        if old:
            for ref in refs_from(old["refs"]):
                self._source(actor, project, ref.source_id)
        revision = expected_revision + 1
        self.db.execute(
            """INSERT INTO wiki_blocks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tenant,project,id) DO UPDATE SET revision=excluded.revision,page=excluded.page,
            text=excluded.text,managed_by=excluded.managed_by,author=excluded.author,refs=excluded.refs,
            dependencies=excluded.dependencies,watermark=excluded.watermark,state=excluded.state""",
            (
                actor.tenant,
                project,
                block_id,
                revision,
                page,
                text,
                managed_by,
                actor.principal,
                dump([asdict(r) for r in refs]),
                dump(dependencies),
                watermark,
                "published",
            ),
        )
        self.db.execute(
            "INSERT INTO wiki_revisions VALUES(?,?,?,?,?,?,?)",
            (actor.tenant, project, block_id, revision, text, dump([asdict(r) for r in refs]), self.clock()),
        )

    def edit_wiki(self, actor, project, block_id, *, page, text, refs, expected_revision):
        """Human-authored prose, not an authorization or silent fact mutation."""
        if not text.strip() or len(text) > 20000:
            raise ValueError("invalid wiki block length")
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            sequence = self._sequence(actor.tenant, project)
            old = self.db.one(
                "SELECT dependencies FROM wiki_blocks WHERE tenant=? AND project=? AND id=?",
                (actor.tenant, project, block_id),
            )
            dependencies = json.loads(old["dependencies"]) if old else {}
            if dependencies:
                current = {r["id"]: r["revision"] for r in self._claims(actor, project)}
                if not set(dependencies) <= current.keys():
                    raise InvalidEvidence("wiki dependency is not currently eligible")
                dependencies = {key: current[key] for key in dependencies}
            self._patch(
                actor,
                project,
                block_id,
                page=page,
                text=text,
                refs=tuple(refs),
                dependencies=dependencies,
                expected_revision=expected_revision,
                managed_by="human",
                watermark=sequence,
            )

    def _invalidate_source(self, actor, project, source_id):
        affected = self.db.all(
            "SELECT DISTINCT assertion_id FROM dependencies WHERE tenant=? AND project=? AND source_id=?",
            (actor.tenant, project, source_id),
        )
        sequence = self._sequence(actor.tenant, project)
        for row in affected:
            self.db.execute(
                "UPDATE assertions SET sequence=? WHERE tenant=? AND project=? AND id=?",
                (sequence, actor.tenant, project, row["assertion_id"]),
            )
            self._job(actor, project, "graph", {"assertion_id": row["assertion_id"]})
        # Mark all blocks citing this source, including human-authored blocks.
        for block in self.db.all(
            "SELECT id,refs FROM wiki_blocks WHERE tenant=? AND project=?", (actor.tenant, project)
        ):
            if any(r.source_id == source_id for r in refs_from(block["refs"])):
                self.db.execute(
                    "UPDATE wiki_blocks SET state='stale' WHERE tenant=? AND project=? AND id=?",
                    (actor.tenant, project, block["id"]),
                )

    def set_source_readers(self, actor, project, source_id, readers: tuple[str, ...]):
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            self._source(actor, project, source_id)
            head = self.db.one(
                "SELECT owner FROM sources WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, source_id)
            )
            if head["owner"] != actor.principal:
                raise AccessDenied("only source owner may change readers")
            acl = sorted(set(readers))
            if acl and actor.principal not in acl:
                raise AccessDenied("owner must retain access")
            self.db.execute(
                "UPDATE sources SET readers=? WHERE tenant=? AND project=? AND id=?",
                (dump(acl), actor.tenant, project, source_id),
            )
            self._sequence(actor.tenant, project)

    def revoke(self, actor, project, assertion_id):
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            row = self.db.one(
                "SELECT * FROM assertion_versions WHERE tenant=? AND project=? AND id=? ORDER BY revision DESC LIMIT 1",
                (actor.tenant, project, assertion_id),
            )
            if not row:
                raise InvalidEvidence("assertion unavailable")
            self._validate_refs(actor, project, refs_from(row["refs"]))
            sequence = self._sequence(actor.tenant, project)
            self.db.execute(
                "UPDATE assertions SET revoked=1,sequence=? WHERE tenant=? AND project=? AND id=?",
                (sequence, actor.tenant, project, assertion_id),
            )
            self.db.execute(
                "UPDATE wiki_blocks SET state='stale' WHERE tenant=? AND project=? AND id=?",
                (actor.tenant, project, f"assertion:{assertion_id}"),
            )
            self._job(actor, project, "graph", {"assertion_id": assertion_id})

    def delete_source(self, actor, project, source_id):
        with self.db.transaction():
            self.db.lock_project(actor.tenant, project)
            self._scope(actor, project, write=True)
            self._source(actor, project, source_id)
            head = self.db.one(
                "SELECT owner FROM sources WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, source_id)
            )
            if head["owner"] != actor.principal:
                raise AccessDenied("only source owner may delete source")
            self._invalidate_source(actor, project, source_id)
            self.db.execute(
                "UPDATE sources SET state='deleted' WHERE tenant=? AND project=? AND id=?",
                (actor.tenant, project, source_id),
            )
            self.db.execute(
                "UPDATE source_versions SET content='' WHERE tenant=? AND project=? AND id=?",
                (actor.tenant, project, source_id),
            )
            self.db.execute(
                "DELETE FROM chunks WHERE tenant=? AND project=? AND source_id=?", (actor.tenant, project, source_id)
            )
            self.db.execute(
                "DELETE FROM source_context WHERE tenant=? AND project=? AND source_id=?",
                (actor.tenant, project, source_id),
            )
            affected = self.db.all(
                "SELECT DISTINCT assertion_id FROM dependencies WHERE tenant=? AND project=? AND source_id=?",
                (actor.tenant, project, source_id),
            )
            affected_ids = {row["assertion_id"] for row in affected}
            for row in affected:
                aid = row["assertion_id"]
                self.db.execute(
                    "UPDATE assertions SET revoked=1 WHERE tenant=? AND project=? AND id=?",
                    (actor.tenant, project, aid),
                )
                self.db.execute(
                    """UPDATE assertion_versions SET subject='',predicate='',value='',text='',metadata='{}'
                    WHERE tenant=? AND project=? AND id=?""",
                    (actor.tenant, project, aid),
                )
                self.db.execute(
                    "UPDATE graph_edges SET subject='',predicate='',value='',active=0 WHERE tenant=? AND project=? AND assertion_id=?",
                    (actor.tenant, project, aid),
                )
            for block in self.db.all(
                "SELECT id,refs,dependencies FROM wiki_blocks WHERE tenant=? AND project=?", (actor.tenant, project)
            ):
                if any(r.source_id == source_id for r in refs_from(block["refs"])) or affected_ids.intersection(
                    json.loads(block["dependencies"])
                ):
                    self.db.execute(
                        "UPDATE wiki_blocks SET text='',state='deleted' WHERE tenant=? AND project=? AND id=?",
                        (actor.tenant, project, block["id"]),
                    )
                    self.db.execute(
                        "UPDATE wiki_revisions SET text='' WHERE tenant=? AND project=? AND block_id=?",
                        (actor.tenant, project, block["id"]),
                    )
            for block in self.db.all(
                "SELECT block_id,revision,refs FROM wiki_revisions WHERE tenant=? AND project=?",
                (actor.tenant, project),
            ):
                if any(r.source_id == source_id for r in refs_from(block["refs"])):
                    self.db.execute(
                        "UPDATE wiki_revisions SET text='' WHERE tenant=? AND project=? AND block_id=? AND revision=?",
                        (actor.tenant, project, block["block_id"], block["revision"]),
                    )

    def _claims(self, actor, project, *, valid_at=None, known_at=None, include_candidates=False):
        # This is the eligibility set, also used to exclude invalid source
        # spans and validate wiki dependencies. A retrieval cap here would
        # misclassify older, still-supported evidence as invalid. Bound
        # candidate scoring later, never authorization/temporal eligibility.
        valid = timestamp(valid_at) if valid_at else self.clock()
        known = timestamp(known_at) if known_at else self.clock()
        rows = self.db.all(
            """SELECT v.* FROM assertion_versions v JOIN assertions a
            ON a.tenant=v.tenant AND a.project=v.project AND a.id=v.id
            WHERE v.tenant=? AND v.project=? AND a.revoked=0
            AND v.revision=(SELECT MAX(x.revision) FROM assertion_versions x WHERE
                x.tenant=v.tenant AND x.project=v.project AND x.id=v.id
                AND x.valid_from<=? AND x.recorded_at<=?)
            ORDER BY v.recorded_at DESC""",
            (actor.tenant, project, valid, known),
        )
        eligible = []
        for row in rows:
            if row["valid_to"] and row["valid_to"] <= valid:
                continue
            if row["status"] != "supported" and not include_candidates:
                continue
            try:
                self._validate_refs(
                    actor,
                    project,
                    refs_from(row["refs"]),
                    known_at=known if known_at else None,
                    historical=bool(valid_at or known_at),
                )
            except (AccessDenied, InvalidEvidence):
                continue
            eligible.append(row)
        return eligible

    def list_claims(self, actor, project, *, valid_at=None, known_at=None, include_candidates=False):
        with self.db.transaction():
            self._scope(actor, project)
            return self._claims(
                actor, project, valid_at=valid_at, known_at=known_at, include_candidates=include_candidates
            )

    def entity_fact_inventory(self, actor, project, entities, *, limit=96, valid_at=None, known_at=None):
        """Return literal evidence behind supported facts about named entities.

        This is a bounded navigation path for broad set/count questions.  It
        uses the compiled subject index to find candidate facts, but exposes
        only the referenced source spans.  Thus a graph/wiki/claim can widen
        recall without becoming reader-visible synthetic evidence or bypassing
        the same ACL, revision, and validity checks as ordinary recall.
        """
        if not isinstance(limit, int) or not 1 <= limit <= 256:
            raise ValueError("entity fact inventory limit must be 1..256")
        entity_groups = [terms(entity) for entity in entities if isinstance(entity, str) and terms(entity)]
        entity_terms = set().union(*entity_groups) if entity_groups else set()
        if not entity_terms:
            return EvidenceBundle("entity-fact-inventory", (), 0, {"entities": [], "claims": 0})
        with self.db.transaction():
            self._scope(actor, project)
            claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
            by_source, source_order, seen, matched_claims = {}, [], set(), 0
            known = timestamp(known_at) if known_at else None
            for row in claims:
                # Subject matching is intentional: a mention of an entity in
                # another person's fact is not evidence for an entity-centric
                # enumeration.  Multi-token names must all match.
                if not any(group <= terms(row["subject"]) for group in entity_groups):
                    continue
                matched_claims += 1
                for ref in refs_from(row["refs"]):
                    # A compiler claim can legitimately cite an episode-wide
                    # span.  Expand it back to the bounded literal chunks
                    # created at ingest so an evidence card does not display
                    # the first unrelated speaker in that episode.
                    chunks = self.db.all(
                        """SELECT * FROM chunks WHERE tenant=? AND project=? AND source_id=? AND source_revision=?
                        AND start_offset < ? AND ? < end_offset ORDER BY start_offset""",
                        (actor.tenant, project, ref.source_id, ref.revision, ref.end, ref.start),
                    )
                    raw_refs = [
                        SourceRef(chunk["source_id"], chunk["source_revision"], chunk["start_offset"],
                                  chunk["end_offset"], ref.content_hash)
                        for chunk in chunks
                    ] or [ref]
                    for raw_ref in raw_refs:
                        key = (raw_ref.source_id, raw_ref.revision, raw_ref.start, raw_ref.end, raw_ref.content_hash)
                        if key in seen:
                            continue
                        try:
                            text = self._validate_refs(actor, project, (raw_ref,), known_at=known,
                                                       historical=bool(valid_at or known_at))[0]
                        except (AccessDenied, InvalidEvidence):
                            continue
                        # ID is deterministic from literal provenance and does
                        # not disclose the assertion ID to reader-facing output.
                        item = Evidence(
                            digest("entity-inventory:" + dump([raw_ref.source_id, raw_ref.revision, raw_ref.start,
                                                                raw_ref.end, raw_ref.content_hash])),
                            "source", text, (raw_ref,), raw_ref.revision, 0.0,
                            {"navigation": "entity_fact_inventory"},
                        )
                        if raw_ref.source_id not in by_source:
                            by_source[raw_ref.source_id] = []
                            source_order.append(raw_ref.source_id)
                        by_source[raw_ref.source_id].append(item)
                        seen.add(key)
            # Round-robin across sources.  Claim ingestion order must not let
            # a recent/dense source crowd out older entity facts needed by a
            # broad enumeration or count query.
            result = []
            while len(result) < limit:
                advanced = False
                for source_id in source_order:
                    if by_source[source_id]:
                        result.append(by_source[source_id].pop(0))
                        advanced = True
                        if len(result) == limit:
                            break
                if not advanced:
                    break
            return EvidenceBundle("entity-fact-inventory", tuple(result), 0,
                                  {"entities": sorted(entity_terms), "claims": matched_claims,
                                   "truncated": any(by_source[source_id] for source_id in source_order)})

    def entity_speaker_inventory(self, actor, project, entities, *, limit=96, valid_at=None, known_at=None):
        """Return dated literal turns spoken by explicitly named entities.

        This is a deliberately narrow raw-source navigation fallback for
        enumeration questions.  A knowledge compiler is useful to navigate a
        project, but it must not become a recall gate: an omitted compiled
        assertion cannot make an otherwise readable primary-source statement
        disappear.  We therefore require an exact ``Name:`` speaker label and
        return only that dated source turn.  No fact, summary, or inferred
        speaker identity is generated here.
        """
        if not isinstance(limit, int) or not 1 <= limit <= 256:
            raise ValueError("entity speaker inventory limit must be 1..256")
        names = tuple(sorted({" ".join(entity.split()) for entity in entities
                              if isinstance(entity, str) and entity.strip()}, key=str.casefold))
        if not names:
            return EvidenceBundle("entity-speaker-inventory", (), 0, {"entities": [], "turns": 0})
        patterns = tuple(re.compile(r"(?m)^" + re.escape(name) + r"\s*:\s*[^\n]+") for name in names)
        with self.db.transaction():
            self._scope(actor, project)
            known = timestamp(known_at) if known_at else None
            sources = self.db.all(
                "SELECT id,revision FROM sources WHERE tenant=? AND project=? AND state='active' ORDER BY id",
                (actor.tenant, project),
            )
            by_source, source_order, seen = {}, [], set()
            for source_row in sources:
                try:
                    source = self._source(actor, project, source_row["id"], revision=source_row["revision"],
                                          known_at=known, historical=bool(valid_at or known_at))
                except (AccessDenied, InvalidEvidence):
                    continue
                if valid_at and source["recorded_at"] > timestamp(valid_at):
                    continue
                content = source["content"]
                spans = []
                for pattern in patterns:
                    for match in pattern.finditer(content):
                        # The LoCoMo-like source convention puts a date line
                        # directly before every turn.  If a caller uses a
                        # different source format, retain only the exact
                        # speaker line rather than inventing a date boundary.
                        date_start = content.rfind("DATE OF CONVERSATION:", 0, match.start())
                        start = (date_start if date_start >= 0
                                 and content[date_start:match.start()].count("\n") == 1 else match.start())
                        end = match.end()
                        # The attachment belongs to this observation, not to
                        # the next speaker. Preserve supplied descriptions.
                        while content.startswith("\nAttachment description", end):
                            line_end = content.find("\n", end + 1)
                            end = line_end if line_end >= 0 else len(content)
                        spans.append((start, end))
                for start, end in sorted(set(spans)):
                    key = (source_row["id"], source_row["revision"], start, end, source["content_hash"])
                    if key in seen:
                        continue
                    ref = SourceRef(*key)
                    try:
                        text = self._validate_refs(actor, project, (ref,), known_at=known,
                                                   historical=bool(valid_at or known_at))[0]
                    except (AccessDenied, InvalidEvidence):
                        continue
                    if source_row["id"] not in by_source:
                        by_source[source_row["id"]] = []
                        source_order.append(source_row["id"])
                    by_source[source_row["id"]].append(Evidence(
                        digest("entity-speaker-inventory:" + dump(list(key))), "source", text, (ref,),
                        ref.revision, 0.0, {"navigation": "entity_speaker_inventory"},
                    ))
                    seen.add(key)
            result = []
            while len(result) < limit:
                advanced = False
                for source_id in source_order:
                    if by_source[source_id]:
                        result.append(by_source[source_id].pop(0))
                        advanced = True
                        if len(result) == limit:
                            break
                if not advanced:
                    break
            return EvidenceBundle("entity-speaker-inventory", tuple(result), 0,
                                  {"entities": list(names), "turns": len(result),
                                   "truncated": any(by_source[source_id] for source_id in source_order)})

    def _wiki(self, actor, project, *, page=None):
        rows = self.db.all(
            """SELECT * FROM wiki_blocks WHERE tenant=? AND project=? AND state='published'
            ORDER BY page,text,refs,revision,id LIMIT 1000""",
            (actor.tenant, project),
        )
        claims = {c["id"]: c["revision"] for c in self._claims(actor, project)}
        result = []
        for row in rows:
            if page and row["page"] != page:
                continue
            if any(claims.get(key) != revision for key, revision in json.loads(row["dependencies"]).items()):
                continue
            try:
                self._validate_refs(actor, project, refs_from(row["refs"]))
            except (AccessDenied, InvalidEvidence):
                continue
            result.append(row)
        return result

    def read_wiki(self, actor, project, *, page=None, level=1):
        if level not in {0, 1, 2}:
            raise ValueError("level must be 0, 1 or 2")
        with self.db.transaction():
            self._scope(actor, project)
            blocks = self._wiki(actor, project, page=page)
            output = []
            for row in blocks:
                item = {k: row[k] for k in ("id", "revision", "page", "managed_by", "watermark")}
                item["text"] = row["text"][:160] if level == 0 else row["text"]
                item["refs"] = json.loads(row["refs"])
                if level == 2:
                    item["evidence"] = self._validate_refs(actor, project, refs_from(row["refs"]))
                output.append(item)
            return {
                "blocks": output,
                "coverage": "authorized published blocks only; not a complete project summary",
                "source_count_covered": len({r["source_id"] for b in output for r in b["refs"]}),
            }

    def recall_planned(self, actor, project, query, **options) -> EvidenceBundle:
        """Opt-in public-query focus retrieval; normal recall remains the control."""
        from .planned_retrieval import recall_planned

        return recall_planned(self, actor, project, query, **options)

    def recall(
        self,
        actor,
        project,
        query,
        *,
        token_budget=2000,
        valid_at=None,
        known_at=None,
        use_wiki=True,
        use_graph=True,
        vector_index=None,
        query_vector=None,
        retrieval_policy="hybrid",
        char_budget=None,
        rank_config: RetrievalConfig = RetrievalConfig(),
        reranker=None,
        candidate_rescorer=None,
        lexical_conjunctive=False,
        parent_vector_index=None,
        parent_vector_limit=0,
        parent_vector_chunk_limit=0,
    ) -> EvidenceBundle:
        if token_budget < 1 or token_budget > 32000 or not query.strip():
            raise ValueError("nonempty query and token budget 1..32000 required")
        if retrieval_policy not in {"overlap", "hybrid"}:
            raise ValueError("retrieval_policy must be overlap or hybrid")
        if reranker is not None and retrieval_policy != "hybrid":
            raise ValueError("reranker requires hybrid retrieval; it cannot be silently ignored")
        if rank_config.semantic_windows and (retrieval_policy != 'hybrid' or reranker is None or use_graph or use_wiki):
            raise ValueError('semantic windows require hybrid scorer and explicit raw-only graph/wiki settings')
        if rank_config.budget_feasible and (retrieval_policy != 'hybrid' or use_graph or use_wiki):
            raise ValueError('budget feasibility requires hybrid raw-only graph/wiki settings')
        if rank_config.rerank_literal_sources and (reranker is None or retrieval_policy != "hybrid"):
            raise ValueError("literal reranking requires a reranker and hybrid retrieval")
        if candidate_rescorer is not None and retrieval_policy != "hybrid":
            raise ValueError("candidate_rescorer requires hybrid retrieval")
        if char_budget is not None and (not isinstance(char_budget, int) or not 1 <= char_budget <= 128000):
            raise ValueError("char_budget must be 1..128000")
        if not isinstance(parent_vector_limit, int) or not 0 <= parent_vector_limit <= 200:
            raise ValueError("parent vector limit must be an integer from 0 to 200")
        if not isinstance(parent_vector_chunk_limit, int) or not 0 <= parent_vector_chunk_limit <= 20:
            raise ValueError("parent vector chunk limit must be an integer from 0 to 20")
        if parent_vector_limit and (parent_vector_index is None or query_vector is None):
            raise ValueError("parent vector retrieval requires an index and query vector")
        with self.db.transaction():
            self._scope(actor, project)
            qterms = terms(query)
            candidates = []
            eligible_claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
            eligible_wiki = self._wiki(actor, project) if use_wiki and not valid_at and not known_at else []
            # Do not reintroduce revoked/superseded/future assertions through raw
            # source fallback. Conservatively omit overlapping chunks.
            # Raw recall remains usable before compilation. Hybrid broadens the
            # bounded pool; candidate BM25 uses only evidence passing checks below.
            raw_limit = 2000 if retrieval_policy == "hybrid" else 500
            search_terms = sorted(set(lexemes(query)))[:32] if retrieval_policy == "hybrid" else sorted(qterms)[:12]
            joiner = " AND " if lexical_conjunctive else " OR "
            pattern = joiner.join("LOWER(c.text) LIKE ? ESCAPE '\\'" for _ in search_terms) or "1=0"
            patterns = tuple(
                "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in search_terms
            )
            chunks = self.db.all(
                f"""SELECT c.* FROM chunks c WHERE c.tenant=? AND c.project=?
                AND ({pattern}) ORDER BY c.id LIMIT {raw_limit}""",
                (actor.tenant, project, *patterns),
            )
            vector_scores = {}
            if vector_index is not None and query_vector is not None:
                for cid, score in vector_index.search(actor.tenant, project, query_vector, limit=50):
                    vector_scores[cid] = score
                    chunk = self.db.one(
                        "SELECT * FROM chunks WHERE tenant=? AND project=? AND id=?", (actor.tenant, project, cid)
                    )
                    if chunk and all(c["id"] != cid for c in chunks):
                        chunks.append(chunk)
            # A parent/source index complements fine-grained chunk retrieval.
            # Parent hits only nominate already-authorized literal chunks; all
            # ACL/revision validation still occurs below before they become
            # evidence.  This creates a dual-level index without allowing a
            # generated parent summary to reach the reader.
            parent_hits = []
            parent_chunks_added = 0
            if parent_vector_limit:
                parent_hits = parent_vector_index.search(
                    actor.tenant, project, query_vector, limit=parent_vector_limit
                )
                known_chunks = {c["id"] for c in chunks}
                for source_id, score in parent_hits:
                    parent_chunks = self.db.all(
                        """SELECT * FROM chunks WHERE tenant=? AND project=? AND source_id=?
                        ORDER BY start_offset""",
                        (actor.tenant, project, source_id),
                    )
                    # A whole-document match is a discovery signal, not proof
                    # that every passage in that document answers the query.
                    # Existing callers can retain legacy all-chunk expansion
                    # with limit=0. Bounded expansion selects only locally
                    # lexical-anchored chunks, while global chunk-vector hits
                    # remain available for semantic-only passages.
                    if parent_vector_chunk_limit:
                        local = [chunk for chunk in sorted(
                            parent_chunks,
                            key=lambda chunk: (
                                -len(qterms & terms(chunk["text"])),
                                chunk["start_offset"],
                                chunk["id"],
                            ),
                        ) if qterms & terms(chunk["text"])][
                            :parent_vector_chunk_limit
                        ]
                    else:
                        # Exact legacy path: source chunks retain database
                        # offset order when no local expansion ablation is on.
                        local = parent_chunks
                    for chunk in local:
                        vector_scores[chunk["id"]] = max(vector_scores.get(chunk["id"], 0), score)
                        if chunk["id"] not in known_chunks:
                            chunks.append(chunk)
                            known_chunks.add(chunk["id"])
                            parent_chunks_added += 1
            source_ids = sorted({c["source_id"] for c in chunks})
            if rank_config.materialize_derived:
                source_ids = sorted(
                    set(source_ids)
                    | {ref.source_id for row in [*eligible_claims, *eligible_wiki] for ref in refs_from(row["refs"])}
                )
            excluded_spans = self._excluded_source_spans(actor, project, source_ids, eligible_claims)
            neighbor_added = 0
            if rank_config.dense_neighbor_radius and retrieval_policy == "hybrid":
                known_chunks = {c["id"] for c in chunks}
                seed_scores = dict(vector_scores)
                # One hop only: never expand newly added neighbors recursively.
                for seed in list(chunks):
                    seed_score = seed_scores.get(seed["id"], 0)
                    if seed_score <= 0 or any(
                        begin < seed["end_offset"] and seed["start_offset"] < end
                        for begin, end in excluded_spans.get((seed["source_id"], seed["source_revision"]), [])
                    ):
                        continue
                    try:
                        source = self._source(
                            actor,
                            project,
                            seed["source_id"],
                            revision=seed["source_revision"],
                            known_at=timestamp(known_at) if known_at else None,
                        )
                        if valid_at and source["recorded_at"] > timestamp(valid_at):
                            continue
                    except (AccessDenied, InvalidEvidence):
                        continue
                    for operator, order in (("<", "DESC"), (">", "ASC")):
                        nearby = self.db.all(
                            f"""SELECT * FROM chunks WHERE tenant=? AND project=? AND source_id=?
                            AND source_revision=? AND start_offset {operator} ? ORDER BY start_offset {order}
                            LIMIT {rank_config.dense_neighbor_radius}""",
                            (actor.tenant, project, seed["source_id"], seed["source_revision"], seed["start_offset"]),
                        )
                        for distance, neighbor in enumerate(nearby, 1):
                            # Propagated retrieval signal, not claimed cosine similarity.
                            vector_scores[neighbor["id"]] = max(
                                vector_scores.get(neighbor["id"], 0), seed_score / (distance + 1)
                            )
                            if neighbor["id"] not in known_chunks:
                                known_chunks.add(neighbor["id"])
                                chunks.append(neighbor)
                                neighbor_added += 1
            for row in chunks:
                if any(
                    start < row["end_offset"] and row["start_offset"] < end
                    for start, end in excluded_spans.get((row["source_id"], row["source_revision"]), [])
                ):
                    continue
                try:
                    source = self._source(
                        actor,
                        project,
                        row["source_id"],
                        revision=row["source_revision"],
                        known_at=timestamp(known_at) if known_at else None,
                    )
                    if valid_at and source["recorded_at"] > timestamp(valid_at):
                        continue
                except (AccessDenied, InvalidEvidence):
                    continue
                ref = SourceRef(
                    row["source_id"],
                    row["source_revision"],
                    row["start_offset"],
                    row["end_offset"],
                    source["content_hash"],
                )
                score = len(qterms & terms(row["text"])) / max(1, len(qterms)) if retrieval_policy == "overlap" else 0.0
                score = max(score, vector_scores.get(row["id"], 0))
                candidates.append(Evidence(row["id"], "source", row["text"], (ref,), row["source_revision"], score))
            if use_graph:
                for row in eligible_claims:
                    score = len(qterms & terms(row["text"] + " " + row["subject"] + " " + row["value"])) / max(
                        1, len(qterms)
                    )
                    if score:
                        candidates.append(
                            Evidence(
                                row["id"],
                                row["kind"],
                                row["text"],
                                refs_from(row["refs"]),
                                row["revision"],
                                score,
                                {
                                    "valid_from": row["valid_from"],
                                    "status": row["status"],
                                    "search_text": row["subject"] + " " + row["value"] + " " + row["text"],
                                },
                            )
                        )
            # Historical questions must not consume a current Wiki view.
            if use_wiki and not valid_at and not known_at:
                for row in eligible_wiki:
                    score = len(qterms & terms(row["text"])) / max(1, len(qterms))
                    if score:
                        candidates.append(
                            Evidence(row["id"], "wiki", row["text"], refs_from(row["refs"]), row["revision"], score)
                        )
            feasibility_rejections = []
            if rank_config.budget_feasible:
                feasible = []
                for original in candidates:
                    item = passage(original,query,rank_config.passage_chars)
                    group = [item]
                    if rank_config.source_context:
                        context = self._source_context(actor,project,item,excluded_spans,known_at=known_at)
                        if context is None:continue
                        group = [*context,item]
                    cost,char_cost = evidence_cost(group)
                    reason = ('token_budget' if cost>token_budget else
                              'char_budget' if char_budget is not None and char_cost>char_budget else None)
                    if reason:
                        feasibility_rejections.append(dict(id=item.id,kind=item.kind,revision=item.revision,
                            reason=reason,group_chars=char_cost,group_estimated_tokens=cost))
                    else:
                        # Retain full original text for unchanged BM25/index scoring.
                        feasible.append(original)
                candidates = feasible
            if candidate_rescorer is not None:
                allowed = {item.id for item in candidates}
                rescored = candidate_rescorer.rescore(
                    sorted(allowed), {cid: s for cid, s in vector_scores.items() if cid in allowed}
                )
                if (
                    not isinstance(rescored, dict)
                    or not set(rescored) <= allowed
                    or not all(
                        isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) and s >= 0
                        for s in rescored.values()
                    )
                ):
                    raise ValueError("candidate_rescorer returned invalid or unauthorized scores")
                vector_scores = rescored
            def literal_rerank_text(item):
                literals = self._materialize_derived(
                    actor, project, [item], query, rank_config.passage_chars,
                    excluded_spans, valid_at=valid_at, known_at=known_at,
                )
                # Multi-reference evidence remains one scoring group. No
                # generated claim is substituted for a missing source span.
                return "\n\n".join(entry.text for entry in literals)

            pool_receipt = None
            pool_inputs = candidate_pool_fingerprints(candidates, vector_scores) if rank_config.rerank_literal_sources else None
            if rank_config.source_reserve:
                pool = source_preserving_shortlist(query, candidates, vector_scores, rank_config,
                                                  source_reserve=rank_config.source_reserve)
                ordered = rerank_shortlist(query, pool.items, rank_config, reranker, literal_rerank_text)
                pool_receipt = dict(source_pool_sha256=pool.source_pool_sha256,
                                    derived_pool_sha256=pool.derived_pool_sha256,
                                    shortlist_sha256=pool.shortlist_sha256,
                                    source_reserve=rank_config.source_reserve)
            else:
                ordered = (
                    rank(
                        query, candidates, vector_scores, rank_config, reranker=reranker,
                        rerank_text=literal_rerank_text if rank_config.rerank_literal_sources else None,
                    )
                    if retrieval_policy == "hybrid"
                    else sorted(candidates, key=lambda item: (-item.score, item.kind, item.id))
                )
            shortlist_sha256 = (
                ordered[0].metadata.get('shortlist_sha256') if ordered else shortlist_fingerprint([])
            ) if retrieval_policy == 'hybrid' else None
            parent_shortlist_sha256 = ordered[0].metadata.get('parent_shortlist_sha256') if ordered else None
            if rank_config.materialize_derived:
                ordered = self._materialize_derived(
                    actor,
                    project,
                    ordered,
                    query,
                    rank_config.passage_chars,
                    excluded_spans,
                    valid_at=valid_at,
                    known_at=known_at,
                )
            selected, seen, used, used_chars = [], set(), 0, 0
            packing_decisions = []
            for item in ordered:
                key = " ".join(item.text.split())
                group = [item]
                if rank_config.source_context and item.kind == "source":
                    context = self._source_context(actor, project, item, excluded_spans, known_at=known_at)
                    if context is None:
                        continue
                    group = [*context, item]
                cost,char_cost = evidence_cost(group,preceding=bool(selected))
                reason = ('duplicate' if key in seen else 'token_budget' if cost + used > token_budget
                          else 'char_budget' if char_budget is not None and used_chars + char_cost > char_budget
                          else 'selected')
                packing_decisions.append(dict(id=item.id,kind=item.kind,revision=item.revision,
                    reason=reason,group_chars=char_cost,group_estimated_tokens=cost))
                if reason != 'selected':
                    continue
                selected.extend(group)
                seen.add(key)
                used += cost
                used_chars += char_cost
            return EvidenceBundle(
                query,
                tuple(selected),
                used,
                {
                    "candidate_count": len(candidates),
                    "parent_shortlist_sha256": parent_shortlist_sha256,
                    "neighbor_candidates_added": neighbor_added,
                    "parent_vector_hits": len(parent_hits),
                    "parent_vector_chunks_added": parent_chunks_added,
                    "budget": token_budget,
                    "historical": bool(valid_at or known_at),
                    "raw_candidate_limit": raw_limit,
                    "retrieval_policy": retrieval_policy,
                    "char_budget": char_budget,
                    "context_chars": used_chars,
                    "packing_decisions": packing_decisions,
                    "feasibility_rejections": feasibility_rejections,
                    "rank_config": asdict(rank_config),
                    "shortlist_sha256": shortlist_sha256,
                    "pool_selection": pool_receipt,
                    "candidate_pool_inputs": pool_inputs,
                    "warning": "raw observations are evidence, not current approvals or tool authorization",
                },
            )

    def focus_event_evidence(self, actor, project, query, pool, *, parent_vector_index=None,
                             focus_vector=None, public_as_of=None, valid_at=None, known_at=None):
        """Route public provenance questions to a complete eligible episode.

        A public document date is a retrieval hint, never ledger validity.
        Whole records and newly navigated siblings get the same fresh access,
        revision, temporal and excluded-span checks as ordinary raw recall.
        """
        from .focused_evidence import complete_source_passage, select_origin

        with self.db.transaction():
            self._scope(actor, project)
            pool = self._eligible_literal_pool(actor, project, pool, valid_at=valid_at, known_at=known_at)
            seed_ids = {ref.source_id for item in pool.items for ref in item.refs}
            routes = self.db.all(
                """SELECT r.source_id,r.revision,r.route FROM source_routes r JOIN sources s
                ON s.tenant=r.tenant AND s.project=r.project AND s.id=r.source_id
                WHERE r.tenant=? AND r.project=? AND s.state='active' AND r.revision=s.revision
                ORDER BY r.source_id""", (actor.tenant, project))
            groups = {row['source_id']: json.loads(row['route']).get('event_id') for row in routes}
            seeded_groups = {groups[sid] for sid in seed_ids if groups.get(sid)}
            known = timestamp(known_at) if known_at else None
            valid = timestamp(valid_at) if valid_at else None
            sources, dates = [], {}
            for row in routes:
                sid = row['source_id']
                if groups.get(sid) not in seeded_groups:
                    continue
                try:
                    source = self._source(actor, project, sid, revision=row['revision'], known_at=known,
                                          historical=bool(valid_at or known_at))
                except (AccessDenied, InvalidEvidence):
                    continue
                if valid and source['recorded_at'] > valid:
                    continue
                text = source['content']
                ref = SourceRef(sid, source['revision'], 0, len(text), source['content_hash'])
                match = re.search(r'^Date:\s*(20\d\d-\d\d-\d\d)\b', text)
                dates[sid] = match[1] if match else None
                sources.append(Evidence('focus-source:' + digest(dump(asdict(ref))), 'source', text,
                                        (ref,), ref.revision, metadata={'navigation': 'focused_event_record'}))
            claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
            excluded = self._excluded_source_spans(actor, project, [e.refs[0].source_id for e in sources], claims)
            sources = [item for item in sources if not excluded.get((item.refs[0].source_id, item.revision))]
            dense = dict(parent_vector_index.search(actor.tenant, project, focus_vector, limit=1000)) \
                if parent_vector_index is not None and focus_vector is not None else {}
            selected, diagnostics = select_origin(query, sources, groups, dates, dense, as_of=public_as_of)
            if not selected:
                return EvidenceBundle(query, pool.items, pool.estimated_tokens,
                                      {**pool.diagnostics, 'event_focus': {**diagnostics, 'applied': False}})
            selected = tuple(complete_source_passage(item, query) for item in selected)
            result = self._eligible_literal_pool(actor, project, EvidenceBundle(query, selected, 0, {}),
                                                 valid_at=valid_at, known_at=known_at)
            return EvidenceBundle(query, result.items, 0,
                {**pool.diagnostics, 'event_focus': {**diagnostics, 'applied': True},
                 'focused_source_count': len(result.items), 'support_passage_max_chars': 3200})

    def event_sibling_evidence(self, actor, project, query, seeds, *, max_items=16, max_groups=0,
                               max_chars=8000, passage_chars=560, valid_at=None, known_at=None):
        """Find literal evidence in eligible siblings of recalled event groups.

        Event IDs are host-supplied ingest metadata stored in source routes.
        They help navigate to related records but do not create support edges.
        Every seed and returned span is revalidated against current ACL,
        revision, and assertion validity inside the memory service.
        """
        from .event_evidence import sibling_evidence

        with self.db.transaction():
            self._scope(actor, project)
            known = timestamp(known_at) if known_at else None
            valid = timestamp(valid_at) if valid_at else None
            seed_sources = set()
            event_ids = set()
            eligible_seeds = []
            for item in seeds:
                if item.kind != "source" or len(item.refs) != 1:
                    continue
                ref = item.refs[0]
                try:
                    actual = self._validate_refs(actor, project, (ref,), known_at=known,
                                                 historical=bool(valid_at or known_at))[0]
                    source = self._source(actor, project, ref.source_id, revision=ref.revision,
                                          known_at=known, historical=bool(valid_at or known_at))
                except (AccessDenied, InvalidEvidence):
                    continue
                if actual != item.text or (valid and source["recorded_at"] > valid):
                    continue
                route_row = self.db.one(
                    "SELECT route FROM source_routes WHERE tenant=? AND project=? AND source_id=? AND revision=?",
                    (actor.tenant, project, ref.source_id, ref.revision),
                )
                if not route_row:
                    continue
                event_id = json.loads(route_row["route"]).get("event_id")
                if isinstance(event_id, str) and event_id:
                    event_ids.add(event_id)
                    seed_sources.add(ref.source_id)
                    eligible_seeds.append(item)
            if not event_ids:
                return EvidenceBundle(query, (), 0, {"event_expansion": "no_seed_event"})

            source_texts, source_metadata, source_versions = {}, {}, {}
            for item in eligible_seeds:
                if item.kind != "source" or len(item.refs) != 1:
                    continue
                ref = item.refs[0]
                route_row = self.db.one(
                    "SELECT route FROM source_routes WHERE tenant=? AND project=? AND source_id=? AND revision=?",
                    (actor.tenant, project, ref.source_id, ref.revision),
                )
                if route_row:
                    event_id = json.loads(route_row["route"]).get("event_id")
                    if event_id in event_ids:
                        source_metadata[ref.source_id] = {"thread_id": event_id}
            route_rows = self.db.all(
                """SELECT r.source_id,r.revision,r.route,s.revision AS head_revision
                FROM source_routes r JOIN sources s
                  ON s.tenant=r.tenant AND s.project=r.project AND s.id=r.source_id
                WHERE r.tenant=? AND r.project=? AND s.state='active' AND r.revision=s.revision
                ORDER BY r.source_id""",
                (actor.tenant, project),
            )
            for row in route_rows:
                event_id = json.loads(row["route"]).get("event_id")
                if event_id not in event_ids or row["source_id"] in seed_sources:
                    continue
                try:
                    source = self._source(actor, project, row["source_id"], revision=row["revision"],
                                          known_at=known, historical=bool(valid_at or known_at))
                except (AccessDenied, InvalidEvidence):
                    continue
                if valid and source["recorded_at"] > valid:
                    continue
                source_texts[row["source_id"]] = source["content"]
                source_metadata[row["source_id"]] = {
                    "thread_id": event_id,
                    "slot_id": row["source_id"],
                }
                source_versions[row["source_id"]] = (source["revision"], source["content_hash"])

            candidates = sibling_evidence(
                query, eligible_seeds, source_texts, source_metadata, source_versions,
                max_items=max_items, max_groups=max_groups, max_chars=max_chars, passage_chars=passage_chars,
            )
            claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
            excluded = self._excluded_source_spans(
                actor, project, [ref.source_id for item in candidates for ref in item.refs], claims
            )
            eligible = []
            for item in candidates:
                ref = item.refs[0]
                try:
                    actual = self._validate_refs(actor, project, (ref,), known_at=known,
                                                 historical=bool(valid_at or known_at))[0]
                    source = self._source(actor, project, ref.source_id, revision=ref.revision,
                                          known_at=known, historical=bool(valid_at or known_at))
                except (AccessDenied, InvalidEvidence):
                    continue
                if actual != item.text or (valid and source["recorded_at"] > valid):
                    continue
                if any(start < ref.end and ref.start < end
                       for start, end in excluded.get((ref.source_id, ref.revision), [])):
                    continue
                eligible.append(item)
            return EvidenceBundle(query, tuple(eligible), 0, {
                "event_expansion": "recalled-event-literal-siblings-v1",
                "event_evidence_policy": "legacy",
                "seed_event_count": len(event_ids),
                "candidate_sources": len(source_texts),
                "selected_spans": len(eligible),
            })

    def _excluded_source_spans(self, actor, project, source_ids, eligible_claims):
        """Shared exclusion policy for raw recall and relation witnesses."""
        excluded = {}
        source_ids = sorted(set(source_ids))
        if not source_ids:
            return excluded
        selected = {(c['id'], c['revision']) for c in eligible_claims}
        placeholders = ','.join('?' for _ in source_ids)
        rows = self.db.all(
            f"""SELECT DISTINCT v.id,v.revision,v.refs,v.status,a.revoked FROM assertion_versions v
            JOIN assertions a ON a.tenant=v.tenant AND a.project=v.project AND a.id=v.id
            JOIN dependencies d ON d.tenant=v.tenant AND d.project=v.project AND d.assertion_id=v.id
            WHERE v.tenant=? AND v.project=? AND d.source_id IN ({placeholders})""",
            (actor.tenant, project, *source_ids),
        )
        for row in rows:
            if (row['id'], row['revision']) in selected or (
                    not row['revoked'] and row['status'] in {'proposed', 'disputed'}):
                continue
            for ref in refs_from(row['refs']):
                excluded.setdefault((ref.source_id, ref.revision), []).append((ref.start, ref.end))
        return excluded

    def _eligible_literal_pool(self, actor, project, pool, *, valid_at=None, known_at=None):
        """Rebuild a raw pool under the caller's live transaction.

        Context anchors are reconstructed from stored structural scopes, never
        trusted from a caller-supplied label. No model calls belong here.
        """
        claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
        excluded = self._excluded_source_spans(actor, project,
            [r.source_id for e in pool.items for r in e.refs], claims)
        known = timestamp(known_at) if known_at else None
        valid = timestamp(valid_at) if valid_at else None
        result, pending = [], []
        for item in pool.items:
            if item.kind == 'source_context':
                pending.append(item)
                continue
            if item.kind != 'source' or any(e.metadata.get('context_for') != item.id for e in pending):
                raise ValueError('literal pool with attached context anchors required')
            try:
                if len(item.refs) != 1 or item.revision != item.refs[0].revision:
                    raise InvalidEvidence('literal pool revision mismatch')
                ref = item.refs[0]
                actual = self._validate_refs(actor, project, (ref,), known_at=known)[0]
                source = self._source(actor, project, ref.source_id, revision=ref.revision, known_at=known)
                if actual != item.text or (valid and source['recorded_at'] > valid):
                    raise InvalidEvidence('literal pool text/time mismatch')
                if any(a < ref.end and ref.start < b for a, b in excluded.get((ref.source_id, ref.revision), [])):
                    raise InvalidEvidence('literal pool overlaps ineligible assertion')
                context = self._source_context(actor, project, item, excluded, known_at=known_at)
                if context is None or any(e not in context for e in pending):
                    raise InvalidEvidence('stale or forged context anchors')
                result.extend((*pending, item))
            except (AccessDenied, InvalidEvidence):
                pass
            pending = []
        if pending:
            raise ValueError('orphaned context anchors')
        return EvidenceBundle(pool.query, tuple(result), 0, {
            **pool.diagnostics, 'live_pool_items_removed': len(pool.items)-len(result),
        })

    def eligible_relations(self, actor, project, relations, *, valid_at=None, known_at=None):
        """Filter supplied experimental relations under current source eligibility.

        This is a read, not semantic approval or a permission token. Consumers
        must recheck at actual use; a returned relation never authorizes writes,
        source revocation or historical-state modification.
        """
        relations = tuple(relations)
        if len(relations) > 128:
            raise ValueError('relation eligibility batch exceeds128')
        with self.db.transaction():
            self._scope(actor, project)
            return self._eligible_relations(actor, project, relations, valid_at=valid_at, known_at=known_at)

    def _eligible_relations(self, actor, project, relations, *, valid_at=None, known_at=None):
        # Internal callers must hold the same transaction through actual use.
        claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
        items = [item for relation in relations for item in
                 (relation.source, relation.target, *relation.witnesses)]
        excluded = self._excluded_source_spans(actor, project,
            [ref.source_id for item in items for ref in item.refs], claims)
        known = timestamp(known_at) if known_at else None
        valid = timestamp(valid_at) if valid_at else None
        result = []
        for relation in relations:
            if relation.kind not in {'supports', 'contradicts', 'supersedes', 'depends_on'}:
                raise ValueError('invalid relation kind')
            endpoints = (relation.source, relation.target)
            if (endpoints[0].id == endpoints[1].id or not 2 <= len(relation.witnesses) <= 4
                    or any(not any(e.text == w.text and e.refs == w.refs and e.revision == w.revision
                                   for w in relation.witnesses) for e in endpoints)):
                raise InvalidEvidence('relation witnesses do not bind both endpoints')
            try:
                for item in (*endpoints, *relation.witnesses):
                    if item.kind != 'source' or len(item.refs) != 1:
                        raise InvalidEvidence('literal relation witness required')
                    ref = item.refs[0]
                    if ref.revision != item.revision:
                        raise InvalidEvidence('witness revision mismatch')
                    actual = self._validate_refs(actor, project, (ref,), known_at=known)[0]
                    source = self._source(actor, project, ref.source_id, revision=ref.revision, known_at=known)
                    if actual != item.text or (valid and source['recorded_at'] > valid):
                        raise InvalidEvidence('witness text/time mismatch')
                    if any(a < ref.end and ref.start < b for a, b in excluded.get((ref.source_id, ref.revision), [])):
                        raise InvalidEvidence('witness overlaps ineligible assertion')
            except (AccessDenied, InvalidEvidence):
                continue
            result.append(relation)
        return tuple(result)

    def _source_context(self, actor, project, item, excluded_spans, *, known_at=None):
        """Attach eligible literal anchors atomically with a ranked passage."""
        if len(item.refs) != 1:
            return None
        ref = item.refs[0]
        try:
            actual = self._validate_refs(actor, project, (ref,), known_at=timestamp(known_at) if known_at else None)[0]
            if actual != item.text:
                return None
            row = self.db.one(
                "SELECT anchors FROM source_context WHERE tenant=? AND project=? AND source_id=? AND revision=?",
                (actor.tenant, project, ref.source_id, ref.revision),
            )
            if not row:
                return []
            context, seen = [], set()
            for data in json.loads(row["anchors"]):
                anchor = ContextAnchor(**data)
                # Containment, not intersection: never attribute a speaker to
                # text crossing multiple caller-supplied structural scopes.
                if not anchor.scope_start <= ref.start < ref.end <= anchor.scope_end:
                    continue
                if ref.start <= anchor.start < anchor.end <= ref.end or (anchor.start, anchor.end) in seen:
                    continue
                if any(start < anchor.end and anchor.start < end
                       for start, end in excluded_spans.get((ref.source_id, ref.revision), [])):
                    return None
                context_ref = replace(ref, start=anchor.start, end=anchor.end)
                text = self._validate_refs(actor, project, (context_ref,),
                                           known_at=timestamp(known_at) if known_at else None)[0]
                context.append(Evidence(f"{item.id}:{anchor.start}-{anchor.end}", "source_context", text,
                                        (context_ref,), ref.revision, item.score,
                                        {"context_for": item.id, "scope": [anchor.scope_start, anchor.scope_end]}))
                seen.add((anchor.start, anchor.end))
            return context
        except (AccessDenied, InvalidEvidence):
            return None

    def _materialize_derived(self, actor, project, ordered, query, max_chars, excluded_spans, *, valid_at, known_at):
        """Use derived ranking signals, but pack only eligible literal source spans."""
        result = []
        for item in ordered:
            if item.kind == "source":
                result.append(item)
                continue
            try:
                texts = self._validate_refs(
                    actor,
                    project,
                    item.refs,
                    known_at=timestamp(known_at) if known_at else None,
                    historical=bool(valid_at or known_at),
                )
                for ref in item.refs:
                    if any(
                        start < ref.end and ref.start < end
                        for start, end in excluded_spans.get((ref.source_id, ref.revision), [])
                    ):
                        raise InvalidEvidence("navigation overlaps ineligible evidence")
                    source = self._source(
                        actor,
                        project,
                        ref.source_id,
                        revision=ref.revision,
                        known_at=timestamp(known_at) if known_at else None,
                        historical=bool(valid_at or known_at),
                    )
                    if valid_at and source["recorded_at"] > timestamp(valid_at):
                        raise InvalidEvidence("navigation source not available at requested time")
            except (AccessDenied, InvalidEvidence):
                continue
            for ref, text in zip(item.refs, texts):
                literal = Evidence(
                    digest(dump(asdict(ref))),
                    "source",
                    text,
                    (ref,),
                    ref.revision,
                    item.score,
                    {"navigation_origin": item.id, "navigation_kind": item.kind},
                )
                result.append(passage(literal, query, max_chars))
        return result

    def traverse(
        self,
        actor,
        project,
        start,
        *,
        predicates=(),
        max_depth=2,
        max_edges=50,
        valid_at=None,
        known_at=None,
        graph_index=None,
    ):
        if not 1 <= max_depth <= 4 or not 1 <= max_edges <= 200:
            raise ValueError("graph traversal budget exceeded")
        with self.db.transaction():
            self._scope(actor, project)
            # The ledger is the eligibility gate even if graph projections lag.
            claims = self._claims(actor, project, valid_at=valid_at, known_at=known_at)
            frontier, seen, paths = [(start, [])], {start}, []
            while frontier and len(paths) < max_edges:
                node, path = frontier.pop(0)
                if len(path) >= max_depth:
                    continue
                # Graph index provides optional candidate IDs, never authorizes them.
                # Historical traversal uses the ledger since the projection is current-only.
                indexed_ids = None
                if graph_index is not None and not valid_at and not known_at:
                    indexed_ids = set(graph_index.outgoing_ids(actor.tenant, project, node, limit=200))
                for row in claims:
                    if indexed_ids is not None and row["id"] not in indexed_ids:
                        continue
                    if row["subject"] != node or (predicates and row["predicate"] not in predicates):
                        continue
                    step = {k: row[k] for k in ("id", "revision", "subject", "predicate", "value", "kind")}
                    step["refs"] = json.loads(row["refs"])
                    next_path = path + [step]
                    paths.append(next_path)
                    if row["value"] not in seen:
                        seen.add(row["value"])
                        frontier.append((row["value"], next_path))
                    if len(paths) >= max_edges:
                        break
            return paths

    def remember_working_set(self, actor, project, session_id, assertion_ids, *, ttl_seconds=3600):
        if not session_id or len(assertion_ids) > 20 or not 1 <= ttl_seconds <= 86400:
            raise ValueError("working set must have <=20 references and TTL <=24 hours")
        with self.db.transaction():
            self._scope(actor, project)
            allowed = {row["id"] for row in self._claims(actor, project)}
            if not set(assertion_ids) <= allowed:
                raise AccessDenied("working set contains unavailable assertions")
            expiry = timestamp(datetime.fromisoformat(self.clock()) + timedelta(seconds=ttl_seconds))
            self.db.execute(
                """INSERT INTO working_sets VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,project,principal,session_id)
                DO UPDATE SET assertion_ids=excluded.assertion_ids,expires_at=excluded.expires_at""",
                (actor.tenant, project, actor.principal, session_id, dump(list(dict.fromkeys(assertion_ids))), expiry),
            )

    def read_working_set(self, actor, project, session_id):
        with self.db.transaction():
            self._scope(actor, project)
            row = self.db.one(
                "SELECT * FROM working_sets WHERE tenant=? AND project=? AND principal=? AND session_id=?",
                (actor.tenant, project, actor.principal, session_id),
            )
            if not row or row["expires_at"] <= self.clock():
                return []
            ids = set(json.loads(row["assertion_ids"]))
            return [c for c in self._claims(actor, project) if c["id"] in ids]

    def job_status(self, actor, project, job_id):
        with self.db.transaction():
            self._scope(actor, project)
            row = self.db.one(
                "SELECT * FROM jobs WHERE tenant=? AND project=? AND id=? AND principal=?",
                (actor.tenant, project, job_id, actor.principal),
            )
            if not row:
                raise AccessDenied("job unavailable")
            return {key: row[key] for key in ("id", "kind", "state", "attempts", "error_code", "created_at")}
