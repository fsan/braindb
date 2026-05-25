// ============================================================
// wiki-render.js — minimal Markdown renderer for BrainDB wiki
// body grammar. No dependencies.
//
// Handles:
//   <!-- wiki:meta key=value key=value … -->
//   # Heading, ## Heading, ### Heading
//   > **Summary:** … and > **Disambiguation:** … callouts
//   <!-- section:slug --> dividers
//   GFM tables (| col | col |)
//   - / * / 1. lists
//   **bold**, `code`, [text](url)
//   [[ref:UUID|optional display]] and [[ref:UUID]] chips,
//     tolerantly handling the grouped form [[ref:a], [ref:b]]
// ============================================================

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Inline pass: refs → chips, **bold**, `code`, [text](url)
function renderInline(text) {
  // Protect anything dangerous, then progressively replace tokens.
  let out = escapeHtml(text);

  // [[ref:UUID|display]]  and  [[ref:UUID]]
  // Tolerant: also catches  [ref:UUID]  inside a grouped  [[ref:a], [ref:b]]
  out = out.replace(/\[\[ref:([0-9a-f-]+)(?:\|([^\]]+))?\]\]/gi,
    (_, id, disp) => refChip(id, disp));
  out = out.replace(/\[ref:([0-9a-f-]+)(?:\|([^\]]+))?\]/gi,
    (_, id, disp) => refChip(id, disp));

  // [text](url)
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g,
    (_, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);

  // `code`
  out = out.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);

  // **bold**
  out = out.replace(/\*\*([^*]+)\*\*/g, (_, b) => `<strong>${b}</strong>`);

  return out;
}

function refChip(id, displayMaybe) {
  if (!UUID_RE.test(id)) return `<span class="ref-chip-broken">${escapeHtml(id)}</span>`;
  const display = displayMaybe ? escapeHtml(displayMaybe) : id.slice(0, 6);
  return `<a class="ref-chip" data-ref="${id}" title="${id}" href="#">${display}</a>`;
}

// Parse  <!-- wiki:meta key=value key=value -->  from the body's first line.
// Tolerant: keys are unquoted; values may contain semicolons; the key list is
// space-separated; we don't enforce a fixed schema.
function parseMeta(line) {
  const m = line.match(/<!--\s*wiki:meta\s+(.+?)\s*-->/);
  if (!m) return null;
  const meta = {};
  const re = /(\w+)=("[^"]*"|[^\s]+)/g;
  let mm;
  while ((mm = re.exec(m[1])) !== null) {
    meta[mm[1]] = mm[2].replace(/^"|"$/g, "");
  }
  return meta;
}

