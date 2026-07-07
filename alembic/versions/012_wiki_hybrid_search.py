"""HNSW index for wiki-article embeddings — hybrid lexical+vector search

Revision ID: 012
Revises: 011
Create Date: 2026-07-07

Wiki articles (``entity_type='datasource' AND source='wiki'``, ~10k rows in
prod) get embeddings backfilled from zero via
``POST /memory/generate-wiki-embeddings`` (mirrors the keyword-embedding
backfill from migration 004) and then written incrementally on every create/
update (see ``_maybe_embed_wiki_datasource`` in ``routers/entities.py``).

HNSW instead of ivfflat: ivfflat's ``lists`` clustering is chosen from the
data present at *build* time — build it before the backfill runs (our case,
since embeddings start NULL) and the clusters are meaningless, degrading scan
quality until a manual ``REINDEX``. HNSW builds its graph incrementally as
rows are inserted/updated, so an index built over an empty (or partially
backfilled) column stays correct as the backfill and later per-write updates
land — no reindex step required after backfill completes. The existing
keyword embedding index (migration 004) predates this pattern and still uses
ivfflat; not touched here since keyword embeddings are already backfilled at
create time and don't share this cold-start problem.

Partial (``WHERE entity_type='datasource' AND source='wiki'``) so the index
only ever covers wiki rows — keyword embeddings (a different semantic space,
already served by ``entities_embedding_idx``) never enter this index's scan.

Built CONCURRENTLY in an autocommit block, same rationale as migration 011:
holds no long write lock, can't be killed mid-transaction by the liveness
probe.
"""
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS entities_wiki_embedding_hnsw_idx "
            "ON entities USING hnsw (embedding vector_cosine_ops) "
            "WHERE entity_type='datasource' AND source='wiki'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS entities_wiki_embedding_hnsw_idx")
