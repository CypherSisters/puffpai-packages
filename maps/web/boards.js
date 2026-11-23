/* Maps 2.2.0: draws one board's spec ("maps/1", see draw.py) as an SVG that fills its pane. The server has laid
   everything out; this file draws it, puts each edge label where it does not cover a name or another label,
   zooms (wheel, + and -), pans (drag on empty space), lets nodes be dragged on boards that allow it, and makes a
   PNG of the whole board in the browser. A board opens fitted to its pane, or at the view it was left at.
   Edge labels hide below 60 percent and come back when zoomed in.

   MapsBoards.draw(container, spec, {onPoint(item), onMoved(), view, onView(view or null), portraits})
   portraits (the atlas's family tree only): {node id: picture URL}, each drawn inside its node's circle.
     -> {fit(), reset(), zoom(f), positions(), png(name)}
*/
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs || {}) if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== '') e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function tip(e, text) { if (text) { const t = el('title', {}, e); t.textContent = text; } }
  const num = v => (typeof v === 'number' && isFinite(v) ? v : 0);

  // sizes as draw.py counts them: a 13 pixel name, an 11 pixel edge label
  const NAME_PX = 7.4, LINE_PX = 16.5, EDGE_PX = 6.2, EDGE_H = 16, EDGE_MAX = 28;
  const nameLines = n => (Array.isArray(n.lines) && n.lines.length ? n.lines : [n.label || n.id]).map(String);
  // the box a node and its name take up, the same as draw.label_box
  function nodeBox(n) {
    const r = num(n.r) || 12, lines = nameLines(n);
    const hw = Math.max(r, Math.max(...lines.map(s => s.length)) * NAME_PX / 2 + 4);
    return [num(n.x) - hw, num(n.y) - r - 2, num(n.x) + hw, num(n.y) + r + 6 + lines.length * LINE_PX];
  }
  const hits = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];

  function textLines(parent, x, y, lines, cls, anchor, rotate) {
    const t = el('text', {x: x, y: y, class: cls || '', 'text-anchor': anchor || 'start',
      transform: rotate ? 'rotate(' + num(rotate) + ' ' + x + ' ' + y + ')' : ''}, parent);
    (lines || []).forEach((line, i) => {
      const s = el('tspan', {x: x, dy: i ? '1.25em' : 0}, t);
      s.textContent = String(line);
    });
    return t;
  }

  // A smooth curve through the points (Catmull-Rom, as cubic Beziers).
  function smoothPath(pts) {
    if (pts.length < 3) return 'M' + pts.map(p => p[0] + ' ' + p[1]).join(' L');
    let d = 'M' + pts[0][0] + ' ' + pts[0][1];
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
      const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
      const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
      d += ' C' + c1.join(' ') + ' ' + c2.join(' ') + ' ' + p2.join(' ');
    }
    return d;
  }

  function shape(g, it) {
    const x = num(it.x), y = num(it.y), r = num(it.r) || 6;
    if (it.shape === 'diamond') return el('polygon', {points: [[x, y - r], [x + r, y], [x, y + r], [x - r, y]].join(' '), class: it.cls}, g);
    if (it.shape === 'flag') {
      el('line', {x1: x - r, y1: y + r, x2: x - r, y2: y - r - 2, class: it.cls}, g);
      return el('polygon', {points: [[x - r, y - r - 2], [x + r, y - r / 2 - 1], [x - r, y]].join(' '), class: it.cls}, g);
    }
    return el('circle', {cx: x, cy: y, r: r, class: it.cls}, g);
  }

  function draw(container, spec, opts) {
    opts = opts || {};
    container.textContent = '';
    const W = Math.max(num(spec.width), 10), H = Math.max(num(spec.height), 10);
    const svg = el('svg', {xmlns: NS, role: 'img', 'aria-label': spec.kind || 'board'}, container);
    const root = el('g', {}, svg);
    const L = {lines: el('g', {}, root), paths: el('g', {}, root), rects: el('g', {}, root), edges: el('g', {}, root),
      nodes: el('g', {}, root), elabels: el('g', {}, root), pins: el('g', {}, root), texts: el('g', {}, root)};

    for (const ln of spec.lines || []) el('line', {x1: ln.x1, y1: ln.y1, x2: ln.x2, y2: ln.y2, class: ln.cls}, L.lines);
    for (const p of spec.paths || []) {
      const pts = (p.points || []).map(q => [num(q[0]), num(q[1])]);
      if (pts.length < 2) continue;
      tip(el('path', {d: p.smooth ? smoothPath(pts) : 'M' + pts.map(q => q.join(' ')).join(' L'), class: p.cls}, L.paths), p.title);
    }
    const clickable = (g, it) => {
      if (!it.point) return;
      g.setAttribute('class', 'item');
      g.setAttribute('tabindex', '0');
      g.setAttribute('role', 'button');
      g.setAttribute('aria-label', it.title || it.label || 'item');
      g._item = it;
      g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (opts.onPoint) opts.onPoint(it); } });
    };
    for (const r of spec.rects || []) {
      const g = el('g', {}, L.rects);
      el('rect', {x: r.x, y: r.y, width: r.w, height: r.h, class: r.cls, rx: 2}, g);
      tip(g, r.title); clickable(g, r);
    }
    // nodes, and the edges that follow them when one is dragged
    const nodes = {};
    const faces = opts.portraits || {};
    const defs = el('defs', {}, svg);
    let clipN = 0;
    for (const n of spec.nodes || []) {
      const g = el('g', {}, L.nodes);
      const c = el('circle', {cx: num(n.x), cy: num(n.y), r: num(n.r) || 12, class: n.cls}, g);
      if (faces[n.id]) {             // the portrait, cropped to the circle
        const r = num(n.r) || 12, cid = 'face-' + (++clipN) + '-' + Math.random().toString(36).slice(2, 7);
        el('circle', {cx: num(n.x), cy: num(n.y), r: r - 1}, el('clipPath', {id: cid}, defs));
        const img = el('image', {x: num(n.x) - r, y: num(n.y) - r, width: 2 * r, height: 2 * r,
          preserveAspectRatio: 'xMidYMid slice', 'clip-path': 'url(#' + cid + ')'}, g);
        img.setAttribute('href', faces[n.id]);
      }
      const t = textLines(g, num(n.x), num(n.y) + (num(n.r) || 12) + 14, nameLines(n), 'label', 'middle');
      tip(g, n.title || n.label); clickable(g, n);
      nodes[n.id] = {n: n, g: g, c: c, t: t, edges: []};
    }
    const edges = [];
    for (const e of spec.edges || []) {
      const a = nodes[e.from], b = nodes[e.to];
      if (!a || !b) continue;
      const g = el('g', {}, L.edges);
      const line = el('line', {class: e.cls}, g);
      tip(g, e.title || e.label);
      let label = null, pill = null, w = 0;
      if (e.label) {               // labels sit above every line and name, on a pill in the card colour
        const lg = el('g', {class: 'elabel-g'}, L.elabels);
        tip(lg, e.title || e.label);
        const full = String(e.label);
        const text = full.length > EDGE_MAX ? full.slice(0, EDGE_MAX - 3).trim() + '...' : full;
        w = text.length * EDGE_PX + 12;
        pill = el('rect', {class: 'pill', width: w, height: EDGE_H, rx: EDGE_H / 2}, lg);
        label = textLines(lg, 0, 0, [text], 'elabel', 'middle');
      }
      const rec = {a: a, b: b, line: line, label: label, pill: pill, w: w};
      a.edges.push(rec); b.edges.push(rec); edges.push(rec);
    }
    const place = rec => {
      const ax = num(rec.a.n.x), ay = num(rec.a.n.y), bx = num(rec.b.n.x), by = num(rec.b.n.y);
      rec.line.setAttribute('x1', ax); rec.line.setAttribute('y1', ay);
      rec.line.setAttribute('x2', bx); rec.line.setAttribute('y2', by);
    };
    // Each label at its edge's midpoint; when that would cover a name or a label already placed, moved along its
    // own edge to the first clear spot (else left at the midpoint).
    const ALONG = [0.5, 0.38, 0.62, 0.28, 0.72, 0.2, 0.8];
    const labels = () => {
      const taken = Object.values(nodes).map(x => nodeBox(x.n));
      for (const rec of edges) {
        if (!rec.label) continue;
        const ax = num(rec.a.n.x), ay = num(rec.a.n.y), bx = num(rec.b.n.x), by = num(rec.b.n.y);
        const box = t => {
          const x = ax + (bx - ax) * t, y = ay + (by - ay) * t;
          return [x - rec.w / 2, y - EDGE_H / 2, x + rec.w / 2, y + EDGE_H / 2];
        };
        let best = box(0.5);
        for (const t of ALONG) {
          const b = box(t);
          if (!taken.some(o => hits(b, o))) { best = b; break; }
        }
        taken.push(best);
        const mx = (best[0] + best[2]) / 2, my = (best[1] + best[3]) / 2;
        rec.pill.setAttribute('x', best[0]); rec.pill.setAttribute('y', best[1]);
        rec.label.setAttribute('x', mx); rec.label.setAttribute('y', my + 4);
        for (const s of rec.label.childNodes) if (s.setAttribute) s.setAttribute('x', mx);
      }
    };
    edges.forEach(place);
    labels();
    for (const p of spec.pins || []) {
      const g = el('g', {}, L.pins);
      shape(g, p); tip(g, p.title); clickable(g, p);
    }
    for (const t of spec.texts || []) textLines(L.texts, num(t.x), num(t.y), t.lines || [t.text], t.cls, t.anchor, t.rotate);

    // the view: scale 1 is one unit to one pixel; ox, oy is the board point at the top left. "fitted" holds until
    // the reader zooms or pans, and a fitted board fits again when its pane changes size.
    const V = {scale: 1, ox: 0, oy: 0, fitted: true};
    const size = () => ({w: container.clientWidth || 400, h: container.clientHeight || 300});
    const apply = () => {
      const s = size();
      svg.setAttribute('viewBox', [V.ox, V.oy, s.w / V.scale, s.h / V.scale].join(' '));
      svg.classList.toggle('far', V.scale < 0.6);             // edge labels hide when zoomed out
      if (!V.fitted && opts.onView) opts.onView({scale: V.scale, ox: V.ox, oy: V.oy});
    };
    const start = () => {
      const s = size();
      V.scale = 1; V.fitted = false;
      V.ox = W < s.w ? -(s.w - W) / 2 : 0;
      V.oy = H < s.h ? -(s.h - H) / 2 : 0;
      apply();
    };
    const fit = () => {
      const s = size();
      V.scale = Math.min(s.w / (W + 20), s.h / (H + 20), 4);
      V.ox = -(s.w / V.scale - W) / 2;
      V.oy = -(s.h / V.scale - H) / 2;
      V.fitted = true;
      apply();
      if (opts.onView) opts.onView(null);                      // fitted again: nothing to remember
    };
    const zoomAt = (f, mx, my) => {
      const s = size();
      if (mx === undefined) { mx = s.w / 2; my = s.h / 2; }
      const next = Math.min(Math.max(V.scale * f, 0.15), 6);
      const px = V.ox + mx / V.scale, py = V.oy + my / V.scale;
      V.scale = next; V.ox = px - mx / next; V.oy = py - my / next; V.fitted = false;
      apply();
    };
    const was = opts.view;
    if (was && [was.scale, was.ox, was.oy].every(v => typeof v === 'number' && isFinite(v)) && was.scale > 0) {
      V.scale = was.scale; V.ox = was.ox; V.oy = was.oy; V.fitted = false;
      apply();
    } else fit();
    if (window.ResizeObserver) new ResizeObserver(() => (V.fitted ? fit() : apply())).observe(container);

    svg.addEventListener('wheel', e => {
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, e.clientX - r.left, e.clientY - r.top);
    }, {passive: false});

    // one pointer at a time: a press on an item is a click (or a drag on a draggable node), anywhere else a pan
    let drag = null;
    svg.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      let g = e.target;
      while (g && g !== svg && !g._item) g = g.parentNode;
      const item = g && g !== svg ? g._item : null;
      const node = item && spec.draggable ? Object.values(nodes).find(x => x.n === item) : null;
      drag = {x: e.clientX, y: e.clientY, ox: V.ox, oy: V.oy, item: item, node: node, moved: false,
        nx: node ? num(node.n.x) : 0, ny: node ? num(node.n.y) : 0};
      svg.setPointerCapture(e.pointerId);
      if (!item) svg.classList.add('panning');
    });
    svg.addEventListener('pointermove', e => {
      if (!drag) return;
      const dx = (e.clientX - drag.x) / V.scale, dy = (e.clientY - drag.y) / V.scale;
      if (!drag.moved && Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 5) return;
      drag.moved = true;
      if (drag.node) {
        const n = drag.node.n;
        n.x = Math.round(drag.nx + dx); n.y = Math.round(drag.ny + dy);
        drag.node.c.setAttribute('cx', n.x); drag.node.c.setAttribute('cy', n.y);
        drag.node.t.setAttribute('x', n.x); drag.node.t.setAttribute('y', n.y + (num(n.r) || 12) + 14);
        for (const s of drag.node.t.childNodes) if (s.setAttribute) s.setAttribute('x', n.x);
        drag.node.edges.forEach(place);
        labels();
      } else if (!drag.item) {
        V.ox = drag.ox - dx; V.oy = drag.oy - dy; V.fitted = false; apply();
      }
    });
    const end = e => {
      if (!drag) return;
      const d = drag; drag = null;
      svg.classList.remove('panning');
      try { svg.releasePointerCapture(e.pointerId); } catch (x) { /* already released */ }
      if (e.type !== 'pointerup') return;
      if (d.item && !d.moved && opts.onPoint) opts.onPoint(d.item);
      else if (d.node && d.moved && opts.onMoved) opts.onMoved();
    };
    svg.addEventListener('pointerup', end);
    svg.addEventListener('pointercancel', end);

    const positions = () => {
      const out = {};
      for (const id in nodes) out[id] = [num(nodes[id].n.x), num(nodes[id].n.y)];
      return out;
    };

    // The whole board as a PNG: a copy of the SVG with its colors written in, drawn on a canvas. Edge labels are
    // in the picture at any zoom.
    const STYLE = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-linejoin', 'opacity',
      'font-family', 'font-size', 'font-weight', 'paint-order'];
    function png(name) {
      return new Promise((resolve, reject) => {
        const copy = svg.cloneNode(true);
        const from = svg.querySelectorAll('*'), to = copy.querySelectorAll('*');
        for (let i = 0; i < from.length; i++) {
          if (from[i].tagName === 'title') continue;
          const cs = getComputedStyle(from[i]);
          to[i].setAttribute('style', STYLE.map(k => k + ':' + cs.getPropertyValue(k)).join(';'));
        }
        const bg = getComputedStyle(document.body).backgroundColor || '#ffffff';
        copy.setAttribute('width', W); copy.setAttribute('height', H);
        copy.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
        copy.removeAttribute('class');
        const back = document.createElementNS(NS, 'rect');
        back.setAttribute('width', W); back.setAttribute('height', H); back.setAttribute('fill', bg);
        copy.insertBefore(back, copy.firstChild);
        const xml = new XMLSerializer().serializeToString(copy);
        const img = new Image();
        img.onload = () => {
          const k = Math.max(1, Math.min(2, 8000 / Math.max(W, H)));
          const cv = document.createElement('canvas');
          cv.width = Math.round(W * k); cv.height = Math.round(H * k);
          const ctx = cv.getContext('2d');
          ctx.scale(k, k);
          ctx.drawImage(img, 0, 0, W, H);
          cv.toBlob(blob => {
            if (!blob) { reject(new Error('The browser could not make the picture.')); return; }
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = (name || 'board').replace(/[\\/:*?"<>|]+/g, ' ').trim() + '.png';
            document.body.appendChild(a); a.click(); a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 4000);
            resolve();
          }, 'image/png');
        };
        img.onerror = () => reject(new Error('The browser could not make the picture.'));
        img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml);
      });
    }

    return {fit: fit, reset: start, zoom: f => zoomAt(f), positions: positions, png: png};
  }

  window.MapsBoards = {draw: draw};
})();
