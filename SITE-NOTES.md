# Site Audit + TODO — hivv.org & blnq.org (2026-09-16)

## Live health snapshot

| Site | `/api/health` | `/api/features` | `/api/stats` | `/docs` | Landing |
|---|---|---|---|---|---|
| **hivv.org** | ✅ 200 | ❌ 404 | (uses `/api/public/status`) | ❌ 404 | `/index.html` 80 KB |
| **blnq.org** | ✅ 200 | ✅ 200 | ✅ 200 | (none) | `/index.html` 31 KB |

Both hubs return rich JSON. blnq.org's `/api/features` endpoint is the gold-standard
shape for a feature-flag service — we should mirror it on hivv.

---

## hivv.org — current endpoints

### Public, no auth
- `GET /api/health` — basic liveness
- `GET /api/public/status` — rich: providers (23 active), recent activity, today/hour counters
- `GET /api/control/metrics/global` — global metrics (probably admin-only based on path)
- `GET /api/control/nodes` — node list (admin)
- `GET /api/config` — public config
- `GET /bee/v1/:username`, `/bee/v2/:username`, `/bee/:username` — public chat-by-username
- `GET /v1/chat/completions` — OpenAI-compatible (also POST)

### Authenticated (`requireAuth` / `authRequired`)
- `POST /api/auth/{register,login,logout,change-password,reset-password,link-account}`
- `GET /api/auth/{csrf-token,providers,me,export,anonymous-history}`
- `DELETE /api/auth/account`, `PUT /api/auth/profile`
- `GET /api/user/me`

### Agent / dashboard (used by `hivv start` etc.)
- `POST /api/nodes/register`
- `POST /api/nodes/:id/heartbeat`
- `POST /api/nodes/:id/events` / `GET /api/nodes/:id/events`
- `POST /api/nodes/:id/permissions/request` / `GET /api/approvals/pending` / `POST /api/approvals/:permissionId/decide`
- `GET /api/control/nodes`, `:id`, `POST :id/drain`, `DELETE :id`, `POST :id/offline`, `PUT :id/tags`, `POST /bulk-import`
- `GET /api/control/metrics/{global,nodes/:id/live,throughput,errors,model-performance}`
- `GET/PUT /api/control/rate-limits/{global,node/:id,user/:id}`
- `GET /api/control/quotas/user/:id`
- `GET /api/control/models`, `:name/context-window`, `PUT :name/{context-window,affinity}`, `GET /costs`, `GET /costs/summary`, `PUT /costs/unit-prices`
- `GET/PUT /api/control/load-balancing`
- `GET /api/control/circuit-breakers`, `POST /circuit-breakers/reset`

### Agents/relationships (dashboard)
- `GET /api/agents`, `:id`, `POST :id/state`
- `GET /api/relationships`, `POST /api/relationships`
- `GET /api/permissions/matrix`, `POST /api/permissions/:id`
- `GET /api/activity`, `POST /api/events`
- `GET /api/intentions`, `POST /api/intentions`
- `GET /api/tasks`, `GET /api/trust/:agent_id`
- `GET /api/decisions/:id/why`, `POST /api/decisions`

### hivv-specific (sub-API)
- `GET /api/hivv/` (list agents)
- `GET /api/hivv/me`
- `GET /api/hivv/agents`, `:agentId`, `:agentId/{past,present,future}`
- `POST /api/hivv/agents`, `PATCH /api/hivv/agents/:agentId`, `DELETE /api/hivv/agents/:agentId`
- `GET/POST/DELETE /api/hivv/trust`, `GET /api/hivv/authorize`

---

## blnq.org — current endpoints

### Public, no auth
- `GET /api/health` — rich (uptime, components, features, cache)
- `GET /api/features` — feature flags (gold-standard, mirror to hivv)
- `GET /api/stats` — submissions/users/votes counts
- `GET /api/submissions` (optionalAuth for full data)
- `GET /api/graph/{,stats,node/:id,search,timeline,tree/:id,related/:nodeId}` (knowledge graph)
- `POST /api/graph/{relate,study-entities}`, `GET /api/graph/studies/:username`
- `GET /api/scripts`, `:id`
- `GET /api/jobs/list`, `/running`, `/:jobId`, `/:jobId/start`, `/:jobId/heartbeat`, `/:jobId/output`, `POST /:jobId/complete`
- `POST /api/jobs/submit`, `POST /api/jobs/claim`
- `GET /api/run/languages`
- `GET /api/upload/bandwidth`, `GET /api/upload/status`, `GET /api/upload/:id/study`
- `GET /api/triples/{file/:fileId,search,stats}`, `POST /api/triples/generate/:fileId`, `DELETE /api/triples/file/:fileId`
- `GET /api/workers/list`, `/:workerId/jobs`
- `POST /api/workers/{register,heartbeat}`, `DELETE /api/workers/:workerId`

