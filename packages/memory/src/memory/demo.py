"""No credentials, network calls, existing databases or production writes."""

import json

from . import AuthContext, ExtractiveCompiler, MemoryService, Store, Worker


def main():
    store = Store.sqlite()
    store.migrate()
    memory = MemoryService(store)
    owner = AuthContext("demo-tenant", "alice", administrator=True)
    memory.create_project(owner, "orion", "Orion")
    receipt = memory.ingest(
        owner, "orion", "Orion rollout was postponed because the security audit failed.", request_key="meeting-1"
    )
    worker = Worker(memory, ExtractiveCompiler())
    worker.drain()
    from .models import ClaimDraft
    from .service import refs_from

    proposal = memory.list_claims(owner, "orion", include_candidates=True)[0]
    # Explicit human-reviewed change, not an automatic approval inferred by LLM.
    decision = memory.commit(
        owner,
        "orion",
        [
            ClaimDraft(
                subject="Orion rollout",
                predicate="BLOCKED_BY",
                value="security audit",
                text=proposal["text"],
                refs=refs_from(proposal["refs"]),
                kind="decision",
                status="supported",
            )
        ],
    )[0]
    worker.drain()
    print(
        json.dumps(
            {
                "receipt": receipt.state,
                "wiki": memory.read_wiki(owner, "orion", level=2),
                "answer_context": memory.recall(owner, "orion", "Why was Orion rollout postponed?").context,
                "paths": memory.traverse(owner, "orion", "Orion rollout"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    memory.revoke(owner, "orion", decision["id"])
    print("After revocation, published wiki blocks:", len(memory.read_wiki(owner, "orion")["blocks"]))
    store.close()


if __name__ == "__main__":
    main()
