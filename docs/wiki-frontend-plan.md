# Read-only Wiki frontend (Reader + Ops) — zero-backend, Wikipedia-serious

> Status: FINALISED PLAN — execute in a later session. No worktree/commits
> created until then. (Mirror of the approved plan; kept in-repo so we can
> resume cleanly without re-planning.)

## Context

Lever 1 (dedup-first writer priority) + Thread-2 (created_at freshness gate)
are shipped, committed (`a03f077`), and **running** on
`feat/wikis-and-maintainer-agent-with-truncation` for a multi-hour
duplication-self-correction observation. Lever 2/3 stay deferred pending that
outcome. In parallel we want a **read-only wiki frontend (Reader + Ops
dashboard)**. Directives shaping this plan:

- The frontend **must never touch the DB directly** (so: no client
  `/memory/sql`).
- **Minimise backend disruption** — a good assessment must show whether the
  backend can be avoided entirely. (It can — see next section.)
- Stack = **simplest**: vanilla static HTML/CSS/JS, no build, no npm, no
  framework, no new Python dependency. CORS already open
  (`braindb/main.py:31`, `allow_origins=["*"]`).
- Design = **clean like Wikipedia, but built for 2026**: professional,
  serious, editorial. Explicitly NOT a colourful/cartoonish/"vibecoded"
  mess.

## Backend assessment — conclusion: ZERO backend changes

A careful pass over every reader/ops need against existing endpoints:

| Need | Existing endpoint | Notes |
|---|---|---|
| Wiki index + variant clusters | `GET /api/v1/entities?entity_type=wiki` | Returns `summary`, `importance`, `keywords`, and a ≤1K **content preview** (post-truncation work). The preview's first lines contain `<!-- wiki:meta canonical_name=… -->` + `# NAME` + `> **Summary:**` → parse `canonical_name` client-side from the preview. **No N+1 for the index/clusters.** |
| One wiki page | `GET /api/v1/entities/{id}` (+`offset/limit`,`content_meta`) | One call when a wiki is opened (full body + ext: revision, retired_at, redirect_to, member_keyword_ids). Page huge bodies via `content_meta`. |
| Resolve `[[ref:UUID]]` | `GET /api/v1/entities/{UUID}` | Lazy: only when a citation chip is opened (or small batch on page open). |
| Provenance / consistency | `GET /api/v1/entities/{id}/relations` (filter `summarises`) | Consistency (inline refs vs `summarises`) computed **client-side**, same logic as `export_wikis._consistency` (~10 lines JS, regex ported from `REF_RE`, `braindb/services/wiki_jobs.py:32-36`). |
| Related entities | `GET /api/v1/memory/tree/{id}?max_depth=1` | Optional sidebar. |
| Search | `POST /api/v1/memory/search` | Only POST used; not SQL, not a write. |
| Job queue (ops) | `GET /api/v1/wiki/jobs?status=&job_type=&limit=` | Queue mix; pending `consolidate` highlighted (shows Lever 1 draining). |
| Maintainer/writer activity (ops) | `GET /api/v1/memory/log?limit=` | Recent pipeline activity. |
| Consolidation / retire map (ops) | `GET /api/v1/entities/{id}` for the **few** retired wikis only | Retired ⇒ `importance≈0` in the index list (cheap signal); fetch ext (`redirect_to`,`retired_at`) only for those few, not all N. |

**Result: the entire Reader + Ops dashboard is built from existing GETs
(plus one allowed `/memory/search` POST). No new endpoint, no new service,
no router/`main.py` edit, no new dependency, no DB schema change, and — by
parsing the already-returned content preview — no N+1.** This fully honours
"avoid the backend" and "no DB-direct access". An earlier proposed BFF
layer is **dropped**.

Out of scope (explicitly NOT in this plan): if the wiki count later grows so
large that even per-open detail calls hurt, a *single* optional read
endpoint could consolidate them — a future decision, not part of this work.

## Observation safety (only matters if executed while the pipeline still runs)

The `api` container bind-mounts `.:/app` but **no longer runs uvicorn with
`--reload`** (removed today to avoid mid-pipeline restarts). Code changes
require an explicit `docker compose up -d --no-deps --force-recreate api`,
so `.py` edits don't auto-reload anyway. This frontend adds **no `.py`** and
touches **no existing file** — only new static files. So:

