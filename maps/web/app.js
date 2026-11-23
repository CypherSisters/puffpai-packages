/* Maps 2.2.0, the page. A manuscript opens on its atlas: Cast, World, Family, Story, Cover, and the eight charts
   folded under Charts. The picture program paints the pictures; this page puts the words, pins and lines on top.
   The server builds the atlas (atlas.py) and lays out every chart (draw.py); boards.js draws the charts.

   Every call goes to the page's own origin with no token; the PuffPAI Desktop's proxy adds the key.
   Open with ?title=<manuscript> to land on its atlas, or ?title=<manuscript>&board=<board id> on one chart
   (Manuscript's "See in Maps").

   Fixture mode: open the page with ?fixture=1 and no server is needed. Every call reads a file under fixtures/
   (next to web/, found at <base>/../fixtures/ when the page is at .../web/, else <base>/fixtures/):

     GET    api/list                    fixtures/list.sample.json
     GET    api/atlas?title             fixtures/atlas.sample.json, with fixtures/illustrate.sample.json as its
                                        progress (the picture named there shows its spinner)
     GET    api/illustrate?title        fixtures/illustrate.sample.json
     GET    api/boards?title&id         fixtures/boards.sample.json, the board with that id
     GET    api/bible?title             fixtures/bible.sample.json
     POST   api/point                   says which section Manuscript would show; nothing is written
     POST   api/boards/<id>/layout      kept in memory only
     DELETE api/boards?title&id         kept in memory only
     POST   api/style                   kept in memory only
     POST   api/draw                    refused with a plain message: the sample page draws no new boards
     GET    api/maker                   no address set
     GET    api/pictures?title          no pictures
     POST   api/maker, api/maker/test,
            api/picture, api/illustrate refused with a plain message: the sample page makes no pictures

   Pictures are fetched as blobs (the page's CSP allows blob: images only). Everything renders without any picture.
*/
(function () {
  'use strict';
  const FIXTURE = new URLSearchParams(location.search).get('fixture') === '1';
  const BASE = location.pathname.replace(/\/[^/]*\.[^/]*$/, '/').replace(/([^/])$/, '$1/');
  const FIX = (/\/web\/$/.test(BASE) ? BASE.replace(/web\/$/, '') : BASE) + 'fixtures/';
  const $ = id => document.getElementById(id);
  const S = {books: [], standard: [], title: null, board: null, view: null, moved: false,
             mode: 'list', atlas: null, atlasRaw: '', page: null, worldMoved: false, worldPos: null,
             gallery: null, pics: [], bible: null, blobs: {}, poll: null, maker: null,
             views: {}};                       // each board's zoom and pan, while the page is open
  const KINDS = {portrait: 'Portrait', place: 'Place', worldmap: 'World map', scene: 'Scene', cover: 'Cover'};

  // ---------------------------------------------------------------- calls

  async function call(path, method, body) {
    method = method || (body === undefined ? 'GET' : 'POST');
    if (FIXTURE) return fixture(path, method, body);
    const opts = {method: method};
    if (body !== undefined) { opts.headers = {'Content-Type': 'application/json'}; opts.body = JSON.stringify(body); }
    const gone = 'Maps did not answer. Open Apps to see why.';
    let r;
    try { r = await fetch(BASE + path, opts); } catch (e) { throw new Error(gone); }
    let out = null;
    try { out = await r.json(); } catch (e) { out = null; }
    if (!r.ok) throw new Error((out && (out.error || out.message)) || gone);
    return out || {};
  }
  async function blob(path) {
    const r = await fetch(BASE + path);
    if (!r.ok) throw new Error('That picture did not load.');
    return r.blob();
  }
  const q = o => Object.entries(o).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&');

  const F = {removed: new Set(), layouts: {}, style: null};
  async function fixture(path, method, body) {
    const [route, qs] = path.split('?');
    const p = new URLSearchParams(qs || '');
    const get = async name => {
      const r = await fetch(FIX + name, {cache: 'no-store'});
      if (!r.ok) throw new Error('The sample file ' + name + ' is missing.');
      return r.json();
    };
    if (route === 'api/list') {
      const d = await get('list.sample.json');
      for (const m of d.manuscripts || []) m.boards = (m.boards || []).filter(b => !F.removed.has(b.id));
      return d;
    }
    if (route === 'api/atlas') {
      const a = await get('atlas.sample.json');
      a.progress = await get('illustrate.sample.json');
      for (const c of a.cast || []) if (c.name === a.progress.now) c.making = true;
      if (F.style !== null) { a.style = F.style || a.default_style; a.own_style = !!F.style; }
      const lay = F.layouts['std-world'];
      if (lay) {
        for (const pin of a.world.pins || []) if (lay[pin.id]) [pin.x, pin.y] = lay[pin.id];
        for (const n of a.world.spec.nodes || []) if (lay[n.id]) [n.x, n.y] = lay[n.id];
      }
      return a;
    }
    if (route === 'api/illustrate' && method === 'GET') return {progress: await get('illustrate.sample.json')};
    if (route === 'api/style') { F.style = (body.style || '').trim(); return {style: F.style, message: 'Kept on this sample page only. Nothing was written.'}; }
    if (route === 'api/bible') return get('bible.sample.json');
    if (route === 'api/boards' && method === 'GET') {
      const all = (await get('boards.sample.json')).boards || [];
      const id = p.get('id');
      if (!id) return {boards: all.filter(b => !/^std-/.test(b.id) && !F.removed.has(b.id))};
      const b = all.find(x => x.id === id);
      if (!b || F.removed.has(id)) throw new Error('That board is not there any more.');
      if (F.layouts[id]) for (const n of b.spec.nodes || []) if (F.layouts[id][n.id]) [n.x, n.y] = F.layouts[id][n.id];
      return {board: b};
    }
    if (route === 'api/boards' && method === 'DELETE') {
      const id = p.get('id');
      if (/^std-/.test(id)) { delete F.layouts[id]; return {message: 'Back to the drawn layout. (Sample page: nothing was written.)'}; }
      F.removed.add(id); return {message: 'Removed that board. (Sample page: nothing was written.)'};
    }
    const lay = /^api\/boards\/([^/]+)\/layout$/.exec(route);
    if (lay) { F.layouts[decodeURIComponent(lay[1])] = body.positions; return {message: 'Kept on this sample page only. Nothing was written.'}; }
    if (route === 'api/point') return {message: 'On the real page, Manuscript would now show section ' + body.section + '. Nothing was written here.'};
    if (route === 'api/draw') throw new Error('This is the sample page, so it draws no new boards. The standard eight are all here.');
    if (route === 'api/maker' && method === 'GET') return {address: '', style: 'painterly, soft light, book illustration', ask: null};
    if (route === 'api/pictures' && method === 'GET') return {pictures: [], kinds: KINDS};
    if (['api/maker', 'api/maker/test', 'api/picture', 'api/illustrate'].includes(route)) throw new Error('This is the sample page, so it makes no pictures.');
    throw new Error('The sample page has nothing for ' + route + '.');
  }

  // ---------------------------------------------------------------- small helpers

  function say(e, text, bad) { e.textContent = text || ''; e.className = 'say' + (bad ? ' bad' : ''); }
  function node(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function button(text, cls, fn) {
    const b = node('button', cls || '', text);
    b.type = 'button';
    if (fn) b.addEventListener('click', fn);
    return b;
  }
  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  }
  const cap = s => (s ? s[0].toUpperCase() + s.slice(1) : '');
  function picURL(pid) {
    if (S.blobs[pid]) return Promise.resolve(S.blobs[pid]);
    return blob('api/picture/' + encodeURIComponent(pid) + '.png?' + q({title: S.title}))
      .then(b => { S.blobs[pid] = S.blobs[pid] || URL.createObjectURL(b); return S.blobs[pid]; });
  }

  // ---------------------------------------------------------------- the list

  async function loadList() {
    try {
      const d = await call('api/list');
      S.books = d.manuscripts || []; S.standard = d.standard || [];
      drawList();
    } catch (e) { $('books').textContent = ''; say($('listsay'), e.message, true); }
  }

  function drawList() {
    const box = $('books');
    box.textContent = '';
    say($('listsay'), '');
    if (!S.books.length) {
      box.appendChild(node('p', 'empty', 'No manuscript has been read yet. Add one in Manuscript and let your bot read it; it shows here.'));
      return;
    }
    for (const m of S.books) {
      const sec = node('section', 'book');
      const open = button(m.title, 'open title', () => openAtlas(m.title));
      if (S.title === m.title && S.mode !== 'list') open.classList.add('chosen');
      sec.appendChild(open);
      let meta = 'Read ' + (m.sections_read != null ? m.sections_read : '?') + ' of ' + (m.sections_total != null ? m.sections_total : '?') + ' sections';
      if (m.built_with) meta += ' by ' + m.built_with + ' on this computer';
      meta += '.';
      if (m.corrections) meta += ' ' + m.corrections + (m.corrections === 1 ? ' correction' : ' corrections') + ' from you in Manuscript.';
      sec.appendChild(node('p', 'meta', meta));
      box.appendChild(sec);
    }
  }

  function chartItem(title, b, saved) {
    const li = node('li');
    const open = button(b.title, 'open', () => openBoard(title, b.id));
    if (saved) {
      const sub = (b.asked ? '"' + b.asked + '"' : '') + (b.made_at ? ' ' + when(b.made_at) : '') +
        (b.tab ? ', also under ' + b.tab + ' in Manuscript' : '');
      if (sub.trim()) open.appendChild(node('span', 'asked', sub.trim()));
    }
    li.appendChild(open);
    if (saved) {
      const x = button('x', 'x', () => removeBoard(title, b));
      x.title = 'Remove this board'; x.setAttribute('aria-label', 'Remove ' + b.title);
      li.appendChild(x);
    }
    return li;
  }

  function askForm(title) {
    const f = node('form', 'askform');
    f.autocomplete = 'off';
    const label = node('label', '', 'Draw');
    const id = 'ask-' + Math.random().toString(36).slice(2);
    label.htmlFor = id;
    const inp = node('input');
    inp.type = 'text'; inp.id = id; inp.maxLength = 300; inp.placeholder = 'who interacts with whom';
    const go = node('button', 'main', 'Draw');
    go.type = 'submit';
    f.append(label, inp, go);
    f.addEventListener('submit', async e => {
      e.preventDefault();
      const asked = inp.value.trim();
      if (!asked) return;
      try {
        const d = await call('api/draw', 'POST', {title: title, kind: '', asked: asked});
        inp.value = '';
        await loadList();
        await openBoard(title, d.board.id);
        say($('bsay'), d.message);
      } catch (x) { say($('bsay'), x.message, true); }
    });
    return f;
  }

  async function removeBoard(title, b) {
    if (!confirm('Remove the board "' + b.title + '"? The manuscript is not touched.')) return;
    try {
      const d = await call('api/boards?' + q({title: title, id: b.id}), 'DELETE');
      await loadList();
      await openAtlas(title);
      say($('bsay'), d.message);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  // ---------------------------------------------------------------- the pane: one mode at a time

  function setMode(mode) {
    S.mode = mode;
    const wrap = $('wrap');
    wrap.classList.toggle('on-board', mode !== 'list');
    wrap.classList.toggle('mode-atlas', mode === 'atlas');
    $('atlas').hidden = !['list', 'atlas', 'person', 'place'].includes(mode);
    $('gallery').hidden = mode !== 'gallery';
    $('board').hidden = mode !== 'board';
    $('legend').hidden = mode !== 'board';
    $('tools').hidden = mode !== 'board';
    $('back').textContent = mode === 'atlas' || mode === 'list' ? 'Back' : 'Back to ' + (S.title || 'the atlas');
    if (mode !== 'board') { S.board = null; S.view = null; }
    if (S.poll && mode !== 'gallery' && mode !== 'atlas' && mode !== 'person' && mode !== 'place') { clearTimeout(S.poll); S.poll = null; }
  }

  function leaving() {
    if ((S.moved || S.worldMoved) && !confirm('You moved things and did not save. Leave?')) return false;
    S.moved = false; S.worldMoved = false;
    return true;
  }

  // ---------------------------------------------------------------- the atlas

  async function openAtlas(title, keepScroll) {
    if (!keepScroll && !leaving()) return;
    S.title = title; S.page = null;
    setMode('atlas');
    $('btitle').textContent = title;
    $('bnote').textContent = '';
    if (!keepScroll) say($('bsay'), '');
    drawList();
    await loadAtlas(true);
  }

  async function loadAtlas(force) {
    const title = S.title;
    if (S.poll) { clearTimeout(S.poll); S.poll = null; }
    let d, raw;
    try { d = await call('api/atlas?' + q({title: title})); raw = JSON.stringify(d); }
    catch (e) { say($('bsay'), e.message, true); return; }
    if (S.title !== title || !['atlas', 'person', 'place'].includes(S.mode)) return;
    const changed = raw !== S.atlasRaw;
    S.atlas = d; S.atlasRaw = raw;
    if (force || changed) {
      if (S.mode === 'atlas' && !S.worldMoved) drawAtlas();
      else if (S.mode === 'atlas') drawProgress();
      else if (S.mode === 'person') drawPerson(S.page);
      else if (S.mode === 'place') drawPlace(S.page);
    }
    const busy = (d.progress && d.progress.state === 'running') || (d.cast || []).concat(d.world.pins || [], d.story.cards || [], [d.world, d.cover]).some(x => x.making);
    if (busy && !FIXTURE) S.poll = setTimeout(() => loadAtlas(false), 3000);
  }

  function keepScroll(fn) {
    const box = $('atlas'), top = box.scrollTop;
    const strip = box.querySelector('.strip'), left = strip ? strip.scrollLeft : 0;
    fn();
    box.scrollTop = top;
    const s2 = box.querySelector('.strip');
    if (s2) s2.scrollLeft = left;
  }

  function toMaker() {
    $('wrap').classList.remove('on-board');
    $('maddr').focus();
    $('maddr').scrollIntoView({block: 'center'});
  }

  // One picture slot: the picture, or a soft placeholder with the subject's name and Paint; the spinner while it is
  // being made. kind and who say what Paint makes.
  function slot(item, name, kind, who, shape) {
    const box = node('div', 'slot ' + (shape || 'square'));
    if (item.picture) {
      const img = node('img');
      img.alt = name;
      box.appendChild(img);
      picURL(item.picture).then(u => { img.src = u; }).catch(() => { box.classList.add('broken'); });
      if (item.making) { box.appendChild(node('span', 'spin over')); }
      return box;
    }
    box.classList.add('empty-slot');
    box.appendChild(node('span', 'who', name));
    if (item.making) {
      box.appendChild(node('span', 'spin'));
      box.appendChild(node('span', 'small', 'Painting...'));
    } else if (!S.atlas.maker) {
      box.appendChild(node('span', 'small', 'No picture maker yet'));
      box.appendChild(button('Picture maker', 'ghost', e => { e.stopPropagation(); toMaker(); }));
    } else {
      if (item.why) box.appendChild(node('span', 'why', item.why));
      box.appendChild(button(item.why ? 'Paint again' : 'Paint', 'ghost', e => { e.stopPropagation(); paint(kind, who); }));
    }
    return box;
  }

  // Paint again, with a small note field, under every picture that exists.
  function again(item) {
    if (!item.picture || !S.atlas.maker) return node('span');
    const d = node('details', 'again');
    d.appendChild(node('summary', '', 'Paint again'));
    const f = node('form');
    f.autocomplete = 'off';
    const inp = node('input');
    inp.type = 'text'; inp.maxLength = 200; inp.placeholder = 'a note, like older, grey hair';
    inp.value = item.note || '';
    inp.setAttribute('aria-label', 'A note for the picture maker');
    const go = node('button', 'main', 'Paint');
    go.type = 'submit';
    f.append(inp, go);
    f.addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const r = await call('api/picture', 'POST', {title: S.title, again: item.picture, note: inp.value.trim()});
        say($('bsay'), r.message);
        await loadAtlas(true);
      } catch (x) { say($('bsay'), x.message, true); }
    });
    d.appendChild(f);
    d.addEventListener('click', e => e.stopPropagation());
    return d;
  }

  // The proof under a picture: the words from the manuscript that told the painter what to draw, one line per owner,
  // each "(section n)" a link that opens Manuscript at those words. A fold shows the whole prompt and the style line.
  function proof(item, kind) {
    const box = node('div', 'proof');
    box.addEventListener('click', e => e.stopPropagation());
    box.addEventListener('keydown', e => e.stopPropagation());
    const list = (item && item.proof) || [];
    const sectioned = list.some(e => e.section != null);
    if (sectioned) {
      const owners = [];
      for (const e of list) if (!owners.includes(e.owner)) owners.push(e.owner);
      for (const o of owners) {
        const line = node('p', 'pline');
        line.appendChild(node('strong', '', cap(o) + ': '));
        list.filter(e => e.owner === o).forEach((e, i) => {
          if (i) line.appendChild(document.createTextNode(', '));
          line.appendChild(document.createTextNode(e.detail));
          if (e.section != null) {
            line.appendChild(document.createTextNode(' ('));
            const a = button('section ' + e.section, 'seclink', () => point({title: o, point: {section: e.section,
              quote: e.quote || '', note: o + ': ' + e.detail + '. From the proof under a picture in Maps.'}}));
            a.title = e.quote ? '"' + e.quote + '"' : 'Open section ' + e.section + ' in Manuscript';
            line.appendChild(a);
            line.appendChild(document.createTextNode(')'));
          }
        });
        box.appendChild(line);
      }
    } else if (kind === 'cover') {
      box.appendChild(node('p', 'pline', 'Painted from the one-line summary and the themes.'));
    } else {
      box.appendChild(node('p', 'pline', 'Painted from the role and traits; read the manuscript again in Manuscript for details with sections.'));
      if (list.length) box.appendChild(node('p', 'pline', 'Used: ' + list.map(e => e.detail).join(', ') + '.'));
    }
    if (item && item.prompt) {
      const d = node('details', 'told');
      d.appendChild(node('summary', '', 'What the painter was told'));
      d.appendChild(node('p', 'prompt', item.prompt));
      if (item.told_style) d.appendChild(node('p', 'small', 'Style line: ' + item.told_style));
      box.appendChild(d);
    }
    return box;
  }

  async function paint(kind, who) {
    try {
      const r = await call('api/picture', 'POST', {title: S.title, kind: kind, who: who || ''});
      say($('bsay'), r.message);
      await loadAtlas(true);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  async function point(it) {
    const p = (it && it.point) || {};
    if (p.section == null) { say($('bsay'), 'There is no section to open for that.'); return; }
    try {
      const d = await call('api/point', 'POST', {title: S.title, section: p.section, quote: p.quote || '', note: p.note || ''});
      say($('bsay'), (it.title ? it.title + '. ' : '') + d.message);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  function section(box, title, id) {
    const s = node('section', 'part');
    s.id = id;
    s.appendChild(node('h3', '', title));
    box.appendChild(s);
    return s;
  }

  function drawProgress() {
    const line = $('iline');
    if (!line) return;
    const p = S.atlas.progress;
    say(line, p ? p.line : '', p && p.state === 'stopped');
    const probs = $('iprobs');
    probs.textContent = '';
    if (p && p.problems && p.problems.length && p.state !== 'running') {
      probs.hidden = false;
      probs.appendChild(node('summary', '', p.problems.length + ' could not be painted'));
      const ul = node('ul');
      for (const x of p.problems) ul.appendChild(node('li', '', x));
      probs.appendChild(ul);
    } else probs.hidden = true;
    const go = $('ibtn');
    if (go) go.disabled = !!(p && p.state === 'running');
  }

  function drawAtlas() {
    keepScroll(() => {
      const a = S.atlas, box = $('atlas');
      box.textContent = '';
      // the head: the one-line summary, Illustrate, and the style line
      const head = node('div', 'ahead');
      if (a.one_line) head.appendChild(node('p', 'oneline', a.one_line));
      const row = node('div', 'row');
      if (a.maker) {
        const go = button('Illustrate', 'main', async () => {
          try {
            const r = await call('api/illustrate', 'POST', {title: S.title, minor: $('iminor').checked});
            say($('bsay'), r.message);
            await loadAtlas(true);
          } catch (e) { say($('bsay'), e.message, true); }
        });
        go.id = 'ibtn';
        const lab = node('label', 'check');
        const cb = node('input');
        cb.type = 'checkbox'; cb.id = 'iminor';
        lab.append(cb, document.createTextNode(' minor characters too'));
        row.append(go, lab);
      } else {
        row.append(node('span', 'small', 'No picture maker yet, so every picture slot waits.'), button('Picture maker', '', toMaker));
      }
      head.appendChild(row);
      const line = node('p', 'say');
      line.id = 'iline'; line.setAttribute('aria-live', 'polite');
      head.appendChild(line);
      const probs = node('details', 'problems');
      probs.id = 'iprobs'; probs.hidden = true;
      head.appendChild(probs);
      const sf = node('form', 'stylef');
      sf.autocomplete = 'off';
      const sl = node('label', '', 'Style');
      sl.htmlFor = 'astyle';
      const si = node('input');
      si.type = 'text'; si.id = 'astyle'; si.maxLength = 200; si.value = a.style || ''; si.placeholder = a.default_style || '';
      const sb = node('button', '', 'Save');
      sb.type = 'submit';
      sf.append(sl, si, sb);
      sf.addEventListener('submit', async e => {
        e.preventDefault();
        try { const r = await call('api/style', 'POST', {title: S.title, style: si.value.trim()}); say($('bsay'), r.message); await loadAtlas(true); }
        catch (x) { say($('bsay'), x.message, true); }
      });
      head.appendChild(sf);
      head.appendChild(node('p', 'small', 'The style line joins every picture of this book, so the cast and the world look like one book.'));
      box.appendChild(head);

      drawCast(section(box, 'Cast', 'a-cast'));
      drawWorld(section(box, 'World', 'a-world'));
      drawFamily(section(box, 'Family', 'a-family'));
      drawStory(section(box, 'Story', 'a-story'));
      drawCover(section(box, 'Cover', 'a-cover'));
      drawCharts(box);
      drawProgress();
    });
  }

  function drawCast(s) {
    const a = S.atlas;
    if (!a.cast.length) { s.appendChild(node('p', 'empty', 'The story bible has no characters yet.')); return; }
    const grid = node('div', 'cast');
    for (const c of a.cast) {
      const f = node('figure', 'person');
      f.tabIndex = 0;
      f.setAttribute('role', 'button');
      f.setAttribute('aria-label', 'Open ' + c.name);
      f.appendChild(slot(c, c.name, 'portrait', c.id, 'tall'));
      const fc = node('figcaption');
      fc.appendChild(node('strong', '', c.name));
      fc.appendChild(node('span', 'small', [c.role, c.importance].filter(Boolean).join(', ')));
      f.appendChild(fc);
      if (c.picture) f.appendChild(proof(c, 'portrait'));
      const open = () => openPerson(c.id);
      f.addEventListener('click', open);
      f.addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target === f) { e.preventDefault(); open(); } });
      grid.appendChild(f);
    }
    s.appendChild(grid);
  }

  // ---------------------------------------------------------------- world

  function drawWorld(s) {
    const w = S.atlas.world;
    if (!w.pins.length) { s.appendChild(node('p', 'empty', 'The story bible has no places yet.')); return; }
    const tools = node('div', 'row');
    const save = button('Save layout', 'main', saveWorld);
    save.id = 'wsave'; save.hidden = true;
    tools.appendChild(save);
    const holder = node('div', 'worldbox');
    s.appendChild(holder);
    if (w.picture) {
      worldPicture(holder, w);
      tools.appendChild(again(w));
      if (w.making) tools.appendChild(node('span', 'small', 'Painting the map again...'));
      s.appendChild(proof(w, 'worldmap'));
    } else {
      const b = node('div', 'board schematic');
      holder.appendChild(b);
      S.worldView = MapsBoards.draw(b, w.spec, {onPoint: it => openPlace(it.id), onMoved: () => { S.worldMoved = true; save.hidden = false; }});
      S.worldPos = null;
      const bar = node('div', 'paintbar');
      if (w.making) bar.append(node('span', 'spin'), node('span', 'small', 'Painting the map...'));
      else if (!S.atlas.maker) bar.append(node('span', 'small', 'No picture maker yet. This is the drawn schematic.'), button('Picture maker', '', toMaker));
      else {
        if (w.why) bar.appendChild(node('span', 'why', w.why));
        bar.appendChild(button('Paint the map', '', () => paint('worldmap', '')));
      }
      holder.appendChild(bar);
    }
    s.appendChild(tools);
    const chips = node('div', 'chips');
    for (const p of w.pins) chips.appendChild(button(p.name, 'chip', () => openPlace(p.id)));
    s.appendChild(chips);
  }

  // The painted map, drawn to fill the board, with the pins in SVG over it at the saved world layout scaled into the
  // picture. Pins drag; a click opens the place.
  function worldPicture(holder, w) {
    const NS = 'http://www.w3.org/2000/svg';
    const mk = (tag, attrs, parent) => {
      const e = document.createElementNS(NS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      if (parent) parent.appendChild(e);
      return e;
    };
    const W = Math.max(w.width || 1, 1), H = Math.max(w.height || 1, 1);
    const svg = mk('svg', {role: 'img', 'aria-label': 'The world map with its places'}, holder);
    svg.classList.add('worldpic');
    S.worldPos = {};
    picURL(w.picture).then(url => {
      const probe = new Image();
      probe.onload = () => {
        const VW = probe.naturalWidth || 1024, VH = probe.naturalHeight || 640;
        svg.setAttribute('viewBox', '0 0 ' + VW + ' ' + VH);
        svg.style.aspectRatio = VW + ' / ' + VH;
        const im = mk('image', {x: 0, y: 0, width: VW, height: VH, preserveAspectRatio: 'none'}, svg);
        im.setAttribute('href', url);
        const fs = Math.max(VW / 55, 10), pr = Math.max(VW / 110, 5);
        // the text's distances and directions: a thin dashed line between the two places, the note as its label
        const rg = mk('g', {class: 'wroutes'}, svg), at2 = {};
        const lines = (w.routes || []).map(r => {
          const g = mk('g', {class: 'wroute'}, rg);
          const tt = mk('title', {}, g); tt.textContent = (r.relation || r.note) + (r.section != null ? ' (section ' + r.section + ')' : '');
          const ln = mk('line', {class: 'wline', 'stroke-width': Math.max(VW / 600, 1.5), 'stroke-dasharray': (VW / 120) + ' ' + (VW / 160)}, g);
          const tx = mk('text', {'text-anchor': 'middle', class: 'wrlabel', 'font-size': fs * 0.8}, g);
          tx.textContent = r.note.length > 40 ? r.note.slice(0, 37) + '...' : r.note;
          return {r: r, ln: ln, tx: tx};
        });
        const drawLines = () => {
          for (const x of lines) {
            const a = at2[x.r.from], b = at2[x.r.to];
            if (!a || !b) continue;
            x.ln.setAttribute('x1', a[0]); x.ln.setAttribute('y1', a[1]); x.ln.setAttribute('x2', b[0]); x.ln.setAttribute('y2', b[1]);
            x.tx.setAttribute('x', (a[0] + b[0]) / 2); x.tx.setAttribute('y', (a[1] + b[1]) / 2 - fs * 0.3);
          }
        };
        for (const p of w.pins) {
          const g = mk('g', {class: 'wpin', tabindex: '0', role: 'button', 'aria-label': p.name}, svg);
          const t = mk('title', {}, g); t.textContent = p.name + (p.kind ? ', ' + p.kind : '');
          const dot = mk('circle', {r: pr, class: 'wdot'}, g);
          const label = p.name.length > 28 ? p.name.slice(0, 25) + '...' : p.name;
          const pw = label.length * fs * 0.56 + fs, ph = fs * 1.45;
          const pill = mk('rect', {width: pw, height: ph, rx: ph / 2, class: 'wpill'}, g);
          const tx = mk('text', {'text-anchor': 'middle', class: 'wlabel', 'font-size': fs}, g);
          tx.textContent = label;
          const put = (x, y) => {
            x = Math.min(Math.max(x, pw / 2), VW - pw / 2); y = Math.min(Math.max(y, pr), VH - pr - ph - 4);
            dot.setAttribute('cx', x); dot.setAttribute('cy', y);
            pill.setAttribute('x', x - pw / 2); pill.setAttribute('y', y + pr + 3);
            tx.setAttribute('x', x); tx.setAttribute('y', y + pr + 3 + ph * 0.72);
            S.worldPos[p.id] = [Math.round(x / VW * W * 10) / 10, Math.round(y / VH * H * 10) / 10];
            g._xy = [x, y];
            at2[p.id] = [x, y];
            drawLines();
          };
          put(p.x / W * VW, p.y / H * VH);
          let drag = null;
          const at = e => { const m = svg.getScreenCTM(); const pt = svg.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; return m ? pt.matrixTransform(m.inverse()) : pt; };
          g.addEventListener('pointerdown', e => {
            if (e.button !== 0) return;
            const s0 = at(e);
            drag = {sx: s0.x, sy: s0.y, x: g._xy[0], y: g._xy[1], moved: false};
            g.setPointerCapture(e.pointerId);
          });
          g.addEventListener('pointermove', e => {
            if (!drag) return;
            const s1 = at(e), dx = s1.x - drag.sx, dy = s1.y - drag.sy;
            if (!drag.moved && Math.abs(dx) + Math.abs(dy) < VW / 200) return;
            drag.moved = true;
            put(drag.x + dx, drag.y + dy);
          });
          const end = e => {
            if (!drag) return;
            const d = drag; drag = null;
            try { g.releasePointerCapture(e.pointerId); } catch (x) { /* already released */ }
            if (e.type !== 'pointerup') return;
            if (d.moved) { S.worldMoved = true; $('wsave').hidden = false; } else openPlace(p.id);
          };
          g.addEventListener('pointerup', end);
          g.addEventListener('pointercancel', end);
          g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPlace(p.id); } });
        }
      };
      probe.src = url;
    }).catch(e => { holder.textContent = ''; holder.appendChild(node('p', 'why', e.message)); });
  }

  async function saveWorld() {
    const positions = S.worldPos || (S.worldView ? S.worldView.positions() : null);
    if (!positions) return;
    try {
      const d = await call('api/boards/std-world/layout', 'POST', {title: S.title, positions: positions});
      S.worldMoved = false;
      say($('bsay'), d.message);
      await loadAtlas(true);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  // ---------------------------------------------------------------- family, story, cover, charts

  function drawFamily(s) {
    const spec = S.atlas.family.spec;
    if (!spec || !(spec.nodes || []).length) { s.appendChild(node('p', 'empty', (spec && spec.note) || 'No family to draw yet.')); return; }
    const b = node('div', 'board family');
    s.appendChild(b);
    const faces = {};
    const draw = () => MapsBoards.draw(b, spec, {onPoint: it => openPerson(it.id), portraits: faces});
    draw();
    const want = S.atlas.cast.filter(c => c.picture && spec.nodes.some(n => n.id === c.id));
    if (want.length) Promise.all(want.map(c => picURL(c.picture).then(u => { faces[c.id] = u; }).catch(() => null))).then(draw);
  }

  function drawStory(s) {
    const st = S.atlas.story;
    if (!st.cards.length) { s.appendChild(node('p', 'empty', 'The story bible has no events yet.')); return; }
    const strip = node('div', 'strip');
    const rail = node('ol', 'rail');
    for (const c of st.cards) {
      const li = node('li', 'scard');
      li.appendChild(node('span', 'tick', c.section != null ? 'Section ' + c.section : ''));
      li.appendChild(slot(c, c.text, 'scene', c.id, 'wide'));
      li.appendChild(node('p', 'caption', c.text));
      if (c.picture) li.appendChild(proof(c, 'scene'));
      const meta = [c.when, c.where_name].filter(Boolean).join(', ');
      if (meta) li.appendChild(node('p', 'small', meta));
      const acts = node('span', 'row');
      if (c.point) acts.appendChild(button('Section ' + c.section, '', () => point({title: c.text, point: c.point})));
      acts.appendChild(again(c));
      li.appendChild(acts);
      rail.appendChild(li);
    }
    strip.appendChild(rail);
    s.appendChild(strip);
  }

  function drawCover(s) {
    const c = S.atlas.cover;
    const f = node('figure', 'cover');
    const sl = slot(c, c.title, 'cover', '', 'tall');
    if (c.picture) {                     // the words are put on by code, never by the picture maker
      const over = node('div', 'covertext');
      over.appendChild(node('span', 'ctitle', c.title));
      sl.appendChild(over);
    } else {
      if (c.one_line) sl.appendChild(node('span', 'small', c.one_line));
    }
    f.appendChild(sl);
    const fc = node('figcaption');
    if (c.one_line) fc.appendChild(node('p', '', c.one_line));
    if (c.themes.length) fc.appendChild(node('p', 'small', 'Themes: ' + c.themes.join(', ')));
    if (c.picture) fc.appendChild(proof(c, 'cover'));
    fc.appendChild(again(c));
    f.appendChild(fc);
    s.appendChild(f);
  }

  function drawCharts(box) {
    const d = node('details', 'charts');
    d.appendChild(node('summary', '', 'Charts'));
    d.appendChild(node('p', 'small', 'The drawn boards: the relationship map, touchpoints, arcs, the structure, the thread board and the rest. Click anything on one and Manuscript opens that passage.'));
    const ul = node('ul', 'chartlist');
    for (const b of S.standard) ul.appendChild(chartItem(S.title, b, false));
    const pli = node('li');
    const pb = button('All pictures', 'open', () => openPictures(S.title));
    pb.appendChild(node('span', 'asked', 'every picture made for this book, with what was sent'));
    pli.appendChild(pb);
    ul.appendChild(pli);
    d.appendChild(ul);
    const m = S.books.find(x => x.title === S.title);
    if (m && (m.boards || []).length) {
      d.appendChild(node('h4', '', 'Drawn from asks'));
      const ul2 = node('ul', 'chartlist');
      for (const b of m.boards) ul2.appendChild(chartItem(S.title, b, true));
      d.appendChild(ul2);
    }
    d.appendChild(askForm(S.title));
    box.appendChild(d);
  }

  // ---------------------------------------------------------------- a character's page and a place's page

  function pageHead(box, name) {
    const top = node('div', 'row');
    top.appendChild(button('Back to the atlas', '', () => openAtlas(S.title, true)));
    box.appendChild(top);
    box.appendChild(node('h3', 'pname', name));
  }

  function face(c) {
    const f = node('span', 'face');
    if (c && c.picture) {
      const img = node('img'); img.alt = '';
      f.appendChild(img);
      picURL(c.picture).then(u => { img.src = u; }).catch(() => null);
    } else f.textContent = (c && c.name ? c.name[0] : '?');
    return f;
  }

  function openPerson(id) {
    if (!leaving()) return;
    S.page = id;
    setMode('person');
    drawPerson(id);
    $('atlas').scrollTop = 0;
  }

  function drawPerson(id) {
    const a = S.atlas, c = a.cast.find(x => x.id === id), box = $('atlas');
    box.textContent = '';
    if (!c) { pageHead(box, 'Not in the story bible any more'); return; }
    pageHead(box, c.name);
    const lay = node('div', 'page');
    const left = node('div', 'pleft');
    left.appendChild(slot(c, c.name, 'portrait', c.id, 'tall big'));
    if (!c.picture) left.appendChild(node('p', 'small', 'When it is painted, the painter is told this:'));
    left.appendChild(proof(c, 'portrait'));
    left.appendChild(again(c));
    lay.appendChild(left);
    const right = node('div', 'pright');
    right.appendChild(node('p', 'small', [cap(c.role), c.importance].filter(Boolean).join(', ') +
      (c.aliases.length ? '. Also called ' + c.aliases.join(', ') + '.' : '')));
    if (c.personality) right.appendChild(node('p', '', c.personality));
    if (c.write_up) right.appendChild(node('p', '', c.write_up));
    if (!c.personality && !c.write_up && c.traits.length) right.appendChild(node('p', '', cap(c.traits.join(', ')) + '.'));
    if (c.point) right.appendChild(button('See in Manuscript', 'main', () => point({title: c.name, point: c.point})));
    if (c.ties.length) {
      right.appendChild(node('h4', '', 'Tied to'));
      const ul = node('ul', 'ties');
      for (const t of c.ties) {
        const o = a.cast.find(x => x.id === t.id);
        const li = node('li');
        const b = button('', 'tie', () => openPerson(t.id));
        b.append(face(o), node('span', '', t.name), node('span', 'small', t.kind));
        li.appendChild(b);
        ul.appendChild(li);
      }
      right.appendChild(ul);
    }
    if (c.sections.length) {
      right.appendChild(node('h4', '', 'Where they appear'));
      const ul = node('ul', 'secs');
      for (const sct of c.sections) {
        const li = node('li');
        li.appendChild(button('Section ' + sct.section, '', () => point({title: c.name, point: {section: sct.section, quote: c.name, note: c.name + ' is in this section.'}})));
        if (sct.summary) li.appendChild(node('span', 'small', ' ' + sct.summary));
        ul.appendChild(li);
      }
      right.appendChild(ul);
    }
    if (c.places.length) {
      const chips = node('div', 'chips');
      for (const p of c.places) chips.appendChild(button(p.name, 'chip', () => openPlace(p.id)));
      right.appendChild(chips);
    }
    lay.appendChild(right);
    box.appendChild(lay);
  }

  function openPlace(id) {
    if (!leaving()) return;
    S.page = id;
    setMode('place');
    drawPlace(id);
    $('atlas').scrollTop = 0;
  }

  function drawPlace(id) {
    const a = S.atlas, p = a.world.pins.find(x => x.id === id), box = $('atlas');
    box.textContent = '';
    if (!p) { pageHead(box, 'Not in the story bible any more'); return; }
    pageHead(box, p.name);
    const lay = node('div', 'page');
    const left = node('div', 'pleft');
    left.appendChild(slot(p, p.name, 'place', p.id, 'square big'));
    if (!p.picture) left.appendChild(node('p', 'small', 'When it is painted, the painter is told this:'));
    left.appendChild(proof(p, 'place'));
    left.appendChild(again(p));
    lay.appendChild(left);
    const right = node('div', 'pright');
    const what = [cap(p.kind), p.where].filter(Boolean).join(', ');
    if (what) right.appendChild(node('p', 'small', what + '.'));
    if (p.notes) right.appendChild(node('p', '', p.notes));
    if (p.point) right.appendChild(button('See in Manuscript', 'main', () => point({title: p.name, point: p.point})));
    if (p.people.length) {
      right.appendChild(node('h4', '', 'Who is here'));
      const ul = node('ul', 'ties');
      for (const pid of p.people) {
        const o = a.cast.find(x => x.id === pid);
        if (!o) continue;
        const li = node('li');
        const b = button('', 'tie', () => openPerson(pid));
        b.append(face(o), node('span', '', o.name));
        li.appendChild(b);
        ul.appendChild(li);
      }
      right.appendChild(ul);
    }
    const here = a.story.cards.filter(c => c.where === p.id);
    if (here.length) {
      right.appendChild(node('h4', '', 'What happens here'));
      const ul = node('ul', 'secs');
      for (const c of here) {
        const li = node('li');
        if (c.point) li.appendChild(button('Section ' + c.section, '', () => point({title: c.text, point: c.point})));
        li.appendChild(node('span', '', ' ' + c.text));
        ul.appendChild(li);
      }
      right.appendChild(ul);
    }
    if (p.appearances.length) right.appendChild(node('p', 'small', 'In sections ' + p.appearances.join(', ') + '.'));
    lay.appendChild(right);
    box.appendChild(lay);
  }

  // ---------------------------------------------------------------- one chart

  async function openBoard(title, id) {
    if (!leaving()) return;
    say($('bsay'), '');
    try {
      const d = await call('api/boards?' + q({title: title, id: id}));
      S.title = title;
      setMode('board');
      S.board = d.board; S.moved = false;
      showBoard();
    } catch (e) { say($('bsay'), e.message, true); }
  }

  function showBoard() {
    const b = S.board, spec = b.spec || {};
    $('btitle').textContent = b.title + ' - ' + S.title;
    $('bnote').textContent = spec.note || '';
    const lg = $('legend');
    lg.textContent = '';
    for (const l of spec.legend || []) lg.appendChild(node('li', 'k-' + String(l.cls || 'muted').split(' ')[0], l.text));
    $('savelayout').hidden = true;
    $('resetlayout').hidden = !(b.standard && b.saved_layout);
    const key = S.title + '/' + b.id;
    S.view = MapsBoards.draw($('board'), spec, {onPoint: point, onMoved: () => { S.moved = true; $('savelayout').hidden = false; },
      view: S.views[key], onView: v => { if (v) S.views[key] = v; else delete S.views[key]; }});
    drawList();
  }

  function closeAll() {
    S.title = null; S.atlas = null; S.atlasRaw = ''; S.gallery = null;
    setMode('list');
    const box = $('atlas');
    box.textContent = '';
    box.appendChild(node('p', 'empty', 'Pick a manuscript to open its atlas: the cast, the world, the family, the story and the cover.'));
    $('btitle').textContent = 'Pick a manuscript';
    $('bnote').textContent = '';
    drawList();
  }

  async function saveLayout() {
    if (!S.board || !S.view) return;
    try {
      const d = await call('api/boards/' + encodeURIComponent(S.board.id) + '/layout', 'POST',
        {title: S.title, positions: S.view.positions()});
      S.moved = false;
      $('savelayout').hidden = true;
      if (S.board.standard) { S.board.saved_layout = true; $('resetlayout').hidden = false; }
      say($('bsay'), d.message);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  async function resetLayout() {
    if (!S.board) return;
    try {
      const d = await call('api/boards?' + q({title: S.title, id: S.board.id}), 'DELETE');
      S.moved = false;
      await openBoard(S.title, S.board.id);
      say($('bsay'), d.message);
    } catch (e) { say($('bsay'), e.message, true); }
  }

  // ---------------------------------------------------------------- all pictures (under Charts)

  async function openPictures(title) {
    if (!leaving()) return;
    S.title = title; S.gallery = title;
    setMode('gallery');
    $('btitle').textContent = 'All pictures - ' + title;
    $('bnote').textContent = 'Painted by the picture program on your home network, from the story bible only. Click a portrait to open where that character first appears.';
    say($('bsay'), '');
    drawList();
    try { S.bible = await call('api/bible?' + q({title: title})); } catch (e) { S.bible = null; }
    await loadPictures();
  }

  async function loadPictures() {
    const title = S.gallery;
    if (!title || S.mode !== 'gallery') return;
    if (S.poll) { clearTimeout(S.poll); S.poll = null; }
    try {
      const d = await call('api/pictures?' + q({title: title}));
      if (S.gallery !== title) return;
      S.pics = d.pictures || [];
    } catch (e) { say($('bsay'), e.message, true); return; }
    drawGallery();
    if (S.pics.some(p => p.state === 'making')) S.poll = setTimeout(loadPictures, 3000);
  }

  function makeRow() {
    const f = node('form', 'makerow');
    f.autocomplete = 'off';
    const kind = node('select');
    kind.setAttribute('aria-label', 'Kind of picture');
    for (const [k, v] of Object.entries(KINDS)) { const o = node('option', '', v); o.value = k; kind.appendChild(o); }
    const who = node('select');
    who.setAttribute('aria-label', 'Who, where or which event');
    const fill = () => {
      const k = kind.value;
      who.textContent = '';
      const b = S.bible || {};
      const list = k === 'portrait' ? (b.characters || []) : k === 'place' ? (b.places || []) :
        k === 'scene' ? (b.events || []).map((e, i) => ({id: e.id || 'e' + (i + 1), name: e.what})) : null;
      who.hidden = list === null;
      for (const x of list || []) { const o = node('option', '', x.name || x.id); o.value = x.id || x.name; who.appendChild(o); }
    };
    kind.addEventListener('change', fill);
    fill();
    const go = node('button', 'main', 'Make');
    go.type = 'submit';
    f.append(node('span', 'lead', 'Make a picture'), kind, who, go);
    f.addEventListener('submit', async e => {
      e.preventDefault();
      try {
        const d = await call('api/picture', 'POST', {title: S.gallery, kind: kind.value, who: who.hidden ? '' : who.value});
        say($('bsay'), d.message);
        await loadPictures();
      } catch (x) { say($('bsay'), x.message, true); }
    });
    return f;
  }

  function drawGallery() {
    const box = $('gallery');
    box.textContent = '';
    box.appendChild(makeRow());
    if (!S.pics.length) { box.appendChild(node('p', 'empty', 'No pictures yet. Pick what to paint and press Make, or Illustrate on the atlas.')); return; }
    const grid = node('div', 'cards');
    for (const p of S.pics) grid.appendChild(card(p));
    box.appendChild(grid);
  }

  function card(p) {
    const c = node('figure', 'card ' + p.state);
    const label = (KINDS[p.kind] || p.kind) + (p.who_name ? ': ' + p.who_name : '');
    const shot = node('div', 'shot');
    if (p.state === 'making') {
      shot.appendChild(node('span', 'spin'));
      shot.appendChild(node('span', 'small', 'Making it...'));
    } else if (p.state === 'failed') {
      shot.appendChild(node('p', 'why', p.why || 'It could not be made.'));
      shot.appendChild(button('Retry', '', async () => {
        try { const d = await call('api/picture', 'POST', {title: S.gallery, retry: p.id}); say($('bsay'), d.message); await loadPictures(); }
        catch (e) { say($('bsay'), e.message, true); }
      }));
    } else {
      const img = node('img');
      img.alt = label;
      shot.appendChild(img);
      picURL(p.id).then(u => { img.src = u; }).catch(e => { shot.textContent = ''; shot.appendChild(node('p', 'why', e.message)); });
      if (p.kind === 'portrait' && p.section != null) {
        img.classList.add('pointable');
        img.title = 'Open where ' + (p.who_name || 'this character') + ' first appears';
        img.addEventListener('click', () => point({title: p.who_name, point: {section: p.section, quote: p.who_name, note: 'From a portrait in Maps.'}}));
      }
    }
    c.appendChild(shot);
    const fc = node('figcaption');
    fc.appendChild(node('strong', '', label));
    if (p.made_at) fc.appendChild(node('span', 'small', ' ' + when(p.made_at)));
    const how = node('details');
    how.appendChild(node('summary', '', 'What was sent'));
    how.appendChild(node('p', 'small', p.prompt || ''));
    fc.appendChild(how);
    const acts = node('span', 'row');
    if (p.state === 'ready') {
      acts.appendChild(button('Save', '', () => {
        if (!S.blobs[p.id]) return;
        const a = node('a');
        a.href = S.blobs[p.id];
        a.download = (S.gallery + ' - ' + label).replace(/[\\/:*?"<>|]+/g, ' ') + '.png';
        document.body.appendChild(a); a.click(); a.remove();
        say($('bsay'), 'The picture is in your downloads.');
      }));
    }
    acts.appendChild(button('Remove', '', async () => {
      if (!confirm('Remove this picture? The manuscript is not touched.')) return;
      try { const d = await call('api/pictures?' + q({title: S.gallery, id: p.id}), 'DELETE'); say($('bsay'), d.message); await loadPictures(); }
      catch (e) { say($('bsay'), e.message, true); }
    }));
    fc.appendChild(acts);
    c.appendChild(fc);
    return c;
  }

  // ---------------------------------------------------------------- the picture maker box

  function showMaker(d, line) {
    S.maker = d;
    if (d.address !== undefined && document.activeElement !== $('maddr')) $('maddr').value = d.address || '';
    if (d.style !== undefined && document.activeElement !== $('mstyle')) $('mstyle').value = d.style || '';
    const ask = d.ask;
    const bad = ask && (ask.state === 'refused' || ask.state === 'unknown');
    let text = line || (ask ? ask.line : (d.address ? '' : 'No address yet.'));
    if (d.avg_s) text += (text ? ' ' : '') + 'Pictures have taken about ' + Math.round(d.avg_s) + ' seconds each.';
    say($('mask'), text, bad);
  }

  async function loadMaker() {
    try { showMaker(await call('api/maker')); } catch (e) { say($('mask'), e.message, true); }
  }

  $('makerform').addEventListener('submit', async e => {
    e.preventDefault();
    say($('mtest'), '');
    try {
      const d = await call('api/maker', 'POST', {address: $('maddr').value.trim(), style: $('mstyle').value.trim()});
      showMaker(d, d.message);
      if (S.title && S.mode === 'atlas') loadAtlas(true);
    } catch (x) { say($('mask'), x.message, true); }
  });
  $('mtestbtn').addEventListener('click', async () => {
    say($('mtest'), 'Testing...');
    try {
      const d = await call('api/maker/test', 'POST', {});
      showMaker(d);
      const found = Object.entries(d.models || {}).filter(([, v]) => v && v.length).map(([k, v]) => k + ': ' + v.join(', '));
      say($('mtest'), d.message + (found.length ? ' Found ' + found.join('; ') + '.' : ''), !d.reached || !d.workflow);
    } catch (x) { say($('mtest'), x.message, true); }
  });

  // ---------------------------------------------------------------- start

  $('back').addEventListener('click', () => {
    if (S.mode === 'atlas' || S.mode === 'list') { if (leaving()) closeAll(); return; }
    if (!leaving()) return;
    openAtlas(S.title, true);
  });
  $('zin').addEventListener('click', () => S.view && S.view.zoom(1.25));
  $('zout').addEventListener('click', () => S.view && S.view.zoom(1 / 1.25));
  $('fit').addEventListener('click', () => S.view && S.view.fit());
  $('savelayout').addEventListener('click', saveLayout);
  $('resetlayout').addEventListener('click', resetLayout);
  $('png').addEventListener('click', async () => {
    if (!S.view) return;
    try { await S.view.png(S.board.title + ' - ' + S.title); say($('bsay'), 'The picture is in your downloads.'); }
    catch (e) { say($('bsay'), e.message, true); }
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    loadList();
    if (S.mode === 'gallery') loadPictures();
    if (['atlas', 'person', 'place'].includes(S.mode)) loadAtlas(false);
  });

  if (FIXTURE) $('sample').hidden = false;
  (async () => {
    loadMaker();
    await loadList();
    const u = new URLSearchParams(location.search);
    const want = u.get('board');
    const m = S.books.find(b => b.title === u.get('title') || b.folder === u.get('title')) || (S.books.length === 1 ? S.books[0] : null);
    if (m && want) openBoard(m.title, want);
    else if (m) openAtlas(m.title);
  })();
})();
