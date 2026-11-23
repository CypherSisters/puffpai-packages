/* Manuscript 0.5.1, the page. See CONTRACT.md for the routes and the block format.

   Every call goes to the page's own origin with no token; the PuffPAI Desktop's proxy adds the key.

   Fixture mode: open the page with ?fixture=1 and no server is needed. Every call reads a file under fixtures/
   (next to web/, found at the origin as <base>/../fixtures/ when the page is at .../web/, else <base>/fixtures/):

     GET    api/list                    fixtures/list.sample.json
     GET    api/section?title&n         fixtures/section.sample.json, the entry with that n
     GET    api/status?title            fixtures/status.reading.json for the first two calls, then status.done.json
     GET    api/bible?title             fixtures/bible.sample.json
     GET    api/tab?title&tab           fixtures/tab.characters.json or fixtures/tab.plot.json;
                                        other tabs answer with a short "no sample for this tab" block
     GET    api/views?title&tab?        fixtures/views.sample.json, filtered by tab
     DELETE api/views?title&id          kept in memory only: the view is hidden until the page reloads
     GET    api/pointer?title           fixtures/pointer.sample.json ({} after Clear)
     DELETE api/pointer?title           kept in memory only
     GET    api/notes?title&tab         empty notes, kept in memory
     POST   api/notes                   kept in memory only, nothing is written
     POST   api/analyze                 shows the reading again (status starts over at reading)
     POST   api/show, POST api/upload   refused with a plain message: there is no bot behind the sample page
     POST   api/correct, api/edit, api/correct/undo   refused the same way
*/
(function () {
  'use strict';
  const FIXTURE = new URLSearchParams(location.search).get('fixture') === '1';
  const BASE = location.pathname.replace(/\/[^/]*\.[^/]*$/, '/').replace(/([^/])$/, '$1/');
  const FIX = (/\/web\/$/.test(BASE) ? BASE.replace(/web\/$/, '') : BASE) + 'fixtures/';
  const $ = id => document.getElementById(id);
  const DEFAULT_TABS = [{id: 'characters', label: 'Characters'}, {id: 'plot', label: 'Plot and subplots'},
    {id: 'timeline', label: 'Timeline'}, {id: 'structure', label: 'Structure'}, {id: 'world', label: 'World'},
    {id: 'science', label: 'Science'}, {id: 'themes', label: 'Themes'}, {id: 'voice', label: 'Voice'}];
  const EXAMPLES = {world: 'every place in the story', characters: 'the character arc of someone',
    plot: 'the threads of the first half', timeline: 'the events in story order', structure: 'the turning points',
    themes: 'where the story talks about home', science: 'the claims about the moon', voice: 'the dialogue habits'};
  // The standard Maps board for each tab; a tab without one opens the Maps list.
  const TAB_BOARD = {characters: 'std-relationships', plot: 'std-threads', timeline: 'std-timeline',
    structure: 'std-structure', world: 'std-world'};
  const smooth = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';

  const S = {
    books: [], tabs: DEFAULT_TABS, book: null, n: null, sec: null, body: '', strip: 0,
    tab: 'characters', tabData: null, tabToken: 0,
    stamp: null, dirty: false, status: null, model: null, statusTimer: null,
    pointer: null, pointerSeen: null, pointerBusy: false, viewsBusy: false, newestView: undefined,
    viewStates: null, fastTimer: null,
    editing: null, drafts: {}, fixWatch: null, fixTimer: null,
  };

  // ---------------------------------------------------------------- calls

  async function call(path, method, body) {
    method = method || (body === undefined ? 'GET' : 'POST');
    if (FIXTURE) return fixture(path, method, body);
    const opts = {method: method};
    if (body !== undefined) { opts.headers = {'Content-Type': 'application/json'}; opts.body = JSON.stringify(body); }
    const gone = 'Manuscript did not answer. Open Apps to see why.';
    let r;
    try { r = await fetch(BASE + path, opts); } catch (e) { throw new Error(gone); }
    let out = null;
    try { out = await r.json(); } catch (e) { out = null; }
    if (!r.ok) throw new Error((out && (out.error || out.message)) || gone);
    return out || {};
  }
  const q = o => Object.entries(o).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');

  const F = {statusCalls: 0, removed: new Set(), cleared: false, notes: {}};
  async function fixture(path, method, body) {
    const [route, qs] = path.split('?');
    const p = new URLSearchParams(qs || '');
    const get = async name => {
      const r = await fetch(FIX + name, {cache: 'no-store'});
      if (!r.ok) throw new Error('The sample file ' + name + ' is missing.');
      return r.json();
    };
    const keep = vs => (vs || []).filter(v => !F.removed.has(v.id));
    switch (route) {
      case 'api/manuscript': return {message: 'Sample page: nothing was put away.'};
      case 'api/list': return get('list.sample.json');
      case 'api/section': {
        const all = await get('section.sample.json');
        const s = (all.sections || []).find(x => String(x.n) === p.get('n'));
        if (!s) throw new Error('There is no section ' + p.get('n') + ' in the sample.');
        return s;
      }
      case 'api/status': return get(F.statusCalls++ < 2 ? 'status.reading.json' : 'status.done.json');
      case 'api/bible': return get('bible.sample.json');
      case 'api/tab': {
        const t = p.get('tab');
        if (t === 'characters' || t === 'plot') { const d = await get('tab.' + t + '.json'); d.views = keep(d.views); return d; }
        return {blocks: [{type: 'markdown', text: 'The sample page has no sample for this tab. Try **Characters** or **Plot and subplots**.'}], views: []};
      }
      case 'api/correct': case 'api/correct/undo': case 'api/edit':
        throw new Error('This is the sample page, so there is no story bible to correct.');
      case 'api/views': {
        if (method === 'DELETE') { F.removed.add(p.get('id')); return {message: 'Removed that view.'}; }
        const all = await get('views.sample.json');
        const t = p.get('tab');
        return {views: keep(all.views).filter(v => !t || v.tab === t)};
      }
      case 'api/pointer':
        if (method === 'DELETE') { F.cleared = true; return {message: 'Cleared.'}; }
        return F.cleared ? {} : get('pointer.sample.json');
      case 'api/notes': {
        const t = method === 'POST' ? body.tab : p.get('tab');
        if (method === 'POST') { F.notes[t] = String(body.text || ''); return {message: 'Kept on this sample page only. Nothing was written.', stamp: 'sample'}; }
        return {title: p.get('title'), tab: t, text: F.notes[t] || '', stamp: 'sample'};
      }
      case 'api/analyze': F.statusCalls = 0; return {message: 'This is the sample page, so it only shows a reading.'};
      case 'api/show': throw new Error('This is the sample page, so your bot is not here to make a view.');
      case 'api/upload': throw new Error('This is the sample page, so uploads are off.');
      case 'api/size': return {line: 'This book uses 4.3 MB: pictures 3.9 MB.'};
      case 'api/settings':
        if (method === 'POST') { F.vault = body.vault_free; return {settings: {vault_free: F.vault}, message: 'Kept on this sample page only. Nothing was written.'}; }
        return {settings: {vault_free: F.vault !== false}};
    }
    throw new Error('The sample page has nothing for ' + route + '.');
  }

  // ---------------------------------------------------------------- small helpers

  function say(el, text, bad) { el.textContent = text || ''; el.className = 'say' + (bad ? ' bad' : ''); }
  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  }
  const tabLabel = id => (S.tabs.find(t => t.id === id) || {label: id}).label;

  // Maps' page for a board. Maps is a sibling package, so its address is built the way the Desktop builds one:
  // /pkg/maps/ on this page's own origin. The Desktop's proxy resolves it to Maps and adds Maps' key, the same way
  // it serves this page; nothing here talks to Maps directly.
  function mapsHref(board, focus) {
    const o = {title: S.book || ''};
    if (board) o.board = board;
    if (focus) o.focus = focus;
    return location.origin + '/pkg/maps/?' + q(o);
  }
  const tabMaps = tab => mapsHref(TAB_BOARD[tab] || '');
  // Inside the PuffPAI window or the dashboard this page is a frame, and on the home network each app page has
  // its own port, so a /pkg/ link would not resolve there. A framed page asks its parent to open Maps instead, with
  // only the query (never an address); the window decides. Outside a frame the plain link stands. Editor's Desk opens
  // the same way.
  document.addEventListener('click', ev => {
    const a = ev.target && ev.target.closest ? ev.target.closest('a.maps-link, a.desk-link') : null;
    if (!a || window.parent === window) return;
    const i = (a.getAttribute('href') || '').indexOf('?');
    ev.preventDefault();
    window.parent.postMessage({puffpai: 'open', pkg: a.classList.contains('desk-link') ? 'editors-desk' : 'maps', query: i >= 0 ? a.getAttribute('href').slice(i + 1) : ''}, '*');
  });
  const sectionsOf = b => (b && (b.sections || b.chapters)) || [];
  function leaveOk() { return !S.dirty || confirm('Your notes on this tab are not saved. Leave them?'); }

  // Where `needles` first appear in the text: the whole phrase (any spacing, any case), else the longest run of
  // its words, else one longer word. Returns {start, end} or null.
  const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  function findRange(text, needles) {
    const tries = [];
    for (const nd of needles) {
      const words = String(nd || '').split(/\s+/).map(w => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, '')).filter(Boolean);
      if (words.length) tries.push(words);
    }
    const hit = words => {
      const m = new RegExp(words.map(esc).join('[^\\p{L}\\p{N}]+'), 'iu').exec(text);
      return m ? {start: m.index, end: m.index + m[0].length} : null;
    };
    const low = text.toLowerCase();
    for (const nd of needles) {
      const t = String(nd || '').trim();
      const i = t ? low.indexOf(t.toLowerCase()) : -1;
      if (i >= 0) return {start: i, end: i + t.length};
    }
    for (const words of tries) { const r = hit(words); if (r) return r; }
    for (const words of tries) {
      for (let len = Math.min(8, words.length - 1); len >= 3; len--) {
        for (let i = 0; i + len <= words.length; i++) { const r = hit(words.slice(i, i + len)); if (r) return r; }
      }
    }
    for (const words of tries) {
      for (const w of words) {
        if (w.length >= 5 && !/^(section|there|their|which|about|where)$/i.test(w)) {
          const r = new RegExp('(^|[^\\p{L}\\p{N}])(' + esc(w) + ')(?![\\p{L}\\p{N}])', 'iu').exec(text);
          if (r) { const s = r.index + r[1].length; return {start: s, end: s + w.length}; }
        }
      }
    }
    return null;
  }

  // ---------------------------------------------------------------- manuscripts and sections

  async function loadList() {
    try {
      const got = await call('api/list');
      S.books = Array.isArray(got.manuscripts) ? got.manuscripts : [];
      if (Array.isArray(got.tabs) && got.tabs.length) S.tabs = got.tabs;
    } catch (e) { emptyReader(e.message); return; }
    drawBooks();
    drawTabs();
    if (!S.books.length) {
      emptyReader('Upload a Word, PDF, text or Markdown file to start. Your bot reads it and fills the tabs. The text is never changed here.');
      return;
    }
    if (!S.book || !S.books.find(b => b.title === S.book)) pickBook(S.books[0].title);
    else drawSections();
  }

  function drawBooks() {
    const sel = $('book');
    sel.textContent = '';
    for (const b of S.books) { const o = node('option', '', b.study ? b.title + ' (a book to study)' : b.title); o.value = b.title; sel.appendChild(o); }
    sel.disabled = !S.books.length;
    $('remove').disabled = !S.books.length;
    if (S.book) sel.value = S.book;
    $('deskopen').href = location.origin + '/pkg/editors-desk/' + (S.book ? '?' + q({title: S.book}) : '');
  }
  function drawSections() {
    const sel = $('sec');
    sel.textContent = '';
    const b = S.books.find(x => x.title === S.book);
    for (const c of sectionsOf(b)) {
      const o = node('option', '', c.n + '. ' + c.heading + (c.words ? ' (' + c.words + ' words)' : ''));
      o.value = String(c.n); sel.appendChild(o);
    }
    sel.disabled = !sectionsOf(b).length;
    if (S.n !== null) sel.value = String(S.n);
  }

  function pickBook(title) {
    const changing = title !== S.book;
    S.book = title; S.n = null; S.sec = null; S.pointer = null; S.pointerSeen = null; S.newestView = undefined; S.viewStates = null; S.model = null;
    say($('upsay'), '');                       // 0.3.2: the last upload's or reading's line belongs to the other manuscript
    drawBooks(); drawSections(); hidePointer();
    const first = sectionsOf(S.books.find(b => b.title === title))[0];
    if (first) openSection(first.n); else emptyReader('This manuscript has no sections yet.');
    loadStatus();
    loadTab();
    loadSize();
    if (changing) loadNotes();
    pollPointer();
  }

  function emptyReader(text) {
    const r = $('reader');
    r.textContent = '';
    r.appendChild(node('p', 'empty', text));
  }

  // Opens section n. mark: {kind: 'pointer' | 'find', start?, end?, quote?, text?, hint?}
  async function openSection(n, mark) {
    n = Number(n);
    if (S.n !== n || !S.sec) {
      S.n = n; S.sec = null; $('sec').value = String(n);
      try {
        const s = await call('api/section?' + q({title: S.book, n: n}));
        if (S.n !== n) return;
        S.sec = s;
        const raw = String(s.text || '');
        const m = /^# .*\n+/.exec(raw);
        S.strip = m ? m[0].length : 0;
        S.pieces = markRuns(raw.slice(S.strip));
        S.body = S.pieces.map(x => x.t).join('');       // her words without the marks: places and finds are in these
      } catch (e) { S.sec = null; emptyReader(e.message); return; }
    }
    drawSection(mark);
  }

  function drawSection(mark) {
    const s = S.sec, r = $('reader');
    r.textContent = '';
    r.appendChild(node('h2', '', s.heading || 'Section ' + s.n));
    r.appendChild(node('div', 'meta', (s.title || S.book) + ', section ' + s.n + (s.words ? ', ' + s.words + ' words' : '') + '. Read only.'));
    if (s.changed) {                             // Editor's Desk changed it after the last reading
      const p = node('p', 'changed', 'This section changed since your bot last read it. ');
      const b = node('button', 'link', 'Read again'); b.type = 'button';
      b.onclick = () => { s.changed = false; p.remove(); startReading(); };
      p.appendChild(b);
      r.appendChild(p);
    }
    const t = node('div', 'text');
    r.appendChild(t);
    let range = null;
    if (mark && mark.kind === 'pointer') {
      const a = Number(mark.start) - S.strip, b = Number(mark.end) - S.strip;
      const norm = x => String(x || '').replace(/\s+/g, ' ').trim().toLowerCase();
      if (Number.isInteger(a) && Number.isInteger(b) && a >= 0 && b > a && b <= S.body.length &&
          (!mark.quote || norm(S.body.slice(a, b)) === norm(mark.quote))) range = {start: a, end: b};
      else range = findRange(S.body, [mark.quote]);
    } else if (mark) range = findRange(S.body, [mark.text, mark.hint]);
    if (!range) { drawPieces(t, S.pieces, null, null); r.scrollTop = 0; return; }
    const hl = node('mark', mark.kind === 'pointer' ? 'hl-pointer' : 'hl-find');
    drawPieces(t, S.pieces, range, hl);
    requestAnimationFrame(() => hl.scrollIntoView({block: 'center', behavior: smooth}));
  }

  // Italics and bold in her text, as the section file keeps them (marks.py): *italic*, **bold**, ***both***, inside one
  // line. A line of only stars and spaces is a scene break and stays as it is. The same rule as marks.py runs().
  const MARK = /(\*{1,3})(?=[^\s*])([^*\n]*?[^\s*]|[^\s*])\1(?!\*)/g;
  function markRuns(text) {
    const out = [];
    String(text || '').split('\n').forEach((line, k) => {
      if (k) out.push({t: '\n', i: false, b: false});
      if (/^[\s*]*$/.test(line)) { if (line) out.push({t: line, i: false, b: false}); return; }
      let at = 0, m;
      MARK.lastIndex = 0;
      while ((m = MARK.exec(line)) !== null) {
        if (m.index > at) out.push({t: line.slice(at, m.index), i: false, b: false});
        const n = m[1].length;
        out.push({t: m[2], i: n === 1 || n === 3, b: n === 2 || n === 3});
        at = m.index + m[0].length;
      }
      if (at < line.length) out.push({t: line.slice(at), i: false, b: false});
    });
    return out;
  }

  // The pieces into box as text nodes, inside em and strong where marked; the part in range goes into hl instead.
  function drawPieces(box, pieces, range, hl) {
    let pos = 0, placed = false;
    const put = (where, text, p) => {
      if (!text) return;
      let n = document.createTextNode(text);
      if (p.b) { const s = document.createElement('strong'); s.appendChild(n); n = s; }
      if (p.i) { const e = document.createElement('em'); e.appendChild(n); n = e; }
      where.appendChild(n);
    };
    for (const p of pieces) {
      const a = pos, b = pos + p.t.length;
      pos = b;
      if (!range || b <= range.start || a >= range.end) {
        if (range && a >= range.end && !placed) { box.appendChild(hl); placed = true; }
        put(box, p.t, p);
        continue;
      }
      const s = Math.max(range.start, a) - a, e = Math.min(range.end, b) - a;
      put(box, p.t.slice(0, s), p);
      if (!placed) { box.appendChild(hl); placed = true; }
      put(hl, p.t.slice(s, e), p);
      put(box, p.t.slice(e), p);
    }
    if (range && !placed) box.appendChild(hl);
  }

  // A click on any section in a block.
  function jump(section, text, hint) {
    openSection(section, {kind: 'find', text: text, hint: hint});
  }

  // ---------------------------------------------------------------- status

  async function loadStatus() {
    clearTimeout(S.statusTimer);
    const title = S.book;
    if (!title) return;
    let st;
    try { st = await call('api/status?' + q({title: title})); } catch (e) { st = {state: 'idle'}; }
    if (title !== S.book) return;
    const before = S.status;
    S.status = st || {state: 'idle'};
    if (S.status.state === 'done' && !S.model) {
      try { const bible = await call('api/bible?' + q({title: title})); S.model = bible.built_with || ''; } catch (e) { S.model = ''; }
    }
    drawStatus();
    if (before && (before.state !== S.status.state || before.done !== S.status.done)) loadTab();
    if (S.status.state === 'reading') S.statusTimer = setTimeout(loadStatus, 5000);
  }

  function drawStatus() {
    const box = $('status'), st = S.status || {};
    box.textContent = '';
    const line = node('span', 'line');
    let action = null;
    if (st.state === 'reading') {
      line.textContent = (st.step || ('Reading section ' + ((st.done || 0) + 1) + ' of ' + (st.total || '?'))) +
        ', so chat answers are slower until it finishes';
      if (st.total) { const p = node('progress'); p.max = st.total; p.value = st.done || 0; box.appendChild(line); box.appendChild(p); }
      else box.appendChild(line);
    } else if (st.state === 'done') {
      line.textContent = S.model ? 'Read by ' + S.model + ' on this computer.' : 'Read on this computer.';
      if (st.unchanged) line.textContent += ' Nothing changed since then, so nothing was read again.';
      box.appendChild(line); action = 'Read again';
    } else if (st.state === 'failed') {
      line.textContent = 'The reading stopped. ' + (st.step || '');
      box.appendChild(line); action = 'Try again';
    } else {
      line.textContent = 'Not read yet.';
      box.appendChild(line); action = 'Start reading';
    }
    if (st.vault && st.state && st.state !== 'idle') line.textContent += ' (' + st.vault + ')';   // 0.3.3
    if (action) {
      const b = node('button', 'link', action); b.type = 'button';
      b.onclick = startReading;
      box.appendChild(b);
    }
    const probs = Array.isArray(st.problems) ? st.problems.filter(Boolean) : [];
    if (probs.length) {
      const d = node('details', 'problems');
      d.appendChild(node('summary', '', probs.length === 1 ? 'One note from the reading' : probs.length + ' notes from the reading'));
      const ul = node('ul');
      for (const p of probs) ul.appendChild(node('li', '', String(p)));
      d.appendChild(ul);
      box.appendChild(d);
    }
  }

  async function startReading() {
    try {
      const got = await call('api/analyze', 'POST', {title: S.book});
      say($('upsay'), got.message || '');
    } catch (e) { say($('upsay'), e.message, true); }
    S.model = null;
    loadStatus();
  }

  // ---------------------------------------------------------------- pointer

  async function pollPointer() {
    if (!S.book || S.pointerBusy) return;
    S.pointerBusy = true;
    const title = S.book;
    try {
      const p = await call('api/pointer?' + q({title: title}));
      if (title !== S.book) return;
      if (p && p.set_at && Number(p.section) > 0) {
        if (p.set_at !== S.pointerSeen) { S.pointerSeen = p.set_at; S.pointer = p; showPointer(true); }
      } else if (S.pointer) { S.pointer = null; hidePointer(); if (S.sec) drawSection(null); }
    } catch (e) { /* quiet: the next poll tries again */ } finally { S.pointerBusy = false; }
  }

  function showPointer(go) {
    const p = S.pointer;
    if (!p) return;
    $('pnote').textContent = (p.note || 'Your bot is pointing here.') + ' (section ' + p.section + ')';
    $('pbar').hidden = false;
    if (go) openSection(p.section, {kind: 'pointer', start: p.start, end: p.end, quote: p.quote});
  }
  function hidePointer() { $('pbar').hidden = true; }

  // ---------------------------------------------------------------- tabs, blocks, views

  function drawTabs() {
    const box = $('tabs');
    box.textContent = '';
    for (const t of S.tabs) {
      const b = node('button', '', t.label); b.type = 'button';
      b.setAttribute('role', 'tab');
      b.setAttribute('aria-selected', String(t.id === S.tab));
      b.onclick = () => { if (t.id !== S.tab && leaveOk()) switchTab(t.id); };
      box.appendChild(b);
    }
    $('askin').placeholder = EXAMPLES[S.tab] || '';
    $('ask-tab').textContent = 'under ' + tabLabel(S.tab);
    $('notes-h').textContent = 'Your ' + tabLabel(S.tab) + ' notes';
    $('maps').href = tabMaps(S.tab);
    $('maps').textContent = TAB_BOARD[S.tab] ? 'See in Maps' : 'See in Maps (all boards)';
  }
  function switchTab(id) {
    S.tab = id; S.dirty = false; S.tabData = null; S.viewStates = null; S.editing = null; S.drafts = {};
    drawTabs(); loadTab(); loadNotes();
  }

  async function loadTab(scrollTo) {
    if (!S.book) return;
    const token = ++S.tabToken, title = S.book, tab = S.tab;
    if (!S.tabData) $('blocks').replaceChildren(node('p', 'empty', 'Loading...'));
    try {
      const d = await call('api/tab?' + q({title: title, tab: tab}));
      if (token !== S.tabToken) return;
      const arr = v => Array.isArray(v) ? v : [];
      S.tabData = {title: title, tab: tab, blocks: arr(d.blocks), views: arr(d.views),
        cards: arr(d.cards), corrections: arr(d.corrections), rewriting: arr(d.rewriting),
        block_keys: d.block_keys || {}, edits: d.edits || {items: {}, adds: [], folds: [], people: [], places: []}};
    } catch (e) {
      if (token !== S.tabToken) return;
      S.tabData = null;
      $('blocks').replaceChildren(node('p', 'say bad', e.message));
      $('views').textContent = '';
      return;
    }
    drawTab(scrollTo);
    watchFixes();
  }

  // While your bot is working out a correction or rewriting a paragraph, look again every 3 seconds. Never over what
  // she is typing in an editor: the next look waits for her.
  function watchFixes() {
    clearTimeout(S.fixTimer);
    const d = S.tabData;
    if (!d) return;
    const seen = S.fixWatch && d.corrections.find(c => c.id === S.fixWatch);
    if (seen && seen.state !== 'working') { say($('fixsay'), seen.message || ''); S.fixWatch = null; }
    const busy = d.rewriting.length || d.corrections.some(c => c.state === 'working' || c.prose_state === 'rewriting');
    if (!busy) return;
    S.fixTimer = setTimeout(() => {
      const at = document.activeElement;
      if (at && at.closest && at.closest('.editor')) watchFixes(); else loadTab();
    }, 3000);
  }

  // Every re-draw comes from one GET api/tab, so the default blocks and the saved views always come together. The
  // notes box is never touched here, and the scroll stays where it was unless a new view asked to be scrolled to.
  function drawTab(scrollTo) {
    const d = S.tabData;
    if (!d) return;
    const bh = $('blocks'), vh = $('views'), side = bh.closest('.region');
    const keepSide = side ? side.scrollTop : 0, keepPage = window.scrollY;
    bh.textContent = ''; vh.textContent = ''; $('fixes').textContent = '';
    try { drawFixes(d); drawTabParts(d, bh, vh); } finally {
      if (side) side.scrollTop = keepSide;
      window.scrollTo(window.scrollX, keepPage);
    }
    if (scrollTo) { const c = document.getElementById('view-' + scrollTo); if (c) c.scrollIntoView({block: 'start', behavior: smooth}); }
    reopenEditor();
  }

  // The writer's corrections for this tab, what each came to, and an Undo on each. A long list folds its older ones.
  function fixLine(c) {
    const li = node('li', c.state === 'working' ? 'fix working' : c.applied ? 'fix done' : 'fix');
    const said = c.what + (c.correction && String(c.what).toLowerCase().indexOf(String(c.correction).toLowerCase()) < 0 ? ' (' + c.correction + ')' : '');
    const top = node('div', 'fix-top');
    if (c.state === 'working') top.appendChild(node('span', 'spinner'));
    top.appendChild(node('span', 'fix-said', (c.state === 'working' ? 'Your bot is working on this: ' : c.applied ? 'You corrected this: ' : 'You said: ') + said));
    const un = node('button', 'link undo', 'Undo'); un.type = 'button';
    un.setAttribute('aria-label', 'Undo: ' + c.what);
    un.onclick = () => undoFix(c, un);
    top.appendChild(un);
    li.appendChild(top);
    if (c.applied && c.line && c.by !== 'hand') li.appendChild(node('p', 'fix-more', 'Changed: ' + c.line + '.'));
    if (c.partial) li.appendChild(node('p', 'fix-more', 'Part of what you said could not be matched, so that part did not change.'));
    if (!c.applied && c.why) li.appendChild(node('p', 'fix-more', c.why));
    if (c.looks) li.appendChild(node('p', 'fix-more', 'A picture Maps already painted keeps the old look until you press Paint again on it.'));
    if (c.prose_state === 'rewriting') li.appendChild(node('p', 'fix-more', 'Your bot is rewriting the paragraphs this touched.'));
    if (c.prose_state === 'after reading') li.appendChild(node('p', 'fix-more', 'The paragraphs this touched are rewritten when the reading ends.'));
    if (c.prose_state === 'failed') li.appendChild(node('p', 'fix-more', 'Your bot could not rewrite the paragraphs this touched. Press Edit to change them yourself.'));
    return li;
  }
  function drawFixes(d) {
    const box = $('fixes');
    if (!d.corrections.length) return;
    const newest = d.corrections.slice().reverse();
    const ul = node('ul', 'fixes');
    newest.slice(0, 5).forEach(c => ul.appendChild(fixLine(c)));
    box.appendChild(ul);
    if (newest.length > 5) {
      const det = node('details', 'dismissed');
      det.appendChild(node('summary', '', 'Older corrections (' + (newest.length - 5) + ')'));
      const more = node('ul', 'fixes');
      newest.slice(5).forEach(c => more.appendChild(fixLine(c)));
      det.appendChild(more);
      box.appendChild(det);
    }
  }
  async function undoFix(c, btn) {
    btn.disabled = true;
    try {
      const got = await call('api/correct/undo', 'POST', {title: S.book, id: c.id});
      say($('fixsay'), got.message || '');
      if (S.fixWatch === c.id) S.fixWatch = null;
      await loadTab();
    } catch (e) { say($('fixsay'), e.message, true); btn.disabled = false; }
  }

  // ---------------------------------------------------------------- Edit, right on the page (0.4)

  // The quiet Edit control of one entry, or null when it has none. Its form opens beside it; a re-draw reopens it,
  // with what she typed still there.
  function editButton(key) {
    const items = (S.tabData && S.tabData.edits.items) || {};
    if (!items[key]) return null;
    const b = node('button', 'edit-btn', 'Edit'); b.type = 'button';
    b.dataset.edit = key;
    b.setAttribute('aria-label', 'Edit ' + items[key].label);
    b.onclick = () => { S.editing = S.editing === key ? null : key; drawTab(); };
    return b;
  }
  function reopenEditor() {
    if (!S.editing) return;
    const add = S.editing.startsWith('add:');
    const found = [...document.querySelectorAll(add ? '[data-add]' : '[data-edit]')].find(b => (add ? 'add:' + b.dataset.add : b.dataset.edit) === S.editing);
    if (!found) { S.editing = null; return; }
    const form = add ? addForm(S.editing.slice(4)) : editor(S.editing);
    if (!form) { S.editing = null; return; }
    const row = found.closest('tr');
    if (row) {
      const tr = node('tr', 'editor-row'), td = node('td');
      td.colSpan = row.children.length;
      td.appendChild(form); tr.appendChild(td);
      row.after(tr);
      // the table sits in a sideways scroll box; the form must stay in the pane
      form.classList.add('in-table');
    } else {
      const host = found.closest('li, article') || found.parentNode;
      host.appendChild(form);
    }
  }
  async function editAct(ops, btn) {
    if (btn) btn.disabled = true;
    try {
      const got = await call('api/edit', 'POST', {title: S.book, ops: ops});
      say($('fixsay'), got.message || '');
      await loadTab();
      return true;
    } catch (e) { say($('fixsay'), e.message, true); if (btn) btn.disabled = false; return false; }
  }
  function belongsTo(it, field, itemKey) {
    const who = it.kind === 'place' ? S.tabData.edits.places : S.tabData.edits.people;
    const others = who.filter(w => w.key !== it.key);
    if (!others.length) return null;
    const sel = node('select', 'belongs');
    sel.setAttribute('aria-label', 'Belongs to');
    sel.appendChild(node('option', '', 'Belongs to...')).value = '';
    for (const w of others) { const o = node('option', '', w.name); o.value = w.key; sel.appendChild(o); }
    sel.onchange = () => {
      if (!sel.value) return;
      const op = {key: it.key, action: 'move', field: field, to: sel.value};
      if (itemKey !== undefined) op.item = itemKey;
      editAct([op], sel);
    };
    return sel;
  }
  function scalarInput(it, f, draft) {
    const now = Object.prototype.hasOwnProperty.call(draft, f.name) ? draft[f.name] : String(f.value === null || f.value === undefined ? '' : f.value);
    let inp;
    if (f.type === 'long') { inp = node('textarea'); inp.value = now; }
    else if (f.type === 'select') {
      inp = node('select');
      f.options.forEach((v, i) => { const o = node('option', '', (f.labels && f.labels[i]) || v || '(none)'); o.value = v; inp.appendChild(o); });
      inp.value = now;
    } else {
      inp = node('input');
      inp.type = f.type === 'number' ? 'number' : 'text';
      inp.value = now;
    }
    inp.oninput = () => { draft[f.name] = inp.value; };
    inp.onchange = inp.oninput;
    return inp;
  }
  function listField(it, f) {
    const box = node('div', 'ed-list');
    box.appendChild(node('span', 'ed-label', f.label));
    if (f.items.length) {
      const ul = node('ul');
      for (const x of f.items) {
        const li = node('li');
        li.appendChild(node('span', 'ed-text', x.text + (x.section !== null && x.section !== undefined ? ' [' + x.section + ']' : '')));
        if (f.moves) { const m = belongsTo(it, f.name, x.key); if (m) li.appendChild(m); }
        const rm = node('button', 'x', 'x'); rm.type = 'button';
        rm.title = 'Remove this'; rm.setAttribute('aria-label', 'Remove ' + x.text);
        rm.onclick = () => editAct([{key: it.key, action: 'remove', field: f.name, item: x.key}], rm);
        li.appendChild(rm);
        ul.appendChild(li);
      }
      box.appendChild(ul);
    }
    const row = node('div', 'ed-add');
    let inp, who = null;
    if (f.add === 'rels' || f.add === 'who') {
      who = node('select');
      who.setAttribute('aria-label', 'Who');
      for (const w of S.tabData.edits.people.filter(w => w.key !== it.key)) { const o = node('option', '', w.name); o.value = w.id; who.appendChild(o); }
      row.appendChild(who);
    }
    if (f.add !== 'who') {
      inp = node('input');
      inp.type = f.add === 'secs' ? 'number' : 'text';
      inp.placeholder = f.add === 'rels' ? 'what they are, like cousin' : f.add === 'secs' ? 'section' : 'add one';
      inp.setAttribute('aria-label', 'Add to ' + f.label);
      row.appendChild(inp);
    }
    const go = node('button', '', 'Add'); go.type = 'button';
    go.onclick = () => {
      const v = inp ? inp.value.trim() : '';
      if (inp && !v) return;
      const value = f.add === 'rels' ? {to: who.value, kind: v} : f.add === 'who' ? who.value : v;
      editAct([{key: it.key, action: 'add', field: f.name, value: value}], go);
    };
    row.appendChild(go);
    box.appendChild(row);
    return box;
  }
  function editor(key) {
    const it = S.tabData.edits.items[key];
    if (!it) return null;
    const draft = S.drafts[key] || (S.drafts[key] = {});
    const form = node('form', 'editor');
    form.setAttribute('aria-label', 'Edit ' + it.label);
    for (const f of it.fields) {
      if (f.type === 'list') { form.appendChild(listField(it, f)); continue; }
      const lab = node('label', 'ed-field');
      lab.appendChild(node('span', 'ed-label', f.label));
      lab.appendChild(scalarInput(it, f, draft));
      form.appendChild(lab);
      if (f.moves && f.value) { const m = belongsTo(it, f.name); if (m) form.appendChild(m); }
    }
    const row = node('div', 'row');
    const save = node('button', 'main', 'Save'); save.type = 'submit';
    row.appendChild(save);
    const close = node('button', '', 'Close'); close.type = 'button';
    close.onclick = () => { S.editing = null; delete S.drafts[key]; drawTab(); };
    row.appendChild(close);
    if (it.removable) {
      const rm = node('button', 'link', 'Remove this ' + it.word); rm.type = 'button';
      rm.onclick = async () => { if (await editAct([{key: key, action: 'remove'}], rm)) { S.editing = null; delete S.drafts[key]; drawTab(); } };
      row.appendChild(rm);
    }
    form.appendChild(row);
    form.onsubmit = async ev => {
      ev.preventDefault();
      const ops = [];
      for (const f of it.fields) {
        if (f.type === 'list' || !Object.prototype.hasOwnProperty.call(draft, f.name)) continue;
        if (String(draft[f.name]) !== String(f.value === null || f.value === undefined ? '' : f.value)) ops.push({key: key, action: 'set', field: f.name, value: draft[f.name]});
      }
      if (ops.length && !(await editAct(ops, save))) return;
      S.editing = null; delete S.drafts[key];
      drawTab();
    };
    return form;
  }
  function addForm(kind) {
    const add = (S.tabData.edits.adds || []).find(a => a.kind === kind);
    if (!add) return null;
    const key = 'add:' + kind, draft = S.drafts[key] || (S.drafts[key] = {});
    const form = node('form', 'editor');
    form.setAttribute('aria-label', 'Add ' + add.word);
    for (const f of add.fields) {
      const lab = node('label', 'ed-field');
      lab.appendChild(node('span', 'ed-label', f.label));
      lab.appendChild(scalarInput(add, f, draft));
      form.appendChild(lab);
    }
    const row = node('div', 'row');
    const save = node('button', 'main', 'Add it'); save.type = 'submit';
    row.appendChild(save);
    const close = node('button', '', 'Close'); close.type = 'button';
    close.onclick = () => { S.editing = null; delete S.drafts[key]; drawTab(); };
    row.appendChild(close);
    form.appendChild(row);
    form.onsubmit = async ev => {
      ev.preventDefault();
      if (await editAct([{key: add.key, action: 'add', value: Object.assign({}, draft)}], save)) {
        S.editing = null; delete S.drafts[key]; drawTab();
      }
    };
    return form;
  }
  // What no block row shows (a timeline's events, the setting, the summaries) folded under the blocks, and Add.
  function drawEditExtras(d, bh) {
    for (const fold of d.edits.folds || []) {
      const det = node('details', 'edit-fold');
      det.appendChild(node('summary', '', fold.label));
      const ul = node('ul', 'items');
      for (const key of fold.keys) {
        const it = d.edits.items[key];
        if (!it) continue;
        const li = node('li');
        li.appendChild(node('span', '', it.label + ' '));
        li.appendChild(editButton(key));
        ul.appendChild(li);
      }
      det.appendChild(ul);
      if (S.editing && fold.keys.includes(S.editing)) det.open = true;
      bh.appendChild(det);
    }
    if ((d.edits.adds || []).length) {
      const row = node('p', 'adds');
      row.appendChild(node('span', '', 'Add: '));
      d.edits.adds.forEach((a, i) => {
        if (i) row.appendChild(document.createTextNode(', '));
        const b = node('button', 'edit-btn', a.word); b.type = 'button';
        b.dataset.add = a.kind;
        b.onclick = () => { S.editing = S.editing === 'add:' + a.kind ? null : 'add:' + a.kind; drawTab(); };
        row.appendChild(b);
      });
      bh.appendChild(row);
    }
  }
  function rewritingNote(host) {
    const m = node('p', 'making');
    m.appendChild(node('span', 'spinner'));
    m.appendChild(document.createTextNode('Your bot is rewriting this...'));
    host.appendChild(m);
  }

  // A main character's card: name, role, an importance pill, personality, write-up, first seen, See in Maps.
  function drawCard(c, host) {
    const card = node('article', 'block char-card');
    const head = node('div', 'card-head');
    head.appendChild(node('h4', '', c.name + (c.corrected ? ' (you corrected this)' : '')));
    if (c.role) head.appendChild(node('span', 'role', c.role));
    head.appendChild(node('span', 'pill imp-' + (c.importance || 'main'), c.importance || 'main'));
    card.appendChild(head);
    if (c.personality) card.appendChild(node('p', 'personality', c.personality));
    if (c.write_up) card.appendChild(node('p', '', c.write_up));
    const looks = Array.isArray(c.looks) ? c.looks : [];
    if (looks.length) {                          // 0.3.3: the proof for pictures, each look opens its passage
      const p = node('p', 'looks');
      p.appendChild(node('strong', '', 'Looks: '));
      looks.forEach((x, i) => {
        if (i) p.appendChild(document.createTextNode('; '));
        p.appendChild(document.createTextNode(x.detail + ' '));
        if (x.section !== null && x.section !== undefined) {
          const b = node('button', 'jump', '[' + x.section + ']'); b.type = 'button';
          b.title = 'Open section ' + x.section;
          b.onclick = () => jump(x.section, x.quote || x.detail, '');
          p.appendChild(b);
        }
      });
      card.appendChild(p);
    }
    const foot = node('div', 'card-foot');
    if (c.first_seen !== null && c.first_seen !== undefined) {
      const b = node('button', 'jump', 'First seen in section ' + c.first_seen); b.type = 'button';
      b.onclick = () => jump(c.first_seen, c.name, '');
      foot.appendChild(b);
    }
    const a = node('a', 'maps-link', 'See in Maps'); a.href = mapsHref('std-relationships', c.id);
    foot.appendChild(a);
    const ed = editButton(c.key);
    if (ed) foot.appendChild(ed);
    card.appendChild(foot);
    if ((S.tabData.rewriting || []).includes(c.key)) rewritingNote(card);
    host.appendChild(card);
  }

  function drawTabParts(d, bh, vh) {
    const cardAt = new Map(d.cards.map(c => [c.block, c]));
    const opts = {onJump: jump, maps: tabMaps(d.tab), onEdit: editButton};
    if (d.blocks.length) {
      d.blocks.forEach((b, i) => {
        if (cardAt.has(i)) { drawCard(cardAt.get(i), bh); return; }
        Blocks.render([b], bh, opts);
        const key = d.block_keys[String(i)];
        if (key && bh.lastChild) {                  // a paragraph of prose: its own Edit, and what is being rewritten
          const ed = editButton(key);
          if (ed) { const p = node('p', 'prose-foot'); p.appendChild(ed); bh.lastChild.appendChild(p); }
          if (d.rewriting.includes(key)) rewritingNote(bh.lastChild);
        }
      });
    } else {
      const st = (S.status || {}).state;
      bh.appendChild(node('p', 'empty', st === 'reading' ? 'Your bot is still reading. This tab fills in as it goes.'
        : st === 'done' ? 'Nothing for this tab in the story yet. Ask for something above.'
          : 'Start reading and your bot fills this tab.'));
    }
    drawEditExtras(d, bh);
    if (d.views.length) vh.appendChild(node('h3', 'views-h', 'Saved views'));
    for (const v of d.views) {
      const card = node('article', 'view');
      card.id = 'view-' + v.id;
      const head = node('div', 'view-head');
      head.appendChild(node('h4', '', v.title || 'A view'));
      const t = when(v.made_at);
      if (t) head.appendChild(node('small', '', t + (v.made_by === 'chat' ? ', from the chat' : '')));
      const x = node('button', 'x', 'x'); x.type = 'button';
      x.setAttribute('aria-label', 'Remove the view ' + (v.title || ''));
      x.title = 'Remove this view';
      x.onclick = () => removeView(v);
      head.appendChild(x);
      card.appendChild(head);
      if (v.asked) card.appendChild(node('p', 'asked', 'Asked: ' + v.asked));
      if (v.board) {                                  // this view has a drawing twin in Maps, by the same id
        const p = node('p', 'maps-line'), a = node('a', 'maps-link', 'See in Maps');
        a.href = mapsHref(v.board); p.appendChild(a); card.appendChild(p);
      }
      vh.appendChild(card);
      if (v.state === 'making') {
        const m = node('p', 'making');
        m.appendChild(node('span', 'spinner'));
        m.appendChild(document.createTextNode('Your bot is making this...'));
        card.appendChild(m);
      } else if (v.state === 'failed') {
        card.appendChild(node('p', 'say bad', v.why || 'Your bot could not make this view.'));
        const r = node('button', '', 'Retry'); r.type = 'button';
        r.onclick = () => retryView(v, r);
        card.appendChild(r);
      } else {
        try { Blocks.render(v.blocks, card, {onJump: jump, maps: v.board ? mapsHref(v.board) : tabMaps(v.tab)}); } catch (e) { card.appendChild(node('p', 'empty', 'This view could not be shown.')); }
      }
    }
  }

  // The same ask again; the failed card goes once the new one is in.
  async function retryView(v, btn) {
    btn.disabled = true;
    try {
      const got = await call('api/show', 'POST', {title: S.book, tab: v.tab, asked: v.asked});
      say($('asksay'), got.message || '');
      try { await call('api/views?' + q({title: S.book, id: v.id}), 'DELETE'); } catch (e) { /* it stays; the x removes it */ }
      if (got.view && got.view.made_at) S.newestView = String(got.view.made_at);
      await loadTab(got.view && got.view.id);
      soon();
    } catch (e) { say($('asksay'), e.message, true); btn.disabled = false; }
  }

  // While a view is being made, look every 3 seconds instead of every 10.
  function soon() {
    clearTimeout(S.fastTimer);
    S.fastTimer = setTimeout(pollViews, 3000);
  }

  async function removeView(v) {
    try {
      await call('api/views?' + q({title: S.book, id: v.id}), 'DELETE');
      loadTab();
    } catch (e) { say($('asksay'), e.message, true); }
  }

  // A view made by the chat: go to its tab and show it.
  async function pollViews() {
    if (!S.book || S.viewsBusy) return;
    S.viewsBusy = true;
    const title = S.book;
    let making = false;
    try {
      const got = await call('api/views?' + q({title: title}));
      if (title !== S.book) return;
      const views = got.views || [];
      making = views.some(v => v.state === 'making');
      // A view on the open tab that changed (made, failed, removed or new): draw the tab again, from api/tab.
      const states = {};
      for (const v of views) if (v.tab === S.tab) states[v.id] = v.state || 'ready';
      const was = S.viewStates;
      S.viewStates = states;
      const changed = was !== null && (Object.keys(states).length !== Object.keys(was).length ||
        Object.keys(states).some(id => was[id] !== states[id]));
      const top = views[0];
      const at = top ? String(top.made_at || '') : '';
      if (S.newestView === undefined) { S.newestView = at; return; }    // the first look: what was there already
      if (!top || at <= S.newestView) { if (changed) loadTab(); return; }
      S.newestView = at;
      if (top.tab === S.tab) loadTab(top.id);
      else if (!S.dirty) { S.tab = top.tab; S.viewStates = null; drawTabs(); S.tabData = null; loadTab(top.id); loadNotes(); }
      else { if (changed) loadTab(); say($('asksay'), 'A new view is under ' + tabLabel(top.tab) + '. Save your notes to see it.'); }
    } catch (e) { /* quiet */ } finally {
      S.viewsBusy = false;
      if (making) soon();
    }
  }

  $('ask').addEventListener('submit', async ev => {
    ev.preventDefault();
    const raw = $('askin').value.trim();
    if (!raw || !S.book) return;
    const asked = /^show me\b/i.test(raw) ? raw : 'show me ' + raw;
    const btn = $('askgo');
    btn.disabled = true;
    say($('asksay'), 'Sending it to your bot...');
    try {
      const got = await call('api/show', 'POST', {title: S.book, tab: S.tab, asked: asked});
      say($('asksay'), got.message || 'Done.');
      $('askin').value = '';
      const id = got.view && got.view.id;
      if (got.view && got.view.made_at) S.newestView = String(got.view.made_at);
      await loadTab(id);
      soon();
    } catch (e) { say($('asksay'), e.message, true); } finally { btn.disabled = false; }
  });

  // "Something wrong?", on every tab: her words go to api/correct. The plain shapes answer at once; the rest may take
  // your bot a while, and the tab shows it when it is done.
  $('correct').addEventListener('submit', async ev => {
    ev.preventDefault();
    const what = $('fixin').value.trim();
    if (!what || !S.book) return;
    const btn = $('fixgo');
    btn.disabled = true;
    say($('fixsay'), 'Your bot is reading that...');
    try {
      const got = await call('api/correct', 'POST', {title: S.book, what: what, tab: S.tab});
      say($('fixsay'), got.message || '');
      $('fixin').value = '';
      S.fixWatch = got.correction && got.correction.state === 'working' ? got.correction.id : null;
      await loadTab();
    } catch (e) { say($('fixsay'), e.message, true); } finally { btn.disabled = false; }
  });

  // ---------------------------------------------------------------- notes (as 0.1.0, with the stale-save refusal)

  async function loadNotes() {
    say($('say'), '');
    if (!S.book) return;
    try {
      const got = await call('api/notes?' + q({title: S.book, tab: S.tab}));
      $('pad').value = got.text || ''; S.stamp = got.stamp; S.dirty = false;
    } catch (e) { say($('say'), e.message, true); }
  }
  $('pad').addEventListener('input', () => { S.dirty = true; });
  $('reload').onclick = () => { if (leaveOk()) loadNotes(); };
  $('save').onclick = async () => {
    say($('say'), 'Saving...');
    try {
      const got = await call('api/notes', 'POST', {title: S.book, tab: S.tab, text: $('pad').value, stamp: S.stamp});
      S.stamp = got.stamp; S.dirty = false; say($('say'), got.message);
    } catch (e) { say($('say'), e.message, true); }
  };

  // ---------------------------------------------------------------- picker, upload, pointer bar

  $('book').onchange = () => {
    const v = $('book').value;
    if (v === S.book) return;
    if (!leaveOk()) { $('book').value = S.book; return; }
    S.dirty = false; S.tabData = null;
    pickBook(v);
  };
  $('sec').onchange = () => openSection($('sec').value);
  $('pnote').onclick = () => showPointer(true);
  $('pclear').onclick = async () => {
    try { await call('api/pointer?' + q({title: S.book}), 'DELETE'); } catch (e) { say($('upsay'), e.message, true); return; }
    S.pointer = null; hidePointer();
    if (S.sec) drawSection(null);
  };

  let asStudy = false;                       // the file picked next is a book to study: added with no reading
  $('upload').onclick = () => { asStudy = false; $('file').click(); };
  $('addstudy').onclick = () => { asStudy = true; $('file').click(); };
  $('remove').onclick = async () => {
    if (!S.book) return;
    if (!confirm('Put "' + S.book + '" away? Its folder moves into a Removed folder inside Manuscripts, with the original file. Nothing is deleted.')) return;
    try {
      const got = await call('api/manuscript?' + q({title: S.book}), 'DELETE');
      say($('upsay'), got.message || '');
      S.book = null; S.dirty = false; S.tabData = null;
      await loadList();
    } catch (e) { say($('upsay'), e.message, true); }
  };
  $('file').onchange = () => {
    const f = $('file').files[0];
    const study = asStudy;
    asStudy = false;
    $('file').value = '';
    if (!f) return;
    // The Core's page route takes 25 MB of base64, about 18.7 MB of file, and its refusal would only confuse.
    if (f.size > 18 * 1024 * 1024) {
      say($('upsay'), 'This file is too big for the Upload button. Send it to Document convert, in chat or on Your documents, then ask your bot to add it.', true);
      return;
    }
    say($('upsay'), 'Reading ' + f.name + '...');
    const rd = new FileReader();
    rd.onload = async () => {
      const b64 = String(rd.result).split(',', 2)[1] || '';
      try {
        const body = {file_name: f.name, content_b64: b64};
        if (study) body.read = false;
        const got = await call('api/upload', 'POST', body);
        say($('upsay'), got.message || '');
        const keep = S.book;
        await loadList();
        if (got.title && got.title !== keep && S.books.find(b => b.title === got.title) && leaveOk()) {
          S.dirty = false; S.tabData = null; pickBook(got.title);
          say($('upsay'), got.message || '');       // 0.3.5: picking clears the line, and this one is about the new book
        } else if (got.title === keep) loadStatus();
      } catch (e) { say($('upsay'), e.message, true); }
    };
    rd.onerror = () => say($('upsay'), 'That file could not be read here.', true);
    rd.readAsDataURL(f);
  };

  window.addEventListener('beforeunload', e => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });

  // Graphs and timelines are drawn to the width they have; draw again when that width changes a lot.
  let lastW = 0, resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      const w = $('blocks').clientWidth;
      if (Math.abs(w - lastW) > 60) { lastW = w; drawTab(); }
    }, 250);
  });

  // ---------------------------------------------------------------- settings (0.3.3)

  async function loadSettings() {
    try {
      const got = await call('api/settings');
      $('vaultfree').checked = (got.settings || {}).vault_free !== false;
    } catch (e) { say($('setsay'), e.message, true); }
  }
  $('vaultfree').onchange = async () => {
    const box = $('vaultfree');
    box.disabled = true;
    try {
      const got = await call('api/settings', 'POST', {vault_free: box.checked});
      say($('setsay'), got.message || '');
      loadStatus();
    } catch (e) { box.checked = !box.checked; say($('setsay'), e.message, true); }
    box.disabled = false;
  };
  loadSettings();

  async function loadSize() {
    if (!S.book || !$('settings').open) return;
    try { $('booksize').textContent = (await call('api/size?' + q({title: S.book}))).line || ''; }
    catch (e) { $('booksize').textContent = ''; }
  }
  $('settings').addEventListener('toggle', loadSize);

  if (FIXTURE) $('sample').hidden = false;
  setInterval(pollPointer, 2000);
  setInterval(pollViews, 10000);
  loadList().then(() => { lastW = $('blocks').clientWidth; });
})();
