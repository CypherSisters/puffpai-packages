/* Manuscript block renderers. The server sends blocks, never HTML; everything here builds DOM nodes and sets text
   with textContent only. Block types: markdown, table, timeline, graph, tree, list, quote (see CONTRACT.md).

   Blocks.render(blocks, host, {onJump(section, text, hint), maps})
   onJump is called when the owner clicks anything that carries a section number. maps, when given, is the address
   (or a function of the block giving it) of a "See in Maps" line drawn above every graph, tree and timeline.
   onEdit, when given, makes the quiet Edit control of a table row or list item that has a key in the block:
   onEdit(key) answers the button, or null when that entry has none. */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function sv(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  const str = v => (v === undefined || v === null) ? '' : (typeof v === 'object' ? String(v.text || v.label || '') : String(v));
  const secOf = v => {
    const n = Number(v);
    return Number.isInteger(n) && n > 0 ? n : null;
  };
  function widthOf(host) {
    const cs = getComputedStyle(host);
    const inner = (host.clientWidth || 0) - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0);
    return Math.max(260, Math.floor(inner > 0 ? inner : 480));
  }

  // Split text into lines of about `max` characters, at most `lines` lines (the last gets an ellipsis if cut).
  function wrap(text, max, lines) {
    const words = String(text || '').split(/\s+/).filter(Boolean);
    const out = [];
    let cur = '';
    for (const w of words) {
      if (cur && (cur + ' ' + w).length > max) { out.push(cur); cur = w; } else cur = cur ? cur + ' ' + w : w;
    }
    if (cur) out.push(cur);
    if (out.length > lines) {
      out.length = lines;
      out[lines - 1] = out[lines - 1].replace(/\s*\S*$/, '') + '...';
    }
    return out.length ? out : [''];
  }

  // A button that jumps to a section, for HTML blocks.
  function jumpButton(label, section, text, hint, opts) {
    const b = el('button', 'jump', label);
    b.type = 'button';
    b.title = 'Open section ' + section;
    b.onclick = () => opts.onJump && opts.onJump(section, text, hint);
    return b;
  }
  // Make an SVG group act like a button that jumps to a section.
  function svgJump(g, section, text, hint, opts) {
    g.setAttribute('class', (g.getAttribute('class') || '') + ' jump');
    g.setAttribute('tabindex', '0');
    g.setAttribute('role', 'button');
    const go = () => opts.onJump && opts.onJump(section, text, hint);
    g.addEventListener('click', go);
    g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
  }

  // ---- markdown: headings, paragraphs, bullets, bold, italic. Text nodes only.
  function inline(text, parent) {
    const parts = String(text).split(/(\*\*[^*\n]+?\*\*|\*[^*\s][^*\n]*?\*|_[^_\s][^_\n]*?_)/);
    for (const p of parts) {
      if (!p) continue;
      if (p.length > 4 && p.startsWith('**') && p.endsWith('**')) parent.appendChild(el('strong', '', p.slice(2, -2)));
      else if (p.length > 2 && ((p[0] === '*' && p.endsWith('*')) || (p[0] === '_' && p.endsWith('_')))) parent.appendChild(el('em', '', p.slice(1, -1)));
      else parent.appendChild(document.createTextNode(p));
    }
  }
  function markdown(b, host) {
    const lines = String(b.text || '').replace(/\r/g, '').split('\n');
    let para = [], list = null;
    const flush = () => {
      if (para.length) { const p = el('p'); inline(para.join(' '), p); host.appendChild(p); para = []; }
    };
    for (const raw of lines) {
      const line = raw.trim();
      let m;
      if (!line) { flush(); list = null; continue; }
      if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
        flush(); list = null;
        const h = el('h' + Math.min(6, m[1].length + 2), 'md-h'); inline(m[2], h); host.appendChild(h);
        continue;
      }
      if ((m = line.match(/^[-*+]\s+(.*)$/))) {
        flush();
        if (!list) { list = el('ul'); host.appendChild(list); }
        const li = el('li'); inline(m[1], li); list.appendChild(li);
        continue;
      }
      list = null; para.push(line);
    }
    flush();
  }

  // ---- table: a cell is a string or {text, section}
  function table(b, host, opts) {
    const box = el('div', 'scroll');
    const t = el('table');
    const cols = Array.isArray(b.columns) ? b.columns : [];
    if (cols.length) {
      const tr = el('tr');
      for (const c of cols) { const th = el('th', '', str(c)); th.scope = 'col'; tr.appendChild(th); }
      if (opts.onEdit && Array.isArray(b.keys)) tr.appendChild(el('th'));
      const thead = el('thead'); thead.appendChild(tr); t.appendChild(thead);
    }
    const tbody = el('tbody');
    (Array.isArray(b.rows) ? b.rows : []).forEach((row, at) => {
      const tr = el('tr');
      const cells = Array.isArray(row) ? row : [row];
      const hint = str(cells[0]);
      for (const c of cells) {
        const td = el('td');
        const n = c && typeof c === 'object' ? secOf(c.section) : null;
        if (n) td.appendChild(jumpButton(str(c), n, str(c), hint, opts));
        else td.textContent = str(c);
        tr.appendChild(td);
      }
      if (opts.onEdit && Array.isArray(b.keys)) {
        const td = el('td', 'edit-cell');
        const ed = opts.onEdit(b.keys[at]);
        if (ed) td.appendChild(ed);
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    box.appendChild(t);
    host.appendChild(box);
  }

  // ---- list: items [{text, section}]
  function list(b, host, opts) {
    const ul = el('ul', 'items');
    for (const it of Array.isArray(b.items) ? b.items : []) {
      const li = el('li');
      const text = str(it);
      const n = it && typeof it === 'object' ? secOf(it.section) : null;
      if (n) {
        const btn = jumpButton(text, n, text, '', opts);
        btn.classList.add('item');
        btn.appendChild(el('small', '', ' section ' + n));
        li.appendChild(btn);
      } else li.textContent = text;
      const ed = opts.onEdit && it && it.key ? opts.onEdit(it.key) : null;
      if (ed) li.appendChild(ed);
      ul.appendChild(li);
    }
    host.appendChild(ul);
  }

  // ---- quote: text, section, note
  function quote(b, host, opts) {
    const fig = el('figure', 'quote');
    fig.appendChild(el('blockquote', '', str(b.text)));
    const cap = el('figcaption');
    const n = secOf(b.section);
    if (n) cap.appendChild(jumpButton('Section ' + n, n, str(b.text), '', opts));
    if (b.note) cap.appendChild(el('span', 'note', str(b.note)));
    fig.appendChild(cap);
    host.appendChild(fig);
  }

  // Text sizes for laying out SVG labels before they are drawn (12px text; bold names a little wider).
  const CH = 7.2, LH = 14;
  const textW = lines => Math.max(...lines.map(t => t.length)) * CH;
  const hit = (a, b) => a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2;

  // ---- timeline (SVG): items [{label, when, section, who}] in order, on one horizontal line. Labels go above and
  // below the rail in turn, and a label that would touch the one before it on its side moves out a tier.
  function timeline(b, host, opts) {
    const items = Array.isArray(b.items) ? b.items : [];
    if (!items.length) { host.appendChild(el('p', 'empty', 'No events yet.')); return; }
    const W = widthOf(host), pad = 72, step = 84, n = items.length, lh = 15, gap = 10;
    const total = Math.max(W, pad * 2 + (n - 1) * step);
    const laid = items.map((it, i) => {
      const x = n === 1 ? total / 2 : pad + i * (total - 2 * pad) / (n - 1);
      const n2 = secOf(it.section);
      const sub = [str(it.when), n2 ? 'section ' + n2 : ''].filter(Boolean).join(', ');
      const all = wrap(str(it.label), 19, 3).map(t => [t, 'lbl']).concat((sub ? wrap(sub, 22, 1) : []).map(t => [t, 'sub']));
      const w = textW(all.map(a => a[0])) + gap;
      return {it: it, x: x, n2: n2, all: all, x1: Math.max(0, x - w / 2), x2: Math.min(total, x + w / 2), side: 0, tier: 0};
    });
    const ends = {};                                  // where each side and tier is free from, left to right
    let tiers = [1, 1];
    laid.forEach((L, i) => {
      const order = i % 2 === 0 ? [-1, 1] : [1, -1];
      let done = false;
      for (let tier = 0; tier < 6 && !done; tier++) {
        for (const side of order) {
          const key = side + ':' + tier;
          if ((ends[key] === undefined || ends[key] <= L.x1)) {
            L.side = side; L.tier = tier; ends[key] = L.x2; done = true;
            tiers[side < 0 ? 0 : 1] = Math.max(tiers[side < 0 ? 0 : 1], tier + 1);
            break;
          }
        }
      }
      if (!done) { L.side = order[0]; L.tier = 5; tiers[order[0] < 0 ? 0 : 1] = 6; }
    });
    const tierH = 4 * lh + 12;
    const y = 30 + tiers[0] * tierH;
    const H = y + 30 + tiers[1] * tierH;
    const box = el('div', 'scroll');
    const svg = sv('svg', {width: total, height: H, viewBox: `0 0 ${total} ${H}`, role: 'img', class: 'tl'});
    sv('title', {}, svg).textContent = 'Timeline of ' + n + ' events';
    sv('line', {x1: pad - 30, x2: total - pad + 30, y1: y, y2: y, class: 'axis'}, svg);
    for (const L of laid) {
      const it = L.it, x = L.x, above = L.side < 0;
      const g = sv('g', {class: 'tl-item'}, svg);
      const who = Array.isArray(it.who) ? it.who.map(str).filter(Boolean) : [];
      sv('title', {}, g).textContent = str(it.label) + (who.length ? '. Who: ' + who.join(', ') : '') + (L.n2 ? '. Section ' + L.n2 : '');
      sv('circle', {cx: x, cy: y, r: 7, class: 'dot'}, g);
      const reach = 16 + L.tier * tierH;
      sv('line', {x1: x, x2: x, y1: above ? y - 8 : y + 8, y2: above ? y - reach : y + reach, class: 'tick'}, g);
      const y0 = above ? y - reach - 6 - (L.all.length - 1) * lh : y + reach + 16;
      L.all.forEach(([t, cls], k) => {
        const tx = sv('text', {x: x, y: y0 + k * lh, 'text-anchor': 'middle', class: cls}, g);
        tx.textContent = t;
      });
      if (L.n2) svgJump(g, L.n2, str(it.label), who[0] || '', opts);
    }
    box.appendChild(svg);
    host.appendChild(box);
  }

  // ---- graph (SVG): nodes [{id, label, kind}], edges [{from, to, label}]. A force layout inside a box with a
  // margin; each node is a box (its circle and its name, wrapped at about 14 characters on two centred lines) and
  // boxes are pushed apart until none touch. Edge labels sit on a pill at the edge's middle, slid along the edge
  // when two would collide. Then the drawing is stretched to fill the card's width. Drag a node to move it.
  function graph(b, host) {
    const R = 10, M = 12;
    const nodes = (Array.isArray(b.nodes) ? b.nodes : []).filter(nd => nd && nd.id !== undefined)
      .map(nd => {
        const label = str(nd.label) || String(nd.id);
        const lines = wrap(label, 14, 2);
        return {id: String(nd.id), label: label, kind: str(nd.kind), lines: lines,
          w: Math.max(2 * R, textW(lines)) + 8, below: R + 6 + lines.length * LH, x: 0, y: 0};
      });
    if (!nodes.length) { host.appendChild(el('p', 'empty', 'No one to map yet.')); return; }
    const byId = new Map(nodes.map(nd => [nd.id, nd]));
    const edges = (Array.isArray(b.edges) ? b.edges : [])
      .filter(e => e && byId.has(String(e.from)) && byId.has(String(e.to)) && String(e.from) !== String(e.to))
      .map(e => ({a: byId.get(String(e.from)), b: byId.get(String(e.to)), label: str(e.label)}));
    const n = nodes.length;
    const W = widthOf(host);
    const H = Math.max(260, Math.round(110 + 44 * n * Math.max(1, 480 / W)));    // grows with the count
    const boxOf = (nd, pad) => ({x1: nd.x - nd.w / 2 - pad, x2: nd.x + nd.w / 2 + pad, y1: nd.y - R - pad, y2: nd.y + nd.below + pad});
    const keepIn = nd => {
      nd.x = Math.min(W - M - nd.w / 2, Math.max(M + nd.w / 2, nd.x));
      nd.y = Math.min(H - M - nd.below, Math.max(M + R, nd.y));
    };
    const cx = W / 2, cy = H / 2;
    nodes.forEach((nd, i) => {
      const a = 2 * Math.PI * i / n;
      nd.x = cx + Math.cos(a) * W * 0.32; nd.y = cy + Math.sin(a) * H * 0.32;
      keepIn(nd);
    });
    const k = Math.sqrt((W - 2 * M) * (H - 2 * M) / n) * 0.7;
    function collide(pad) {                          // push overlapping boxes apart along the shorter overlap
      let moved = false;
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const p = nodes[i], q = nodes[j], A = boxOf(p, pad), B = boxOf(q, pad);
        if (!hit(A, B)) continue;
        const ox = Math.min(A.x2, B.x2) - Math.max(A.x1, B.x1), oy = Math.min(A.y2, B.y2) - Math.max(A.y1, B.y1);
        if (ox < oy) { const s = (p.x < q.x ? -1 : 1) * ox / 2; p.x += s; q.x -= s; }
        else { const s = (p.y < q.y ? -1 : 1) * oy / 2; p.y += s; q.y -= s; }
        keepIn(p); keepIn(q); moved = true;
      }
      return moved;
    }
    let temp = W / 8;
    for (let it = 0; it < 300; it++) {
      for (const p of nodes) { p.dx = 0; p.dy = 0; }
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        const p = nodes[i], q = nodes[j];
        let dx = p.x - q.x, dy = p.y - q.y, d = Math.hypot(dx, dy);
        if (d < 0.01) { dx = 0.1 * (i - j); dy = 0.1; d = Math.hypot(dx, dy); }
        const f = k * k / d;
        p.dx += dx / d * f; p.dy += dy / d * f; q.dx -= dx / d * f; q.dy -= dy / d * f;
      }
      for (const e of edges) {
        const dx = e.a.x - e.b.x, dy = e.a.y - e.b.y, d = Math.max(0.01, Math.hypot(dx, dy));
        const f = d * d / k;
        e.a.dx -= dx / d * f; e.a.dy -= dy / d * f; e.b.dx += dx / d * f; e.b.dy += dy / d * f;
      }
      for (const p of nodes) {
        p.dx += (cx - p.x) * 0.03; p.dy += (cy - p.y) * 0.03;
        const d = Math.max(0.01, Math.hypot(p.dx, p.dy)), s = Math.min(d, temp) / d;
        p.x += p.dx * s; p.y += p.dy * s;
        keepIn(p);
      }
      if (it % 10 === 0) collide(4);
      temp = Math.max(1, temp * 0.97);
    }
    for (let pass = 0; pass < 200 && collide(4); pass++) { /* until no two boxes touch */ }
    // stretch to fill the card: the boxes' extent mapped onto the box inside the margin (text keeps its size)
    const ext = nodes.map(nd => boxOf(nd, 0));
    const x1 = Math.min(...ext.map(e => e.x1)), x2 = Math.max(...ext.map(e => e.x2));
    const y1 = Math.min(...ext.map(e => e.y1)), y2 = Math.max(...ext.map(e => e.y2));
    const fit = (v, a, b, lo, hi) => b - a < 1 ? (lo + hi) / 2 : lo + (v - a) * (hi - lo) / (b - a);
    for (const nd of nodes) {
      const bx = boxOf(nd, 0), half = nd.w / 2;
      nd.x = fit(bx.x1, x1, x2 - (bx.x2 - bx.x1), M, W - M - (bx.x2 - bx.x1)) + half;
      nd.y = fit(bx.y1, y1, y2 - (bx.y2 - bx.y1), M, H - M - (bx.y2 - bx.y1)) + R;
      keepIn(nd);
    }
    for (let pass = 0; pass < 50 && collide(4); pass++) { /* stretching never adds overlap; this is a guard */ }

    const wrapBox = el('div', 'graph-box');          // scrolls when the drawing is taller than 640 pixels
    const svg = sv('svg', {viewBox: `0 0 ${W} ${H}`, width: '100%', role: 'img', class: 'graph'});
    sv('title', {}, svg).textContent = 'Map of ' + n + ' people and places. Drag a name to move it.';
    const eg = sv('g', {}, svg), lg = sv('g', {}, svg), ng = sv('g', {}, svg);
    for (const e of edges) {
      e.line = sv('line', {class: 'edge'}, eg);
      if (e.label) {
        e.g = sv('g', {class: 'epill'}, lg);
        e.lines = wrap(e.label, 18, 1);
        e.w = textW(e.lines) + 10; e.h = 16;
        e.rect = sv('rect', {width: e.w, height: e.h, rx: 8}, e.g);
        e.text = sv('text', {class: 'elbl', 'text-anchor': 'middle'}, e.g);
        e.text.textContent = e.lines[0];
      }
    }
    const kinds = ['character', 'place'];
    for (const nd of nodes) {
      const kind = kinds.includes(nd.kind) ? nd.kind : 'other';
      nd.g = sv('g', {class: 'node k-' + kind, tabindex: '0'}, ng);
      sv('title', {}, nd.g).textContent = nd.label + (nd.kind ? ' (' + nd.kind + ')' : '');
      sv('circle', {r: R}, nd.g);
      nd.lines.forEach((t, i) => {
        const tx = sv('text', {y: R + 16 + i * LH, 'text-anchor': 'middle', class: 'nlbl'}, nd.g);
        tx.textContent = t;
      });
    }
    function place() {
      for (const nd of nodes) nd.g.setAttribute('transform', `translate(${nd.x.toFixed(1)},${nd.y.toFixed(1)})`);
      const taken = nodes.map(nd => boxOf(nd, 2));
      for (const e of edges) {
        e.line.setAttribute('x1', e.a.x); e.line.setAttribute('y1', e.a.y);
        e.line.setAttribute('x2', e.b.x); e.line.setAttribute('y2', e.b.y);
        if (!e.g) continue;
        let best = null, bestCost = Infinity;
        const len = Math.max(1, Math.hypot(e.b.x - e.a.x, e.b.y - e.a.y));
        const nx = -(e.b.y - e.a.y) / len, ny = (e.b.x - e.a.x) / len;
        const area = (a, o) => Math.max(0, Math.min(a.x2, o.x2) - Math.max(a.x1, o.x1)) *
          Math.max(0, Math.min(a.y2, o.y2) - Math.max(a.y1, o.y1));
        search:
        for (const side of [0, 18, -18, 36, -36, 54, -54]) {  // on the edge first, then just beside it
          for (const t of [0.5, 0.38, 0.62, 0.28, 0.72, 0.2, 0.8, 0.12, 0.88]) {   // the middle, else slid along
            const px = e.a.x + (e.b.x - e.a.x) * t + nx * side, py = e.a.y + (e.b.y - e.a.y) * t + ny * side;
            const bx = {x1: px - e.w / 2, x2: px + e.w / 2, y1: py - e.h / 2, y2: py + e.h / 2};
            if (bx.x1 < 0 || bx.x2 > W || bx.y1 < 0 || bx.y2 > H) continue;
            // another label counts far more than a name: a pill over a name still reads, two pills do not
            const cost = taken.reduce((s, o, k) => s + area(bx, o) * (k >= nodes.length ? 10 : 1), 0);
            if (cost < bestCost) { best = bx; bestCost = cost; }
            if (cost === 0) break search;
          }
        }
        if (!best) {
          const px = (e.a.x + e.b.x) / 2, py = (e.a.y + e.b.y) / 2;
          best = {x1: px - e.w / 2, x2: px + e.w / 2, y1: py - e.h / 2, y2: py + e.h / 2};
        }
        const sx = Math.min(W - e.w, Math.max(0, best.x1)), sy = Math.min(H - e.h, Math.max(0, best.y1));
        taken.push({x1: sx, x2: sx + e.w, y1: sy, y2: sy + e.h});
        e.rect.setAttribute('x', sx.toFixed(1)); e.rect.setAttribute('y', sy.toFixed(1));
        e.text.setAttribute('x', (sx + e.w / 2).toFixed(1)); e.text.setAttribute('y', (sy + e.h / 2 + 4).toFixed(1));
      }
    }
    place();
    function toSvg(ev) {
      const m = svg.getScreenCTM();
      if (!m) return null;
      const pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
      return pt.matrixTransform(m.inverse());
    }
    for (const nd of nodes) {
      nd.g.addEventListener('pointerdown', ev => {
        ev.preventDefault();
        nd.g.setPointerCapture(ev.pointerId);
        nd.g.classList.add('drag');
        const move = e2 => {
          const p = toSvg(e2);
          if (!p) return;
          nd.x = p.x; nd.y = p.y; keepIn(nd);
          place();
        };
        const up = () => {
          nd.g.classList.remove('drag');
          nd.g.removeEventListener('pointermove', move);
          nd.g.removeEventListener('pointerup', up);
          nd.g.removeEventListener('pointercancel', up);
        };
        nd.g.addEventListener('pointermove', move);
        nd.g.addEventListener('pointerup', up);
        nd.g.addEventListener('pointercancel', up);
      });
      nd.g.addEventListener('keydown', ev => {      // arrow keys move a focused node
        const d = {ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10]}[ev.key];
        if (!d) return;
        ev.preventDefault();
        nd.x += d[0]; nd.y += d[1]; keepIn(nd);
        place();
      });
    }
    wrapBox.appendChild(svg);
    host.appendChild(wrapBox);
    host.appendChild(el('p', 'hint', 'Drag a name to move it.'));
  }

  // ---- tree (SVG): root {label, children: [...]}, layered top down. Each leaf has its own column and a parent sits
  // over its children, so no two boxes share space; long names wrap onto up to three lines and the box grows.
  function tree(b, host, opts) {
    const root = b.root;
    if (!root || typeof root !== 'object') { host.appendChild(el('p', 'empty', 'Nothing to draw yet.')); return; }
    const colW = 140, boxW = 128, rowGap = 40;
    let leaves = 0, depthMax = 0, count = 0;
    const tallest = [];
    function lay(node, depth) {
      count++;
      if (count > 400) return null;                 // a runaway tree: draw the first 400
      const lines = wrap(str(node.label), 16, 3);
      const out = {label: str(node.label), lines: lines, h: 16 + lines.length * 15, section: secOf(node.section),
        depth: depth, kids: []};
      tallest[depth] = Math.max(tallest[depth] || 0, out.h);
      depthMax = Math.max(depthMax, depth);
      const kids = Array.isArray(node.children) ? node.children : [];
      for (const c of kids) {
        if (c && typeof c === 'object' && depth < 12) { const k = lay(c, depth + 1); if (k) out.kids.push(k); }
      }
      if (!out.kids.length) out.x = leaves++;
      else out.x = (out.kids[0].x + out.kids[out.kids.length - 1].x) / 2;
      return out;
    }
    const t = lay(root, 0);
    const top = [8];
    for (let d = 1; d <= depthMax; d++) top[d] = top[d - 1] + tallest[d - 1] + rowGap;
    const W = Math.max(widthOf(host), leaves * colW + 16);
    const H = top[depthMax] + tallest[depthMax] + 10;
    const off = (W - leaves * colW) / 2 + colW / 2;
    const box = el('div', 'scroll');
    const svg = sv('svg', {width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', class: 'tree'});
    sv('title', {}, svg).textContent = 'Tree starting at ' + t.label;
    const lg = sv('g', {}, svg), ng = sv('g', {}, svg);
    function draw(nd) {
      const x = off + nd.x * colW, y = top[nd.depth];
      for (const k of nd.kids) {
        const kx = off + k.x * colW, ky = top[k.depth], mid = y + tallest[nd.depth] + rowGap / 2;
        sv('path', {d: `M${x},${y + nd.h} V${mid} H${kx} V${ky}`, class: 'link'}, lg);
        draw(k);
      }
      const g = sv('g', {class: 'tnode'}, ng);
      sv('title', {}, g).textContent = nd.label;
      sv('rect', {x: x - boxW / 2, y: y, width: boxW, height: nd.h, rx: 7}, g);
      nd.lines.forEach((ln, i) => {
        const tx = sv('text', {x: x, y: y + nd.h / 2 + 5 + (i - (nd.lines.length - 1) / 2) * 15, 'text-anchor': 'middle'}, g);
        tx.textContent = ln;
      });
      if (nd.section) svgJump(g, nd.section, nd.label, '', opts);
    }
    draw(t);
    box.appendChild(svg);
    host.appendChild(box);
  }

  const KINDS = {markdown: markdown, table: table, timeline: timeline, graph: graph, tree: tree, list: list, quote: quote};

  const DRAWN = ['graph', 'tree', 'timeline'];

  function render(blocks, host, opts) {
    opts = opts || {};
    for (const b of Array.isArray(blocks) ? blocks : []) {
      const card = el('div', 'block');
      host.appendChild(card);                       // attached first, so SVG blocks can measure the width
      const fn = b && KINDS[b.type];
      if (!fn) { card.appendChild(el('p', 'empty', 'This part could not be shown.')); continue; }
      card.classList.add('block-' + b.type);
      if (opts.maps && DRAWN.includes(b.type)) {          // the same drawing, big, in Maps
        const p = el('p', 'maps-line');
        const a = el('a', 'maps-link', 'See in Maps');
        a.href = typeof opts.maps === 'function' ? opts.maps(b) : opts.maps;
        p.appendChild(a);
        card.appendChild(p);
      }
      try { fn(b, card, opts); } catch (e) {
        for (const c of Array.from(card.childNodes)) if (!(c.classList && c.classList.contains('maps-line'))) c.remove();
        card.appendChild(el('p', 'empty', 'This part could not be shown.'));
      }
    }
  }

  window.Blocks = {render: render};
})();
