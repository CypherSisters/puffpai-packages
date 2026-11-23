/* Editor's Desk, the Write view: her whole book as one page she types in.

   How it is built. One contenteditable root (#doc) holds every section in order. Each section is a <div class="sec"> with a
   head that is not editable (the chapter heading, Earlier versions, bars and notes not found) and a <div class="body">
   with one <p> for each line of the section's text. One root means the arrow keys and the caret cross a seam as in Word.
   An edit can never reach two sections: every beforeinput, paste and drop whose range does not lie inside one body is
   refused, so a seam can never be deleted or typed over.

   Saving. A MutationObserver notes which bodies changed. Only those are written back: each <p> becomes one line again
   (italic and bold as *, ** and ***, the same grammar as marks.py), and a paragraph whose words and marks are as they were
   gives back its original line byte for byte. A section whose text comes out as it was is not saved at all. Highlights
   use the CSS Highlight registry, so they never touch the text.

   The server sends data, never markup: everything here is built with text nodes. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);

  function el(tag, attrs, kids) {
    const n = document.createElement(tag);
    for (const k in attrs || {}) {
      if (k === 'on') for (const e in attrs.on) n.addEventListener(e, attrs.on[e]);
      else if (k === 'text') n.textContent = attrs.text;
      else if (k === 'cls') n.className = attrs.cls;
      else if (attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== false) n.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    }
    for (const c of kids || []) if (c) n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    return n;
  }
  const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const q = o => Object.keys(o).map(k => encodeURIComponent(k) + '=' + encodeURIComponent(o[k])).join('&');
  async function call(path, body, keep) {
    let r;
    try {
      r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), keepalive: !!keep});
    } catch (e) {
      const err = new Error('I could not reach Editor\'s Desk');
      err.offline = true;
      throw err;
    }
    let got = {};
    try { got = await r.json(); } catch (e) { got = {}; }
    if (!r.ok) {
      const err = new Error(got.error || 'That did not work');
      err.data = got;
      throw err;
    }
    return got;
  }
  const hm = d => d.toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
  const when = iso => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

  // ---------------------------------------------------------------- marks: marks.py, line for line

  const MARK = /(\*{1,3})(?=[^\s*])([^*\n]*?[^\s*]|[^\s*])\1(?!\*)/g;
  const BREAK = /^[\s*]*$/;

  function runs(line) {
    if (BREAK.test(line)) return line ? [[line, false, false]] : [];
    const out = [];
    let at = 0, m;
    MARK.lastIndex = 0;
    while ((m = MARK.exec(line)) !== null) {
      if (m.index > at) out.push([line.slice(at, m.index), false, false]);
      const n = m[1].length;
      out.push([m[2], n === 1 || n === 3, n === 2 || n === 3]);
      at = m.index + m[0].length;
    }
    if (at < line.length) out.push([line.slice(at), false, false]);
    return out;
  }
  function stripMarks(text) {
    return text.split('\n').map(line => runs(line).map(r => r[0]).join('')).join('\n');
  }
  function merged(pieces) {
    const out = [];
    for (const [words, italic, bold] of pieces) {
      if (!words) continue;
      const last = out[out.length - 1];
      if (last && last[1] === italic && last[2] === bold) last[0] += words;
      else out.push([words, italic, bold]);
    }
    return out;
  }
  function wrap(pieces) {
    const out = [];
    for (const [words, italic, bold] of merged(pieces)) {
      const stars = '*'.repeat((italic ? 1 : 0) + (bold ? 2 : 0));
      const core = words.trim();
      if (!stars || !core || core.includes('*')) { out.push(words); continue; }
      const lead = words.slice(0, words.length - words.trimStart().length);
      const tail = words.slice(words.trimEnd().length);
      out.push(lead + stars + core + stars + tail);
    }
    return out.join('');
  }
  const samePieces = (a, b) => JSON.stringify(merged(a)) === JSON.stringify(merged(b));

  // ---------------------------------------------------------------- state

  const W = {
    title: null, secs: [], byN: {}, loaded: false, findings: [], asks: [], status: null, notes: true,
    timer: 0, retry: 0, retryWait: 5000, quiet: false, focus: null, cards: [], lastSaved: null, hooks: {},
  };
  const doc = $('doc'), margin = $('margin'), sheet = $('sheet'), scroller = $('wscroll');
  const narrow = window.matchMedia('(max-width: 1099px)');
  const HL = !!(window.CSS && CSS.highlights && window.Highlight);

  // ---------------------------------------------------------------- drawing the book

  function paraEl(line) {
    const p = document.createElement('p');
    p._orig = line;
    if (line && BREAK.test(line)) {
      p.className = 'brk';
      p.textContent = line;
      return p;
    }
    for (const [words, italic, bold] of runs(line)) {
      let node = document.createTextNode(words);
      if (bold) { const b = document.createElement('b'); b.appendChild(node); node = b; }
      if (italic) { const i = document.createElement('i'); i.appendChild(node); node = i; }
      p.appendChild(node);
    }
    if (!p.firstChild) p.appendChild(document.createElement('br'));
    return p;
  }
  function fillBody(body, text) {
    clear(body);
    for (const line of text.split('\n')) body.appendChild(paraEl(line));
  }
  const PART = /^(.*?),\s*part\s+(\d+)\s*$/i;
  function chapterOf(heading) {
    const m = PART.exec(heading || '');
    return m ? m[1] : (heading || '');
  }

  function quietly(fn) {
    W.quiet = true;
    try { fn(); } finally { watcher.takeRecords(); W.quiet = false; }
  }

  function draw(sections) {
    W.secs = [];
    W.byN = {};
    quietly(() => {
      clear(doc);
      let chapter = null;
      for (const s of sections) {
        const ch = chapterOf(s.heading);
        const first = !PART.test(s.heading || '') || ch !== chapter;
        chapter = ch;
        const head = el('div', {cls: 'shead' + (first ? ' chap' : ''), contenteditable: 'false'});
        const body = el('div', {cls: 'body'});
        const box = el('div', {cls: 'sec', 'data-n': String(s.n)}, [head, body]);
        const sec = {n: s.n, heading: s.heading, chapter: ch, first, sha: s.sha, saved: s.text, versions: s.versions, words: s.words,
          unread: s.unread, el: box, head, body, touched: false, saving: false, again: false, err: '', stale: null, lost: []};
        fillBody(body, s.text);
        drawHead(sec);
        doc.appendChild(box);
        W.secs.push(sec);
        W.byN[s.n] = sec;
      }
    });
    drawToc();
  }

  function drawHead(sec) {
    const head = clear(sec.head);
    if (sec.first) {
      head.appendChild(el('h2', {cls: 'chead', text: sec.chapter || 'Untitled'}));
      head.appendChild(el('button', {type: 'button', cls: 'vbtn', text: 'Earlier versions', 'aria-expanded': String(!!sec.vbox),
        on: {click: e => toggleVersions(sec, e.currentTarget)}}));
      if (sec.vbox) head.appendChild(sec.vbox);
    }
    if (sec.stale) head.appendChild(staleBar(sec));
    if (sec.lost.length && W.notes) {
      const list = el('div', {cls: 'lost'}, [el('span', {cls: 'lab', text: plural(sec.lost.length, 'note') + ' not found in the text:'})]);
      for (const c of sec.lost) list.appendChild(el('button', {type: 'button', cls: 'link', text: c.title, on: {click: () => focusCard(c, true)}}));
      head.appendChild(list);
    }
    head.hidden = !head.firstChild;
  }

  // ---------------------------------------------------------------- chapters list

  function groups() {
    const out = [];
    for (const s of W.secs) {
      if (s.first || !out.length) out.push({name: s.chapter || 'Untitled', secs: [s]});
      else out[out.length - 1].secs.push(s);
    }
    return out;
  }
  function drawToc() {
    const toc = clear($('toc'));
    const gs = groups();
    const total = W.secs.reduce((a, s) => a + (s.words || 0), 0);
    toc.appendChild(el('div', {cls: 'ttotal', text: total.toLocaleString() + ' words'}));
    const list = el('ol', {cls: 'tlist'});
    for (const g of gs) {
      const words = g.secs.reduce((a, s) => a + (s.words || 0), 0);
      const row = el('li', {}, [el('button', {type: 'button', on: {click: () => { scrollToSec(g.secs[0]); closeToc(); }}},
        [el('span', {cls: 'tname', text: g.name}), el('span', {cls: 'twords', text: words.toLocaleString()})])]);
      g.row = row;
      list.appendChild(row);
    }
    toc.appendChild(list);
    W.groups = gs;
    markCurrent();
  }
  function scrollToSec(sec) {
    const top = sec.el.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 8;
    scroller.scrollTo({top: Math.max(0, top)});
  }
  function markCurrent() {
    if (!W.groups) return;
    const line = scroller.getBoundingClientRect().top + 90;
    let cur = W.groups[0];
    for (const g of W.groups) if (g.secs[0].el.getBoundingClientRect().top <= line) cur = g;
    for (const g of W.groups) g.row.classList.toggle('on', g === cur);
    if (cur && cur.row) cur.row.setAttribute('aria-current', 'true');
    for (const g of W.groups) if (g !== cur) g.row.removeAttribute('aria-current');
  }
  function closeToc() { $('toc').classList.remove('open'); $('tocbtn').setAttribute('aria-expanded', 'false'); }
  $('tocbtn').addEventListener('click', () => {
    const open = $('toc').classList.toggle('open');
    $('tocbtn').setAttribute('aria-expanded', String(open));
  });
  let ticking = false;
  scroller.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; markCurrent(); });
  });

  // ---------------------------------------------------------------- reading the page back into text

  const BLOCKS = new Set(['P', 'DIV', 'LI', 'UL', 'OL', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'BLOCKQUOTE', 'PRE', 'SECTION', 'ARTICLE']);

  function linesOf(node) {
    const lines = [[]];
    const walk = (n, it, bo) => {
      for (const c of n.childNodes) {
        if (c.nodeType === 3) { if (c.data) lines[lines.length - 1].push([c.data, it, bo]); continue; }
        if (c.nodeType !== 1) continue;
        if (c.tagName === 'BR') { lines.push([]); continue; }
        if (c.getAttribute('contenteditable') === 'false') continue;
        const t = c.tagName, st = c.style;
        const i2 = it || t === 'I' || t === 'EM' || st.fontStyle === 'italic';
        const b2 = bo || t === 'B' || t === 'STRONG' || st.fontWeight === 'bold' || st.fontWeight === 'bolder' || Number(st.fontWeight) >= 600;
        const block = BLOCKS.has(t);
        if (block && lines[lines.length - 1].length) lines.push([]);
        walk(c, i2, b2);
        if (block) lines.push([]);
      }
    };
    walk(node, false, false);
    if (lines.length > 1 && !lines[lines.length - 1].length) lines.pop();
    return lines;
  }
  function lineText(pieces, orig) {
    if (orig !== undefined && samePieces(pieces, runs(orig))) return orig;
    const plain = pieces.map(x => x[0]).join('');
    if (plain && BREAK.test(plain)) return plain;            // a scene break, as typed
    return wrap(pieces.map(([w, i, b]) => [w.replace(/\u00a0/g, ' ').replace(/\n/g, ' '), i, b]));
  }
  function serialize(sec) {
    const out = [];
    for (const c of sec.body.childNodes) {
      if (c.nodeType === 3) { if (c.data) out.push(lineText([[c.data, false, false]])); continue; }
      if (c.nodeType !== 1) continue;
      if (c.tagName === 'BR') { out.push(''); continue; }
      const lines = linesOf(c);
      if (lines.length === 1) out.push(lineText(lines[0], c._orig));
      else for (const l of lines) out.push(lineText(l));
    }
    return out.join('\n');
  }

  // ---------------------------------------------------------------- one section per edit

  const bodyOf = node => {
    const e = node && (node.nodeType === 1 ? node : node.parentElement);
    return e && doc.contains(e) ? e.closest('.body') : null;
  };
  const secOfNode = node => { const b = bodyOf(node); return b ? W.byN[b.parentElement.dataset.n] : null; };
  function rangeNow(e) {
    const rs = e && e.getTargetRanges ? e.getTargetRanges() : [];
    if (rs.length) return rs[0];
    const sel = document.getSelection();
    return sel.rangeCount ? sel.getRangeAt(0) : null;
  }
  function oneBody(r) {
    if (!r) return null;
    const a = bodyOf(r.startContainer), b = bodyOf(r.endContainer);
    return a && a === b ? a : null;
  }
  doc.addEventListener('beforeinput', e => {
    if (e.inputType === 'historyUndo' || e.inputType === 'historyRedo') return;
    if (!oneBody(rangeNow(e))) {
      e.preventDefault();
      flash('That reaches across two parts of your book. Change one part at a time.');
    }
  });
  doc.addEventListener('dragstart', e => e.preventDefault());
  doc.addEventListener('drop', e => e.preventDefault());

  // Paste comes in as plain text, keeping italic and bold.
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function pastedLines(html, text) {
    if (!html) return text.replace(/\r\n?/g, '\n').split('\n').map(l => [[l, false, false]]);
    const d = new DOMParser().parseFromString(html, 'text/html');      // inert: nothing in it runs
    for (const x of d.querySelectorAll('script, style, title, meta, head')) x.remove();
    const lines = [[]];
    const walk = (n, it, bo) => {
      for (const c of n.childNodes) {
        if (c.nodeType === 3) {
          const t = c.data.replace(/[\r\n\t ]+/g, ' ');
          if (t.trim() || lines[lines.length - 1].length) lines[lines.length - 1].push([t, it, bo]);
          continue;
        }
        if (c.nodeType !== 1) continue;
        if (c.tagName === 'BR') { lines.push([]); continue; }
        const t = c.tagName, st = c.style || {};
        const i2 = it || t === 'I' || t === 'EM' || st.fontStyle === 'italic';
        const b2 = bo || t === 'B' || t === 'STRONG' || st.fontWeight === 'bold' || Number(st.fontWeight) >= 600;
        const block = BLOCKS.has(t) || t === 'TR' || t === 'TABLE';
        if (block && lines[lines.length - 1].length) lines.push([]);
        walk(c, i2, b2);
        if (block) lines.push([]);
      }
    };
    walk(d.body, false, false);
    while (lines.length > 1 && !lines[lines.length - 1].length) lines.pop();
    while (lines.length > 1 && !lines[0].length) lines.shift();
    return lines.map(l => { if (l.length) { l[0][0] = l[0][0].replace(/^ +/, ''); l[l.length - 1][0] = l[l.length - 1][0].replace(/ +$/, ''); } return l; });
  }
  doc.addEventListener('paste', e => {
    e.preventDefault();
    if (!oneBody(rangeNow(null))) { flash('Click where the words should go, inside one part of your book.'); return; }
    const cd = e.clipboardData;
    const lines = pastedLines(cd.getData('text/html'), cd.getData('text/plain'));
    const html = lines.map(l => l.map(([w, i, b]) => {
      let s = esc(w);
      if (b) s = '<b>' + s + '</b>';
      if (i) s = '<i>' + s + '</i>';
      return s;
    }).join('')).join('<br>');
    document.execCommand('insertHTML', false, html || '');
  });

  // Ctrl+I, Ctrl+B, Ctrl+S, Ctrl+F
  function format(cmd) {
    if (!oneBody(rangeNow(null))) { flash('Select words inside one part of your book first.'); return; }
    document.execCommand(cmd, false, null);
  }
  for (const [id, cmd] of [['italic', 'italic'], ['bold', 'bold']]) {
    $(id).addEventListener('mousedown', e => e.preventDefault());      // keep her selection
    $(id).addEventListener('click', () => format(cmd));
  }
  document.addEventListener('keydown', e => {
    if ($('v-write').hidden || !(e.ctrlKey || e.metaKey) || e.altKey) return;
    const k = e.key.toLowerCase();
    if (k === 's') { e.preventDefault(); flush(false); }
    else if (k === 'f') { e.preventDefault(); openFind(); }
    else if ((k === 'i' || k === 'b') && doc.contains(document.activeElement)) {
      e.preventDefault();
      format(k === 'i' ? 'italic' : 'bold');
    }
  });

  // ---------------------------------------------------------------- what changed, and saving it

  const watcher = new MutationObserver(list => {
    if (W.quiet) return;
    let any = false;
    for (const m of list) {
      const sec = secOfNode(m.target);
      if (sec) { sec.touched = true; any = true; }
    }
    if (any) { fixParas(); schedule(); say(); relayoutSoon(); }
  });
  watcher.observe(doc, {subtree: true, childList: true, characterData: true});

  function fixParas() {
    // the paragraph she is in, and the one before: a scene break is centered, anything else is not; an empty body gets a line
    const sel = document.getSelection();
    const node = sel.rangeCount ? sel.getRangeAt(0).startContainer : null;
    const b = bodyOf(node);
    if (!b) return;
    if (!b.firstChild) { b.appendChild(el('p', {}, [el('br')])); return; }
    let p = node.nodeType === 1 ? node : node.parentElement;
    p = p && p.closest('.body > *');
    for (const x of [p, p && p.previousElementSibling, p && p.nextElementSibling]) {
      if (!x || x.tagName !== 'P') continue;
      const t = x.textContent;
      const brk = !!t && BREAK.test(t);
      if (x.classList.contains('brk') !== brk) x.classList.toggle('brk', brk);
    }
  }

  function schedule() {
    clearTimeout(W.timer);
    W.timer = setTimeout(() => flush(true), 2000);
  }

  async function flush(auto, keep) {
    clearTimeout(W.timer);
    const jobs = [];
    for (const sec of W.secs) {
      if (!sec.touched || sec.stale) continue;
      if (sec.saving) { sec.again = true; continue; }
      const text = serialize(sec);
      if (text === sec.saved) { sec.touched = false; continue; }
      jobs.push(saveSec(sec, text, auto, keep));
    }
    await Promise.all(jobs);
    say();
  }

  async function saveSec(sec, text, auto, keep, sha) {
    sec.saving = true;
    say();
    try {
      const got = await call('api/save', {title: W.title, n: sec.n, text, sha: sha || sec.sha, auto: !!auto}, keep);
      sec.sha = got.sha;
      sec.saved = text;
      sec.err = '';
      sec.stale = null;
      sec.words = got.words;
      sec.versions = (got.versions || []).length;
      W.lastSaved = new Date();
      W.retryWait = 5000;
      sec.touched = serialize(sec) !== text;            // she typed on while it saved
      drawTocWords();
      drawHead(sec);
    } catch (e) {
      if (e.data && e.data.stale) {
        sec.stale = {sha: e.data.sha, text: e.data.text};
        sec.err = 'this part changed somewhere else';
        drawHead(sec);
      } else {
        sec.err = e.offline ? 'I could not reach Editor\'s Desk' : e.message.replace(/[.\s]+$/, '');
        clearTimeout(W.retry);
        W.retry = setTimeout(() => flush(true), W.retryWait);
        W.retryWait = Math.min(W.retryWait * 2, 60000);
      }
    } finally {
      sec.saving = false;
      if (sec.again) { sec.again = false; if (sec.touched) schedule(); }
      say();
    }
  }

  function drawTocWords() {
    if (!W.groups) return;
    const total = W.secs.reduce((a, s) => a + (s.words || 0), 0);
    const t = $('toc').querySelector('.ttotal');
    if (t) t.textContent = total.toLocaleString() + ' words';
    for (const g of W.groups) {
      const w = g.row.querySelector('.twords');
      if (w) w.textContent = g.secs.reduce((a, s) => a + (s.words || 0), 0).toLocaleString();
    }
    if (W.hooks.words) W.hooks.words(total);
  }

  let flashing = 0;
  function flash(line) {
    const n = $('wstatus');
    n.textContent = line;
    n.className = 'wstatus warn';
    clearTimeout(flashing);
    flashing = setTimeout(say, 4000);
  }
  function say() {
    const n = $('wstatus');
    const bad = W.secs.filter(s => s.err);
    if (bad.length) {
      n.textContent = 'Not saved: ' + bad[0].err + '. Your words are still here.';
      n.className = 'wstatus bad';
    } else if (W.secs.some(s => s.saving)) {
      n.textContent = 'Saving...';
      n.className = 'wstatus';
    } else if (W.secs.some(s => s.touched)) {
      n.textContent = 'Not saved yet';
      n.className = 'wstatus';
    } else {
      n.textContent = W.lastSaved ? 'Saved ' + hm(W.lastSaved) : (W.loaded ? 'All saved' : '');
      n.className = 'wstatus';
    }
  }
  const unsaved = () => W.secs.some(s => s.touched || s.saving || s.err);

  doc.addEventListener('focusout', () => flush(true));
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(true, true); });
  window.addEventListener('pagehide', () => flush(true, true));
  window.addEventListener('beforeunload', e => { if (unsaved()) { flush(true, true); e.preventDefault(); e.returnValue = ''; } });

  // When the section changed somewhere else: never lose her words.
  function staleBar(sec) {
    const other = el('div', {cls: 'other', hidden: true});
    const bar = el('div', {cls: 'stalebar', role: 'alert'}, [
      el('p', {text: 'This part changed somewhere else, so your words here are not saved yet. They are still on the page.'}),
      el('div', {cls: 'row'}, [
        el('button', {type: 'button', cls: 'main', text: 'Keep mine', on: {click: async () => {
          const st = sec.stale;
          sec.stale = null;
          sec.err = '';
          await saveSec(sec, serialize(sec), false, false, st.sha);
          if (sec.err) drawHead(sec);
        }}}),
        el('button', {type: 'button', text: 'Show the other', on: {click: e => {
          other.hidden = !other.hidden;
          e.currentTarget.textContent = other.hidden ? 'Show the other' : 'Hide the other';
        }}}),
      ]),
      other,
    ]);
    other.appendChild(el('p', {cls: 'fine', text: 'The other text. Copy what you want from it. Keep mine puts it in Earlier versions.'}));
    other.appendChild(readOnly(sec.stale.text));
    return bar;
  }
  function readOnly(text) {
    const box = el('div', {cls: 'ro'});
    for (const line of text.split('\n')) box.appendChild(paraEl(line));
    return box;
  }

  // ---------------------------------------------------------------- earlier versions

  async function toggleVersions(sec, btn) {
    if (sec.vbox) {
      sec.vbox.remove();
      sec.vbox = null;
      btn.setAttribute('aria-expanded', 'false');
      return;
    }
    const box = el('div', {cls: 'vbox'});
    sec.vbox = box;
    btn.setAttribute('aria-expanded', 'true');
    sec.head.appendChild(box);
    const group = (W.groups || []).find(g => g.secs[0] === sec);
    const parts = group ? group.secs : [sec];          // a chapter's versions, part by part
    for (const part of parts) {
      let got;
      try { got = await call('api/section?' + q({title: W.title, n: part.n})); } catch (e) { box.appendChild(el('p', {cls: 'fine', text: e.message})); continue; }
      if (parts.length > 1) box.appendChild(el('h4', {text: part.heading}));
      if (!got.versions.length) { box.appendChild(el('p', {cls: 'fine', text: 'No earlier versions yet.'})); continue; }
      const list = el('ul', {});
      for (const v of got.versions) {
        list.appendChild(el('li', {}, [el('button', {type: 'button', cls: 'link', text: when(v.saved_at), on: {click: () => showVersion(part, v, box)}}),
          el('span', {cls: 'fine', text: plural(v.words, 'word') + (v.by && v.by !== 'you' ? ', before ' + v.by : '')})]));
      }
      box.appendChild(list);
    }
  }
  async function showVersion(part, v, box) {
    let got;
    try { got = await call('api/version?' + q({title: W.title, n: part.n, id: v.id})); } catch (e) { flash(e.message); return; }
    const old = box.querySelector('.vshow');
    if (old) old.remove();
    const show = el('div', {cls: 'vshow'}, [
      el('p', {cls: 'fine', text: 'As it was before ' + when(v.saved_at) + ':'}),
      readOnly(got.text),
      el('div', {cls: 'row'}, [
        el('button', {type: 'button', cls: 'main', text: 'Bring this back', on: {click: () => restore(part, v)}}),
        el('button', {type: 'button', text: 'Close', on: {click: () => show.remove()}}),
      ]),
    ]);
    box.appendChild(show);
  }
  async function restore(sec, v) {
    await flush(false);
    if (sec.touched || sec.err || sec.stale) { flash('Your latest words here are not saved yet, so nothing was brought back.'); return; }
    try {
      const got = await call('api/restore', {title: W.title, n: sec.n, id: v.id, sha: sec.sha});
      quietly(() => fillBody(sec.body, got.text));
      sec.sha = got.sha;
      sec.saved = got.text;
      sec.words = got.words;
      sec.touched = false;
      if (sec.vbox) { sec.vbox.remove(); sec.vbox = null; }
      drawHead(sec);
      drawTocWords();
      flash(got.message);
      anchorAll();
    } catch (e) { flash(e.message); }
  }

  // ---------------------------------------------------------------- finding words in the page

  const FOLD = {'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"', '\u2014': '-', '\u2013': '-'};
  function fold(text) {
    // one spacing, one kind of quote mark, lowercase: as _folded in book.py. where[i] is the place in text of folded char i.
    let out = '';
    const where = [];
    let space = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (/\s/.test(ch)) {
        if (!space && out) { out += ' '; where.push(i); }
        space = true;
        continue;
      }
      space = false;
      out += (FOLD[ch] || ch).toLowerCase()[0];
      where.push(i);
    }
    return {out, where};
  }
  function indexSec(sec) {
    // the body's words, paragraphs joined by a newline, and which text node each part sits in
    const nodes = [];
    let text = '';
    for (const blk of sec.body.childNodes) {
      const walker = document.createTreeWalker(blk, NodeFilter.SHOW_TEXT);
      if (blk.nodeType === 3) { nodes.push({node: blk, at: text.length}); text += blk.data; }
      else for (let n = walker.nextNode(); n; n = walker.nextNode()) { nodes.push({node: n, at: text.length}); text += n.data; }
      text += '\n';
    }
    const f = fold(text);
    return {text, nodes, folded: f.out, where: f.where};
  }
  function point(ix, c, end) {
    // character c of the flat text as (node, offset); an end point sits after its character
    let lo = 0, hi = ix.nodes.length - 1, best = 0;
    const want = end ? c - 1 : c;
    while (lo <= hi) { const mid = (lo + hi) >> 1; if (ix.nodes[mid].at <= want) { best = mid; lo = mid + 1; } else hi = mid - 1; }
    const x = ix.nodes[best];
    return [x.node, Math.min(x.node.data.length, Math.max(0, c - x.at))];
  }
  function rangeOf(ix, i, len) {
    const a = ix.where[i], z = ix.where[i + len - 1] + 1;
    const r = document.createRange();
    const s = point(ix, a, false), e = point(ix, z, true);
    r.setStart(s[0], s[1]);
    r.setEnd(e[0], e[1]);
    return r;
  }
  function foldQuote(quote) {
    return fold(stripMarks(String(quote || '').trim().replace(/^["\u201c\u201d]+|["\u201c\u201d]+$/g, ''))).out.trim();
  }
  function findIn(sec, quote, ix) {
    const want = foldQuote(quote);
    if (!want || !sec) return null;
    ix = ix || indexSec(sec);
    const i = ix.folded.indexOf(want);
    return i < 0 ? null : rangeOf(ix, i, want.length);
  }

  // ---------------------------------------------------------------- highlights

  function paint() {
    if (!HL) return;
    const hl = CSS.highlights;
    for (const name of ['note', 'note-big', 'note-q', 'note-on', 'asked']) hl.delete(name);
    if (!W.notes) return;
    const sets = {note: [], 'note-big': [], 'note-q': [], 'note-on': [], asked: []};
    for (const c of W.cards) {
      for (const r of c.ranges) sets[c.kind === 'ask' ? 'asked' : c.big ? 'note-big' : c.quiet ? 'note-q' : 'note'].push(r);
      if (c === W.focus) for (const r of c.ranges) sets['note-on'].push(r);
    }
    for (const name in sets) if (sets[name].length) hl.set(name, new Highlight(...sets[name]));
  }

  // ---------------------------------------------------------------- notes in the margin

  const SIZE = {big: 'Big', medium: 'Worth a look', small: 'Small'};

  function anchorAll() {
    if (!W.loaded) return;
    const drafts = W.cards.filter(c => c.id === 'draft');      // a question she is writing stays as it is
    for (const c of W.cards) if (c.node && c.id !== 'draft') c.node.remove();
    W.cards = drafts;
    for (const s of W.secs) s.lost = [];
    const ix = {};
    const index = sec => ix[sec.n] || (ix[sec.n] = indexSec(sec));
    for (const f of W.findings) {
      if (f.state !== 'open') continue;
      const c = {kind: 'finding', id: f.id, f, title: f.what, big: f.size === 'big' && f.kind !== 'question', quiet: f.kind === 'question', ranges: []};
      let home = null;
      for (const w of f.where || []) {
        const sec = W.byN[w.section];
        if (!sec) continue;
        home = home || sec;
        const r = w.quote ? findIn(sec, w.quote, index(sec)) : null;
        if (r) c.ranges.push(r);
      }
      if (!home) continue;
      c.sec = home;
      if (!c.ranges.length) home.lost.push(c);
      c.node = findingCard(c);
      W.cards.push(c);
    }
    for (const a of W.asks) addAskCard(a, index);
    for (const s of W.secs) drawHead(s);
    if (W.focus) W.focus = W.cards.find(c => c.id === W.focus.id) || null;
    if (W.focus) W.focus.node.classList.add('on');
    paint();
    layout();
  }

  function addAskCard(a, index) {
    const sec = W.byN[a.n];
    if (!sec) return null;
    const r = findIn(sec, a.quote, index ? index(sec) : null);
    const c = {kind: 'ask', id: a.id, a, title: 'Your question: ' + (a.question || ''), ranges: r ? [r] : [], sec};
    if (!r) sec.lost.push(c);
    c.node = askCard(c);
    W.cards.push(c);
    return c;
  }

  function findingCard(c) {
    const f = c.f;
    const card = el('article', {cls: 'card' + (c.big ? ' big' : '') + (c.quiet ? ' quiet' : ''), tabindex: '-1'}, [
      el('div', {cls: 'chead'}, [el('span', {cls: 'size', text: c.quiet ? 'A question' : SIZE[f.size] || ''}), el('span', {text: f.check_name || ''}),
        el('button', {type: 'button', cls: 'x', text: 'Close', on: {click: e => { e.stopPropagation(); unfocus(); }}})]),
      el('div', {cls: 'what', text: f.what}),
    ]);
    if (f.why) card.appendChild(el('p', {}, [el('span', {cls: 'lab', text: 'Why it matters'}), f.why]));
    if (f.fix) card.appendChild(el('p', {}, [el('span', {cls: 'lab', text: 'One way to fix it'}), f.fix]));
    if (f.kind === 'question' && f.reason) card.appendChild(el('p', {cls: 'fine', text: f.reason}));
    if (f.changed) card.appendChild(el('p', {cls: 'fine', text: 'This part changed since your editor found this.'}));
    const marks = el('div', {cls: 'marks'});
    for (const [id, label] of [['fixed', 'Fixed'], ['not_a_problem', 'Not a problem'], ['later', 'Later']]) {
      marks.appendChild(el('button', {type: 'button', text: label, on: {click: e => { e.stopPropagation(); mark(f, {state: id}); }}}));
    }
    card.appendChild(marks);
    const note = el('textarea', {cls: 'mynote', placeholder: 'Your note', 'aria-label': 'Your note', rows: '2',
      on: {change: e => mark(f, {note: e.target.value}, true)}});
    note.value = f.note || '';
    card.appendChild(note);
    card.addEventListener('click', () => focusCard(c, false));
    return card;
  }
  async function mark(f, change, quiet) {
    try {
      await call('api/mark', Object.assign({title: W.title, id: f.id}, change));
      if (change.note !== undefined) f.note = change.note;
      if (change.state) {
        f.state = change.state;
        if (W.focus && W.focus.id === f.id) W.focus = null;
        anchorAll();
      }
      if (!quiet && W.hooks.changed) W.hooks.changed();
    } catch (e) { flash(e.message); }
  }

  function askCard(c) {
    const a = c.a;
    const card = el('article', {cls: 'card ask', tabindex: '-1'});
    const head = el('div', {cls: 'chead'}, [el('span', {cls: 'size', text: 'You asked ' + staffName(a.who || 'chief')}),
      el('button', {type: 'button', cls: 'x', text: 'Close', on: {click: e => { e.stopPropagation(); unfocus(); }}})]);
    card.appendChild(head);
    if (a.question) card.appendChild(el('div', {cls: 'what', text: a.question}));
    if (a.state === 'waiting') {
      card.appendChild(el('p', {cls: 'wait', text: 'Your editor is reading...'}));
      const st = W.status;
      if (st && st.state === 'running') card.appendChild(el('p', {cls: 'fine', text: (st.check_name || 'A check') + ' is running. Your editor answers after the step it is on.'}));
    } else {
      const ans = el('div', {cls: 'answer' + (a.state === 'failed' ? ' bad' : '')});
      for (const para of String(a.answer || '').split(/\n\s*\n/)) if (para.trim()) ans.appendChild(el('p', {text: para.trim()}));
      card.appendChild(ans);
    }
    if (a.id) {
      card.appendChild(el('div', {cls: 'marks'}, [el('button', {type: 'button', text: 'Remove', on: {click: async e => {
        e.stopPropagation();
        try { await call('api/asks', {title: W.title, id: a.id}); } catch (err) { flash(err.message); return; }
        W.asks = W.asks.filter(x => x.id !== a.id);
        if (W.focus && W.focus.id === a.id) W.focus = null;
        anchorAll();
      }}})]));
    }
    card.addEventListener('click', () => focusCard(c, false));
    return card;
  }

  // who she can ask, from the desk's staff list; before it comes, only the editor-in-chief
  const ASKABLE = ['chief', 'story', 'facts', 'line', 'copy'];
  const staffName = id => ((W.staff || []).find(s => s.id === id) || {}).name || (id === 'chief' ? 'Dewey' : 'your editor');

  // a card for a new question, at her selection
  function draftCard(range, sec) {
    const c = {kind: 'ask', id: 'draft', ranges: [range], sec, title: 'Your question'};
    const box = el('textarea', {cls: 'mynote', rows: '2', placeholder: 'Ask about these words', 'aria-label': 'Your question'});
    const askable = (W.staff || []).filter(s => ASKABLE.includes(s.id));
    const pick = el('select', {cls: 'askwho', id: 'askwho'}, (askable.length ? askable : [{id: 'chief', name: 'Dewey'}])
      .map(s => el('option', {value: s.id, text: s.name})));
    const send = question => {
      const qn = (question || box.value).trim();
      if (!qn) { box.focus(); return; }
      ask(c, range, sec, qn, pick.value || 'chief');
    };
    const card = el('article', {cls: 'card ask', tabindex: '-1'}, [
      el('div', {cls: 'chead'}, [el('span', {cls: 'size', text: 'Ask your editor'}),
        el('button', {type: 'button', cls: 'x', text: 'Close', on: {click: e => { e.stopPropagation(); dropDraft(c); }}})]),
      el('div', {cls: 'quick'}, ['How does this read?', 'Is this clear?', 'Does this drag?'].map(t => el('button', {type: 'button', text: t, on: {click: () => send(t)}}))),
      el('div', {cls: 'askrow'}, [el('label', {for: 'askwho', text: 'Ask:'}), pick]),
      box,
      el('div', {cls: 'marks'}, [el('button', {type: 'button', cls: 'main', text: 'Ask', on: {click: () => send()}})]),
    ]);
    c.node = card;
    return c;
  }
  function dropDraft(c) {
    c.node.remove();
    W.cards = W.cards.filter(x => x !== c);
    if (W.focus === c) W.focus = null;
    paint();
    layout();
  }
  async function ask(c, range, sec, question, who) {
    const passage = range.toString();
    const wait = {id: '', n: sec.n, quote: passage, question, state: 'waiting', who};
    const tmp = Object.assign({}, c, {a: wait, kind: 'ask'});
    const node = askCard(tmp);
    c.node.replaceWith(node);
    c.node = node;
    let got;
    try {
      got = await call('api/ask', {title: W.title, n: sec.n, passage, question, who});
    } catch (e) {
      wait.state = 'failed';
      wait.answer = e.message;
      const n2 = askCard(tmp);
      c.node.replaceWith(n2);
      c.node = n2;
      layout();
      return;
    }
    W.asks.push(got.ask);
    if (W.focus === c) W.focus = null;
    dropDraft(c);
    anchorAll();
    const mine = W.cards.find(x => x.id === got.ask.id);
    if (mine) focusCard(mine, false);
    if (got.ask.state === 'waiting') pollAsks();
  }
  let polling = 0;
  function pollAsks() {
    clearTimeout(polling);
    polling = setTimeout(async () => {
      let got;
      try { got = await call('api/asks?' + q({title: W.title})); } catch (e) { pollAsks(); return; }
      const before = JSON.stringify(W.asks.map(a => [a.id, a.state]));
      W.asks = got.asks || [];
      if (before !== JSON.stringify(W.asks.map(a => [a.id, a.state]))) anchorAll();
      if (W.asks.some(a => a.state === 'waiting')) pollAsks();
    }, 3000);
  }

  // the "Ask your editor" button by her selection
  const askbtn = $('askbtn');
  document.addEventListener('selectionchange', () => {
    const sel = document.getSelection();
    const r = sel.rangeCount ? sel.getRangeAt(0) : null;
    if (!r || r.collapsed || !oneBody(r) || !r.toString().trim() || $('v-write').hidden) { askbtn.hidden = true; return; }
    const rect = r.getBoundingClientRect();
    askbtn.hidden = false;
    const top = Math.min(window.innerHeight - 40, rect.bottom + 6);
    askbtn.style.top = Math.max(0, top) + 'px';
    askbtn.style.left = Math.max(4, Math.min(window.innerWidth - askbtn.offsetWidth - 4, rect.right - askbtn.offsetWidth)) + 'px';
  });
  askbtn.addEventListener('mousedown', e => e.preventDefault());
  askbtn.addEventListener('click', () => {
    const sel = document.getSelection();
    const r = sel.rangeCount ? sel.getRangeAt(0) : null;
    const b = oneBody(r);
    if (!b) return;
    const sec = W.byN[b.parentElement.dataset.n];
    const old = W.cards.find(x => x.id === 'draft');
    if (old) dropDraft(old);
    if (!W.notes) setNotes(true);
    const c = draftCard(r.cloneRange(), sec);
    W.cards.push(c);
    askbtn.hidden = true;
    paint();
    focusCard(c, false);
    const box = c.node.querySelector('textarea');
    if (box) box.focus({preventScroll: true});
  });

  // where the cards sit: level with their words, never on top of each other
  function topOf(c) {
    const s = sheet.getBoundingClientRect().top;
    const r = c.ranges.find(x => !x.collapsed) || c.ranges[0];
    if (r) {
      const rects = r.getClientRects();
      const box = rects.length ? rects[0] : r.getBoundingClientRect();
      if (box && (box.top || box.bottom)) return box.top - s;
    }
    return c.sec.el.getBoundingClientRect().top - s;
  }
  function bottomOfPara(c) {
    const s = sheet.getBoundingClientRect().top;
    const r = c.ranges[0];
    const node = r ? r.endContainer : null;
    const p = node && (node.nodeType === 1 ? node : node.parentElement).closest('.body > *');
    return (p ? p.getBoundingClientRect().bottom : c.sec.el.getBoundingClientRect().top + 40) - s + 4;
  }
  function layout() {
    if (!W.loaded) return;
    margin.hidden = !W.notes;
    if (!W.notes) return;
    const small = narrow.matches;
    for (const c of W.cards) if (c.node.parentNode !== margin) margin.appendChild(c.node);
    if (small) {
      for (const c of W.cards) {
        c.node.hidden = c !== W.focus;
        if (c === W.focus) c.node.style.top = (c.ranges.length ? bottomOfPara(c) : topOf(c)) + 'px';
      }
      return;
    }
    for (const c of W.cards) {             // a note not found in the text shows only when opened from its section's list
      if (!c.ranges.length && c !== W.focus) c.node.hidden = true;
    }
    const placed = W.cards.filter(c => c.ranges.length || c === W.focus).map(c => ({c, want: topOf(c)})).sort((a, b) => a.want - b.want);
    let next = 0;
    for (const x of placed) {
      x.c.node.hidden = false;
      const top = Math.max(x.want, next);
      x.c.node.style.top = top + 'px';
      next = top + x.c.node.offsetHeight + 6;
    }
    if (W.focus) {               // the card she is on sits level with its words; the ones below move down for it
      const i = placed.findIndex(x => x.c === W.focus);
      if (i >= 0) {
        let top = placed[i].want;
        W.focus.node.style.top = top + 'px';
        let below = top + W.focus.node.offsetHeight + 6;
        for (let j = i + 1; j < placed.length; j++) {
          const t = Math.max(parseFloat(placed[j].c.node.style.top), below);
          placed[j].c.node.style.top = t + 'px';
          below = t + placed[j].c.node.offsetHeight + 6;
        }
        for (let j = i - 1; j >= 0; j--) {
          const node = placed[j].c.node;
          const t = Math.min(parseFloat(node.style.top), top - node.offsetHeight - 6);
          node.style.top = t + 'px';
          top = t;
        }
      }
    }
  }
  let relaying = 0;
  function relayoutSoon() {
    cancelAnimationFrame(relaying);
    relaying = requestAnimationFrame(() => setTimeout(layout, 120));
  }
  window.addEventListener('resize', relayoutSoon);
  narrow.addEventListener('change', layout);

  function focusCard(c, scroll) {
    if (!W.notes) setNotes(true);
    if (W.focus && W.focus.node) W.focus.node.classList.remove('on');
    W.focus = c;
    c.node.classList.add('on');
    paint();
    layout();
    if (scroll) {
      const top = c.ranges.length ? topOf(c) : c.sec.el.getBoundingClientRect().top - sheet.getBoundingClientRect().top;
      const onScreen = sheet.getBoundingClientRect().top + top - scroller.getBoundingClientRect().top;
      scroller.scrollTo({top: Math.max(0, scroller.scrollTop + onScreen - scroller.clientHeight / 3)});
    }
  }
  function unfocus() {
    if (W.focus && W.focus.id === 'draft') { dropDraft(W.focus); return; }
    if (W.focus && W.focus.node) W.focus.node.classList.remove('on');
    W.focus = null;
    paint();
    layout();
  }

  // a click on highlighted words opens their card
  doc.addEventListener('click', e => {
    if (!W.notes || !W.cards.length) return;
    let node = null, off = 0;
    if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(e.clientX, e.clientY);
      if (p) { node = p.offsetNode; off = p.offset; }
    } else if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(e.clientX, e.clientY);
      if (r) { node = r.startContainer; off = r.startOffset; }
    }
    if (!node) return;
    const hit = W.cards.find(c => c.ranges.some(r => {
      try { return !r.collapsed && r.comparePoint(node, off) === 0 && !(r.endContainer === node && r.endOffset === off); } catch (err) { return false; }
    }));
    if (hit) focusCard(hit, false);
  });

  function setNotes(on) {
    W.notes = on;
    $('notesbtn').setAttribute('aria-pressed', String(on));
    document.body.classList.toggle('nonotes', !on);
    for (const s of W.secs) drawHead(s);
    paint();
    layout();
  }
  $('notesbtn').addEventListener('click', () => setNotes(!W.notes));

  // ---------------------------------------------------------------- find and replace

  const F = {hits: [], at: -1};
  function openFind() {
    const bar = $('findbar');
    bar.hidden = false;
    const sel = document.getSelection();
    const t = sel.rangeCount && !sel.isCollapsed && oneBody(sel.getRangeAt(0)) ? sel.toString() : '';
    if (t && t.length < 200 && !t.includes('\n')) $('findq').value = t;
    $('findq').focus();
    $('findq').select();
    search(true);
  }
  function closeFind() {
    $('findbar').hidden = true;
    F.hits = [];
    F.at = -1;
    if (HL) { CSS.highlights.delete('find'); CSS.highlights.delete('find-now'); }
    doc.focus({preventScroll: true});
  }
  function search(keepPlace) {
    const want = foldQuote($('findq').value);
    const was = F.hits[F.at];
    F.hits = [];
    if (want) {
      for (const sec of W.secs) {
        const ix = indexSec(sec);
        let i = ix.folded.indexOf(want);
        while (i >= 0) {
          const r = rangeOf(ix, i, want.length);
          r.sec = sec;
          F.hits.push(r);
          i = ix.folded.indexOf(want, i + want.length);
        }
      }
    }
    F.at = -1;
    if (F.hits.length) {
      // the first match at or after where she was, else at or after the top of the screen
      let from = null;
      if (keepPlace && was) from = was;
      F.at = 0;
      if (from) {
        const k = F.hits.findIndex(r => r.compareBoundaryPoints(Range.START_TO_START, from) >= 0);
        F.at = k < 0 ? 0 : k;
      } else {
        const top = scroller.getBoundingClientRect().top;
        const k = F.hits.findIndex(r => r.getBoundingClientRect().bottom >= top);
        F.at = k < 0 ? 0 : k;
      }
    }
    showHit();
  }
  function showHit() {
    $('fcount').textContent = $('findq').value ? (F.hits.length ? (F.at + 1) + ' of ' + F.hits.length : 'Not found') : '';
    if (!HL) return;
    CSS.highlights.delete('find');
    CSS.highlights.delete('find-now');
    if (F.hits.length) CSS.highlights.set('find', new Highlight(...F.hits));
    const r = F.hits[F.at];
    if (!r) return;
    CSS.highlights.set('find-now', new Highlight(r));
    const box = r.getBoundingClientRect(), view = scroller.getBoundingClientRect();
    if (box.top < view.top + 40 || box.bottom > view.bottom - 40) scroller.scrollTo({top: scroller.scrollTop + box.top - view.top - view.height / 3});
  }
  function step(d) {
    if (!F.hits.length) return;
    F.at = (F.at + d + F.hits.length) % F.hits.length;
    showHit();
  }
  function replaceAt(r, text) {
    const sel = document.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    if (text) document.execCommand('insertText', false, text);
    else document.execCommand('delete', false, null);
  }
  function replaceOne() {
    const r = F.hits[F.at];
    if (!r) return;
    if (foldQuote(r.toString()) !== foldQuote($('findq').value)) { search(true); return; }
    doc.focus({preventScroll: true});
    replaceAt(r, $('findr').value);
    search(true);
    $('findr').focus();
  }
  function replaceAll() {
    const n = F.hits.length;
    if (!n) return;
    if (!window.confirm('Replace ' + plural(n, 'place') + ' in your book? Ctrl+Z undoes them one at a time.')) return;
    doc.focus({preventScroll: true});
    const text = $('findr').value;
    for (let i = n - 1; i >= 0; i--) replaceAt(F.hits[i], text);
    search(false);
    flash('Replaced ' + plural(n, 'place') + '.');
    $('findr').focus();
  }
  $('findbtn').addEventListener('click', openFind);

  // Save a Word copy: her book as it stands, saved first, into My files. It reads her words and changes none of them.
  $('wordcopy').addEventListener('click', async () => {
    const said = $('wsaid');
    if (!W.title) return;
    said.className = 'wsaid';
    said.textContent = 'Making the Word copy...';
    try {
      await flush(true);
      said.textContent = (await call('api/wordcopy', {title: W.title})).message;
    } catch (e) {
      said.textContent = e.message;
      said.className = 'wsaid bad';
    }
  });
  $('fclose').addEventListener('click', closeFind);
  $('fnext').addEventListener('click', () => step(1));
  $('fprev').addEventListener('click', () => step(-1));
  $('frep').addEventListener('click', replaceOne);
  $('freps').addEventListener('click', replaceAll);
  $('findq').addEventListener('input', () => search(false));
  $('findq').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1); }
    if (e.key === 'Escape') closeFind();
  });
  $('findr').addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); replaceOne(); }
    if (e.key === 'Escape') closeFind();
  });

  // ---------------------------------------------------------------- opening a book

  function open(title) {
    W.opening = opening(title);
    return W.opening;
  }
  async function opening(title) {
    if (W.title === title && W.loaded) { await refreshQuiet(); return; }
    if (W.title && unsaved()) await flush(true);
    W.title = title;
    W.loaded = false;
    W.focus = null;
    $('wsaid').textContent = '';                   // the last Word copy's line was about the other book
    W.lastSaved = null;
    for (const c of W.cards) if (c.node) c.node.remove();
    W.cards = [];
    quietly(() => clear(doc));
    doc.appendChild(el('p', {cls: 'fine', contenteditable: 'false', text: 'Opening your book...'}));
    let got, asks;
    try {
      [got, asks] = await Promise.all([call('api/book?' + q({title})), call('api/asks?' + q({title})).catch(() => ({asks: []}))]);
    } catch (e) {
      quietly(() => clear(doc).appendChild(el('p', {cls: 'fine', contenteditable: 'false', text: e.message})));
      return;
    }
    if (W.title !== title) return;
    draw(got.sections);
    W.asks = asks.asks || [];
    W.loaded = true;
    scroller.scrollTop = 0;
    say();
    anchorAll();
    if (W.asks.some(a => a.state === 'waiting')) pollAsks();
  }

  // the book again from the server, for parts she has not touched (Use this, a restore, another window)
  async function refreshQuiet() {
    let got;
    try { got = await call('api/book?' + q({title: W.title})); } catch (e) { return; }
    const ns = got.sections.map(s => s.n).join(',');
    if (ns !== W.secs.map(s => s.n).join(',') && !unsaved()) { draw(got.sections); anchorAll(); return; }
    let moved = false;
    for (const s of got.sections) {
      const sec = W.byN[s.n];
      if (!sec || sec.sha === s.sha || sec.touched || sec.saving || sec.err || sec.stale) continue;
      quietly(() => fillBody(sec.body, s.text));
      Object.assign(sec, {sha: s.sha, saved: s.text, words: s.words, versions: s.versions});
      moved = true;
    }
    if (moved) { drawTocWords(); anchorAll(); }
  }

  // from the Editor's desk: go to a finding's words
  async function goTo(n, quote, fid) {
    if (W.opening) await W.opening;               // the book may still be on its way
    const sec = W.byN[n];
    if (!sec) return;
    const card = fid && W.cards.find(c => c.id === fid);
    if (card) { focusCard(card, true); return; }
    const r = findIn(sec, quote);
    if (!r) { scrollToSec(sec); return; }
    if (HL) CSS.highlights.set('find-now', new Highlight(r));
    const box = r.getBoundingClientRect(), view = scroller.getBoundingClientRect();
    scroller.scrollTo({top: scroller.scrollTop + box.top - view.top - view.height / 3});
  }

  // what the desk knows: open findings and whether a check is running
  function desk(v) {
    W.status = v && v.status;
    if (v && v.staff && v.staff.length) W.staff = v.staff;
    const open = (v && v.findings || []).filter(f => f.state === 'open');
    const key = JSON.stringify(open.map(f => [f.id, f.state, f.where, f.note, f.what]));
    if (key === W.fkey) return;
    if (v.title !== W.title) { W.fkey = key; W.findings = open; return; }     // for the book about to open
    if (margin.contains(document.activeElement)) return;      // never redraw under her while she types a note
    W.fkey = key;
    W.findings = open;
    anchorAll();
  }

  document.execCommand('defaultParagraphSeparator', false, 'p');
  document.execCommand('styleWithCSS', false, false);

  window.WriteView = {
    open, flush, goTo, desk, refreshQuiet, unsaved, hooks: W.hooks,
    show() { layout(); markCurrent(); },
    // for a quick check in the console: the port of marks.py, and what a section would save as now
    marks: {runs, wrap, strip: stripMarks},
    textOf: n => (W.byN[n] ? serialize(W.byN[n]) : null),
  };
})();
