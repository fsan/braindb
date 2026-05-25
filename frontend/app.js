// ============================================================
// app.js — BrainDB read-only frontend
//   - Data layer: thin fetch wrappers over the existing API
//   - Routing: hash-based (#/wiki/<id>, #/ops, #/)
//   - Reader, Ops, and Ask drawer wiring
// ============================================================

import { renderWiki, extractSections, consistencyCheck } from "./wiki-render.js";
import * as graph from "./graph.js";

const API = (window.BRAINDB_API_URL || "http://localhost:8000") + "/api/v1";

// ============================================================
// Data layer
// ============================================================
async function api(path, opts = {}) {
  const r = await fetch(API + path, opts);
  if (!r.ok) {
    let body = "";
    try { body = await r.text(); } catch {}
    throw new Error(`HTTP ${r.status} ${r.statusText} on ${path}\n${body.slice(0, 400)}`);
  }
  return r.json();
}
const apiGet = (path) => api(path);
const apiPost = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

const data = {
  listWikis: () => apiGet("/entities?entity_type=wiki&limit=200"),
  entity: (id) => apiGet(`/entities/${id}`),
  relations: (id) => apiGet(`/entities/${id}/relations`),
  search: (queries) => apiPost("/memory/context", { queries, max_depth: 1 }),
  jobs: () => apiGet("/wiki/jobs?limit=200"),
  log: () => apiGet("/memory/log?limit=50"),
  stats: () => apiGet("/memory/stats"),
  rules: () => apiGet("/memory/rules"),
  agent: (query) => apiPost("/agent/query", { query }),
};

