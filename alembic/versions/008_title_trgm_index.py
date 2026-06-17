"""GIN trigram index on entities.title

Revision ID: 008
Revises: 007
Create Date: 2026-06-17

Purely additive. fuzzy_search's WHERE clause matched titles with
``similarity(COALESCE(e.title, ''), q) > 0.2``. The bare ``similarity() >
threshold`` form is NOT index-eligible (only the ``%`` operator is), so that
branch seq-scanned every row. Content already had ``entities_trgm_idx``; this
adds the equivalent for title so the title branch can switch to the indexed
``e.title % q`` form and the whole WHERE clause is served by GIN bitmap scans.
"""
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS entities_title_trgm_idx "
        "ON entities USING GIN (title gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS entities_title_trgm_idx")
