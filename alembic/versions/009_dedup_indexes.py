"""Indexes for ingestion dedup lookups

Revision ID: 009
Revises: 008
Create Date: 2026-06-18

Purely additive. The cookpadia ingestion path dedups every entity it creates
via /memory/sql lookups that filter on columns with no supporting index:

  - datasources_ext.url        (find_datasource_by_url)
  - sources_ext.url            (find_source_by_url)
  - facts_ext.source_entity_id (list_facts_by_source_entity_id, fact dedup)
  - entities.content + 'fact'  (find_fact_by_content, exact equality)

Each lookup seq-scanned ~339k rows. Under a backfill these run thousands of
times concurrently with bulk INSERTs, saturating Postgres IO and pushing the
single-process braindb past its health-probe and client-timeout windows.

entities.content uses a HASH index (not btree): the dedup is a pure ``=``
match, fact content has no length bound (btree's ~2704-byte row limit would
reject long bodies), and a partial predicate keeps the index scoped to the
fact rows the query actually touches. ``entities_trgm_idx`` is GIN and only
serves ``%``/similarity, never ``=``.

All indexes are built CONCURRENTLY inside an autocommit block so the build
holds no long write lock and cannot be killed mid-transaction by the liveness
probe (cf. migration 008's non-concurrent build, a latent landmine under load).
"""
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS datasources_ext_url_idx "
            "ON datasources_ext (url)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS sources_ext_url_idx "
            "ON sources_ext (url)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS facts_ext_source_entity_id_idx "
            "ON facts_ext (source_entity_id)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS entities_fact_content_hash_idx "
            "ON entities USING HASH (content) WHERE entity_type = 'fact'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS entities_fact_content_hash_idx")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS facts_ext_source_entity_id_idx")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS sources_ext_url_idx")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS datasources_ext_url_idx")
