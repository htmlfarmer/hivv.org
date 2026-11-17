# CLI README Update Notes — hivv + blnq (2026-09-16)

Both CLIs ship from their own host (NOT xyzo.org):
- **hivv**: `/var/www/vhosts/hivv.org/public_html/hivv-cli.py`  +  `hivv` (bash launcher)  +  `install-hivv.sh`  +  `install-hivv.ps1`  +  `hivv.cmd`
- **blnq**: `/var/www/vhosts/blnq.org/public_html/blnq-cli.py`  +  `blnq` (bash launcher)  +  `install-blnq.sh`  +  `install-blnq.ps1`  +  `blnq.cmd`

Neither host currently has a CLI-specific `README.md` — the existing
`/var/www/vhosts/blnq.org/public_html/README.md` is for the **blnq web app**,
not the CLI. So we need to create **two new README files** (one per host),
plus ship a few cross-referenced files next to them.

These notes describe what each new README should contain.

---

## Both READMEs share these sections

### 1. One-paragraph pitch
- hivv: "HIVV is a friendly, AI-powered CLI for hivv.org. Talk to the AI, see your
  bee's health, and contribute idle CPU/GPU to the network. Stdlib-only Python —
  no pip, no Docker, no cloud account. `curl | bash` to install."
- blnq: "BLNQ runs your scripts in the cloud. Drop a `.py` file, we scan it for
  secrets and dangerous stdlib calls, upload the safe parts to blnq.org, and
  stream the output back to your terminal. Stdlib-only Python — no pip, no
  cloud account. `curl | bash` to install."

### 2. Quick start (3-line install)
```bash
# system-wide (needs sudo)
curl -sSL https://hivv.org/install-hivv.sh | sudo bash

# user-local (no sudo)
curl -sSL https://hivv.org/install-hivv.sh | bash -s -- --prefix=$HOME/.local

# PowerShell (Windows)
iwr https://hivv.org/install-hivv.ps1 -useb | iex
```

(hivv and blnq have parallel `install-*.sh` and `install-*.ps1` files.)

### 3. Architecture (cross-platform, stdlib-only)
- bash installer (Linux/macOS/WSL/MSYS/Cygwin)
- PowerShell installer (Windows native)
- bash launcher `hivv` / `blnq` (auto-downloads the Python client on first run)
- `.cmd` launcher for Windows command-prompt users
- Python client: stdlib only (>=3.7), no pip dependencies
- Self-updating: `hivv update` / `blnq update` (alias: `upgrade`)

### 4. Command reference — operator / local
| command | what it does |
|---|---|
| `start` | spawn the local agent daemon (background) |
| `stop` | stop the agent |
| `restart` | stop + start |
| `status` | agent pid, last heartbeat, last error, hub health |
| `logs [N] [-f]` | tail `~/.local/share/<app>/agent.log` |
| `free` | host mem + swap + disk + per-process RSS |
| `pct [--watch=N]` | live CPU%/MEM% line, refreshes like `top` |
| `info` | install paths, hub URL, hub health |
| `update` / `upgrade` | pull latest `<app>-cli.py` (alias!) |
| `doctor` | one-glance health report — pasteable for support |
| `speed` | latency: DNS, TCP, 3× hub ping, CPU/RAM/disk |
| `test` | doctor + speed + a small remote smoke test |
| `volunteer on/off/status` | opt in/out of running other people's jobs |
| `panic` / `help-bang` | 4-step emergency help |
| `hi` | first-run greeter + examples |
| `thanks` | (hivv only) one-line chat reply |
| `support` | AI diagnosis (hivv) or paste-ready bundle + FAQ (blnq) |
| `uninstall` | stop + remove install (keeps config + logs) |
| `--help` | root help (custom-rendered, see caveat) |

### 5. Command reference — one-shot / hub-talking
| command | what it does |
|---|---|
| `health` | `GET /api/health` — hub reachability |
| `whoami` | (hivv) `GET /api/me`;  (blnq) shows registered worker |
| `metrics [--local]` | `GET /api/stats`; `--local` prints this bee's CPU/GPU/MEM/DISK |
| `nodes` / `workers list` | list the network (hivv nodes / blnq workers) |
| `chat MSG...` | (hivv only) stream a chat reply |
| `completions MSG...` | (hivv only) OpenAI-compat `/v1/chat/completions`, non-stream |
| `submissions list/add` | (blnq only) community free tools |
| `graph [stats|search|timeline]` | (blnq only) tool graph |
| `scripts list/get` | (blnq only) cached community scripts |
| `login` | (blnq only) cookie auth |
| `scan FILE` | (blnq only) local security scan — see below |
| `llm` | local AI runtime helper (detect / suggest / bench / status / install / pull / serve) |

