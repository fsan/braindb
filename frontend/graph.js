// ============================================================
// graph.js — vis-network powered graph view of BrainDB entities.
//   - Different shapes / colours per entity_type.
//   - Edge labels hidden by default, revealed on hover / select.
//   - Soft cap of 300 nodes.
//   - Hidden-container mount handled via IntersectionObserver +
//     network.setSize() so the graph centres correctly once visible.
//
// Depends on `window.vis` from the vis-network CDN script tag in
// index.html. All other modules call this through the public API
// below — graph.ensureMounted / seed / searchSeeds / clear / fit /
// reset / toggleKeywords / refreshStyle.
// ============================================================

const API = (window.BRAINDB_API_URL || "http://localhost:8000") + "/api/v1";

// ------------------------------------------------------------
// Self-contained fetch wrappers
// ------------------------------------------------------------
async function apiGet(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(`HTTP ${r.status} on ${path}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status} on ${path}`);
  return r.json();
}

// ------------------------------------------------------------
// Per-session state
// ------------------------------------------------------------
let network = null;
let nodesDS = null;
let edgesDS = null;
const inFlight = new Set();         // entity IDs currently being expanded
let onClickCb = null;
const HARD_CAP = 300;

// ------------------------------------------------------------
// Theme tokens
// ------------------------------------------------------------
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function colours() {
  return {
    ink: cssVar("--ink") || "#1b1b1b",
    inkSoft: cssVar("--ink-soft") || "#4a4a4a",
    inkMuted: cssVar("--ink-muted") || "#8a8a8a",
    paper: cssVar("--paper") || "#fff",
    paperSoft: cssVar("--paper-soft") || "#f8f8f7",
    rule: cssVar("--rule") || "#eaeaea",
    accent: cssVar("--accent") || "#0645ad",
    amber: "#b86c00",
    teal: "#226666",
    rule_: "#7a4a8a",
  };
}

// ------------------------------------------------------------
// Entity → vis-network node config
// ------------------------------------------------------------
function nodeConfig(entity) {
  const c = colours();
  const t = entity.entity_type;
  const label = entityLabel(entity);
  const base = {
    id: entity.id,
    label,
    title: label,              // hover tooltip
    entity_type: t,            // custom field, used by toggleKeywords
    font: { size: 11, color: c.ink, face: "-apple-system, system-ui, sans-serif" },
    borderWidth: 1,
  };
  switch (t) {
    case "wiki":
      return { ...base, shape: "box", color: { background: c.accent, border: c.accent },
        font: { ...base.font, color: c.paper, vadjust: 0 },
        shapeProperties: { borderRadius: 6 },
        widthConstraint: { maximum: 110 },
        margin: 6 };
    case "fact":
      return { ...base, shape: "ellipse",
        color: { background: c.paperSoft, border: c.inkSoft },
        widthConstraint: { maximum: 110 } };
    case "thought":
      return { ...base, shape: "ellipse",
        color: { background: c.paperSoft, border: c.inkMuted },
        shapeProperties: { borderDashes: [4, 2] },
        widthConstraint: { maximum: 110 } };
    case "keyword":
      // Diamond/dot/database etc. all place their label BELOW the shape,
      // so the label needs ink colour to be readable on the paper background.
      return { ...base, shape: "diamond",
        color: { background: c.amber, border: c.amber },
        font: { ...base.font, color: c.inkSoft },
        size: 10 };
    case "source":
    case "datasource":
      return { ...base, shape: "database",
        color: { background: c.teal, border: c.teal },
        font: { ...base.font, color: c.inkSoft } };
    case "rule":
      // `box` shape contains its label INSIDE, so white-on-purple is fine.
      return { ...base, shape: "box",
        color: { background: c.rule_, border: c.rule_ },
        font: { ...base.font, color: c.paper },
        shapeProperties: { borderRadius: 6 } };
    default:
      return { ...base, shape: "dot", size: 8,
        color: { background: c.paperSoft, border: c.inkSoft } };
  }
}

function edgeConfig(rel) {
  const c = colours();
  const id = rel.id || `${rel.from_entity_id}__${rel.relation_type}__${rel.to_entity_id}`;
  let color = c.inkMuted;
  if (rel.relation_type === "summarises") color = c.accent;
  else if (rel.relation_type === "tagged_with") color = c.amber;
  else if (rel.relation_type === "derived_from") color = c.teal;
  else if (rel.relation_type === "consolidates") color = c.rule_;
  return {
    id,
    from: rel.from_entity_id,
    to: rel.to_entity_id,
    relation_type: rel.relation_type,
    arrows: { to: { enabled: true, scaleFactor: 0.5 } },
    color: { color, opacity: 0.5 },
    width: 1,
    smooth: { enabled: true, type: "dynamic", roundness: 0.3 },
    font: { size: 0, align: "middle" },   // hidden by default
  };
}

