---
name: hivv
description: Use when the user wants cheap, private, or fast AI via the hivv.org hive, wants hive status (nodes, health, honey, stats), or asks about hivv or its CLI — also covers response receipts and wiring hivv into OpenCode as a model provider.
---

# hivv — distributed AI gateway

## What hivv is

hivv.org is a distributed AI gateway: a hive of machines sharing LLM capacity, with a hub that
routes each request to the best answer available — local llama.cpp/ollama, another bee (direct
or NAT-relayed), or a cloud key as a last resort. Bees earn honey and ranks (larva → queen-bee)
for the compute they contribute, tracked with `hivv honey`.

## When to use it

- The user wants **cheap / private / fast AI**, or asks who would answer a question and why.
- The user wants **hive status** — online nodes, cluster stats, their honey, rank, or streak.
- The user mentions **hivv**, the `hivv` CLI, or wants hivv wired into OpenCode.

## Core commands

Install the CLI first if it's missing:

```bash
curl -fsSL https://hivv.org/install-hivv.sh | sh
```

| Command | What it does |
|---|---|
| `hivv chat "msg" --mode=smart\|fast\|cheap\|private` | one-shot chat; mode picks the routing strategy |
| `hivv route "msg"` | dry-run — who *would* answer & why, never executes |
| `hivv nodes` | table of every registered bee |
| `hivv reach <name>` | test connectivity to one bee |
| `hivv ask <name> "msg"` | chat with ONE bee — pins it (direct → hub relay); aliases `talk`, `node-chat` |
| `hivv health` | hub health check |
| `hivv up` | quick 3-line status ("you good?") |
| `hivv test` | full end-to-end smoke test (doctor + speed + network) |
| `hivv honey --board` | your honey, rank, streak + the leaderboard |
| `hivv stats` | per-node stats + cluster totals |
| `hivv llm status` | detect/bench/suggest local LLM runtimes on this box |
| `hivv setup` | print ready-to-paste OpenCode / VSCode / local-LLM config |

Notes:

- Unknown subcommands fall through to chat — `hivv explain kubernetes` just works.
- `smart` is an alias for `quality`, `cheap` for `cost`; `hivv route --mode=…` previews the
  same modes without sending anything.

## Receipts

Every answer says which bee served it:

- `X-Hivv-Answered-By: <node>/<model> (direct|relay)` — the bee and the transport it used.
- `X-Hivv-Route: tier=… p_wise=… bucket=…` — what the route brain decided, and why.

The CLI prints the receipt under each answer (`— answered by …`); in OpenCode they show up in
the HTTP logs. Set `HIVV_RECEIPTS=off` to turn them off.

## Using hivv as an OpenCode provider

Canonical snippet (mirrored at `https://hivv.org/opencode-hivv.jsonc`):

```jsonc
// Merge into ~/.config/opencode/opencode.jsonc (global) or a project opencode.jsonc.
// HIVV_API_KEY optional — leave unset for anonymous tier. `hivv setup` generates this for you.
// P2P: `hivv setup --write` starts the local dial shim and repoints this at http://127.0.0.1:3911/v1 (direct-to-bee; hivv.org only for route dials).
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "hivv": {
      "name": "HIVV",
      "env": ["HIVV_API_KEY"],
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "https://hivv.org/v1" },
      "models": {
        "auto":    { "name": "HIVV Auto", "capabilities": { "tools": true } },
        "smart":   { "modelID": "auto", "name": "HIVV Smart", "body": { "route": { "mode": "quality" } }, "capabilities": { "tools": true } },
        "fast":    { "modelID": "auto", "name": "HIVV Fast", "body": { "route": { "mode": "fast" } }, "capabilities": { "tools": true } },
        "cheap":   { "modelID": "auto", "name": "HIVV Cheap", "body": { "route": { "mode": "cost" } }, "capabilities": { "tools": true } },
        "private": { "modelID": "auto", "name": "HIVV Private", "body": { "route": { "mode": "private" } }, "capabilities": { "tools": true } }
      }
    }
  },
  "model": "hivv/auto"
}
```

- Provider id `hivv`; aliases `auto`, `smart`, `fast`, `cheap`, `private` each stamp their
  route mode into the request body — no protocol change.
- `HIVV_API_KEY` is optional: leave it unset for the anonymous tier (rate-limited).
- Discover this skill from any OpenCode config with
  `skills: ["https://hivv.org/.well-known/skills/"]`.

## Gotchas

- **Relay answers are buffered** — a NAT'd volunteer bee returns the whole reply at once, so
  no token streaming; expect a pause, then the full answer in one chunk.
- **`--mode=private` keeps data in the hive** — localhost/direct/relay only, never a cloud key.
- **MAYBE policy flows** — gated capabilities come back asking for a grant; answer with
  `hivv approve <grant_id>` (`--list` shows pending, `--all` approves everything) or
  `hivv deny <grant_id>`.
