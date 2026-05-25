# BrainDB frontend

A thin, read-mostly browser UI for BrainDB. Vanilla HTML / CSS / JS — no build, no npm, no framework.

## Run

The BrainDB backend must be running first (default: `http://localhost:8000`). From the repo root:

```bash
python -m http.server 8090 -d frontend
```

Then open <http://localhost:8090>.

That's it. The frontend is a static page that talks to the API directly via `fetch`. CORS is already open on the backend.

> If `8090` is in use on your machine, pick any free port: `python -m http.server <PORT> -d frontend`. (Avoid `8080` on Windows with Docker Desktop installed — it's commonly held by the WSL backend.)

## Pointing at a non-local backend

If your API lives somewhere other than `localhost:8000`, set `window.BRAINDB_API_URL` before `app.js` loads — e.g. inject a small `<script>window.BRAINDB_API_URL="https://my-host:8000"</script>` before the module tag in `index.html`.

## What's in here

| File | Purpose |
|---|---|
| `index.html` | Layout shell: top bar with Reader / Ops tabs, the Reader grid (rail / wiki body / relations), the Ops grid (stats / queue / log / rules), and two slide-over drawers (entity / Ask). |
| `style.css` | Design language: near-monochrome palette, refined serif for body, clean grotesque for UI, hairline rules, one restrained accent. Light + dark via `data-theme` on `<html>`. |
| `wiki-render.js` | Small Markdown-ish renderer specialised for the BrainDB wiki body grammar — `<!-- wiki:meta -->`, headings, `> **Summary:** / **Disambiguation:**` callouts, `<!-- section:slug -->` dividers, GFM tables, lists, `**bold**`, `` `code` ``, and `[[ref:UUID]]` / `[[ref:UUID|display]]` citation chips (tolerant of the grouped form). |
| `app.js` | Data layer + routing + Ops auto-refresh + Ask drawer wiring. ES module; imports `wiki-render.js`. |

## Keyboard

- `/` — focus the search box (Reader)
- `Cmd/Ctrl+K` — open the Ask drawer
- `Esc` — close any open drawer

## Endpoints used

Read-only except where intentional:

- `GET /api/v1/entities?entity_type=wiki` — wiki index
- `GET /api/v1/entities/{id}` — full entity (wiki body, fact, thought, …)
- `GET /api/v1/entities/{id}/relations` — relations of one entity
- `GET /api/v1/wiki/jobs` — Ops queue
- `GET /api/v1/memory/log` — Ops activity log
- `GET /api/v1/memory/stats` — Ops stats strip
- `GET /api/v1/memory/rules` — Ops rules list
- `POST /api/v1/memory/context` — search (the modern keyword-mediated path)
- `POST /api/v1/agent/query` — the Ask drawer

No `/memory/sql`. No direct database access. No write outside the user-driven Ask drawer.
