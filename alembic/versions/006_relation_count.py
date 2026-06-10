"""relation co-occurrence count for proportional edge weight

Revision ID: 006
Revises: 005
Create Date: 2026-06-10

Purely additive. Adds a `count` column to `relations` (co-occurrence count,
used for proportional edge weighting). Default 1, must be >= 1. Existing rows
get the default. Ported from the cook_wiki patch line so the cook_wiki /
cookpadia ingest pipeline (which reads/writes relations.count) works on main.
"""
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE relations
        ADD COLUMN IF NOT EXISTS count INTEGER NOT NULL DEFAULT 1
        CHECK (count >= 1)
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE relations DROP COLUMN IF EXISTS count")
