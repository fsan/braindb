"""Index for the source-scoped recent-entities listing

Revision ID: 010
Revises: 009
Create Date: 2026-06-18

Purely additive. cookpadia's /facts page calls list_recent(entity_type=
'datasource', source='wiki') -> a SELECT filtered on (entity_type, source) and
ORDER BY created_at DESC LIMIT 40. No index covered that combination: the
planner BitmapAnd'd entities_type_idx (~49k 'datasource' rows) against
entities_source_idx (~6.8k 'wiki' rows), heap-fetched ~3000 wide rows, then
sorted. Warm it was ~17ms, but under concurrent ingest the buffer cache is
evicted, the heap fetch hits disk, and the query crossed the /memory/sql 5s
statement_timeout -> 400 -> /facts 500.

A composite btree on (entity_type, source, created_at DESC) lets Postgres do an
index-ordered scan of just the matching rows (no 49k-row bitmap, no separate
sort), serving the LIMIT 40 from the index head. created_at is in the key so
the ORDER BY is satisfied by the index order.

Built CONCURRENTLY in an autocommit block so the build holds no long write lock
and can't be killed mid-transaction by the liveness probe (cf. migration 008's
non-concurrent build — deploy with ingestion paused so the concurrent build
isn't blocked by long-lived writer transactions).
"""
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS entities_type_source_created_idx "
            "ON entities (entity_type, source, created_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS entities_type_source_created_idx")