function entityLabel(entity) {
  if (!entity) return "?";
  const idShort = (entity.id || "").slice(0, 6);
  if (entity.entity_type === "wiki") {
    const h = (entity.content || "").match(/^#\s+(.+)$/m);
    if (h) return truncate(h[1].trim(), 24);
    const m = (entity.content || "").match(/canonical_name=(?:"([^"]+)"|([^\s][^>]*?))(?=\s+\w+=|\s*-->|$)/);
    if (m) return truncate((m[1] || m[2] || "").trim(), 24);
    return idShort;
  }
  if (entity.entity_type === "keyword") {
    const k = (entity.content || "").trim();
    return k ? truncate(k, 20) : idShort;
  }
  const c = (entity.title || entity.content || "").toString().trim();
  if (c) return truncate(c.split(/\r?\n/)[0], 24);
  return idShort;
}

function truncate(s, n) {
  if (!s) return "";
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// ------------------------------------------------------------
// Public API
// ------------------------------------------------------------
export function isInitialised() {
  return network !== null;
}

export function ensureMounted(container, onNodeClick) {
  if (network) return network;

  if (typeof window.vis === "undefined" || !window.vis.Network) {
    container.innerHTML = `<div class="graph-empty">Graph library failed to load (vis-network unavailable). Check the CDN script in index.html.</div>`;
    return null;
  }

  onClickCb = onNodeClick;

  nodesDS = new window.vis.DataSet([]);
  edgesDS = new window.vis.DataSet([]);

  const options = {
    autoResize: true,
    nodes: {
      // sensible default; per-node overrides supplied in nodeConfig()
      borderWidth: 1,
      borderWidthSelected: 3,
      scaling: { label: { enabled: false } },
    },
    edges: {
      color: { inherit: false },
      smooth: { enabled: true, type: "dynamic" },
      hoverWidth: 0,
      selectionWidth: 0,
    },
    physics: {
      enabled: true,
      barnesHut: {
        gravitationalConstant: -8000,
        centralGravity: 0.05,
        springLength: 130,
        springConstant: 0.04,
        damping: 0.6,
        avoidOverlap: 0.5,
      },
      stabilization: {
        enabled: true,
        iterations: 250,
        updateInterval: 25,
        fit: true,                 // <-- vis-network's own auto-fit after settle
      },
    },
    interaction: {
      hover: true,
      hoverConnectedEdges: true,
      zoomView: true,
      dragView: true,
      tooltipDelay: 250,
      multiselect: false,
    },
    layout: {
      improvedLayout: true,
    },
  };

  network = new window.vis.Network(container, { nodes: nodesDS, edges: edgesDS }, options);

  // Click → drawer
  network.on("click", (params) => {
    if (params.nodes && params.nodes[0] && onClickCb) onClickCb(params.nodes[0]);
  });

  // Double-click → expand neighbourhood
  network.on("doubleClick", async (params) => {
    if (params.nodes && params.nodes[0]) {
      await expandNode(params.nodes[0]);
    }
  });

  // Hover an edge → reveal its label
  network.on("hoverEdge", (params) => {
    edgesDS.update({ id: params.edge, font: { size: 10, color: colours().inkSoft, strokeWidth: 3, strokeColor: colours().paper } });
  });
  network.on("blurEdge", (params) => {
    edgesDS.update({ id: params.edge, font: { size: 0 } });
  });

  // Selecting a node → reveal incident edges' labels
  network.on("selectNode", (params) => {
    const nid = params.nodes[0];
    if (!nid) return;
    const incident = network.getConnectedEdges(nid);
    const updates = incident.map(eid => ({ id: eid, font: { size: 10, color: colours().inkSoft, strokeWidth: 3, strokeColor: colours().paper } }));
    edgesDS.update(updates);
  });
  network.on("deselectNode", () => {
    const updates = edgesDS.getIds().map(eid => ({ id: eid, font: { size: 0 } }));
    edgesDS.update(updates);
  });

  // Once the layout settles, fit explicitly (belt-and-braces; physics.stabilization.fit=true also does it).
  network.on("stabilized", () => {
    network.fit({ animation: { duration: 250, easingFunction: "easeInOutQuad" } });
  });

  // Hidden-container handling: if the canvas wasn't visible at mount, fit
  // again once it appears. IntersectionObserver fires when the container
  // gets non-zero dimensions.
  if (typeof IntersectionObserver !== "undefined") {
    const io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting && e.target.clientWidth > 10) {
          network.setSize(e.target.clientWidth + "px", e.target.clientHeight + "px");
          network.redraw();
          network.fit({ animation: false });
        }
      }
    });
    io.observe(container);
  }

  // Window resize → refit
  window.addEventListener("resize", () => {
    if (network) network.redraw();
  });

  return network;
}