// ============================================================
// Helpers
// ============================================================
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// Build a clean human-readable snippet from an entity's content.
// Strips the leading `<!-- wiki:meta … -->` comment, the `# Title` line, and
// blockquote markers; for wikis it prefers the `> **Summary:** …` callout text.
function entitySnippet(content, isWiki = false) {
  if (!content) return "";
  let body = String(content);
  // Strip a single leading HTML comment (the wiki:meta header).
  body = body.replace(/^\s*<!--[\s\S]*?-->\s*/, "");
  if (isWiki) {
    const sum = body.match(/^>\s*\*\*Summary[:\s]\*\*\s*([\s\S]+?)(?:\n>|\n\n|$)/im);
    if (sum) return sum[1].replace(/\s+/g, " ").trim().slice(0, 160);
  }
  const cleaned = body
    .split(/\r?\n/)
    .map(l => l.replace(/^#+\s*/, "").replace(/^>\s*/, "").trim())
    .filter(l => l.length > 0 && !/^<!--/.test(l));
  return (cleaned[0] || "").slice(0, 160);
}

// Pull a clean wiki name from the preview's first lines without a full fetch.
// Prefers the `# Title` body line (always clean) over the meta header, because
// LLM emitters sometimes write multi-word canonical_name VALUES unquoted, e.g.
// `canonical_name=Value Investing language=en …`, which a naive regex
// truncates to the first word.
function previewCanonicalName(preview) {
  if (!preview) return null;
  const h = preview.match(/^#\s+(.+)$/m);
  if (h) return h[1].trim();
  // Fallback: parse the meta header tolerantly (quoted OR unquoted multi-word).
  const m = preview.match(/canonical_name=(?:"([^"]+)"|([^\s][^>]*?))(?=\s+\w+=|\s*-->|$)/);
  if (m) return (m[1] || m[2] || "").trim();
  return null;
}

function isRetired(wiki) {
  // The list endpoint returns previews + the entity row; importance≈0 + a
  // visible "redirect_to:" line in the preview is a strong retirement signal.
  // Defensive — fall back to false.
  if (wiki.importance != null && wiki.importance < 0.05) return true;
  return false;
}

// ============================================================
// Theme
// ============================================================
function initTheme() {
  const saved = localStorage.getItem("braindb-theme");
  if (saved === "dark") document.documentElement.dataset.theme = "dark";
  $("#theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    if (cur === "dark") {
      delete document.documentElement.dataset.theme;
      localStorage.setItem("braindb-theme", "light");
    } else {
      document.documentElement.dataset.theme = "dark";
      localStorage.setItem("braindb-theme", "dark");
    }
  });
}

// ============================================================
// Tabs
// ============================================================
function setTab(name) {
  $$(".tab").forEach(t => t.classList.toggle("is-active", t.dataset.tab === name));
  $$(".view").forEach(v => {
    const on = v.id === name;
    v.classList.toggle("is-active", on);
    v.hidden = !on;
  });
}

function initTabs() {
  $$(".tab").forEach(t => t.addEventListener("click", () => {
    const name = t.dataset.tab;
    if (name === "ops") location.hash = "#/ops";
    else if (name === "graph") {
      // Snap to the currently-open wiki if there is one
      const cur = parseHash();
      location.hash = cur.route === "wiki" ? `#/graph/${cur.id}` : "#/graph";
    } else location.hash = "#/";
  }));
}

// ============================================================
// Routing
// ============================================================
function parseHash() {
  const h = (location.hash || "#/").slice(1);
  if (h.startsWith("/wiki/")) return { route: "wiki", id: h.slice(6) };
  if (h.startsWith("/graph/")) return { route: "graph", id: h.slice(7) };
  if (h.startsWith("/graph")) return { route: "graph" };
  if (h.startsWith("/ops")) return { route: "ops" };
  return { route: "reader" };
}

async function handleRoute() {
  const r = parseHash();
  if (r.route === "ops") {
    setTab("ops");
    await loadOps();
    return;
  }
  if (r.route === "graph") {
    setTab("graph");
    await openGraph(r.id || null);
    return;
  }
  setTab("reader");
  if (r.route === "wiki") {
    await openWiki(r.id);
  }
}

// ============================================================
// Reader — wiki index
// ============================================================
let wikiIndexCache = null;

async function loadWikiIndex() {
  const items = await data.listWikis();
  // items is a list of entity rows with `content` containing the preview
  wikiIndexCache = items;
  renderWikiIndex(items);
}

function renderWikiIndex(items) {
  const ul = $("#wiki-index");
  ul.innerHTML = "";
  const sorted = [...items].sort((a, b) => {
    const an = previewCanonicalName(a.content) || a.id;
    const bn = previewCanonicalName(b.content) || b.id;
    return an.localeCompare(bn);
  });
  for (const w of sorted) {
    const li = document.createElement("li");
    const name = previewCanonicalName(w.content) || w.id.slice(0, 8);
    li.dataset.id = w.id;
    li.textContent = name;
    if (isRetired(w)) {
      const tag = document.createElement("span");
      tag.className = "retired-tag";
      tag.textContent = "retired";
      li.appendChild(tag);
    }
    li.addEventListener("click", () => {
      location.hash = `#/wiki/${w.id}`;
    });
    ul.appendChild(li);
  }
}

function markActiveInIndex(id) {
  $$("#wiki-index li").forEach(li => li.classList.toggle("is-active", li.dataset.id === id));
}

// ============================================================
// Reader — open one wiki
// ============================================================
async function openWiki(id) {
  markActiveInIndex(id);
  const view = $("#wiki-view");
  view.innerHTML = '<div class="empty">Loading…</div>';

  let entity, relations;
  try {
    [entity, relations] = await Promise.all([data.entity(id), data.relations(id)]);
  } catch (e) {
    view.innerHTML = `<div class="empty">Failed to load wiki:<br><code>${escapeHtml(e.message)}</code></div>`;
    return;
  }

  const body = entity.content || "";
  const rendered = renderWiki(body);
  const meta = rendered.meta || {};

  // Consistency: compare inline refs to `summarises` relations
  const summarisesIds = (relations || [])
    .filter(r => r.relation_type === "summarises")
    .map(r => r.to_entity_id);
  const cc = consistencyCheck(body, summarisesIds);

  const metaStrip = `
    <div class="wiki-meta">
      ${meta.canonical_name ? `<span class="meta-piece">${escapeHtml(meta.canonical_name)}</span>` : ""}
      ${meta.language ? `<span class="meta-piece">${escapeHtml(meta.language)}</span>` : ""}
      ${entity.revision != null ? `<span class="meta-piece">rev ${entity.revision}</span>` : ""}
      ${entity.retired_at ? `<span class="badge retired">retired</span>` : ""}
      ${cc.consistent
        ? `<span class="badge consistent">CONSISTENT ✓</span>`
        : `<span class="badge inconsistent">${cc.inline} inline / ${cc.relations} relations</span>`}
      <a class="meta-piece graph-link" href="#/graph/${id}">Show in graph →</a>
    </div>
  `;

  view.innerHTML = `
    <div class="wiki-content">
      ${metaStrip}
      ${rendered.html}
    </div>
  `;

  // Wire citation chips: clicking opens the entity drawer
  $$(".ref-chip", view).forEach(a => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      openEntityDrawer(a.dataset.ref);
    });
  });

  // Render relations panel
  renderRelations(relations);
}

