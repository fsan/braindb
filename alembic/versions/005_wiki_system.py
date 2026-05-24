"""wiki system — wiki entity type, wikis_ext, wiki_job queue

Revision ID: 005
Revises: 004
Create Date: 2026-05-16

Purely additive. Mirrors the 004 CHECK-rewrite pattern. No backfill;
existing rows are untouched. Adds the 'wiki' entity type, the wikis_ext
extension table, and the wiki_job queue table that drives the
cron / maintainer / writer pipeline.
"""
from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0. Add 'wiki' to the entity_type CHECK constraint (same DROP/ADD as 004)
    op.execute("ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_entity_type_check")
    op.execute("""
        ALTER TABLE entities ADD CONSTRAINT entities_entity_type_check
        CHECK (entity_type IN ('thought','fact','source','datasource','rule','keyword','wiki'))
    """)

    # 1. Wiki extension table — base entity columns (title/content/summary/
    #    keywords/importance/notes/metadata) are reused; only wiki-specific
    #    structured fields live here.
    op.execute("""
        CREATE TABLE wikis_ext (
            entity_id           UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
            canonical_name      VARCHAR(500) NOT NULL,
            disambiguation      TEXT,
            language            VARCHAR(10) DEFAULT 'en',
            member_keyword_ids  UUID[] DEFAULT '{}',
            revision            INT DEFAULT 1,
            last_synthesised_at TIMESTAMPTZ,
            retired_at          TIMESTAMPTZ,
            redirect_to         UUID REFERENCES entities(id) ON DELETE SET NULL
        )
    """)
    op.execute("CREATE INDEX wikis_ext_canonical_idx ON wikis_ext (lower(canonical_name))")
    op.execute("CREATE INDEX wikis_ext_member_kw_idx ON wikis_ext USING GIN (member_keyword_ids)")

    # 2. Structured maintainer/cron job queue
    op.execute("""
        CREATE TABLE wiki_job (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            job_type        VARCHAR(20) NOT NULL
                            CHECK (job_type IN ('triage','attach','create','consolidate')),
            status          VARCHAR(12) NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','assigned','done','rejected','failed')),
            target_wiki_id  UUID REFERENCES entities(id) ON DELETE CASCADE,
            entity_ids      UUID[] NOT NULL DEFAULT '{}',
            dedupe_key      TEXT NOT NULL,
            rationale       TEXT,
            proposed_name   VARCHAR(500),
            batch_id        UUID,
            created_at      TIMESTAMPTZ DEFAULT now(),
            assigned_at     TIMESTAMPTZ,
            completed_at    TIMESTAMPTZ,
            attempts        INT DEFAULT 0,
            last_error      TEXT
        )
    """)
    # Idempotency: only one active job per logical work item. Once a job is
    # done/rejected the key frees, so a genuinely new later situation can
    # re-propose. Inserts use ON CONFLICT DO NOTHING (same as 004 backfill).
    op.execute("""
        CREATE UNIQUE INDEX wiki_job_dedupe_active_idx
        ON wiki_job(dedupe_key) WHERE status IN ('pending','assigned')
    """)
    op.execute("CREATE INDEX wiki_job_status_idx ON wiki_job(status)")
    op.execute("CREATE INDEX wiki_job_target_idx ON wiki_job(target_wiki_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS wiki_job")
    op.execute("DROP TABLE IF EXISTS wikis_ext")
    # Restore the 004 entity_type CHECK constraint (without 'wiki')
    op.execute("ALTER TABLE entities DROP CONSTRAINT IF EXISTS entities_entity_type_check")
    op.execute("""
        ALTER TABLE entities ADD CONSTRAINT entities_entity_type_check
        CHECK (entity_type IN ('thought','fact','source','datasource','rule','keyword'))
    """)
