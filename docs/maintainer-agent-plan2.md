# BrainDB Wiki System — cron / maintainer / writer

> **Frozen snapshot.** This is the verbatim plan as approved before implementation
> began. It is an immutable historical reference — do **not** edit it as the design
> evolves. The living design doc is `maintainer-agent-plan.md`.

## Context

BrainDB stores a graph of typed entities. Keyword entities act as soft "entity hubs"
(everything about a thing gets `tagged_with` that keyword), but there is **no
synthesised, human-readable page per concept** the way Karpathy's LLM-wiki has. The
prior draft (`docs/maintainer-agent-plan.md`) framed this as keyword-dedup-first. The
user has reframed it as a **three-stage pipeline** and set two hard constraints
(below) that supersede the prior draft.

1. **Cron** — read-only: find keyword/thought/fact entities not connected to any wiki (orphans) and enqueue **one triage case per orphan**.
2. **Maintainer** — pulls **one case at a time** (never the whole batch), researches it against existing wikis + graph via the current APIs, and emits a structured suggestion job for *that case*: attach / create / possible-duplicate.
3. **Wiki writer** — invoked **per wiki**; the LLM consumes that wiki's suggestion jobs and writes/updates the wiki, managing relations itself through existing tools.

### Two governing constraints (from user feedback)

- **C1 — Per-case maintainer.** The maintainer must reason about a single orphan case per invocation. The cron only *enqueues* cases; it never hands the maintainer a bulk dump.
- **C2 — No programmatic destruction without LLM awareness.** No autonomous SQL procedure may delete/repoint relations, retire/merge entities, or change importance behind the LLM's back. The deterministic layer is restricted to: **(1) read-only detection** (orphans — *suggestions only*), **(2) safe non-destructive job-queue plumbing** (enqueue, claim, idempotency, status), and **(3) at most additive bookkeeping that exactly mirrors LLM-authored content**. Every consequential graph mutation is performed by the LLM via existing tools — visible, logged, reversible. Postgres FK `ON DELETE CASCADE` self-healing is acceptable (it is correct DB behaviour we do not author); the resulting dead inline token is *flagged for an LLM*, never auto-edited.

- **C3 — Reuse the existing APIs; no bloat.** BrainDB already has sophisticated search/scoring (`/memory/context` & the `recall_memory`/`quick_search`/`view_tree`/`search_sql` tools: combined fuzzy + full-text + keyword-embedding, graph traversal, temporal decay, `final_rank`). Every stage that needs to *find*, *rank*, or *compare* anything **must call that existing infra**. Do not write a new similarity query, scoring heuristic, or embedding path that duplicates what these already do. New code is allowed only for: the additive migration, the `wiki_job` queue plumbing, the deterministic non-destruction gate (the safety guarantee C2 requires), and prompts. If a proposed piece of code re-implements search/scoring, it is bloat and is cut.

Goals: wikis live **inside the DB** (entities, not files); reuse existing machinery
(embeddings, graph traversal, the agent HTTP endpoint, relations, activity log);
**must not regress** existing endpoints, retrieval, or the ingest watcher; agent track
first, Claude-Code-skill track later.

This file records the **recommended** path only; alternatives/trade-offs are in the
conversation.

---

## Key design decisions