// ============================================================
// Reader — relations panel
// ============================================================
function renderRelations(relations) {
  const panel = $("#relations-panel");
  const body = $("#relations-body");
  body.innerHTML = "";

  if (!relations || relations.length === 0) {
    panel.hidden = true;
    return;
  }

  // Dedupe by (relation_type, from, to) — DB can have duplicate edge rows
  // (especially for tagged_with) from ingest race conditions; collapse them.
  const seenKey = new Set();
  const deduped = relations.filter(r => {
    const k = `${r.relation_type}|${r.from_entity_id}|${r.to_entity_id}`;
    if (seenKey.has(k)) return false;
    seenKey.add(k);
    return true;
  });
  relations = deduped;

  // Group by relation_type
  const groups = {};
  for (const r of relations) {
    (groups[r.relation_type] ||= []).push(r);
  }

  for (const [type, rows] of Object.entries(groups).sort()) {
    const g = document.createElement("div");
    g.className = "relations-group";
    const h = document.createElement("h4");
    h.textContent = `${type} (${rows.length})`;
    g.appendChild(h);
    for (const r of rows) {
      const target = r.to_entity_id || r.from_entity_id;
      const row = document.createElement("div");
      row.className = "relation-row";
      row.dataset.id = target;
      row.innerHTML = `
        <span class="entity-chip" data-resolve-id="${escapeHtml(target)}">
          <span class="type-pill type-unknown">…</span>
          <span class="entity-name">${escapeHtml((target || "").slice(0, 8))}</span>
        </span>
        ${r.description ? `<span class="preview">${escapeHtml(r.description)}</span>` : ""}
      `;
      row.addEventListener("click", () => openEntityDrawer(row.dataset.id));
      g.appendChild(row);
    }
    body.appendChild(g);
  }

  panel.hidden = false;
  resolveAndPatch(body);
}

