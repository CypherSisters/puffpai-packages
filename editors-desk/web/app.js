/* Editor's Desk, the page. Every call goes to the page's own origin with no token; the PuffPAI Desktop's proxy adds the key.
   The server sends data, never markup: everything here is built with text nodes. Her text changes only from her own typing
   in Write (write.js), Bring this back, and Use this. This file is the shell: the book picker, the views, and the desk. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const SIZE = {big: 'Big', medium: 'Worth a look', small: 'Small'};
  const STATE = [['open', 'Open'], ['later', 'Later'], ['fixed', 'Fixed'], ['not_a_problem', 'Not a problem']];
  const S = {books: [], book: null, v: null, view: null, filter: 'open', only: '', who: '', opened: {}, typed: {}};
  const Write = window.WriteView;
  const Study = window.StudyView;
  const Office = window.OfficeView;
  const Board = window.BoardView;
  // the stage each staff member's desk is (staff.py); a row may also carry its own stage
  const DESK_STAGE = {chief: 'any', story: 'story', facts: 'facts', line: 'prose', copy: 'polish', reader: 'out'};

  // ---------------------------------------------------------------- small helpers

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
  function when(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return '';
    return d.toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
  }
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

  // ---------------------------------------------------------------- loading

  async function loadBooks() {
    const b = $('book');
    try {
      S.books = (await call('api/list')).books;
    } catch (e) {
      clear($('letter')).appendChild(el('p', {text: e.message}));
      Office.note(e.message); Board.note(e.message);
      return;
    }
    clear(b);
    if (!S.books.length) {
      clear($('letter')).append(el('h1', {text: "Your editor's letter"}), el('p', {text: 'There are no books yet. Add one on the Manuscript page, let your bot read it, and come back.'}));
      Office.note('There are no books yet. Add one on the Manuscript page, let your bot read it, and come back.');
      Board.note('There are no books yet.');
      return;
    }
    const order = S.books.filter(x => !x.study).concat(S.books.filter(x => x.study));    // books to study last
    for (const x of order) b.appendChild(el('option', {value: x.title, text: x.title + (x.study ? ' (a book to study)' : '')}));
    b.disabled = false;
    const want = new URLSearchParams(location.search).get('title');
    const first = order.find(x => x.read && !x.study) || order.find(x => !x.study) || order[0];   // her own book, read first
    await pick(want && S.books.some(x => x.title === want) ? want : first.title);
  }

  async function pick(title) {
    if (S.book) await Write.flush(true);            // her words in the book she leaves are saved first
    S.book = title;
    $('book').value = title;
    try { await call('api/open', {title}); } catch (e) { /* the view below says what is wrong */ }
    await refresh();
    if (S.view === 'write') Write.open(title);
    if (S.view === 'study') Study.open(title);
    if (S.view === 'office') Office.open(title);
    if (S.view === 'board') Board.open(title);
  }

  async function refresh() {
    if (!S.book) return;
    try {
      const got = await call('api/desk?' + q({title: S.book}));
      const same = S.seen === JSON.stringify({...got, status: null});
      S.seen = JSON.stringify({...got, status: null});
      S.v = got;
      Write.desk(got);
      Study.desk(got);
      Office.desk(got);
      Board.desk(got);
      if (same) { drawWork(); return; }                // nothing but the progress line moved: leave what she has open
    } catch (e) {
      for (const id of ['work', 'swork', 'bwork']) { clear($(id)).appendChild(el('span', {text: e.message})); $(id).hidden = false; }
      return;
    }
    draw();
  }

  function draw() {
    const v = S.v;
    $('count').textContent = plural(v.sections.length, 'section') + ', ' + v.words.toLocaleString() + ' words';
    const st = $('stale');
    st.hidden = !(v.unread && v.unread.length);
    if (!st.hidden) st.textContent = (v.unread.length === 1 ? 'Section ' + v.unread[0] + ' has' : 'Sections ' + v.unread.join(', ') + ' have') + ' changed since your book was last read. Your editor goes by your new words, but the story bible still has the old ones. Read the book again in Manuscript when you are ready.';
    $('private').hidden = !v.private;
    drawWork(); drawWho(); drawLetter(); drawNext(); drawStages(); drawAnytime(); drawFilters(); drawList(); drawDocs();
  }

  // ---------------------------------------------------------------- one staff member's desk

  // From the office: only that staff member's checks and findings, with a way back to every desk.
  const whoRow = () => (S.v && S.v.staff || []).find(x => x.id === S.who);
  const deskStage = () => { const r = whoRow(); return (r && r.stage) || DESK_STAGE[S.who] || ''; };
  const mineCheck = c => !S.who || c.stage === deskStage() || c.who === S.who;
  const mineFinding = f => !S.who || (f.who ? f.who === S.who : (S.v.stages || []).some(s => s.id === deskStage() && s.checks.some(c => c.id === f.check)));
  function drawWho() {
    const box = clear($('deskwho'));
    const r = whoRow();
    if (S.who && !r) S.who = '';
    box.hidden = !S.who;
    if (!S.who) return;
    box.append(el('strong', {text: r.name + "'s desk"}), el('span', {cls: 'fine', text: r.role || ''}),
      el('button', {type: 'button', text: 'Show every desk', on: {click: () => { S.who = ''; draw(); }}}));
  }

  // ---------------------------------------------------------------- what your editor is doing

  // The same line shows on the desk, in Study a book and on the storyboard: one line of work, whichever view she is in.
  function drawWork() {
    workInto($('work'));
    workInto($('swork'));
    workInto($('bwork'));
  }
  function workInto(target) {
    const box = clear(target);
    const st = S.v.status;
    const live = st && ['running', 'waiting', 'queued'].includes(st.state);
    box.hidden = !st;
    if (!st) return;
    box.className = 'work' + (st.state === 'failed' ? ' bad' : '');
    if (live) {
      box.append(el('b', {text: 'Your editor: ' + (st.check_name || '')}), el('span', {cls: 'grow', text: st.step || ''}));
      if (st.total) box.appendChild(el('progress', {max: st.total, value: Math.min(st.done || 0, st.total)}));
      if (st.total) box.appendChild(el('span', {text: 'step ' + (st.done || 0) + ' of ' + st.total}));
      box.appendChild(el('span', {text: st.left || (st.total ? 'timing your bot...' : '')}));
      if (st.in_line && st.in_line.length) box.appendChild(el('span', {text: 'then ' + st.in_line.join(', ')}));
      box.appendChild(el('button', {type: 'button', text: 'Stop', on: {click: stop}}));
    } else {
      box.appendChild(el('span', {cls: 'grow', text: st.step || ''}));
      if (st.problems && st.problems.length) {
        const list = el('ul', {}, st.problems.map(p => el('li', {text: p})));
        box.appendChild(el('details', {}, [el('summary', {text: plural(st.problems.length, 'thing') + ' to know'}), list]));
      }
    }
  }
  async function stop() {
    try { await call('api/stop', {title: S.book}); } catch (e) { /* the status says */ }
    refresh();
  }

  // ---------------------------------------------------------------- the letter

  function drawLetter() {
    const box = clear($('letter'));
    const v = S.v, lt = v.letter;
    box.appendChild(el('h1', {text: "Your editor's letter"}));
    if (!lt) {
      box.appendChild(el('p', {text: v.has_bible ? 'It comes with the first check. Start with the one below, and your editor will write to you when it is done.' :
        'Your bot has not read this book yet. Open Manuscript and let it read the book; your editor works from what it finds. A few checks need no reading and can run now.'}));
      if (!v.has_bible) box.appendChild(el('button', {type: 'button', text: 'Open Manuscript', on: {click: openManuscript}}));
      return;
    }
    box.appendChild(el('p', {text: lt.book}));
    if (lt.working && lt.working.length) {
      box.appendChild(el('h3', {text: "What's working"}));
      box.appendChild(el('ul', {}, lt.working.map(x => el('li', {text: x}))));
    }
    const byId = {};
    for (const f of v.findings) byId[f.id] = f;
    const still = (lt.problems || []).filter(p => byId[p.id] && byId[p.id].kind !== 'question');   // never one since dropped
    if (still.length) {
      box.appendChild(el('h3', {text: 'The biggest problems'}));
      box.appendChild(el('ol', {}, still.map(p => {
        const f = byId[p.id];
        const done = f && f.state !== 'open' && f.state !== 'later';
        return el('li', {cls: done ? 'done' : ''}, [f ? el('button', {cls: 'link', type: 'button', text: p.line, on: {click: () => showFinding(f)}}) : p.line]);
      })));
    }
    if (lt.first) { box.appendChild(el('h3', {text: 'What to do first'})); box.appendChild(el('p', {text: lt.first})); }
    if (v.plan && v.plan.length) {
      const list = el('ol', {}, v.plan.map(p => el('li', {}, [el('button', {cls: 'link', type: 'button', text: p.what, on: {click: () => showFinding(byId[p.id])}}), p.stage ? ' (' + p.stage + ')' : ''])));
      box.appendChild(el('details', {}, [el('summary', {text: 'The revision plan: ' + plural(v.open_now, 'thing') + ' open, in the order to do them'}), list]));
    }
    const moved = lt.open_then !== undefined && lt.open_then !== v.open_now;
    box.appendChild(el('p', {cls: 'fine', text: 'Written ' + when(lt.written_at) + (lt.from_checks && lt.from_checks.length ? ', from ' + plural(lt.from_checks.length, 'check') : '') + '.' + (moved ? ' You have marked things since; the revision plan keeps up, and the letter is as it was then.' : '')}));
    box.appendChild(el('button', {type: 'button', text: 'Write it again', disabled: isBusy('letter'), on: {click: () => run('letter')}}));
  }

  // ---------------------------------------------------------------- next

  function drawNext() {
    const box = clear($('next'));
    const n = S.v.next;
    box.hidden = !n;
    if (!n) return;
    if (!n.check) {
      box.append(el('strong', {text: 'Next: settle ' + n.stage}), el('p', {text: n.why}));
      return;
    }
    box.append(el('strong', {text: 'Next: ' + n.name}), el('p', {text: n.why}));
    if (!n.can_run) { box.appendChild(el('p', {text: 'It needs your book read in Manuscript first.'})); return; }
    let input = null;
    if (n.box) { input = boxFor(n.check, n.box); box.appendChild(input); }
    box.appendChild(el('button', {cls: 'main', type: 'button', text: n.button === 'Run' ? 'Run ' + n.name : n.button, disabled: isBusy(n.check), on: {click: () => run(n.check, input)}}));
  }

  // ---------------------------------------------------------------- the stages

  const isBusy = id => {
    const st = S.v.status;
    return !!(st && ['running', 'waiting', 'queued'].includes(st.state) && st.check === id);
  };

  function boxFor(id, label) {
    const box = el('input', {type: 'text', maxlength: '1000', placeholder: label, 'aria-label': label, value: S.typed[id] || '',
      on: {input: e => { S.typed[id] = e.target.value; }}});
    return box;
  }

  function checkRow(c) {
    const running = c.running;
    const states = [];
    if (running) states.push(running === 'queued' ? 'In line' : 'Running now');
    else if (c.last_run) states.push('Last run ' + when(c.last_run));
    else states.push('Not run yet');
    if (c.open) states.push(plural(c.open, 'open finding'));
    if (c.fixed) states.push(c.fixed + ' fixed');
    if (c.skipped && c.skipped.length) states.push('Not finished: ran out of time on ' + c.skipped.join('; '));
    if (c.last_run && !c.open && !c.fixed) states.push(c.found ? '' : 'nothing found');
    const input = c.box ? boxFor(c.id, c.box) : null;
    const go = el('button', {cls: 'act', type: 'button', text: running ? '...' : c.last_run && c.button === 'Run' ? 'Run again' : c.button, disabled: !!running || !c.can_run,
      title: c.can_run ? '' : 'It needs your book read in Manuscript first.', on: {click: () => run(c.id, input)}});
    return el('div', {cls: 'check', id: 'c-' + c.id}, [
      el('div', {cls: 'title'}, [c.name, c.thin ? el('span', {cls: 'pill', text: 'thin'}) : null]),
      go,
      el('div', {cls: 'says', text: c.says}),
      el('div', {cls: 'state', text: states.filter(Boolean).join('. ')}),
      input,
      el('details', {}, [el('summary', {text: 'How far to trust it'}), el('div', {text: c.trust})]),
    ]);
  }

  function drawStages() {
    const box = clear($('stages'));
    const nextStage = S.v.next && S.v.next.stage;
    S.v.stages.forEach((s, i) => {
      if (S.who && !s.checks.some(mineCheck)) return;
      const open = S.opened[s.id] !== undefined ? S.opened[s.id] : s.name === nextStage || !!S.who;
      const tally = s.big_open ? plural(s.big_open, 'big thing') + ' open' : s.ran ? s.ran + ' of ' + s.total + ' run' : '';
      const d = el('details', {cls: 'stage', open: open}, [
        el('summary', {}, [el('span', {cls: 'num', text: (i + 1) + '.'}), el('span', {cls: 'name', text: s.name}), el('span', {cls: 'plain', text: s.plain}), el('span', {cls: 'tally' + (s.big_open ? ' big' : ''), text: tally})]),
        el('p', {cls: 'why', text: s.why}),
      ].concat(s.checks.filter(mineCheck).map(checkRow)));
      d.addEventListener('toggle', () => { S.opened[s.id] = d.open; });
      box.appendChild(d);
    });
  }

  function drawAnytime() {
    const box = clear($('anytime'));
    const mine = S.v.anytime.filter(mineCheck);
    if (!mine.length) return;
    box.appendChild(el('h2', {text: 'Any time'}));
    for (const c of mine) box.appendChild(checkRow(c));
  }

  async function run(id, input) {
    const plan = input ? input.value.trim() : '';
    try {
      await call('api/run', {title: S.book, check: id, plan});
      if (input) S.typed[id] = '';
    } catch (e) {
      clear($('work')).appendChild(el('span', {text: e.message}));
      $('work').hidden = false;
      $('work').className = 'work bad';
      return;
    }
    refresh();
  }

  // ---------------------------------------------------------------- findings

  function drawFilters() {
    const box = clear($('filters'));
    for (const [id, label] of STATE) {
      const n = S.v.findings.filter(f => f.state === id && f.kind !== 'question' && (!S.only || f.check === S.only) && mineFinding(f)).length;
      box.appendChild(el('button', {type: 'button', role: 'tab', 'aria-selected': String(S.filter === id), text: label + ' ' + n,
        on: {click: () => { S.filter = id; S.all = false; drawFilters(); drawList(); }}}));
    }
    const names = {};
    for (const f of S.v.findings) if (mineFinding(f)) names[f.check] = f.check_name;
    if (Object.keys(names).length > 1) {
      const sel = el('select', {'aria-label': 'Show one check', on: {change: e => { S.only = e.target.value; drawFilters(); drawList(); }}},
        [el('option', {value: '', text: 'Every check'})].concat(Object.keys(names).map(k => el('option', {value: k, text: names[k]}))));
      sel.value = S.only;
      box.appendChild(sel);
    }
  }

  function findingCard(f) {
    const heads = [el('span', {cls: 'size-' + f.size, text: SIZE[f.size] || ''}), el('span', {text: f.check_name}), f.plan ? el('span', {text: 'If: ' + f.plan}) : null,
      f.carried ? el('span', {text: 'from the old desk'}) : null];
    const card = el('article', {cls: 'f' + (f.size === 'big' && f.state === 'open' ? ' big' : ''), id: 'f-' + f.id}, [
      el('div', {cls: 'head'}, heads),
      el('div', {cls: 'what', text: f.what}),
    ]);
    for (const w of f.where || []) {
      if (w.quote) card.appendChild(el('button', {cls: 'q', type: 'button', on: {click: () => openAt(w.section, w.quote, f.id)}}, [w.quote, el('small', {text: 'Go to section ' + w.section})]));
      else card.appendChild(el('button', {cls: 'link', type: 'button', text: 'Go to section ' + w.section, on: {click: () => openAt(w.section, '', f.id)}}));
    }
    if (f.why) card.appendChild(el('p', {}, [el('span', {cls: 'lab', text: 'Why it matters'}), f.why]));
    if (f.fix) card.appendChild(el('p', {}, [el('span', {cls: 'lab', text: 'One way to fix it'}), f.fix]));
    if (f.still_broken) card.appendChild(el('div', {cls: 'flag', text: 'You marked this Fixed, and your editor still sees it. Have another look.'}));
    if (f.gone) card.appendChild(el('div', {cls: 'flag calm', text: 'Your editor did not find this last time. It stays because you wrote on it.'}));
    if (f.changed) card.appendChild(el('div', {cls: 'flag calm', text: 'This section changed since your editor found this.'}));
    if (f.doubt) card.appendChild(el('div', {cls: 'flag calm', text: 'Your bot thinks the text may explain this: ' + f.doubt + ' You be the judge.'}));
    if (f.kind === 'question' && f.reason) card.appendChild(el('div', {cls: 'flag calm', text: f.reason}));
    if (f.unweighed) card.appendChild(el('div', {cls: 'flag calm', text: 'Found by matching words. Your bot could not weigh it, so check it yourself.'}));
    const marks = el('div', {cls: 'marks'});
    for (const [id, label] of STATE) {
      marks.appendChild(el('button', {type: 'button', cls: (f.state === id ? 'on ' : '') + (id === 'fixed' ? 'fix' : ''), text: label, 'aria-pressed': String(f.state === id), on: {click: () => mark(f, id)}}));
    }
    if (f.can_use && f.state === 'open') marks.appendChild(el('button', {type: 'button', cls: 'main', text: 'Use this', on: {click: () => useThis(f, card)}}));
    card.appendChild(marks);
    const note = el('textarea', {cls: 'mynote', placeholder: 'Your note', 'aria-label': 'Your note', on: {change: e => saveNote(f, e.target.value)}});
    note.value = f.note || '';
    card.appendChild(note);
    return card;
  }

  function drawList() {
    const box = clear($('list'));
    const shown = S.v.findings.filter(f => f.state === S.filter && f.kind !== 'question' && (!S.only || f.check === S.only) && mineFinding(f));
    const asks = S.v.findings.filter(f => f.kind === 'question' && f.state === 'open' && (!S.only || f.check === S.only) && mineFinding(f));
    if (S.filter === 'fixed') {
      const all = S.v.findings.filter(f => f.state !== 'not_a_problem').length;
      if (all) box.appendChild(el('p', {cls: 'prog', text: S.v.findings.filter(f => f.state === 'fixed').length + ' fixed of ' + all + ' so far.'}));
    }
    const questions = () => {
      if (asks.length && S.filter === 'open') box.appendChild(el('details', {cls: 'asks'}, [el('summary', {text: 'Questions for you (' + asks.length + '): things that may be fine, not counted as problems'})].concat(asks.map(findingCard))));
    };
    if (!shown.length) {
      questions();
      box.appendChild(el('p', {cls: 'empty', text: {open: S.v.findings.length ? 'Nothing open here.' : 'Nothing yet. Findings appear here as the checks run.', later: 'Nothing put off for later.', fixed: 'Nothing marked fixed yet.', not_a_problem: 'Nothing here.'}[S.filter]}));
      return;
    }
    const most = S.all ? shown.length : 12;              // biggest first; the rest a press away, not a wall
    for (const f of shown.slice(0, most)) box.appendChild(findingCard(f));
    if (shown.length > most) box.appendChild(el('button', {type: 'button', text: 'Show the other ' + (shown.length - most), on: {click: () => { S.all = true; drawList(); }}}));
    questions();
    if (S.v.old_notes) box.appendChild(el('details', {cls: 'fine'}, [el('summary', {text: 'Your notes from the old desk'}), el('div', {cls: 'oldtext', text: S.v.old_notes})]));
  }

  async function mark(f, state) {
    try { await call('api/mark', {title: S.book, id: f.id, state}); } catch (e) { return; }
    refresh();
  }
  async function saveNote(f, note) {
    try { await call('api/mark', {title: S.book, id: f.id, note}); } catch (e) { /* the box keeps what she typed */ }
    f.note = note;
  }
  function showFinding(f) {
    if (!f) return;
    S.filter = f.state;
    S.only = '';
    S.all = true;
    if (!mineFinding(f)) { S.who = ''; draw(); }
    drawFilters(); drawList();
    const n = $('f-' + f.id);
    if (n) n.scrollIntoView({block: 'center'});
  }

  // PROPOSED: Use this, for grammar and proofreading only, after she has seen the before and the after.
  function useThis(f, card) {
    const old = card.querySelector('.ba');
    if (old) { old.remove(); return; }
    const w = f.where[0];
    const box = el('div', {cls: 'ba'}, [
      el('div', {}, [el('span', {cls: 'lab', text: 'Now'}), el('span', {cls: 'was', text: w.quote})]),
      el('div', {}, [el('span', {cls: 'lab', text: 'Would be'}), el('span', {cls: 'now', text: f.correct})]),
    ]);
    const say = el('p', {cls: 'fine', text: 'You can always bring back the old version.'});
    const row = el('div', {cls: 'row'}, [
      el('button', {type: 'button', cls: 'main', text: 'Yes, put it in', on: {click: async () => {
        try {
          const got = await call('api/use', {title: S.book, id: f.id});
          await refresh();
          await Write.refreshQuiet();
          openAt(got.n, f.correct);
        } catch (e) { say.textContent = e.message; }
      }}}),
      el('button', {type: 'button', text: 'Not now', on: {click: () => box.remove()}}),
    ]);
    box.append(say, row);
    card.appendChild(box);
  }

  // ---------------------------------------------------------------- documents

  function drawDocs() {
    const box = clear($('docs'));
    const docs = S.v.documents || [];
    if (!docs.length) return;
    box.appendChild(el('h2', {text: 'Documents'}));
    box.appendChild(el('p', {cls: 'fine', text: 'About your book, for you to keep and rewrite. Never part of your manuscript.'}));
    const seen = {};
    const older = [];
    const rowOf = d => {
      const row = el('div', {cls: 'doc'}, [el('button', {cls: 'link', type: 'button', text: d.name, on: {click: () => openDoc(d, row)}}), el('span', {cls: 'when', text: when(d.made_at)})]);
      return row;
    };
    for (const d of docs) {
      if (seen[d.kind]) older.push(d);
      else box.appendChild(rowOf(d));
      seen[d.kind] = true;
    }
    if (older.length) box.appendChild(el('details', {}, [el('summary', {cls: 'fine', text: 'Earlier drafts (' + older.length + ')'})].concat(older.map(rowOf))));
  }
  async function openDoc(d, row) {
    const was = row.nextElementSibling;
    if (was && was.dataset.doc) { was.remove(); return; }
    let got;
    try { got = await call('api/document?' + q({title: S.book, id: d.id})); } catch (e) { return; }
    const wrap = el('div', {'data-doc': d.id});
    if (got.ext === 'docx') {
      wrap.appendChild(el('p', {text: 'A Word file, ready to open and send. It is in your files at: ' + got.where}));
    } else {
      const area = el('textarea', {cls: 'docbox', 'aria-label': d.name});
      area.value = got.text || '';
      const say = el('span', {cls: 'fine'});
      wrap.append(area, el('div', {cls: 'row'}, [el('button', {type: 'button', text: 'Save', on: {click: async () => {
        try { say.textContent = (await call('api/document', {title: S.book, id: d.id, text: area.value})).message; } catch (e) { say.textContent = e.message; }
      }}}), say]), el('p', {cls: 'fine', text: 'In your files at: ' + got.where}));
    }
    row.after(wrap);
  }

  // ---------------------------------------------------------------- to her words, in Write

  async function openAt(n, quote, fid) {
    showView('write');
    await Write.goTo(n, quote, fid);
  }
  // From the office: the desk, maybe one staff member's only, maybe at one finding or one check.
  async function openDesk(o) {
    o = o || {};
    S.who = o.who || '';
    await showView('desk');
    if (!S.v) return;
    draw();                                         // refresh() leaves the desk alone when nothing moved; the filter did
    if (o.finding) { showFinding(S.v.findings.find(f => f.id === o.finding)); return; }
    if (o.check) {
      const s = S.v.stages.find(x => x.checks.some(c => c.id === o.check));
      if (s) { S.opened[s.id] = true; drawStages(); }
      const n = $('c-' + o.check);
      if (n) { n.scrollIntoView({block: 'center'}); const b = n.querySelector('button'); if (b) b.focus({preventScroll: true}); }
      return;
    }
    $('v-desk').scrollTop = 0;
  }
  function openManuscript() {
    if (window.parent !== window) window.parent.postMessage({puffpai: 'open', pkg: 'manuscript', query: q({title: S.book})}, '*');
    else location.href = location.origin + '/pkg/manuscript/?' + q({title: S.book});
  }

  // ---------------------------------------------------------------- wiring

  // ---------------------------------------------------------------- the views

  // One line per view. A view is a <section class="view" id="v-..."> in index.html; show() runs each time it opens.
  const VIEWS = [
    {id: 'office', label: 'Your editor', show: () => { if (S.book) Office.open(S.book); }},
    {id: 'write', label: 'Write', show: () => { if (S.book) Write.open(S.book); Write.show(); }},
    {id: 'desk', label: "Editor's desk", show: () => refresh()},
    {id: 'board', label: 'Storyboard', show: () => { if (S.book) Board.open(S.book); }},
    {id: 'study', label: 'Study a book', show: () => { if (S.book) Study.open(S.book); }},
  ];
  function drawViews() {
    const box = clear($('views'));
    for (const v of VIEWS) {
      box.appendChild(el('button', {type: 'button', role: 'tab', id: 'tab-' + v.id, 'aria-selected': String(S.view === v.id), 'aria-controls': 'v-' + v.id,
        text: v.label, on: {click: () => { if (v.id === 'desk' && S.who) { S.who = ''; if (S.v) draw(); } showView(v.id); }}}));
    }
  }
  function showView(id) {
    const v = VIEWS.find(x => x.id === id) || VIEWS[0];
    if (S.view === 'write' && v.id !== 'write') Write.flush(true);
    if (S.view === 'office' && v.id !== 'office') Office.leave();
    S.view = v.id;
    for (const x of VIEWS) { const n = $('v-' + x.id); if (n) n.hidden = x.id !== v.id; }
    drawViews();
    try { sessionStorage.setItem('editors-desk-view', v.id); } catch (e) { /* private window */ }
    try { localStorage.setItem('editors-desk-view', v.id); } catch (e) { /* private window */ }
    return v.show();
  }
  Write.hooks.changed = () => refresh();
  Study.hooks.goTo = (n, quote) => openAt(n, quote);
  Study.hooks.manuscript = openManuscript;
  Study.hooks.refresh = () => refresh();
  Office.hooks.desk = openDesk;
  Office.hooks.goTo = openAt;
  Office.hooks.view = id => showView(id);
  Office.hooks.refresh = () => refresh();
  Board.hooks.goTo = (n, quote) => openAt(n, quote);
  Board.hooks.refresh = () => refresh();
  Write.hooks.words = total => {
    if (S.v) $('count').textContent = plural(S.v.sections.length, 'section') + ', ' + total.toLocaleString() + ' words';
  };
  let firstView = '';
  try { firstView = sessionStorage.getItem('editors-desk-view') || ''; } catch (e) { /* private window */ }
  if (!firstView) try { firstView = localStorage.getItem('editors-desk-view') || ''; } catch (e) { /* private window */ }
  S.view = null;
  showView(firstView || 'office');

  $('book').addEventListener('change', e => pick(e.target.value));
  $('vaultfree').addEventListener('change', e => { call('api/settings', {vault_free: e.target.checked}).catch(() => {}); });
  call('api/settings').then(g => { $('vaultfree').checked = g.settings.vault_free !== false; }).catch(() => {});

  // What the server says moves on its own while a check runs; she may be typing in a note, so never redraw under her.
  setInterval(() => {
    const st = S.v && S.v.status;
    const live = st && ['running', 'waiting', 'queued'].includes(st.state);
    const typing = document.activeElement && ['TEXTAREA', 'INPUT'].includes(document.activeElement.tagName);
    if (!typing && (live || document.visibilityState === 'visible')) refresh();
    if (S.view === 'office') Office.tick();          // each asks again only while something of its own is on the move
    if (S.view === 'board') Board.tick();
  }, 5000);
  loadBooks();
})();
