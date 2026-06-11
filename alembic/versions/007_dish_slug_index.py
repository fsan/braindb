"""partial expression index on cookpad datasource dish_slug

Revision ID: 007
Revises: 006
Create Date: 2026-06-11

Purely additive. The cook_wiki/cookpadia retrieve_sources gate counts distinct
cookpad datasources by ``metadata->>'dish_slug'`` (see
``count_recipes_for_dish_slug``). The only matching indexes were on the
low-cardinality ``entity_type`` / ``source`` columns, so that count fell back
to a sequential scan and tripped Postgres ``statement_timeout`` as the
datasource table grew — dead-lettering the queue item.

A partial expression index on the slug, scoped to the rows the query touches
(cookpad datasources), turns the count into an index scan.
"""
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS entities_dish_slug_idx
        ON entities ((metadata->>'dish_slug'))
        WHERE entity_type = 'datasource' AND source = 'cookpad'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS entities_dish_slug_idx")