// ============================================================
// Entity drawer (for refs + relation rows)
// ============================================================
async function openEntityDrawer(id) {
  const drawer = $("#entity-drawer");
  const title = $("#entity-drawer-title");
  const body = $("#entity-drawer-body");
  title.textContent = id.slice(0, 8) + "…";
  body.innerHTML = '<div class="empty">Loading…</div>';
  openDrawer(drawer);

  let entity, relations;
  try {
    [entity, relations] = await Promise.all([data.entity(id), data.relations(id).catch(() => [])]);
  } catch (e) {
    body.innerHTML = `<div class="empty">Failed to load entity:<br><code>${escapeHtml(e.message)}</code></div>`;
    return;
  }

  const titleLabel = previewCanonicalName(entity.content)
                  || entitySnippet(entity.content, entity.entity_type === "wiki").slice(0, 60)
                  || id.slice(0, 8);
  title.textContent = entity.entity_type
    ? `${entity.entity_type} · ${titleLabel}`
    : titleLabel;

  const sourcePill = entity.source
    ? `<span class="pill">source: ${escapeHtml(entity.source)}</span>` : "";
  const certPill = entity.certainty != null
    ? `<span class="pill">certainty: ${entity.certainty}</span>` : "";
  const impPill = entity.importance != null
    ? `<span class="pill">importance: ${entity.importance}</span>` : "";

  const isWiki = entity.entity_type === "wiki";
  const isDatasource = entity.entity_type === "datasource";
  const isSource = entity.entity_type === "source";
  const rendered = renderWiki(entity.content || "");
  const contentHtml = isWiki
    ? `<div class="wiki-content">${rendered.html}</div>`
    : `<pre id="drawer-content-pre">${escapeHtml(entity.content || "")}</pre>`;

  // Top action row: "Open full wiki" for wikis; external URL for source.
  // file_path is NOT a clickable link — browsers block file:// from http://
  // pages — instead it's rendered as copy-able text in the meta strip below.
  const actions = [];
  if (isWiki) {
    actions.push(`<a class="drawer-action" href="#/wiki/${id}" data-close-drawer="entity">Open full wiki →</a>`);
  }
  if ((isSource || isDatasource) && entity.url) {
    actions.push(`<a class="drawer-action" href="${escapeHtml(entity.url)}" target="_blank" rel="noopener">Open URL ↗</a>`);
  }
  const actionsRow = actions.length ? `<div class="drawer-actions-row">${actions.join("")}</div>` : "";

  // For datasources / sources with a local file_path, show it as copy-able text
  // (no broken file:// link).
  const filePathRow = ((isSource || isDatasource) && entity.file_path)
    ? `<div class="entity-filepath" title="Local file path — copy and open in your file manager"><span class="filepath-label">File:</span> <code>${escapeHtml(entity.file_path)}</code></div>`
    : "";

  // Pagination hint for big bodies (datasources especially)
  const meta = entity.content_meta || {};
  const hasMore = meta.next_offset != null;
  const loadMoreRow = hasMore
    ? `<button id="drawer-load-more" class="drawer-action secondary" data-next-offset="${meta.next_offset}">Load more (chunk ${meta.chunk_index + 1 || "?"}) ↓</button>`
    : "";

  // Dedupe relations by (relation_type, opposite endpoint) — backend or
  // earlier ingest bug can produce duplicate rows; show each pair once.
  let relationsHtml = "";
  if (relations && relations.length) {
    const seenKey = new Set();
    const deduped = relations.filter(r => {
      const opp = r.to_entity_id === id ? r.from_entity_id : r.to_entity_id;
      const k = `${r.relation_type}|${opp}`;
      if (seenKey.has(k)) return false;
      seenKey.add(k);
      return true;
    });
    const groups = {};
    for (const r of deduped) (groups[r.relation_type] ||= []).push(r);
    const groupHtml = Object.entries(groups).sort().map(([type, rows]) => {
      const items = rows.map(r => {
        const target = r.to_entity_id === id ? r.from_entity_id : r.to_entity_id;
        return `<div class="relation-row" data-id="${target}">
          <span class="entity-chip" data-resolve-id="${escapeHtml(target)}">
            <span class="type-pill type-unknown">…</span>
            <span class="entity-name">${escapeHtml((target || "").slice(0, 8))}</span>
          </span>
        </div>`;
      }).join("");
      return `<div class="relations-group"><h4>${type} (${rows.length})</h4>${items}</div>`;
    }).join("");
    relationsHtml = `<div class="entity-section"><h4>Relations</h4>${groupHtml}</div>`;
  }

  body.innerHTML = `
    ${actionsRow}
    <div class="entity-meta">${sourcePill}${certPill}${impPill}</div>
    ${filePathRow}
    <div class="entity-section">
      <h4>Content</h4>
      ${contentHtml}
      ${loadMoreRow}
    </div>
    ${relationsHtml}
  `;

  // Resolve all chip placeholders inside the drawer (relations block)
  resolveAndPatch(body);

  // Wire "Open full wiki" / external link to also close the drawer cleanly
  $$('.drawer-action[data-close-drawer]', body).forEach(a => {
    a.addEventListener("click", () => closeDrawer($("#entity-drawer")));
  });

  // Wire "Load more" pagination for big bodies
  const loadMoreBtn = $("#drawer-load-more", body);
  if (loadMoreBtn) {
    loadMoreBtn.addEventListener("click", async () => {
      loadMoreBtn.disabled = true;
      loadMoreBtn.textContent = "Loading…";
      try {
        const offset = parseInt(loadMoreBtn.dataset.nextOffset, 10);
        const next = await apiGet(`/entities/${id}?offset=${offset}&limit=4000`);
        const pre = $("#drawer-content-pre", body);
        if (pre) pre.textContent += next.content || "";
        // Update / remove button based on next.content_meta.next_offset
        const nm = next.content_meta || {};
        if (nm.next_offset != null) {
          loadMoreBtn.dataset.nextOffset = nm.next_offset;
          loadMoreBtn.textContent = `Load more (chunk ${(nm.chunk_index || 0) + 1}) ↓`;
          loadMoreBtn.disabled = false;
        } else {
          loadMoreBtn.remove();
        }
      } catch (e) {
        loadMoreBtn.textContent = `Failed: ${e.message}`;
      }
    });
  }

  // Drill-down: clicking a relation in the drawer swaps the drawer to that entity
  $$(".relation-row", body).forEach(row => {
    row.addEventListener("click", () => openEntityDrawer(row.dataset.id));
  });
  // Citation chips inside the drawer also drill in
  $$(".ref-chip", body).forEach(a => {
    a.addEventListener("click", (ev) => {
      ev.preventDefault();
      openEntityDrawer(a.dataset.ref);
    });
  });
}

// ============================================================
// Drawer plumbing
// ============================================================
function openDrawer(drawer) {
  drawer.hidden = false;
  $("#backdrop").hidden = false;
}
function closeDrawer(drawer) {
  drawer.hidden = true;
  // Hide the backdrop only if no drawer is open
  if ($("#entity-drawer").hidden && $("#ask-drawer").hidden) {
    $("#backdrop").hidden = true;
  }
}
function initDrawers() {
  $$(".drawer-close").forEach(b => {
    b.addEventListener("click", () => {
      const which = b.dataset.close;
      closeDrawer($(`#${which}-drawer`));
    });
  });
  $("#backdrop").addEventListener("click", () => {
    closeDrawer($("#entity-drawer"));
    closeDrawer($("#ask-drawer"));
  });
}

// ============================================================
// Search (uses /memory/context)
// ============================================================