### 6. Smart defaults
- `hivv MESSAGE` → sends to chat (streams reply).
- `blnq FILE` → auto-scans → uploads → runs remotely → streams output.
  - Add `--no-scan` to skip the local scan (server-side filter still runs).
  - Add `--lang=LANG` to override the extension-mapped language.
- Unknown subcommands fall through to the smart-default branch.

### 7. The local agent
- One-line agent daemon: `python3 <app>-cli.py _agent`
- Register on startup, heartbeat every `HEARTBEAT_S` (60 s).
- hivv → `POST /api/nodes/register`, heartbeat `POST /api/nodes/<id>/heartbeat`
  with `{current_load: {cpu, mem, active_jobs}}` and the full metrics snapshot.
- blnq → `POST /api/workers/register`, heartbeat `POST /api/workers/heartbeat`
  with `{worker_id, current_load}` (worker_id IN THE BODY, not URL).
- State persisted to `~/.local/share/<app>/state.json`.
- Config: `~/.config/<app>/config.json` (and `contribute.json` for volunteer).

### 8. Heartbeat metrics payload (added 2026-09-16)
Real metrics — not `current_load: 0` anymore. `_collect_metrics()` returns:
- CPU: `cpu_count`, `load_1`, `load_5`, `load_15` (`os.getloadavg`)
- Memory: `mem_total_mb`, `mem_used_mb`, `mem_avail_mb`, `mem_pct`, swap (`/proc/meminfo`)
- Disk: `disk_total_gb`, `disk_used_gb`, `disk_free_gb`, `disk_pct` (`os.statvfs("/")`)
- GPU: `gpus: [{index, name, mem_total_mb, mem_used_mb, util_pct, temp_c}]` via `nvidia-smi` — empty list if no NVIDIA GPU
- `ts` (epoch) and `uptime_s` (`/proc/uptime`)

See also `metrics --local` — same snapshot, printed locally without talking to the hub.