export async function seed(entityId, onStatus) {
  if (!network) return;
  onStatus?.("Loading…");
  try {
    const entity = await apiGet(`/entities/${entityId}`);
    addOrUpdateNode(entity);
    await expandNode(entityId, /*silent=*/true);
    onStatus?.("");
  } catch (e) {
    console.error("graph.seed failed:", e);
    onStatus?.(`Seed failed: ${e.message}`);
  }
}

export async function searchSeeds(query) {
  if (!query.trim()) return [];
  const res = await apiPost("/memory/context", { queries: [query], max_depth: 1, max_results: 12 });
  return res.items || [];
}

export function clear() {
  if (!network) return;
  nodesDS.clear();
  edgesDS.clear();
}

export function fit() {
  if (network) network.fit({ animation: { duration: 250 } });
}

export function reset() {
  clear();
}

export function toggleKeywords(hide) {
  if (!nodesDS) return;
  const updates = nodesDS.get({
    filter: n => n.entity_type === "keyword",
  }).map(n => ({ id: n.id, hidden: !!hide }));
  nodesDS.update(updates);
}

export function refreshStyle() {
  if (!nodesDS) return;
  // Re-apply colours from CSS variables for every node + edge (theme change).
  const nodeUpdates = nodesDS.get().map(n => {
    // Reconstitute config from stored entity_type + label
    const cfg = nodeConfig({ id: n.id, entity_type: n.entity_type, content: n.label, title: n.label });
    return { id: n.id, color: cfg.color, font: cfg.font, shapeProperties: cfg.shapeProperties };
  });
  nodesDS.update(nodeUpdates);
}

// ------------------------------------------------------------
// Internals
// ------------------------------------------------------------
function addOrUpdateNode(entity) {
  if (!nodesDS) return null;
  const existing = nodesDS.get(entity.id);
  const cfg = nodeConfig(entity);
  if (existing) {
    nodesDS.update(cfg);
    return cfg;
  }
  if (nodesDS.length >= HARD_CAP) return null;
  nodesDS.add(cfg);
  return cfg;
}

function addOrUpdateEdge(rel) {
  if (!edgesDS) return null;
  const cfg = edgeConfig(rel);
  if (edgesDS.get(cfg.id)) return cfg;
  // Both endpoints must already be in the graph for the edge to render.
  if (!nodesDS.get(rel.from_entity_id) || !nodesDS.get(rel.to_entity_id)) return null;
  edgesDS.add(cfg);
  return cfg;
}

async function expandNode(entityId, silent = false) {
  if (!network) return;
  if (inFlight.has(entityId)) return;
  inFlight.add(entityId);
  try {
    const relations = await apiGet(`/entities/${entityId}/relations`);
    const ids = new Set();
    for (const r of relations) {
      ids.add(r.from_entity_id);
      ids.add(r.to_entity_id);
    }
    ids.delete(entityId);
    const missing = [...ids].filter(id => !nodesDS.get(id));
    if (nodesDS.length + missing.length > HARD_CAP) {
      if (!silent) flashToast(`Graph capped at ${HARD_CAP} nodes; prune before expanding more.`);
    } else {
      const fetched = await Promise.all(missing.map(async id => {
        try { return await apiGet(`/entities/${id}`); } catch { return null; }
      }));
      for (const e of fetched) if (e) addOrUpdateNode(e);
    }
    for (const r of relations) addOrUpdateEdge(r);
  } catch (e) {
    console.error(`expandNode(${entityId}) failed:`, e);
    if (!silent) flashToast(`Expand failed: ${e.message}`);
  } finally {
    inFlight.delete(entityId);
  }
}

// Small toast for non-blocking status (cap message etc.)
let toastEl = null;
function flashToast(msg) {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.className = "graph-toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.classList.add("show");
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => toastEl.classList.remove("show"), 3200);
}