// Extracted so BOTH the submit handler and the live-debounce can call it
// directly. The previous `form.requestSubmit()` indirection was fragile
// (silent InvalidStateError inside setTimeout → debounce appeared dead).
async function runReaderSearch() {
  const input = $("#search-input");
  const results = $("#search-results");
  const q = input.value.trim();
  if (!q) { results.hidden = true; return; }
  results.hidden = false;
  results.innerHTML = `<div class="results-empty">Searching…</div>`;
  try {
    const queries = [q, ...q.split(/\s+/).filter(w => w.length > 2)].slice(0, 4);
    const dedup = [...new Set(queries)];
    const res = await data.search(dedup);
    const items = res.items || res || [];
    if (items.length === 0) {
      results.innerHTML = `<div class="results-empty">No matches.</div>`;
      return;
    }
    // Type-breakdown badge row so the variety of result types is obvious.
    const breakdown = {};
    for (const it of items) {
      const t = it.entity_type || "?";
      breakdown[t] = (breakdown[t] || 0) + 1;
    }
    const breakdownHtml = `
      <div class="result-breakdown">
        ${Object.entries(breakdown).sort((a,b)=>b[1]-a[1]).map(
          ([t, n]) => `<span class="type-pill type-${escapeHtml(t)}">${escapeHtml(t)} ${n}</span>`
        ).join("")}
      </div>`;
    // Result rows
    const rowsHtml = items.slice(0, 30).map(it => {
      const id = it.id || it.entity_id;
      const type = it.entity_type || "?";
      const isWiki = type === "wiki";
      const snippet = entitySnippet(it.content, isWiki);
      const name = isWiki
        ? (previewCanonicalName(it.content) || snippet.slice(0, 80) || id.slice(0, 8))
        : (snippet.slice(0, 80) || id.slice(0, 8));
      const shortPreview = (snippet && snippet !== name) ? snippet : "";
      return `
        <div class="result-item" data-id="${escapeHtml(id)}" data-type="${escapeHtml(type)}">
          <span class="type-pill type-${escapeHtml(type)}">${escapeHtml(type)}</span>
          <span class="result-name">${escapeHtml(name)}</span>
          ${shortPreview ? `<div class="result-preview">${escapeHtml(shortPreview)}</div>` : ""}
        </div>`;
    }).join("");
    results.innerHTML = breakdownHtml + rowsHtml;
    // Wire row clicks
    $$(".result-item", results).forEach(row => {
      row.addEventListener("click", () => {
        if (row.dataset.type === "wiki") location.hash = `#/wiki/${row.dataset.id}`;
        else openEntityDrawer(row.dataset.id);
      });
    });
  } catch (e) {
    console.error("Reader search failed:", e);
    results.innerHTML = `<div class="results-empty">Search failed: ${escapeHtml(e.message)}</div>`;
  }
}

function initSearch() {
  const form = $("#search-form");
  const input = $("#search-input");
  const results = $("#search-results");

  form.addEventListener("submit", (ev) => { ev.preventDefault(); runReaderSearch(); });

  // Live debounced search — calls runReaderSearch directly (no form.requestSubmit
  // indirection, which was silently failing inside setTimeout). 180 ms feels
  // live without hammering the server.
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    if (!input.value.trim()) { results.hidden = true; return; }
    timer = setTimeout(runReaderSearch, 180);
  });
}

// ============================================================
// Graph view
// ============================================================
let graphSearchTimer = null;
let graphSeeded = false;

async function openGraph(seedId) {
  // Defer one animation frame so the browser has actually laid out the now-
  // visible #graph section. Otherwise Cytoscape mounts into a 0×0 container,
  // computes layout in degenerate coordinates, and fit() centres on garbage.
  await new Promise(resolve => requestAnimationFrame(() => resolve()));
  const container = $("#graph-canvas");
  const cy = graph.ensureMounted(container, (nodeId) => openEntityDrawer(nodeId));
  if (!cy) return; // CDN failed

  // Cold start (no explicit seed): pick the first wiki from the cached index
  // so the user sees the graph features in action immediately instead of a
  // blank canvas. They can change the seed via search or "Reset".
  if (!seedId && !graphSeeded && wikiIndexCache && wikiIndexCache.length) {
    seedId = wikiIndexCache[0].id;
  }
  if (seedId && seedId !== graphSeeded) {
    graphSeeded = seedId;
    await graph.seed(seedId, (msg) => { $("#graph-status").textContent = msg; });
  }
}