- If the observation is **still running** when we execute: create the static
  app in an **isolated git worktree** (`git worktree add ../braindb-frontend
  -b feat/wikis-and-maintainer-agent-frontend`) so branch/commits never
  `checkout` the bind-mounted main tree. Serve via stdlib
  `python -m http.server` from the worktree; browser → it; JS `fetch`es
  `http://localhost:8000`.
- If the observation is **already over**: no worktree needed — just add a
  new `frontend/` dir on a dedicated branch (new files don't trigger
  reload).

Either way: zero backend process touched, observation undisturbed.

## Design language — Wikipedia-grade, 2026-professional

Reference feel: a serious reference work / editorial knowledge tool, like
Wikipedia's content discipline with a modern 2026 refinement — **not** a SaaS
landing page, **not** colourful, **not** playful.

DO: content-first single-column reading measure (~68–72ch); restrained
near-monochrome palette (ink `#1b1b1b` on paper `#fff`/`#f8f8f7`, hairline
`#eaeaea` rules, ONE restrained link/citation accent ≈ classic encyclopedic
blue, used sparingly); clear typographic hierarchy (a refined serif for body
e.g. system "Georgia/Charter"-class, clean grotesque for UI/headings/labels);
generous whitespace; quiet left TOC/section nav from the `<!-- section:X -->`
markers; citation chips as small superscript-style references that open a
calm side panel (the entity's content + provenance); a sober Ops view
(plain dense tables, monospace ids, status as quiet text/diamonds — no
traffic-light candy); subtle, near-instant transitions only; light/dark
toggle with the same restraint; fully keyboard navigable; fast, no layout
shift.

DON'T: bright/multi-colour fills, gradients, glow/neon, big rounded "cards",
emojis as UI, drop shadows everywhere, bouncy animation, dashboard
"widgets", decorative icons. Seriousness over decoration. If in doubt, look
plainer.

## Files (all NEW, no existing file modified)

```
frontend/index.html        layout shell (reader + ops tabs), no inline mess
frontend/style.css         the design language above; CSS variables; dark mode
frontend/app.js            data layer (existing endpoints only) + routing + ops
frontend/wiki-render.js    ~150-line purpose-built renderer for the real body
                           grammar: <!-- wiki:meta -->, # / ##,
                           > **Summary:/Disambiguation:** callouts,
                           <!-- section:X --> dividers, GFM tables, lists,
                           **bold**, `code`, [[ref:UUID|display]] / [[ref:UUID]]
                           chips (tolerant of grouped [[ref:a], [ref:b]] seen
                           in real bodies)
frontend/README.md         how to run: `python -m http.server` + open URL
```

No Python, no dependency, no schema, no write/agent/SQL calls.

## Verification

1. **Undisturbed**: `docker logs braindb_wiki_scheduler --tail 3` keeps
   advancing across the whole build; main-tree `git status` clean; only new
   static files exist.
2. **Pure read**: browser Network tab shows only GETs + the one
   `/memory/search` POST — no write/agent/SQL/`/memory/sql`.
3. **Reader**: index lists all wikis (canonical_name parsed from preview),
   retired ones flagged; opening `braindb-1785a337` renders
   meta/summary/sections/tables faithfully; every `[[ref:UUID]]` chip
   resolves to the real entity in the side panel; client consistency badge
   equals `export_wikis` (`CONSISTENT ✓`, 3 body / 3 relations).
4. **Ops**: variant panel surfaces the Koutsoumpos / SaaSpocalypse /
   BrainDB clusters; queue from `/wiki/jobs` with pending `consolidate`
   highlighted and visibly draining first across auto-refreshes (Lever 1);
   activity from `/memory/log`; retire/redirect map correct for the few
   retired wikis.
5. **Design review**: matches the Wikipedia-serious / 2026 language above —
   monochrome+one accent, editorial type, no candy; passes a "does this look
   like a serious reference tool, not a vibecoded dashboard" check.

## Standing constraints

`.env` never committed/touched. Public repo — no personal names in commit
messages, no Co-Authored-By trailer. Don't push unless asked. No `.py`
edit / `checkout` / restart on the main tree while the observation runs.
Don't touch LLM profiles/.env. Lever 2 / 3 remain deferred pending the
observation outcome.