| # | Decision | Choice |
|---|---|---|
| D1 | Wiki granularity | Born one-per-keyword; collapsed toward per-canonical-cluster **by LLM-driven consolidation** over time. `wikis_ext.member_keyword_ids` is the cluster. |
| D2 | Where jobs live | New `wiki_job` table with lifecycle + deterministic `dedupe_key` partial-unique index for idempotency. Two job sources: `triage` rows (cron, one per orphan) and suggestion rows (maintainer: `attach`/`create`/`consolidate`). |
| D3 | Orchestration | Manual endpoints first (`/api/v1/wiki/{cron,maintain,write,jobs}`) driving the existing `POST /api/v1/agent/query`. Maintainer endpoint processes **one triage case per call** (C1). Separate `wiki_scheduler` sidecar (clone of `ingest_watcher.py`) only after endpoints are verified. Ingest watcher never touched. |
| D4 | Inline ref ↔ SQL consistency | Body is source of truth. Writer LLM emits `[[ref:UUID]]` **and** owns its relations via `create_relation`/`delete_relation`. A reconcile step is **additive + advisory only**: it may add a relation that exactly mirrors a ref the LLM wrote; it *flags* (never deletes/repoints) drift as an LLM fix-up case (C2). |
| D8 | Writer robustness | Surgical add/modify/**delete** is allowed but **accounted-for**: writer returns body + a change manifest; an **accounted-change gate** rejects+retries any *undeclared* drop/add or out-of-scope section change (blocks accidental destruction, permits justified deletion). Mandatory contradiction-gathering via existing recall; prior revision snapshotted to the activity log (deleted ≠ destroyed). Fixed contract + template + validation make style robust. See "Wiki document contract" below. |
| D5 | Duplicate wikis | **No new search/scoring code.** Detection is a by-product of the per-case maintainer's existing `recall_memory` call (= `/memory/context`: text + keyword-embedding + graph + decay + `final_rank`, all already built). If that recall surfaces an existing wiki very close to the case's concept, the maintainer emits a `consolidate` suggestion. The **wiki-writer LLM performs the merge** via existing tools, logged, reversible — no `merge_wikis()` SQL, no bespoke cosine query. `not_duplicate`/`duplicate_of` are plain relations the LLM sees via existing relation/graph tools and is prompted to respect (self-clearing without a custom SQL filter). |
| D6 | Summary / disambiguation / language | Reuse `entities.summary` for the cheap one-line header; `wikis_ext.disambiguation` + `wikis_ext.language` (mirrors `datasources_ext.language`). |
| D7 | Driver | In-house agent first (new prompts + reuse `/agent/query`). Claude-Code skill later; persisted `wiki_job` rows are the shared contract so both drivers interoperate. |

---

## Schema — single additive migration `005_wiki_system.py` (`down_revision = "004"`)

Mirrors the `004` CHECK-rewrite pattern. Purely additive; no backfill; existing rows untouched.

```sql
ALTER TABLE entities DROP CONSTRAINT entities_entity_type_check;
ALTER TABLE entities ADD CONSTRAINT entities_entity_type_check
  CHECK (entity_type IN ('thought','fact','source','datasource','rule','keyword','wiki'));

CREATE TABLE wikis_ext (
    entity_id           UUID PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    canonical_name      VARCHAR(500) NOT NULL,
    disambiguation      TEXT,
    language            VARCHAR(10) DEFAULT 'en',
    member_keyword_ids  UUID[] DEFAULT '{}',
    revision            INT DEFAULT 1,
    last_synthesised_at TIMESTAMPTZ,
    retired_at          TIMESTAMPTZ,          -- set by the LLM via tools, not by SQL procedure
    redirect_to         UUID REFERENCES entities(id) ON DELETE SET NULL
);
CREATE INDEX wikis_ext_canonical_idx ON wikis_ext (lower(canonical_name));
CREATE INDEX wikis_ext_member_kw_idx ON wikis_ext USING GIN (member_keyword_ids);

CREATE TABLE wiki_job (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type        VARCHAR(20) NOT NULL
                    CHECK (job_type IN ('triage','attach','create','consolidate')),
    status          VARCHAR(12) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','assigned','done','rejected','failed')),
    target_wiki_id  UUID REFERENCES entities(id) ON DELETE CASCADE,   -- NULL for triage/create
    entity_ids      UUID[] NOT NULL DEFAULT '{}',     -- triage: the single orphan (+context anchors)
    dedupe_key      TEXT NOT NULL,
    rationale       TEXT,
    proposed_name   VARCHAR(500),
    batch_id        UUID,
    created_at      TIMESTAMPTZ DEFAULT now(),
    assigned_at     TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    attempts        INT DEFAULT 0,
    last_error      TEXT
);
CREATE UNIQUE INDEX wiki_job_dedupe_active_idx
  ON wiki_job(dedupe_key) WHERE status IN ('pending','assigned');
CREATE INDEX wiki_job_status_idx ON wiki_job(status);
CREATE INDEX wiki_job_target_idx ON wiki_job(target_wiki_id);
```

**No new embedding path.** Wikis are found through the *existing* retrieval infra:
the body is full-text indexed automatically (the `search_vector` trigger), the wiki
is `summarises`-linked to its member entities and `tagged_with` its keywords, and
keyword embeddings + graph + `final_rank` already route queries to it. We do **not**
add a wiki-embedding generator or a wiki-vs-wiki cosine query.

`RELATION_TYPES` (Python-side only, no DB constraint) gains: `summarises`,
`not_duplicate`, `duplicate_of`, `consolidated_into`.

---

## Inline reference syntax + additive/advisory reconcile

Token in `entities.content`: `[[ref:ENTITY_UUID]]` or `[[ref:ENTITY_UUID|display text]]`.
Regex: `\[\[ref:([0-9a-f-]{36})(?:\|[^\]]*)?\]\]`.

**The writer LLM is responsible for its relations.** The writer prompt instructs it
to call `create_relation` (`wiki --summarises--> entity`, relevance 0.9) for each
entity it cites and `delete_relation` when it removes a citation. The deterministic
`reconcile_wiki_refs(conn, wiki_id, body)` is a **safety net, additive + advisory only**:

1. `cited` = UUIDs parsed from body that exist in `entities`.
2. `current` = `to_entity_id` where `from=wiki_id AND relation_type='summarises'`.
3. **Add**: insert `summarises` for `cited - current` (`ON CONFLICT DO NOTHING`) — mirrors what the LLM wrote in the body.
4. **Declared removals**: for `current - cited` where the UUID is in `manifest.removed_refs`, the writer has already re-typed/handled the relation via tools (gate step 6); the reconciler just confirms consistency. For `current - cited` *not* in the manifest, that is an undeclared drop → already rejected by the gate (this branch should never persist). Dangling refs (cited UUID not in `entities`) → fix-up `triage` job + `log_activity('wiki_ref_drift', ...)`. The reconciler itself still never deletes/repoints — the *writer* does, declared and via tools.

Cited entity later genuinely deleted → FK `ON DELETE CASCADE` removes the relation
(correct DB behaviour, not our code). The orphaned `[[ref:]]` token is flagged for an
LLM rewrite — prose is never blind-edited.

---

## Wiki document contract + writer robustness

The writer's safety does **not** rest on model judgement. Robustness is structural.

### Fixed document skeleton (every wiki, enforced)

```
<!-- wiki:meta canonical_name=... language=en revision=N -->
# {canonical_name}
> **Summary:** {one line, ≤ 280 chars — kept short on purpose}
> **Disambiguation:** {what this is / is NOT; the true meaning(s)}

<!-- section:overview -->        ...prose with [[ref:UUID]]...
<!-- section:timeline -->        ...dated claims, each carrying [[ref:UUID]]...
<!-- section:contradictions -->  ...conflicts flagged inline with BOTH refs...
<!-- section:sources -->         ...narrative provenance...
<!-- section:references -->      AUTO-GENERATED — do not hand-write
- [[ref:UUID]] — one-line what this entity contributes
```

Anchors are HTML comments → invisible in render, deterministically splittable. The
`section:references` ledger is **machine-generated** from the parsed `[[ref:]]` set
on every save (the LLM writes refs inline in prose; it never authors the ledger), so
inline tokens, the ledger, and the `summarises` SQL relations all derive from one
parse and **cannot disagree**.

### Surgical editing IS allowed — the rule is "accounted-for", not "append-only"

The writer **must** be able to revise a specific part: rewrite a sentence, drop a
claim that is wrong/superseded, resolve a contradiction by removing the losing side.
The earlier "may only add refs" idea is wrong — it would freeze bad content forever.
The real guarantee is: **every removal/modification is deliberate, justified, and
recoverable; nothing is lost silently or accidentally.**

The writer returns two things, not one: the new body **and a structured change
manifest**:

```
{ "added_refs":   [UUID, ...],
  "removed_refs": [{ "ref": UUID, "reason": "superseded|contradicted|wrong|merged|irrelevant",
                     "note": "one line", "prior_text": "the sentence/para removed" }],
  "modified_sections": ["timeline", ...],
  "contradictions_resolved": [{ "kept": UUID, "demoted": UUID, "how": "..." }] }
```

Edit mode is still set by **job type, not the model** (`create` = template;
`attach` = section-scoped, untargeted sections byte-identical; `consolidate`/
resynthesise = full rewrite), but within the targeted scope the writer may freely
add/modify/delete **provided the manifest accounts for it**.

### Accounted-change gate (the actual guarantee)

Around every writer save, in the same transaction:

1. `R_before` / `R_after` = `{[[ref:UUID]]}` parsed from old / new body.
2. `dropped = R_before − R_after`, `gained = R_after − R_before`.
3. **Every** UUID in `dropped` must appear in `manifest.removed_refs` with a valid `reason`; **every** UUID in `gained` must appear in `manifest.added_refs`. An undeclared drop or add ⇒ violation (this is what blocks *accidental* destruction while *allowing* declared deletion).
4. **Section guard** (`attach` only): non-targeted sections hash-identical; a change outside `manifest.modified_sections` ⇒ violation.
5. **Structural validation**: required anchors present; `summary` ≤ 280 chars; `disambiguation` non-empty; every surviving `[[ref:UUID]]` resolves in `entities`.
6. **Provenance is preserved, not erased.** A declared removal does **not** silently delete the entity or just drop the `summarises` edge into the void. The writer must, via existing tools, either (a) replace `summarises` with a typed relation that records the judgement — `contradicts` (this member opposes the consensus), `challenges`, or keep a low-relevance historical `summarises` — or (b) raise a fix-up `triage` job if the source entity itself looks wrong. The writer never deletes *other* entities; it only re-types its own link and explains why. `removed_refs[].prior_text` + reason are written to the wiki `notes` / activity log.
7. Any violation ⇒ **rollback**, job → `pending` with `last_error`, retry with the explicit defect ("undeclared drop of X", "section Z changed but not in manifest", "summary too long"). Capped by `attempts`; exhaustion ⇒ `failed`, surfaced via `GET /jobs`. Never a silent bad write.

### Contradiction handling (the writer must reason about opposition)

Before editing, the writer is **required** to gather opposition context using the
**existing infra** (C3): `recall_memory` / `view_tree` / `view_entity_relations`
over the member entities surface any `contradicts`/`challenges` relations and
semantically opposed claims (the existing scoring already clusters them). The writer
prompt mandates a populated `section:contradictions`: every detected opposition is
either (a) reconciled in prose with **both** refs kept, or (b) one side explicitly
demoted via the manifest (`contradictions_resolved`) with reasoning — never one side
silently dropped. The gate cross-checks: a UUID that vanished and was part of a
detected contradiction must appear in `contradictions_resolved`.

### Reversibility (deleted ≠ destroyed)

Every writer save first snapshots the prior `content` + parsed refs into the activity
log (`operation='wiki_revise'`, with `revision` n→n+1) before mutation. So any
removal — even a correct one — is auditable and restorable from the log. "Edited a
specific part / removed something that doesn't make sense" is fully supported;
"content vanished with no record or reason" is structurally impossible.

This makes "surgical edits yes, destruction no" a checked invariant, not a hope —
true regardless of which LLM profile is active.

### Style robustness levers (in `wiki_writer_prompt.md`)

- The skeleton above is the mandatory output contract (sections, order, anchors, ref syntax, tone: encyclopedic, third-person, dated, contradictions flagged with both refs, every non-trivial claim carries a `[[ref:]]`).
- A **golden template** for `create` so structure is identical across all wikis from day one.
- A **few-shot exemplar**: one well-formed wiki + a before/after `attach` showing existing content preserved and the new member integrated.
- Deliberately **small focused context** (one wiki's body + only that wiki's new members) — the maintainer being per-case keeps the writer's input bounded; focused context is itself a major robustness lever.

---

## Pipeline mechanics

**Cron** (`POST /api/v1/wiki/cron`, pure SQL, read-only + safe enqueue, no LLM):
select keyword/thought/fact entities with no `summarises`/member link to any wiki and
not already in an active job; for **each** orphan insert one `triage` `wiki_job`
(`dedupe_key = triage:<entity_id>`, `ON CONFLICT DO NOTHING`). Returns counts.
Idempotent and non-destructive by construction.

**Maintainer** (`POST /api/v1/wiki/maintain` — processes **one** triage case per call,
C1): claim a single `triage` job (`FOR UPDATE SKIP LOCKED`, LIMIT 1). Build a focused
prompt for *that one orphan only* (its content + its graph neighbourhood via
`recall_memory`/`view_tree`, plus the candidate existing wikis' `summary`/
`disambiguation` found via search). The agent decides for this case: attach to wiki W
/ create new wiki / flag possible duplicate of wikis. The service parses the agent's
structured result and writes the corresponding suggestion job (`attach`/`create`/
`consolidate`) with a service-computed `dedupe_key`
(`attach:<wiki>:<sorted ents>` / `create:<sorted ents>` /
`consolidate:<sorted wikis>`, `ON CONFLICT DO NOTHING`), then closes the triage job
(`done`/`rejected`). A loop/sidecar calls this endpoint repeatedly to drain the
triage queue one case at a time.

**Writer** (`POST /api/v1/wiki/write {wiki_id? | job_ids? | next_pending}`): pick one
target (a wiki id, or a `create`/`consolidate` job group). In one `get_conn()`
transaction: `SELECT pg_try_advisory_xact_lock(hashtext('wiki:'||id))` → claim that
target's pending suggestion jobs (`FOR UPDATE SKIP LOCKED`) → **snapshot prior
`content`+refs to activity log (`wiki_revise`)** + per-section hashes → one agent run
with a focused prompt (current body pre-split by anchors for `attach` + cited members
+ **mandatory contradiction context gathered via existing `recall_memory`/`view_tree`**;
edit mode chosen by job type) → the LLM returns **new body + change manifest** and
**calls `create_relation`/`delete_relation`/`update_entity` itself** for citations
and declared removals → **accounted-change gate** (every drop/add declared in
manifest; section guard; structural validation; contradiction cross-check; on
failure: rollback, job→`pending`, retry with defect, cap by `attempts`) →
regenerate `section:references` ledger from parsed refs → additive
`reconcile_wiki_refs` consistency check → bump `revision`, set `last_synthesised_at`
→ finalise jobs → `log_activity('wiki_write', ...)`.

**Consolidation reuses existing scoring; LLM-performed (C2).** There is **no
dedicated dedup query**. Duplicate detection falls out of the maintainer's normal
per-case `recall_memory` (the existing `/memory/context` scoring — text +
keyword-embedding + graph + decay + `final_rank`). When that recall returns an
existing wiki ranked very close to the case's concept, the maintainer emits a
`consolidate` suggestion. It already has the markers in view (the recall's graph
neighbourhood / `view_entity_relations` exposes any `not_duplicate`/`duplicate_of`)
and the prompt tells it not to re-propose a cleared pair — self-clearing with zero
custom SQL. The writer agent then, for that job, deliberately and with full context:
uses the **existing `final_rank`/importance signals from that same recall** to decide
which wiki is canonical, rewrites the canonical body to absorb the other's content
and refs, moves/creates `summarises` relations via tools, sets the loser's
`importance` low + `retired_at` + `redirect_to` via `update_entity`, and creates the
`duplicate_of` (or `not_duplicate` if distinct) marker via `create_relation`. Every
step is a logged tool call, reversible, never a hidden bulk SQL mutation.

---

## Reuse map (C3) — existing infra per stage, and what we are NOT building

| Stage | Needs to… | Uses existing | New code? |
|---|---|---|---|
| Cron | find orphans | one read-only SQL `NOT EXISTS` against `relations` (no scoring involved) | tiny query + enqueue only |
| Maintainer | find candidate wikis for a case; spot duplicates | `recall_memory` / `/memory/context` (text+embedding+graph+decay+`final_rank`), `view_tree`, `search_sql` | **none** — prompt + parse only |
| Writer | pull a wiki's body/members; rank canonical in a merge | `get_entity`, `recall_memory`, `view_entity_relations`; existing `final_rank`/importance from recall | **none** for retrieval/scoring |
| Mutations | create/edit wiki, link, retire, merge | `create_relation`, `delete_relation`, `update_entity` tools | **none** — existing tools |
| Ranking wikis in results | surface wikis well | existing `final_rank` + `importance` + `decay_rate_wiki` config | **none** (config value only) |

**Explicitly NOT building:** no wiki-vs-wiki cosine query, no `find_similar_keywords`
retarget, no wiki-embedding generator, no winner-selection heuristic in code, no
bespoke dedup pass/filter, no scoring formula change. Detection and ranking are
entirely the existing search infra; the LLM consumes its output.

---

## No-regression guarantees

- `context.py:~220` keyword filter is `entity_type != "keyword"`; `wiki` passes unchanged — **do not edit that line**.
- Add `"wiki": settings.decay_rate_wiki` (default `0.0`) to `DECAY_RATES`; `decay_rate_wiki` to `config.py`. Config addition, **not** a ranking-formula change.
- Add `"wiki": ("wikis_ext", "...")` to `EXT_QUERIES` (context.py) and a `wiki` branch to `ENTITY_SELECT`/`_flatten()` (entities.py) — same mechanical pattern as the other 5 types.
- `graph.py` already walks all relation types; `summarises` traversed unmodified. No graph/search code change. Existing entity types untouched.
- Migration additive; ingest watcher and `api`/`watcher` compose services untouched.

---

## Files to create / modify

| File | New/Mod | Purpose |
|---|---|---|
| `alembic/versions/005_wiki_system.py` | new | entity type + `wikis_ext` + `wiki_job` (raw SQL, down_revision "004") |
| `braindb/services/wiki_jobs.py` | new | **non-destructive only**: orphan query, per-orphan triage enqueue, `dedupe_key`, single-job claim (SKIP LOCKED), status transitions, advisory lock, anchor splitter/joiner, **accounted-change gate** (manifest vs parsed-ref diff + section-hash + structural + contradiction cross-check), prior-revision snapshot to activity log, references-ledger regenerator, additive+consistency `reconcile_wiki_refs`. **No search/scoring code** (C3) — detection/ranking/contradiction-context delegated to existing `recall_memory`/`/memory/context`. |
| `braindb/routers/wiki.py` | new | `POST /cron`, `/maintain` (one case/call), `/write` (gate + retry loop), `GET /jobs` under `/api/v1/wiki` |
| `braindb/agent/prompts/wiki_maintainer_prompt.md` | new | maintainer — reason about one case, emit one structured suggestion |
| `braindb/agent/prompts/wiki_writer_prompt.md` | new | writer — mandatory skeleton/anchors/style contract, golden template, few-shot exemplar, edit-mode rules, **change-manifest output**, mandatory contradiction-gathering via existing recall, own relations via tools, consolidate deliberately |
| `braindb/wiki_scheduler.py` | new (Stage 2) | sidecar; clone of `ingest_watcher.py` loop; drains triage one case at a time |
| `braindb/schemas/entities.py` | mod | `WikiCreate`/`WikiRead`/`WikiUpdate`, add to `AnyEntityRead` |
| `braindb/routers/entities.py` | mod | wiki CRUD + extend `ENTITY_SELECT`/`_flatten()`; hook additive `reconcile_wiki_refs` |
| `braindb/schemas/relations.py` | mod | add `summarises`, `not_duplicate`, `duplicate_of`, `consolidated_into` |
| `braindb/services/context.py` | mod | `DECAY_RATES["wiki"]`, `EXT_QUERIES["wiki"]` |
| `braindb/config.py` | mod | `decay_rate_wiki`, `wiki_dedup_similarity_threshold`, interval knobs |
| `braindb/main.py` | mod | `app.include_router(wiki.router)` (1 line) |
| `docker-compose.yml` | mod (Stage 2) | add `wiki_scheduler` service (clone of `watcher`); `api`/`watcher` untouched |
| `docs/maintainer-agent-plan2.md` | new | **frozen** verbatim snapshot of this approved plan (step 0) — historical reference, not edited afterward |
| `docs/maintainer-agent-plan.md` | mod | the *living* design doc — update to the evolved pipeline + C1/C2/C3 constraints + writer accounted-change model; iterated as implementation proceeds |

No new Python dependencies.

---

## Staged build order

0. **Freeze a historical snapshot.** Before any code or further plan edits, copy this approved plan verbatim to `c:\Users\dimkn\source\repos\cityfalcon\braindb\docs\maintainer-agent-plan2.md` (sibling to the original `maintainer-agent-plan.md`). This is an immutable reference point: the live plan will keep moving as we implement and test, but `maintainer-agent-plan2.md` preserves the design as approved. (`maintainer-agent-plan.md` is updated separately, per the files table.)
1. **Migration 005** + `schemas`/`entities.py`/`relations.py`/`context.py`/`config.py` wiki CRUD wiring. Verify wiki entities create/read/rank and no retrieval regression.
2. **`services/wiki_jobs.py`** + `routers/wiki.py` `/cron` and `/jobs` (pure SQL, no LLM, non-destructive). Verify per-orphan triage enqueue + idempotency.
3. **`/maintain`** (one case/call) + maintainer prompt. Verify a single triage case → one suggestion job; re-run → no dupes; queue drains case by case.
4. **`/write`** + writer prompt + golden skeleton + **accounted-change gate** + revision snapshot + ledger regen + `reconcile_wiki_refs`. Verify: a *declared* removal (claim demoted with reason, relation re-typed via tools) succeeds and is restorable from the `wiki_revise` log; an *undeclared* drop is rejected+retried; a detected contradiction left unresolved is rejected; untargeted sections on `attach` stay byte-identical; structural validation rejects a bad-style draft.
5. **LLM consolidation** (no new query): duplicate spotted via the maintainer's existing `recall_memory`; writer-driven merge through existing tools + `not_duplicate`/`duplicate_of` self-clearing. Verify every mutation was a logged tool call and is reversible, and that no new search/scoring code was added (C3).
6. **Stage 2**: `wiki_scheduler.py` sidecar + compose service (drains triage one case at a time).
7. **Later track**: Claude-Code `braindb` skill variant driving the same `/api/v1/wiki/*` endpoints without the agent.

---

## Verification (end-to-end)

Pre-state: README + Karpathy gist already ingested → many keyword/fact entities.

1. `POST /api/v1/wiki/cron` → one `triage` job per orphan; re-run → no duplicates.
2. `POST /api/v1/wiki/maintain` → consumes exactly **one** triage case, produces one suggestion job; repeat calls drain the queue one at a time.
3. `GET /api/v1/wiki/jobs` → triage + suggestion jobs visible with status.
4. `POST /api/v1/wiki/write {next_pending:true}` → wiki entity with skeleton anchors, `summary` (≤280), `disambiguation`, body `[[ref:UUID]]`, auto-generated references ledger matching the inline tokens and the `summarises` relations exactly. Then exercise surgical editing: an `attach` that **deliberately removes** a now-wrong claim succeeds when the manifest declares it (relation re-typed to `contradicts`/flagged via tools, prior text in the `wiki_revise` log → restorable); the same removal **without** a manifest entry is rolled back and retried (`last_error`/`attempts`); a member that contradicts the consensus forces a populated `section:contradictions` or an explicit demotion; untargeted sections stay byte-identical.
5. `POST /api/v1/memory/context {"queries":["What does the system know about BrainDB?"]}` → BrainDB wiki ranks above individual facts; existing entity types returned exactly as before (baseline unchanged).
6. Seed a near-duplicate wiki → the maintainer's normal `recall_memory` for a related case surfaces it (existing scoring, no new query) → `consolidate` suggestion → writer LLM merges deliberately: activity log shows each `create_relation`/`update_entity` call; loser is soft-retired and still resolves via `GET /entities/{id}`; pair never re-flagged.
7. Delete an entity cited by a wiki → relation removed by FK cascade; dead `[[ref:]]` flagged as a fix-up case (no prose auto-edit).
8. Re-run cron over a fully-wiki'd corpus → 0 new triage jobs (self-clearing verified).
