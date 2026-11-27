/**
 * HIVVSuggest — ONE suggestions component for every chat surface.
 *
 * WHY A COMPONENT AND NOT FOUR HARD-CODED PROMPTS
 * ───────────────────────────────────────────────
 * A blank chat box is a dead end; a person who does not know what to ask
 * leaves. Humans LOVE suggestions — but a suggestion that is wrong is worse
 * than none. So this component asks the hub (/api/suggestions) for chips
 * that are:
 *
 *   * RELATED  — built from what the visitor is doing right now (the draft
 *     they are typing, the thread they are in) and from the LIVE hive (the
 *     models it serves, the bees on the roster, the measured numbers).
 *     THE STATS PIVOT lives here: someone typing about a model can be
 *     handed "show me the stats for gemma-4-E2B" — the question they were
 *     about to ask anyway.
 *   * DIFFERENT — the serendipity channel: a curated pool about THIS hive,
 *     rotated by a seed so two visitors do not see the same four, plus a
 *     shuffle button that gives NEW ones.
 *   * WRITTEN BY A BEE — opt-in ("live"): the hive suggests for itself.
 *
 * THE CONTRACT (why this never breaks a chat)
 * ──────────────────────────────────────────
 *   * a chip FILLS the input; it never posts a message by itself — the
 *     assistant's turn is never interrupted, and the tool contract (one
 *     consistent answer) is untouched;
 *   * the suggestions request is its OWN request: it is never a frame in the
 *     chat stream, and it never carries tool_calls;
 *   * when the request fails, the static fallback chips stay and the console
 *     stays quiet — the site's law: a failed request may DEGRADE the page,
 *     never BREAK it.
 *
 * Usage:
 *   HIVVSuggest.mount({
 *     target: document.getElementById('suggest-row'),
 *     getDraft: () => document.getElementById('chat-input').value,
 *     getConversation: () => lastFewTurns,
 *     onPick: (text) => { input.value = text; input.focus(); }
 *   });
 */
(function () {
  'use strict';

  var FALLBACK = [
    'What is this hive doing right now?',
    'Which model is fastest here?',
    'How do I add my own machine to the hive?',
    'What tools can I use here?',
    'How does the router choose which bee answers me?',
    'Show me the stats for this hive'
  ];

  var state = {
    target: null,
    getDraft: null,
    getConversation: null,
    onPick: null,
    chips: [],
    timer: null,
    inflight: false,
    lastKey: '',
    liveAsked: false
  };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function safeJson(url) {
    // The site's law: a failed / HTML / blocked answer is null, never a throw.
    return fetch(url, { cache: 'no-store' })
      .then(function (r) {
        if (!r || !r.ok) return null;
        var ct = (r.headers && r.headers.get('content-type')) || '';
        if (ct && ct.indexOf('json') === -1) return null;
        return r.json();
      })
      .catch(function () { return null; });
  }

  function renderFallback() {
    if (!state.target) return;
    state.target.innerHTML = '';
    FALLBACK.forEach(function (text) {
      state.target.appendChild(chip(text, null, 'fallback'));
    });
  }

  function chip(text, why, kind) {
    var b = el('button', 'hivv-suggest-chip');
    b.type = 'button';
    b.textContent = text;
    if (kind) b.setAttribute('data-kind', kind);
    if (why) b.setAttribute('title', why);
    b.addEventListener('click', function () {
      if (state.onPick) state.onPick(text);
    });
    return b;
  }

  function render(list) {
    if (!state.target) return;
    state.chips = list || [];
    if (!state.chips.length) { renderFallback(); return; }
    state.target.innerHTML = '';
    state.chips.forEach(function (s) {
      state.target.appendChild(chip(s.prompt, s.why || null, s.kind || 'context'));
    });
    var more = el('button', 'hivv-suggest-more', '↻');
    more.type = 'button';
    more.title = 'show me something different';
    more.addEventListener('click', function () { shuffle(); });
    state.target.appendChild(more);
  }

  function contextKey() {
    var draft = '';
    try { draft = (state.getDraft && state.getDraft()) || ''; } catch (_e) { draft = ''; }
    return String(draft).trim().slice(0, 120);
  }

  function buildUrl(extra) {
    var draft = contextKey();
    var conv = [];
    try { conv = (state.getConversation && state.getConversation()) || []; } catch (_e) { conv = []; }
    var p = ['mode=mixed', 'limit=8', 'seed=' + encodeURIComponent(String(Date.now() % 100000))];
    if (draft) p.push('q=' + encodeURIComponent(draft));
    if (conv.length) p.push('conversation=' + encodeURIComponent(JSON.stringify(conv)));
    if (extra) p.push(extra);
    return '/api/suggestions?' + p.join('&');
  }

  /** The instant channels — cheap, local, never blocked. */
  function refresh() {
    if (!state.target || state.inflight) return;
    state.inflight = true;
    var key = contextKey();
    safeJson(buildUrl()).then(function (j) {
      state.inflight = false;
      if (!j || !j.ok || !j.suggestions || !j.suggestions.length) return;   // keep what we have
      state.lastKey = key;
      render(j.suggestions);
      // the bees write their own — in the BACKGROUND, never blocking
      if (!state.liveAsked) {
        state.liveAsked = true;
        safeJson(buildUrl('live=1')).then(function (l) {
          if (!l || !l.suggestions || !l.suggestions.length) return;
          var written = l.suggestions.filter(function (s) { return s.kind === 'model'; });
          if (!written.length) return;
          var merged = j.suggestions.concat(written).slice(0, 8);
          if (contextKey() === state.lastKey) render(merged);
        });
      }
    }).catch(function () { state.inflight = false; });
  }

  /** New random ones — the serendipity channel on demand. */
  function shuffle() {
    if (!state.target) return;
    safeJson('/api/suggestions?mode=random&limit=5&seed=' + Date.now())
      .then(function (j) {
        if (!j || !j.suggestions || !j.suggestions.length) return;
        render(j.suggestions);
      });
  }

  /** The draft changed — re-ask, but only when it matters (debounced). */
  function onDraftChanged() {
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(function () {
      if (contextKey() === state.lastKey) return;
      refresh();
    }, 900);
  }

  function mount(opts) {
    opts = opts || {};
    state.target = opts.target || null;
    state.getDraft = opts.getDraft || null;
    state.getConversation = opts.getConversation || null;
    state.onPick = opts.onPick || null;
    if (!state.target) return null;
    state.target.classList.add('hivv-suggest-row');
    renderFallback();       // something is ALWAYS there, even offline
    refresh();              // then the real thing, as soon as it answers
    if (opts.input) {
      var input = opts.input;
      input.addEventListener('input', onDraftChanged);
      input.addEventListener('keyup', onDraftChanged);
    }
    return state;
  }

  window.HIVVSuggest = {
    mount: mount,
    refresh: refresh,
    shuffle: shuffle,
    state: state,
    FALLBACK: FALLBACK
  };
})();
