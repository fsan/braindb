# BrainDB Wiki System — cron / maintainer / writer (living design doc)

> **Living document.** This is the iterated source of truth and is updated as
> implementation proceeds. The frozen, as-approved snapshot is
> [`maintainer-agent-plan2.md`](maintainer-agent-plan2.md) — do not edit that one.

> **Operating model (current):** wiki maintenance is **hands-off, default-on**.
> `wiki_scheduler` is a normal always-on compose sidecar (same posture as the
> ingest `watcher`, no opt-in profile) that loops cron(~20m) → maintain →
> write autonomously. The `/api/v1/wiki/{cron,maintain,write}` endpoints are
> **dev/debugging only**, never the operating procedure. The maintainer
> staleness guard + skip-self-clearing keep it idempotent and cheap. Disable
> for cost like the watcher (exclude the service / scale to 0). Inspection
> (`export_wikis`) is an optional read-only dev tool, outside the operating
> path; no test scaffolding lives in operational modules.

## ⚠ Correction applied (supersedes earlier "gate/manifest/ledger" design)

The first implementation inserted programmatic algorithms between the process
and the LLM that destroyed its grasp of reality (e.g. "Subject A is an ML
engineer", "Koutsoumpos is a marine engineer", "Artificial Intelligence" =
one NVIDIA earnings call). Root cause: per-orphan pinhole context, an
accounted-change gate that *blocked self-correction*, a rigid JSON manifest, a
code-generated references ledger, and prompts that never told the LLM to
investigate. **Principle reinstated: programmatic = process / queue /
bookkeeping / commands / reversibility ONLY; the LLM owns all
understanding/identity/content/revision and must research with the existing
tools.**

What changed in code (net negative LOC, no new machinery):
- **Deleted** `accounted_change_gate`, `regenerate_references_ledger`,
  `split_sections`, `_structural_errors`, the JSON manifest contract, the
  section-hash guard, and `keywords=[canonical_name]`.
- `apply_manifest_relations` → **`reconcile_summarises_additive`**: creates a
  `summarises` edge per inline `[[ref:]]`; **never deletes/re-types behind the
  LLM** (the LLM calls `delete_relation` itself if needed).
- Writer returns **only the body** (`<<<WIKI_BODY>>>`); consolidate adds one
  command line `<<<CANONICAL: id>>>`. Body persisted **verbatim**; prior
  version snapshotted to `wiki_revise` (reversible). Wiki `keywords` read from
  the LLM's own `<!-- wiki:meta … keywords=… -->` line, else empty.
- Maintainer & writer prompts rewritten: **research-first** with
  `recall_memory` + `delegate_to_subagent` (SQL = rare aggregation exception);
  identity/scope discipline (no third-party attribute transfer, no invented
  identity, distinct entities stay distinct); **represent ambiguity** instead
  of fabricating; writer **MUST revise** summary/disambiguation/scope on
  better data (self-healing); no keyword-token citations. Agent turns raised
  so it can actually investigate/delegate.
- New maintainer action **`ambiguous`** (treated as a deliberate skip →
  self-clears via `run_cron`).
- **Tool-priority** correction applied everywhere: `system_prompt.md`
  (TOOL PRIORITY rule + Example 3 rewritten), `skills/braindb/SKILL.md`,
  `skills/braindb-agent/SKILL.md`, `CLAUDE.md`, `BRAINDB_GUIDE.md`,
  `export_wikis.py` consistency/checklist. `recall_memory`/`/memory/context`
  + subagents are the default; `/memory/sql` is an aggregation-only exception.

Frozen snapshot `maintainer-agent-plan2.md` is intentionally left as the
original approved record. The cron / claim / skip-self-clear / soft-retire /
snapshot bookkeeping is unchanged.

### Self-heal test result (Subject A) — honest

- **Structural fix: PASS.** No cage; writer revises freely; prior versions
  snapshotted (`wiki_revise` rev 1→4, reversible); LLM authored body/keywords/
  ledger; additive reconcile; writer **did** research via `recall_memory`.
