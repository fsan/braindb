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
| `index.html` | Layout shell: top bar with Reader / Graph / Ops tabs, the Reader grid (rail / wiki body / relations), the Graph view (Cytoscape canvas + toolbar + legend), the Ops grid (stats / queue / log / rules), and two slide-over drawers (entity / Ask). Loads Cytoscape + fCoSE from a CDN. |
| `style.css` | Design language: near-monochrome palette, refined serif for body, clean grotesque for UI, hairline rules, one restrained accent. Light + dark via `data-theme` on `<html>`. Graph view tokens included. |
| `wiki-render.js` | Small Markdown-ish renderer specialised for the BrainDB wiki body grammar — `<!-- wiki:meta -->`, headings, `> **Summary:** / **Disambiguation:**` callouts, `<!-- section:slug -->` dividers, GFM tables, lists, `**bold**`, `` `code` ``, and `[[ref:UUID]]` / `[[ref:UUID|display]]` citation chips (tolerant of the grouped form). |
| `graph.js` | Cytoscape.js integration: per-entity-type shapes/colours, edge-label-on-hover, click → entity drawer, double-click → expand 1-hop neighbourhood, 300-node soft cap. |
| `app.js` | Data layer + routing + Ops auto-refresh + Ask drawer wiring + Graph tab wiring. ES module; imports `wiki-render.js` and `graph.js`. |

## Keyboard

- `/` — focus the search box (Reader)
- `Cmd/Ctrl+K` — open the Ask drawer
- `Esc` — close any open drawer
- `F` — fit graph to viewport (Graph tab)

## Graph tab — quick tour

- Open a wiki in **Reader**, then click the **Graph** tab → the graph seeds with that wiki and its direct neighbours (keywords, facts, sources).
- Cold-start the Graph tab → empty canvas with a search box; pick a result to seed.
- **Click** any node → opens the entity drawer (same one Reader uses).
- **Double-click** a node → expands its 1-hop neighbourhood.
- Scroll to zoom, drag empty space to pan. Soft-capped at 300 nodes.

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

## External dependencies (CDN, optional)

- [Cytoscape.js](https://js.cytoscape.org/) 3.30.4 — graph rendering for the Graph tab.
- [cytoscape-fcose](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose) 2.2.0 — force-directed layout for the Graph tab.

Both are loaded from `unpkg.com` in `index.html` with pinned versions. If the CDN is unreachable, the Graph tab shows an error message and the rest of the app (Reader / Ops / Ask) continues to work.
