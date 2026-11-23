/* Editor's Desk, the Storyboard view: Kit lays the book out as panels, the way a film or picture-book artist would. A panel
   is a note about a picture, never a sentence for her book. Drawing starts only on her press; so does Wren's painting.
   Without a picture, each panel shows a sketch drawn here from its shot and mood. Everything is built with text nodes. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const SVG = 'http://www.w3.org/2000/svg';

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
  function sv(tag, attrs, kids) {
    const n = document.createElementNS(SVG, tag);
    for (const k in attrs || {}) n.setAttribute(k, attrs[k]);
    for (const c of kids || []) if (c) n.appendChild(c);
    return n;
  }
  const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); return n; };
  const q = o => Object.keys(o).map(k => encodeURIComponent(k) + '=' + encodeURIComponent(o[k])).join('&');
  async function call(path, body) {
    const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let got = {};
    try { got = await r.json(); } catch (e) { got = {}; }
    if (!r.ok) throw new Error(got.error || 'That did not work. Try again.');
    return got;
  }
  const when = iso => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
  const MOODS = ['calm', 'tense', 'sad', 'happy', 'dark', 'warm', 'cold', 'fear', 'angry', 'quiet'];

  // book: her book; b: what api/board gave; secs: the book's sections from the desk; pick: the chapters ticked
  const T = {book: null, b: null, seen: '', deskOf: null, deskSecs: [], pick: null, whole: false, gen: 0, cgen: 0, live: false, hooks: {}, sayNode: null};

  Object.defineProperty(T, 'secs', {get: () => (T.deskOf === T.book ? T.deskSecs : [])});

  // ---------------------------------------------------------------- loading

  function open(title) {
    if (!title) return;
    if (title !== T.book) { T.book = title; T.b = null; T.seen = ''; T.pick = null; T.whole = false; say(''); }
    return load();
  }

  async function load() {
    const gen = ++T.gen;
    let got;
    try {
      got = await call('api/board?' + q({title: T.book}));
    } catch (e) {
      if (gen !== T.gen) return;
      say(e.message, true);
      if (!T.b) clear($('bbody')).appendChild(el('p', {cls: 'bnote', text: 'These are notes for pictures, not words for your book.'}));
      return;
    }
    if (gen !== T.gen) return;
    if (T.live && !got.running) say('');          // the drawing is done: its "is drawing" line goes
    T.b = got;
    T.live = !!got.running;
    if (T.pick === null) firstPick();
    const key = JSON.stringify(got) + '|' + JSON.stringify(T.secs);
    if (key === T.seen) return;
    T.seen = key;
    keepFocus(draw);
  }

  function say(line, bad) {
    if (!T.sayNode) {
      T.sayNode = el('p', {cls: 'bsay', role: 'status', 'aria-live': 'polite', hidden: true});
      $('bbody').before(T.sayNode);
    }
    T.sayNode.textContent = line || '';
    T.sayNode.className = 'bsay' + (bad ? ' bad' : '');
    T.sayNode.hidden = !line;
  }

  function keepFocus(fn) {
    const a = document.activeElement;
    const k = a && a.dataset ? a.dataset.k : '';
    fn();
    if (!k) return;
    const n = $('bbody').querySelector('[data-k="' + (window.CSS && CSS.escape ? CSS.escape(k) : k) + '"]');
    if (n && n !== a) n.focus({preventScroll: true});
  }

  // ---------------------------------------------------------------- which chapters

  const board = () => (T.b && T.b.board) || {};
  const panels = () => (board().panels || []).filter(p => p && p.id);
  const changed = () => ((T.b && T.b.changed) || []).map(String);
  // the book's sections, in order: from the plan, else from the desk
  function numbers() {
    const plan = (T.b && T.b.plan && T.b.plan.sections) || [];
    return (plan.length ? plan : T.secs.map(s => s.n)).map(String);
  }
  const heading = n => { const s = T.secs.find(x => String(x.n) === String(n)); return (s && s.heading) || 'Section ' + n; };

  // first time on a book: the chapters that changed, else the whole book when nothing is drawn yet, else nothing
  function firstPick() {
    T.pick = {};
    const ch = changed();
    if (ch.length) for (const n of ch) T.pick[n] = true;
    else if (!panels().length) T.whole = true;
  }
  const picked = () => (T.whole ? 'all' : numbers().filter(n => T.pick[n]));

  function picker() {
    const ns = numbers();
    const box = el('fieldset', {cls: 'bpick'}, [el('legend', {text: 'Which chapters to draw'})]);
    const whole = el('input', {type: 'checkbox', 'data-k': 'whole', checked: T.whole});
    const boxes = [];
    const secs = el('div', {cls: 'bsecs'});
    for (const n of ns) {
      const cb = el('input', {type: 'checkbox', 'data-k': 'sec-' + n, checked: T.whole || !!T.pick[n]});
      cb.addEventListener('change', () => {
        T.pick[n] = cb.checked;
        if (!cb.checked && T.whole) { T.whole = false; whole.checked = false; for (const [m, x] of boxes) T.pick[m] = x.checked; }
        cost(); ready();
      });
      boxes.push([n, cb]);
      secs.appendChild(el('label', {cls: 'bsec'}, [cb, ' ' + heading(n) + (changed().includes(n) ? ' (changed)' : '')]));
    }
    whole.addEventListener('change', () => {
      T.whole = whole.checked;
      for (const [n, x] of boxes) { x.checked = T.whole; T.pick[n] = T.whole; }
      cost(); ready();
    });
    box.append(el('label', {cls: 'bsec bwhole'}, [whole, ' The whole book']), secs);
    T.costNode = el('p', {cls: 'bcost', 'aria-live': 'polite'});
    T.goNode = el('button', {type: 'button', cls: 'main', 'data-k': 'draw', text: 'Draw the storyboard', on: {click: () => start(picked())}});
    box.append(T.costNode, el('div', {cls: 'row'}, [T.goNode]));
    if (!ns.length) box.appendChild(el('p', {cls: 'fine', text: 'Your book has no chapters to draw yet.'}));
    return box;
  }
  function ready() {
    const p = picked();
    T.goNode.disabled = T.live || (p !== 'all' && !p.length);
  }

  // what it costs, before anything starts
  async function cost() {
    const p = picked();
    const gen = ++T.cgen;
    if (p !== 'all' && !p.length) { T.costNode.textContent = 'Pick at least one chapter.'; return; }
    let plan;
    if (p === 'all') plan = (T.b && T.b.plan) || {};
    else {
      T.costNode.textContent = 'Working out the time...';
      try {
        const got = await call('api/board/plan?' + q({title: T.book, sections: p.join(',')}));
        plan = got.plan || got;
      } catch (e) {
        if (gen === T.cgen) T.costNode.textContent = e.message;
        return;
      }
    }
    if (gen !== T.cgen) return;
    const steps = Number(plan.steps) || 0;
    if (!steps) { T.costNode.textContent = 'This takes no time: nothing there needs your bot.'; return; }
    T.costNode.textContent = plural(steps, 'step') + ' for your bot' + (plan.time ? ', ' + plan.time : '. Your bot has not been timed yet') + '.';
  }

  // ---------------------------------------------------------------- her presses

  async function start(sections) {
    if (sections !== 'all' && !sections.length) return;
    try {
      await call('api/board', {title: T.book, sections});
    } catch (e) { say(e.message, true); return; }
    T.live = true;
    say('Kit is drawing. The panels come in as each chapter is done.');
    if (T.hooks.refresh) T.hooks.refresh();
    load();
  }
  async function paint() {
    try {
      await call('api/board/paint', {title: T.book, panels: 'all'});
    } catch (e) { say(e.message, true); return; }
    say('Wren is painting. The pictures come in as each one is done.');
    load();
  }

  // ---------------------------------------------------------------- drawing

  function draw() {
    const box = clear($('bbody'));
    box.appendChild(el('p', {cls: 'bnote', text: 'These are notes for pictures, not words for your book.'}));
    box.appendChild(picker());
    ready();
    cost();
    const all = panels();
    const b = board();
    if (T.live) box.appendChild(el('p', {cls: 'fine', text: 'Kit is drawing now. The line above shows how far.'}));
    if (!all.length) {
      if (!T.live) box.appendChild(el('p', {cls: 'empty', text: 'No storyboard yet. Pick chapters above and press Draw the storyboard.'}));
    } else {
      const top = el('div', {cls: 'row bpaint'});
      if (b.made_at) top.appendChild(el('span', {cls: 'fine', text: 'Drawn ' + when(b.made_at) + '. ' + plural(all.length, 'panel') + '.'}));
      if (T.b.can_paint) top.appendChild(el('button', {type: 'button', 'data-k': 'paint', text: 'Have Wren paint these', on: {click: paint}}));
      box.appendChild(top);
      if (T.b.paint_note) box.appendChild(el('p', {cls: 'fine', text: T.b.paint_note}));
    }
    // panels grouped by section, in the book's order, each in its own order
    const order = numbers();
    const rank = n => { const i = order.indexOf(String(n)); return i < 0 ? 1e6 + Number(n) : i; };
    const groups = {};
    for (const p of all) (groups[p.section] = groups[p.section] || []).push(p);
    const ch = changed();
    for (const n of Object.keys(groups).sort((a, z) => rank(a) - rank(z))) {
      const mine = groups[n].sort((a, z) => (a.n || 0) - (z.n || 0));
      const sec = el('section', {cls: 'bgroup', 'aria-label': mine[0].heading || heading(n)}, [el('h2', {cls: 'bhead', text: mine[0].heading || heading(n)})]);
      if (ch.includes(String(n))) {
        sec.appendChild(el('div', {cls: 'bchanged'}, [el('span', {text: 'This chapter changed since it was drawn.'}),
          el('button', {type: 'button', 'data-k': 'again-' + n, text: 'Draw this chapter again', disabled: T.live, on: {click: () => start([Number(n)])}})]));
      }
      sec.appendChild(el('div', {cls: 'bstrip'}, mine.map(panelCard)));
      box.appendChild(sec);
    }
    const skipped = b.skipped || [];
    if (skipped.length) {
      box.appendChild(el('h2', {text: 'Left out'}));
      box.appendChild(el('ul', {cls: 'bskip'}, skipped.map(s => el('li', {text: heading(s.section) + ': ' + (s.why || 'nothing to draw')}))));
    }
  }

  function panelCard(p) {
    const pic = ((T.b && T.b.pictures) || {})[p.id] || {};
    const frame = el('div', {cls: 'bpic'});
    if (pic.state === 'ready' && pic.url) {
      const img = el('img', {src: pic.url, alt: "Wren's painting: " + (p.what || ''), loading: 'lazy'});
      img.addEventListener('error', () => img.replaceWith(sketch(p)));
      frame.appendChild(img);
    } else {
      frame.appendChild(sketch(p));
    }
    const card = el('article', {cls: 'bpanel', 'aria-label': 'Panel ' + (p.n || '')}, [frame]);
    if (pic.state === 'making') card.appendChild(el('p', {cls: 'fine', text: 'Wren is painting this one.'}));
    if (pic.state === 'failed') card.appendChild(el('p', {cls: 'fine', text: 'Wren could not paint this one.'}));
    card.appendChild(el('div', {cls: 'bnum', text: 'Panel ' + (p.n || '')}));
    if (p.what) card.appendChild(el('p', {cls: 'bwhat', text: p.what}));
    const who = Array.isArray(p.who) ? p.who.filter(Boolean) : [];
    const chips = [p.where ? ['Where', p.where] : null, who.length ? ['Who', who.join(', ')] : null, p.mood ? ['Mood', p.mood] : null].filter(Boolean);
    if (chips.length) card.appendChild(el('div', {cls: 'bchips'}, chips.map(([k, v]) => el('span', {cls: 'chip'}, [el('span', {cls: 'lab', text: k + ': '}), v]))));
    if (p.frame) card.appendChild(el('p', {cls: 'bframe', text: p.frame}));
    if (p.quote) card.appendChild(el('blockquote', {cls: 'bquote', text: '“' + p.quote + '”'}));
    card.appendChild(el('button', {type: 'button', cls: 'link', 'data-k': 'go-' + p.id, text: 'Go to this in your book',
      on: {click: () => T.hooks.goTo && T.hooks.goTo(p.section, p.quote || '')}}));
    return card;
  }

  // ---------------------------------------------------------------- the sketch: a 16:9 frame from the shot and the mood

  function moodOf(mood) {
    const m = String(mood || '').toLowerCase();
    return MOODS.find(w => m.includes(w)) || 'neutral';
  }
  function figure(x) {                                // a small whole figure, standing on the ground at y 78
    return sv('g', {class: 'sk-fig'}, [
      sv('circle', {cx: x, cy: '58', r: '3.4'}),
      sv('path', {d: 'M' + x + ' 61.5 V71 M' + (x - 4.5) + ' 65.5 H' + (x + 4.5) + ' M' + x + ' 71 L' + (x - 3.5) + ' 78 M' + x + ' 71 L' + (x + 3.5) + ' 78'}),
    ]);
  }
  function bust(x, cy, r, w) {                        // head and shoulders, cut off by the bottom of the frame
    return sv('g', {class: 'sk-fig'}, [
      sv('path', {class: 'sk-body', d: 'M' + (x - w) + ' 90 Q' + (x - w) + ' ' + (cy + r + 4) + ' ' + x + ' ' + (cy + r + 2) + ' Q' + (x + w) + ' ' + (cy + r + 4) + ' ' + (x + w) + ' 90 Z'}),
      sv('circle', {class: 'sk-body', cx: x, cy, r}),
    ]);
  }
  function sketch(p) {
    const shot = ['wide', 'medium', 'close'].includes(p.shot) ? p.shot : 'medium';
    const who = (Array.isArray(p.who) ? p.who.filter(Boolean) : []).slice(0, 4);
    const kids = [sv('rect', {class: 'sk-bg', x: '0', y: '0', width: '160', height: '90'})];
    if (shot === 'wide') {
      kids.push(sv('line', {class: 'sk-line', x1: '0', y1: '46', x2: '160', y2: '46'}));
      kids.push(sv('line', {class: 'sk-line', x1: '0', y1: '78', x2: '160', y2: '78'}));
      who.forEach((_, i) => kids.push(figure(Math.round(160 * (i + 1) / (who.length + 1)))));
    } else if (shot === 'medium') {
      const xs = who.length >= 2 ? [54, 106] : [80];
      for (const x of xs) kids.push(bust(x, 38, 11, 25));
    } else {
      kids.push(bust(80, 38, 22, 52));
    }
    kids.push(sv('rect', {class: 'sk-frame', x: '0.5', y: '0.5', width: '159', height: '89'}));
    const words = 'A sketch: ' + shot + ' shot' + (who.length ? ', ' + who.join(', ') : '') + (p.mood ? ', ' + p.mood : '');
    return sv('svg', {class: 'sk sk-' + shot + ' mood-' + moodOf(p.mood), viewBox: '0 0 160 90', role: 'img', 'aria-label': words}, kids);
  }

  // ---------------------------------------------------------------- what the shell tells this view

  // what the desk knows: the book's sections and their headings
  function desk(v) {
    if (!v) return;
    const was = JSON.stringify(T.secs);
    T.deskOf = v.title;                             // the desk may speak of the book about to open
    T.deskSecs = v.sections || [];
    if (T.b && v.title === T.book && was !== JSON.stringify(T.secs) && !$('v-board').hidden) { T.seen = ''; load(); }
  }
  // every five seconds from the shell, while she is in this view: ask again only while Kit draws or Wren paints
  function tick() {
    const making = T.b && Object.values(T.b.pictures || {}).some(x => x && x.state === 'making');
    if ((T.live || making) && T.book && document.visibilityState === 'visible') load();
  }
  function note(line) { say(line, true); }

  window.BoardView = {open, desk, tick, note, hooks: T.hooks};
})();
