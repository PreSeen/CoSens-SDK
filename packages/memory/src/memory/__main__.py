"""Trusted local administration only; not an unauthenticated HTTP endpoint."""

import argparse
import os

from . import ExtractiveCompiler, MemoryService, Store, Worker
from .compiler import OpenAICompatibleCompiler
from .demo import main as demo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("demo", "init", "worker"))
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--db", default="memory.db")
    parser.add_argument("--compiler", choices=("extractive", "llm"), default="extractive")
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument("--neo4j", action="store_true", help="use explicit FRIDAY_NEO4J_* settings")
    parser.add_argument(
        "--embeddings", action="store_true", help="process embed jobs with configured pgvector and endpoint"
    )
    args = parser.parse_args()
    if args.command == "demo":
        return demo()
    if not 1 <= args.max_jobs <= 10000:
        parser.error("max-jobs must be between 1 and 10000")
    store = Store.postgres(os.environ["FRIDAY_POSTGRES_DSN"]) if args.backend == "postgres" else Store.sqlite(args.db)
    driver = None
    try:
        if args.command == "init":
            store.migrate()
            print("Ledger schema initialized in the selected database; no projects created.")
            return
        compiler = (
            ExtractiveCompiler()
            if args.compiler == "extractive"
            else OpenAICompatibleCompiler(
                base_url=os.environ["FRIDAY_LLM_BASE_URL"], model=os.environ["FRIDAY_LLM_MODEL"]
            )
        )
        sink = None
        if args.neo4j:
            from neo4j import GraphDatabase

            from .adapters import Neo4jGraph

            driver = GraphDatabase.driver(
                os.environ["FRIDAY_NEO4J_URI"],
                auth=(os.environ["FRIDAY_NEO4J_USER"], os.environ["FRIDAY_NEO4J_PASSWORD"]),
            )
            sink = Neo4jGraph(driver, database=os.environ.get("FRIDAY_NEO4J_DATABASE", "neo4j"))
            sink.migrate()
        embedder = index = None
        if args.embeddings:
            from .adapters import PgVectorIndex
            from .compiler import OpenAICompatibleEmbeddings

            model = os.environ["FRIDAY_EMBEDDING_MODEL"]
            dimensions = int(os.environ["FRIDAY_EMBEDDING_DIMENSIONS"])
            index = PgVectorIndex(store, dimensions=dimensions, model=model)
            index.migrate()
            embedder = OpenAICompatibleEmbeddings(
                base_url=os.environ["FRIDAY_EMBEDDING_BASE_URL"], model=model, dimensions=dimensions
            )
        worker = Worker(MemoryService(store), compiler, graph_sink=sink, embedder=embedder, vector_index=index)
        print(f"Processed {worker.drain(max_jobs=args.max_jobs)} jobs; inspect job_status for outcomes.")
    finally:
        store.close()
        if driver:
            driver.close()


if __name__ == "__main__":
    main()