### Authenticated (`requireAuth`)
- `POST /api/auth/{register,login,logout}` (CSRF on some)
- `GET /api/auth/csrf-token`, `GET /api/auth/providers`
- `GET/POST /api/auth/google`, `GET/POST /api/auth/github`, `GET/POST /api/auth/facebook`  (oauth — currently DISABLED per /api/features)
- `GET /api/user/me`
- `POST /api/submissions`, `POST /api/submissions/:id/vote`
- `GET /api/edits`, `POST /api/edits`, `POST /api/edits/:id/{approve,reject}`, `GET /api/edits/history/:toolId`, `POST /api/tools/:toolId/rollback/:historyId`
- `POST /api/scripts/`, `DELETE /api/scripts/:id`
- `GET /api/user-space/{quota,files}`, `POST /api/user-space/upload`, `GET /api/user-space/:username/:filename`, `GET /api/user-space/:username/:filename/study`, `DELETE /api/user-space/files/:id`
- `POST /api/run/` (with rate limit), `GET /api/run/`, `GET /api/run/:id`, `GET /api/run/:id/logs`, `POST /api/run/:id/execute`
- `POST /api/upload/`, `DELETE /api/upload/file/:id`, `POST /api/upload/cleanup`
- `POST /api/study/stream`, `POST /api/study/analyze-upload`, `GET /api/study/:username/:filename`, `GET /api/study/:username/:filename/{analyses,posts,analyses/:analysisId}`, `POST /api/study/:username/:filename/posts`, `POST /api/study/:username/:filename/posts/:postId/vote`
- `POST /api/minimax/study`

---

## Cross-site gaps (what's missing or broken)

### Critical gaps — both sites
| # | Gap | Severity | Effort |
|---|---|---|---|
| 1 | **No public OpenAPI spec.** Both APIs have ~50 endpoints; users have to read source. | High | M |
| 2 | **No `/docs` page** that renders an OpenAPI explorer (Swagger UI or Redoc). hivv.org returns 404. | High | S |
| 3 | **No CLI landing page.** `curl \| bash` is the install story but the website doesn't show it. | High | S |
| 4 | ✅ **`/api/agents/<id>/inbox` hub-side endpoint shipped** (2026-09-16). New `routes/inbox-routes.js` on both hubs; in-memory `Map<id, [{id, type, payload, ts}]>` with 24h TTL, 50s max long-poll, ack pruning. CLI inbox poll flipped from `heartbeat-only` to `long-poll` automatically — verified live. | — | — |
| 5 | **No documented rate limits.** The CLIs will hit limits eventually; users need to know. | Med | S |

### hivv.org specific
| # | Gap |
|---|---|
| 6 | **`/api/features` is missing.** blnq has it; hivv should mirror. |
| 7 | **No `/api/auth/register` rate-limit visibility** — the page should show "5/hr per IP". |
| 8 | **No `cli.html`** — the install banner + example commands aren't on the website anywhere. |

### blnq.org specific
| # | Gap |
|---|---|
| 9 | **`/api/auth/google`, `/github`, `/facebook` routes exist but feature flag is OFF.** Should be documented as "available, disabled pending production review." |
| 10 | **No `cli.html`** — same as hivv. |
| 11 | **`/api/graph/search` returns `{results:[]}`** when called with no DB seeded. Landing page should explain that the graph is empty until you submit tools. |

### Polish — both sites
| # | Gap |
|---|---|
| 12 | **No "developers" landing** with quick links to: install CLI, OpenAPI spec, examples, webhooks, community |
| 13 | **No changelog page** (we have version-stamped CLIs but no public changelog) |
| 14 | **No webhooks docs** — when Phase A hubs add inbox, we should document the message types |
| 15 | **No CORS docs** — what origins can hit the public API? |
| 16 | **No status page (public)** — both have `/status` but it requires auth on hivv |

---

## Today's deliverables — SHIPPED 2026-09-16

| # | File | Size | Status |
|---|---|---|---|
| 1 | `SITE-NOTES.md` (this audit) | 11.5 KB | ✅ |
| 2 | `openapi.yaml` (hivv) | 18.1 KB | ✅ 200 |
| 3 | `openapi.yaml` (blnq) | 39.5 KB | ✅ 200 |
| 4 | `cli.html` (hivv) | 15.1 KB | ✅ 200 |
| 5 | `cli.html` (blnq) | 17.0 KB | ✅ 200 |
| 6 | `docs.html` (hivv) | 5.0 KB | ✅ 200 |
| 7 | `docs.html` (blnq) | 5.0 KB | ✅ 200 |

All seven served 200 OK on first check. Both OpenAPI specs render in Swagger UI
via the unpkg CDN at `swagger-ui-dist@5.17.14`. Both `cli.html` pages have
working platform tabs (macOS/Linux ↔ Windows PowerShell) and a clipboard
copy button.