- **Cooperative/radical policy: PASS (mechanically).** With the
  cooperative-default + strong-conviction + mandatory-subagent-confirmation
  prompt, the writer stayed cooperative, **detected the conflation**, and
  **delegated a subagent** to independently resolve identity before acting —
  exactly the requested guardrail.
- **Correctness (first attempt): FAIL.** Then fixed — see RESOLVED below. The
  earlier "root cause is irreducibly DATA identity" verdict was **wrong**: it
  was a *process* failure (anchored subagent delegation + the existing wrong
  page acting as a top-ranked recall attractor + greedy positive
  same-first-name matching + richness-over-correctness).

### RESOLVED — fix verified (2026-05-16)

Three non-bloat changes (prompt + one-time safe reset, no code/gates):
1. **Non-anchored resolution delegation** (writer prompt): the writer MUST
   delegate IDENTITY RESOLUTION giving the subagent **only raw `id: content`
   facts** — never the page name, its claims, or an expected answer — with
   explicit DISQUALIFIERS and an AMBIGUOUS bucket; then writes only the
   resolved subject's facts.
2. **Exclusion + circuit-breaker** (writer & maintainer prompts): a shared
   first-name fact not uniquely tied is AMBIGUOUS → excluded; correctness over
   richness; shrink to an honest stub if unresolved.
3. **Safe clean slate**: deleted wiki layer only (7 wikis, 774 jobs,
   wiki-only relations). Knowledge byte-identical (fact 134, thought 23,
   source 8, datasource 7, keyword 603, activity_log 1199 — unchanged).

Re-created "Subject A" via the corrected flow (logs confirm the
verbatim non-anchored template, no leakage). Result page:
- Summary: "A Greek youth and natural tinkerer born in 2011 who aspires to
  become a boat mechanic." ✓
- Disambiguation: explicitly "the nephew of the ML engineer Dimitrios
  Koutsoumpos; **not** the professional AI/ML engineer at CityFalcon." ✓
- The ambiguous professional "Dimitris" facts (ML engineer / 18-yr investing
  / coaching) were **correctly excluded**, not fused. Consistency ✓.

Conclusion: conflation was a **process** failure, now fixed with prompt +
safe reset only — no new code, gates, or bloat. Caveats: verified on the
Subject A case in create mode; the ~700 triage backlog still to be drained,
and per-wiki runs are slow (recall + a real resolution subagent on
gemma-4-31B → minutes each → this is background-scheduler work, not
interactive). Upstream fact-level identity anchoring remains a *possible
future enhancement*, but is **not required** to get correct pages.

## What this is

A wiki layer inside BrainDB. Wikis are synthesised, human-readable pages
(`entity_type = 'wiki'`) about one concept each, built from the
keyword/thought/fact entities that concern it — Karpathy-style, but stored as
entities (not files) and kept consistent with the graph.

Three-stage pipeline:

1. **Cron** (`POST /api/v1/wiki/cron`) — read-only orphan scan; enqueues one
   `triage` job per entity not yet connected to a wiki. Idempotent.
2. **Maintainer** (`POST /api/v1/wiki/maintain`) — processes **exactly one**
   triage case per call (C1); the existing agent decides
   attach / create / consolidate / skip and a structured suggestion job is
   persisted.
3. **Writer** (`POST /api/v1/wiki/write`) — one wiki per call. The agent
   authors the body + a change manifest; a deterministic **accounted-change
   gate** validates it; the references ledger and `summarises` relations are
   reconciled from the body+manifest; the prior revision is snapshotted.

Inspection: `GET /api/v1/wiki/jobs`. Always-on driving (Stage 2): the
`wiki_scheduler` sidecar, **opt-in** via the `wiki` compose profile.

## Governing constraints

- **C1 — per-case maintainer.** Never a bulk dump; one orphan per invocation.
- **C2 — no programmatic destruction without LLM awareness.** Deterministic
  code is limited to read-only detection, safe queue plumbing, and additive
  bookkeeping that mirrors LLM-authored content / executes the LLM's explicit
  manifest. Every consequential change is logged and reversible.
