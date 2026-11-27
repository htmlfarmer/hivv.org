/**
 * HIVV Live Stats — the ONE binding between the website and the hive.
 * ---------------------------------------------------------------------------
 * The site used to hardcode its numbers ("3 bees", "64 TOK/S", "108 GB RAM").
 * Those were true on the day they were typed and a lie the next morning.
 *
 * Now every number the site shows is fetched from ONE endpoint
 * (/api/hivv/stats — the brand door; /api/hive/stats is the same router) and
 * written into the DOM by a declarative attribute:
 *
 *   <span data-hivv-stat="bees.online">…</span>
 *
 * HOUSE RULES (kept in step with AGENTS.md):
 *   · null means NO DATA — the element shows an em dash, never a zero.
 *   · a failed fetch must not blank a page or leave a stale lie on screen.
 *   · the same file, the same numbers, every page.
 *
 * Usage:
 *   <script src="/js/hivv-live.js"></script>
 *   <script>HIVVLive.start();</script>
 *
 * Optional attributes:
 *   data-hivv-stat="path.to.value"   the number to write
 *   data-hivv-format="int|dec1|compact|pct|tps|ms|bytes|duration|plain"
 *   data-hivv-default="—"            what to show when there is no data
 *   data-hivv-append="% total"       suffix text (format-aware tokens)
 *   data-hivv-prefix="~ "            literal prefix
 *   data-hivv-title="path"           also set title= from another path
 */
