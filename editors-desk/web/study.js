/* Editor's Desk, the Study a book view: her book beside a book she admires. The numbers come at once, by code; on her
   press, your editor reads matched passages of both, one pair at a time, in the same line as the checks. Nothing here
   changes her text. The server sends data, never markup: everything here is built with text nodes. */
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
  async function call(path, body) {
    const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    let got = {};
    try { got = await r.json(); } catch (e) { got = {}; }
    if (!r.ok) throw new Error(got.error || 'That did not work. Try again.');
    return got;
  }
  const when = iso => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); };
  const num = x => (typeof x === 'number' ? x.toLocaleString(undefined, {maximumFractionDigits: 1}) : String(x == null ? '' : x));
  const pct = x => (typeof x === 'number' ? Math.round(x * 100) + '%' : '');

  // book: her book; theirs: the one picked; books: the others on the shelf; n: the numbers; r: the latest report
  const T = {book: null, theirs: '', books: [], n: null, r: null, live: false, focus: {}, hooks: {}, gen: 0};

  // ---------------------------------------------------------------- loading

  async function open(title) {
    if (!title) return;
    if (title !== T.book) { T.book = title; T.theirs = ''; T.n = null; T.r = null; }
    const gen = ++T.gen;
    let got;
    try { got = await call('api/study/books?' + q({title})); } catch (e) { return say(e.message); }
    if (gen !== T.gen) return;
    T.books = got.books || [];
    if (T.theirs && !T.books.some(b => b.title === T.theirs)) T.theirs = '';
    if (!T.theirs) {
      let latest = [];
      try { latest = (await call('api/study?' + q({title}))).reports || []; } catch (e) { latest = []; }
      if (gen !== T.gen) return;
      const last = latest.find(x => T.books.some(b => b.title === x.theirs));
      if (last) T.theirs = last.theirs;                    // the book she compared with last time
      else if (T.books.length === 1) T.theirs = T.books[0].title;
    }
    draw();
    if (T.theirs) await load();
  }

  async function load() {
    const gen = ++T.gen;
    T.n = null; T.r = null;
    draw();
    try {
      const [n, r] = await Promise.all([call('api/study/numbers?' + q({title: T.book, theirs: T.theirs})),
        call('api/study?' + q({title: T.book, theirs: T.theirs}))]);
      if (gen !== T.gen) return;
      T.n = n; T.r = r.report; T.live = !!r.running;
    } catch (e) {
      if (gen === T.gen) say(e.message);
      return;
    }
    draw();
  }

  function say(line) {
    const box = clear($('sbody'));
    box.appendChild(el('p', {cls: 'note', text: line}));
  }

  // ---------------------------------------------------------------- drawing

  function draw() {
    const box = clear($('sbody'));
    box.appendChild(head());
    if (!T.books.length) {
      box.appendChild(el('p', {text: 'There is no other book here to compare with. On the Manuscript page, press Add a book to study and pick a book you admire. It is not read into a story bible.'}));
      box.appendChild(el('button', {type: 'button', text: 'Open Manuscript', on: {click: () => T.hooks.manuscript && T.hooks.manuscript()}}));
      return;
    }
    if (!T.theirs) { box.appendChild(el('p', {cls: 'fine', text: 'Pick a book you admire. The numbers show at once.'})); return; }
    if (!T.n) { box.appendChild(el('p', {cls: 'fine', text: 'Measuring both books...'})); return; }
    box.appendChild(numbers());
    box.appendChild(ask());
    if (T.r) box.appendChild(results());
  }

  function head() {
    const sel = el('select', {id: 'theirs', 'aria-label': 'The book to compare with', on: {change: e => { T.theirs = e.target.value; if (T.theirs) load(); else draw(); }}},
      [el('option', {value: '', text: 'Pick a book'})].concat(T.books.map(b => el('option', {value: b.title, text: b.title + (b.study ? ' (a book to study)' : '')}))));
    sel.value = T.theirs;
    sel.disabled = !T.books.length;
    return el('div', {cls: 'shead'}, [el('h1', {}, ['Compare ', el('b', {text: T.book}), ' with']), sel]);
  }

  // the numbers, by code: where the books differ most, where they match, a small table, and two strips per book
  const ROWS = [
    ['Words', n => num(n.words)],
    ['Chapters', n => num(n.chapters)],
    ['Words in a chapter, on average', n => num(n.chapter_words.mean)],
    ['Scenes in a chapter, on average', n => num(n.scenes_per_chapter)],
    ['Words in a sentence, on average', n => num(n.sentences.mean)],
    ['Sentences under 8 words', n => pct(n.sentences.under_8)],
    ['Sentences over 30 words', n => pct(n.sentences.over_30)],
    ['Words in a paragraph, on average', n => num(n.paragraphs.mean)],
    ['People talking', n => pct(n.dialogue.share)],
    ['Tags that are said or asked', n => (n.dialogue.tags ? pct(n.dialogue.said_share) : 'no tags')],
    ['Told in', n => n.narration.person + ' person, ' + n.narration.tense + ' tense'],
    ['Words ending in -ly, per 1,000', n => num(n.per_1000.ly_adverbs)],
    ['Dashes, per 1,000 words', n => num(n.per_1000.dashes)],
  ];

  function numbers() {
    const n = T.n;
    const wrap = el('section', {cls: 'snums', 'aria-label': 'The numbers'});
    wrap.appendChild(el('h2', {text: 'Where the two books differ most'}));
    wrap.appendChild(n.apart.length ? el('ul', {cls: 'lines'}, n.apart.map(x => el('li', {text: x.line}))) : el('p', {cls: 'fine', text: 'Nothing measured stands far apart.'}));
    wrap.appendChild(el('h2', {text: 'Where you already match'}));
    wrap.appendChild(n.alike.length ? el('ul', {cls: 'lines'}, n.alike.map(x => el('li', {text: x.line}))) : el('p', {cls: 'fine', text: 'Nothing measured is close.'}));
    wrap.appendChild(el('p', {cls: 'fine', text: 'Counted by code, not by your editor. Different is not wrong.'}));
    const mine = n.numbers.mine, theirs = n.numbers.theirs;
    const table = el('table', {cls: 'stable'}, [
      el('thead', {}, [el('tr', {}, [el('th', {text: ''}), el('th', {scope: 'col', text: 'Yours'}), el('th', {scope: 'col', text: 'Theirs'})])]),
      el('tbody', {}, ROWS.map(([label, f]) => el('tr', {}, [el('th', {scope: 'row', text: label}), el('td', {text: f(mine)}), el('td', {text: f(theirs)})]))),
    ]);
    wrap.appendChild(el('h2', {text: 'The main numbers'}));
    wrap.appendChild(table);
    const most = Math.max(1, ...mine.sentences.by_chapter, ...theirs.sentences.by_chapter);
    const cols = el('div', {cls: 'scols'});
    for (const [label, who, x] of [['Yours', T.n.mine, mine], ['Theirs', T.n.theirs, theirs]]) {
      cols.appendChild(el('div', {cls: 'scol'}, [
        el('h3', {}, [label + ': ', el('span', {cls: 'stitle', text: who})]),
        strip('Words in a sentence, chapter by chapter', x.sentences.by_chapter, most, v => num(v) + ' words'),
        strip('Share of people talking, chapter by chapter', x.dialogue.by_chapter, 1, v => pct(v)),
      ]));
    }
    wrap.appendChild(cols);
    return wrap;
  }

  function strip(label, values, most, say_) {
    const bars = el('div', {cls: 'bars', role: 'img', 'aria-label': label + ': ' + values.map(say_).join(', ')});
    values.forEach((v, i) => {
      const bar = el('span', {cls: 'bar', title: 'Chapter ' + (i + 1) + ': ' + say_(v)});
      bar.style.height = Math.max(2, Math.round(100 * v / most)) + '%';      // through the CSSOM: the page allows no inline styles
      bars.appendChild(bar);
    });
    return el('div', {cls: 'strip'}, [el('div', {cls: 'fine', text: label}), bars]);
  }

  // her press
  function ask() {
    const n = T.n;
    const box = el('section', {cls: 'sask', 'aria-label': 'Ask your editor'});
    if (!n.pairs_count) {
      box.appendChild(el('p', {text: 'There is not enough story in one of the books to set passages side by side, so the numbers are all there is.'}));
      return box;
    }
    const input = el('input', {type: 'text', id: 'sfocus', maxlength: '240', placeholder: 'For example: how she moves from one scene to the next',
      value: T.focus[T.theirs] || (T.r && T.r.focus !== 'narrative style and flow' ? T.r.focus : '') || '', on: {input: e => { T.focus[T.theirs] = e.target.value; }}});
    box.appendChild(el('label', {for: 'sfocus', text: 'What do you most want to learn from this book?'}));
    box.appendChild(input);
    const cost = n.pairs_count + ' passages' + (n.time_left ? ', ' + n.time_left : '. Your bot has not been timed yet');
    const go = el('button', {type: 'button', cls: 'main', text: T.r ? 'Compare again' : 'Ask your editor to compare passages', disabled: T.live,
      on: {click: () => start(input.value.trim())}});
    box.appendChild(el('div', {cls: 'row'}, [go, el('span', {cls: 'fine', text: T.live ? 'Your editor is comparing passages now. The line above shows how far.' : cost + '.'})]));
    if (T.r) box.appendChild(el('p', {cls: 'fine', text: 'Compared ' + when(T.r.made_at) + (T.r.focus ? '. Your focus: ' + T.r.focus : '') + '.'}));
    return box;
  }

  async function start(focus) {
    try {
      await call('api/study', {title: T.book, theirs: T.theirs, focus});
      T.live = true;
    } catch (e) {
      const w = $('swork');
      clear(w).appendChild(el('span', {text: e.message}));
      w.hidden = false;
      w.className = 'work bad';
      return;
    }
    draw();
    if (T.hooks.refresh) T.hooks.refresh();
  }

  // the report: what to try first, then each pair of passages
  function results() {
    const r = T.r;
    const wrap = el('section', {cls: 'sres', 'aria-label': "Your editor's notes"});
    if (r.pointers && r.pointers.length) {
      wrap.appendChild(el('h2', {text: 'What to try first'}));
      wrap.appendChild(el('ol', {cls: 'first'}, r.pointers.map(p => el('li', {}, [el('b', {text: p.try}), el('div', {cls: 'why', text: p.why})]))));
    }
    for (const s of r.skipped || []) if (s.step === 'all' || s.step === 'pointers') wrap.appendChild(el('p', {cls: 'fine', text: s.line}));
    const skipped = {};
    for (const s of r.skipped || []) skipped[s.step] = s.line;
    for (const p of r.pairs || []) {
      const sec = el('section', {cls: 'pair', 'aria-label': p.label});
      sec.appendChild(el('h3', {text: p.label}));
      if (p.state !== 'done') {
        sec.appendChild(el('p', {cls: 'fine', text: skipped[p.kind] || 'Your editor left these passages out.'}));
        wrap.appendChild(sec);
        continue;
      }
      const theirQ = (p.pointers || []).map(x => x.their_quote).filter(Boolean);
      const myQ = (p.pointers || []).map(x => x.my_quote).filter(Boolean);
      const go = el('button', {type: 'button', cls: 'link', text: 'Go to this in your book',
        on: {click: () => T.hooks.goTo && T.hooks.goTo(p.mine.section, firstWords(p.mine.text))}});
      sec.appendChild(el('div', {cls: 'scols'}, [
        el('div', {cls: 'scol'}, [el('div', {cls: 'lab', text: 'Theirs'}), passage(p.theirs.text, theirQ), el('p', {}, [el('span', {cls: 'lab', text: 'What theirs does'}), p.theirs_does])]),
        el('div', {cls: 'scol'}, [el('div', {cls: 'lab'}, ['Yours ', go]), passage(p.mine.text, myQ), el('p', {}, [el('span', {cls: 'lab', text: 'What yours does'}), p.mine_does])]),
      ]));
      if (p.pointers && p.pointers.length) {
        sec.appendChild(el('ul', {cls: 'tries'}, p.pointers.map(x => el('li', {}, [
          el('b', {text: x.try}), el('div', {cls: 'why', text: x.why}),
          x.their_quote ? el('div', {cls: 'qline'}, [el('span', {cls: 'lab', text: 'Theirs'}), el('mark', {text: x.their_quote})]) : null,
          x.my_quote ? el('div', {cls: 'qline'}, [el('span', {cls: 'lab', text: 'Yours'}), el('mark', {text: x.my_quote})]) : null,
        ]))));
      }
      wrap.appendChild(sec);
    }
    return wrap;
  }

  // one passage, a paragraph at a time, its quoted words marked where they are found
  function passage(text, quotes) {
    const box = el('div', {cls: 'passage'});
    for (const para of String(text || '').split(/\n\s*\n/)) {
      const t = para.replace(/\s+/g, ' ').trim();
      if (!t) continue;
      const spans = [];
      for (const qt of quotes) {
        const at = t.indexOf(qt);
        if (at >= 0 && !spans.some(([a, z]) => at < z && at + qt.length > a)) spans.push([at, at + qt.length]);
      }
      spans.sort((a, b) => a[0] - b[0]);
      const p = el('p');
      let i = 0;
      for (const [a, z] of spans) {
        if (a > i) p.appendChild(document.createTextNode(t.slice(i, a)));
        p.appendChild(el('mark', {text: t.slice(a, z)}));
        i = z;
      }
      if (i < t.length) p.appendChild(document.createTextNode(t.slice(i)));
      box.appendChild(p);
    }
    return box;
  }
  const firstWords = text => String(text || '').split(/\n\s*\n/)[0].split(/\s+/).filter(Boolean).slice(0, 8).join(' ');

  // what the desk knows: is a comparison running for this book; when it ends, the results come in
  function desk(v) {
    if (!v || v.title !== T.book) return;
    const st = v.status;
    const live = !!(st && st.check === 'study' && ['running', 'waiting', 'queued'].includes(st.state));
    if (T.live && !live && T.theirs) { T.live = false; load(); return; }
    if (live !== T.live) { T.live = live; if (T.n) draw(); }
  }

  window.StudyView = {open, desk, hooks: T.hooks};
})();