- **C3 — reuse existing APIs; no bloat.** Detection/ranking/contradiction
  context all go through the existing `recall_memory` / `/memory/context`
  scoring. No new similarity query, scoring heuristic, or embedding path.

## Writer robustness (the accounted-change model)

Surgical add/modify/**delete** is allowed; *undeclared* or *accidental* loss
is impossible. The writer returns body + manifest
(`added_refs` / `removed_refs[{ref,reason,note,prior_text}]` /
`modified_sections` / `contradictions_resolved` / `canonical_wiki_id`). The
gate (deterministic, in-transaction):

1. every dropped ref must be declared in `removed_refs` with a valid reason;
   every gained ref in `added_refs`;
2. on `attach`, non-targeted sections must be byte-identical;
3. structural validation (5 required section anchors, Summary ≤ 280,
   Disambiguation present, every surviving ref resolves);
4. any violation → rollback, job → `pending`, retry with the defect; capped
   by `attempts` → `failed` (surfaced via `GET /jobs`).

Provenance preserved: a declared removal re-types the `summarises` edge
(`contradicted` → `contradicts`) rather than vanishing; prior content is
snapshotted to the activity log (`wiki_revise`), so deleted ≠ destroyed.
The `section:references` ledger is machine-regenerated from parsed refs, so
inline tokens, the ledger, and the SQL relations cannot disagree.

Consolidation is LLM-performed: duplicates are spotted via the maintainer's
normal `recall_memory` (no dedup query); the writer picks the canonical and
the loser is soft-retired (`importance=0`, `retired_at`, `redirect_to`,
`duplicate_of` + `consolidated_into` edges) — still resolvable, dropped from
ranking, and self-clearing (the maintainer is prompted to skip marked pairs).

## What was built

| File | Role |
|---|---|
| `alembic/versions/005_wiki_system.py` | additive migration: `wiki` type, `wikis_ext`, `wiki_job` (down_revision 004) |
| `braindb/schemas/entities.py` | `WikiCreate/Read/Update` + `AnyEntityRead` |
| `braindb/schemas/relations.py` | `summarises`, `not_duplicate`, `duplicate_of`, `consolidated_into` |
| `braindb/routers/entities.py` | wiki CRUD; `ENTITY_SELECT`/`_flatten` extended (`member_keyword_ids::text[]`) |
| `braindb/services/context.py` | `DECAY_RATES["wiki"]`, `EXT_QUERIES["wiki"]` |
| `braindb/config.py` | `decay_rate_wiki = 0.0` |
| `braindb/services/wiki_jobs.py` | all non-LLM plumbing: orphan/cron, claim (SKIP LOCKED), dedupe_key, gate, ledger, reconcile, snapshot, soft-retire, advisory lock |
| `braindb/routers/wiki.py` | `/cron` `/maintain` `/write` `/jobs` |
| `braindb/agent/prompts/wiki_maintainer_prompt.md` | per-case triage → structured suggestion |
| `braindb/agent/prompts/wiki_writer_prompt.md` | skeleton contract + manifest + consolidate |
| `braindb/wiki_scheduler.py` + compose `wiki_scheduler` (profile `wiki`) | Stage-2 always-on, opt-in |

No new Python dependencies. The agent itself is reused unchanged (no new
agent factory) — prompts are passed as the query to `run_agent_query`.

## Verification status (DeepInfra profile)

- Migration 005 auto-applies on startup (rev `005`, both tables present). ✓
- Wiki CRUD + no retrieval regression; wiki participates in ranking, existing types unaffected. ✓
- Cron: 757 triage enqueued; re-run → 0 (idempotent). ✓
- Maintainer: one case/call; `create`/`skip` decisions; deterministic dedupe_key; cron does not re-enqueue in-flight orphans. ✓
- Writer `create`: skeleton anchors, inline refs, machine ledger, `summarises` relation — all consistent. ✓
- Accounted-change gate (deterministic, no LLM): undeclared drop/section-change rejected, declared changes pass, bad structure rejected. ✓
- Consolidation: LLM picked canonical, loser soft-retired + provenance edges, canonical ranks / loser→0, still resolvable. ✓
- Scheduler: loop healthy, drives cron on schedule; opt-in profile. ✓

Not yet exercised live (deferred to a broader end-to-end pass; needs
maintainer-produced attach jobs and is LLM-cost-bearing): the live `attach`
path with restorability from the `wiki_revise` log, and a live
contradiction-resolution edit. The deterministic guarantees behind them are
unit-verified.

## Quality trial (10-case controlled batch) — findings

Tool: `docker compose exec -T api python -m braindb.tools.export_wikis`
(read-only; writes `data/wiki_review/*.md` + `INDEX.md`; gitignored).

**Mechanics — solid.** 10 maintain calls → 2 create / 4 attach / 4 skip
(sane distribution, coherent rationales). Writers produced/updated wikis with
all skeleton anchors. **Consistency ✓ on every wiki** (inline refs = ledger =
`summarises` relations). The **accounted-change gate fired live**: an attach
that changed the `sources` section without declaring it was rejected and
requeued; the retry passed (no bad write persisted; `attempts` capped).
Skip self-clearing verified (post-trial cron enqueued 0; `failed` triage still
retries). Manifest now logged in `wiki_write` activity (writer reasoning is
inspectable).

**Content — weak, and the export proves why (the important finding).** The
orphans being wiki-ified are overwhelmingly **bare keyword entities** whose
`content` is an auto-generated token (e.g. `_pytest_82a2e09b`). The writer has
no real substance to ground on, so it: (a) writes fluent prose from world
knowledge, and (b) **cites those keyword-token entities as if they were
sources** — even fabricating a sentence ("supported by various internal
identifiers [[ref:…]] [[ref:…]]") to wrap junk refs. The wikis are
structurally perfect and provenance-consistent but **not evidence-grounded**.
Scaling now would mass-produce fluent-but-hollow pages citing tokens.

Root cause is *not* pipeline code (which works). It is **what is fed in**: the
maintainer/writer act on the bare keyword, not the keyword's connected
facts/thoughts. Options to decide before scaling:
- writer pulls the keyword's `tagged_with` fact/thought neighbourhood (via the
  existing `recall_memory`/`view_tree`) as the real sources, and the prompt
  forbids citing `keyword`-type entities as provenance;
- and/or the maintainer `skip`s keyword orphans that have no real
  fact/thought substance behind them (only wiki-ify concepts with evidence).

## Known follow-ups (decide before scaling)

1. **Skip self-clearing — DONE.** `run_cron()` now excludes orphans with a
   `rejected` triage job (deliberate skip). Permanent like `not_duplicate`;
   `failed` triage still retries. No schema change.
2. **Grounding (NEW — highest priority).** See "Quality trial" above. Decide
   the sourcing fix before any scale-up; mechanics are ready, content is not.
3. **Backlog cost.** ~750 pending triage × one agent call each. Scheduler is
   opt-in; consider prioritising high-importance / evidence-bearing orphans.
4. **LLM profile.** `.env` switched `vllm_workstation → deepinfra` for
   verification (local vLLM down). Switch back when available.
5. Live contradiction-resolution edit still not exercised (no opposing
   sources in the trial corpus). Deterministic guarantee unit-verified.

## Operational notes — review tooling

- Inspect quality any time: `docker compose exec -T api python -m
  braindb.tools.export_wikis`, then open `data/wiki_review/INDEX.md` and the
  per-wiki files. Each file shows body, the consistency verdict, **provenance
  (cited entities' real content — judge grounding here)**, maintainer
  rationale, writer manifest, and revision snapshots.

## Operational notes

- Stage 1 is manual: hit the endpoints by hand. Nothing wiki-related runs on
  startup (the existing ingest watcher is untouched).
- Enable always-on: `docker compose --profile wiki up -d wiki_scheduler`
  (env: `WIKI_CRON_INTERVAL`, `WIKI_MAINTAIN_INTERVAL`, `WIKI_WRITE_INTERVAL`).
- Migrations run automatically on `api` startup.