(function (global) {
  'use strict';

  var ENDPOINT = '/api/hivv/stats';
  var DEFAULT_INTERVAL = 30000;

  function isNum(v) { return typeof v === 'number' && isFinite(v); }

  function group(n) {
    try { return n.toLocaleString('en-US'); } catch (_e) { return String(n); }
  }

  function compact(n) {
    var abs = Math.abs(n);
    if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, '') + 'B';
    if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
    if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return group(n);
  }

  var FORMATTERS = {
    plain: function (n) { return String(n); },
    int: function (n) { return group(Math.round(n)); },
    dec1: function (n) { return (Math.round(n * 10) / 10).toFixed(1); },
    compact: compact,
    pct: function (n) { return (Math.round(n * 10) / 10) + '%'; },
    tps: function (n) { return (Math.round(n * 10) / 10) + ' tok/s'; },
    ms: function (n) { return group(Math.round(n)) + ' ms'; },
    bytes: function (n) {
      if (Math.abs(n) >= 1024) return (Math.round(n / 102.4) / 10) + ' GB';
      return group(Math.round(n)) + ' MB';
    },
    duration: function (n) {
      n = Math.max(0, Math.floor(n));
      var d = Math.floor(n / 86400), h = Math.floor((n % 86400) / 3600), m = Math.floor((n % 3600) / 60);
      if (d) return d + 'd ' + h + 'h';
      if (h) return h + 'h ' + m + 'm';
      return m + 'm';
    },
    uptime: function (n) { return FORMATTERS.duration(n); }
  };

  // Resolve a dotted path against the stats payload. Returns undefined when
  // the path does not exist so the caller can tell "no data" from "zero".
  function getPath(obj, path) {
    if (!obj || !path) return undefined;
    var parts = String(path).split('.');
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || cur === undefined) return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  var HIVVLive = {
    endpoint: ENDPOINT,
    intervalMs: DEFAULT_INTERVAL,
    data: null,
    error: null,
    _timer: null,
    _subs: [],
    _started: false,

    get: function (path) { return getPath(this.data, path); },

    format: function (value, fmt) {
      if (!isNum(value)) return null;
      var f = FORMATTERS[fmt] || FORMATTERS.int;
      try { return f(value); } catch (_e) { return String(value); }
    },

    /**
     * Write the payload into the DOM.
     * @param {object} data   stats payload (defaults to this.data)
     * @param {Element} root  subtree to scan (defaults to document)
     */
    apply: function (data, root) {
      if (!data) data = this.data;
      if (!data) return;
      var scope = root || document;
      var nodes = scope.querySelectorAll('[data-hivv-stat]');
      for (var i = 0; i < nodes.length; i++) {
        var el = nodes[i];
        var path = el.getAttribute('data-hivv-stat');
        var fmt = el.getAttribute('data-hivv-format') || 'int';
        var dflt = el.getAttribute('data-hivv-default');
        var raw = getPath(data, path);
        var text;
        if (isNum(raw)) {
          text = this.format(raw, fmt);
          var pre = el.getAttribute('data-hivv-prefix');
          var app = el.getAttribute('data-hivv-append');
          if (pre) text = pre + text;
          if (app) {
            // "%" and "% total" both resolve against the SAME value.
            if (app.indexOf('%') === 0) {
              var rest = app.slice(1).trim();
              text = text + (rest ? ' ' + rest : '');
            } else {
              text = text + ' ' + app;
            }
          }
        } else if (raw === true || raw === false) {
          text = raw ? 'yes' : 'no';
        } else if (raw !== null && raw !== undefined && typeof raw !== 'object') {
          text = String(raw);
        } else {
          text = dflt !== null && dflt !== undefined ? dflt : '—';
        }
        if (el.textContent !== text) el.textContent = text;
        el.setAttribute('data-hivv-state', isNum(raw) ? 'ok' : 'nodata');

        var titlePath = el.getAttribute('data-hivv-title');
        if (titlePath) {
          var tv = getPath(data, titlePath);
          el.setAttribute('title', isNum(tv) ? String(tv) : '');
        }
      }

      // The bee pill: a live "N/M online" with a truthful tooltip + dot colour.
      var pill = document.querySelector('[data-hivv-pill]');
      if (pill) {
        var online = getPath(data, 'bees.online');
        var total = getPath(data, 'bees.registered');
        var countEl = pill.querySelector('[data-hivv-pill-count]');
        if (countEl && isNum(online) && isNum(total)) {
          countEl.textContent = total > 0 ? online + '/' + total : String(online);
        } else if (countEl) {
          countEl.textContent = '—';
        }
        var dot = pill.querySelector('[data-hivv-pill-dot]');
        if (dot) {
          var colour = !isNum(online) ? '#a3a3a3'
            : online === 0 ? '#ef4444'
            : (total && online < total) ? '#fbbf24' : '#22c55e';
          dot.style.background = colour;
          dot.style.boxShadow = '0 0 8px ' + colour + '80';
        }
        if (isNum(online) && isNum(total)) {
          pill.setAttribute('title',
            online + ' bee' + (online === 1 ? '' : 's') + ' online · ' + total + ' total registered');
        }
      }
    },

    refresh: function () {
      var self = this;
      var opts = { cache: 'no-store', headers: { 'Accept': 'application/json' } };
      return fetch(this.endpoint, opts).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (j) {
        self.error = null;
        if (j && j.ok === false) throw new Error(j.error || 'stats unavailable');
        self.data = j;
        self.apply(j);
        for (var i = 0; i < self._subs.length; i++) {
          try { self._subs[i](j); } catch (_e) { /* a subscriber must not break the rest */ }
        }
        return j;
      }).catch(function (e) {
        self.error = e;
        // Show the truth: no data. Never leave the last number frozen on
        // screen pretending to be current.
        self.apply(null);
        var nodes = document.querySelectorAll('[data-hivv-stat]');
        for (var i = 0; i < nodes.length; i++) {
          var el = nodes[i];
          if (el.getAttribute('data-hivv-state') === 'nodata') continue;
          var dflt = el.getAttribute('data-hivv-default');
          el.textContent = dflt !== null && dflt !== undefined ? dflt : '—';
          el.setAttribute('data-hivv-state', 'nodata');
        }
        var pill = document.querySelector('[data-hivv-pill]');
        if (pill) {
          var countEl = pill.querySelector('[data-hivv-pill-count]');
          if (countEl) countEl.textContent = '—';
          var dot = pill.querySelector('[data-hivv-pill-dot]');
          if (dot) { dot.style.background = '#a3a3a3'; dot.style.boxShadow = 'none'; }
          pill.setAttribute('title', 'bee status unavailable');
        }
        return null;
      });
    },

    subscribe: function (cb) {
      if (typeof cb === 'function') this._subs.push(cb);
      return this;
    },

    start: function (opts) {
      opts = opts || {};
      if (opts.endpoint) this.endpoint = opts.endpoint;
      if (opts.intervalMs) this.intervalMs = opts.intervalMs;
      var self = this;
      // Idempotent. hivv-nav.js starts this AND the page starts it, so a
      // second call used to fire a second immediate fetch — every page
      // requested /api/hivv/stats two or three times on load. Already
      // running? Say so and do nothing; the interval covers it.
      if (this._started) return this;
      this._started = true;

      var boot = function () { self.refresh(); };
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
      } else {
        boot();
      }

      this._timer = setInterval(function () { self.refresh(); }, this.intervalMs);

      // A tab that was asleep for an hour must not show an hour-old number.
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) self.refresh();
      });

      // A page restored from bfcache gets the same treatment — and ONLY
      // then. `pageshow` fires on EVERY load, including the first, so an
      // unguarded handler doubled the very first fetch: every page asked
      // /api/hivv/stats twice before anything had changed. `persisted` is
      // true only when the browser is handing back a page it froze.
      global.addEventListener('pageshow', function (e) {
        if (e && e.persisted) self.refresh();
      });

      return this;
    },

    stop: function () {
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      this._started = false;
      return this;
    }
  };

  global.HIVVLive = HIVVLive;
  if (typeof module !== 'undefined' && module.exports) module.exports = HIVVLive;
})(typeof window !== 'undefined' ? window : this);