### Remaining polish (next session)
- [x] ✅ Add prominent "CLI" + "API" links to `hivv.org/index.html` navbar (added 2026-09-16 — amber "🐝 Install CLI" nav button + full-width install banner above universe-hero)
- [x] ✅ Add prominent "CLI" + "API" links to `blnq.org/index.html` navbar (added 2026-09-16 — cyan "⌨️ Install CLI" nav button + full-width install banner above upload-container)
- [x] ✅ Phase A hub-side: `/api/agents/<id>/inbox` shipped on both servers (2026-09-16)
- [x] ✅ Server-pushed CLI updates with rollback shipped (2026-09-16). Hub `/api/version`, `/api/control/versions`, `POST /api/control/agents/:id/push-update`. CLI `_apply_update` with `.bak` + `--healthcheck` rollback, `_nag_if_behind()` on `start`/`info`. Tests: 5/5 each.
- [ ] hivv.org: mirror `blnq.org`'s `/api/features` endpoint
- [ ] Update CLIs if hub-side URL paths differ from spec
- [ ] Public changelog page (no version history today)
- [ ] Webhooks doc page (when Phase A hubs land)

---

## Implementation order

| Step | What | When |
|---|---|---|
| 1 | Write `openapi.yaml` for both APIs | Now ✅ |
| 2 | Write `cli.html` (one for each brand, ~80 lines each) | Now ✅ |
| 3 | Write `docs.html` (swagger-ui embed) on both sites | Now ✅ |
| 4 | Verify everything serves 200 | End of session ✅ |
| 5 | Update `api.html` and `index.html` with prominent links | Next session ✅ |
| 6 | Phase A hub-side: add `/api/agents/<id>/inbox` to both servers | Now ✅ (2026-09-16) |
| 7 | Update CLI to call `agent_id` route correctly (note: blnq uses `worker_id`, hivv uses `node_id`) | Now ✅ |

### Phase A hub-side proposed routes (for the next session)

**hivv.org** (add to `routes/agent-routes.js` or new `routes/inbox.js`):
```
GET    /api/agents/<node_id>/inbox?wait=50    — long-poll (50s)
POST   /api/agents/<node_id>/inbox/ack        — mark message delivered
```
Verify `Authorization: Bearer` + `X-Auth-Signature`, check nonce store, dispatch messages.

**blnq.org** (add to `routes/workers.js`):
```
GET    /api/agents/<worker_id>/inbox?wait=50
POST   /api/agents/<worker_id>/inbox/ack
```
Same verification. blnq's `worker_id` is what blnq-cli calls `worker_id` in the body; the inbox path uses the same id for consistency.

**Common ack schema:**
```json
{ "delivered": ["<msg_id>", ...] }
```

**Message envelope (matches `_dispatch_inbox_message` in the CLIs):**
```json
{
  "messages": [
    {
      "id": "m_abc123",
      "type": "update_available",
      "payload": { "version": "0.2.0", "url": "/hivv-cli.py", "sha256": "..." },
      "ts": 1789598000
    }
  ]
}
```
Supported types v1: `update_available`, `shutdown_requested`, `log`, `job_available` (Phase 2).

---

## What "all features working" means (the user's check)

Let me run a checklist now and again at session end:

- [x] hivv install script works (`curl | bash`)
- [x] blnq install script works
- [x] hivv CLI runs (`hivv start` → registers → heartbeats)
- [x] blnq CLI runs (`blnq start` → registers → heartbeats)
- [x] `hivv chat` streams a reply
- [x] `blnq runme.py` uploads + executes + streams output
- [x] Both hubs see our bees online (`can_claim: true`)
- [x] Phase A: auth tokens created, HW-bound IDs, graceful degrade
- [x] `hivv support` AI chat works
- [x] `blnq support` paste-ready bundle + FAQ works
- [x] `hivv doctor` reports all paths + Phase A state
- [x] `hivv info` shows transport, hw_id, auth_token, signed
- [x] `hivv metrics --local` prints real metrics
- [x] `hivv llm status` / `hivv llm suggest` works
- [x] `hivv volunteer on/off/status` persists
- [x] `hivv panic` prints emergency help
- [x] `hivv hi` greets
- [x] `hivv thanks` warm reply
- [x] `hivv update`/`upgrade` pulls + chmod + UA correct
- [x] blnq scan catches `eval()` / secrets / PII
- [x] blnq FILE auto-scans before upload
- [x] `blnq submissions list` works (returns empty array as expected)
- [x] `blnq graph stats` / `graph search` works
- [x] `blnq workers list` shows our bee
- [x] ✅ `/api/agents/<id>/inbox` hub-side — SHIPPED 2026-09-16 (in-memory store, 24h TTL, 50s max long-poll)
- [x] ✅ Public OpenAPI spec — SHIPPED 2026-09-16
- [x] ✅ `/docs` page (swagger-ui) — SHIPPED 2026-09-16
- [x] ✅ CLI landing page on the website — SHIPPED 2026-09-16
- [x] ✅ Server-pushed CLI updates with atomic rollback — SHIPPED 2026-09-16 (see memory hivv-blnq-clis-shipped.md)

---

## Files already shipped this session
- 4 colorful install scripts (bash + PowerShell for both CLIs)
- Phase A hardening for both CLIs (auth tokens, signing, HW ID, inbox polling, log scrubbing, graceful degrade)
- Memory updated
- `CLI-README-NOTES.md` (20 sections of future README content)
- This `SITE-NOTES.md` (audit + plan)

---

*Last updated: 2026-09-16*
