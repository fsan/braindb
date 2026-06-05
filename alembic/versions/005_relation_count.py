"""relation co-occurrence count for proportional edge weight

Revision ID: 005
Revises: 004
Create Date: 2026-04-22
"""
from alembic import op

revision = "005"
down_revision = "004"
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
