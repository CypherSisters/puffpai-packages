/* Editor's Desk, the Your editor view: Dewey in her office, the meeting, and her staff at their desks. All of it comes from
   api/office, built by code from what is kept; nothing here asks the brain. A pass starts only on her press, after she has
   seen the plan. The server sends data, never markup: everything here is built with text nodes. */
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
  const initial = name => String(name || '?').trim().charAt(0).toUpperCase() || '?';
  const isLive = st => !!(st && ['running', 'waiting', 'queued'].includes(st.state));
  const still = () => !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const PICTURE_DESKS = ['board', 'art'];          // Kit and Wren work on the storyboard, not on a desk of checks

  // book: the book open; o: the office as the server gave it; nodes: the parts built once
  const T = {book: null, o: null, seen: '', gen: 0, visit: false, live: false, deskLive: false, hooks: {}, nodes: null,
    voice: null, talk: null, words: false, open: 0, target: 0, raf: 0, shut: 0, wait: 0, pulse: 0, noface: false, dlg: null};

  // ---------------------------------------------------------------- loading

  function open(title) {
    build();
    if (!title) return;
    if (title !== T.book) { T.book = title; T.o = null; T.seen = ''; }
    T.visit = true;                                 // one visit stamp for each time she opens the view
    return load();
  }

  async function load() {
    const gen = ++T.gen;
    const title = T.book;
    let got;
    try {
      got = await call('api/office?' + q({title}));
    } catch (e) {
      if (gen !== T.gen) return;
      say(e.message, true);
      if (!T.o) clear(T.nodes.lines).appendChild(el('p', {cls: 'oline', text: 'Your editor could not open this book just now.'}));
      return;
    }
    if (gen !== T.gen) return;
    T.o = got;
    T.live = (got.staff || []).some(s => s.state === 'working' || s.state === 'in_line') || !!got.pass || isLive(got.status);
    const key = JSON.stringify(got);
    if (key !== T.seen) { T.seen = key; keepFocus(draw); }
    if (T.visit) {
      T.visit = false;
      call('api/visit', {title}).catch(() => { /* the next meeting says Welcome back a little early; nothing lost */ });
    }
  }

  function say(line, bad) {
    if (!T.nodes) return;
    T.nodes.say.textContent = line || '';
    T.nodes.say.className = 'osay' + (bad ? ' bad' : '');
    T.nodes.say.hidden = !line;
  }

  // the page is redrawn while she works; the button she is on stays the one she is on
  function keepFocus(fn) {
    const a = document.activeElement;
    const k = a && a.dataset ? a.dataset.k : '';
    fn();
    if (!k) return;
    const n = $('obody').querySelector('[data-k="' + (window.CSS && CSS.escape ? CSS.escape(k) : k) + '"]');
    if (n && n !== a) n.focus({preventScroll: true});
  }

  // ---------------------------------------------------------------- the parts built once

  function build() {
    if (T.nodes) return;
    const n = T.nodes = {};
    n.face = portrait();
    n.name = el('h1', {cls: 'oname', text: 'Dewey'});
    n.role = el('p', {cls: 'orole', text: 'Editor-in-chief'});
    n.lines = el('div', {cls: 'olines'});
    n.hear = el('button', {type: 'button', 'data-k': 'hear', 'aria-pressed': 'false', text: 'Hear it', hidden: true, on: {click: hear}});
    n.voices = el('select', {'data-k': 'voices', 'aria-label': 'Dewey\'s voice', title: 'Dewey\'s voice', hidden: true, on: {change: chooseVoice}});
    n.all = el('button', {type: 'button', cls: 'main owide', 'data-k': 'all', text: 'Have the whole staff edit this book, big to small', on: {click: () => passDialog('all')}});
    n.say = el('p', {cls: 'osay', role: 'status', 'aria-live': 'polite', hidden: true});
    n.pass = el('p', {cls: 'opass', hidden: true});
    n.staff = el('div', {cls: 'ostaff'});
    const box = clear($('obody'));
    box.append(
      el('section', {cls: 'ohead', 'aria-label': 'Your meeting'}, [
        el('div', {cls: 'oframe'}, [n.face]),
        el('div', {cls: 'omeet'}, [n.name, n.role, n.lines, el('div', {cls: 'row'}, [n.hear, n.voices]), n.pass, el('div', {cls: 'row'}, [n.all]), n.say]),
      ]),
      el('h2', {text: 'Your staff'}),
      n.staff,
    );
    pickVoice();
    if (window.speechSynthesis && speechSynthesis.addEventListener) speechSynthesis.addEventListener('voiceschanged', pickVoice);
  }

  // Dewey's face: the 768 picture in a round frame, and a mouth over it that moves only while she speaks
  function portrait() {
    const img = sv('image', {href: 'img/staff/chief.png', x: '0', y: '0', width: '768', height: '768'});
    T.mouth = sv('path', {d: mouthPath(0), fill: '#4a161c', stroke: '#8c2f33', 'stroke-width': '1.6', visibility: 'hidden'});
    img.addEventListener('error', () => { img.remove(); T.noface = true; T.mouth.setAttribute('visibility', 'hidden'); });
    T.init = sv('text', {x: '384', y: '250', 'text-anchor': 'middle', 'dominant-baseline': 'central', class: 'oinit-svg'});
    T.init.textContent = 'D';
    return T.svg = sv('svg', {viewBox: '164 30 440 440', class: 'oface', role: 'img', 'aria-label': 'Dewey, your editor-in-chief'}, [
      sv('defs', {}, [sv('clipPath', {id: 'oface-clip'}, [sv('circle', {cx: '384', cy: '250', r: '215'})])]),
      sv('circle', {cx: '384', cy: '250', r: '215', class: 'oface-bg'}),
      T.init,
      sv('g', {'clip-path': 'url(#oface-clip)'}, [img, T.mouth]),
      sv('circle', {cx: '384', cy: '250', r: '215', fill: 'none', 'stroke-width': '5', class: 'oface-ring'}),
    ]);
  }

  // ---------------------------------------------------------------- the mouth

  const mouthPath = open => 'M345 369 Q384 ' + (381 - 6 * open) + ' 423 368 Q384 ' + (381 + 26 * open) + ' 345 369 Z';
  function frame() {
    T.open = T.target > T.open ? T.open + (T.target - T.open) * 0.5 : T.open * 0.85;
    const show = T.open >= 0.02 && !T.noface;
    T.mouth.setAttribute('visibility', show ? 'visible' : 'hidden');
    if (show) T.mouth.setAttribute('d', mouthPath(T.open));
    T.raf = (T.talk || T.open >= 0.02) ? requestAnimationFrame(frame) : 0;
  }
  function setTarget(v) {
    if (still()) return;
    T.target = v;
    clearTimeout(T.shut);
    T.shut = setTimeout(() => { T.target = 0; }, 110);       // each word opens it, and it closes between words
    if (!T.raf) T.raf = requestAnimationFrame(frame);
  }
  const PULSE = [0.85, 0.2, 0.6, 0.05, 1, 0.35, 0.7, 0.1, 0.5, 0.9];
  function startPulse() {
    stopPulse();
    let i = 0;
    T.pulse = setInterval(() => setTarget(PULSE[i++ % PULSE.length]), 140);
  }
  function stopPulse() { clearInterval(T.pulse); T.pulse = 0; }

  // ---------------------------------------------------------------- Hear it: the computer's own voice, nothing leaves it

  const VOICE_KEY = 'editors-desk.voice';
  const WOMEN = /zira|hazel|susan|linda|heather|catherine|samantha|karen|moira|tessa|victoria|fiona|allison|ava|serena|kate|sonia|libby|aria|jenny|michelle|female|woman/i;

  function pickVoice() {
    T.voice = null;
    let pool = [];
    const ss = window.speechSynthesis;
    if (ss && typeof window.SpeechSynthesisUtterance === 'function') {
      let all = [];
      try { all = ss.getVoices() || []; } catch (e) { all = []; }
      const local = all.filter(v => v.localService === true);
      const lang = (document.documentElement.lang || 'en').toLowerCase().slice(0, 2);
      const starts = (v, l) => String(v.lang || '').toLowerCase().startsWith(l);
      const mine = local.filter(v => starts(v, lang));
      const en = local.filter(v => starts(v, 'en'));
      pool = mine.length ? mine : en.length ? en : local;
      let kept = '';
      try { kept = localStorage.getItem(VOICE_KEY) || ''; } catch (e) { kept = ''; }
      // the one she chose; or a woman's voice, Dewey being a woman; or whatever the computer has
      T.voice = pool.find(v => v.name === kept) || pool.find(v => WOMEN.test(v.name)) || pool[0] || null;
    }
    T.pool = pool;
    if (T.nodes) {
      const sel = clear(T.nodes.voices);
      pool.forEach(v => sel.append(el('option', {value: v.name, text: String(v.name).replace(/^Microsoft\s+/, '').replace(/\s+-\s+.*$/, '')})));
      if (T.voice) sel.value = T.voice.name;
    }
    showHear();
  }
  function chooseVoice() {
    const v = (T.pool || []).find(x => x.name === T.nodes.voices.value);
    if (!v) return;
    stopTalk();
    T.voice = v;
    try { localStorage.setItem(VOICE_KEY, v.name); } catch (e) { /* it holds for this visit only */ }
  }
  function showHear() {
    if (!T.nodes) return;
    const text = T.o && T.o.meeting && T.o.meeting.say;
    T.nodes.hear.hidden = !(T.voice && text);
    T.nodes.voices.hidden = T.nodes.hear.hidden || (T.pool || []).length < 2;
    if (T.nodes.hear.hidden && T.talk) stopTalk();
  }

  function hear() {
    if (T.talk) { stopTalk(); return; }
    const text = T.o && T.o.meeting && T.o.meeting.say;
    if (!text || !T.voice) return;
    const u = new SpeechSynthesisUtterance(text);
    u.voice = T.voice;
    u.lang = T.voice.lang;
    T.talk = u;
    T.words = false;
    u.addEventListener('start', () => {
      clearTimeout(T.wait);
      T.wait = setTimeout(() => { if (T.talk === u && !T.words && !still()) startPulse(); }, 400);
    });
    u.addEventListener('boundary', e => {
      if (T.talk !== u || (e.name && e.name !== 'word')) return;
      T.words = true;
      stopPulse();
      const len = e.charLength || ((text.slice(e.charIndex || 0).match(/^\S+/) || [''])[0].length);
      setTarget(Math.min(1, 0.35 + 0.12 * (len % 6)));
    });
    u.addEventListener('end', () => { if (T.talk === u) quiet(); });
    u.addEventListener('error', e => {
      if (T.talk !== u) return;
      quiet();
      if (!['interrupted', 'canceled'].includes(e.error)) say('Your computer could not read this aloud.', true);
    });
    T.nodes.hear.textContent = 'Stop';                // before speak(): a voice that fails at once puts it back
    T.nodes.hear.setAttribute('aria-pressed', 'true');
    try {
      speechSynthesis.cancel();
      speechSynthesis.speak(u);
    } catch (e) { quiet(); say('Your computer could not read this aloud.', true); }
  }
  function quiet() {
    T.talk = null;
    clearTimeout(T.wait);
    stopPulse();
    T.target = 0;
    if (T.nodes) { T.nodes.hear.textContent = 'Hear it'; T.nodes.hear.setAttribute('aria-pressed', 'false'); }
  }
  function stopTalk() {
    if (!T.talk) return;
    quiet();
    try { speechSynthesis.cancel(); } catch (e) { /* nothing was speaking */ }
  }

  // ---------------------------------------------------------------- drawing

  const staffOf = id => ((T.o && T.o.staff) || []).find(s => s.id === id);
  const passName = who => (who === 'all' ? 'the whole staff' : (staffOf(who) || {}).name || 'your editor');

  function draw() {
    const o = T.o, n = T.nodes;
    const chief = o.chief || {};
    const name = chief.name || 'Dewey';
    n.name.textContent = name;
    n.role.textContent = chief.role || 'Editor-in-chief';
    T.init.textContent = initial(name);
    T.svg.setAttribute('aria-label', name + ', ' + (chief.role || 'Editor-in-chief'));
    const lines = (o.meeting && o.meeting.lines) || [];
    clear(n.lines);
    for (const l of lines) n.lines.appendChild(lineOf(l));
    if (!lines.length) n.lines.appendChild(el('p', {cls: 'oline', text: 'Nothing new to tell you.'}));
    showHear();
    const p = o.pass;
    n.pass.hidden = !p;
    if (p) {
      const left = p.left || [];
      n.pass.textContent = 'On now: ' + passName(p.who) + ', ' + (p.done || 0) + ' of ' + (p.total || 0) + ' done' + (left.length ? '. Still to come: ' + left.join(', ') : '') + '.';
    }
    clear(n.staff);
    for (const s of o.staff || []) n.staff.appendChild(card(s));
    if (!(o.staff || []).length) n.staff.appendChild(el('p', {cls: 'fine', text: 'Your staff is not here yet.'}));
  }

  function lineOf(l) {
    const p = el('p', {cls: 'oline' + (l.kind ? ' k-' + l.kind : '')}, [String(l.text || '')]);
    const b = lineButton(l);
    if (b) p.append(' ', b);
    return p;
  }
  function lineButton(l) {
    const k = 'line-' + (l.kind || '') + '-' + String(l.text || '').slice(0, 40);
    if (l.do && l.do.pass) return el('button', {type: 'button', cls: 'small', 'data-k': k, text: 'Ask ' + passName(l.do.pass) + '...', on: {click: () => passDialog(l.do.pass)}});
    if (l.do && l.do.check) return el('button', {type: 'button', cls: 'small', 'data-k': k, text: 'Show me', on: {click: () => T.hooks.desk({check: l.do.check})}});
    if (l.go) return el('button', {type: 'button', cls: 'small', 'data-k': k, text: 'Show me', on: {click: () => go(l.go)}});
    return null;
  }
  function go(g) {
    if (g.view === 'desk') T.hooks.desk({who: g.who || '', finding: g.finding || ''});
    else if (g.view === 'write') T.hooks.goTo(g.section, '', g.finding);
    else T.hooks.view(g.view || 'desk');
  }

  function roundPic(src, name) {
    const box = el('span', {cls: 'opic', 'aria-hidden': 'true'}, [el('span', {cls: 'oinit', text: initial(name)})]);
    if (src) {
      const img = el('img', {src, alt: '', width: '96', height: '96', loading: 'lazy'});
      img.addEventListener('error', () => img.remove());       // the initial shows instead
      box.appendChild(img);
    }
    return box;
  }

  function card(s) {
    const pictures = PICTURE_DESKS.includes(s.id);
    const opens = () => (pictures ? T.hooks.view('board') : T.hooks.desk({who: s.id}));
    const where = pictures ? 'Open the storyboard' : 'Open ' + s.name + "'s desk";
    const head = el('div', {cls: 'ohd'}, [
      el('button', {type: 'button', cls: 'oport', 'data-k': 'face-' + s.id, 'aria-label': where, title: where, on: {click: opens}}, [roundPic(s.img, s.name)]),
      el('div', {cls: 'owho'}, [
        el('button', {type: 'button', cls: 'link ocname', 'data-k': 'name-' + s.id, title: where, text: s.name || '', on: {click: opens}}),
        el('div', {cls: 'ocrole', text: s.role || ''}),
      ]),
    ]);
    const c = el('article', {cls: 'ocard' + (s.state === 'working' ? ' busy' : ''), 'aria-label': (s.name || '') + ', ' + (s.role || '')}, [head]);
    if (s.does) c.appendChild(el('p', {cls: 'odoes', text: s.does}));
    c.appendChild(stateOf(s));
    const counts = countsOf(s);
    if (counts) c.appendChild(el('p', {cls: 'ocount', text: counts}));
    const top = (s.top || []).slice(0, 3);
    if (top.length) {
      c.appendChild(el('ul', {cls: 'otop', 'aria-label': 'Biggest open things'}, top.map(t => el('li', {cls: t.size === 'big' ? 'big' : ''}, [
        el('button', {type: 'button', cls: 'link', 'data-k': 'top-' + t.id, text: t.what || '', on: {click: () => T.hooks.desk({who: s.id, finding: t.id})}}),
      ]))));
    }
    const acts = el('div', {cls: 'row oacts'});
    if (s.pass) {
      const ok = !!s.pass.can_start;
      acts.appendChild(el('button', {type: 'button', 'data-k': 'pass-' + s.id, text: s.pass.label || 'Ask ' + s.name + ' to go over the book', disabled: !ok,
        title: ok ? (s.pass.time || '') : (s.pass.why_not || ''), on: {click: () => passDialog(s.id)}}));
      if (ok && s.pass.time) acts.appendChild(el('span', {cls: 'fine', text: s.pass.time}));
    }
    if (s.id === 'line') acts.appendChild(el('button', {type: 'button', cls: 'link', 'data-k': 'study', text: 'Study a book', on: {click: () => T.hooks.view('study')}}));
    if (acts.firstChild) c.appendChild(acts);
    if (s.pass && !s.pass.can_start && s.pass.why_not) c.appendChild(el('p', {cls: 'fine', text: s.pass.why_not}));
    return c;
  }

  function stateOf(s) {
    const box = el('div', {cls: 'ostate'});
    if (s.state === 'working') {
      box.append(el('span', {cls: 'obar', 'aria-hidden': 'true'}, [el('span')]), el('span', {text: s.doing || 'Working on your book now.'}));
    } else if (s.state === 'in_line') {
      box.appendChild(el('span', {text: 'In line' + (s.doing ? ': ' + s.doing : '')}));
    } else if (s.state === 'new') {
      box.appendChild(el('span', {text: 'Has not looked at this book yet'}));
    } else if (s.doing) {
      box.appendChild(el('span', {text: s.doing}));
    } else if (s.last_worked) {
      box.appendChild(el('span', {cls: 'fine', text: 'Last worked on it ' + when(s.last_worked)}));
    }
    return box;
  }

  function countsOf(s) {
    const b = (T.o && T.o.board) || {};
    if (s.id === 'art') return plural(b.painted || 0, 'panel') + ' painted';
    if (s.id === 'board') return b.panels ? plural(b.panels, 'panel') + ' drawn' + (b.made_at ? ', ' + when(b.made_at) : '') : 'No storyboard yet';
    if (s.state === 'new') return '';
    const bits = [];
    if (typeof s.open === 'number') bits.push(s.open + ' open');
    if (s.big) bits.push(s.big + ' big');
    if (s.questions) bits.push(plural(s.questions, 'question'));
    return bits.join(', ');
  }

  // ---------------------------------------------------------------- a pass: the plan first, then her Start

  async function passDialog(who) {
    const back = document.activeElement;
    if (!T.dlg) {
      T.dlg = el('dialog', {cls: 'odlg', 'aria-labelledby': 'odlg-h'});
      T.dlg.addEventListener('close', () => { if (T.back && T.back.isConnected) T.back.focus(); });
      document.body.appendChild(T.dlg);
    }
    T.back = back;
    const dlg = T.dlg;
    const name = passName(who);
    const head = el('h2', {id: 'odlg-h', text: who === 'all' ? 'The whole staff, big to small' : 'Ask ' + name + ' to go over your book'});
    const shut = el('button', {type: 'button', text: 'Not now', on: {click: () => dlg.close()}});
    clear(dlg).append(head, el('p', {cls: 'fine', role: 'status', text: 'Looking at what it takes...'}), el('div', {cls: 'row'}, [shut]));
    if (!dlg.open) dlg.showModal();
    let plan;
    try {
      plan = await call('api/pass?' + q({title: T.book, who}));
    } catch (e) {
      clear(dlg).append(head, el('p', {cls: 'bad', role: 'alert', text: e.message}), el('div', {cls: 'row'}, [shut]));
      shut.textContent = 'Close';
      shut.focus();
      return;
    }
    if (!dlg.open) return;
    const checks = plan.checks || [], out = plan.left_out || [];
    const kids = [head];
    if (checks.length) {
      kids.push(el('h3', {text: (who === 'all' ? 'What they would do' : 'What ' + (plan.name || name) + ' would do') + ', in this order'}));
      kids.push(el('ol', {cls: 'oplan'}, checks.map(c => el('li', {}, [c.name || c.id || '', c.estimate ? el('span', {cls: 'fine', text: ', ' + String(c.estimate).replace(/^That is /, '').replace(', and I have not timed your bot yet', '').replace(/\.$/, '')}) : null]))));
    }
    if (out.length) {
      kids.push(el('h3', {text: 'Left out'}));
      kids.push(el('ul', {cls: 'oplan'}, out.map(c => el('li', {}, [el('b', {text: c.name || c.id || ''}), c.why ? ': ' + c.why : '']))));
    }
    kids.push(el('p', {cls: 'otime', text: plan.time ? 'In all: ' + plan.time + '.' : 'Your bot has not been timed yet, so there is no time to give.'}));
    const said = el('p', {cls: 'bad', role: 'alert', hidden: true});
    kids.push(said);
    const row = el('div', {cls: 'row'});
    if (plan.can_start) {
      const start = el('button', {type: 'button', cls: 'main', text: 'Start', on: {click: async () => {
        start.disabled = true;
        let got;
        try {
          got = await call('api/pass', {title: T.book, who});
        } catch (e) {
          said.textContent = e.message; said.hidden = false; start.disabled = false;
          return;
        }
        dlg.close();
        const queued = got.queued || [];
        const bits = ['Started.'];
        if (queued.length) bits.push('In line: ' + queued.join(', ') + '.');
        if (got.time) bits.push('In all: ' + got.time + '.');
        say(bits.join(' '));
        load();
        if (T.hooks.refresh) T.hooks.refresh();
      }}});
      row.append(start, shut);
      shut.textContent = 'Not now';
    } else {
      kids.push(el('p', {text: plan.why_not || 'Nothing can start just now.'}));
      shut.textContent = 'Close';
      row.append(shut);
    }
    kids.push(row);
    clear(dlg).append(...kids);
    (row.firstChild || shut).focus();
  }

  // ---------------------------------------------------------------- what the shell tells this view

  // what the desk knows: when the line goes quiet, what the staff did is new
  function desk(v) {
    if (!v || v.title !== T.book) return;
    const live = isLive(v.status);
    if (T.deskLive && !live && !$('v-office').hidden) load();
    T.deskLive = live;
  }
  // every five seconds from the shell, while she is in this view: ask again only while someone is working or in line
  function tick() {
    if (T.live && T.book && document.visibilityState === 'visible') load();
  }
  // she left the view: the voice stops with her
  function leave() { stopTalk(); }
  function note(line) { build(); say(line, true); }

  window.OfficeView = {open, desk, tick, leave, note, hooks: T.hooks};
})();
