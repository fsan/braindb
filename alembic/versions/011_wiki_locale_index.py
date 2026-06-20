"""Index the locale-scoped datasource counts

Revision ID: 011
Revises: 010
Create Date: 2026-06-20

Purely additive. cookpadia's per-locale landing counts call
count_facts_and_docs_by_locale() and count_wiki_facts_by_locale() -> a
GROUP BY on ``metadata->>'locale'`` filtered by (entity_type, source). The
combined facts+docs count runs on EVERY page (it backs the locale article
counter in the base template), and count_entities(source='cookpad') backs
/documents. Migration 010's (entity_type, source, created_at DESC) index
serves the ORDER BY created_at listing but NOT this aggregate: the GROUP BY
key ``metadata->>'locale'`` is not in 010's index, so the plan does an index
scan on (entity_type, source) and then HEAP-fetches every matching row to read
``metadata->>'locale'`` -- which detoasts the out-of-line JSONB ``metadata``
column per row. Warm that is ~12ms, but the ~5k datasource rows are scattered
cold across the 290MB TOAST relation; under concurrent ingest the buffer cache
is evicted, the heap+TOAST fetch hits disk, and the query crossed the
/memory/sql 5s statement_timeout -> 400 -> degraded/blocked page loads.

A composite btree on (entity_type, source, (metadata->>'locale')) carries the
extracted locale in the index key, so the (entity_type, source) filter and the
GROUP BY on ``metadata->>'locale'`` are both served from the index with no heap
fetch and no JSONB detoast. The index is non-partial so it covers every
``source`` (the facts+docs count touches both 'wiki' and 'cookpad'), not just
one. Validated on prod: the planner picks it naturally (no hints) and the
heap/TOAST access disappears from the plan.

Built CONCURRENTLY in an autocommit block so the build holds no long write lock
and can't be killed mid-transaction by the liveness probe (cf. migration 008's
non-concurrent build -- deploy with ingestion paused so the concurrent build
isn't blocked by long-lived writer transactions).
"""
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS entities_type_source_locale_idx "
            "ON entities (entity_type, source, (metadata->>'locale'))"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS entities_type_source_locale_idx")
