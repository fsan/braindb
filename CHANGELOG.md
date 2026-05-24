# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-24

The first substantial release beyond the v0.1.0 memory-store baseline. The
headline addition is the **wiki layer**: an always-on background pipeline
that turns the entity graph into self-maintaining, human-readable pages —
the same hands-off posture as the file watcher.

### Added

- **Wiki pipeline** (`braindb/wiki_scheduler.py`, `braindb/routers/wiki.py`):
  the in-house agent decides per-orphan whether to *attach* to an existing
  wiki, *create* a new one, *consolidate* duplicates, or *skip*. A separate
  writer agent then researches and writes/maintains each page, citing every
  claim with `[[ref:UUID]]`, with auto-self-healing on conflated subjects.
- **Wiki HTTP endpoints**: `POST /api/v1/wiki/cron` (orphan scan, idempotent),
  `POST /api/v1/wiki/maintain` (one triage decision per call),
  `POST /api/v1/wiki/write` (one writer pass), `GET /api/v1/wiki/jobs`
  (queue visibility). Normal operation is the scheduler sidecar; these are
  for hand-driving / observability.
- **Wiki section-edit tools**: `read_wiki_outline`, `read_wiki_section`,
  `edit_wiki_section`, `delete_wiki_section`, `validate_wiki` — let the
  writer do surgical edits on large pages without rewriting the full body.
- **Writer context-handoff**: when the writer's running context grows past
  a budget, it calls `handoff_to_successor` with a structured brief; the
  router respawns a successor agent with fresh context. Bounded by depth.
- **Typed agent termination**: every agent finish (`/agent/query`,
  maintainer, writer, subagent) is now a Pydantic model — schema-validated,
  no scraped free-text. Models live in `braindb/agent/schemas.py`.
- **Layer-4 retry-with-correction**: when a run ends without
  `final_answer`, the runner appends a synthetic correction message and
  re-invokes once with a small budget; recovers transparently.
- **`CountdownHooks` nudges**: a context-aware "wrap up" message arrives
  before `max_turns` is exhausted; a separate token-budget watch nudges
  the writer toward handoff when the conversation is getting big.
- **Auto-consolidation of duplicate wikis** via the maintainer's
  `consolidate` action, with reversible `wiki_revise` snapshots.
- **Per-wiki cooldown for attaches** in the scheduler so cron ticks don't
  thrash the same wiki across overlapping ticks.
- **Local vLLM profiles**: `vllm_workstation`, `vllm_workstation_qwen`,
  `vllm_workstation_gemma` for running against your own GPU box.
- **Tests**: session-teardown fixture in `tests/conftest.py` that sweeps
  any `_pytest_*` keyword artefacts that escape per-test cleanup.
- **CI**: minimal GitHub Actions workflow runs the typed-final + handoff
  unit tests on every PR + push to main.

### Configurable

New environment variables exposed in `.env.example` and consumed by the
api / wiki scheduler:

- `WIKI_ENABLED` — opt-in flag for the wiki scheduler (default `false`).
- `WIKI_INTERVAL` — scheduler tick in seconds (default `60`).
- `WIKI_FRESHNESS_MINUTES` — orphan eligibility gate; an entity must be
  this old before it's picked up for triage (default `30`).
- `WIKI_ATTACH_COOLDOWN_SECONDS` — per-wiki throttle between attach claims.
- `WIKI_AGENT_TIMEOUT` — HTTP timeout the scheduler uses for maintainer /
  writer calls (default `1200` seconds, i.e. 20 minutes).
- `AGENT_VERBOSE` — log every agent tool call with args and result preview
  (default `false`).

### Changed

- **Recall is keyword-mediated**: `/memory/context` now matches both the
  fuzzy (pg_trgm) and the embedding pathway against keyword entities, then
  surfaces facts via `tagged_with`. Two-level diversity quota
  (per-search-term + per-keyword, geometric decay) prevents one popular
  hub keyword from monopolising top-N. Narrow short queries outperform
  long phrases for keyword recall.
- **Multi-item recall returns previews**: `/memory/context` and
  `list_entities` now return short (~1 KB) previews per item; the full
  body is fetched on demand via `GET /api/v1/entities/{id}`, with optional
  `?offset=&limit=` paging for large documents. Keeps the LLM-visible
  context tight without losing access to the underlying content.
- **`deepinfra` (`google/gemma-4-31B-it`) promoted as the recommended
  default** across README, BRAINDB_GUIDE, CLAUDE, and CONTRIBUTING. Fast
  (5–30s per agent call), cheap, validated end-to-end. The `vllm_*`
  profiles are now documented as advanced / offline / requires GPU.
- **`WIKI_ENABLED` defaults to `false`** in compose so the scheduler
  sidecar boots but doesn't tick until explicitly opted in — keeps a
  fresh clone from spending on the LLM by accident.
- **Agent `max_turns` defaults bumped** (15 → 20) and `countdown_threshold`
  (5 → 8) after live observation on slower providers; deepinfra/Gemma is
  unaffected because it finishes well before the budget.
- **Wiki scheduler** collapsed three timers into one gated loop — no idle
  LLM spend, parallel maintain + writer fan-out per tick.
- **Skill files**: agent-call timeout guidance bumped to 10 minutes max
  for slow providers; wiki awareness + always-ASK-before-saving added.

### Fixed

- **Double-escaped JSON tool-call payload** (Qwen AWQ-INT4 quirk):
  `_maybe_parse_json_string` now unwraps the second layer when needed.
  Compliant providers (deepinfra/OpenAI/Anthropic via LiteLLM) unaffected.
- **JSON-string tool-call payload** (vLLM/Qwen format): typed schemas
  accept `arguments.payload` as either a JSON object or a JSON-encoded
  string of a dict; the LLM-visible contract is unchanged.
- **Writer no-op on already-cited members** no longer leaks the orphan
  back into the triage queue — it now closes the loop cleanly.
- **Big-body writes** retry on transient `BadRequestError` and stub out
  the body when the provider truncates, so the wiki isn't lost.
- **Reference-by-catalog-number** in maintainer prompts replaced the
  earlier uuid form to stop hallucinated wiki IDs.
- **Stale assigned jobs** in `wiki_job` are reclaimable on the next cron
  tick (stale-lease).
- **`output_type` dropped from agent builder** — restored tool use; typed
  `final_answer` still enforced via mutable-slot capture.
- **Compose**: no more `--reload` on the api command — code changes apply
  explicitly via `docker compose up -d --no-deps --force-recreate api`,
  preventing mid-run reloads that broke in-flight LLM calls.

### Upgrading from 0.1.0

Migration `005_wiki_system.py` adds two new tables (`wikis_ext`,
`wiki_job`) and the `wiki` entity type. It runs automatically on
container startup via `alembic upgrade head` (already in the api
`command`). Existing rows are untouched; no manual action required.

The wiki scheduler ships **disabled by default** — set
`WIKI_ENABLED=true` in `.env` to opt in. This prevents an upgraded
deployment from spending on the LLM until the operator says go.

## [0.1.0] — initial public baseline

Memory store: entities (`thought`, `fact`, `source`, `datasource`, `rule`),
relations, `pg_trgm` + `pgvector` retrieval, the BrainDB agent
(`/api/v1/agent/query`), the always-on file watcher (`data/sources/`),
Claude Code skills.