### 9. Friendly commands — the fun ones
- `hi` greeter
- `panic` / `help-bang` emergency help
- `thanks` (hivv only — blnq doesn't chat)
- `volunteer on/off/status` (persists to `~/.config/<app>/contribute.json`)
- `support` — AI-powered help with auto-attached context (see §11)

### 10. Local security scanner (blnq only — section for the blnq README)
Three passes, stdlib only, no AI, no pip:

**10a. AST pass (Python files only)** — `ast.parse()` walks Call nodes:
- `eval(...)`, `exec(...)` → CRITICAL
- `os.system(...)` → CRITICAL
- `subprocess.{call,run,Popen,...}(..., shell=True)` → CRITICAL
- `pickle.load/loads`, `marshal.load/loads` → CRITICAL
- `yaml.load` (vs `yaml.safe_load`) → CRITICAL
If `ast.parse` raises SyntaxError (non-Python file), the AST pass is skipped silently and the regex pass runs alone.

**10b. Regex pass (all files)** — secrets:
- `\bAKIA[0-9A-Z]{16}\b` — AWS access key ID
- `\bghp_[A-Za-z0-9]{36}\b` — GitHub PAT
- `\bxox[baprs]-[A-Za-z0-9-]{10,}\b` — Slack token
- `-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----` — PEM block
- `\beyJ...\....\....\b` — JWT
- `password|passwd|pwd\s*[:=]\s*['"][^'"\s]{6,}['"]` — generic password literal
- `api[_-]?key|api[_-]?token|secret[_-]?key|access[_-]?token\s*[:=]\s*['"][A-Za-z0-9_\-]{16,}['"]` — generic API key
- `/dev/tcp/` — bash reverse shell
- `\bnc\s+-[a-zA-Z]*e\b` — netcat -e reverse shell

**10c. PII pass (all files)** — accidental personal info (severity split):
- CRITICAL: credit_card (with Luhn), ssn_us, iban, bitcoin_legacy, bitcoin_bech32, eth_address
- WARN: phone_us, phone_intl, ipv4, ipv6, email, dob_labeled

**10d. Wire-up & exit codes:**
- Standalone: `blnq scan FILE [--min-severity info|warn|critical] [--json]`
- Smart-default gate: `blnq FILE` runs scan automatically; CRITICAL → `die(code=3)` before upload
- `--no-scan` bypasses (stripped from anywhere in argv)
- Output → stderr (so it doesn't pollute the script's stdout when upload succeeds)
- Exit codes: 0 clean / 1 warn / 2 critical

**10e. Caveats** — pure regex false positives:
- `password = "x"` regex matches any 6+ char string-literal password, including docstrings
- ETH regex matches any 40-hex string starting with `0x`, including content hashes
- AST pass is exact but Python-only — JS/Bash/etc. rely on regex

**10f. Defense in depth** — blnq.org has its OWN server-side risk filter
on `/api/run` that rejects `import os`, `eval`, network calls etc. even if
`--no-scan` is used. Local scan catches obvious dirty code before it leaves
your box; server filter catches risk patterns that bypass the local scan.

### 11. `support` — the AI / paste-bundle command (BOTH CLIs)
Both CLIs auto-attach context so the user (or AI) has real data to work with:

**Context collected** (same in both):
- `v{VERSION}`, hub URL, hostname, agent pid, node_id/worker_id
- last-heartbeat age (human-friendly)
- bee metrics (CPU cores, mem%, disk%, GPU list with util/temp)
- last 10 lines of the agent log

**hivv `support`** — actual AI chat. Two modes:
- `hivv support` → streams a greeting, summarizes the context, asks what they need
- `hivv support "my question"` → streams an AI diagnosis of that question

The chat uses the system prompt `[System: You are a friendly, expert support agent for the HIVV.org CLI. Be brief, concrete, and accurate. ...]` and streams the SSE response the same way `hivv chat` does.

**blnq `support`** — blnq.org doesn't expose `/api/chat` or `/v1/chat/completions` (both 404). So instead it prints the same context as a paste-ready markdown bundle, then a blnq-specific FAQ:
- why a script got rejected by the server-side filter
- why the agent keeps going offline (and how to read the log)
- how to get human help (github issues + email)
- a nudge to install hivv if they want actual AI chat

### 12. `volunteer` config (`~/.config/<app>/contribute.json`)
```json
{
  "share_cpu": false,
  "max_cpu_pct": 25,
  "pause_on_battery": true,
  "sandbox": "subprocess",
  "opted_in_at": null,
  "tags": []
}
```
- `share_cpu=true` → your bee is eligible to run other people's jobs
- `max_cpu_pct` → cap the CPU time consumed per process
- `pause_on_battery=true` → automatically pause when on battery
- The agent reads this file when deciding whether to claim a job

(Note: agent-side polling of this file is still Phase 2 — currently the file
is written but the agent doesn't yet consult it before claiming. README should
call this out honestly: "your opt-in is recorded; the long-poll for hub-pushed
jobs is the next milestone.")

### 13. Cross-platform install notes
- bash installer detects MSYS/Cygwin/Git Bash and redirects
- PowerShell installer uses `irm | iex` pattern (compatible with PS 5+)
- `.cmd` launcher for plain Windows command prompt
- All paths use `Path(__file__).resolve().parent` so wrappers work from any location
- The launcher derives install dir from its own location: `~/.local/bin/hivv` → `~/.local/lib/hivv/hivv-cli.py`

### 14. Cross-platform firewall posture
- **All outbound.** No inbound ports opened.
- hivv.org and blnq.org are the only hosts the CLI talks to.
- No P2P, no listening socket, no daemon-mode server. Agent is pure HTTP client.
- Works behind corporate firewalls that allow outbound HTTPS.

### 15. Reciprocity / network contribution
Two ways a hive of bees strengthens the network:
- **`volunteer on`** — your bee runs other people's jobs. CPU/GPU cap is honored.
- **Network visibility** — every heartbeat shows you as `online` to the hub
  dashboard. Hubs use this for capacity planning; bees show up on the public
  node/worker list (`hivv nodes`, `blnq workers list`).

The long-poll channel for hub-INITIATED messages (so the hub can push work
to your bee without you polling) is the next milestone — README should say
"in development" honestly.

### 16. Self-update mechanics (`update` / `upgrade`)
- `hivv update` (alias `hivv upgrade`) downloads `https://hivv.org/hivv-cli.py`
- Atomic write: `tempfile.NamedTemporaryFile` → `os.chmod(0o644)` BEFORE `shutil.move`
- Setting the **User-Agent** header explicitly is REQUIRED — blnq.org returns
  403 to `Python-urllib/x.y` (the default UA)
- Restart the agent automatically if it was running

### 17. Caveats / build-time gotchas (mention these!)

1. **`print_help()` crashes** with argparse + nested subparsers + REMAINDER nargs
   (Python 3.11, deep recursion through `_format_action`). Both CLIs render
   help manually via `_print_root_help()` / `_print_sub_help()` — DO NOT call
   `parser.print_help()` if you fork these.
2. **Don't name a function `http(...)`** — it shadows the stdlib `http` package
   and breaks `http.cookiejar`. blnq imports `http.cookiejar as cookiejar` AND
   renames its helper to `do_http(...)`. hivv doesn't import http.* at all, but
   the same rule applies.
3. **hivv's node_id** is derived server-side from `body.id || body.token` —
   send a stable per-host id or every heartbeat will 404.
4. **blnq's heartbeat** requires `worker_id` IN THE BODY (not URL path) —
   different schema from hivv despite both being "node heartbeat" endpoints.
5. **The chmod + User-Agent gotcha in `cmd_update`** — chmod the TMP file
   before `shutil.move` (since `move` preserves source mode), use `0o644`
   not `0o755` (Python doesn't need +x when imported by a web server), and
   set `User-Agent` explicitly (blnq.org 403s the default Python-urllib UA).
6. **`upgrade` is an alias** for `update` — without it, `hivv upgrade` falls
   into the smart-default chat and sends the literal word "upgrade" to the LLM.
   Aliased in `main()` BEFORE dispatch.
7. **Phone regex needs NO leading `\b`** — `\b` fails between `"` and `(`
   (both non-word). The fix is to drop the leading `\b` on phone patterns so
   `"(415) 555-2671"` inside a string literal matches.

### 18. Trust / safety
- **Local scan + server-side filter = defense in depth.** No code reaches
  blnq.org until both layers pass (or you explicitly `--no-scan` past the
  local one — server still runs its filter).
- **No telemetry beyond what you see.** Heartbeat carries CPU/GPU/MEM/DISK
  and pid, not file contents, not browsing data, not keystrokes.
- **Cookie file** lives at `~/.config/blnq/cookies.txt` with mode `0600`.
- **Agent log** at `~/.local/share/<app>/agent.log` — tail it with
  `<app> logs 100`.

### 19. Uninstall
```bash
hivv uninstall      # stops agent + removes CLI; keeps config + logs
# or
curl -sSL https://hivv.org/install-hivv.sh | bash -s -- --uninstall
```

### 20. Support links
- hivv: github.com/hivv/issues  ·  support@hivv.org
- blnq: github.com/blnq/issues  ·  support@blnq.org

---

## Files to write

1. `/var/www/vhosts/hivv.org/public_html/README.md` — full readme using §1–§20 above, hivv examples
2. `/var/www/vhosts/blnq.org/public_html/README.md` — full readme using §1–§20 above, blnq examples, INCLUDING §10 (scanner section)
3. `/var/www/vhosts/hivv.org/public_html/CHANGELOG.md` — bullet-point recap of 2026-09-16 changes (added `support`, `volunteer`, etc.)
4. `/var/www/vhosts/blnq.org/public_html/CHANGELOG.md` — same + scanner added, PII pass added
5. Optionally: `docs/security.md` in blnq for the scanner details (currently lives in the README notes file)

---

## What's still missing from the CLIs (not in scope for the README, but worth noting)

These are real features we built into the code but don't yet work end-to-end:

- **Hub-pushed messages / long-poll.** The `volunteer` config is written; the
  agent doesn't yet poll `/api/agents/inbox` or equivalent for hub-initiated
  jobs. README §15 should call this "in development."
- **PII regex false positives.** `password = "x"` matches docstring text.
  Mitigation: server-side filter still runs.
- **Windows ARM and Apple Silicon benchmarks** are tuned for x86_64. The
  cross-platform install path works, but the `llm suggest` table doesn't yet
  include ARM-tuned models.
- **blnq `chat` endpoint.** Would be a nice complement to `support` but blnq
  currently has no `/api/chat` route. README should NOT mention it.

---

## Order of operations

1. Write the two README files using the sections above
2. Write the two CHANGELOG files (brief — just the 2026-09-16 changes)
3. Cross-link from each project's hub landing page (hivv.html, blnq.html) to the new README
4. Sanity-check the example commands in each README actually work
   (run each one before committing the docs)

---

*Last updated: 2026-09-16*