// Top-level: turn the full wiki body markdown into HTML.
// Returns { html, meta, refs:[uuid...] }
export function renderWiki(body) {
  if (!body) return { html: "", meta: null, refs: [] };

  const lines = body.split(/\r?\n/);
  let meta = null;
  let i = 0;

  // 1. meta header (first non-empty line that matches)
  while (i < lines.length && lines[i].trim() === "") i++;
  if (i < lines.length) {
    const candidate = parseMeta(lines[i]);
    if (candidate) {
      meta = candidate;
      i++;
    }
  }

  const out = [];
  const refs = new Set();

  while (i < lines.length) {
    const line = lines[i];

    // Section divider marker
    const sec = line.match(/^<!--\s*section:([\w-]+)\s*-->/);
    if (sec) {
      out.push(`<hr class="section-divider" data-section="${sec[1]}" />`);
      i++;
      continue;
    }

    // Other HTML comments — skip
    if (/^<!--.*-->$/.test(line.trim())) { i++; continue; }

    // Blank line
    if (line.trim() === "") { i++; continue; }

    // # Title  (h1)
    if (/^#\s+/.test(line)) {
      const text = line.replace(/^#\s+/, "");
      out.push(`<h1>${renderInline(text)}</h1>`);
      i++;
      continue;
    }
    // ## Heading (h2)
    if (/^##\s+/.test(line)) {
      const text = line.replace(/^##\s+/, "");
      out.push(`<h2>${renderInline(text)}</h2>`);
      i++;
      continue;
    }
    // ### Heading (h3)
    if (/^###\s+/.test(line)) {
      const text = line.replace(/^###\s+/, "");
      out.push(`<h3>${renderInline(text)}</h3>`);
      i++;
      continue;
    }

    // Callout: > **Summary:** ...   (multi-line — collect contiguous > lines)
    if (/^>\s*/.test(line)) {
      const block = [];
      while (i < lines.length && /^>\s*/.test(lines[i])) {
        block.push(lines[i].replace(/^>\s?/, ""));
        i++;
      }
      const joined = block.join(" ");
      const kindMatch = joined.match(/^\*\*(Summary|Disambiguation)[:\s]\*\*\s*(.+)/i);
      let cls = "callout";
      if (kindMatch) {
        cls += " " + kindMatch[1].toLowerCase();
        // Escape the label, render-inline the prose, concatenate outside —
        // never put pre-built HTML inside a string that goes through
        // renderInline (escapeHtml would mangle the tags).
        const labelText = kindMatch[1];
        const rest = kindMatch[2];
        out.push(`<blockquote class="${cls}"><strong>${escapeHtml(labelText)}:</strong> ${renderInline(rest)}</blockquote>`);
      } else {
        out.push(`<blockquote class="${cls}">${renderInline(joined)}</blockquote>`);
      }
      continue;
    }

    // Table (GFM): a line starting with `|`, followed by a separator line
    if (/^\|/.test(line) && i + 1 < lines.length && /^\|\s*[-:|\s]+$/.test(lines[i + 1])) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) {
        rows.push(lines[i]);
        i++;
      }
      out.push(renderTable(rows));
      continue;
    }

    // Bullet list (- or *)
    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ""));
        i++;
      }
      const li = items.map(it => `<li>${renderInline(it)}</li>`).join("");
      out.push(`<ul>${li}</ul>`);
      continue;
    }

    // Numbered list
    if (/^\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ""));
        i++;
      }
      const li = items.map(it => `<li>${renderInline(it)}</li>`).join("");
      out.push(`<ol>${li}</ol>`);
      continue;
    }

    // Paragraph: gather consecutive non-empty lines
    const para = [];
    while (i < lines.length && lines[i].trim() !== ""
      && !/^[#>|-]/.test(lines[i])
      && !/^\d+\.\s+/.test(lines[i])
      && !/^<!--/.test(lines[i].trim())) {
      para.push(lines[i]);
      i++;
    }
    if (para.length > 0) {
      out.push(`<p>${renderInline(para.join(" "))}</p>`);
    } else {
      // safety — should not happen, but advance to avoid an infinite loop
      i++;
    }
  }

  // Collect all referenced UUIDs from the rendered HTML for caller use
  const html = out.join("\n");
  const refRe = /data-ref="([0-9a-f-]+)"/gi;
  let m;
  while ((m = refRe.exec(html)) !== null) refs.add(m[1]);

  return { html, meta, refs: Array.from(refs) };
}

function renderTable(rows) {
  // rows[0] = header, rows[1] = separator, rows[2..] = body
  const splitRow = r => r.replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());
  const header = splitRow(rows[0]);
  const body = rows.slice(2).map(splitRow);
  const th = header.map(c => `<th>${renderInline(c)}</th>`).join("");
  const tb = body.map(r => "<tr>" + r.map(c => `<td>${renderInline(c)}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`;
}

// Extract sections from rendered HTML for a TOC. Returns [{slug, title}]
export function extractSections(body) {
  if (!body) return [];
  const sections = [];
  const lines = body.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const sec = lines[i].match(/^<!--\s*section:([\w-]+)\s*-->/);
    if (sec) {
      // Look ahead for the next heading or callout-bold to use as a title
      let title = sec[1].replace(/-/g, " ");
      for (let j = i + 1; j < Math.min(i + 4, lines.length); j++) {
        const h = lines[j].match(/^#{2,3}\s+(.+)/);
        if (h) { title = h[1]; break; }
      }
      sections.push({ slug: sec[1], title });
    }
  }
  return sections;
}

// Compare inline [[ref:UUID]] chips to the wiki's `summarises` relations.
// Returns { inline: N, relations: M, consistent: bool, only_inline: [...], only_relations: [...] }
// Mirrors the spirit of export_wikis._consistency.
export function consistencyCheck(body, summarisesIds) {
  const inline = new Set();
  if (body) {
    const re = /\[\[?ref:([0-9a-f-]+)/gi;
    let m;
    while ((m = re.exec(body)) !== null) inline.add(m[1]);
  }
  const rel = new Set(summarisesIds || []);
  const onlyInline = [...inline].filter(x => !rel.has(x));
  const onlyRel = [...rel].filter(x => !inline.has(x));
  return {
    inline: inline.size,
    relations: rel.size,
    consistent: onlyInline.length === 0 && onlyRel.length === 0,
    only_inline: onlyInline,
    only_relations: onlyRel,
  };
}