function initGraph() {
  // Toolbar buttons
  $("#graph-zoom-in").addEventListener("click", () => graph.zoomIn());
  $("#graph-zoom-out").addEventListener("click", () => graph.zoomOut());
  $("#graph-fit").addEventListener("click", () => graph.fit());
  $("#graph-reset").addEventListener("click", () => {
    graph.reset();
    graphSeeded = false;
    location.hash = "#/graph";
  });
  $("#graph-hide-kw").addEventListener("change", (e) => graph.toggleKeywords(e.target.checked));

  // Search box (graph-specific) — debounced /memory/context call
  const form = $("#graph-search-form");
  const input = $("#graph-search-input");
  const results = $("#graph-search-results");

  async function runSearch() {
    const q = input.value.trim();
    if (!q) { results.hidden = true; return; }
    results.hidden = false;
    results.innerHTML = `<div class="result-empty">Searching…</div>`;
    try {
      const items = await graph.searchSeeds(q);
      if (!items.length) {
        results.innerHTML = `<div class="result-empty">No matches.</div>`;
        return;
      }
      results.innerHTML = "";
      for (const it of items) {
        const id = it.id || it.entity_id;
        const type = it.entity_type || "?";
        const label = (previewCanonicalName(it.content) || (it.content || "").split("\n")[0] || id).slice(0, 60);

        // Only wikis carry a retired flag. wikiIndexCache (loaded at app
        // boot) has retired_at for every wiki — reuse it.
        let retiredTag = "";
        if (type === "wiki" && wikiIndexCache) {
          const w = wikiIndexCache.find(x => x.id === id);
          if (w && w.retired_at) retiredTag = ` <span class="retired-tag">retired</span>`;
        }

        const row = document.createElement("div");
        row.className = "result-item";
        row.innerHTML = `<span class="result-type">${escapeHtml(type)}</span> ${escapeHtml(label)}${retiredTag}`;
        row.addEventListener("click", async () => {
          results.hidden = true;
          input.value = "";
          location.hash = `#/graph/${id}`;
        });
        results.appendChild(row);
      }
    } catch (e) {
      results.innerHTML = `<div class="result-empty">Search failed: ${escapeHtml(e.message)}</div>`;
    }
  }

  form.addEventListener("submit", (ev) => { ev.preventDefault(); runSearch(); });
  input.addEventListener("input", () => {
    clearTimeout(graphSearchTimer);
    if (!input.value.trim()) { results.hidden = true; return; }
    graphSearchTimer = setTimeout(runSearch, 250);
  });

  // F = fit
  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return;
    if (location.hash.startsWith("#/graph") && (ev.key === "f" || ev.key === "F")) {
      ev.preventDefault();
      graph.fit();
    }
  });

  // Theme change re-styles the graph
  const themeBtn = $("#theme-toggle");
  themeBtn.addEventListener("click", () => {
    // app.js's initTheme handler runs first; refresh graph styles right after
    setTimeout(() => graph.refreshStyle(), 0);
  });
}

// ============================================================
// Ops view
// ============================================================
let opsTimer = null;

async function loadOps() {
  // Stop any prior auto-refresh
  if (opsTimer) clearInterval(opsTimer);

  await Promise.all([loadStats(), loadJobs(), loadLog(), loadRules()]);
  opsTimer = setInterval(() => {
    loadJobs();
    loadLog();
  }, 30_000);
}

