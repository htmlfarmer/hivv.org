/**
 * HIVV Nav — the ONE navigation bar for the whole site.
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * Every page used to carry its OWN hand-written nav, and every one of them
 * listed a DIFFERENT subset of the site. nodes.html linked to /hivv.html
 * (a page that does not exist). status.html linked to /api.html while every
 * other page linked to /docs. The map had no Home link. The "some pages are
 * missing" feeling was not a feeling: it was forty different navs.
 *
 * THE FIX
 * One component, one inventory, every page. It:
 *   · lists EVERY page in the site, grouped by what it is FOR
 *   · marks the page you are on (the old navs mostly guessed)
 *   · filters the list as you type
 *   · carries the live bee pill (real numbers, from /api/hivv/stats)
 *   · works the same on all 40+ pages
 *
 * USAGE
 *   <script src="/js/hivv-nav.js"></script>
 * or, to render only the link row inside an existing header:
 *   <div data-hivv-nav="links"></div>
 */
(function () {
  'use strict';

  // ── THE INVENTORY ────────────────────────────────────────────────────────
  // Every page the site serves, grouped by what it is for. The redirect
  // stubs (watch.html -> /live/, account.html -> /bee, hivv-page.html ->
  // /hivv-dashboard.html) are NOT listed: their target is listed instead,
  // so the menu never sends you to a page that immediately bounces.
  var GROUPS = [
    {
      title: 'The Hive',
      note: 'the live network',
      items: [
        { href: '/map/',        icon: '🗺️', label: 'Map',        desc: 'every bee, every connection, on one map' },
        { href: '/nodes.html',  icon: '🐝',      label: 'Nodes',      desc: 'the registered bees and their hardware' },
        { href: '/live/',       icon: '📡',      label: 'Live',       desc: 'the hive doing things, this second' },
        { href: '/logs/',       icon: '📜',      label: 'Logs',       desc: 'the traffic, in the open' },
        { href: '/bugs/',       icon: '🐛',      label: 'Bugs',       desc: 'the open issues, and the bridge failures' },
        { href: '/proposals/',  icon: '📝',      label: 'Proposals',  desc: 'what the hive is asked to change, and the verdict on each' },
        { href: '/bridge.html', icon: '🌉',      label: 'Bridge',     desc: 'hive-to-hive, the simple explanation' },
        { href: '/connect.html',icon: '🔗',      label: 'Connect',    desc: 'join your hive to the network' },
        { href: '/party/',      icon: '🎉',      label: 'Party',      desc: 'the offline party — for when the network is not' },
        { href: '/yalla/',      icon: '🐾',      label: 'Yalla',      desc: 'the sighting protocol' },
        { href: '/wish/',       icon: '🌟',      label: 'Wish',       desc: 'the hive keeps no money' },
        { href: '/music.html',  icon: '🎵',      label: 'Music',      desc: 'the music tests' },
      ],
    },
    {
      title: 'Talk & Build',
      note: 'use it, extend it',
      items: [
        { href: '/chat/',              icon: '🗣️', label: 'Chat',        desc: 'talk to the hive' },
        { href: '/cli.html',           icon: '🐝',      label: 'Install CLI', desc: 'one command, and your machine joins' },
        { href: '/aihub.html',         icon: '🧠',      label: 'AI Hub',      desc: 'point the gateway at your own endpoints' },
        { href: '/docs',               icon: '📖',      label: 'API Docs',    desc: 'the OpenAI-compatible surface' },
        { href: '/docs.html',          icon: '📑', label: 'Swagger',     desc: 'the interactive spec' },
        { href: '/hivv-agent-spec.html',icon: '🧐',      label: 'Agent Spec',  desc: 'how to write an agent for this hive' },
        { href: '/account',            icon: '🐝',      label: 'My Bee',      desc: 'your machine, registered' },
        { href: '/dashboard.html',     icon: '👤',      label: 'My Account',  desc: 'keys, quota and usage' },
        { href: '/database.html',      icon: '🗂️', label: 'Database',    desc: 'the public tables, in the open' },
      ],
    },
    {
      title: 'Governance',
      note: 'who may do what, and who says no',
      items: [
        { href: '/hivv-dashboard.html', icon: '⚖️', label: 'Control Center', desc: 'the agent mesh and its provenance' },
        { href: '/command-center.html', icon: '🧭',      label: 'Command Center',desc: 'network intelligence, live' },
        { href: '/panel.html',          icon: '📱',      label: 'Panel',         desc: 'the pocket-sized view' },
        { href: '/control.html',        icon: '🎛️', label: 'Control Panel', desc: 'the older control surface' },
        { href: '/system.html',         icon: '🖥️', label: 'System',        desc: 'the box itself: cpu, gpu, memory' },
        { href: '/purpose.html',        icon: '🎯',      label: 'Purpose',       desc: 'does this help life on Earth?' },
        { href: '/security/',           icon: '🛡️',      label: 'Security',      desc: 'living trust, logical AND spiritual' },
        { href: '/veil/',               icon: '🕶️', label: 'The Veil',      desc: 'the open hand and the closed door' },
        { href: '/statement.html',      icon: '🗒️',      label: 'Statement',     desc: "the night's discoveries, in public" },
        { href: '/admin.html',          icon: '🔐',      label: 'Admin',         desc: 'the operator door' },
      ],
    },
    {
      title: 'The Project',
      note: 'who we are and what we promise',
      items: [
        { href: '/about.html',       icon: '💜',      label: 'About',        desc: 'and how to reach a human' },
        { href: '/team/',            icon: '👥',      label: 'Team',         desc: 'everyone trying to hack the hive' },
        { href: '/origin/',          icon: '🌍',      label: 'Origin',       desc: 'who built HIVV, and why' },
        { href: '/contributors.html',icon: '🙏',      label: 'Contributors', desc: 'the code, counted' },
        { href: '/style/',           icon: '✨}',      label: 'Style',        desc: 'the personality court' },
        { href: '/legacy.html',      icon: '🕯️', label: 'Tonight',      desc: 'what we actually use' },
        { href: '/donate.html',      icon: '🎁',      label: 'Donate',       desc: 'a key, if you want to give one' },
        { href: '/privacy.html',     icon: '🔒',      label: 'Privacy',      desc: 'what is kept, and what is not' },
        { href: '/terms.html',       icon: '📄',      label: 'Terms',        desc: 'the agreement' },
        { href: '/aup.html',         icon: '✍️', label: 'AUP',          desc: 'acceptable use' },
      ],
    },
  ];

  // The primary row — the destinations that earn a permanent place.
  var PRIMARY = [
    { href: '/map/',              icon: '🗺️', label: 'Map' },
    { href: '/chat/',             icon: '🗣️', label: 'Chat' },
    { href: '/hivv-dashboard.html',icon: '⚖️', label: 'Governance' },
    { href: '/status',            icon: '📊',      label: 'Status' },
    { href: '/live/',             icon: '📡',      label: 'Live' },
    { href: '/nodes.html',        icon: '🐝',      label: 'Nodes' },
    { href: '/cli.html',          icon: '⬇️', label: 'Install CLI' },
    { href: '/docs',              icon: '📖',      label: 'API Docs' },
  ];

  // ── CSS (scoped, so it cannot fight a page's own styles) ────────────────
  var CSS = [
    '.hivv-nav{position:sticky;top:0;z-index:9000;display:flex;align-items:center;gap:14px;',
    'padding:10px 18px;background:rgba(6,7,12,.96);backdrop-filter:blur(18px);',
    'border-bottom:1px solid rgba(255,255,255,.09);font-family:inherit;}',
    '.hivv-nav *{box-sizing:border-box;}',
    '.hivv-nav-brand{display:inline-flex;align-items:center;gap:8px;font-weight:800;',
    'color:#e8ecf4;text-decoration:none;font-size:.95rem;letter-spacing:-.01em;white-space:nowrap;}',
    '.hivv-nav-brand:hover{color:#fff;}',
    '.hivv-nav-links{display:flex;align-items:center;gap:2px;flex:1;min-width:0;overflow-x:auto;',
    'scrollbar-width:none;}',
    '.hivv-nav-links::-webkit-scrollbar{display:none;}',
    '.hivv-nav-links>a{display:inline-flex;align-items:center;gap:6px;padding:7px 11px;',
    'border-radius:8px;font-size:.82rem;font-weight:600;color:#9aa4b6;text-decoration:none;',
    'white-space:nowrap;transition:background .18s,color .18s;}',
    '.hivv-nav-links>a:hover{color:#fff;background:rgba(255,255,255,.07);}',
    '.hivv-nav-links>a.is-current{color:#00ff88;background:rgba(0,255,136,.12);',
    'box-shadow:inset 0 0 0 1px rgba(0,255,136,.25);}',
    '.hivv-nav-tail{display:flex;align-items:center;gap:6px;flex:0 0 auto;}',
    '.hivv-nav-pill{display:inline-flex;align-items:center;gap:6px;color:#fef3c7;font-weight:600;',
    'border:1px solid #57534e;border-radius:999px;padding:4px 12px;font-size:.8rem;',
    'background:rgba(254,243,199,.05);text-decoration:none;white-space:nowrap;}',
    '.hivv-nav-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#a3a3a3;}',
    '.hivv-nav-toggle{display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border-radius:8px;',
    'font-size:.8rem;font-weight:700;color:#00f0ff;background:rgba(0,240,255,.10);',
    'border:1px solid rgba(0,240,255,.28);cursor:pointer;white-space:nowrap;font-family:inherit;}',
    '.hivv-nav-toggle:hover,.hivv-nav-toggle[aria-expanded="true"]{background:rgba(0,240,255,.20);}',
    '.hivv-nav-toggle:focus-visible{outline:2px solid #00f0ff;outline-offset:2px;}',
    '.hivv-nav-panel{position:fixed;inset:0;z-index:8999;background:rgba(4,5,10,.97);',
    'backdrop-filter:blur(10px);overflow-y:auto;padding:0 18px 60px;}',
    '.hivv-nav-panel[hidden]{display:none;}',
    '.hivv-nav-panel-head{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;',
    'padding:16px 0 14px;background:linear-gradient(rgba(4,5,10,.98) 78%,rgba(4,5,10,0));}',
    '.hivv-nav-search{flex:1;max-width:520px;padding:11px 14px;border-radius:10px;font-size:.9rem;',
    'background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.14);color:#e8ecf4;',
    'font-family:inherit;}',
    '.hivv-nav-search:focus{outline:none;border-color:#00ff88;}',
    '.hivv-nav-count{color:#7d8798;font-size:.8rem;white-space:nowrap;}',
    '.hivv-nav-close{padding:9px 14px;border-radius:10px;font-size:.85rem;font-weight:700;cursor:pointer;',
    'background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.14);color:#e8ecf4;',
    'font-family:inherit;}',
    '.hivv-nav-close:hover{background:rgba(255,255,255,.12);}',
    '.hivv-nav-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));',
    'gap:26px;max-width:1400px;margin:14px auto 0;}',
    '.hivv-nav-group h3{margin:0 0 4px;font-size:.78rem;letter-spacing:.09em;text-transform:uppercase;',
    'color:#00ff88;}',
    '.hivv-nav-group .hivv-nav-note{margin:0 0 12px;font-size:.74rem;color:#6b7688;}',
    '.hivv-nav-group ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px;}',
    '.hivv-nav-group a{display:flex;gap:10px;align-items:baseline;padding:8px 10px;border-radius:9px;',
    'text-decoration:none;color:#c9d1de;border:1px solid transparent;}',
    '.hivv-nav-group a:hover{background:rgba(255,255,255,.06);border-color:rgba(255,255,255,.12);color:#fff;}',
    '.hivv-nav-group a.is-current{background:rgba(0,255,136,.12);border-color:rgba(0,255,136,.3);color:#fff;}',
    '.hivv-nav-group .n{font-weight:700;font-size:.88rem;white-space:nowrap;}',
    '.hivv-nav-group .d{font-size:.74rem;color:#6b7688;}',
    '.hivv-nav-group a:hover .d{color:#9aa4b6;}',
    '.hivv-nav-empty{color:#6b7688;font-size:.86rem;padding:18px 4px;}',
    '@media (max-width:720px){',
    '.hivv-nav-pill .hivv-nav-word{display:none;}',
    '.hivv-nav-toggle-text{display:none;}',
    '.hivv-nav-toggle{padding:7px 10px;}',
    '.hivv-nav{padding:9px 12px;gap:9px;}',
    '.hivv-nav-brand span:last-child{display:none;}',   // keep the bee, drop the word
    '.hivv-nav-groups{grid-template-columns:1fr;gap:20px;}',
    '}',
  ].join('');

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // The page we are on, normalised so /nodes.html and /nodes both match.
  function currentPath() {
    var p = location.pathname.replace(/\/+$/, '');
    if (!p) p = '/';
    return p;
  }

  function isCurrent(href) {
    var here = currentPath();
    var target = String(href).split('#')[0].split('?')[0].replace(/\/+$/, '') || '/';
    if (target === here) return true;
    // /nodes and /nodes.html are the same page.
    var bare = here.replace(/\.html$/, '') || '/';
    var tBare = target.replace(/\.html$/, '') || '/';
    return bare === tBare;
  }

  function renderPrimaryLinks() {
    return PRIMARY.map(function (l) {
      return '<a href="' + esc(l.href) + '"' + (isCurrent(l.href) ? ' class="is-current"' : '') + '>'
        + '<span aria-hidden="true">' + l.icon + '</span>' + esc(l.label) + '</a>';
    }).join('');
  }

  // The tail is PINNED: on a narrow screen the primary row scrolls
  // sideways, but the live count and the "All pages" button must never
  // scroll out of reach. (They used to: on a phone the whole menu was
  // unreachable.)
  function renderTail() {
    var pill = '<a href="/nodes.html" class="hivv-nav-pill" data-hivv-pill title="bee status loading">'
      + '<span class="hivv-nav-dot" data-hivv-pill-dot></span>'
      + '<span class="hivv-nav-pill-text">🐝 <span data-hivv-pill-count>…</span><span class="hivv-nav-word"> online</span></span></a>';
    var toggle = '<button class="hivv-nav-toggle" type="button" aria-expanded="false" aria-controls="hivv-nav-panel">'
      + '<span aria-hidden="true">☰</span><span class="hivv-nav-toggle-text"> All pages</span></button>';
    return pill + toggle;
  }

  function renderPanel() {
    var groups = GROUPS.map(function (g) {
      var items = g.items.map(function (it) {
        return '<li><a href="' + esc(it.href) + '"' + (isCurrent(it.href) ? ' class="is-current"' : '') + '>'
          + '<span aria-hidden="true">' + it.icon + '</span>'
          + '<span><span class="n">' + esc(it.label) + '</span>'
          + '<br><span class="d">' + esc(it.desc) + '</span></span></a></li>';
      }).join('');
      return '<div class="hivv-nav-group" data-group="' + esc(g.title) + '">'
        + '<h3>' + esc(g.title) + '</h3>'
        + '<p class="hivv-nav-note">' + esc(g.note) + '</p>'
        + '<ul>' + items + '</ul></div>';
    }).join('');
    return '<div class="hivv-nav-panel-head">'
      + '<input class="hivv-nav-search" type="search" placeholder="Filter pages… (map, privacy, yalla, api…)" aria-label="Filter pages">'
      + '<span class="hivv-nav-count" aria-live="polite"></span>'
      + '<button class="hivv-nav-close" type="button">Close ×</button>'
      + '</div><div class="hivv-nav-groups">' + groups + '</div>';
  }

  function mount() {
    if (document.querySelector('.hivv-nav')) return;   // already there

    var host = document.querySelector('[data-hivv-nav]');
    var compact = host && host.getAttribute('data-hivv-nav') === 'links';

    if (!document.getElementById('hivv-nav-style')) {
      var st = document.createElement('style');
      st.id = 'hivv-nav-style';
      st.textContent = CSS;
      document.head.appendChild(st);
    }

    var bar = document.createElement('header');
    bar.className = 'hivv-nav';
    bar.innerHTML = (compact ? '' : '<a class="hivv-nav-brand" href="/"><span aria-hidden="true">🐝</span> <span>HIVV</span></a>')
      + '<nav class="hivv-nav-links" aria-label="Primary">' + renderPrimaryLinks() + '</nav>'
      + '<div class="hivv-nav-tail">' + renderTail() + '</div>';

    var panel = document.createElement('div');
    panel.className = 'hivv-nav-panel';
    panel.id = 'hivv-nav-panel';
    panel.hidden = true;
    panel.innerHTML = renderPanel();

    if (host) {
      host.replaceWith(bar);
    } else if (document.body) {
      document.body.insertBefore(bar, document.body.firstChild);
    } else {
      document.addEventListener('DOMContentLoaded', mount);
      return;
    }
    document.body.appendChild(panel);

    // ── the toggle ────────────────────────────────────────────────────────
    var toggle = bar.querySelector('.hivv-nav-toggle');
    var close = panel.querySelector('.hivv-nav-close');
    var search = panel.querySelector('.hivv-nav-search');
    var count = panel.querySelector('.hivv-nav-count');

    function openPanel() {
      panel.hidden = false;
      toggle.setAttribute('aria-expanded', 'true');
      document.body.style.overflow = 'hidden';
      if (search) setTimeout(function () { search.focus(); }, 30);
    }
    function closePanel() {
      panel.hidden = true;
      toggle.setAttribute('aria-expanded', 'false');
      document.body.style.overflow = '';
    }
    toggle.addEventListener('click', function () {
      if (panel.hidden) openPanel(); else closePanel();
    });
    close.addEventListener('click', closePanel);
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) { closePanel(); toggle.focus(); }
    });
    // A click on the backdrop closes it.
    panel.addEventListener('click', function (e) {
      if (e.target === panel) closePanel();
    });

    // ── the filter ────────────────────────────────────────────────────────
    if (search) {
      var total = GROUPS.reduce(function (n, g) { return n + g.items.length; }, 0);
      function applyFilter() {
        var q = search.value.trim().toLowerCase();
        var shown = 0;
        GROUPS.forEach(function (g) {
          var groupEl = panel.querySelector('[data-group="' + g.title.replace(/"/g, '\\"') + '"]');
          if (!groupEl) return;
          var hits = 0;
          g.items.forEach(function (it) {
            var a = groupEl.querySelector('a[href="' + it.href + '"]');
            if (!a) return;
            var hit = !q || (it.label + ' ' + it.desc + ' ' + it.href).toLowerCase().indexOf(q) !== -1;
            a.style.display = hit ? '' : 'none';
            if (hit) hits++;
          });
          shown += hits;
          groupEl.style.display = hits ? '' : 'none';
        });
        var empty = panel.querySelector('.hivv-nav-empty');
        if (shown === 0) {
          if (!empty) {
            empty = document.createElement('p');
            empty.className = 'hivv-nav-empty';
            empty.textContent = 'No page matches that.';
            panel.querySelector('.hivv-nav-groups').appendChild(empty);
          }
          empty.style.display = '';
        } else if (empty) {
          empty.style.display = 'none';
        }
        if (count) {
          count.textContent = q
            ? (shown + ' of ' + total + ' pages')
            : (total + ' pages');
        }
      }
      search.addEventListener('input', applyFilter);
      applyFilter();
    }

    // ── the live pill ─────────────────────────────────────────────────────
    // The pill carries REAL numbers from /api/hivv/stats. If the shared
    // binding is not on this page, load it.
    var pill = bar.querySelector('[data-hivv-pill]');
    if (pill && !window.HIVVLive) {
      var s = document.createElement('script');
      s.src = '/js/hivv-live.js';
      s.onload = function () { if (window.HIVVLive) window.HIVVLive.start({ intervalMs: 30000 }); };
      document.head.appendChild(s);
    } else if (pill && window.HIVVLive) {
      window.HIVVLive.start({ intervalMs: 30000 });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', mount);
  } else {
    mount();
  }
})();
