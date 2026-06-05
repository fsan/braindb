import logging
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from braindb.db import get_conn
from braindb.routers import agent, entities, memory, relations
from braindb.services.embedding_service import get_embedding_service

_START_TIME = time.time()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = FastAPI(
    title="BrainDB",
    description="Memory database and REST API for LLM agents",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(entities.router)
app.include_router(relations.router)
app.include_router(memory.router)
app.include_router(agent.router)


@app.on_event("startup")
def startup():
    """Initialize the embedding service on startup."""
    emb = get_embedding_service()
    emb.initialize()


@app.get("/health")
def health():
    emb = get_embedding_service()
    return {
        "status": "ok",
        "embeddings": emb.is_available(),
    }


@app.get("/metrics")
def metrics():
    import psycopg2.extras

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT entity_type, COUNT(*) AS count FROM entities GROUP BY entity_type ORDER BY entity_type"
            )
            rows = cur.fetchall()
    entity_counts = {row["entity_type"]: row["count"] for row in rows}
    return {
        "uptime_seconds": int(time.time() - _START_TIME),
        "entities": entity_counts,
        "total_entities": sum(entity_counts.values()),
    }