async function loadStats() {
  try {
    const s = await data.stats();
    const counts = s.entity_counts || {};
    const entries = Object.entries(counts);
    const html = entries.map(([k, v]) => `
      <div class="stat">
        <div class="v">${v}</div>
        <div class="k">${escapeHtml(k)}</div>
      </div>
    `).join("");
    $("#ops-stats").innerHTML = html || `<div class="empty">No stats.</div>`;
  } catch (e) {
    console.error("loadStats failed:", e);
    $("#ops-stats").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

function actorOf(jobType) {
  if (jobType === "triage") return "MAINTAINER";
  if (jobType === "attach" || jobType === "create" || jobType === "consolidate") return "WRITER";
  return "SCHEDULER";
}

async function loadJobs() {
  try {
    const jobs = await data.jobs();
    const items = jobs.items || jobs || [];
    if (items.length === 0) {
      $("#ops-jobs").innerHTML = `<div class="empty">Queue empty.</div>`;
      return;
    }
    const rows = items.slice(0, 100).map(j => {
      const classes = [];
      if (j.status === "pending" && j.job_type === "consolidate") classes.push("highlight-consolidate");
      if (j.status === "failed") classes.push("row-failed");
      // The wiki_job table has TWO note columns: `rationale` (LLM's reasoning,
      // benign) and `last_error` (real errors). Show whichever is set; only
      // colour as an error when status='failed' AND we actually have a
      // last_error message.
      const note = j.rationale || j.last_error || "";
      const isRealError = j.status === "failed" && j.last_error;
      const actor = actorOf(j.job_type);
      const firstEntity = (j.entity_ids && j.entity_ids[0]) || "";

      const chip = (id) => id
        ? `<span class="entity-chip" data-resolve-id="${escapeHtml(id)}">
             <span class="type-pill type-unknown">…</span>
             <span class="entity-name">${escapeHtml(id.slice(0, 8))}</span>
           </span>`
        : `<span class="ink-muted">—</span>`;

      return `
        <tr class="${classes.join(" ")}" title="job ${escapeHtml(j.id)} · attempts ${j.attempts ?? 0}">
          <td><span class="actor-pill actor-${actor.toLowerCase()}">${actor}</span></td>
          <td>${escapeHtml(j.job_type || "")}</td>
          <td>${escapeHtml(j.status || "")}</td>
          <td>${chip(j.target_wiki_id)}</td>
          <td>${chip(firstEntity)}</td>
          <td>${escapeHtml((j.created_at || "").slice(0, 19))}</td>
          <td class="context ${isRealError ? "context-error" : ""}">${escapeHtml(note)}</td>
        </tr>
      `;
    }).join("");
    $("#ops-jobs").innerHTML = `
      <table class="ops-table">
        <thead><tr>
          <th>actor</th><th>action</th><th>status</th><th>target wiki</th>
          <th>entity</th><th>created</th><th>note</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
    resolveAndPatch($("#ops-jobs"));
  } catch (e) {
    console.error("loadJobs failed:", e);
    $("#ops-jobs").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// ============================================================
// Universal entity resolver — no raw UUIDs anywhere in the UI.
// ============================================================
// Caches the full entity object by id so renderers can reach for type +
// canonical name + preview without round-tripping the API every time.
const entityCache = new Map();
const entityInflight = new Map();          // id → Promise (dedupe concurrent fetches)

async function resolveEntity(id) {
  if (!id) return null;
  if (entityCache.has(id)) return entityCache.get(id);
  if (entityInflight.has(id)) return entityInflight.get(id);
  // Fast path: wikiIndexCache (loaded once at boot, covers all wikis).
  if (wikiIndexCache) {
    const w = wikiIndexCache.find(x => x.id === id);
    if (w) { entityCache.set(id, w); return w; }
  }
  const p = data.entity(id).then(e => {
    entityCache.set(id, e || null);
    entityInflight.delete(id);
    return e;
  }).catch(() => {
    entityCache.set(id, null);
    entityInflight.delete(id);
    return null;
  });
  entityInflight.set(id, p);
  return p;
}

// HTML for a resolved entity chip: small type-pill + canonical-name / snippet.
// Falls back to short-UUID if the entity hasn't resolved yet.
function entityChipHtml(entity, idFallback = "") {
  if (!entity) {
    return `<span class="entity-chip"><span class="type-pill type-unknown">?</span><span class="entity-name">${escapeHtml((idFallback || "").slice(0, 8))}</span></span>`;
  }
  const t = entity.entity_type || "?";
  const isWiki = t === "wiki";
  const label = previewCanonicalName(entity.content)
              || entitySnippet(entity.content, isWiki).slice(0, 60)
              || (entity.id || "").slice(0, 8);
  return `<span class="entity-chip" title="${escapeHtml(entity.id)}"><span class="type-pill type-${escapeHtml(t)}">${escapeHtml(t)}</span><span class="entity-name">${escapeHtml(label)}</span></span>`;
}

// After rendering a section that contains placeholder chips (elements with
// `data-resolve-id`), batch-fetch all the IDs and patch each chip in-place.
// Uses cached entities when available — first paint is the only one that hits
// the network.
async function resolveAndPatch(root) {
  if (!root) return;
  const slots = $$(".entity-chip[data-resolve-id]", root);
  const ids = [...new Set(slots.map(s => s.dataset.resolveId).filter(Boolean))];
  if (ids.length === 0) return;
  await Promise.all(ids.map(id => resolveEntity(id)));
  slots.forEach(s => {
    const id = s.dataset.resolveId;
    const e = entityCache.get(id);
    if (e) s.outerHTML = entityChipHtml(e, id);
  });
}

function logActorOf(operation) {
  const op = (operation || "").toLowerCase();
  if (op.startsWith("wiki_maintain") || op.includes("triage")) return "MAINTAINER";
  if (op.startsWith("wiki_write") || op.includes("attach") || op.includes("create") || op.includes("consolidate")) return "WRITER";
  if (op.startsWith("wiki_cron") || op.includes("schedule")) return "SCHEDULER";
  if (op.startsWith("ingest") || op.includes("watch")) return "WATCHER";
  return "SYSTEM";
}

async function loadLog() {
  try {
    const log = await data.log();
    const items = log.items || log || [];
    if (items.length === 0) {
      $("#ops-log").innerHTML = `<div class="empty">No recent activity.</div>`;
      return;
    }
    const rows = items.slice(0, 50).map(l => {
      const actor = logActorOf(l.operation);
      const entityCell = l.entity_id
        ? `<span class="entity-chip" data-resolve-id="${escapeHtml(l.entity_id)}">
             <span class="type-pill type-unknown">…</span>
             <span class="entity-name">${escapeHtml(l.entity_id.slice(0, 8))}</span>
           </span>`
        : `<span class="ink-muted">—</span>`;
      return `
        <tr>
          <td>${escapeHtml((l.timestamp || "").slice(0, 19))}</td>
          <td><span class="actor-pill actor-${actor.toLowerCase()}">${actor}</span></td>
          <td>${escapeHtml(l.operation || "")}</td>
          <td>${entityCell}</td>
          <td class="context">${escapeHtml(l.context_note || "")}</td>
        </tr>
      `;
    }).join("");
    $("#ops-log").innerHTML = `
      <table class="ops-table">
        <thead><tr>
          <th>when</th><th>actor</th><th>operation</th><th>entity</th><th>note</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    `;
    resolveAndPatch($("#ops-log"));
  } catch (e) {
    console.error("loadLog failed:", e);
    $("#ops-log").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

async function loadRules() {
  try {
    const rules = await data.rules();
    const items = rules.items || rules || [];
    if (items.length === 0) {
      $("#ops-rules").innerHTML = `<div class="empty">No always-on rules.</div>`;
      return;
    }
    $("#ops-rules").innerHTML = items.map(r => `
      <div class="rule-row">
        <div class="rule-meta">${escapeHtml(r.category || "")} · priority ${r.priority != null ? r.priority : "?"}</div>
        <div>${escapeHtml(r.content || "")}</div>
      </div>
    `).join("");
  } catch (e) {
    console.error("loadRules failed:", e);
    $("#ops-rules").innerHTML = `<div class="empty">${escapeHtml(e.message)}</div>`;
  }
}

// ============================================================
// Ask drawer
// ============================================================
function initAsk() {
  const drawer = $("#ask-drawer");
  const form = $("#ask-form");
  const input = $("#ask-input");
  const submit = $("#ask-submit");
  const status = $("#ask-status");
  const out = $("#ask-output");

  $("#ask-toggle").addEventListener("click", () => {
    openDrawer(drawer);
    input.focus();
  });

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    submit.disabled = true;
    out.innerHTML = "";

    let elapsed = 0;
    status.textContent = `Thinking… 0s`;
    const t0 = Date.now();
    const ticker = setInterval(() => {
      elapsed = Math.round((Date.now() - t0) / 1000);
      status.textContent = `Thinking… ${elapsed}s`;
    }, 500);

    try {
      const res = await data.agent(q);
      clearInterval(ticker);
      status.textContent = `Done in ${Math.round((Date.now() - t0) / 1000)}s.`;
      const answer = res.answer || JSON.stringify(res, null, 2);
      const rendered = renderWiki(answer);
      out.innerHTML = `<div class="wiki-content">${rendered.html}</div>`;
      // Wire any ref chips in the answer
      $$(".ref-chip", out).forEach(a => {
        a.addEventListener("click", (e2) => {
          e2.preventDefault();
          openEntityDrawer(a.dataset.ref);
        });
      });
    } catch (e) {
      clearInterval(ticker);
      status.textContent = "Error.";
      out.innerHTML = `<div class="empty"><strong>Failed:</strong> <code>${escapeHtml(e.message)}</code></div>`;
    } finally {
      submit.disabled = false;
    }
  });
}

// ============================================================
// Keyboard shortcuts
// ============================================================
function initKeys() {
  document.addEventListener("keydown", (ev) => {
    const tag = (ev.target.tagName || "").toLowerCase();
    const inField = tag === "input" || tag === "textarea";

    // Esc: close any drawer
    if (ev.key === "Escape") {
      closeDrawer($("#entity-drawer"));
      closeDrawer($("#ask-drawer"));
      return;
    }
    // Cmd/Ctrl+K: focus Ask
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      openDrawer($("#ask-drawer"));
      $("#ask-input").focus();
      return;
    }
    if (inField) return;
    // / : focus search (Reader)
    if (ev.key === "/") {
      ev.preventDefault();
      if (location.hash !== "#/ops") {
        $("#search-input").focus();
      }
    }
  });
}

// ============================================================
// Boot
// ============================================================
async function boot() {
  initTheme();
  initTabs();
  initDrawers();
  initSearch();
  initAsk();
  initGraph();
  initKeys();

  // Initial data load: the wiki index is cheap and needed by Reader.
  try {
    await loadWikiIndex();
  } catch (e) {
    $("#wiki-index").innerHTML = `<li class="empty">Index failed: ${escapeHtml(e.message)}</li>`;
  }

  window.addEventListener("hashchange", handleRoute);
  await handleRoute();
}

boot();
