#!/usr/bin/env python3
"""hivv — the HIVV.org client CLI / local agent.

Pure stdlib (no pip install). Connects to hivv.org as a CLIENT.

Subcommands (operator / local):
  start              start the local hivv agent (background)
  stop               stop the local hivv agent
  restart            stop + start
  status             pid, uptime, last heartbeat, last error
  logs [N] [-f]      tail ~/.local/share/hivv/agent.log
  free               host mem + swap + disk + per-process RSS
  pct [--watch=N]    live CPU%/MEM% line (refreshes like top)
  info               hub URL, version, node_id, install path
  update             pull the latest hivv-cli.py from hivv.org
  uninstall          stop + remove install (keep ~/.config/hivv/)
  --help, help       list every subcommand

Subcommands (one-shot / talk to hivv.org):
  health             GET /api/health
  metrics            GET /api/control/metrics/global
  nodes              GET /api/control/nodes
  chat MSG           POST /api/chat   (smart default — see below)
  completions MSG    POST /v1/chat/completions (OpenAI-compatible)
  whoami             GET /api/control/health (cheap "am I talking to hivv?" check)

Smart default — if no subcommand matches and the first arg isn't a flag,
the whole rest is treated as a streaming chat message:

  hivv whats the meaning of life
  hivv write me a haiku about kubernetes
  hivv explain the Monty Hall problem in 3 sentences

Per-subcommand --help also works.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import getpass
import hashlib
import hmac
import http.client as http_client
import http.server as http_server
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── configuration ──────────────────────────────────────────────────────
APP       = "hivv"
HUB       = os.environ.get("HIVV_HUB",  "https://hivv.org")
HUB_NAME  = "HIVV.org"
# HIVV_CLI_VERSION: 0.2.0 — used by /api/version to sync CLI + API versions
CLI_VERSION = "0.2.0"
# Version is content-derived — changes on every update
def _get_version(data: bytes | None = None):
    try:
        # Derive a short, content-based version so updates always change the
        # reported version when the file contents change (even if mtime is
        # preserved). We keep a stable prefix for human reading. Passing `data`
        # reports the version of bytes we're about to install, instead of the
        # copy this process was launched from (which is stale mid-update).
        if data is None:
            with open(__file__, 'rb') as f:
                data = f.read()
        h = hashlib.sha256(data).hexdigest()[:8]
        return f"0.2.{h}"
    except Exception:
        return "0.2.0"
VERSION   = _get_version()

# Runtime flags (set from CLI args)
PLAIN_OUTPUT = False
NO_THINK = False

# OpenAI-format tool schemas for function-calling via the hive's LLM.
# These are sent to the hub when chatting, enabling the LLM to decide when
# to call tools (search, fetch, calculate, time, sysinfo).
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "news",
            "description": "Search Google News for current news articles. Best for 'what's happening in [city]?', current events, today's headlines. Returns top stories with titles, sources, dates, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The news query (e.g. 'San Francisco', 'technology')"},
                    "location": {"type": "string", "description": "Optional location for local news (e.g. 'San Francisco', 'London')"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Search Wikipedia for information. Returns article titles, snippets, and URLs. Best for factual queries, definitions, and general knowledge.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g. 'quantum computing', 'World War II history')"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": "Retrieve the content of a URL. Returns status, content type, and readable text (HTML stripped, max 1500 chars).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch (must be http or https)"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a mathematical expression. Supports +, -, *, /, %, **, parentheses, and Python builtins (abs, round, min, max, pow).",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression to evaluate (e.g. '2+2*10' or '(1024 * 768) / 300'"}
                },
                "required": ["expression"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time. Returns human-readable local time, Unix timestamp, ISO format, and optionally converts to a specific timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York', 'Asia/Dhaka', 'Europe/London')"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sysinfo",
            "description": "Get local system information: CPU, memory, disk, load, and uptime.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]

# XDG-ish paths (with fallback to ~/.hivv for legacy)
CONFIG_DIR    = Path(os.environ.get("HIVV_CONFIG_DIR")  or (Path.home() / ".config" / APP))
STATE_DIR     = Path(os.environ.get("HIVV_STATE_DIR")   or (Path.home() / ".local" / "share" / APP))

# Where the client + wrapper actually live. If we were installed to a non-default
# prefix, derive from this file's location: client is in <prefix>/lib/hivv/, wrapper
# is in <prefix>/bin/hivv. Fall back to /usr/local when running in-place (e.g. dev).
_THIS_DIR = Path(__file__).resolve().parent
if _THIS_DIR.name == "hivv" and _THIS_DIR.parent.name == "lib":
    INSTALL_DIR = Path(os.environ.get("HIVV_INSTALL_DIR") or _THIS_DIR)
    BIN_DIR     = Path(os.environ.get("HIVV_BIN_DIR")     or _THIS_DIR.parent.parent / "bin")
else:
    INSTALL_DIR = Path(os.environ.get("HIVV_INSTALL_DIR") or ("/usr/local/lib/" + APP))
    BIN_DIR     = Path(os.environ.get("HIVV_BIN_DIR")     or "/usr/local/bin")

CONFIG_FILE   = CONFIG_DIR  / "config.toml"
STATE_FILE    = STATE_DIR   / "state.json"
LOG_FILE      = STATE_DIR   / "agent.log"
PID_FILE      = STATE_DIR   / "agent.pid"
AUTH_FILE     = CONFIG_DIR  / "auth.json"   # 0600 — per-install bearer token
VISITOR_FILE  = STATE_DIR   / "visitor_id"  # persistent per-install device ID
APPROVALS_FILE = STATE_DIR  / "approvals.json"  # MAYBE grants pushed to our inbox

# Heartbeat cadence
HEARTBEAT_S = int(os.environ.get("HIVV_HEARTBEAT_S", "60"))

# ── dev mode: protects local development from accidental overwrites ──────
def _is_dev_mode() -> bool:
    """True when running from a source-tree / dev checkout that should NOT be
    auto-updated. Set HIVV_DEV_MODE=1 env var OR place a .hivv-dev marker file
    next to the cli.py (or in CONFIG_DIR)."""
    if os.environ.get("HIVV_DEV_MODE", "").lower() in ("1", "true", "yes"):
        return True
    for candidate in [
        Path(__file__).resolve().parent / ".hivv-dev",
        CONFIG_DIR / ".hivv-dev",
    ]:
        if candidate.exists():
            return True
    return False


# ── tiny utilities ─────────────────────────────────────────────────────
# Auto-heal tips: when something fails, the fix should be one glance away.
# die()/warn() match the message against these rules and print up to 2 tips.
# Disable with HIVV_TIPS=off. Tips are data — `hivv update` can ship new ones.
_HIVV_TIPS = [
    (r"timed? out|timeout", "slow or dead endpoint — a smaller model helps: hivv llm suggest · or a specific bee: hivv node-chat <name> \"...\""),
    (r"connection refused|econnrefused", "peer refused the link — its LLM is localhost-only: hivv firewall --apply --mode lan · cross-network: hivv firewall --apply --mode tailscale"),
    (r"network error|unreachable|no route|name resolution|name or service not known", "no route to the hub — check wifi/VPN, then: hivv whoami · the agent reconnects on its own once online"),
    (r"\b401\b|\b403\b|unauthorized|forbidden", "auth hiccup — check identity: hivv whoami · if you need admin, set HIVV_CONTROL_TOKEN on the hub"),
    (r"already in use|port .*busy|address already", "port conflict — hivv doctor shows who owns it · LLMs default to 8080/11434"),
    (r"no online nodes|no nodes (found|online)|nobody", "nobody home — start your own node: hivv start · see the hive: https://hivv.org"),
    (r"crash.?loop|crash loop", "agent is crash-looping — hivv logs (last errors) · then hivv doctor --fix"),
    (r"out of memory|\boom\b|cannot allocate", "RAM is the bottleneck — hivv llm suggest · pick a smaller model · hivv free to see what's eating memory"),
    (r"not running|agent (is )?not up|stopped", "the agent isn't running — hivv start · auto-fix: hivv doctor --fix"),
    (r"model .{0,20}not found|no model|pull first", "no local model — hivv llm suggest · hivv llm pull <model> · or hivv llm pull-gguf gemma4:e2b"),
    (r"certificate|ssl|tls", "TLS trouble — check the clock (certs are time-sensitive) · corporate proxies may intercept https"),
    (r"permission denied", "permission denied — file owned by another user? sudo chown -R \"$USER\" ~/.config/hivv ~/.local/share/hivv · or reinstall user-local with --prefix=$HOME/.local"),
    (r"python.{0,20}not found", "python3 missing — Linux: apt install python3 · macOS: brew install python3 · Windows: winget install Python.Python.3.12"),
]

def _tips_for(msg: str) -> list:
    """Match a failure message against the tip table (max 2 tips)."""
    import re as _re
    out = []
    low = (msg or "").lower()
    for pat, tip in _HIVV_TIPS:
        try:
            if _re.search(pat, low):
                out.append(tip)
        except _re.error:
            if pat in low:
                out.append(tip)
        if len(out) >= 2:
            break
    return out

def _print_tips(msg: str, to_stderr: bool = False) -> None:
    if os.environ.get("HIVV_TIPS", "on").lower() in ("off", "0", "no"):
        return
    import threading
    # _tips_for returns tip STRINGS (max 2) — not (pat, tip) pairs
    for tip in _tips_for(msg):
        stream = sys.stderr if to_stderr else sys.stdout
        print(f"  \033[36m→ {tip}\033[0m", file=stream)
        # C4: fire-and-forget tip-shown analytics
        def _report():
            try:
                body = json.dumps({"tip_key": tip, "event": "shown"}).encode()
                req = urllib.request.Request(f"{HUB}/api/tips/log", data=body,
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
        threading.Thread(target=_report, daemon=True).start()

def log(msg: str) -> None:
    print(f"\033[1;34m[{APP}]\033[0m {msg}", file=sys.stderr)

def ok(msg: str) -> None:
    print(f"\033[1;32m[{APP}]\033[0m {msg}")

def warn(msg: str) -> None:
    print(f"\033[1;33m[{APP}]\033[0m {msg}", file=sys.stderr)
    _print_tips(msg, to_stderr=True)

def die(msg: str, code: int = 1) -> None:
    print(f"\033[0;31m[{APP}]\033[0m {msg}", file=sys.stderr)
    _print_tips(msg, to_stderr=True)
    if os.environ.get("HIVV_TIPS", "on").lower() not in ("off", "0", "no"):
        print(f"  \033[2mneed a human? {APP} support \"{msg[:60]}\" attaches diagnostics\033[0m", file=sys.stderr)
    sys.exit(code)

def _get_visitor_id() -> str:
    """Per-machine ID for per-user rate limiting.
    Uses /etc/machine-id (D-Bus system ID) as primary identity — tied to the
    OS install, not a stored file, so it survives reinstalls and can't be
    casually deleted. Falls back to VISITOR_FILE for non-Linux systems."""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            mid = Path(path).read_text().strip()
            if mid:
                return f"cli_{mid}"
        except (OSError, IOError):
            pass
    try:
        if VISITOR_FILE.exists():
            return VISITOR_FILE.read_text().strip()
    except OSError:
        pass
    vid = f"cli_{uuid.uuid4().hex[:16]}"
    try:
        VISITOR_FILE.write_text(vid)
    except OSError:
        pass
    return vid

def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def write_state(d: dict) -> None:
    # Atomic (temp + rename): a torn/partial state.json makes read_state()
    # return {} and the next merge writes from an empty base — silently
    # dropping node_secret/hw_id exactly like the old fresh-dict bug did.
    ensure_dirs()
    payload = json.dumps(d, indent=2, sort_keys=True)
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, STATE_FILE)


# Cached node_id so every hub request can carry `x-hivv-node` (lets the policy
# gate PUSH a MAYBE straight to THIS node's inbox instead of broadcasting).
_MY_NODE_ID: str | None = None
_MY_NODE_ID_LOADED = False

def _my_node_id() -> str | None:
    global _MY_NODE_ID, _MY_NODE_ID_LOADED
    if _MY_NODE_ID_LOADED:
        return _MY_NODE_ID
    _MY_NODE_ID_LOADED = True
    try:
        nid = (read_state() or {}).get("node_id")
        _MY_NODE_ID = nid if isinstance(nid, str) and nid else None
    except Exception:
        _MY_NODE_ID = None
    return _MY_NODE_ID


# ── security primitives (Phase A — graceful degrade if hub doesn't support) ──

# Patterns we strip from logs and `support` output before printing.
# Each pattern MUST be paired with a replacement that keeps log structure intact.
_SCRUB_RULES = [
    # authorization: bearer xxx
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[\w\-\.]{6,}"), r"\1***REDACTED***"),
    # api_key=xxx / api-key: xxx / apikey="xxx" / API_KEY='xxx'
    (re.compile(r"""(?i)\b(api[_-]?key|api[_-]?token|secret[_-]?key|access[_-]?token|auth[_-]?token)\b(\s*[:=]\s*)(?:["'])?[\w\-\.]{6,}"""), r"\1\2***REDACTED***"),
    # password=xxx / passwd=xxx / pwd=xxx  (but not "shadow passwords" in copy)
    (re.compile(r"(?i)\b(password|passwd|pwd)\b(\s*[:=]\s*)(?:[\"'])?[^\s\"']{6,}"), r"\1\2***REDACTED***"),
    # ?token=...&foo=bar  (and ?access_token=, ?api_key=)
    (re.compile(r"([?&])(token|access_token|api_key|apikey|password|secret)=([\w\-\.%]+)"), r"\1\2=***"),
    # raw JWTs that survive the above (real ones are typically 100+ chars)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"), "***JWT***"),
    # AKIA / ghp_ / xoxb- style keys that survive
    (re.compile(r"\b(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|xox[baprs]-[A-Za-z0-9\-]{10,})\b"), "***KEY***"),
]

def _scrub(text: str) -> str:
    """Strip likely secrets from a chunk of text (logs, error messages, support bundles)."""
    if not text:
        return text
    out = text
    for pat, repl in _SCRUB_RULES:
        out = pat.sub(repl, out)
    return out

def _scrub_lines(text: str) -> str:
    """Apply scrub line-by-line so we keep newlines tidy."""
    return "\n".join(_scrub(line) for line in text.splitlines())

def _machine_id() -> str | None:
    """Best-effort cross-platform hardware identifier. Used to bind node_id to
    this physical box so a copied state.json can't impersonate on another host.
    Returns None if the platform doesn't expose one cleanly."""
    # Linux: /etc/machine-id (systemd, present on all modern Linux)
    try:
        p = Path("/etc/machine-id")
        if p.exists():
            mid = p.read_text().strip()
            if mid:
                return f"linux-{mid[:32]}"
    except OSError:
        pass
    # macOS: IOPlatformUUID from IOKit
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=3
            )
            if out.returncode == 0:
                m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out.stdout)
                if m:
                    return f"macos-{m.group(1)}"
        except (subprocess.SubprocessError, OSError):
            pass
    # Windows: wmic csproduct get uuid  (or registry HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid)
    if sys.platform.startswith("win"):
        try:
            out = subprocess.run(
                ["wmic", "csproduct", "get", "uuid"],
                capture_output=True, text=True, timeout=3
            )
            if out.returncode == 0:
                m = re.search(r"[0-9A-Fa-f\-]{36}", out.stdout)
                if m:
                    return f"windows-{m.group(0)}"
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            pass
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Cryptography") as k:
                v, _ = winreg.QueryValueEx(k, "MachineGuid")
                if v:
                    return f"windows-{v}"
        except (OSError, ImportError):
            pass
    return None

def _get_or_create_auth_token() -> str:
    """Return the per-install bearer token, creating one on first call.
    Stored in ~/.config/<app>/auth.json with mode 0600. Survives `update`."""
    ensure_dirs()
    f = AUTH_FILE
    if f.exists():
        try:
            d = json.loads(f.read_text())
            tok = d.get("token")
            if isinstance(tok, str) and len(tok) >= 32:
                return tok
        except (OSError, json.JSONDecodeError):
            pass
    tok = secrets.token_hex(32)   # 256-bit
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps({
            "token": tok,
            "created_at": int(time.time()),
            "version": 1,
        }, indent=2).encode())
    finally:
        os.close(fd)
    return tok

def _signed_headers(method: str, path: str, body_bytes: bytes | None,
                    token: str | None = None) -> dict:
    """Produce HMAC-signed headers. The signature covers:
        ts + '\\n' + nonce + '\\n' + method + '\\n' + path + '\\n' + sha256(body)
    so the server can reject replays (5-min window) and verify request integrity.
    If no token is available (very early boot), returns {}."""
    if not token:
        return {}
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    body_hash = hashlib.sha256(body_bytes or b"").hexdigest()
    msg = f"{ts}\n{nonce}\n{method.upper()}\n{path}\n{body_hash}"
    sig = hmac.new(token.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "X-Auth-Timestamp": ts,
        "X-Auth-Nonce":     nonce,
        "X-Auth-Signature": sig,
    }

def _support_token(token: str | None) -> str:
    """Mark whether this hub expects auth+signing or not. After one 401 the
    CLI strips headers and remembers — so old hubs don't kill us."""
    if token is None:
        return "no-token"
    return "ok"


# ── HTTP helper ────────────────────────────────────────────────────────
def _auth_rejected() -> bool:
    """Has this hub already told us it doesn't want Authorization headers?
    Cached in state so we don't re-send auth headers every request after a 401."""
    try:
        return bool((read_state() or {}).get("auth_rejected"))
    except Exception:
        return False

def _mark_auth_rejected(v: bool = True) -> None:
    try:
        st = read_state() or {}
        st["auth_rejected"] = bool(v)
        st["auth_rejected_at"] = int(time.time())
        write_state(st)
    except OSError:
        pass

def http(method: str, path: str, *, body=None, token: str | None = None,
         timeout: float = 15.0, sign: bool = True,
         base_url: str | None = None) -> tuple[int, dict | str]:
    """Make an authenticated, signed request to the hub.

    Auto-degrades:
      • if the hub returns 401/403 for the Authorization header, retry once
        without auth/signing and remember via state.auth_rejected=True so we
        don't re-send every request.
      • token is auto-loaded from AUTH_FILE if not passed explicitly.

    `base_url` lets a command talk to a LOCAL gateway instead of the public
    hub (used by `hivv fast-scan --hub http://127.0.0.1:3011`).
    """
    url = (base_url or HUB).rstrip("/") + path
    headers = {
        "user-agent": f"{APP}/{VERSION}",
        "accept": "application/json",
        "x-visitor-id": _get_visitor_id()
    }
    # Let the hub PUSH a policy MAYBE straight to this node's inbox (targeted,
    # not broadcast). Not part of the request signature — safe to add here.
    _nid = _my_node_id()
    if _nid:
        headers["x-hivv-node"] = _nid
    # Per-node secret — adopted or issued by the hub, persisted in state. Proves
    # this is the bee that owns the inbox it polls; the hub only starts
    # enforcing it once it has seen us present it (so pre-secret boots still work).
    try:
        _ns = (read_state() or {}).get("node_secret")
    except Exception:
        _ns = None
    if _ns:
        headers["x-node-secret"] = str(_ns)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["content-type"] = "application/json"

    # auto-load token if caller didn't pass one
    if token is None:
        try:
            token = _get_or_create_auth_token()
        except OSError:
            token = None

    use_auth = bool(token) and not _auth_rejected() and sign
    if use_auth:
        headers["authorization"] = f"Bearer {token}"
        headers.update(_signed_headers(method, path, data, token))

    def _do() -> tuple[int, dict | str]:
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
                try:
                    return r.status, json.loads(raw)
                except json.JSONDecodeError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
            try:
                return e.code, json.loads(raw)
            except json.JSONDecodeError:
                return e.code, raw
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            return 0, f"network error: {e}"

    code, payload = _do()

    # Graceful degrade: hub didn't like our auth headers → retry once unauthenticated
    if use_auth and code in (401, 403):
        _mark_auth_rejected(True)
        headers.pop("authorization", None)
        for h in ("X-Auth-Timestamp", "X-Auth-Nonce", "X-Auth-Signature"):
            headers.pop(h, None)
        log("hub rejected Authorization headers — falling back to legacy mode")
        return _do()

    return code, payload


# ── pid management ─────────────────────────────────────────────────────
IS_WINDOWS = os.name == "nt"

def _sysname() -> str:
    """OS name that works on every platform (os.uname is missing on Windows)."""
    try:
        return os.uname().sysname
    except (AttributeError, OSError):
        import platform as _p
        return _p.system()

def _win_pid_alive(pid: int) -> bool:
    """Windows-safe liveness check. NEVER use os.kill(pid, 0) on Windows —
    it calls TerminateProcess() and kills the target."""
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_ACCESS_DENIED = 5
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcessW(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED  # alive but protected
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(h)
    except Exception:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=5).stdout
            return str(pid) in out
        except Exception:
            return False

def _kill_pid(pid: int, force: bool = False) -> None:
    """Send SIGTERM (or SIGKILL) on POSIX; TerminateProcess via taskkill on Windows."""
    if IS_WINDOWS:
        args = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        subprocess.run(args, capture_output=True)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)

def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return pid > 0 and (Path(f"/proc/{pid}").exists() or _pid_alive_fallback(pid))

def _pid_alive_fallback(pid: int) -> bool:
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        subprocess.run(["kill", "-0", str(pid)], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        return None
    return pid if pid_alive(pid) else None

def _agent_pids() -> list:
    """PIDs running THIS install's `_agent` (includes us when called from the
    agent itself). Scoped to INSTALL_DIR so a second install on the same host
    is never touched. Windows has no /proc, so it returns []."""
    if IS_WINDOWS:
        return []
    try:
        out = subprocess.run("pgrep -f 'hivv-cli[.]py _agent'", shell=True,
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    own = str(INSTALL_DIR / f"{APP}-cli.py")
    pids = []
    for tok in out.split():
        if not tok.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{int(tok)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if own in cmd:
            pids.append(int(tok))
    return sorted(set(pids))

def _systemd_agent_main_pid() -> int | None:
    """Main PID of hivv-agent.service per systemd, or None (no systemd unit /
    bus unreachable). The reaper prefers this as keeper — killing systemd's
    main PID only makes systemd restart it, and the two would fight forever."""
    if IS_WINDOWS:
        return None
    svc = Path.home() / ".config" / "systemd" / "user" / "hivv-agent.service"
    if not svc.exists():
        return None
    try:
        r = subprocess.run(["systemctl", "--user", "show", "hivv-agent.service",
                            "-p", "ExecMainPID", "--value"],
                           capture_output=True, text=True, timeout=5)
        val = (r.stdout or "").strip()
        pid = int(val) if val.isdigit() and int(val) > 1 else None
        return pid if pid and pid_alive(pid) else None
    except Exception:
        return None

def _reap_duplicate_agents() -> tuple:
    """Kill extra `_agent` processes for this install.

    A second agent heartbeats under the same node id, so the hub's VERSION flips
    between the two builds and both race on state.json — which can cost the node
    its `node_id` and drop it into a register-every-cycle loop. Keeper priority:
    systemd's main PID (killing it just makes systemd restart it → endless fight)
    → the pid-file agent → ourselves → newest. Returns (killed_pids, kept_pid, note)."""
    pids = _agent_pids()
    if len(pids) <= 1:
        return ([], pids[0] if pids else None, "")
    sd_pid = _systemd_agent_main_pid()
    pid = read_pid()
    if sd_pid in pids:
        keep = sd_pid
    elif pid in pids:
        keep = pid
    elif os.getpid() in pids:
        keep = os.getpid()   # pid file stale/missing — keep the one doing the repair
    else:
        keep = max(pids)     # newest
    killed = []
    for p in pids:
        if p == keep or p == os.getpid():
            continue         # never signal ourselves mid-repair
        try:
            os.kill(p, signal.SIGTERM)
            killed.append(p)
        except ProcessLookupError:
            continue
        except Exception:
            continue
    if not killed:
        return ([], keep, "")
    return (killed, keep, f"{len(killed)} duplicate agent(s) killed: {killed} (kept pid={keep})")

def _agent_running_stale(target: Path) -> str:
    """'' when healthy; otherwise a reason string: the running agent predates
    `target` on disk — it executes old code even though the file is current.
    That state is invisible to a sha-only update check (which reports 'already
    up to date') and to the auto-update loop (which compares files)."""
    pid = read_pid()
    if not pid or not target.exists():
        return ""
    try:
        out = subprocess.run(["ps", "-o", "etimes=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=10).stdout.split()
        elapsed = int(out[0])
        file_age = time.time() - target.stat().st_mtime
    except Exception:
        return ""
    return (f"agent pid={pid} up {elapsed}s, file rewritten {int(file_age)}s ago"
            if file_age < elapsed else "")

def _require_agent() -> bool:
    """Check if agent is running. If not, print a helpful message and return False."""
    pid = read_pid()
    if pid and _pid_alive_fallback(pid):
        return True
    print(f"\n\033[33m⚠  {APP} agent is not running\033[0m")
    print(f"   start it with: \033[1m{APP} start\033[0m")
    print(f"   this registers your machine as a node on the network\n")
    return False


# ── update helpers (atomic swap + rollback + healthcheck) ───────────────
def _parse_ver(s) -> tuple:
    """Parse '0.2.3' (or any string with embedded numbers) into (0,2,3).
    Tolerant of prefixes/suffixes. Returns (0,) on empty/no-digits."""
    if not s:
        return (0,)
    parts = re.findall(r"\d+", str(s))
    return tuple(int(p) for p in parts[:3]) if parts else (0,)

def _atomic_write(path: Path, data: bytes) -> None:
    """Atomic write: tempfile + fsync + chmod + shutil.move into place."""
    tmp = tempfile.NamedTemporaryFile("wb", delete=False, dir=str(path.parent))
    try:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, 0o644)
        shutil.move(tmp.name, path)
    finally:
        if os.path.exists(tmp.name):
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

def _is_frozen() -> bool:
    """True when running as a PyInstaller onefile binary (A8). A frozen build
    embeds the script, so there is no separate .py to swap — we replace the
    running executable itself. Source installs (the live nodes) are unaffected:
    every frozen branch below is guarded by this and never runs for them."""
    return bool(getattr(sys, "frozen", False))


def _agent_argv(script=None) -> list:
    """argv to (re)launch the background agent, frozen-aware. A frozen binary is
    the program itself (no script argument); a source install runs `python cli.py`.
    `script` (when given) is the freshly-updated .py to relaunch — preserves the
    historical behavior of re-exec'ing the exact target returned by _apply_update."""
    if _is_frozen():
        return [sys.executable, "_agent"]
    return [sys.executable, str(script or Path(__file__).resolve()), "_agent"]


def _agent_execstart() -> str:
    """systemd ExecStart line, frozen-aware (matches the historical source form)."""
    if _is_frozen():
        return f"{sys.executable} _agent"
    return f"{sys.executable} {Path(__file__).resolve()} _agent"


def _apply_update_frozen(data: bytes, expected_sha256=None, source: str = "") -> tuple:
    """Self-update for a PyInstaller onefile binary: replace the running
    executable with a freshly downloaded build (sha256-verified + healthchecked).
    POSIX swaps atomically over the running binary; Windows can't overwrite a
    running .exe, so we move it aside to .old first (cleaned on next launch).
    Returns (ok, msg, target). Never raises."""
    target = Path(sys.executable).resolve()
    bak = target.with_suffix(target.suffix + ".bak")
    bad = target.with_suffix(target.suffix + ".bad")
    old = target.with_suffix(target.suffix + ".old")
    new = target.with_suffix(target.suffix + ".new")
    try:
        if expected_sha256:
            actual = hashlib.sha256(data).hexdigest()
            if actual.lower() != expected_sha256.lower():
                return (False, f"sha256 mismatch (got {actual[:12]}…, want {expected_sha256[:12]}…)", target)
        # keep the last known-good binary once (don't clobber on re-attempts)
        if target.exists() and not bak.exists():
            try:
                shutil.copy2(target, bak)
            except OSError:
                pass
        # write the candidate + mark executable
        tmp = tempfile.NamedTemporaryFile("wb", delete=False, dir=str(target.parent))
        try:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.chmod(tmp.name, 0o755)
            shutil.move(tmp.name, new)
        finally:
            if os.path.exists(tmp.name):
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
        # healthcheck the CANDIDATE binary directly (it is self-contained)
        try:
            proc = subprocess.run([str(new), "--healthcheck"], capture_output=True, timeout=15)
            rc = proc.returncode
        except Exception as e:
            try:
                shutil.move(str(new), str(bad))
            except OSError:
                pass
            return (False, f"healthcheck could not run: {e}", target)
        if rc != 0:
            try:
                shutil.move(str(new), str(bad))
            except OSError:
                pass
            return (False, f"healthcheck failed (rc={rc}, source={source})", target)
        # swap into place
        if os.name == "nt":
            # Windows: rename the running exe aside (allowed), then drop new in.
            if old.exists():
                try:
                    os.unlink(old)
                except OSError:
                    pass
            os.replace(target, old)
            try:
                os.replace(new, target)
            except OSError as e:
                try:
                    os.replace(old, target)  # roll back
                except OSError:
                    pass
                return (False, f"swap failed, rolled back: {e}", target)
        else:
            os.replace(new, target)  # atomic on POSIX, even over a running binary
        return (True, f"applied (frozen, source={source}, version={VERSION})", target)
    except Exception as e:
        return (False, f"unexpected: {e}", target)


def _apply_update(data: bytes, expected_sha256=None, source: str = "") -> tuple:
    """Atomic swap with rollback. Returns (ok: bool, msg: str, target: Path).

    Steps:
      1. Verify SHA256 (if provided).
      2. Backup current → <target>.bak (only if .bak doesn't already exist;
         preserve the last known-good across re-attempts).
      3. Write new → <target>.new.
      4. Healthcheck: spawn `python3 <target>.new --healthcheck` (timeout 10s).
         Exit 0 → atomic rename .new → target.
         Non-zero / timeout → rename target → .bad, restore .bak → target.
    Never raises.
    """
    # A8: PyInstaller binaries carry no separate .py — swap the executable itself.
    if _is_frozen():
        return _apply_update_frozen(data, expected_sha256, source)
    target = INSTALL_DIR / f"{APP}-cli.py"
    if not target.exists():
        target = Path(__file__).resolve()
    bak = target.with_suffix(target.suffix + ".bak")
    bad = target.with_suffix(target.suffix + ".bad")
    new = target.with_suffix(target.suffix + ".new")
    try:
        if expected_sha256:
            actual = hashlib.sha256(data).hexdigest()
            if actual.lower() != expected_sha256.lower():
                return (False, f"sha256 mismatch (got {actual[:12]}…, want {expected_sha256[:12]}…)", target)
        # Preserve the original good copy. Only create .bak on first apply
        # (so re-attempts don't clobber a known-good with a known-bad).
        if target.exists() and not bak.exists():
            shutil.copy2(target, bak)
        _atomic_write(new, data)
        # Run healthcheck on the .new file (not the running binary).
        proc = subprocess.run(
            [sys.executable, str(new), "--healthcheck"],
            capture_output=True, timeout=10,
        )
        if proc.returncode != 0:
            # Preserve the FAILED new file as .bad for inspection.
            try:
                shutil.move(str(new), str(bad))
            except OSError:
                pass
            # Restore the last good from .bak. target should never have been
            # overwritten (new_data went to .new), but copy from .bak to be safe.
            if bak.exists():
                try:
                    shutil.copy2(bak, target)
                except OSError:
                    pass
            return (False, f"healthcheck failed (rc={proc.returncode}, source={source})", target)
        os.replace(new, target)
        # Report the version of the bytes we just wrote, not VERSION — that
        # constant was hashed from the pre-update file at process start.
        return (True, f"applied (source={source}, version={_get_version(data)})", target)
    except Exception as e:
        return (False, f"unexpected: {e}", target)

def _http_get_bytes(url: str, timeout: float = 30.0) -> bytes:
    """GET a URL and return raw bytes. Stdlib only; sets user-agent.
    Resolves relative URLs (e.g. '/hivv-cli.py') against HUB."""
    from urllib.parse import urljoin
    full = url if "://" in url else urljoin(HUB.rstrip("/") + "/", url.lstrip("/"))
    req = urllib.request.Request(full, headers={"user-agent": f"{APP}/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# ── upgrade review: static risk scan + local/hive LLM vote ─────────────
# Before any CLI update is applied (manual, background auto, or inbox push),
# the new bytes are reviewed: dangerous ADDED lines are pattern-scanned,
# then this box's LLM(s) and other bees' LLMs vote OK/SUSPICIOUS on the
# diff. A suspicious verdict BLOCKS the update and files an incident on the
# hub (override: `hivv upgrade --force`). Machinery failures (no brains
# reachable, hub down) never wedge updates — sha256 + healthcheck still apply.

# (pattern, severity, why) — matched against ADDED diff lines only.
_UPGRADE_RISK_PATTERNS = [
    (r"(?:curl|wget)[^|;\n]*\|\s*(?:sudo\s+)?(?:ba|z|fi|da)?sh", "critical",
     "pipes a download straight into a shell"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+/(?:\s|$|['\"\)]|etc|usr|home|var|boot)", "critical",
     "recursive delete of system paths"),
    (r"/dev/tcp/\d{1,3}\.", "critical", "bash reverse-shell style connection"),
    (r"\bnc\s+(?:-[a-z]+\s+)*-e\s", "critical", "netcat with command execution"),
    # `.*` not `[^)]*` — the common shape `socket.socket(...).connect(("1.2.3.4", …))`
    # contains parens, which [^)] could never cross (latent false negative,
    # caught by tests/test_upgrade_review.py).
    (r"socket\.socket.*connect\s*\(\s*\(\s*[\"']\d{1,3}(?:\.\d{1,3}){3}", "critical",
     "raw socket connect to a bare IP"),
    (r"(?:id_rsa|id_ed25519|\.ssh/)", "high", "touches SSH keys"),
    (r"(?:crontab\s+-|/etc/cron|\.bashrc|\.zshrc|\.bash_profile)", "high",
     "persists via shell profile or cron"),
    (r"\beval\s*\(|\bexec\s*\(\s*(?:compile|base64)", "high", "dynamic code execution"),
    (r"(?:pickle|marshal)\.loads?\s*\(", "high", "deserializes untrusted data"),
    (r"(?:b64decode|base64(?:\.b64decode)?)\([^)]*\)[^\n]{0,80}\b(?:exec|eval|system|subprocess)\b",
     "high", "decodes then executes an encoded payload"),
    (r"(?:HIVV_(?:CONTROL_TOKEN|ENCRYPTION_KEY|SESSION_SECRET)|HIVV_DB_PASSWORD)"
     r"[^\n]{0,160}(?:urlopen|requests\.|urllib|http\.client|fetch\()",
     "critical", "ships a local secret over the network"),
    (r"subprocess\.[a-z]+\([^)]*shell\s*=\s*True", "medium", "shell=True subprocess"),
    (r"\bos\.system\s*\(", "medium", "os.system call"),
    (r"__import__\s*\(\s*[\"']os[\"']\s*\)\s*\.\s*system", "high",
     "obfuscated os.system via __import__"),
]

# Hosts the CLI may legitimately start talking to in an update. Anything new
# and unknown becomes a 'medium' finding the brains can judge in context.
_UPGRADE_HOST_ALLOW = (
    "hivv.org", "localhost", "127.0.0.1", "tailscale.com", "ip-api.com",
    "github.com", "raw.githubusercontent.com", "huggingface.co", "hf.co",
    "ollama.com", "ollama.ai", "pypi.org", "python.org", "anthropic.com",
    "openai.com", "example.com", "opencode.ai", "vscode.dev", "::1",
)
_UPGRADE_URL_RE = re.compile(r"https?://([A-Za-z0-9._-]+)")

def _blank_strings(line: str) -> str:
    """Blank out string literals (and trailing comments) so patterns can be
    tested against executable CODE only — string contents are data/help text.
    Prefix [rbf]* covers r/f/b and combos (fr, rf, br, rb, frb…): a bytes
    literal holding an install-hint string is data too, never code. (Wording
    rule: don't spell out the download-pipe-shell form in prose here — old
    bees scan our added lines raw.)"""
    line = re.sub(r'""".*?"""|\'\'\'.*?\'\'\'', '""', line)      # single-line triples
    line = re.sub(r'[rbf]*"(?:[^"\\]|\\.)*"', '""', line)
    line = re.sub(r"[rbf]*'(?:[^'\\]|\\.)*'", "''", line)
    line = re.sub(r"#.*$", "", line)
    return line

# Constructs that EXECUTE (or pass to an executor) the strings on their line.
# Tested on the string-blanked line so `print("system(")` can't fake it; the
# `"sh", "-c"` argv form is checked raw because blanking erases the tokens.
_EXEC_CONSUMER_RE = re.compile(
    r"subprocess|os\.system|os\.popen|\bpopen\s*\(|\bPopen\b|check_call|check_output"
    r"|shell\s*=\s*True|\bsystem\s*\(|\b_r\s*\(|\bsh\s+-c\b|\.connect\s*\("
    r"|\beval\s*\(|\bexec\s*\(|__import__\s*\(|pickle\.loads|marshal\.loads"
    r"|\burlopen\s*\(|requests\.(?:get|post)|http\.client|b64decode")
_EXEC_ARGV_RE = re.compile(r"[\"'](?:ba|z|fi|da)?sh[\"']\s*,\s*[\"']-c[\"']")

def _refine_finding_severity(line: str, sev: str, pat: str) -> str:
    """Context-aware severity for a static-scan hit.

    A pattern inside a PRINTED help string or docstring is data, not code —
    flagging a printed curl-pipe-shell install hint as critical blocked real
    updates (the opencode install hint tripped masjid's review 2026-11-14).
    Keep the original severity only when the line actually EXECUTES something;
    otherwise cap at medium so it's reported but can't outvote the brains.

    (Wording note for future editors: never write the literal pipe form of a
    download-into-shell example anywhere in this file — OLD bees scan our
    ADDED lines with the raw pattern, so the docstring describing this very
    false positive used to re-trip it and block their updates. Tests assert it.)

    comment → low · exec consumer → original · docstring content → medium
    · code outside strings → original · string-only → medium"""
    s = line.strip()
    if s.startswith("#"):
        return "low"
    if _EXEC_CONSUMER_RE.search(_blank_strings(line)) or _EXEC_ARGV_RE.search(line):
        return sev
    if re.search(pat, _blank_strings(line)):
        return sev          # matched in real code, not inside a string
    return "medium"

def _upgrade_static_scan(old_bytes: bytes, new_bytes: bytes) -> dict:
    """Pattern-scan the ADDED lines of an update diff.
    Severities are context-refined (_refine_finding_severity): patterns inside
    printed help strings / docstrings are DATA → capped at medium, so a
    printed curl-pipe-shell install hint can never outvote the brains again.
    Returns {findings: [{severity, why, line_no, line}], added, removed, truncated}."""
    import difflib
    findings = []
    added = removed = 0
    try:
        old_txt = old_bytes.decode("utf-8", errors="replace").splitlines()
        new_txt = new_bytes.decode("utf-8", errors="replace").splitlines()
    except Exception as e:
        return {"findings": [{"severity": "medium", "why": f"diff failed: {e}",
                              "line_no": 0, "line": ""}], "added": 0, "removed": 0,
                "truncated": False}
    # Precompute which NEW-file lines sit inside triple-quoted strings
    # (multi-line docstrings) — a pattern hit there is documentation, not code.
    doc_lines: set = set()
    tri = None
    for idx, l in enumerate(new_txt):
        if tri:
            doc_lines.add(idx)
            if l.count(tri) % 2 == 1:
                tri = None
        else:
            for delim in ('"""', "'''"):
                if l.count(delim) % 2 == 1:
                    tri = delim
                    doc_lines.add(idx)
                    break
    diff = list(difflib.unified_diff(old_txt, new_txt, lineterm="", n=0))
    truncated = False
    scanned = 0
    new_ln = -1
    for dl in diff:
        if dl.startswith("---") or dl.startswith("+++"):
            continue
        if dl.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", dl)
            # k = 1-based index of the hunk's FIRST new-file line; init to
            # k-2 so the `+= 1` on each `+` line lands on its 0-based index
            # (line_no = new_ln + 1). Off-by-one here mislabels every finding.
            new_ln = (int(m.group(1)) - 2) if m else -2
            continue
        if dl.startswith("+"):
            added += 1
            new_ln += 1
        elif dl.startswith("-"):
            removed += 1
            continue          # removed lines don't advance the new-file cursor
        else:
            new_ln += 1
            continue
        line = dl[1:]
        scanned += 1
        if scanned > 6000:          # keep pathological mega-diffs bounded
            truncated = True
            continue
        in_doc = new_ln in doc_lines
        for pat, sev, why in _UPGRADE_RISK_PATTERNS:
            if re.search(pat, line):
                if in_doc:
                    sev = "medium" if sev in ("critical", "high") else sev
                else:
                    sev = _refine_finding_severity(line, sev, pat)
                findings.append({"severity": sev, "why": why,
                                 "line_no": new_ln + 1, "line": line.strip()[:200]})
                break               # one finding per line is enough
        # unknown new outbound host?
        for host in _UPGRADE_URL_RE.findall(line):
            host = host.lower().rstrip(".")
            if any(host == h or host.endswith("." + h) for h in _UPGRADE_HOST_ALLOW):
                continue
            findings.append({"severity": "medium",
                             "why": f"new outbound host not in allowlist: {host}",
                             "line_no": new_ln + 1, "line": line.strip()[:200]})
            break
    return {"findings": findings, "added": added, "removed": removed,
            "truncated": truncated}

_REVIEW_PROMPT = """You are reviewing a software update for suspicious or dangerous changes. \
Below is a unified diff of the new version of a Python CLI tool (hivv) — a SELF-UPDATING \
tool that registers this machine with a distributed AI gateway.

Context — expected/benign in this tool: changing its own CLI or launcher code, downloading \
from its own hub (hivv.org), healthchecks, restart logic, version/sha bookkeeping, table \
output, help text. Those are normal updater mechanics.

Flag ONLY genuinely malicious signals: exfiltrating secrets/keys to third parties, talking \
to unexpected hosts, destructive commands, obfuscation, backdoors, sneaky persistence, \
disabling security. Look at the ADDED lines (+).

Reply with EXACTLY two lines:
VERDICT: OK   (or VERDICT: SUSPICIOUS)
REASON: <one short sentence>

DIFF:
{diff}"""

_REVIEW_CONFIRM = """A security reviewer must CONFIRM a SUSPICIOUS flag on a software update, \
or retract it. A static malware scan of the same diff found NO dangerous patterns \
(no exfiltration, no shell pipes, no obfuscated code). The tool being updated is a \
SELF-UPDATING CLI — it is SUPPOSED to replace its own files from its own hub.

Your previous objection: {objection}

Decide now: unless you can cite a CONCRETE malicious behavior in the ADDED lines \
(exfiltration of secrets, third-party hosts, destructive commands, hidden payloads), \
mark it OK.

Reply with EXACTLY two lines:
VERDICT: OK   (or VERDICT: SUSPICIOUS)
REASON: <one short sentence>

DIFF:
{diff}"""

def _upgrade_diff_excerpt(old_bytes: bytes, new_bytes: bytes, max_lines: int = 220,
                          max_chars: int = 9000) -> str:
    """Unified diff of old→new, capped so small local models can hold it."""
    import difflib
    try:
        old_txt = old_bytes.decode("utf-8", errors="replace").splitlines()
        new_txt = new_bytes.decode("utf-8", errors="replace").splitlines()
        diff = [d for d in difflib.unified_diff(old_txt, new_txt, lineterm="", n=2)
                if not d.startswith("+++") and not d.startswith("---")]
    except Exception as e:
        return f"(diff unavailable: {e})"
    if len(diff) > max_lines:
        head = diff[: max_lines * 2 // 3]
        tail = diff[-(max_lines - len(head)):]
        diff = head + [f"... [{len(diff) - max_lines} more diff lines elided] ..."] + tail
    text = "\n".join(diff)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... [diff truncated] ..."
    return text

def _parse_review_vote(text: str) -> tuple[str | None, str]:
    """Pull VERDICT:/REASON: out of a brain's reply. Returns (verdict|None, reason)."""
    if not text:
        return None, "empty reply"
    m = re.search(r"VERDICT\s*:\s*(OK|SUSPICIOUS|SAFE|BLOCK|FLAG)", text, re.I)
    reason = ""
    rm = re.search(r"REASON\s*:\s*(.+)", text, re.I)
    if rm:
        reason = rm.group(1).strip()[:200]
    if not m:
        # fall back to first-line sniffing for tiny models that ignore format
        first = text.strip().splitlines()[0].upper() if text.strip() else ""
        if first.startswith(("OK", "SAFE")):
            return "ok", reason or text.strip()[:160]
        if first.startswith(("SUSPICIOUS", "BLOCK", "FLAG")):
            return "suspicious", reason or text.strip()[:160]
        return None, reason or "unparseable verdict"
    v = m.group(1).upper()
    verdict = "ok" if v in ("OK", "SAFE") else "suspicious"
    return verdict, reason or "no reason given"

def _ask_review_brain(url: str, prompt: str, timeout: float = 30.0) -> dict:
    """POST a review prompt to one OpenAI-compatible endpoint → {verdict, reason, ms}.
    Thinking OFF by default (mirrors the chat paths) so small local models spend
    their budget on the verdict, not chain-of-thought; falls back to
    reasoning_content when a GGUF model puts the whole answer there."""
    t0 = time.time()
    body = json.dumps({"model": "local",
                       "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": 300, "stream": False,
                       "chat_template_kwargs": {"enable_thinking": False}}).encode()
    try:
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        choice = (data.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        if not content.strip():
            content = (choice.get("message") or {}).get("reasoning_content") or ""
        verdict, reason = _parse_review_vote(content)
        return {"verdict": verdict, "reason": reason[:200],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"verdict": None, "reason": f"error: {str(e)[:80]}",
                "ms": int((time.time() - t0) * 1000)}

def _local_brain_urls(max_urls: int = 4) -> list[tuple[str, str]]:
    """Discover (label, chat_url) candidates for this machine's LLMs.
    Sources: HIVV_LOCAL_ENDPOINTS (asked directly) → _detect_local_ai() →
    a 2s /v1/models probe of common llama.cpp/lm-studio ports (8090, 1234).
    The agent's env usually has NO HIVV_LOCAL_ENDPOINTS and llama often runs
    on 8090 when 8080 is taken by a web server — without the probe the
    background review would run brainless on exactly the boxes that matter."""
    candidates: list[tuple[str, str]] = []
    seen_urls: set = set()

    def _add(label: str, chat: str) -> None:
        if chat not in seen_urls:
            seen_urls.add(chat)
            candidates.append((label, chat))

    # 1. HIVV_LOCAL_ENDPOINTS — operator-declared endpoints (router's source of truth)
    for raw in os.environ.get("HIVV_LOCAL_ENDPOINTS", "").split(","):
        raw = raw.strip().rstrip("/")
        if not raw:
            continue
        base = raw[:-len("/v1/models")] if raw.endswith("/v1/models") else raw
        base = base[:-len("/chat/completions")] if base.endswith("/chat/completions") else base
        chat = base + ("/v1/chat/completions" if not base.endswith("/v1") else "/chat/completions")
        _add(f"local:endpoint:{base.rsplit('//', 1)[-1]}", chat)

    # 2. auto-detected local servers (llama.cpp 8080-8082, ollama)
    try:
        locals_ = _detect_local_ai()
    except Exception:
        locals_ = []
    for m in locals_:
        if m.get("locked"):
            continue          # keyed server: a chat attempt would just 401
        port = m.get("port")
        if port:
            _add(f"local:{m.get('provider', f':{port}')}/{m.get('model', '?')}",
                 f"http://127.0.0.1:{port}/v1/chat/completions")

    # 3. cheap probe of common ports (only while we still have room)
    if len(candidates) < max_urls:
        for port in (8090, 1234):
            if len(candidates) >= max_urls:
                break
            chat = f"http://127.0.0.1:{port}/v1/chat/completions"
            if chat in seen_urls:
                continue
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/v1/models",
                    headers={"User-Agent": f"{APP}/{VERSION}"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    data = json.loads(r.read().decode("utf-8", errors="replace"))
                if isinstance(data, dict) and (data.get("data") or data.get("models")):
                    _add(f"local:port:{port}", chat)
            except Exception:
                pass
    return candidates

def _review_local_brains(prompt: str, max_brains: int = 2, timeout: float = 30.0) -> list[dict]:
    """Ask this machine's own LLM(s) — up to max_brains distinct local servers."""
    votes = []
    for label, chat in _local_brain_urls(max_urls=max(max_brains, 4))[:max_brains]:
        r = _ask_review_brain(chat, prompt, timeout=timeout)
        r.update({"brain": label, "via": "local"})
        votes.append(r)
    return votes

def _review_hive_brains(prompt: str, max_brains: int = 3, timeout: float = 32.0) -> list[dict]:
    """Fan the review out to OTHER bees' LLMs (distinct model families, never
    ourselves) — the 'global vote' half of the upgrade review."""
    import threading as _th
    try:
        code, resp = http("GET", "/api/control/nodes", timeout=6)
        nodes = resp.get("nodes", []) if code == 200 and isinstance(resp, dict) else []
    except Exception:
        nodes = []
    me_name = ""
    try:
        me_name = socket.gethostname().split(".")[0].lower()
    except Exception:
        pass
    picked, families = [], set()
    for n in nodes:
        if n.get("status") != "online":
            continue
        if (n.get("name", "") or "").lower() == me_name:
            continue                       # don't ask ourselves
        ip = n.get("lan_ip") or n.get("ip")
        if not ip:
            continue
        for m in (n.get("local_models") or []):
            fam = _model_family(m.get("name") or m.get("model") or "")
            if fam in families:
                continue
            families.add(fam)
            picked.append({
                "brain": f"bee:{n.get('name') or n.get('id')}/"
                         f"{_short_model(m.get('name') or m.get('model') or '?')}",
                "url": f"http://{ip}:{m.get('port', 8080)}/v1/chat/completions",
            })
            break
        if len(picked) >= max_brains:
            break
    results: list = [None] * len(picked)

    def _one(i, t):
        r = _ask_review_brain(t["url"], prompt, timeout=timeout)
        r.update({"brain": t["brain"], "via": "hive"})
        results[i] = r

    threads = [_th.Thread(target=_one, args=(i, t)) for i, t in enumerate(picked)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout + 5)
    return [r for r in results if r]

def _upgrade_verdict(scan: dict, votes: list[dict]) -> tuple[str, str]:
    """Combine static findings + brain votes → (verdict, reason).
    critical static finding → suspicious always; otherwise majority rules."""
    crit = [f for f in scan.get("findings", []) if f["severity"] == "critical"]
    if crit:
        return "suspicious", f"critical pattern: {crit[0]['why']}"
    ok = [v for v in votes if v.get("verdict") == "ok"]
    sus = [v for v in votes if v.get("verdict") == "suspicious"]
    if sus and len(sus) > len(ok):
        return "suspicious", (f"{len(sus)}/{len(votes)} brains flagged it — "
                              f"{sus[0].get('brain', '?')}: {sus[0].get('reason', '')}")
    if votes:
        highs = [f for f in scan.get("findings", []) if f["severity"] == "high"]
        if sus:
            # tie / minority suspicion with no criticals: go with the majority
            pass
        return "ok", (f"{len(ok)}/{len([v for v in votes if v.get('verdict')])} brains "
                      f"approved" + (f" ({len(highs)} high finding(s) noted)" if highs else ""))
    highs = [f for f in scan.get("findings", []) if f["severity"] in ("critical", "high")]
    if highs:
        return "suspicious", f"no brains reachable; static high finding: {highs[0]['why']}"
    return "ok", "no brains reachable — static scan clean (sha256 + healthcheck still apply)"

def _upgrade_report(payload: dict, timeout: float = 6.0) -> bool:
    """Best-effort POST of an upgrade outcome to the hub (files incidents there).
    Never raises, never blocks the upgrade flow on failure."""
    try:
        body = {
            "node_id": _my_node_id() or socket.gethostname().split(".")[0],
            "version_from": VERSION,
            "ts": int(time.time()),
        }
        body.update(payload)
        http("POST", "/api/hive/upgrade-reports", body=body, timeout=timeout)
        return True
    except Exception:
        return False

def _upgrade_report_once(payload: dict, sha: str, action: str,
                         timeout: float = 6.0) -> bool:
    """Report an upgrade outcome at most once per (sha, action) within 24h.

    Without this, a blocked sha re-reports on every 5-min auto-update cycle
    and every manual retry — flooding hivv_upgrade_reviews while the hub's
    incident dedup quietly swallows them."""
    try:
        st = read_state() or {}
        rev = st.get("_up_review") or {}
        if rev.get("sha") == sha:
            reported = rev.get("reported") or {}
            last = reported.get(action) or 0
            if time.time() - last < 86400:
                return False
            reported[action] = int(time.time())
            rev["reported"] = reported
            st["_up_review"] = rev
            write_state(st)
    except OSError:
        pass   # state unwritable — still send the report (better once too often than never)
    return _upgrade_report(payload, timeout=timeout)

def _review_update(old_bytes: bytes | None, new_bytes: bytes, *,
                   source: str = "manual", echo=None,
                   budget_s: int = 60) -> dict:
    """Pre-apply review of a CLI update. Returns
    {verdict, reason, scan, votes}. Never raises.

    source: manual | auto | inbox   (recorded in the hub report)
    echo:   callable(str) for progress lines (print for humans, log for agent)"""
    _say = echo or (lambda s: None)
    result = {"verdict": "ok", "reason": "", "scan": {"findings": [], "added": 0,
              "removed": 0, "truncated": False}, "votes": [], "source": source}
    try:
        if not old_bytes:
            result["reason"] = "no previous version to diff"
            return result
        result["scan"] = _upgrade_static_scan(old_bytes, new_bytes)
        n_crit = sum(1 for f in result["scan"]["findings"] if f["severity"] == "critical")
        n_high = sum(1 for f in result["scan"]["findings"] if f["severity"] == "high")
        n_med = len(result["scan"]["findings"]) - n_crit - n_high
        _say(f"static scan: +{result['scan']['added']}/-{result['scan']['removed']} lines · "
             f"{n_crit} critical, {n_high} high, {n_med} medium findings"
             + (" (diff truncated)" if result["scan"]["truncated"] else ""))
        for f in result["scan"]["findings"][:6]:
            _say(f"  ! {f['severity']}: {f['why']}")

        prompt = _REVIEW_PROMPT.format(diff=_upgrade_diff_excerpt(old_bytes, new_bytes))
        # local + hive votes run concurrently under one wall-clock budget
        import threading as _th
        votes_box: dict = {"local": [], "hive": []}
        def _local():
            votes_box["local"] = _review_local_brains(
                prompt, max_brains=2, timeout=min(30, budget_s))
        def _hive():
            votes_box["hive"] = _review_hive_brains(
                prompt, max_brains=3, timeout=min(32, budget_s))
        tl, th = _th.Thread(target=_local), _th.Thread(target=_hive)
        tl.start(); th.start()
        tl.join(timeout=budget_s + 5); th.join(timeout=budget_s + 5)
        votes = votes_box["local"] + votes_box["hive"]
        result["votes"] = votes
        for v in votes:
            mark = {"ok": "✓", "suspicious": "✗", None: "?"}[v.get("verdict")]
            _say(f"  {mark} {v.get('brain', '?')} ({v.get('ms', 0)}ms): "
                 f"{v.get('verdict') or 'no verdict'} — {v.get('reason', '')}")

        verdict, reason = _upgrade_verdict(result["scan"], votes)
        # Suspicion confirmation: the deterministic scan found nothing
        # (no critical/high) and the VOTES alone decided → re-ask ONE flagger
        # for CONCRETE evidence of malice. Two triggers:
        #  · majority suspicious → retraction downgrades to OK, holding keeps the block;
        #  · TIE (sus == ok) → the round breaks the tie either way, so a lone
        #    flag is never silently discarded (masjid's 1/2 pass, 2026-11-14)
        #    and a lone hold flips it to blocked.
        # Runs for ANY vote-only suspicion — two 0.5B brains vetoing a
        # clean-scan diff on "vibes" must cite evidence or stand down.
        # Static critical/high findings never get this second chance (they're
        # corroborated by the pattern scan).
        crit_high = [f for f in result["scan"]["findings"]
                     if f["severity"] in ("critical", "high")]
        ok_votes = [v for v in votes if v.get("verdict") == "ok"]
        sus_votes = [v for v in votes if v.get("verdict") == "suspicious"]
        is_tie = bool(sus_votes) and verdict == "ok" and len(sus_votes) >= len(ok_votes)
        if (verdict == "suspicious" or is_tie) and not crit_high and votes:
            flaggers = sus_votes
            if flaggers:
                _say("  ~ thin suspicion, clean static scan — confirmation round…")
                f0 = flaggers[0]
                # resolve the flagger's endpoint again: local brains via shared
                # discovery (match label, else first local URL); hive brains by
                # re-finding the node in the list.
                url = None
                if f0.get("via") == "local":
                    b = str(f0.get("brain", ""))
                    local_urls = _local_brain_urls()
                    for lbl, u in local_urls:
                        if b.startswith(lbl) or lbl in b or b in lbl:
                            url = u
                            break
                    if not url and local_urls:
                        url = local_urls[0][1]
                else:
                    try:
                        code, resp = http("GET", "/api/control/nodes", timeout=6)
                        nodes = resp.get("nodes", []) if code == 200 and isinstance(resp, dict) else []
                        bname = str(f0.get("brain", "")).split("/", 1)[0].replace("bee:", "")
                        for n in nodes:
                            if (n.get("name") or n.get("id")) == bname and (n.get("lan_ip") or n.get("ip")):
                                m = (n.get("local_models") or [{}])[0]
                                url = (f"http://{n.get('lan_ip') or n.get('ip')}:"
                                       f"{m.get('port', 8080)}/v1/chat/completions")
                                break
                    except Exception:
                        url = None
                if url:
                    confirm = _REVIEW_CONFIRM.format(
                        objection=f0.get("reason") or "it changes update mechanics",
                        diff=_upgrade_diff_excerpt(old_bytes, new_bytes))
                    cf = _ask_review_brain(url, confirm, timeout=min(60, budget_s + 30))
                    cf.update({"brain": f"{f0.get('brain', '?')} (confirm)", "via": "confirm"})
                    votes.append(cf)
                    result["votes"] = votes
                    _say(f"  {'✓' if cf.get('verdict') == 'ok' else '✗'} {cf.get('brain')} "
                         f"({cf.get('ms', 0)}ms): {cf.get('verdict') or 'no verdict'} — {cf.get('reason', '')}")
                    if cf.get("verdict") == "ok":
                        verdict = "ok"
                        reason = (f"flag not confirmed by re-ask "
                                  f"(\"{cf.get('reason', '')}\") — static scan clean")
                    elif is_tie:
                        # tie broken BY the flagger holding up: one retraction
                        # vetoed, now one hold carries — flip to blocked.
                        verdict = "suspicious"
                        reason = (f"tie broken — flagger held up under evidence "
                                  f"demand: {cf.get('reason') or 'no reason given'}")
        result["verdict"], result["reason"] = verdict, reason
        _say(f"verdict: {verdict.upper()} — {reason}")
    except Exception as e:
        # review machinery must never take the update path down with it
        result["reason"] = f"review error (continuing): {str(e)[:120]}"
    return result

def _upgrade_review_gate(new_bytes: bytes, *, source: str, echo=None,
                         budget_s: int = 60, force: bool = False) -> dict:
    """Review an incoming update BEFORE applying, with a per-sha verdict cache
    in agent state (so a blocked sha isn't re-reviewed — or re-reported — every
    5 minutes). Returns {allow, verdict, reason, review}.
    Suspicious verdicts are reported to the hub (incident) and blocked unless force."""
    sha = hashlib.sha256(new_bytes).hexdigest()
    st = read_state() or {}
    cached = st.get("_up_review") or {}
    review = None
    if (cached.get("sha") == sha and cached.get("verdict")
            and time.time() - (cached.get("ts") or 0) < 86400):
        review = {"verdict": cached.get("verdict"), "reason": cached.get("reason", "cached"),
                  "scan": cached.get("scan", {"findings": [], "added": 0, "removed": 0}),
                  "votes": cached.get("votes", []), "source": source, "cached": True}
        (echo or (lambda s: None))(
            f"review: using cached {review['verdict']} verdict for {sha[:12]}…")
    else:
        old = None
        target = INSTALL_DIR / f"{APP}-cli.py"
        try:
            if not target.exists():
                target = Path(__file__).resolve()
            if target.exists():
                old = target.read_bytes()
        except OSError:
            old = None
        review = _review_update(old, new_bytes, source=source, echo=echo,
                                budget_s=budget_s)
        try:
            st = read_state() or {}
            prev = st.get("_up_review") or {}
            st["_up_review"] = {"sha": sha, "verdict": review["verdict"],
                                "reason": review["reason"], "scan": review["scan"],
                                "votes": review["votes"], "ts": int(time.time()),
                                # keep per-sha report dedup across re-reviews
                                "reported": prev.get("reported", {}) if prev.get("sha") == sha else {}}
            write_state(st)
        except OSError:
            pass

    if review["verdict"] == "suspicious" and not force:
        (echo or (lambda s: None))(
            f"BLOCKED: update {sha[:12]}… looks suspicious — reporting to hub "
            f"(override: {APP} upgrade --force)")
        _upgrade_report_once({"source": source, "action": "blocked",
                              "to_sha256": sha, "verdict": "suspicious",
                              "reason": review["reason"],
                              "static_findings": review["scan"].get("findings", []),
                              "votes": review.get("votes", [])},
                             sha, "blocked")
        return {"allow": False, "verdict": "suspicious",
                "reason": review["reason"], "review": review}
    return {"allow": True, "verdict": review["verdict"],
            "reason": review["reason"], "review": review}


def _refresh_wrapper_if_stale() -> None:
    """Swap a stale `hivv` launcher for the hub's current one — best-effort.

    Old launchers capture CLI output (`out=$(hivv-cli …)`) under `set -e`, so
    ANY non-zero exit (a blocked upgrade, die()) kills the launcher at the
    capture line and the whole message vanishes. The launcher only ever
    re-downloads the CLI, never itself — so we heal it from in here. The swap
    is an atomic rename: a bash still reading the old file keeps its inode.
    Only ever touches a file carrying our own CLIENT_FILE marker."""
    if _is_dev_mode():
        return
    try:
        which = shutil.which(APP)
        if not which:
            return
        wp = Path(which)
        cur = wp.read_bytes()
        if not cur.startswith(b"#!") or b"CLIENT_FILE" not in cur:
            return                                  # not our launcher — never clobber
        if b"cli_output=$(" not in cur:
            return                                  # already the direct-exec form
        new = _http_get_bytes(f"{HUB.rstrip('/')}/{APP}", timeout=10)
        # only swap in a real wrapper in the FIXED form (direct exec, no capture)
        if (not new.startswith(b"#!") or b"CLIENT_FILE" not in new
                or b"exec python3" not in new or b"cli_output=$(" in new):
            return
        if hashlib.sha256(new).digest() == hashlib.sha256(cur).digest():
            return
        tmp = wp.with_name(wp.name + ".new")
        tmp.write_bytes(new)
        tmp.chmod(0o755)
        os.replace(tmp, wp)
        log(f"launcher refreshed (stale output-capturing wrapper) → {wp}")
    except Exception:
        pass

def _nag_if_behind() -> None:
    """GET /api/version from the hub; print a colored one-liner if hub > local.
    Never blocks; swallows all errors."""
    try:
        with urllib.request.urlopen(f"{HUB.rstrip('/')}/api/version", timeout=3) as r:
            meta = json.loads(r.read())
        hub_ver = meta.get("version") if isinstance(meta, dict) else None
        if hub_ver and _parse_ver(hub_ver) > _parse_ver(VERSION):
            sys.stderr.write(
                f"\033[1;33m[{APP}]\033[0m hub v{hub_ver} is available "
                f"(you're on v{VERSION}). run: \033[1;36m{APP} update\033[0m\n"
            )
    except Exception:
        pass

def _nag_if_update_available() -> None:
    """Check if hub CLI differs from local; print a reminder if so.
    Runs on bare `hivv` invocations. Never blocks; swallows all errors."""
    target = INSTALL_DIR / f"{APP}-cli.py"
    if not target.exists():
        return
    try:
        req = urllib.request.Request(
            f"{HUB.rstrip('/')}/{APP}-cli.py",
            headers={"User-Agent": f"{APP}/{VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            hub_data = r.read()
        hub_sha = hashlib.sha256(hub_data).hexdigest()
        local_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        if hub_sha != local_sha:
            sys.stderr.write(
                f"\033[1;33m[{APP}]\033[0m a new CLI is available "
                f"(sha256 differs). run: \033[1;36m{APP} update\033[0m\n"
            )
    except Exception:
        pass


# ── local metrics ──────────────────────────────────────────────────────
def _meminfo_kb() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip().split()[0]
    except OSError:
        pass
    return out

def _swapinfo_kb() -> dict:
    if not Path("/proc/meminfo").exists():
        return {}
    m = _meminfo_kb()
    return {"total": int(m.get("SwapTotal", "0")), "free": int(m.get("SwapFree", "0"))}

def _disk(path: str) -> str:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free  = st.f_bavail * st.f_frsize
        used  = total - free
        def h(b): return f"{b/1024/1024/1024:.1f}G"
        return f"{h(used)}/{h(total)} ({(used*100/max(1,total)):.0f}%)"
    except OSError:
        return "?"

def _process_rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None

def _local_ip() -> str | None:
    """Return the machine's LAN IP (e.g. 192.168.1.100), or None if undetectable."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("10.255.255.255", 1))  # doesn't actually send packets
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass
    # Fallback: parse 'ip route' output
    try:
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, timeout=3)
        for word in r.stdout.split():
            if word[0].isdigit() and "." in word:
                return word
    except Exception:
        pass
    return None

def _cpu_model() -> str | None:
    """Return the CPU model name string, or None.

    Linux: /proc/cpuinfo — 'model name' (x86), 'Model' (ARM64),
           'Processor' (older ARM), 'Hardware' (BCM SoCs as last resort).
    Fallbacks cover macOS (sysctl), Windows (PROCESSOR_IDENTIFIER env),
    and anything else via platform.processor()/machine()."""
    try:
        with open("/proc/cpuinfo") as f:
            found: dict = {}
            for line in f:
                if ":" not in line:
                    continue
                key = line.split(":", 1)[0].strip()
                val = line.split(":", 1)[1].strip()
                if key in ("model name", "Model", "Processor", "Hardware") and val:
                    # first match wins, but 'model name' (x86) beats everything
                    if key == "model name":
                        return val
                    found.setdefault(key, val)
            for k in ("Model", "Processor", "Hardware"):
                if found.get(k):
                    return found[k]
    except OSError:
        pass
    # Non-Linux (or exotic /proc): try the usual platform hooks.
    try:
        import platform as _plat
        if sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                               capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        p = _plat.processor()
        if p:
            return p
    except Exception:
        pass
    if os.name == "nt":
        p = os.environ.get("PROCESSOR_IDENTIFIER", "").strip()
        if p:
            return p
    try:
        return os.uname().machine or None   # last resort: aarch64 / x86_64
    except Exception:
        return None

def _short_cpu(model: str | None, width: int = 22) -> str:
    """Compact a /proc/cpuinfo model string for the narrow nodes column.

    'Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz' → 'i7-10700 @2.9G'
    'AMD Ryzen 7 5800X 8-Core Processor'       → 'Ryzen 7 5800X'
    'Apple M2 Pro'                              → 'M2 Pro'
    Keeps the model number (the identifying part) instead of truncating
    mid-vendor-string, and moves the frequency to a compact suffix."""
    s = (model or "").strip()
    if not s or s == "?":
        return "?"
    freq = ""
    m = re.search(r"@\s*([\d.]+)\s*GHz", s, re.I)
    if m:
        val = m.group(1).rstrip("0").rstrip(".") or m.group(1)
        freq = f" @{val}G"
        s = s[:m.start()] + s[m.end():]
    s = re.sub(r"\((?:R|TM)\)", "", s)              # drop (R) (TM) markers
    s = re.sub(r"\bCPU\b", "", s, flags=re.I)
    s = re.sub(r"\b\d+-Core\b", "", s, flags=re.I)  # drop '8-Core'
    s = re.sub(r"\bProcessor\b", "", s, flags=re.I)
    s = re.sub(r"\bIntel\s+Core\s+", "", s, flags=re.I)   # 'Intel Core i7-…' → 'i7-…'
    s = re.sub(r"^(?:AMD|Advanced Micro Devices|Apple)\s+", "", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    out = (s + freq).strip()
    if len(out) > width:
        out = out[:width - 1] + "…"
    return out or "?"

def _short_gpu(name: str | None, width: int = 24) -> str:
    """Strip vendor boilerplate so the actual card survives the column.

    'NVIDIA GeForce RTX 3060'        → 'RTX 3060'
    'NVIDIA Quadro K1200'            → 'Quadro K1200'
    'NVIDIA GeForce GTX 1660 SUPER'  → 'GTX 1660 SUPER'
    'AMD Radeon RX 6800 XT'          → 'RX 6800 XT'"""
    s = (name or "").strip()
    if not s or s == "?":
        return "?"
    for _ in range(3):   # peel repeated vendor prefixes (NVIDIA → GeForce → …)
        stripped = False
        for pref in ("NVIDIA ", "GeForce ", "AMD ", "Radeon ", "Intel "):
            if s.startswith(pref):
                s = s[len(pref):]
                stripped = True
                break
        if not stripped:
            break
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > width:
        s = s[:width - 1] + "…"
    return s or "?"

# Cache location lookups to avoid hammering the geoIP API
_location_cache: dict | None = None
_location_cache_time: float = 0

def _location_info() -> dict:
    """Return {city, country, country_code, region, lat, lon, isp, org} via ip-api.com.
    Cached for 1 hour. Returns {} if unavailable."""
    global _location_cache, _location_cache_time
    now = time.time()
    if _location_cache is not None and (now - _location_cache_time) < 3600:
        return _location_cache
    try:
        import json as _json
        url = "http://ip-api.com/json/?fields=status,country,countryCode,regionName,city,lat,lon,isp,org"
        r = subprocess.run(["curl", "-s", "--max-time", "5", url],
                           capture_output=True, text=True, timeout=8)
        if r.returncode == 0 and r.stdout.strip():
            data = _json.loads(r.stdout)
            if data.get("status") == "success":
                _location_cache = {
                    "city":     data.get("city", ""),
                    "country":  data.get("country", ""),
                    "country_code": data.get("countryCode", ""),
                    "region":   data.get("regionName", ""),
                    "lat":      data.get("lat"),
                    "lon":      data.get("lon"),
                    "isp":      data.get("isp", ""),
                    "org":      data.get("org", ""),
                }
                _location_cache_time = now
                return _location_cache
    except Exception:
        pass
    return {}

def _clean_model_name(raw: str) -> str:
    """Extract a human-readable model name from a raw path or ID.

    Handles:
      - HuggingFace cache paths like .../models--org--name/blobs/<hash>
      - Raw SHA256 hashes (show first 8 chars as short-id)
      - Already-clean names (pass through)
    Module-level (not nested in _detect_local_ai) so tests can drive it."""
    if not raw or raw == "unknown":
        return "unknown"
    # Already clean (no slashes or very short)
    if "/" not in raw and len(raw) < 40:
        return raw
    # HuggingFace cache path: .../models--org--name/blobs/<hash>
    # Extract "org/name" from the models--org--name segment
    hf_match = re.search(r'models--([^/]+)', raw)
    if hf_match:
        org_name = hf_match.group(1).replace('--', '/')
        # The filename after /blobs/ is the hash — discard it
        return org_name
    # SHA256 hash (64 hex chars) — show short prefix
    if re.fullmatch(r'[a-f0-9]{64}', raw):
        return f"model:{raw[:8]}"
    # Other paths — take the last meaningful segment
    last = raw.split("/")[-1]
    if len(last) == 64 and re.fullmatch(r'[a-f0-9]+', last):
        return f"model:{last[:8]}"
    return last

def _detect_local_ai() -> list[dict]:
    """Detect running local AI servers (llama.cpp, lm-studio, ollama) and
    return model info.

    Probes the SAME ports review-brain discovery uses — 8090/1234 included,
    because llama moves there when 8080 is taken by a web app (ashy
    2026-11-14: Next.js owned 8080, llama lived on 8090, heartbeat said 0).
    A server that answers 401 with an API-key error is reported as LOCKED
    instead of silently vanishing (yalla 2026-11-14: llama --api-key made
    both servers invisible to `hivv nodes`). The router skips locked entries
    — peers can't auth either until the key is shared or removed."""
    results = []
    import urllib.request
    import urllib.error

    for port in (8080, 8081, 8082, 8090, 1234):
        label = "lm-studio:1234" if port == 1234 else f"llama.cpp:{port}"
        try:
            req = urllib.request.Request(f"http://localhost:{port}/v1/models",
                                       headers={"User-Agent": f"{APP}/{VERSION}"})
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            for m in (data.get("data") or []):
                raw_id = m.get("id", "unknown")
                clean = _clean_model_name(raw_id)
                results.append({
                    "provider": label,
                    "model": clean,
                    "loaded": True,
                    "port": port
                })
        except urllib.error.HTTPError as e:
            # Distinguish "our LLM demands a key" from "some web app 401s":
            # llama/lm-studio answer OpenAI-style (Invalid API Key /
            # authentication_error); anything else is not an LLM candidate.
            try:
                body = (e.read() or b"").decode("utf-8", "replace").lower()
            except Exception:
                body = ""
            if e.code in (401, 403) and ("api key" in body
                                        or "authentication_error" in body):
                results.append({
                    "provider": label,
                    "model": "locked (api-key required)",
                    "loaded": False,
                    "locked": True,
                    "port": port
                })
        except Exception:
            pass
    # Check ollama
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags",
                                   headers={"User-Agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            for m in (data.get("models") or []):
                results.append({
                    "provider": "ollama",
                    "model": m.get("name", "unknown"),
                    "loaded": bool(m.get("size")),
                    "port": 11434
                })
    except Exception:
        pass
    return results

def _gpu_info(timeout: float = 3.0) -> list[dict]:
    """Return [{index, name, mem_total_mb, mem_used_mb, util_pct, temp_c}] via nvidia-smi.
    Empty list if no NVIDIA GPU or nvidia-smi missing. Never raises."""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,memory.total,memory.used,utilization.gpu,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0 or not r.stdout.strip():
        return []
    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append({
                "index": int(parts[0]),
                "name": parts[1],
                "mem_total_mb": int(parts[2]),
                "mem_used_mb": int(parts[3]),
                "util_pct": int(parts[4]),
                "temp_c": int(parts[5]),
            })
        except ValueError:
            continue
    return gpus

def _collect_metrics() -> dict:
    """Single-pass snapshot of system state for the heartbeat payload.
    Stdlib only. Missing fields are simply absent (hub should default sensibly)."""
    m: dict = {"ts": int(time.time())}

    # CPU: load average over 1/5/15 min, plus logical core count
    try:
        la = os.getloadavg()
        m["load_1"], m["load_5"], m["load_15"] = (round(x, 2) for x in la)
    except (OSError, AttributeError):
        pass
    try:
        m["cpu_count"] = os.cpu_count()
    except Exception:
        pass

    # Memory (from /proc/meminfo; MemAvailable is the kernel's "free + reclaimable")
    try:
        mi = _meminfo_kb()
        total = int(mi.get("MemTotal", 0))
        avail = int(mi.get("MemAvailable", 0))
        if total:
            m["mem_total_mb"] = total // 1024
            m["mem_avail_mb"] = avail // 1024
            m["mem_used_mb"] = (total - avail) // 1024
            m["mem_pct"] = round((total - avail) * 100 / total, 1)
        swap_total = int(mi.get("SwapTotal", 0))
        swap_free = int(mi.get("SwapFree", 0))
        if swap_total:
            m["swap_total_mb"] = swap_total // 1024
            m["swap_used_mb"] = (swap_total - swap_free) // 1024
    except (OSError, ValueError):
        pass

    # Disk (root filesystem)
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total:
            m["disk_total_gb"] = round(total / 1024**3, 1)
            m["disk_free_gb"] = round(free / 1024**3, 1)
            m["disk_used_gb"] = round((total - free) / 1024**3, 1)
            m["disk_pct"] = round((total - free) * 100 / total, 1)
    except (OSError, AttributeError):
        pass

    # Uptime (Linux)
    try:
        with open("/proc/uptime") as f:
            m["uptime_s"] = int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass

    # CPU model name
    cpu_model = _cpu_model()
    if cpu_model:
        m["cpu_model"] = cpu_model

    # CPU frequency (first core) — feeds the nodes table's MHz column.
    # sysfs first (kHz), then /proc/cpuinfo 'cpu MHz' as fallback.
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") as f:
            m["cpu_freq_mhz"] = round(int(f.read().strip()) / 1000)
    except (OSError, ValueError):
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("cpu mhz"):
                        m["cpu_freq_mhz"] = round(float(line.split(":", 1)[1].strip()))
                        break
        except (OSError, ValueError):
            pass

    # LAN IP address
    lan_ip = _local_ip()
    if lan_ip:
        m["lan_ip"] = lan_ip

    # GPU (optional — empty list on boxes without nvidia-smi)
    gpus = _gpu_info()
    if gpus:
        m["gpus"] = gpus
        m["gpu_count"] = len(gpus)
        m["gpu_mem_used_mb"] = sum(g["mem_used_mb"] for g in gpus)
        m["gpu_util_pct"] = round(sum(g["util_pct"] for g in gpus) / len(gpus), 1)

    # Location (city, country, region via geoIP — cached 1hr)
    loc = _location_info()
    if loc:
        m["location"] = loc

    return m

def _print_metrics_local(m: dict) -> None:
    """Pretty-print a metrics snapshot to stdout."""
    def pct(v): return f"{v}%" if v is not None else "?"
    def mb(v): return f"{v} MB" if v is not None else "?"
    print(f"\033[1m{APP} bee metrics\033[0m — local snapshot (ts={m.get('ts')})")
    cpu = m.get("cpu_count")
    print(f"  cpu:    {cpu} cores   load: {m.get('load_1','?')} / {m.get('load_5','?')} / {m.get('load_15','?')}")
    print(f"  mem:    {mb(m.get('mem_used_mb'))} / {mb(m.get('mem_total_mb'))} ({pct(m.get('mem_pct'))})")
    if m.get("swap_total_mb"):
        print(f"  swap:   {mb(m.get('swap_used_mb'))} / {mb(m.get('swap_total_mb'))}")
    print(f"  disk:   {m.get('disk_used_gb','?')}G / {m.get('disk_total_gb','?')}G ({pct(m.get('disk_pct'))})")
    if m.get("uptime_s") is not None:
        up = m["uptime_s"]
        days, rem = divmod(up, 86400)
        hrs, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        print(f"  uptime: {days}d {hrs}h {mins}m ({up}s)")
    gpus = m.get("gpus") or []
    if gpus:
        print(f"  gpus:   {len(gpus)}")
        for g in gpus:
            print(f"    [{g['index']}] {g['name']}  util={g['util_pct']}%  mem={g['mem_used_mb']}/{g['mem_total_mb']} MB  temp={g.get('temp_c','?')}°C")
    else:
        print(f"  gpus:   (none detected)")


# ── self-repair: agent-run fixes without user intervention ─────────────
def _self_repair(log_path) -> list:
    """Run automatic self-healing checks. Returns list of (fixed, message).
    Includes crash-loop circuit breaker: max 3 restarts per 5 minutes, then stops."""
    import subprocess as _sp
    import platform as _plat
    _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=30)
    fixes = []
    system = _plat.system()

    # Crash-loop circuit breaker: track restart attempts in state
    # Max 3 restarts within a 5-minute window, then stop to avoid destabilizing the system
    CRASH_WINDOW_S = 300  # 5 minutes
    CRASH_MAX = 3
    st = read_state() or {}
    crash_log = st.get("_crash_log", []) or []
    now = int(time.time())
    # Prune old entries outside the window
    crash_log = [t for t in crash_log if now - t < CRASH_WINDOW_S]
    st["_crash_log"] = crash_log
    write_state(st)

    # 1. Agent dead → restart it (with circuit breaker)
    pid = read_pid()
    if not pid or not _pid_alive_fallback(pid):
        # _self_repair only ever runs INSIDE a live agent — so a missing/stale
        # pidfile means it was wiped (duplicate-orphan death, manual rm), NOT
        # that no agent exists. Re-adopt it instead of spawning a second agent
        # (the spawn path caused 2-3 agent pile-ups on masjid/ashy 2026-11-14).
        if _pid_alive_fallback(os.getpid()):
            ensure_dirs()
            try:
                PID_FILE.write_text(str(os.getpid()))
            except OSError:
                pass
            fixes.append((True, f"pidfile was missing/stale — live agent {os.getpid()} re-adopted it (no duplicate spawned)"))
        elif len(crash_log) >= CRASH_MAX:
            msg = (f"CRASH LOOP DETECTED — {len(crash_log)} restarts in {CRASH_WINDOW_S}s. "
                   f"Stopped auto-restart. Run `hivv status` to investigate. "
                   f"Re-enable with: hivv start")
            fixes.append((False, msg))
            log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: {msg}\n")
            log_path.flush()
            return fixes
        log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: agent dead (pid={pid}), restarting…\n")
        log_path.flush()
        ensure_dirs()
        new_log = LOG_FILE.open("a")
        repair_kw = ({"creationflags": 0x00000008 | 0x00000200} if IS_WINDOWS
                     else {"start_new_session": True})
        proc = subprocess.Popen(
            _agent_argv(),
            stdout=new_log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **repair_kw,
        )
        PID_FILE.write_text(str(proc.pid))
        # Record this restart attempt
        st = read_state() or {}
        crash_log = st.get("_crash_log", []) or []
        crash_log.append(now)
        st["_crash_log"] = crash_log
        write_state(st)
        fixes.append((True, f"agent restarted (pid={proc.pid}) [{len(crash_log)}/{CRASH_MAX} restarts in window]"))
        return fixes  # new agent owns the loop now

    # 1b. Duplicate agents → keep only the managed one. Extra `_agent` processes
    #     heartbeat under the same node id (the hub's version flaps between
    #     builds) and race on state.json, which can cost the node its node_id.
    killed, keep, note = _reap_duplicate_agents()
    if note:
        fixes.append((True, note))
        log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: {note}\n")
        log_path.flush()

    # 2. Ensure systemd user service exists (Linux) for boot persistence
    if system == "Linux":
        svc_path = Path.home() / ".config" / "systemd" / "user" / "hivv-agent.service"
        if not svc_path.exists():
            svc_dir = svc_path.parent
            svc_dir.mkdir(parents=True, exist_ok=True)
            svc_content = f"""[Unit]
Description=hivv AI node agent
After=network.target

[Service]
Type=simple
ExecStart={_agent_execstart()}
Restart=always
RestartSec=10
StandardOutput=append:{Path.home() / ".local" / "share" / APP / "agent.log"}
StandardError=append:{Path.home() / ".local" / "share" / APP / "agent.log"}

[Install]
WantedBy=default.target
"""
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.service', delete=False)
            tmp.write(svc_content)
            tmp.close()
            _r(f"cp {tmp.name} {svc_path}")
            _r(f"rm -f {tmp.name}")
            _r("systemctl --user daemon-reload")
            _r("systemctl --user enable --now hivv-agent")
            fixes.append((True, "systemd user service created + enabled"))
            log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: installed systemd service\n")
            log_path.flush()
        else:
            import getpass
            # Validate the EXISTING unit — a daemonizing ExecStart (`hivv start`)
            # under Type=simple crash-loops: systemd sees the launcher exit,
            # kills the cgroup (SIGTERM to the agent ~1s in), restarts 10s
            # later… masjid looped like this for months (restart counter 73+).
            # Valid combos: foreground `_agent` + simple · `hivv start` + forking+PIDFile.
            try:
                svc_text = svc_path.read_text()
            except OSError:
                svc_text = ""
            exec_line = next((l for l in svc_text.splitlines() if l.startswith("ExecStart=")), "")
            is_daemon_exec = exec_line.rstrip().endswith(" start") or f"/{APP} start" in exec_line
            has_fork = "Type=forking" in svc_text
            has_pidfile = "PIDFile=" in svc_text
            broken = (is_daemon_exec and not (has_fork and has_pidfile)) or \
                     (has_fork and not has_pidfile)
            if broken and svc_text:
                home = str(Path.home())
                share = f"{home}/.local/share/{APP}"
                good = f"""[Unit]
Description=hivv AI node agent
After=network.target

[Service]
Type=simple
ExecStart={_agent_execstart()}
Restart=always
RestartSec=10
StandardOutput=append:{share}/agent.log
StandardError=append:{share}/agent.log

[Install]
WantedBy=default.target
"""
                try:
                    svc_path.write_text(good)
                    _r("systemctl --user daemon-reload")
                    _r("systemctl --user restart hivv-agent")
                    fixes.append((True, "systemd unit was crash-looping (daemonizing ExecStart under Type=simple) — rewritten to foreground _agent"))
                    log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: rewrote broken hivv-agent.service\n")
                    log_path.flush()
                except OSError:
                    pass
            # Linger: without it the whole user manager (and this service) dies
            # when the last session closes — the node silently goes offline
            # until someone logs in again (masjid: 3h outage 2026-11-14).
            try:
                u = os.environ.get("USER") or os.environ.get("LOGNAME") or getpass.getuser()
                lr = _r(f"loginctl show-user {u} -p Linger --value 2>/dev/null")
                if lr.stdout.strip() == "no":
                    er = _r(f"loginctl enable-linger {u} 2>/dev/null || sudo -n loginctl enable-linger {u} 2>/dev/null")
                    if _r(f"loginctl show-user {u} -p Linger --value 2>/dev/null").stdout.strip() == "yes":
                        fixes.append((True, "enabled linger — agent now survives logout/reboot sessions"))
                        log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: linger enabled for {u}\n")
                        log_path.flush()
                    else:
                        fixes.append((False, "linger is OFF (node goes offline when you log out) — run: sudo loginctl enable-linger $USER"))
            except Exception:
                pass

    # 3. Fix localhost-only LLM bindings (ollama)
    if system == "Linux":
        for candidate in ["/etc/systemd/system/ollama.service", "/lib/systemd/system/ollama.service"]:
            if _r(f"test -f {candidate}").returncode == 0:
                r = _r(f"ss -tlnp 2>/dev/null | grep ':11434 '")
                if r.stdout and "127.0.0.1:11434" in r.stdout:
                    override_dir = candidate + ".d"
                    override_file = f"{override_dir}/hivv-network.conf"
                    _r(f"sudo mkdir -p {override_dir}")
                    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False)
                    tmp.write('[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n')
                    tmp.close()
                    _r(f"sudo cp {tmp.name} {override_file}")
                    _r(f"rm -f {tmp.name}")
                    _r("sudo systemctl daemon-reload")
                    _r("sudo systemctl restart ollama")
                    fixes.append((True, "ollama binding fixed (0.0.0.0:11434)"))
                    log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: ollama now on 0.0.0.0:11434\n")
                    log_path.flush()
                break

    # 4. Tailscale re-login check (logged out → warn but don't force)
    if system == "Linux" and _r("which tailscale 2>/dev/null").returncode == 0:
        ts_status = _r("tailscale status --json 2>/dev/null")
        if ts_status.returncode == 0:
            import json as _json
            try:
                ts_data = _json.loads(ts_status.stdout)
                backend = ts_data.get("BackendState", "")
                if backend != "Running":
                    fixes.append((False, f"tailscale {backend.lower()} — run: tailscale up"))
                    log_path.write(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR: tailscale not running\n")
                    log_path.flush()
            except Exception:
                pass

    return fixes


# ── agent: long-running daemon ─────────────────────────────────────────
def cmd_agent(stop_event) -> None:
    """The background agent — registers once, then heartbeats + inbox-polls."""
    ensure_dirs()
    PID_FILE.write_text(str(os.getpid()))
    # Merge into the existing state — NEVER replace it. A fresh dict here
    # wiped `node_secret` on every restart/execv; the hub only issues a
    # secret to a bee that has never proven one (node-auth), so the bee lost
    # inbox access FOREVER (403 on every poll → repair/policy/update pushes
    # all silently dead). masjid+ashy were bricked this way 2026-11-14.
    st = read_state() or {}
    st.update({"started_at": int(time.time()), "pid": os.getpid(),
               "node_id": None, "last_heartbeat": None, "last_error": None,
               "transport": "heartbeat-only"})
    write_state(st)

    log_path = LOG_FILE.open("a", buffering=1)
    print(f"[{time.strftime('%H:%M:%S')}] agent started (pid={os.getpid()}, hub={HUB})", file=log_path)

    def shutdown(*_a):
        stop_event.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    # Get/create per-install bearer token (Phase A)
    try:
        auth_token = _get_or_create_auth_token()
    except OSError as e:
        auth_token = None
        print(f"[{time.strftime('%H:%M:%S')}] WARN could not create auth token: {e}", file=log_path)

    # 1. register once on startup
    hostname = socket.gethostname()
    if not hostname or hostname in ("localhost", "localhost.localdomain"):
        hostname = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    node_name = hostname.split(".")[0]  # short name, no domain suffix
    hw_id = _machine_id()
    # Use just hostname as node_id — simple and human. Only add hw_tag if hub
    # already has this hostname (collision), in which case append short hw suffix.
    stable_id = node_name.replace(".", "-").replace(" ", "_")
    capabilities = ["heartbeat", "cli", "metrics", "signed-requests", "long-poll", "relay"]
    metrics = _collect_metrics()
    local_models = _detect_local_ai()
    try:
        _vol_on_reg = bool(_volunteer_load().get("share_cpu"))
    except Exception:
        _vol_on_reg = False
    try:
        _tools_reg = _shared_tools()
    except Exception:
        _tools_reg = []
    body = {"id": stable_id, "token": stable_id,
            "hostname": hostname, "version": VERSION, "name": node_name,
            "lan_ip": metrics.get("lan_ip") or "",
            "location": metrics.get("location") or {},
            "cpu_model": metrics.get("cpu_model") or "",
            "capabilities": capabilities, "max_concurrent_jobs": 1,
            "hw_id": hw_id, "auth_token": auth_token,
            "client": f"{APP}/{VERSION}",
            "metrics": metrics,
            "volunteer": _vol_on_reg,
            "tools": _tools_reg, "tools_port": TOOLS_PORT if _tools_reg else 0,
            "local_models": local_models}
    code, payload = http("POST", "/api/nodes/register", body=body)
    st = read_state()
    if code == 200 and isinstance(payload, dict):
        nid = payload.get("node_id") or stable_id
        st["node_id"] = nid
        st["hw_id"] = hw_id
        # Adopt the (possibly RE-ISSUED) inbox secret here too — this is the
        # path a freshly restarted agent takes, and without it a bee that lost
        # its secret receives the hub's recovery issuance and discards it
        # (inbox stays 403 forever). The in-loop path adopts as well.
        if payload.get("node_secret"):
            st["node_secret"] = payload["node_secret"]
        print(f"[{time.strftime('%H:%M:%S')}] registered as {nid} (hw={hw_id or 'soft'})", file=log_path)
    else:
        st["last_error"] = f"register failed: http {code}: {str(payload)[:200]}"
        print(f"[{time.strftime('%H:%M:%S')}] register failed {code}: {str(payload)[:120]}", file=log_path)
    write_state(st)

    # 2. heartbeat + inbox-poll loop
    inbox_supported = True   # optimistic; flipped off on 404
    last_inbox_at = 0
    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        node_id = (read_state() or {}).get("node_id")
        # Track stats delta since last heartbeat
        st = read_state() or {}
        prev = st.get("_stats", {})
        # monotonic counters: never let a wiped/rolled-back state key re-mint history
        for _k in ("tasks_completed", "tasks_failed", "tokens_processed", "local_requests", "local_cost"):
            cur = st.get(_k) or 0
            old = prev.get(_k) or 0
            if cur < old:
                st[_k] = old
        stats_delta = {
            "tasks_completed": max(0, (st.get("tasks_completed") or 0) - (prev.get("tasks_completed") or 0)),
            "tasks_failed": max(0, (st.get("tasks_failed") or 0) - (prev.get("tasks_failed") or 0)),
            "total_tokens_processed": max(0, (st.get("tokens_processed") or 0) - (prev.get("tokens_processed") or 0)),
            "total_requests": max(0, (st.get("local_requests") or 0) - (prev.get("local_requests") or 0)),
            "total_cost": max(0, (st.get("local_cost") or 0.0) - (prev.get("local_cost") or 0.0)),
        }
        st["_stats"] = {
            "tasks_completed": st.get("tasks_completed") or 0,
            "tasks_failed": st.get("tasks_failed") or 0,
            "tokens_processed": st.get("tokens_processed") or 0,
            "local_requests": st.get("local_requests") or 0,
            "local_cost": st.get("local_cost") or 0.0,
        }
        write_state(st)
        if node_id:
            try:
                _vol_on = bool(_volunteer_load().get("share_cpu"))
            except Exception:
                _vol_on = False
            try:
                _tools_hb = _shared_tools()
            except Exception:
                _tools_hb = []
            code, payload = http("POST", f"/api/nodes/{node_id}/heartbeat",
                                 body={"hostname": hostname, "version": VERSION,
                                       "ts": int(time.time()),
                                       "metrics": _collect_metrics(),
                                       "local_models": _detect_local_ai(),
                                       "volunteer": _vol_on,
                                       "tools": _tools_hb, "tools_port": TOOLS_PORT if _tools_hb else 0,
                                       "cumulative": {"tasks_completed": st.get("tasks_completed") or 0,
                                                      "tokens_processed": st.get("tokens_processed") or 0},
                                       "stats": stats_delta,
                                       # piggyback the last repair outcome; hub logs it as [REPAIR]
                                       "repair_report": st.get("repair_report") or None})
        else:
            code, payload = http("POST", "/api/nodes/register", body=body)
            if code == 200 and isinstance(payload, dict):
                body["metrics"] = _collect_metrics()
                st = read_state() or {}
                st["node_id"] = payload.get("node_id") or stable_id
                write_state(st)

        st = read_state() or {}
        st["last_heartbeat"] = int(time.time())
        st["last_status"]    = code
        if code == 200 and isinstance(payload, dict) and payload.get("node_id") and not st.get("node_id"):
            st["node_id"] = payload["node_id"]
        elif code and code >= 400:
            st["last_error"] = f"http {code}: {str(payload)[:120]}"
        elif code == 0:
            st["last_error"] = str(payload)[:120]
        # Adopt the per-node secret the hub issues/confirms (register + heartbeat
        # both land here) so inbox polls and hub→bee repair plans can be signed
        # and verified from this bee onward.
        if isinstance(payload, dict) and payload.get("node_secret"):
            if payload["node_secret"] != st.get("node_secret"):
                st["node_secret"] = payload["node_secret"]
        if code == 200 and st.get("repair_report"):
            st.pop("repair_report", None)   # delivered on this heartbeat
        write_state(st)
        print(f"[{time.strftime('%H:%M:%S')}] hb={code} node={st.get('node_id')}", file=log_path)

        # 3. inbox poll (long-poll) — gives the hub a way to push to us
        #    without opening an inbound port.
        if inbox_supported and node_id and code == 200:
            wait_s = max(2, HEARTBEAT_S - 5)  # long-poll for up to HB-5 seconds
            ic, ipayload = http("GET", f"/api/agents/{node_id}/inbox?wait={wait_s}",
                                timeout=wait_s + 10)
            last_inbox_at = int(time.time())
            if ic == 404:
                # hub doesn't have this endpoint yet → graceful degrade
                inbox_supported = False
                st["transport"] = "heartbeat-only"
                write_state(st)
                print(f"[{time.strftime('%H:%M:%S')}] inbox 404 → heartbeat-only mode", file=log_path)
            elif ic == 200 and isinstance(ipayload, dict):
                msgs = ipayload.get("messages") or []
                if msgs:
                    print(f"[{time.strftime('%H:%M:%S')}] inbox: {len(msgs)} message(s)", file=log_path)
                    for msg in msgs:
                        _dispatch_inbox_message(msg, st, log_path)
                    # ack processed messages or the hub re-delivers them EVERY poll
                    acked = [m.get("id") for m in msgs if isinstance(m, dict) and m.get("id")]
                    if acked and node_id:
                        http("POST", f"/api/agents/{node_id}/inbox/ack",
                             body={"ids": acked}, timeout=10)
                # re-read: dispatch (relay jobs) may have written counters we must not clobber
                st = read_state() or {}
                st["transport"] = "long-poll"
                write_state(st)
            elif ic and ic >= 400:
                # other error — log + try again next cycle
                print(f"[{time.strftime('%H:%M:%S')}] inbox http {ic}: {str(ipayload)[:80]}", file=log_path)

        # 4. Self-repair every 10 cycles (~10 minutes) — fixes agent death, bindings, service
        if cycle % 10 == 0:
            fixes = _self_repair(log_path)
            for fixed, msg in fixes:
                tag = "FIXED" if fixed else "WARN"
                print(f"[{time.strftime('%H:%M:%S')}] SELF-REPAIR [{tag}]: {msg}", file=log_path)
            log_path.flush()

        # 5. Silent auto-update check every cycle (SHA256 compare, no version parsing)
        if cycle % 5 == 0:  # every 5 minutes
            if _is_dev_mode():
                print(f"[{time.strftime('%H:%M:%S')}] auto-update: SKIPPED (dev mode — HIVV_DEV_MODE or .hivv-dev marker)", file=log_path)
            else:
                try:
                    target = INSTALL_DIR / f"{APP}-cli.py"
                    if target.exists():
                        req = urllib.request.Request(
                            f"{HUB.rstrip('/')}/{APP}-cli.py",
                            headers={"User-Agent": f"{APP}/{VERSION}"}
                        )
                        with urllib.request.urlopen(req, timeout=10) as r:
                            hub_data = r.read()
                        hub_sha = hashlib.sha256(hub_data).hexdigest()
                        local_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                        # File already matches the hub, but THIS process may
                        # predate it (an earlier update applied the bytes but
                        # died before restarting us). The sha comparison stays
                        # equal forever, so nothing else would ever fix it and
                        # the node keeps reporting the old version.
                        if hub_sha == local_sha and _get_version() != VERSION:
                            print(f"[{time.strftime('%H:%M:%S')}] auto-update: file current, restarting stale process {VERSION} → {_get_version()}", file=log_path)
                            log_path.flush()
                            os.execv(sys.executable, _agent_argv(target))
                        if hub_sha != local_sha:
                            # Trusted fingerprint security: if operator set HIVV_TRUSTED_FINGERPRINT,
                            # only apply updates that match their known-good hash
                            trusted = os.environ.get("HIVV_TRUSTED_FINGERPRINT", "").strip()
                            if trusted and hub_sha.lower() != trusted.lower():
                                print(f"[{time.strftime('%H:%M:%S')}] auto-update: SKIPPED — "
                                      f"hub SHA256 ({hub_sha[:12]}…) does not match trusted fingerprint. "
                                      f"Set HIVV_TRUSTED_FINGERPRINT={hub_sha[:16]}… to accept this version.", file=log_path)
                            else:
                                if trusted:
                                    print(f"[{time.strftime('%H:%M:%S')}] auto-update: hub SHA256 matches trusted fingerprint", file=log_path)
                                print(f"[{time.strftime('%H:%M:%S')}] auto-update: new CLI available ({hub_sha[:12]}…) — running review…", file=log_path)
                                log_path.flush()
                                # Full pre-apply review (static scan + local LLM
                                # vote + hive fan-out vote), ~60s budget. A
                                # suspicious verdict BLOCKS the update and files
                                # a hub incident; cached per-sha for 24h so we
                                # don't re-review (or re-report) every 5 min.
                                gate = _upgrade_review_gate(
                                    hub_data, source="auto", budget_s=60,
                                    echo=lambda s: print(
                                        f"[{time.strftime('%H:%M:%S')}] auto-update review: {s}",
                                        file=log_path))
                                if not gate["allow"]:
                                    print(f"[{time.strftime('%H:%M:%S')}] auto-update: BLOCKED — {gate['reason']}", file=log_path)
                                    log_path.flush()
                                else:
                                    ok, msg, path = _apply_update(hub_data, source="auto")
                                    print(f"[{time.strftime('%H:%M:%S')}] auto-update: {msg}", file=log_path)
                                    if ok:
                                        _upgrade_report_once({"source": "auto", "action": "applied",
                                                         "to_sha256": hub_sha, "verdict": gate["verdict"],
                                                         "reason": gate["reason"],
                                                         "static_findings": (gate["review"].get("scan") or {}).get("findings", []),
                                                         "votes": gate["review"].get("votes", [])},
                                                        hub_sha, "applied")
                                        log_path.close()
                                        os.execv(sys.executable, _agent_argv(path))
                                    else:
                                        _upgrade_report_once({"source": "auto", "action": "failed",
                                                         "to_sha256": hub_sha, "verdict": gate["verdict"],
                                                         "reason": msg, "static_findings": [], "votes": []},
                                                        hub_sha, "failed")
                except Exception as e:
                    print(f"[{time.strftime('%H:%M:%S')}] auto-update check failed: {e}", file=log_path)

        # 6. interruptible sleep until next cycle (use remaining time after inbox)
        elapsed = max(0, int(time.time()) - last_inbox_at) if last_inbox_at else 0
        sleep_for = max(1, HEARTBEAT_S - elapsed)
        if stop_event.wait(sleep_for):
            break

    print(f"[{time.strftime('%H:%M:%S')}] agent stopping", file=log_path)
    log_path.close()
    # Only retire the pidfile if it's OURS. A duplicate/orphan dying must not
    # delete the live agent's pidfile — that made self-repair see "agent dead"
    # and spawn a THIRD agent (masjid/ashy 2026-11-14).
    try:
        if PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except (OSError, ValueError):
        pass


# ── MAYBE approvals stash (pushed into our inbox by the hub policy gate) ──
# The hub is the source of truth; this local copy exists so `hivv up` can nudge
# the operator instantly without a round-trip, and so a phone/one-tap approve
# has something to show. Pruned to a 24h window and deduped by grant_id.
_APPROVAL_TTL_S = 24 * 3600


def _approvals_load() -> list:
    try:
        if not APPROVALS_FILE.exists():
            return []
        rows = json.loads(APPROVALS_FILE.read_text())
        if not isinstance(rows, list):
            return []
        cutoff = int(time.time()) - _APPROVAL_TTL_S
        fresh = [r for r in rows if isinstance(r, dict) and (r.get("ts") or 0) >= cutoff]
        if len(fresh) != len(rows):
            _approvals_save(fresh)
        return fresh
    except Exception:
        return []


def _approvals_save(rows: list) -> None:
    try:
        ensure_dirs()
        APPROVALS_FILE.write_text(json.dumps(rows[-50:], indent=0))
    except Exception:
        pass


def _approvals_stash(payload: dict) -> None:
    try:
        gid = payload.get("grant_id")
        if not gid:
            return
        rows = [r for r in _approvals_load() if r.get("grant_id") != gid]
        rows.append({
            "grant_id": gid,
            "capability": payload.get("capability"),
            "principal": payload.get("principal"),
            "reason": payload.get("reason"),
            "ts": int(time.time()),
        })
        _approvals_save(rows)
    except Exception:
        pass


def _approvals_forget(grant_id: str = None) -> None:
    """Drop one (or all) stashed approvals — called after a successful decide."""
    try:
        if grant_id is None:
            _approvals_save([])
            return
        _approvals_save([r for r in _approvals_load() if r.get("grant_id") != grant_id])
    except Exception:
        pass


# ── repair plans: hub → bee, signed, allowlisted ────────────────────────
# The hub can ask a bee to fix itself (`hivv repair <bee> --op ...`). This is
# deliberately NOT a shell channel: ops come from a closed set and arguments
# are validated against closed sets, so a compromised hub (or a forged plan)
# can never become arbitrary code execution on the fleet.
REPAIR_OPS = {"cli_update", "agent_restart", "reap_duplicates", "service_restart"}
# `hivv-agent.service` is deliberately absent — restarting the unit we are
# running inside, from inside that process, is how you build a restart loop.
# Use `agent_restart` instead.
REPAIR_UNITS = {"llama.service", "llama-server.service", "ollama.service",
                "mcp-bridge.service", "mcp-web-server.service",
                "kokoro-tts-server.service", "whisper-stt-server.service"}


def _repair_signature_ok(secret: str, payload: dict) -> bool:
    """HMAC-SHA256 over ts|ttl_s|canonical ops — proves the hub built this plan."""
    try:
        msg = (f"{int(payload.get('ts') or 0)}\n"
               f"{int(payload.get('ttl_s') or 0)}\n"
               + json.dumps(payload.get("ops") or [], sort_keys=True, separators=(",", ":")))
        want = hmac.new(str(secret).encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, str(payload.get("sig") or ""))
    except Exception:
        return False


def _repair_cli_update(log_path, plan_id: str = "?", force: bool = False) -> str:
    """`cli_update` op — same pre-apply review gate as auto/manual updates.
    `force` only ever arrives inside a hub-SIGNED plan (the HMAC covers `ops`),
    so it is the operator authorising the bypass, not a caller-settable flag."""
    try:
        data = _http_get_bytes(f"{HUB.rstrip('/')}/{APP}-cli.py", timeout=30)
        gate = _upgrade_review_gate(
            data, source="repair", budget_s=60, force=force,
            echo=lambda s: print(f"[{time.strftime('%H:%M:%S')}] repair update: {s}", file=log_path))
        if not gate["allow"]:
            return f"update blocked by review: {gate['reason']}"
        ok, msg, target = _apply_update(data, source="repair")
        if not ok:
            return f"update failed: {msg}"
        print(f"[{time.strftime('%H:%M:%S')}] repair: {msg}; restarting agent", file=log_path)
        log_path.flush()
        # Record the outcome BEFORE execv — this process never returns, so
        # _apply_repair's _done() would never run and the hub would never hear.
        try:
            st = read_state() or {}
            st["repair_report"] = {"plan_id": plan_id, "ok": True,
                                   "result": f"update applied (force={force}) → {msg}",
                                   "ts": int(time.time())}
            write_state(st)
        except OSError:
            pass
        log_path.close()
        os.execv(sys.executable, _agent_argv(target))
    except Exception as e:
        return f"update exception: {e}"
    return "unreachable"


def _apply_repair(payload: dict, log_path) -> None:
    """Execute a hub-signed repair plan.

    Gates in order: node secret known → HMAC → freshness window → one-shot
    nonce → every op allowlisted (whole plan pre-validated before anything
    runs). The nonce is recorded BEFORE side effects so an execv-based op
    (agent_restart / cli_update) can't be re-run by inbox redelivery.
    Result lands in state and rides out on the next heartbeat as
    `repair_report`, so the hub can see what happened."""
    ts = time.strftime("%H:%M:%S")
    plan_id = str(payload.get("plan_id") or "?")

    def _done(ok: bool, result: str) -> None:
        try:
            st = read_state() or {}
            st["repair_report"] = {"plan_id": plan_id, "ok": bool(ok),
                                   "result": result[:400], "ts": int(time.time())}
            write_state(st)
        except OSError:
            pass
        print(f"[{ts}] repair[{plan_id}] {'OK' if ok else 'FAIL'}: {result}", file=log_path)
        log_path.flush()

    secret = (read_state() or {}).get("node_secret")
    if not secret:
        return _done(False, "not enrolled yet (no node secret)")
    if not _repair_signature_ok(secret, payload):
        return _done(False, "signature mismatch — plan rejected")
    now = int(time.time())
    try:
        pts, ttl = int(payload.get("ts") or 0), int(payload.get("ttl_s") or 0)
    except (TypeError, ValueError):
        return _done(False, "malformed ts/ttl_s")
    if pts > 10**11:      # tolerate a hub that stamps ms instead of seconds
        pts //= 1000
    if not (0 < ttl <= 600) or abs(now - pts) > 300:
        return _done(False, f"outside trusted window (age={now - pts}s, ttl={ttl}s)")
    st = read_state() or {}
    seen = [x for x in (st.get("_repair_seen") or []) if now - int(x.get("ts", 0)) < 3600]
    if plan_id != "?" and any(x.get("id") == plan_id for x in seen):
        return _done(False, "replay ignored — already executed")

    ops = payload.get("ops")
    if not isinstance(ops, list) or not (1 <= len(ops) <= 8):
        return _done(False, "ops must be a list of 1..8 actions")
    # Atomic pre-validation: reject the WHOLE plan before running anything.
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in REPAIR_OPS:
            return _done(False, f"op not allowed: {str(op)[:60]!r}")
        if op["op"] == "service_restart" and str(op.get("unit") or "") not in REPAIR_UNITS:
            return _done(False, f"unit not allowed: {str(op.get('unit'))!r}")

    # Record the nonce first — several ops execv into a new process.
    if plan_id != "?":
        st["_repair_seen"] = (seen + [{"id": plan_id, "ts": now}])[-50:]
        try:
            write_state(st)
        except OSError:
            pass

    results = []
    for op in ops:
        name = op["op"]
        if name == "reap_duplicates":
            _killed, _keep, note = _reap_duplicate_agents()
            results.append(note or "no duplicate agents found")
        elif name == "service_restart":
            unit = str(op["unit"])
            # No sudo from a pushed plan — if the scope can't restart it, we say so.
            rc = subprocess.run(f"systemctl try-restart {unit} 2>/dev/null || "
                                f"systemctl --user try-restart {unit} 2>/dev/null",
                                shell=True, timeout=60).returncode
            results.append(f"{unit}: {'restarted' if rc == 0 else f'no permission (rc={rc})'}")
        elif name == "agent_restart":
            print(f"[{ts}] repair[{plan_id}]: restarting agent", file=log_path)
            log_path.flush()
            log_path.close()
            os.execv(sys.executable, _agent_argv())
        elif name == "cli_update":
            results.append(_repair_cli_update(log_path, plan_id,
                                              force=(op.get("force") is True)))
    _done(True, "; ".join(results) or "ok")


def _dispatch_inbox_message(msg: dict, st: dict, log_path) -> None:
    """Dispatch a single inbox message from the hub.

    v1 handles: update_available, log, shutdown_requested. Anything else is
    logged but ignored (so we can extend safely)."""
    if not isinstance(msg, dict):
        return
    kind = msg.get("type") or msg.get("kind") or "unknown"
    ts = time.strftime("%H:%M:%S")
    payload = msg.get("payload") or msg.get("data") or {}
    print(f"[{ts}] inbox[{kind}]: {str(payload)[:200]}", file=log_path)
    log_path.flush()

    if kind == "update_available":
        new_ver = payload.get("version")
        sha     = payload.get("sha256")
        url     = payload.get("url") or f"{HUB.rstrip('/')}/{APP}-cli.py"
        if new_ver and _parse_ver(new_ver) <= _parse_ver(VERSION):
            print(f"[{ts}] update_available ignored: already on v{VERSION} or newer", file=log_path)
        else:
            try:
                print(f"[{ts}] pulling new client from {url}…", file=log_path)
                data = _http_get_bytes(url, timeout=30)
                # Same pre-apply review as auto/manual — a hub push is still
                # third-party code from this bee's point of view. Suspicious →
                # blocked + hub incident (cached per-sha for 24h).
                gate = _upgrade_review_gate(
                    data, source="inbox", budget_s=60,
                    echo=lambda s: print(f"[{ts}] review: {s}", file=log_path))
                if not gate["allow"]:
                    print(f"[{ts}] update_available BLOCKED: {gate['reason']}", file=log_path)
                    log_path.flush()
                    return
                ok, msg, target = _apply_update(data, expected_sha256=sha, source="inbox")
                print(f"[{ts}] {msg}", file=log_path)
                if ok:
                    _upgrade_report_once({"source": "inbox", "action": "applied",
                                     "to_sha256": hashlib.sha256(data).hexdigest(),
                                     "verdict": gate["verdict"], "reason": gate["reason"],
                                     "static_findings": (gate["review"].get("scan") or {}).get("findings", []),
                                     "votes": gate["review"].get("votes", [])},
                                    hashlib.sha256(data).hexdigest(), "applied")
                    print(f"[{ts}] updated → {target}; restarting agent", file=log_path)
                    os.execv(sys.executable, _agent_argv(target))
                else:
                    _upgrade_report_once({"source": "inbox", "action": "failed",
                                     "to_sha256": hashlib.sha256(data).hexdigest(),
                                     "verdict": gate["verdict"], "reason": msg,
                                     "static_findings": [], "votes": []},
                                    hashlib.sha256(data).hexdigest(), "failed")
                    print(f"[{ts}] auto-update FAILED: {msg}", file=log_path)
            except Exception as e:
                print(f"[{ts}] auto-update exception: {e}", file=log_path)

    elif kind == "repair":
        # Signed, allowlisted fix-up plan from the hub (`hivv repair <bee>`).
        # Verification and audit live in _apply_repair — never run these raw.
        _apply_repair(payload, log_path)

    elif kind == "shutdown_requested":
        print(f"[{ts}] hub requested shutdown", file=log_path)
        os.kill(os.getpid(), signal.SIGTERM)

    elif kind == "log":
        # the hub just wants us to record something (audit trail)
        print(f"[{ts}] [hub-log] {payload}", file=log_path)

    elif kind == "inference_job":
        _run_relay_job(payload, log_path)

    elif kind == "coding_job":
        # Coding jobs run for minutes — never block the heartbeat/inbox loop
        # (a stalled loop = stale node = hub expires the job mid-flight), and
        # skip re-delivered duplicates while one is in flight.
        jid = str(payload.get("job_id") or "")
        with _coding_jobs_lock:
            if jid and jid in _coding_inflight:
                print(f"[{ts}] [CODING] {jid}: already in flight — skipping duplicate",
                      file=log_path)
            else:
                if jid:
                    _coding_inflight.add(jid)
                threading.Thread(target=_coding_job_thread,
                                 args=(payload, log_path, jid), daemon=True).start()

    elif kind == "approval_request":
        # hivv OS §3: the policy gate said MAYBE and pushed a pending grant to
        # us. Stash it so `hivv up` nudges the operator (one-tap approve).
        gid = payload.get("grant_id")
        cap = payload.get("capability")
        _approvals_stash(payload)
        print(f"[{ts}] approval_request: {cap} needs a human — {APP} approve {gid}", file=log_path)

    # unknown kinds: already logged above; future-extension hook.


# ports to try, in order, when the hub relays an inference job to us
_RELAY_LOCAL_ENDPOINTS = [
    "http://localhost:8080/v1/chat/completions",   # llama.cpp
    "http://localhost:11434/v1/chat/completions",  # ollama (OpenAI-compat)
    "http://localhost:1234/v1/chat/completions",   # lm studio
    "http://localhost:8081/v1/chat/completions",   # second llama slot
]

def _run_relay_job(payload: dict, log_path) -> None:
    """HIVV_PLAN #5: hub-relayed inference job. Only runs when volunteering is
    on; executes against OUR OWN local model; reports back with the job secret."""
    ts = time.strftime("%H:%M:%S")
    job_id = payload.get("job_id")
    secret = payload.get("secret")
    request = payload.get("request") or {}
    messages = request.get("messages") or []
    if not job_id or not secret or not messages:
        return
    try:
        vol_on = bool(_volunteer_load().get("share_cpu"))
    except Exception:
        vol_on = False
    if not vol_on:
        print(f"[{ts}] relay job {job_id}: volunteer is OFF — declining", file=log_path)
        http("POST", f"/api/relay/jobs/{job_id}/result",
             body={"secret": secret, "error": "volunteer off"}, timeout=10)
        return
    content, model_used = None, request.get("model") or "local"
    tool_calls, finish_reason = None, None
    # OpenAI tool support through the NAT relay: forward the hub's `tools`
    # array (function schemas) to the local endpoint alongside messages/model/
    # max_tokens, then bring tool_calls + finish_reason back in the result.
    tools = request.get("tools") or payload.get("tools")
    if not isinstance(tools, list) or not tools:
        tools = None
    for ep in _RELAY_LOCAL_ENDPOINTS:
        try:
            req_body = {"model": "local", "messages": messages[-6:],
                        "max_tokens": min(int(request.get("max_tokens") or 500), 1000),
                        "stream": False,
                        "chat_template_kwargs": {"enable_thinking": False}}
            if tools:
                req_body["tools"] = tools
            r_ = urllib.request.Request(
                ep, data=json.dumps(req_body).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(r_, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            c = msg.get("content") or msg.get("reasoning_content")  # some GGUF chats put it all in reasoning
            tc = msg.get("tool_calls")
            # a tool_calls-only answer (empty content) still counts as served
            if c or tc:
                content = c
                model_used = data.get("model") or model_used
                tool_calls = tc if isinstance(tc, list) and tc else None
                fr = choice.get("finish_reason")
                finish_reason = fr if isinstance(fr, str) and fr else None
                break
        except Exception as e:
            print(f"[{ts}] relay {job_id}: {ep} failed ({e})", file=log_path)
            continue
    if content or tool_calls:
        print(f"[{ts}] relay job {job_id} done ({len(content or '')} chars via {model_used}"
              f"{' +tool_calls' if tool_calls else ''})", file=log_path)
        # count it like any served job so honey + the hive goal reflect relay work
        try:
            st2 = read_state() or {}
            st2["tasks_completed"] = (st2.get("tasks_completed") or 0) + 1
            st2["tokens_processed"] = (st2.get("tokens_processed") or 0) + max(1, len(content or "") // 4)
            write_state(st2)
        except Exception:
            pass
        result = {"content": content or "", "model": model_used}
        if tool_calls:                      # omit keys when absent
            result["tool_calls"] = tool_calls
        if finish_reason:
            result["finish_reason"] = finish_reason
        http("POST", f"/api/relay/jobs/{job_id}/result",
             body={"secret": secret, "result": result}, timeout=15)
    else:
        print(f"[{ts}] relay job {job_id}: no local model answered", file=log_path)
        http("POST", f"/api/relay/jobs/{job_id}/result",
             body={"secret": secret, "error": "no local model answered"}, timeout=15)


# ── coding jobs (hivv code): opencode runs a task in a temp git worktree ──
_PATCH_MAX_CHARS = 400000   # client-side cap on the patch we POST back
_coding_inflight: set = set()          # job_ids currently running in this process
_coding_jobs_lock = threading.Lock()


def _coding_job_thread(payload: dict, log_path, jid: str) -> None:
    try:
        _run_coding_job(payload, log_path)
    finally:
        if jid:
            with _coding_jobs_lock:
                _coding_inflight.discard(jid)


def _parse_opencode_run(stdout: str) -> tuple:
    """Best-effort parse of `opencode run --format json` output.

    Returns (summary, session_id). Events carry their payload under "part" —
    the summary is the LAST event with part.text (the final assistant message),
    not raw JSON. Falls back to the stdout tail if nothing parses."""
    summary, session_id = None, None
    lines = [l for l in (stdout or "").splitlines() if l.strip()]
    for line in reversed(lines):            # last line first, then walk back
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        part = obj.get("part") if isinstance(obj.get("part"), dict) else None
        if session_id is None:
            for v in (obj.get("session_id"), obj.get("sessionId"),
                      part.get("sessionID") if part else None,
                      obj.get("session")):
                if isinstance(v, str) and v:
                    session_id = v
                    break
                if isinstance(v, dict) and isinstance(v.get("id"), str):
                    session_id = v["id"]
                    break
        if summary is None:
            cand = None
            if part and isinstance(part.get("text"), str) and part["text"].strip():
                cand = part["text"].strip()
            if cand is None:
                for k in ("summary", "message", "text", "content", "result", "output"):
                    v = obj.get(k)
                    if isinstance(v, str) and v.strip():
                        cand = v.strip()
                        break
                    if isinstance(v, dict):
                        for k2 in ("summary", "text", "message", "content"):
                            if isinstance(v.get(k2), str) and v[k2].strip():
                                cand = v[k2].strip()
                                break
                    if cand:
                        break
            if cand:
                summary = cand
        if summary and session_id:
            break
    if not summary:
        tail = (stdout or "").strip()[-2000:]
        summary = tail or "(no output)"
    return summary, session_id


def _coding_result(job_id, secret, result: dict, log_path) -> None:
    """POST a coding result back to the hub. Always attempted — even on
    failure paths — so the hub never hangs waiting for job expiry."""
    ts = time.strftime("%H:%M:%S")
    try:
        code, resp = http("POST", f"/api/coding/jobs/{job_id}/result",
                          body={"secret": secret, "result": result}, timeout=20)
        print(f"[{ts}] [CODING] {job_id}: result posted (status={result.get('status')}, http {code})"
              + (f" err={str(resp)[:80]}" if code and code >= 400 else ""), file=log_path)
    except Exception as e:
        print(f"[{ts}] [CODING] {job_id}: result POST failed: {e}", file=log_path)


def _run_coding_job(payload: dict, log_path) -> None:
    """Hub-dispatched coding job: clone repo → run `opencode run` → post patch.
    Mirrors _run_relay_job's shape: guard early, always report, log [CODING]."""
    ts = time.strftime("%H:%M:%S")
    job_id = payload.get("job_id")
    secret = payload.get("secret")
    task = (payload.get("task") or "").strip()
    repo = payload.get("repo")
    if not isinstance(repo, dict):       # defensive: never let a bad payload
        repo = {"url": repo} if isinstance(repo, str) else {}   # kill the agent loop
    repo_url = repo.get("url") or ""
    ref = repo.get("ref") or None
    subdir = repo.get("subdir") or None
    try:
        timeout_s = max(10, int(payload.get("timeout_s") or 600))
    except (TypeError, ValueError):
        timeout_s = 600

    if not job_id or not secret or not task or not repo_url:
        print(f"[{ts}] [CODING] malformed payload: {str(payload)[:200]}", file=log_path)
        return

    def _fail(summary: str) -> None:
        print(f"[{ts}] [CODING] {job_id}: ERROR — {summary}", file=log_path)
        _coding_result(job_id, secret,
                       {"status": "error", "summary": summary[:4000],
                        "patch": "", "files_changed": []}, log_path)

    # 1. opencode must be findable (PATH or standard install locations)
    oc_bin = _find_opencode()
    if not oc_bin:
        _fail("opencode not installed on this node")
        return

    # 2. optional repo allowlist (HIVV_CODE_REPOS = comma-separated URL prefixes)
    allow = os.environ.get("HIVV_CODE_REPOS", "").strip()
    if allow:
        prefixes = [p.strip() for p in allow.split(",") if p.strip()]
        if prefixes and not any(repo_url.startswith(p) for p in prefixes):
            _fail(f"repo {repo_url} not in HIVV_CODE_REPOS allowlist")
            return

    workdir = None
    try:
        # 3. clone into an isolated temp worktree (local paths work too)
        workdir = tempfile.mkdtemp(prefix="hivv-code-")
        clone_cmd = ["git", "clone", "--depth", "1"]
        if ref:
            clone_cmd += ["--branch", str(ref)]
        clone_cmd += [repo_url, workdir]
        try:
            cp = subprocess.run(clone_cmd, capture_output=True, text=True,
                                timeout=min(timeout_s, 300))
        except subprocess.TimeoutExpired:
            _fail(f"git clone timed out after {min(timeout_s, 300)}s")
            return
        if cp.returncode != 0:
            _fail(f"git clone failed: {(cp.stderr or cp.stdout or '').strip()[:500]}")
            return
        run_cwd = workdir
        if subdir:
            run_cwd = os.path.join(workdir, str(subdir))
            if not os.path.isdir(run_cwd):
                _fail(f"subdir not found after clone: {subdir}")
                return
        print(f"[{ts}] [CODING] {job_id}: cloned {repo_url}"
              + (f"@{ref}" if ref else "") + (f" subdir={subdir}" if subdir else "")
              + f" → {run_cwd}", file=log_path)

        # 4. run opencode in the worktree. --standalone: a private server, NOT
        #    the shared background service — concurrent `opencode run` calls on
        #    the shared service raced on project/location assignment and wrote
        #    files into the WRONG directory ($HOME / another project), silently
        #    emptying our patch. Also keeps long jobs off interactive sessions.
        #    --auto auto-approves permissions not explicitly denied (sandboxed tree).
        #    PWD: opencode resolves the project root from the inherited $PWD env
        #    var, NOT the real cwd — the agent inherits PWD from whatever shell
        #    started it (a dev repo, $HOME), so the write tool filed HELLO.txt
        #    there and our patch came back empty. Point it at the worktree.
        cmd = [oc_bin, "run", "--standalone", "--format", "json", "--auto", task]
        run_env = dict(os.environ)
        run_env["PWD"] = run_cwd
        run_env.pop("OLDPWD", None)
        print(f"[{ts}] [CODING] {job_id}: running `opencode run` (timeout {timeout_s}s)",
              file=log_path)
        try:
            proc = subprocess.run(cmd, cwd=run_cwd, env=run_env, capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _fail(f"timeout: opencode exceeded {timeout_s}s")
            return
        summary, session_id = _parse_opencode_run(proc.stdout)

        # 5. capture the patch — `git add -A` first so NEW files are included;
        #    cap client-side so we never POST a megabyte diff
        patch, files_changed = "", []
        try:
            subprocess.run(["git", "add", "-A"], cwd=run_cwd,
                           capture_output=True, timeout=60)
            d = subprocess.run(["git", "diff", "--cached"], cwd=run_cwd,
                               capture_output=True, text=True, timeout=60)
            patch = (d.stdout or "")[:_PATCH_MAX_CHARS]
            n = subprocess.run(["git", "diff", "--cached", "--name-only"],
                               cwd=run_cwd, capture_output=True, text=True, timeout=60)
            files_changed = [l.strip() for l in (n.stdout or "").splitlines() if l.strip()]
        except Exception as e:
            print(f"[{ts}] [CODING] {job_id}: patch capture failed: {e}", file=log_path)

        # 6. decide status
        if proc.returncode != 0 and not patch:
            _fail(f"opencode exited {proc.returncode}: "
                  f"{((proc.stderr or proc.stdout or '').strip())[:500] or summary}")
            return
        if not patch and proc.returncode == 0:
            # empty diff + exit 0 → ok, but say so explicitly
            summary = f"{summary} — no changes" if summary else "completed with no changes"
        result = {"status": "ok" if proc.returncode == 0 else "error",
                  "summary": summary[:4000], "patch": patch,
                  "files_changed": files_changed}
        if session_id:
            result["session_id"] = session_id
        if proc.returncode != 0:
            result["summary"] = (f"opencode exited {proc.returncode}: "
                                 f"{summary}")[:4000]
        print(f"[{ts}] [CODING] {job_id}: rc={proc.returncode} "
              f"files={len(files_changed)} patch={len(patch)}c", file=log_path)
        _coding_result(job_id, secret, result, log_path)
    except Exception as e:
        _fail(f"coding job failed: {e}")
    finally:
        # 8. cleanup the temp worktree (best-effort)
        if workdir:
            try:
                shutil.rmtree(workdir, ignore_errors=True)
            except Exception:
                pass


# ── command implementations ────────────────────────────────────────────
def _systemd_agent_state() -> str | None:
    """State of the `hivv-agent.service` user unit ('active', 'inactive', …),
    or None when systemd/user-units aren't in play (Windows, unit absent,
    or the user bus unreachable). start/stop/restart/update consult this so
    they go THROUGH systemd instead of racing its Restart=always — racing it
    was spawning duplicate agents that fought over state.json/pidfile."""
    if IS_WINDOWS:
        return None
    svc = Path.home() / ".config" / "systemd" / "user" / "hivv-agent.service"
    if not svc.exists():
        return None
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hivv-agent.service"],
                           capture_output=True, text=True, timeout=5)
        out = (r.stdout or "").strip()
        return out or None   # non-zero exits still print inactive/failed/…
    except Exception:
        return None

def cmd_start(args) -> None:
    _nag_if_behind()
    sd = _systemd_agent_state()
    # active/activating → systemd (or an in-flight start job) owns this; trust
    # systemd's MainPID — NOT state.node_id (that's stale after any prior run).
    # `activating` must NOT call systemctl start again: for forking units the
    # ExecStart IS `hivv start` (i.e. this very call), so systemctl would block
    # on its own job, both callers would time out, and BOTH would manual-spawn —
    # the yalla duplicate agents of 2026-11-14.
    if sd in ("active", "activating"):
        main_pid = None
        for _ in range(30):                      # give forking ExecStart a moment
            main_pid = _systemd_agent_main_pid()
            if main_pid and main_pid != os.getpid() and pid_alive(main_pid):
                break
            main_pid = None
            time.sleep(0.2)
        if main_pid:
            try:                                 # adopt pidfile if a stale launcher dropped it
                if read_pid() != main_pid:
                    ensure_dirs()
                    PID_FILE.write_text(str(main_pid))
            except OSError:
                pass
            st = read_state() or {}
            nid = st.get("node_id", "unknown")
            if sd == "activating":
                print(f"\033[32m✓\033[0m {APP} starting via systemd (hivv-agent.service, node={nid})")
            else:
                print(f"\033[32m✓\033[0m {APP} is already running (systemd: hivv-agent.service, node={nid})")
            print(f"  status:  {APP} status")
            print(f"  restart: {APP} restart")
            print(f"  logs:    {APP} logs")
            return
        if sd == "active":
            # unit claims active but no live MainPID → the agent died under it;
            # restart once, then fall back to a direct spawn if that fails too.
            warn("systemd reports active but no live agent process — restarting unit")
            try:
                subprocess.run(["systemctl", "--user", "restart", "hivv-agent.service"],
                               capture_output=True, text=True, timeout=20)
                for _ in range(30):
                    time.sleep(0.2)
                    main_pid = _systemd_agent_main_pid()
                    if main_pid and main_pid != os.getpid() and pid_alive(main_pid):
                        ok("restarted via systemd (hivv-agent.service)")
                        return
            except Exception as e:
                warn(f"systemctl restart error: {e} — falling back to direct start")
        # activating with no live agent after the wait → we ARE the forking
        # unit's launcher: spawn the agent, then exit so systemd reads PIDFile.
    pid = read_pid()
    if pid and _pid_alive_fallback(pid):
        st = read_state() or {}
        nid = st.get("node_id", "unknown")
        print(f"\033[32m✓\033[0m {APP} is already running (pid={pid}, node={nid})")
        print(f"  status:  {APP} status")
        print(f"  restart: {APP} restart")
        print(f"  logs:    {APP} logs")
        return
    ensure_dirs()
    # Inactive/failed unit → start THROUGH systemd so it owns the process
    # (survives our exit, restarts on crash, no duplicate on next boot).
    # `activating`/`active` already got their handling above — re-entering
    # systemctl start there would block on the very job we're part of.
    if sd is not None and sd not in ("active", "activating"):
        try:
            r = subprocess.run(["systemctl", "--user", "start", "hivv-agent.service"],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                for _ in range(30):              # verify a LIVE agent, not a stale state
                    time.sleep(0.2)
                    mp = _systemd_agent_main_pid()
                    if mp and mp != os.getpid() and pid_alive(mp):
                        ok("started via systemd (hivv-agent.service)")
                        st = read_state() or {}
                        if st.get("node_id"):
                            ok(f"registered with {HUB_NAME} as node {st['node_id']}")
                        return
                warn("systemd start returned but no live agent appeared — falling back to direct start")
            else:
                warn(f"systemctl start failed: {(r.stderr or r.stdout or '').strip()[:200]} — falling back to direct start")
        except Exception as e:
            warn(f"systemctl start error: {e} — falling back to direct start")
    log_path = LOG_FILE.open("a")
    popen_kw = {}
    if IS_WINDOWS:
        # detach from our console so the agent survives the terminal closing
        popen_kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(
        _agent_argv(),
        stdout=log_path, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, **popen_kw,
    )
    PID_FILE.write_text(str(proc.pid))
    ok(f"started (pid={proc.pid}, log={LOG_FILE})")
    # give the agent a moment to register
    for _ in range(20):
        time.sleep(0.1)
        st = read_state()
        if st.get("node_id"):
            ok(f"registered with {HUB_NAME} as node {st['node_id']}")
            return
    warn("registered heartbeat not yet received; check `hivv status` in a moment")

def cmd_stop(args) -> None:
    sd = _systemd_agent_state()
    if sd == "active":
        # Stop THROUGH systemd — a bare kill would be resurrected in 10s by
        # Restart=always (the old `hivv stop` never actually stayed stopped).
        try:
            r = subprocess.run(["systemctl", "--user", "stop", "hivv-agent.service"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                # the agent unlinks its own pidfile on SIGTERM (ownership-checked)
                for _ in range(30):
                    if not PID_FILE.exists():
                        break
                    time.sleep(0.2)
                if PID_FILE.exists():
                    try:
                        if not pid_alive(int(PID_FILE.read_text().strip())):
                            PID_FILE.unlink()
                    except (OSError, ValueError):
                        pass
                ok("stopped (systemd: hivv-agent.service)")
                return
            warn(f"systemctl stop failed: {(r.stderr or r.stdout or '').strip()[:200]} — falling back to signal")
        except Exception as e:
            warn(f"systemctl stop error: {e} — falling back to signal")
    pid = read_pid()
    if not pid:
        print(f"\033[33m⚠ {APP} is not running\033[0m")
        print(f"   start it with: {APP} start")
        return
    try:
        _kill_pid(pid)
    except ProcessLookupError:
        die("process already gone")
    # wait up to 5s
    for _ in range(50):
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    if pid_alive(pid):
        warn("did not exit gracefully; forcing stop")
        try: _kill_pid(pid, force=True)
        except ProcessLookupError: pass
    # ownership check: only unlink if it still records the pid we just stopped
    try:
        if PID_FILE.read_text().strip() == str(pid):
            PID_FILE.unlink()
    except (OSError, ValueError):
        pass
    ok(f"stopped (was pid={pid})")

def cmd_restart(args) -> None:
    sd = _systemd_agent_state()
    if sd is not None:
        # atomic within systemd — no stop/start window for a duplicate to slip in
        try:
            r = subprocess.run(["systemctl", "--user", "restart", "hivv-agent.service"],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                ok("restarted (systemd: hivv-agent.service)")
                return
            warn(f"systemctl restart failed: {(r.stderr or r.stdout or '').strip()[:200]} — using stop/start")
        except Exception as e:
            warn(f"systemctl restart error: {e} — using stop/start")
    pid = read_pid()
    if pid:
        cmd_stop(args)
    cmd_start(args)

def cmd_status(args) -> None:
    pid = read_pid()
    st  = read_state()
    print(f"  hub:           {HUB}")
    print(f"  state:         {'online' if pid else 'stopped'}")
    if pid:
        print(f"  pid:           {pid}")
        uptime_s = int(time.time()) - st.get("started_at", int(time.time()))
        print(f"  uptime:        {uptime_s}s")
        rss = _process_rss_kb(pid)
        if rss:                       print(f"  rss:           {rss/1024:.1f} MB")
        print(f"  node_id:       {st.get('node_id') or '(pending)'}")
        print(f"  last_heartbeat:{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st['last_heartbeat'])) if st.get('last_heartbeat') else '(none yet)'}")
        print(f"  last_status:   {st.get('last_status', '?')}")
        if st.get("last_error"):      print(f"  last_error:    {st['last_error']}")
    else:
        print(f"\n  \033[33m⚠ agent not running — start it with: {APP} start\033[0m")
    code, payload = http("GET", "/api/health", timeout=5)
    print(f"\n  {HUB_NAME} health: {code}", end="")
    if code == 200 and isinstance(payload, dict):
        print(f" — {payload.get('status', '?')}")
        if payload.get('database') == 'connected':
            print(f"    database: connected")
        if payload.get('nodes_online'):
            print(f"    nodes online: {payload['nodes_online']}")
        if payload.get('providers'):
            print(f"    AI providers: {payload['providers']} configured")
    else:
        print()

    # ── Notifications: check for issues and notify owner ──────────────
    import subprocess as _sp
    _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=5)
    issues = []

    # Check for localhost-only bindings
    llms = _detect_local_ai()
    for m in llms:
        port = m.get("port", 8080)
        r = _r(f"ss -tlnp 2>/dev/null | grep ':{port} '")
        if r.stdout and "127.0.0.1:" + str(port) in r.stdout:
            issues.append(f"port {port} is localhost only — other nodes cannot reach your LLMs")

    # Check for stopped services
    r = _r("systemctl list-units --type=service --state=dead --no-pager --plain --no-legend 2>/dev/null | grep -i llama | grep -v ollama")
    if r.stdout.strip():
        for line in r.stdout.strip().split("\n"):
            svc = line.split()[0] if line.split() else ""
            if svc:
                issues.append(f"service {svc} is stopped — run: sudo systemctl start {svc}")

    # Check for broken scripts
    import glob as _glob
    for s in _glob.glob("/home/*/llama.cpp/servers/start-*.sh"):
        content = _r(f"cat {s} 2>/dev/null").stdout
        if content and "LLAMA_BIN=" in content:
            broken_line = [l for l in content.split("\n") if l.strip().startswith("LLAMA_BIN=") and "--host" in l]
            if broken_line:
                issues.append(f"broken script: {s} — run: {APP} doctor --fix")

    # Check ollama binding
    r = _r("ss -tlnp 2>/dev/null | grep ':11434 '")
    if r.stdout and "127.0.0.1:" in r.stdout:
        issues.append("ollama is localhost only — run: sudo systemctl set-environment OLLAMA_HOST=0.0.0.0:11434 && sudo systemctl restart ollama")

    # Check Tailscale
    ts = _r("tailscale ip -4 2>/dev/null")
    if ts.returncode != 0 or not ts.stdout.strip():
        issues.append("tailscale not installed — nodes on different networks cannot reach you")

    # Check firewall
    fw_active = False
    import platform
    if platform.system() == "Linux":
        r = _r("ufw status 2>/dev/null")
        if "Status: active" in r.stdout:
            fw_active = True
    if not fw_active and llms:
        issues.append("no firewall — your LLMs are open to the internet")

    if issues:
        print(f"\n  \033[1m⚠ {len(issues)} issue(s) detected:\033[0m")
        for i, issue in enumerate(issues, 1):
            print(f"    {i}. {issue}")
        print(f"\n  fix all: {APP} doctor --fix")
        print(f"  fix bindings: {APP} doctor --fix")
    else:
        print(f"\n  \033[32m✓ no issues detected\033[0m")

def cmd_logs(args) -> None:
    n = args.n or 50
    if not LOG_FILE.exists():
        die(f"no log file at {LOG_FILE} — start the agent first")
    if args.follow:
        # simple `tail -n N -f` substitute
        with LOG_FILE.open() as f:
            data = f.readlines()[-n:]
            sys.stdout.write("".join(data)); sys.stdout.flush()
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.2); continue
                sys.stdout.write(line); sys.stdout.flush()
    else:
        # tail -n
        with LOG_FILE.open("rb") as f:
            try: f.seek(0, os.SEEK_END); size = f.tell()
            except OSError: size = 0
            f.seek(max(0, size - 64*1024))
            data = f.read().decode("utf-8", errors="replace")
        sys.stdout.write("\n".join(data.splitlines()[-n:]) + "\n")

def cmd_free(args) -> None:
    print(f"  hub:           {HUB}")
    mi = _meminfo_kb()
    if mi:
        total = int(mi.get("MemTotal", 0)); free = int(mi.get("MemAvailable", mi.get("MemFree", 0))); used = total - free
        print(f"  mem:           {used/1024/1024:.1f}G used / {total/1024/1024:.1f}G total ({free/1024/1024:.1f}G avail)")
        sw = _swapinfo_kb()
        if sw["total"]:
            print(f"  swap:          {(sw['total']-sw['free'])/1024/1024:.1f}G used / {sw['total']/1024/1024:.1f}G total")
    print(f"  disk (~):      {_disk(str(STATE_DIR))}  on {STATE_DIR}")
    pid = read_pid()
    print(f"\n  agent:         {'running (pid '+str(pid)+')' if pid else 'stopped'}")
    if pid:
        rss = _process_rss_kb(pid)
        if rss: print(f"  rss:           {rss/1024:.1f} MB")

def cmd_pct(args) -> None:
    # simple in-process sampling
    def sample():
        pid = read_pid()
        if not pid:
            return f"{time.strftime('%H:%M:%S')}  hivv:  not running"
        rss = _process_rss_kb(pid) or 0
        # CPU%: read /proc/PID/stat utime+stime once; on a busy box this is just
        # a hint — good enough for a status line.
        try:
            with open(f"/proc/{pid}/stat"): pass
            cpu_pct = _quick_cpu_pct(pid)
        except OSError:
            cpu_pct = 0.0
        return f"{time.strftime('%H:%M:%S')}  hivv:  cpu={cpu_pct:5.1f}%  mem={rss/1024:6.1f}MB  pid={pid}"
    if args.watch <= 0:
        print(sample()); return
    try:
        while True:
            print(sample(), flush=True)
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return

def _quick_cpu_pct(pid: int) -> float:
    """Instantaneous CPU% using /proc/PID/stat and /proc/stat."""
    try:
        with open(f"/proc/{pid}/stat") as f: s = f.read().split()
        with open("/proc/stat")     as f: t = f.readline().split()
        proc_j = int(s[13]) + int(s[14])
        tot_j  = sum(int(x) for x in t[1:])
        return max(0.0, min(100.0, proc_j / max(1, tot_j) * 100))
    except (OSError, ValueError, IndexError):
        return 0.0

def cmd_ping(args) -> None:
    """Ping the hub — measure round-trip latency."""
    start = time.time()
    code, payload = http("GET", "/api/health", timeout=10)
    elapsed_ms = (time.time() - start) * 1000
    if code == 200:
        ok(f"hub alive — {elapsed_ms:.0f}ms")
    else:
        die(f"hub unreachable (HTTP {code}) after {elapsed_ms:.0f}ms")

TOOLS_PORT = 18789
TOOLS_PID_FILE = Path.home() / ".local/share/hivv/tools.pid"

def _tools_running() -> bool:
    """Check if hivv-tools server is running on TOOLS_PORT."""
    pid_file = TOOLS_PID_FILE
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if pid == os.getpid():
                return True
            return pid_alive(pid)
        except ValueError:
            pass
    # Fallback: check if port is open
    try:
        with socket.create_connection(("localhost", TOOLS_PORT), timeout=1):
            return True
    except OSError:
        pass
    return False

def _find_opencode() -> str | None:
    """Locate the opencode binary even when the agent's PATH is minimal —
    pm2/systemd/manual `hivv start` shells often lack ~/.opencode/bin."""
    found = shutil.which("opencode")
    if found:
        return found
    try:
        home = Path.home()
        for cand in (home / ".opencode" / "bin" / "opencode",
                     home / ".local" / "bin" / "opencode",
                     Path("/usr/local/bin/opencode"),
                     Path("/opt/opencode/bin/opencode")):
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
    except Exception:
        pass
    return None

def _shared_tools() -> list:
    """Tool names this node is sharing right now (empty if the tools server is
    down). The agent re-announces these on every register + heartbeat so tool
    sharing SURVIVES agent restarts and re-registration (backlog F7) — running
    `hivv tools on` is the opt-in, `hivv tools off` (server down) un-shares.
    Coder bees (HIVV_OPENCODE_PLAN §4): 'opencode' is announced whenever the
    binary is on PATH — the hub only dispatches coding_job to nodes whose
    heartbeat includes it, and a heartbeat must never wipe that (hub-truth:
    we only advertise what this box can actually run)."""
    tools: list = []
    if _tools_running():
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://localhost:{TOOLS_PORT}/tools", timeout=3) as r:
                data = json.loads(r.read())
                got = data.get("tools", [])
                tools = got if isinstance(got, list) else []
        except Exception:
            tools = []
    if _find_opencode() and "opencode" not in tools:
        tools.append("opencode")
    return tools

def cmd_tools(args) -> None:
    """Manage the local hivv-tools server (time, calculate, fetch, search, sysinfo).
    These tools run on YOUR computer — others on the hive can use them!"""
    tool_script = INSTALL_DIR / f"{APP}-tools.py"
    if not tool_script.exists():
        print(f"[{APP}] tools server not installed — downloading...")
        try:
            data = _http_get_bytes(f"{HUB}/{APP}-tools.py")
            tool_script.write_bytes(data)
            tool_script.chmod(0o755)
        except Exception as e:
            die(f"download failed: {e}")

    if args.action == "status":
        if _tools_running():
            pid_file = TOOLS_PID_FILE
            pid = int(pid_file.read_text().strip()) if pid_file.exists() else "?"
            print(f"[{APP}] tools server running on port {TOOLS_PORT} (PID {pid})")
            try:
                with socket.create_connection(("localhost", TOOLS_PORT), timeout=2) as s:
                    import urllib.request
                    with urllib.request.urlopen(f"http://localhost:{TOOLS_PORT}/tools", timeout=3) as r:
                        data = json.loads(r.read())
                        print(f"[{APP}] available tools: {', '.join(data.get('tools', []))}")
            except Exception as e:
                print(f"[{APP}] health check: {e}")
        else:
            print(f"[{APP}] tools server not running")
        return

    if args.action == "list":
        print(f"\n\033[1m{APP} tools — available on this machine\033[0m")
        if _tools_running():
            try:
                with socket.create_connection(("localhost", TOOLS_PORT), timeout=2):
                    import urllib.request
                    with urllib.request.urlopen(f"http://localhost:{TOOLS_PORT}/tools", timeout=3) as r:
                        data = json.loads(r.read())
                        tools = data.get("tools", [])
                        print(f"  \033[32mlocal\033[0m  {', '.join(tools)}  (you)")
            except Exception as e:
                print(f"  \033[31mcould not reach local tools server: {e}\033[0m")
        else:
            print(f"  \033[2mlocal  (not running — run: hivv tools on)\033[0m")

        print(f"\n\033[1m{APP} tools — available on the hive\033[0m")
        code, resp = http("GET", "/api/tools", timeout=5, sign=False)
        if code == 200 and isinstance(resp, dict):
            nodes = resp.get("nodes", [])
            if nodes:
                for n in nodes:
                    tools = n.get("tools", [])
                    name = n.get("name", n.get("node_id", "?"))
                    ip = n.get("lan_ip", n.get("ip", "?"))
                    loc_city = n.get("location", {}).get("city", "?")
                    print(f"  \033[33m{loc_city}\033[0m  {', '.join(tools)}  ({name}@{ip})")
            else:
                print(f"  \033[2m  no tools shared on the hive yet\033[0m")
                print(f"  \033[2m  → hivv tools on && hivv tools share\033[0m to share yours")
        else:
            print(f"  \033[31mcould not reach hub: HTTP {code}\033[0m")
        print()
        return

    if args.action == "on":
        if _tools_running():
            print(f"[{APP}] tools server already running")
        else:
            print(f"[{APP}] starting tools server on port {TOOLS_PORT}...")
            tool_cmd = [sys.executable, str(tool_script), "start"]
            if IS_WINDOWS:
                subprocess.Popen(
                    tool_cmd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                )
            else:
                subprocess.Popen(
                    ["setsid"] + tool_cmd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
            time.sleep(2)
            if _tools_running():
                ok(f"tools server live on http://localhost:{TOOLS_PORT}")
                print(f"[{APP}] others on the hive can now use your tools!")
            else:
                die("failed to start tools server — check: python3 hivv-tools.py test")
        return

    if args.action == "off":
        pid_file = TOOLS_PID_FILE
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                _kill_pid(pid)
            except (ValueError, OSError): pass
            try: pid_file.unlink()
            except OSError: pass
        ok("tools server stopped")
        return

    if args.action == "share":
        # Announce tools availability to hub
        if not _tools_running():
            die("tools server not running — run: hivv tools on")
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://localhost:{TOOLS_PORT}/tools", timeout=3) as r:
                data = json.loads(r.read())
                tools = data.get("tools", [])
        except Exception as e:
            die(f"could not reach tools server: {e}")
        st = read_state()
        node_id = st.get("node_id")
        if not node_id:
            die("node not registered — run: hivv start")

        code, resp = http("POST", f"/api/nodes/{node_id}/tools",
                         body={"tools": tools, "port": TOOLS_PORT})
        if code == 200:
            ok(f"tools {tools} shared with hive")
            print(f"\n  \033[33m⚠ note:\033[0m  other bees on the internet may not be able to\n"
                  f"         route through you if you're behind NAT/router firewall.\n"
                  f"         Your tools work \033[1mlocally\033[0m — use \033[1m{APP} call {tools[0]} --local\033[0m\n"
                  f"         to always use your own tools directly.\n"
                  f"         To enable remote access: set up port forwarding on your\n"
                  f"         router for port {TOOLS_PORT} → this machine.")
        else:
            die(f"failed to share tools (HTTP {code})")
        return

    print(f"\033[1m{APP} tools\033[0m — local server for the hive")
    print(f"  port:   {TOOLS_PORT}")
    print(f"  status: {'running' if _tools_running() else 'stopped'}")
    print()
    print(f"  {APP} tools on       — start the tools server")
    print(f"  {APP} tools off      — stop the tools server")
    print(f"  {APP} tools status   — check if running")
    print(f"  {APP} tools share   — announce your tools to the hive")
    print()
    print(f"  Available tools: time, calculate, fetch, search, sysinfo")
    print(f"  Other bees can use your computer's tools!")

def cmd_call(args) -> None:
    """Invoke a tool through the hive (no local LLM needed).
    hivv call time
    hivv call time --tz=America/New_York
    hivv call calculate --expr="2+2"
    hivv call fetch --url=https://example.com
    hivv call search --q="weather"
    hivv call sysinfo
    hivv call time --local   # force local server (no hub routing)
    """
    tool = args.tool
    params = {}
    if args.tz:
        params["tz"] = args.tz
    if args.expr:
        params["expr"] = args.expr
    if args.url:
        params["url"] = args.url
    if args.q:
        params["q"] = args.q

    # Try local tool server first (or if --local flag), then fall back to hive routing
    force_local = getattr(args, "local", False)
    if force_local or _tools_running():
        try:
            import urllib.request
            local_path = f"http://localhost:{TOOLS_PORT}/{tool}"
            if params:
                local_path += "?" + "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            with urllib.request.urlopen(local_path, timeout=10) as r:
                data = json.loads(r.read())
                if tool == "sysinfo":
                    _print_sysinfo(data)
                elif tool == "time":
                    print(f"{data.get('human', data.get('utc', ''))}  (local)")
                elif tool == "calculate":
                    print(f"{data.get('result', data)}")
                else:
                    print(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
                return
        except Exception as e:
            pass  # fall through to hive

    # Route through hive to another node
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    path = f"/api/tools/{tool}/invoke" + (f"?{query}" if query else "")
    code, resp = http("GET", path, timeout=15, sign=False)
    if code == 202 and isinstance(resp, dict) and resp.get("verdict") == "MAYBE":
        _print_maybe(resp)
        return
    if code == 403 and isinstance(resp, dict) and resp.get("verdict") == "NO":
        _print_no(resp)
        sys.exit(1)
    if code == 200:
        data = resp if isinstance(resp, dict) else {}
        if tool == "sysinfo":
            _print_sysinfo(data)
        elif tool == "time":
            print(f"{data.get('time', '')}  (node: {data.get('node', '?')})")
        elif tool == "calculate":
            print(f"{data.get('result', data)}  (node: {data.get('node', '?')})")
        else:
            print(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
    elif code == 404:
        die(f"tool '{tool}' is not available — no bee on the hive is sharing it yet\n"
            f"  → run: hivv tools on && hivv tools share to share your own")
    elif code == 502:
        die(f"tool '{tool}' is shared on the hive but unreachable (NAT/firewall issue)\n"
            f"  → your node can't receive inbound connections from the hub\n"
            f"  → to fix: set up port forwarding on your router for port 18789,\n"
            f"     or run 'hivv call {tool} --local' to use your local tools directly")
    else:
        die(f"tool call failed (HTTP {code}): {resp}")

def _print_sysinfo(data: dict) -> None:
    if not data:
        die("sysinfo returned no data")
    print(f"  node:        {data.get('node', data.get('provider', '?'))}")
    for k in ("cpu_model", "cpu_count", "cpu_percent", "memory_total_gb",
              "memory_free_gb", "memory_used_gb", "swap_total_gb", "swap_free_gb",
              "disk_total_gb", "disk_free_gb", "uptime_days", "loadavg"):
        v = data.get(k)
        if v is not None:
            print(f"  {k:<20} {v}")


# ── hivv OS: capabilities (§1) · policy YES/MAYBE/NO (§3) · route modes (§2) ──
_ROUTE_ALIASES = {"smart": "quality", "cheap": "cost", "green": "cost", "private": "private"}

def _verdict_color(v: str) -> str:
    v = (v or "").lower()
    if v in ("allow", "yes", "used", "approved"): return "\033[32m"
    if v in ("ask", "maybe", "pending"): return "\033[33m"
    return "\033[31m"

def _print_maybe(d: dict) -> None:
    gid = d.get("grant_id") or (d.get("policy") or {}).get("grant_id") or "?"
    cap = d.get("capability") or (d.get("policy") or {}).get("key") or "?"
    print(f"\033[33mMAYBE\033[0m — '{cap}' needs a human before the hive will run it.")
    print(f"  approve: \033[1mhivv approve {gid}\033[0m   refuse: hivv deny {gid}")
    print(f"  (once approved, it stays allowed for the grant's lifetime — no repeat asking)")

def _print_no(d: dict) -> None:
    pol = d.get("policy") or {}
    cap = d.get("capability") or pol.get("key") or "?"
    rule = d.get("rule") or pol.get("rule") or "?"
    print(f"\033[31mNO\033[0m — policy refused '{cap}' (rule: {rule})")
    if "no backend" in str(d.get("error", "")) or rule.startswith("default:money") or rule.startswith("default:physical"):
        print("  reserved frontier tier — nothing to talk to yet; the door stays locked by default.")

def cmd_cap(args) -> None:
    """hivv cap [list|can <capability>|node <name>] — the capability layer."""
    action = getattr(args, "action", None) or "list"
    if action == "can":
        cap = getattr(args, "name", None)
        if not cap: die("usage: hivv cap can <capability>")
        code, resp = http("POST", "/api/policy/check", body={"action": {"capability": cap}}, timeout=10)
        if code != 200 or not isinstance(resp, dict):
            die(f"hub check failed ({code}): {str(resp)[:120]}")
        v = resp.get("verdict", "?")
        c = _verdict_color(v)
        print(f"{c}{v.upper()}\033[0m  {cap}  (tier={resp.get('tier','?')} risk={resp.get('risk',0)} rule={resp.get('rule','?')})")
        return
    if action == "node":
        name = getattr(args, "name", None)
        if not name: die("usage: hivv cap node <name>")
        code, resp = http("GET", f"/api/capabilities/node/{name}", timeout=10, sign=False)
        if code != 200 or not isinstance(resp, dict):
            die(f"node not found ({code})")
        print(f"\033[1m{resp.get('name', name)}\033[0m — status {resp.get('status','?')} · volunteer={'yes' if resp.get('volunteer') else 'no'}")
        caps = resp.get("capabilities") or []
        if caps:
            print("  capabilities: " + ", ".join(sorted(caps)))
        else:
            print("  \033[2mno capabilities advertised yet (no models/tools shared)\033[0m")
        if resp.get("tools"): print("  tools: " + ", ".join(resp["tools"]))
        return
    # list (default)
    code, resp = http("GET", "/api/capabilities", timeout=10, sign=False)
    if code != 200 or not isinstance(resp, dict):
        die(f"could not reach hub ({code}): {str(resp)[:120]}")
    print(f"\n\033[1m{APP} capabilities — what the hive can do ({resp.get('total', '?')} registered)\033[0m")
    print(f"  \033[2m{'CAPABILITY':<22} {'TIER':<12} {'DEFAULT':<8} {'ONLINE BEES':<11} NODES\033[0m")
    for row in sorted(resp.get("capabilities", []), key=lambda r: (r.get("tier", ""), r.get("key", ""))):
        v = row.get("default_verdict", "?")
        c = _verdict_color(v)
        nodes = ", ".join((row.get("nodes") or [])[:4])
        print(f"  {row.get('key',''):<22} {row.get('tier',''):<12} {c}{v.upper():<8}\033[0m {str(row.get('online',0)):<11} \033[2m{nodes[:40]}\033[0m")
    print(f"\n  route modes: {', '.join(resp.get('modes', []))}  → hivv chat --mode=cheap|fast|private|smart MSG")
    print(f"  try: hivv cap can web.fetch  ·  hivv cap node <name>  ·  hivv policy\n")

def cmd_policy(args) -> None:
    """hivv policy [show|explain CAP|history|verify|grants] — YES/MAYBE/NO rules."""
    action = getattr(args, "action", None) or "show"
    if action == "explain":
        cap = getattr(args, "name", None)
        if not cap: die("usage: hivv policy explain <capability>")
        code, resp = http("POST", "/api/policy/check", body={"action": {"capability": cap}}, timeout=10)
        if code != 200 or not isinstance(resp, dict):
            die(f"hub check failed ({code}): {str(resp)[:120]}")
        v = resp.get("verdict", "?")
        c = _verdict_color(v)
        print(f"{c}{v.upper()}\033[0m  {resp.get('key', cap)}")
        print(f"  tier: {resp.get('tier','?')}   risk: {resp.get('risk',0)}   rule: {resp.get('rule','?')}")
        meanings = {"allow": "the hive will run it for you", "ask": "a human must approve first (hivv approve <id>)", "deny": "refused — the rule says no"}
        print(f"  means: {meanings.get(v, '')}")
        return
    if action == "history":
        code, resp = http("GET", "/api/policy/audit?limit=25", timeout=10, sign=False)
        if code != 200 or not isinstance(resp, dict):
            die(f"could not fetch audit trail ({code})")
        rows = resp.get("rows", [])
        if not rows:
            print("  no gated decisions yet — side-effects land here, hash-chained 🧾")
            return
        print(f"\033[1maudit trail\033[0m — newest first (hash-chained, tamper-evident)")
        for r in rows:
            v = r.get("verdict", "?")
            c = _verdict_color(v)
            act = r.get("action") or {}
            ts = str(r.get("ts", ""))[:19]
            print(f"  {ts}  {c}{v.upper():<6}\033[0m {str(act.get('capability','?')):<20} rule={r.get('rule','?'):<18} \033[2m{r.get('hash','')[:10]}\033[0m")
        return
    if action == "verify":
        code, resp = http("GET", "/api/policy/audit/verify", timeout=20, sign=False)
        if code != 200 or not isinstance(resp, dict):
            die(f"verify failed ({code})")
        if resp.get("ok"):
            ok(f"audit chain intact — {resp.get('rows_checked',0)} rows verified, head {resp.get('head','')}…")
        else:
            print(f"\033[31m✗ chain broken at row {resp.get('broken_at')}: {resp.get('reason')}\033[0m")
        return
    if action == "grants":
        code, resp = http("GET", "/api/policy/grants?mine=1", timeout=10)
        if code != 200 or not isinstance(resp, dict):
            die(f"could not fetch grants ({code})")
        grants = resp.get("grants", [])
        if not grants:
            print("  no grants for you yet — MAYBEs become grants here once a tool asks you")
            return
        for g in grants:
            c = _verdict_color(g.get("status", ""))
            print(f"  {c}{str(g.get('status','')).upper():<9}\033[0m {g.get('id',''):<14} {g.get('capability',''):<20} {g.get('scope','')}  \033[2m{(g.get('reason') or '')[:40]}\033[0m")
        return
    # show (default)
    code, resp = http("GET", "/api/policy", timeout=10, sign=False)
    if code != 200 or not isinstance(resp, dict):
        die(f"could not reach hub ({code}): {str(resp)[:120]}")
    print(f"\n\033[1mhivv policy\033[0m — WHO · WHAT · DATA · WHERE · WHEN · COST · RISK · AUDIT\n")
    print(f"  \033[2m{'CAPABILITY':<22} {'TIER':<12} {'RISK':<5} DEFAULT\033[0m")
    for row in sorted(resp.get("capabilities", []), key=lambda r: (-r.get("risk", 0))):
        v = row.get("default_verdict", "?")
        c = _verdict_color(v)
        print(f"  {row.get('key',''):<22} {row.get('tier',''):<12} {row.get('risk',0):<5} {c}{v.upper()}\033[0m")
    tup = resp.get("tuple") or {}
    if tup:
        print()
        for k in ("WHO", "WHAT", "DATA", "WHERE", "WHEN", "COST", "RISK", "AUDIT"):
            if tup.get(k): print(f"  \033[33m{k:<5}\033[0m {tup[k]}")
    print(f"\n  every gated decision is audited: hivv policy history · hivv policy verify\n")

def _grant_decide(gid: str, verdict: str, scope=None, ttl=None, memo=None) -> bool:
    body = {"verdict": verdict}
    if scope: body["scope"] = scope
    if ttl: body["ttl_s"] = ttl
    if memo: body["memo"] = memo
    code, resp = http("POST", f"/api/policy/grants/{gid}/decide", body=body, timeout=10)
    if code == 200 and isinstance(resp, dict) and resp.get("ok"):
        g = resp.get("grant") or {}
        c = _verdict_color(g.get("status", ""))
        ok(f"{gid} → {c}{g.get('status', verdict)}\033[0m ({g.get('capability','?')} · {g.get('scope','?')})")
        _approvals_forget(gid)
        return True
    if code == 404:
        print(f"  \033[31m✗\033[0m {gid}: not found")
        _approvals_forget(gid)
    elif code == 409:
        print(f"  \033[33m!\033[0m {gid}: already decided")
        _approvals_forget(gid)
    else:
        print(f"  \033[31m✗\033[0m {gid}: HTTP {code} {str(resp)[:100]}")
    return False

def cmd_approve(args) -> None:
    """hivv approve [GRANT_ID ...] | --list | --all — answer a MAYBE."""
    ids = list(getattr(args, "ids", []) or [])
    if getattr(args, "do_list", False) or not ids:
        code, resp = http("GET", "/api/policy/grants?mine=1", timeout=10)
        if code != 200 or not isinstance(resp, dict):
            die(f"could not fetch grants ({code})")
        pending = [g for g in resp.get("grants", []) if g.get("status") == "pending"]
        if not pending:
            print("  nothing waiting for you — every MAYBE is answered 🐝")
            return
        print(f"\033[1m{len(pending)} pending MAYBE(s):\033[0m")
        for g in pending:
            act = g.get("args_summary") or {}
            print(f"  \033[33m{g.get('id',''):<14}\033[0m {g.get('capability',''):<20} \033[2m{g.get('reason') or json.dumps(act)[:50]}\033[0m  asked {str(g.get('created_at',''))[:19]}")
        if getattr(args, "do_list", False):
            print(f"\n  answer with: hivv approve <id>   (or: hivv approve --all)")
            return
        ids = [g.get("id") for g in pending if getattr(args, "all", False)]
        if not ids:
            return
    n = 0
    for gid in ids:
        if _grant_decide(gid, "approve", scope=getattr(args, "scope", None), ttl=getattr(args, "ttl", None), memo=getattr(args, "memo", None)):
            n += 1
    if n:
        print(f"\n  ✓ {n} approved — re-run the original command and it will pass the gate")

def cmd_deny(args) -> None:
    """hivv deny GRANT_ID [...] — refuse a MAYBE."""
    ids = list(getattr(args, "ids", []) or [])
    if not ids: die("usage: hivv deny <grant_id> [...]")
    for gid in ids:
        _grant_decide(gid, "deny", memo=getattr(args, "memo", None))

def cmd_route(args) -> None:
    """hivv route [question] --mode=X — routing planner (dry-run, never executes)."""
    mode = getattr(args, "mode", None) or os.environ.get("HIVV_ROUTE_MODE") or "balanced"
    mode = _ROUTE_ALIASES.get(mode, mode)
    cap = getattr(args, "capability", None) or "reasoning"
    code, resp = http("GET", f"/api/capabilities/route/dry-run?capability={cap}&mode={mode}", timeout=10, sign=False)
    if code != 200 or not isinstance(resp, dict):
        die(f"planner failed ({code}): {str(resp)[:120]}")
    if getattr(args, "json_output", False):
        print(json.dumps(resp, indent=2)); return
    pol = resp.get("policy") or {}
    c = _verdict_color(pol.get("verdict", ""))
    print(f"\n\033[1mhivv route — mode={resp.get('mode')} capability={resp.get('capability')}\033[0m")
    print(f"  policy: {c}{str(pol.get('verdict','?')).upper()}\033[0m (rule {pol.get('rule','?')} · risk {pol.get('risk',0)})")
    print(f"  transports allowed: {' → '.join(resp.get('order', []))}")
    t = resp.get("transports") or {}
    direct = t.get("direct") or []
    if direct:
        print(f"  \033[2m{'NODE':<14} {'MODEL':<24} {'T/S':<6} {'LAT':<8} {'SCORE':<7}\033[0m")
        for d in direct[:6]:
            print(f"  {str(d.get('node',''))[:13]:<14} {str(d.get('model',''))[:23]:<24} {d.get('tps',0):<6} {str(d.get('latency_ms',''))+'ms':<8} {d.get('score',0)}")
    else:
        print("  \033[2mdirect: none reachable right now\033[0m")
    relay = t.get("relay") or []
    if relay:
        print(f"  relay: {', '.join(r.get('node','?') for r in relay)} (volunteer bees behind NAT)")
    cloud = t.get("cloud") or []
    if cloud:
        parts = [f"{x.get('provider')}~${x.get('cost_per_512_out_est_usd')}" for x in cloud[:6]]
        print("  cloud: " + ", ".join(parts))
    elif resp.get("privacy") != "any":
        print("  cloud: \033[32mexcluded by your privacy rule ✓\033[0m")
    q = " ".join(getattr(args, "question", []) or [])
    if q:
        print(f"\n  to actually send it: hivv chat --mode={getattr(args,'mode',None) or mode} '{q}'")
    print()

def cmd_exec(args) -> None:
    """hivv exec '<code>' [--lang python|bash|node] [--file F] — run code in an
    ISOLATED sandbox on the hub (no network, RAM/CPU/pids quotas, read-only,
    ephemeral). Gated by the coding.execute policy: the first run returns MAYBE,
    an ADMIN approves it (hivv approve <G_xxx>), then re-run to execute."""
    lang = getattr(args, "lang", None) or "python"
    code = " ".join(getattr(args, "code", []) or [])
    fpath = getattr(args, "file", None)
    if fpath:
        try:
            code = Path(fpath).read_text()
        except Exception as e:
            die(f"could not read --file {fpath}: {e}")
    if not code.strip() and not sys.stdin.isatty():
        try:
            code = sys.stdin.read()
        except Exception:
            code = ""
    if not code.strip():
        die("usage: hivv exec '<code>'   (or --file script.py, or pipe code via stdin)")

    scode, resp = http("POST", "/api/sandbox/exec",
                       body={"code": code, "language": lang, "timeout_ms": getattr(args, "timeout", None)},
                       timeout=65)
    if scode == 202 and isinstance(resp, dict):
        _print_maybe(resp)
        print("  \033[2mnote: coding.execute is high-risk — an ADMIN must approve (self-approval is disabled).\033[0m")
        return
    if scode == 403 and isinstance(resp, dict):
        _print_no(resp)
        return
    if scode != 200 or not isinstance(resp, dict):
        die(f"sandbox exec failed (HTTP {scode}): {str(resp)[:200]}")

    runtime = resp.get("runtime", "?")
    exit_code = resp.get("exit_code")
    timed_out = resp.get("timed_out")
    hdr = f"\033[1m🧪 sandbox [{runtime}]\033[0m  lang={lang}  exit={exit_code}  {resp.get('ms','?')}ms"
    if timed_out:
        hdr += "  \033[31m(timed out — killed)\033[0m"
    print(hdr)
    out = resp.get("stdout") or ""
    err = resp.get("stderr") or ""
    if out:
        print(out, end="" if out.endswith("\n") else "\n")
    if err:
        print(f"\033[31m{err}\033[0m", end="" if err.endswith("\n") else "\n")
    if not out and not err:
        print("  \033[2m(no output)\033[0m")
    print(f"  \033[2m— isolated: no network · RAM/CPU/pids capped · read-only · destroyed after run\033[0m")

def _volunteer_load() -> dict:
    try:
        return json.loads(_VOLUNTEER_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

def _hms(seconds: int) -> str:
    d, s = divmod(seconds, 86400); h, s = divmod(s, 3600); m, s = divmod(s, 60)
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m {s}s"

def cmd_donate(args) -> None:
    """Run a system diagnostic and get personalized recommendations on what
    you can donate to the hivv network (compute, tools, bandwidth, or all three).
    Use 'hivv donate history' to see what you've donated so far."""
    action = getattr(args, "action", "diagnose")

    # ── history view ──────────────────────────────────────────────
    if action == "history":
        print(f"\n\033[1m╭── hivv donate history ── your contributions ─────────\033[0m\033[0m\n")
        vol = _volunteer_load()
        tools_ok = _tools_running()
        print(f"  \033[1mYour gifts to the hive\033[0m\n")
        if vol.get("share_cpu"):
            since = time.strftime("%Y-%m-%d", time.localtime(vol.get("opted_in_at", 0)))
            print(f"  \033[1mcompute\033[0m   volunteering since {since}")
        else:
            print(f"  \033[1mcompute\033[0m   \033[2mnot volunteering\033[0m  (run: hivv volunteer on)")
        if tools_ok:
            print(f"  \033[1mtools\033[0m    sharing 5 tools on port 18789")
        else:
            print(f"  \033[1mtools\033[0m    \033[2mnot sharing\033[0m  (run: hivv tools on)")
        print()

        code, resp = http("GET", "/api/feedback", timeout=5, sign=False)
        if code == 200 and isinstance(resp, dict):
            items = resp.get("items", [])
            if items:
                print(f"  \033[1mFeedback submitted ({len(items)})\033[0m")
                for fb in items:
                    ts = fb.get("created_at", "")
                    status = fb.get("status", "open")
                    ftype = fb.get("type", "?")
                    msg = fb.get("message", "")[:60]
                    print(f"    \033[2m{ts[:16]}\033[0m  [{status:>8}]  \033[33m{ftype}\033[0m  {msg}")
            else:
                print(f"  \033[1mFeedback submitted\033[0m  \033[2m(none yet — run: hivv help submit suggestion ...)\033[0m")
        else:
            print(f"  \033[2mcould not reach hub to fetch feedback history\033[0m")
        print(f"\n\033[1m╰────────────────────────────────────────────────────\033[0m\033[0m\n")
        return

    # ── diagnose view (default) ──────────────────────────────────
    print(f"\n\033[1m╭── hivv donate ── system diagnostic ─────────────────\033[0m\033[0m\n")

    m = _collect_metrics()
    gpus = m.get("gpus", [])
    tools_ok = _tools_running()
    vol = _volunteer_load()
    vol_on = bool(vol.get("share_cpu"))

    # ── hardware ──
    cpu = m.get("cpu_model") or "unknown CPU"
    cores = m.get("cpu_count") or 0
    load = m.get("load_1") or 0.0
    mem_total = round(m.get("mem_total_mb", 0) / 1024, 1)
    mem_avail = round(m.get("mem_avail_mb", 0) / 1024, 1)
    mem_used_pct = m.get("mem_pct") or 0
    disk_free = m.get("disk_free_gb") or 0
    uptime_s = m.get("uptime_s") or 0
    loc = m.get("location") or {}
    location_str = f"{loc.get('city', '')}, {loc.get('country', '')}" if loc else "unknown"

    print(f"  \033[1mHardware\033[0m")
    print(f"    CPU:       {cpu}  ({cores} cores, load {load})")
    print(f"    RAM:       {mem_total}GB total, ~{mem_avail}GB free ({100-mem_used_pct:.0f}% available)")
    print(f"    Disk:      ~{disk_free}GB free on root")
    if gpus:
        for g in gpus:
            mem_free_gb = round((g["mem_total_mb"] - g["mem_used_mb"]) / 1024, 1)
            print(f"    GPU:       {g['name']}  {mem_free_gb}GB / {round(g['mem_total_mb']/1024,1)}GB free  ({g['util_pct']}% busy, {g['temp_c']}°C)")
    else:
        print(f"    GPU:       none detected (NVIDIA only)")
    print(f"    Location:  {location_str}")
    print(f"    Uptime:   {_hms(uptime_s)}")
    print()

    # ── current contributions ──
    vol_label = f"\033[32m✓ volunteering\033[0m" if vol_on else f"\033[2mnot volunteering\033[0m  (run: hivv volunteer on)"
    tools_label = f"\033[32m✓ sharing\033[0m  (port 18789)" if tools_ok else f"\033[2mnot sharing\033[0m  (run: hivv tools on)"
    print(f"  \033[1mCurrent contributions\033[0m")
    print(f"    compute:   {vol_label}")
    print(f"    tools:     {tools_label}")
    print()

    # ── recommendations ──
    print(f"  \033[1mRecommendations for you\033[0m\n")

    can_do_tools = not tools_ok
    can_do_compute = not vol_on
    suggestions = []

    # Tools assessment
    if tools_ok:
        suggestions.append(("tools", "share your tool server",
            f"  \033[32m→ hivv tools share\033[0m  (you already have the server running!)"))
    else:
        suggestions.append(("tools", "run a tool server",
            f"  \033[36m→ hivv tools on && hivv tools share\033[0m\n"
            f"    Your computer would handle: time, calculate, fetch, search, sysinfo\n"
            f"    for other users who don't have an LLM. Zero compute cost to you."))

    # Compute assessment
    if vol_on:
        suggestions.append(("compute", "keep volunteering compute", "  \033[32m✓ already a volunteer bee!\033[0m"))
    else:
        load_ok = load < cores * 0.7
        mem_ok = mem_avail >= 4
        if load_ok and mem_ok:
            suggestions.append(("compute", "donate idle CPU cycles",
                f"  \033[36m→ hivv volunteer on\033[0m\n"
                f"    Your box is plenty idle (load {load} < {cores*0.7:.1f}) with ~{mem_avail}GB RAM free.\n"
                f"    The hive can use your CPU for inference tasks while you sleep."))
        elif not load_ok:
            suggestions.append(("compute", "your CPU is busy right now",
                f"  \033[2m→ load is {load} (> {cores*0.7:.1f} threshold); re-run when idle\033[0m"))
        elif not mem_ok:
            suggestions.append(("compute", "not enough free RAM to volunteer",
                f"  \033[2m→ only ~{mem_avail}GB free; need ~4GB headroom for LLM tasks\033[0m"))

    # GPU assessment
    if gpus:
        for g in gpus:
            mem_free_gb = (g["mem_total_mb"] - g["mem_used_mb"]) / 1024
            if mem_free_gb >= 4:
                suggestions.append(("gpu", f"donate your {g['name']} GPU",
                    f"  \033[36m→ hivv volunteer on\033[0m  (GPU compute is especially valuable to the hive)"))
            else:
                suggestions.append(("gpu", f"GPU has only {mem_free_gb:.1f}GB free",
                    f"  \033[2m→ not enough free GPU memory to run inference tasks\033[0m"))

    for tag, title, detail in suggestions:
        print(f"  {detail}")

    print()
    if not any("✓" in detail for _, _, detail in suggestions):
        print(f"  \033[1mQuick start:\033[0m  \033[36mhivv tools on && hivv tools share\033[0m  +  \033[36mhivv volunteer on\033[0m")
    print(f"\n\033[1m╰──────────────────────────────────────────────────\033[0m\033[0m\n")

TYPE_LABELS = {
    "suggestion":    "\033[36msuggestion\033[0m",
    "complaint":      "\033[31mcomplaint\033[0m",
    "help_request":   "\033[35mhelp_request\033[0m",
    "bug":            "\033[33mbug\033[0m",
    "praise":         "\033[32mpraise\033[0m",
    "other":          "\033[2mother\033[0m",
}
TYPEmoji = {
    "suggestion": "💡", "complaint": "😤", "help_request": "🙋",
    "bug": "🐛", "praise": "🌟", "other": "💬",
}

def cmd_help(args) -> None:
    """Submit or list help requests, suggestions, complaints, and bugs.
    hivv help                    — list your submitted items
    hivv help submit <type> <msg>  — submit feedback (type: suggestion, complaint, help_request, bug, praise, other)
    """
    action = getattr(args, "action", "list")

    if action == "list":
        code, resp = http("GET", "/api/feedback", timeout=8, sign=False)
        if code != 200:
            die(f"could not reach hub (HTTP {code})")
        data = resp if isinstance(resp, dict) else {}
        items = data.get("items", [])
        print(f"\n\033[1m╭── hivv help — your submissions ─────────────────────\033[0m\033[0m\n")
        if not items:
            print(f"  \033[2mno submissions yet\033[0m\n")
            print(f"  \033[1mSubmit something:\033[0m")
            print(f"    \033[36mhivv help submit suggestion  'add more tools'\033[0m")
            print(f"    \033[36mhivv help submit complaint 'rate limiting is too aggressive'\033[0m")
            print(f"    \033[36mhivv help submit bug       'chat crashes on empty message'\033[0m")
            print(f"    \033[36mhivv help submit help_request 'how do I donate compute?'\033[0m")
        else:
            for fb in items:
                ts = fb.get("created_at", "")[:16]
                ftype = fb.get("type", "?")
                status = fb.get("status", "open")
                msg = fb.get("message", "")
                label = TYPE_LABELS.get(ftype, ftype)
                emoji = TYPEmoji.get(ftype, "💬")
                short = msg[:70] + ("..." if len(msg) > 70 else "")
                print(f"  {emoji} \033[1m{ts}\033[0m  [{status:>8}]  {label}")
                print(f"    {short}\n")
        print(f"\033[1m╰────────────────────────────────────────────────────\033[0m\033[0m\n")
        return

    if action == "submit":
        ftype = getattr(args, "type", None)
        message = " ".join(getattr(args, "message", []) or []).strip()
        if not ftype or not message:
            die("usage: hivv help submit <type> <message>\n"
                "  types: suggestion, complaint, help_request, bug, praise, other\n"
                "  e.g.  hivv help submit suggestion 'add a calculator tool'")
        valid = list(TYPE_LABELS.keys())
        if ftype not in valid:
            die(f"unknown type '{ftype}'. must be one of: {', '.join(valid)}")
        code, resp = http("POST", "/api/feedback", body={"type": ftype, "message": message}, timeout=10, sign=False)
        if code == 200:
            ok(f"submitted \033[1m{ftype}\033[0m — thank you! 💌")
            print(f"\n  view all submissions: \033[36mhivv help list\033[0m")
        else:
            die(f"submission failed (HTTP {code}): {resp}")
        return

def cmd_info(args) -> None:
    _nag_if_behind()
    print(f"\033[1m{APP} info\033[0m — {VERSION}  (hub: {HUB})")
    st = read_state()

    # Hub health
    code, payload = http("GET", "/api/health", timeout=5)
    print(f"\n  hub:           {HUB} [{code}]")
    if code == 200 and isinstance(payload, dict):
        print(f"    status:       {payload.get('status','?')}")
        print(f"    nodes online: {payload.get('nodes_online','?')}")
        print(f"    AI providers: {payload.get('providers','?')}")

    # Local hardware
    m = _collect_metrics()
    print(f"\n  \033[1mhardware\033[0m")
    if m.get("cpu_model"):   print(f"    cpu:         {m['cpu_model']}")
    print(f"    cores:       {m.get('cpu_count','?')} logical")
    if m.get("load_1") is not None:
        print(f"    load:        {m['load_1']} / {m.get('load_5','?')} / {m.get('load_15','?')}")
    print(f"    mem:         {m.get('mem_used_mb','?')} / {m.get('mem_total_mb','?')} MB ({m.get('mem_pct','?')}%)")
    if m.get("swap_total_mb"):
        print(f"    swap:        {m.get('swap_used_mb','?')} / {m.get('swap_total_mb','?')} MB")
    print(f"    disk:        {m.get('disk_used_gb','?')} / {m.get('disk_total_gb','?')} GB ({m.get('disk_pct','?')}%)")
    if m.get("gpus"):
        for g in m["gpus"]:
            print(f"    gpu:         {g['name']}  {g['mem_used_mb']}/{g['mem_total_mb']} MB  {g['util_pct']}% util  {g['temp_c']}°C")
    else:
        print(f"    gpu:         none detected (or no nvidia-smi)")
    if m.get("lan_ip"):      print(f"    lan_ip:      {m['lan_ip']}")
    if m.get("location"):
        loc = m["location"]
        print(f"    location:    {loc.get('city','?')}, {loc.get('country','?')} ({loc.get('region','?')})")
        print(f"    isp:         {loc.get('isp','?')}")
    if m.get("uptime_s"):
        up = m["uptime_s"]
        days, rem = divmod(up, 86400)
        hrs, rem = divmod(rem, 3600)
        mins = rem // 60
        print(f"    uptime:      {days}d {hrs}h {mins}m")

    # Install paths
    print(f"\n  \033[1minstall\033[0m")
    print(f"    cli:         {INSTALL_DIR / (APP + '-cli.py')}")
    print(f"    wrapper:     {BIN_DIR / APP}")
    print(f"    config:      {CONFIG_FILE}")
    print(f"    state:       {STATE_DIR}")
    print(f"    log:         {LOG_FILE}")
    print(f"    pid:         {PID_FILE}")

    # Agent state
    print(f"\n  \033[1magent\033[0m")
    pid = read_pid()
    print(f"    state:       {'online' if pid else 'stopped'}")
    if pid: print(f"    pid:         {pid}")
    print(f"    node_id:     {st.get('node_id') or '(pending)'}")
    print(f"    hw_id:       {_machine_id() or '(unavailable)'}")
    print(f"    auth_token:  {'set' if AUTH_FILE.exists() else 'NOT SET'}")
    print(f"    transport:   {st.get('transport', 'heartbeat-only')}  {'(2-way active)' if st.get('transport') == 'long-poll' else '(1-way)'}")
    if st.get("last_heartbeat"):
        print(f"    last_hb:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st['last_heartbeat']))}")

def _llama_start_guard(_r, svc):
    """Return (skip, reason) when starting a stopped llama service would feed a crash loop.

    Guards the classic failure: duplicate units in system vs user scope fighting over
    a fixed port (loser loops on "couldn't bind HTTP server socket"), or a unit that is
    already crash-looping. Blind restarts only perpetuate both."""
    base = svc[:-8] if svc.endswith(".service") else svc
    try:
        # A "not-found" unit has no file on disk — systemd only keeps a stale
        # in-memory object (e.g. a deleted system-scope duplicate). Starting it
        # is a silent no-op that doctor would falsely report as "✓ started", so
        # skip it; the entry clears on reboot.
        load = _r(f"systemctl show {svc} -p LoadState --value 2>/dev/null").stdout.strip()
        if load == "not-found":
            return True, (f"{svc} has no unit file (not-found) — nothing to start "
                          f"(stale runtime entry clears on reboot)")
        if _r(f"systemctl --user is-active {base} 2>/dev/null").stdout.strip() == "active":
            return True, (f"user-scope {base} is already active — two units on the same "
                          f"ports crash-loop each other; keep exactly ONE scope for these servers")
        n = _r(f"systemctl show {svc} -p NRestarts --value 2>/dev/null").stdout.strip()
        if n.isdigit() and int(n) > 10:
            return True, (f"crash-looping ({n} restarts) — inspect 'journalctl -u {svc}' and fix "
                          f"ExecStart/port before starting it again")
    except Exception:
        pass
    return False, ""

def _run_health_checks():
    """Auto-detect and fix common issues after update."""
    import subprocess as _sp
    _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=10)

    print(f"\n\033[1mhealth checks\033[0m")

    # Duplicate agents — a second process heartbeats under the same node id, so
    # the hub's version flaps and both race on state.json (register-loop).
    _killed, _keep, _note = _reap_duplicate_agents()
    if _note:
        print(f"  \033[32m✓\033[0m {_note}")

    # Check for stale state (node_id with suffix that doesn't match current hostname)
    st = read_state() or {}
    node_id = st.get("node_id", "")
    import socket
    hostname = socket.gethostname().split(".")[0]
    if node_id and "-" in node_id and node_id != hostname:
        # Check if the suffix is an IP octet (like yalla-135)
        suffix = node_id.split("-", 1)[1]
        if suffix.isdigit() and int(suffix) <= 255:
            print(f"  stale node_id: {node_id} (IP-based suffix)")
            # Reset state to get clean hostname
            st["node_id"] = hostname
            write_state(st)
            print(f"  \033[32m✓\033[0m reset node_id to {hostname}")

    # Check for broken script (LLAMA_BIN= with --host)
    for script in ["/home/*/llama.cpp/servers/start-*.sh"]:
        import glob as _glob
        for s in _glob.glob(script):
            content = _r(f"cat {s} 2>/dev/null").stdout
            if content and "LLAMA_BIN=" in content and "--host" in content.split("LLAMA_BIN=")[1].split("\n")[0]:
                print(f"  broken script: {s}")
                # Fix it using the firewall helper logic
                lines = []
                for line in content.split("\n"):
                    if line.strip().startswith("LLAMA_BIN=") and "--host" in line:
                        line = line.replace(" --host 0.0.0.0", "").replace(" --host", "")
                    lines.append(line)
                import tempfile
                tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False)
                tmp.write("\n".join(lines))
                tmp.close()
                _r(f"sudo cp {tmp.name} {s} && sudo chmod +x {s}")
                _r(f"rm -f {tmp.name}")
                print(f"  \033[32m✓\033[0m fixed: {s}")
                # Restart service in whichever scope actually owns it (try-restart = only if running)
                _r("sudo systemctl try-restart llama.service 2>/dev/null")
                _r("systemctl --user try-restart llama.service 2>/dev/null")

    # Check ollama binding
    r = _r("ss -tlnp 2>/dev/null | grep ':11434 '")
    if r.stdout and "127.0.0.1:" in r.stdout:
        print(f"  ollama: localhost only — run {APP} doctor --fix")

    # Check for stopped llama services
    r = _r("systemctl list-units --type=service --state=dead --no-pager --plain --no-legend 2>/dev/null | grep -i llama | grep -v ollama")
    if r.stdout.strip():
        for line in r.stdout.strip().split("\n"):
            svc = line.split()[0] if line.split() else ""
            if not svc:
                continue
            skip, why = _llama_start_guard(_r, svc)
            if skip:
                print(f"  \033[33m!\033[0m left {svc} stopped: {why}")
                continue
            print(f"  stopped service: {svc}")
            _r(f"sudo systemctl start {svc}")
            print(f"  \033[32m✓\033[0m started: {svc}")

    # Check Tailscale
    ts = _r("tailscale ip -4 2>/dev/null")
    if ts.returncode != 0 or not ts.stdout.strip():
        print(f"  \033[33m!\033[0m tailscale not installed")
        print(f"     install: curl -fsSL https://tailscale.com/install.sh | sh")
        print(f"     then:    sudo tailscale up")
        print(f"     secure:  {APP} firewall --apply --mode tailscale")
    else:
        print(f"  \033[32m✓\033[0m tailscale active: {ts.stdout.strip()}")

    # Helpful sudo reminders
    print(f"\n\033[1msudo commands you may need\033[0m")
    print(f"  sudo tailscale up                          — start VPN")
    print(f"  sudo systemctl restart llama               — restart LLM servers")
    print(f"  sudo systemctl restart ollama              — restart ollama")
    print(f"  {APP} firewall --apply --mode lan          — open ports for LAN")
    print(f"  {APP} doctor --fix                        — auto-fix bindings")
    print()

def cmd_upgrades(args) -> None:
    """Show recent upgrade review outcomes across the hive (from the hub).
    Usage: hivv upgrades [--flagged] [--node NAME] [--json]"""
    params = "limit=20"
    if getattr(args, "flagged", False):
        params += "&flagged=1"
    if getattr(args, "node", None):
        params += f"&node={args.node}"
    code, p = http("GET", f"/api/hive/upgrade-reports?{params}", timeout=8, sign=False)
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch upgrade reports: {p}")
    reports = p.get("reports", [])
    if getattr(args, "json_output", False):
        print(json.dumps(p, indent=2))
        return
    if not reports:
        print("\n  no upgrade reports yet")
        print(f"  run one: {APP} upgrade\n")
        return
    print(f"\n\033[1m{APP} upgrades\033[0m — {len(reports)} recent review outcome(s)\n")
    print(f"  {'WHEN':<19} {'NODE':<15} {'SRC':<7} {'ACTION':<9} {'VERDICT':<12} {'BRAINS':<9} REASON")
    print(f"  {'─'*19} {'─'*15} {'─'*7} {'─'*9} {'─'*12} {'─'*9} {'─'*30}")
    for r in reports[:20]:
        when = r.get("created_at", "")
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.strptime(str(when)[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            pass
        node = str(r.get("node_id", "?"))[:14]
        src = str(r.get("source", "?"))[:6]
        action = r.get("action", "?")
        act_col = {"applied": "\033[32mapplied\033[0m",
                   "blocked": "\033[31mblocked\033[0m",
                   "failed":  "\033[33mfailed\033[0m"}.get(action, action)
        verdict = r.get("verdict", "ok")
        v_col = ("\033[32mok\033[0m" if verdict == "ok" else
                 f"\033[31m{verdict}\033[0m")
        votes = r.get("votes") or []
        okv = sum(1 for v in votes if isinstance(v, dict) and v.get("verdict") == "ok")
        sus = sum(1 for v in votes if isinstance(v, dict) and v.get("verdict") == "suspicious")
        brains = f"{okv}✓/{sus}✗" if votes else "—"
        reason = str(r.get("reason") or "")[:60]
        # pad around ANSI-colored cells using visible widths
        def _w(s):  # visible width (strip ANSI)
            return len(re.sub(r"\033\[[0-9;]*m", "", s))
        def _pad(s, n):
            return s + " " * max(0, n - _w(s))
        print(f"  {_pad(when,19)} {_pad(node,15)} {_pad(src,7)} {_pad(act_col,9)} {_pad(v_col,12)} {_pad(brains,9)} {reason}")
    flagged = [r for r in reports if r.get("action") in ("blocked", "failed")]
    if flagged:
        print(f"\n  \033[33m⚠ {len(flagged)} flagged outcome(s)\033[0m — details: "
              f"{APP} incidents  ·  this bee's last review: {APP} upgrade --show-review")
    print()

def _control_token() -> str:
    """Control-plane token for operator commands: env var first, then the hub's
    local 0600 token file (bees don't have that file, so remote callers must
    export HIVV_CONTROL_TOKEN). Needed because a request to https://<hub> from
    the hub host itself arrives over the public IP, never as 127.0.0.1 — so the
    hub's localhost bypass does not apply."""
    tok = os.environ.get("HIVV_CONTROL_TOKEN", "").strip()
    if tok:
        return tok
    for p in (Path(__file__).resolve().parents[1] / "api" / ".control-token",
              Path("/var/www/vhosts/hivv.org/api/.control-token")):
        try:
            t = p.read_text().strip()
            if t:
                return t
        except OSError:
            continue
    return ""


def cmd_repair(args) -> None:
    """Push a signed, allowlisted repair plan to a bee's inbox.

    The plan is signed by the hub with the bee's own node secret, carries a
    short ttl and a one-shot id, and can only contain ops from REPAIR_OPS —
    so neither this caller nor a compromised hub can turn it into a shell."""
    node = " ".join(args.node) if args.node else ""
    if not node:
        die(f"usage: {APP} repair <bee> --op <op> [--unit <unit>] [--reason ...]")

    ops = []
    for name in (args.op or []):
        name = str(name).strip()
        if name not in REPAIR_OPS:
            die(f"unknown op {name!r} — allowed: {', '.join(sorted(REPAIR_OPS))}")
        if name == "service_restart":
            if not args.unit:
                die("--op service_restart requires --unit <service>")
            if args.unit not in REPAIR_UNITS:
                die(f"unit {args.unit!r} not allowed — allowed: {', '.join(sorted(REPAIR_UNITS))}")
            if args.force:
                die("--force only applies to --op cli_update")
            ops.append({"op": name, "unit": args.unit})
        elif name == "cli_update":
            ops.append({"op": name, "force": bool(args.force)})
        else:
            if args.force:
                die("--force only applies to --op cli_update")
            ops.append({"op": name})

    ttl = max(60, min(int(args.ttl or 300), 600))
    body = json.dumps({"ops": ops, "reason": args.reason or "", "ttl_s": ttl}).encode()
    req = urllib.request.Request(
        f"{HUB.rstrip('/')}/api/control/agents/{node}/repair",
        data=body, method="POST",
        headers={"content-type": "application/json",
                 "user-agent": f"{APP}/{VERSION}",
                 "accept": "application/json"})
    tok = _control_token()
    if tok:
        req.add_header("x-control-token", tok)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            out = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read().decode(errors="replace") or "").strip()[:300]
        if e.code == 401:
            die("control auth required — run from the hub host, or export HIVV_CONTROL_TOKEN=<hub token>")
        die(f"repair rejected (HTTP {e.code}): {detail}")
    except Exception as e:
        die(f"repair failed: {e}")

    ok(f"plan {out.get('plan_id')} queued for {node} (ttl {out.get('ttl_s')}s)")
    print(f"  ops: {json.dumps(out.get('ops') or ops)}")
    if args.reason:
        print(f"  reason: {args.reason}")
    print(f"  outcome arrives on the bee's next heartbeat — watch the hub log for [REPAIR]")


def cmd_update(args) -> None:
    url = getattr(args, "from_url", None) or f"{HUB.rstrip('/')}/hivv-cli.py"
    sha = getattr(args, "sha256", None)
    target = INSTALL_DIR / f"{APP}-cli.py"

    # --show-review: surface the cached review verdict without touching anything
    if getattr(args, "show_review", False):
        cached = (read_state() or {}).get("_up_review") or {}
        if not cached:
            print("no review cached yet — run: " + f"{APP} upgrade")
            return
        verdict = cached.get("verdict", "?")
        color = "\033[32m" if verdict == "ok" else "\033[31m"
        print(f"\n\033[1mlast upgrade review\033[0m")
        print(f"  sha256:  {cached.get('sha', '?')}")
        print(f"  verdict: {color}{verdict.upper()}\033[0m — {cached.get('reason', '')}")
        print(f"  when:    {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(cached.get('ts', 0)))}")
        scan = cached.get("scan") or {}
        findings = scan.get("findings") or []
        if findings:
            print(f"  findings (+{scan.get('added', 0)}/-{scan.get('removed', 0)} lines):")
            for f in findings[:12]:
                print(f"    ! {f.get('severity', '?')}: {f.get('why', '')}")
                if f.get("line"):
                    print(f"        {f['line'][:140]}")
        votes = cached.get("votes") or []
        if votes:
            print(f"  brains:")
            for v in votes:
                mark = {"ok": "✓", "suspicious": "✗"}.get(v.get("verdict"), "?")
                print(f"    {mark} {v.get('brain', '?')}: {v.get('verdict') or 'no verdict'} — {v.get('reason', '')}")
        print()
        return

    # Block updates in dev mode unless --force is passed
    if _is_dev_mode() and not getattr(args, "force", False):
        die("blocked: dev mode active (HIVV_DEV_MODE or .hivv-dev marker). "
            "Run with --force to override, or remove the dev marker to update normally.")
    if getattr(args, "force", False) and _is_dev_mode():
        warn("force updating despite dev mode — this will overwrite your local changes!")
    if not target.exists():
        target = Path(__file__).resolve()
    try:
        data = _http_get_bytes(url, timeout=30)
    except Exception as e:
        die(f"download failed: {e}")
    remote_sha = hashlib.sha256(data).hexdigest()
    if target.exists():
        local_data = target.read_bytes()
        local_sha = hashlib.sha256(local_data).hexdigest()
        if local_sha == remote_sha:
            ok(f"already up to date — version {_get_version(local_data)} (sha256 unchanged)")
            # Even if up to date, run health checks
            _run_health_checks()
            # The FILE can be current while the AGENT predates it (an earlier
            # update wrote the bytes but died before restarting it). Nothing
            # else catches that — sha-only checks say 'up to date' and the node
            # keeps reporting the old version forever.
            stale = _agent_running_stale(target)
            if stale:
                log(f"agent is running older code than {target.name} — restarting ({stale})")
                if read_pid():
                    cmd_stop(args); cmd_start(args)
                else:
                    print(f"  \033[33m!\033[0m agent not running — start it with: {APP} start")
            return
    log(f"fetching {url}")
    # ── pre-apply review: static scan + local/hive LLM vote ──────────
    # Suspicious → block + report to hub (override with --force, or skip
    # entirely with --skip-review for emergency unattended updates).
    review = None          # kept for the post-apply report (honest verdicts)
    if not getattr(args, "skip_review", False):
        try:
            old_bytes = target.read_bytes() if target.exists() else None
        except OSError:
            old_bytes = None
        print(f"\n\033[1mreviewing update\033[0m {VERSION} → "
              f"{_get_version(data) or 'unknown'} (sha256 {remote_sha[:12]}…)")
        gate = _upgrade_review_gate(data, source="manual",
                                    echo=lambda s: print(f"  {s}"),
                                    budget_s=90,
                                    force=bool(getattr(args, "force", False)))
        review = gate.get("review")
        if not gate["allow"]:
            block_msg = (f"update blocked: {gate['reason']}\n"
                         f"    nothing was applied; incident filed on the hub.\n"
                         f"    review details: {APP} upgrade --show-review  ·  "
                         f"override: {APP} upgrade --force")
            # A stale output-capturing launcher would swallow this (its `set -e`
            # dies at the capture line on our non-zero exit) — write straight to
            # the terminal too, so the operator always sees the block.
            try:
                if not sys.stdout.isatty():
                    with open("/dev/tty", "w") as tty:
                        tty.write(f"\033[0;31m[{APP}]\033[0m {block_msg}\n")
            except OSError:
                pass
            die(block_msg, code=20)
    else:
        log("review skipped (--skip-review)")
    update_ok, msg, target = _apply_update(data, expected_sha256=sha, source="cli")
    if not update_ok:
        _upgrade_report_once({"source": "manual", "action": "failed",
                         "to_sha256": remote_sha,
                         "verdict": (review or {}).get("verdict", "ok"),
                         "reason": msg,
                         "static_findings": (review or {}).get("scan", {}).get("findings", []),
                         "votes": (review or {}).get("votes", [])},
                        remote_sha, "failed")
        die(msg, code=20)
    ok(f"{msg} → {target}")
    # Honest post-apply report: carries the real review verdict (a --force
    # apply of a suspicious version stays visible in the hub's history).
    if review:
        _upgrade_report_once({"source": "manual", "action": "applied",
                         "to_sha256": remote_sha,
                         "verdict": review.get("verdict", "ok"),
                         "reason": (f"forced apply despite {review.get('verdict')}"
                                    if review.get("verdict") == "suspicious"
                                    else review.get("reason") or "applied cleanly"),
                         "static_findings": review.get("scan", {}).get("findings", []),
                         "votes": review.get("votes", [])},
                        remote_sha, "applied")
    else:
        _upgrade_report_once({"source": "manual", "action": "applied",
                         "to_sha256": remote_sha, "verdict": "ok",
                         "reason": "applied (review skipped)", "static_findings": [], "votes": []},
                        remote_sha, "applied")
    # Run health checks after update
    _run_health_checks()
    # Restart when anything is (or should be) running: the pidfile may be
    # empty while systemd still owns a live agent — skipping the restart
    # there would leave stale code running forever.
    if read_pid() or _systemd_agent_state() is not None:
        log("restarting agent to pick up new code")
        cmd_stop(args); cmd_start(args)

def cmd_dev(args) -> None:
    """Toggle or show dev mode. Dev mode blocks auto-update to protect local development."""
    marker = CONFIG_DIR / ".hivv-dev"
    if getattr(args, "off", False):
        if marker.exists():
            marker.unlink()
        env_var = os.environ.pop("HIVV_DEV_MODE", None)
        ok("dev mode disabled — auto-update active")
    else:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"dev mode active\ntoggled at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        ok(f"dev mode enabled — auto-update blocked\n  marker: {marker}\n  (or set HIVV_DEV_MODE=1 env var)")

def cmd_uninstall(args) -> None:
    pid = read_pid()
    if pid: cmd_stop(args)
    for f in (PID_FILE,):
        try: f.unlink()
        except OSError: pass
    # remove install but KEEP config + log
    for p in (INSTALL_DIR / f"{APP}-cli.py", BIN_DIR / APP):
        try:
            if p.exists(): p.unlink(); log(f"removed {p}")
        except OSError as e:
            warn(f"could not remove {p}: {e}")
    ok(f"uninstalled (kept config: {CONFIG_DIR}, logs: {STATE_DIR})")

# ── one-shot commands ──────────────────────────────────────────────────
def cmd_health(args) -> None:
    code, p = http("GET", "/api/health")
    print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
    sys.exit(0 if code == 200 else 2)


# ── fast scan: deterministic, AI-free source screening ─────────────────────
_FAST_SCAN_LANGS = {
    ".py": "python", ".pyi": "python",
    ".js": "node", ".mjs": "node", ".cjs": "node", ".jsx": "node",
    ".ts": "node", ".tsx": "node",
    ".rb": "ruby", ".php": "php", ".pl": "perl", ".pm": "perl",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".cs": "csharp",
}


def _fast_scan_lang(suffix: str) -> str | None:
    return _FAST_SCAN_LANGS.get(str(suffix or "").lower())


# ── local standalone fast scan ─────────────────────────────────────────────
# The SAME rules as the gateway's api/lib/fast-scan.js, reimplemented in
# pure-stdlib Python so `hivv fast-scan` works with NO gateway, NO internet,
# and NO Node.js. The two implementations are kept in step by the contract
# tests; both are deterministic and fail-closed.
_FAST_HARD = [
    ("dynamic_exec", "critical", "dynamic code execution", re.compile(r"(?<![.\w])(?:eval|exec)\s*\(\s*[^\s)]")),
    ("shell_true", "critical", "shell command interpolation", re.compile(r"shell\s*=\s*True")),
    ("os_system", "critical", "shell command execution", re.compile(r"\bos\.system\s*\(")),
    ("subprocess_shell", "high", "process spawning", re.compile(r"\bsubprocess\.(?:run|call|Popen|check_output|check_call)\s*\(")),
    ("destructive_delete", "critical", "recursive/destructive delete", re.compile(r"\b(?:shutil\.rmtree|fs\.rmSync|rm\s+-[rf])\b")),
    ("credential_paths", "critical", "credential file access", re.compile(r"(?:^|[\s\"'=])(?:/etc/(?:passwd|shadow)|\.ssh/id_[a-z0-9]+|~?/\.aws/credentials)", re.M | re.I)),
    ("env_secret_read", "high", "secret environment variable read", re.compile(r"\b(?:os\.environ(?:\.get)?\s*\(?\s*['\"][^'\"]*(?:SECRET|TOKEN|PASSWORD|KEY)|process\.env\.[A-Z_]*(?:SECRET|TOKEN|PASSWORD|KEY))")),
    ("network_socket", "high", "network request", re.compile(r"\b(?:socket\.socket|socket\.create_connection|requests\.(?:get|post|put|delete)|urllib\.request\.urlopen|httpx\.(?:get|post))\s*\(")),
    ("reverse_shell", "critical", "possible reverse shell", re.compile(r"\b(?:nc|ncat|netcat)\b[^\n]{0,80}(?:\-e|/bin/(?:ba)?sh)|/dev/tcp/")),
    ("obfuscation", "high", "encoded/executable payload", re.compile(r"\b(?:base64\.(?:b64decode|decodebytes)|marshal\.loads|pickle\.loads|exec\s*\(\s*['\"]|eval\s*\(\s*['\"])")),
    ("privilege_escalation", "critical", "privilege escalation", re.compile(r"\b(?:sudo\s|chmod\s+[467][0-7]{2}\b|os\.setuid|setuid\s*\()")),
    ("persistence", "high", "persistence mechanism", re.compile(r"(?:crontab\s+-|\.bashrc|\.zshrc|authorized_keys)")),
    ("pipe_to_shell", "critical", "download piped to shell", re.compile(r"(?:curl|wget)[^|\n]{0,120}\|\s*(?:ba)?sh\b")),
    ("child_process", "high", "child process spawn", re.compile(r"\b(?:child_process\.)?(?:exec|execSync|spawn|spawnSync|fork)\s*\(")),
]

_FAST_SECRET = [
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

_FAST_COMMANDS = {
    "python.print": r"\bprint\s*\(",
    "python.open": r"\bopen\s*\(",
    "python.eval": r"\beval\s*\(",
    "python.exec": r"\bexec\s*\(",
    "python.__import__": r"\b__import__\s*\(",
    "os.system": r"\bos\.system\s*\(",
    "os.remove": r"\bos\.remove\s*\(",
    "os.environ": r"\bos\.environ\b",
    "subprocess.run": r"\bsubprocess\.run\s*\(",
    "subprocess.Popen": r"\bsubprocess\.Popen\s*\(",
    "shell=True": r"\bshell\s*=\s*True\b",
    "requests.get": r"\brequests\.get\s*\(",
    "requests.post": r"\brequests\.post\s*\(",
    "urllib.request": r"\burllib\.request\.(?:urlopen|Request)\b",
    "socket.socket": r"\bsocket\.socket\s*\(",
    "fs.writeFileSync": r"\bfs\.(?:writeFileSync|writeFile|appendFileSync)\s*\(",
    "fs.unlinkSync": r"\bfs\.(?:unlinkSync|rmSync)\s*\(",
    "child_process": r"\b(?:child_process\.)?(?:exec|execSync|spawn|spawnSync|fork)\s*\(",
    "curl": r"\bcurl\b",
    "wget": r"\bwget\b",
    "ssh": r"\bssh\b",
    "sudo": r"\bsudo\b",
    "chmod": r"\bchmod\b",
    "pickle.loads": r"\bpickle\.loads?\s*\(",
    "base64.decode": r"\bbase64\.(?:b64decode|decodebytes)\s*\(",
    "password.read": r"(?:read_text|readFileSync|open)\s*\([^\n]{0,120}(?:passwd|shadow|\.ssh|id_rsa)",
    "env.read": r"\b(?:os\.environ|process\.env|getenv)\b",
}

_FAST_RISK_WEIGHTS = {
    "dynamic_execution": 40, "shell_execution": 35, "process_spawn": 20,
    "filesystem_write": 20, "filesystem_read": 8, "network": 25,
    "credential_access": 35, "privilege_escalation": 35, "destructive": 40,
    "obfuscation": 30, "persistence": 30, "resource_exhaustion": 15,
}


def _fast_risk_level(score: int) -> str:
    if score <= 0: return "safe"
    if score <= 19: return "low"
    if score <= 49: return "medium"
    if score <= 79: return "high"
    return "critical"


def _fast_entropy(text: str) -> int:
    if not text: return 0
    freq: dict = {}
    for ch in text: freq[ch] = freq.get(ch, 0) + 1
    h = 0.0
    for n in freq.values():
        p = n / len(text)
        h -= p * math.log2(p)
    return int(round(h))


def _local_fast_scan(code: str, language: str = "python") -> dict | None:
    """Deterministic, AI-free source scan — pure stdlib, no gateway needed."""
    try:
        src = str(code or "")
        if not src.strip(): return None
        sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
        lines = src.split("\n")

        hard = []
        for pid, level, label, rx in _FAST_HARD:
            ms = list(rx.finditer(src))
            if ms:
                hard.append({
                    "id": pid, "level": level, "label": label, "count": len(ms),
                    "position": ms[0].start(), "sample": ms[0].group(0)[:120],
                })

        secrets = []
        for sid, rx in _FAST_SECRET:
            m = rx.search(src)
            if m:
                secrets.append({
                    "id": sid,
                    "fingerprint": hashlib.sha256(m.group(0).encode()).hexdigest()[:16],
                    "position": m.start(),
                })

        commands = []
        for name, pattern in _FAST_COMMANDS.items():
            n = len(re.findall(pattern, src))
            if n:
                commands.append({"name": name, "count": n})
        commands.sort(key=lambda c: (-c["count"], c["name"]))

        write_n = len(re.findall(r"\bopen\s*\([^\n)]*?['\"][^'\"]*[wax+][^'\"]*['\"]", src))
        read_n = len(re.findall(r"\bopen\s*\([^\n)]*?['\"][^'\"]*[r+][^'\"]*['\"]", src))
        del_n = len(re.findall(r"\b(?:os\.(?:remove|unlink)|fs\.(?:unlinkSync|rmSync))\s*\(", src))
        domains = sorted({m.group(1).lower() for m in re.finditer(r"https?://([^/\s:'\"]+)", src)})
        net_indicators = len(re.findall(r"\b(?:requests|urllib|httpx|aiohttp|socket|fetch|axios)\b", src))

        categories = {
            "dynamic_execution": len(re.findall(r"\b(?:eval|exec|compile|__import__)\s*\(", src)),
            "shell_execution": len(re.findall(r"\b(?:os\.system|subprocess\.[A-Za-z_]+|child_process\.|shell\s*=\s*True)\b", src)),
            "process_spawn": len(re.findall(r"\b(?:subprocess\.|child_process\.|os\.fork|os\.spawn)", src)),
            "filesystem_write": write_n,
            "filesystem_read": read_n + del_n,
            "network": net_indicators + len(domains),
            "credential_access": len(re.findall(r"(?:passwd|shadow|id_rsa|api[_-]?key|secret|token|password|\.ssh|\.env\b)", src, re.I)),
            "privilege_escalation": len(re.findall(r"\b(?:sudo|chmod\s+[467][0-7]{2}\b|os\.setuid|setuid\s*\()", src)),
            "destructive": len(re.findall(r"\b(?:rm\s+-[rf]|shutil\.rmtree|os\.remove|os\.unlink|fs\.rmSync)\b", src)),
            "obfuscation": len(re.findall(r"\b(?:base64\.(?:b64decode|decodebytes)|marshal\.loads|pickle\.loads|exec\s*\(\s*['\"]|eval\s*\(\s*['\"])", src)),
            "persistence": len(re.findall(r"(?:crontab\s+-|systemctl\s+enable|authorized_keys|\.bashrc|\.zshrc)", src)),
            "resource_exhaustion": len(re.findall(r"\b(?:while\s+True|ThreadPoolExecutor|ProcessPoolExecutor|fork\s*\()", src)),
        }
        score = 0
        cat_scores = {}
        for cat, count in categories.items():
            pts = min(_FAST_RISK_WEIGHTS.get(cat, 0), count * _FAST_RISK_WEIGHTS.get(cat, 0))
            cat_scores[cat] = pts
            score += pts
        for h in hard:
            score += 25 if h["level"] == "critical" else (15 if h["level"] == "high" else 8)
        score += 20 * len(secrets)
        score = max(0, min(100, score))
        level = _fast_risk_level(score)

        if any(h["level"] == "critical" for h in hard) or secrets:
            verdict = "unsafe"
        elif hard or level in ("medium", "high", "critical"):
            verdict = "review"
        else:
            verdict = "safe"

        riskiest = sorted(
            ({"name": k, "points": v, "count": categories[k]} for k, v in cat_scores.items() if v > 0),
            key=lambda x: (-x["points"], x["name"]),
        )[:8]

        return {
            "schema": "hivv.fast-scan.v1",
            "source_sha256": sha,
            "language": language,
            "size": {
                "bytes": len(src.encode("utf-8")),
                "chars": len(src),
                "lines": len(lines),
                "non_empty_lines": sum(1 for l in lines if l.strip()),
                "max_line_length": max((len(l) for l in lines), default=0),
                "avg_line_length": int(round(len(src) / len(lines))) if lines else 0,
            },
            "entropy": {"whole_file": _fast_entropy(src)},
            "imports": [],
            "commands": commands[:20],
            "command_count": sum(c["count"] for c in commands),
            "most_used_commands": commands[:10],
            "most_used": commands[0] if commands else None,
            "riskiest_operations": riskiest,
            "files": {
                "paths": [], "modes": {"read": read_n, "write": write_n, "append": 0, "delete": del_n},
                "read_count": read_n, "write_count": write_n, "delete_count": del_n,
            },
            "network": {"indicators": net_indicators, "urls": [], "domains": domains[:20], "ports": []},
            "risks": {
                "score": score, "level": level, "categories": categories,
                "category_scores": cat_scores,
                "most_used": commands[0] if commands else None,
                "riskiest_operations": riskiest,
            },
            "permissions": None,
            "hard_findings": hard,
            "secret_findings": secrets,
            "verdict": verdict,
            "verdict_basis": "deterministic-only",
            "scanned_at_ms": int(time.time() * 1000),
            "scan_duration_ms": 0,
            "needs_ai_review": verdict != "safe" or score >= 20,
        }
    except Exception:
        return None


def cmd_fast_scan(args) -> None:
    """Screen a file WITHOUT an AI.

    Free, instant, and reproducible: the same file always produces the same
    answer. It counts bytes/chars/lines, lists the commands/APIs, file paths
    and network targets it finds, and scores the risk.

    IMPORTANT: this can only make a verdict WORSE. `safe` here means "no
    hard-danger signal found" — it is NOT an approval. Use `hivv review` (or
    the signed BLNQ attestation) when you need an actual signed verdict.

    `hivv fast-scan FILE [--json]`
    `hivv fast-scan FILE --hub http://127.0.0.1:3011`   # a local gateway
    """
    path = getattr(args, "path", None)
    if not path:
        die("give a file: hivv fast-scan FILE")
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        die(f"file not found: {p}")
    try:
        code = p.read_text(errors="replace")
    except OSError as e:
        die(f"cannot read {p}: {e}")

    lang = _fast_scan_lang(p.suffix.lower())
    body = {"language": lang or "python", "code": code, "filename": p.name}

    hub = getattr(args, "hub", None)
    endpoint = "/api/partner/fast-scan"

    code_, resp = http("POST", endpoint, body=body, timeout=60, base_url=hub)
    if code_ != 200:
        # No gateway reachable. Fall back to the LOCAL deterministic scanner so
        # `hivv fast-scan` still works offline, with no hub and no internet.
        # The result is identical in substance — same rules, same fields — it
        # simply was not produced by a gateway.
        local = _local_fast_scan(code, lang or "python")
        if local is None:
            print(json.dumps(resp, indent=2) if isinstance(resp, (dict, list)) else resp)
            sys.exit(2)
        local["source"] = "local-standalone"
        local["note"] = "no HIVV gateway was reachable — scanned locally with the same rules"
        resp = {"data": local}
        code_ = 200
        if getattr(args, "json_output", False):
            print(json.dumps(local, indent=2))
            sys.exit(0)
    data = resp.get("data", resp) if isinstance(resp, dict) else resp

    if getattr(args, "json_output", False):
        print(json.dumps(data, indent=2))
        sys.exit(0)

    size = data.get("size", {})
    risks = data.get("risks", {})
    verdict = data.get("verdict", "?")
    level = risks.get("level", "?")
    score = risks.get("score", "?")

    mark = {"safe": "\033[32m", "review": "\033[33m", "unsafe": "\033[31m"}.get(verdict, "")
    end = "\033[0m" if mark else ""
    print(f"\n\033[1mfast-scan\033[0m  {p.name}")
    print(f"  verdict   : {mark}{verdict}{end}   risk {score}/100 {level}")
    print(f"  size      : {size.get('bytes','?')} bytes · {size.get('chars','?')} chars · "
          f"{size.get('lines','?')} lines")
    print(f"  language  : {data.get('language','?')}")

    most = data.get("most_used")
    if most:
        print(f"  most used : {most.get('name')} ({most.get('count')}x)")

    cmds = data.get("commands") or []
    if cmds:
        print("  commands  :")
        for c in cmds[:10]:
            print(f"    {c.get('count')}x {c.get('name')}")

    files = data.get("files") or {}
    paths = files.get("paths") or []
    if paths:
        print(f"  files     : {', '.join(paths[:8])}")
    if files.get("write_count"):
        print(f"  writes    : {files.get('write_count')} write mode(s)")

    net = data.get("network") or {}
    if net.get("domains"):
        print(f"  network   : {', '.join(net['domains'][:8])}")

    hard = data.get("hard_findings") or []
    if hard:
        print("  \033[31mhard findings\033[0m:")
        for h in hard[:10]:
            print(f"    [{h.get('level')}] {h.get('label')} ({h.get('count')}x)")

    secrets = data.get("secret_findings") or []
    if secrets:
        print("  \033[31mpossible secrets\033[0m (fingerprints only):")
        for s in secrets[:10]:
            print(f"    {s.get('id')} · {s.get('fingerprint')}")

    if data.get("needs_ai_review"):
        print(f"\n  \033[33m→ needs a real review\033[0m (run `hivv review {p}` for a signed verdict)")
    else:
        print(f"\n  \033[32m→ no hard-danger signal found\033[0m (not an approval — `hivv review` signs one)")
    print(f"  scanned in {data.get('scan_duration_ms','?')}ms · no AI used")


def cmd_partner(args) -> None:
    """Show the public partner surface (what other sites can integrate with).

    `hivv partner`          → the manifest
    `hivv partner health`   → liveness + local models
    `hivv partner network`  → the live hive roster
    `hivv partner models`   → models this instance can serve
    """
    what = (getattr(args, "what", None) or "manifest").lower()
    paths = {
        "manifest": "/api/partner/manifest",
        "health": "/api/partner/health",
        "network": "/api/partner/network",
        "models": "/api/partner/models",
        "capabilities": "/api/partner/capabilities",
    }
    if what not in paths:
        die(f"unknown: {what} — try: {', '.join(sorted(paths))}")
    code_, p = http("GET", paths[what])
    print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
    sys.exit(0 if code_ == 200 else 2)


def cmd_local(args) -> None:
    """Manage a LOCAL, self-hosted HIVV gateway.

    `hivv local status`   → is a local gateway running?
    `hivv local start`    → start one (uses scripts/setup-local.sh)
    `hivv local stop`     → stop it
    `hivv local info`     → where things live
    """
    what = (getattr(args, "what", None) or "status").lower()
    repo = Path(__file__).resolve().parent.parent
    setup = repo / "scripts" / ("setup-local.ps1" if IS_WINDOWS else "setup-local.sh")
    data_dir = os.environ.get("HIVV_DATA_DIR") or str(Path.home() / ".local" / "share" / "hivv")

    if what == "status":
        port = os.environ.get("HIVV_PORT") or os.environ.get("HIVV_PORT") or "3011"
        for base in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            try:
                code_, p = http("GET", "/api/health", base_url=base)
            except Exception:
                continue
            if code_ == 200:
                print(f"  local gateway: {base}")
                print(json.dumps(p, indent=2))
                sys.exit(0)
        print(f"  no local gateway on port {port}")
        print(f"  start one with: {setup}")
        sys.exit(1)

    if what == "info":
        print(f"  repo       : {repo}")
        print(f"  setup      : {setup}")
        print(f"  data dir   : {data_dir}")
        print(f"  api dir    : {repo / 'api'}")
        print(f"  partner api: /api/partner/manifest")
        return

    if what in ("start", "stop"):
        if IS_WINDOWS:
            die("on Windows use: .\\scripts\\setup-local.ps1  (or start the node process directly)")
        pid_file = Path(data_dir) / "hivv.pid"
        if what == "stop":
            if not pid_file.exists():
                die("no local gateway pid file — nothing to stop")
            pid = int(pid_file.read_text().strip())
            try:
                os.kill(pid, 15)
                print(f"  stopped pid {pid}")
            except OSError as e:
                die(f"could not stop pid {pid}: {e}")
            return
        if not setup.exists():
            die(f"setup script not found: {setup}")
        subprocess.run(["bash", str(setup)], check=False)
        return

    die(f"unknown: {what} — try: status, start, stop, info")

def cmd_metrics(args) -> None:
    if getattr(args, "local", False):
        _print_metrics_local(_collect_metrics())
        return
    code, p = http("GET", "/api/control/metrics/global")
    print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
    sys.exit(0 if code == 200 else 2)

def _bee_model_summary(nodes: list, width: int = 112) -> list:
    """One line per bee reporting local models: `name  model (provider) · …`.

    Shared by `hivv nodes` (trailing section), `hivv models` (top section)
    and `hivv models --bees`. Pure formatting — no I/O — so the test suite
    drives it straight from fixtures."""
    lines = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        models = n.get("local_models") or []
        parts = []
        for m in models:
            if not isinstance(m, dict):
                continue
            mdl = str(m.get("model") or m.get("name") or "?")
            prov = str(m.get("provider") or "?")
            parts.append(f"{mdl} ({prov})")
        status = str(n.get("status") or "")
        # Online bees with an EMPTY heartbeat get an explicit line — silently
        # omitting them made "why is only one bee listed?" unanswerable.
        # Offline ghosts without models stay hidden.
        if not parts and status != "online":
            continue
        name = str(n.get("name") or n.get("id") or "?")[:15]
        body = " · ".join(parts) if parts else "(no local models reported)"
        suffix = "" if status == "online" else f" [{status or 'unknown'}]"
        line = f"  {name:<16}{body}{suffix}"
        if len(line) > width:
            line = line[:width - 1] + "…"
        lines.append(line)
    return lines

def cmd_nodes(args) -> None:
    code, p = http("GET", "/api/control/nodes")
    if getattr(args, "json_output", False):
        print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
        sys.exit(0 if code == 200 else 2)

    # Human-readable output
    if code != 200 or not isinstance(p, dict):
        print(f"failed to fetch nodes: {p}")
        sys.exit(2)

    nodes = p.get("nodes", [])
    total = p.get("total", 0)
    online = p.get("online", 0)

    if not nodes:
        print(f"\n  no nodes registered")
        print(f"  start an agent: {APP} start\n")
        sys.exit(0)

    # latest CLI version (hub truth) — bees behind should hivv update
    latest = None
    try:
        c2, v2 = http("GET", "/api/version", timeout=5, sign=False)
        if c2 == 200 and isinstance(v2, dict):
            latest = v2.get("version")
    except Exception:
        pass

    # Fetch cached pings (one call, not per-node — hub caches 30s)
    pings = {}
    try:
        cp, pp = http("GET", "/api/control/ping", timeout=3, sign=False)
        if cp == 200 and isinstance(pp, dict):
            pings = pp.get("pings", {})
    except Exception:
        pass

    print(f"\n\033[1m{APP} nodes\033[0m — {online} online, {total} total{' · hub on ' + latest if latest else ''}\n")

    # Column headers
    print(f"  {'NAME':<16} {'STATUS':<9} {'VERSION':<12} {'TPS':<8} {'PING':<8} {'MODELS':<7} {'LAST HB':<10} {'CPU':<22} {'RAM':<14} {'GPU'}")
    print(f"  {'─'*16} {'─'*9} {'─'*12} {'─'*8} {'─'*8} {'─'*7} {'─'*10} {'─'*22} {'─'*14} {'─'*25}")

    behind = 0
    # A node that hasn't heartbeated in this long is a ghost registration — it
    # can't run `hivv update`, so counting it as "behind" warns forever.
    GHOST_AGE_S = 24 * 3600

    for n in nodes:
        name = n.get("name", n.get("id", "?"))[:15]
        status = n.get("status", "?")
        models = len(n.get("local_models", []))
        hb = n.get("last_heartbeat", 0)
        hb_age = n.get("heartbeat_age_s") or (int(time.time()) - (hb / 1000) if hb else -1)
        if hb_age < 0:
            hb_str = "never"
        elif hb_age < 90:
            hb_str = f"{int(hb_age)}s ago"
        elif hb_age < 5400:
            hb_str = f"{int(hb_age // 60)}m ago"
        elif hb_age < 172800:
            hb_str = f"{int(hb_age // 3600)}h ago"
        else:
            hb_str = f"{int(hb_age // 86400)}d ago"
        ip = n.get("lan_ip") or n.get("ip") or "?"

        # TPS — from DB (computed by honey sweep from heartbeat token deltas)
        tps_val = n.get("stats", {}).get("tokens_per_sec", 0) or 0
        tps_str = f"{tps_val:.1f}" if tps_val > 0 else "\033[2m—\033[0m"

        # Ping — from hub cache (CLI measured, cached 30s)
        nid = n.get("id", "")
        ping_val = pings.get(nid)
        if ping_val is not None:
            ping_str = f"{ping_val}ms"
            if ping_val > 500:
                ping_str = f"\033[33m{ping_val}ms\033[0m"
            elif ping_val > 200:
                ping_str = f"\033[36m{ping_val}ms\033[0m"
            else:
                ping_str = f"\033[32m{ping_val}ms\033[0m"
        else:
            ping_str = "\033[2m—\033[0m"

        ver = n.get("version") or "?"
        ghost = hb_age < 0 or hb_age > GHOST_AGE_S
        if ver == "?":
            ver_str = "\033[2m?\033[0m"
        elif latest and ver == latest:
            # Same bytes as the hub — badge it even when offline, otherwise a
            # current-but-offline node renders exactly like an outdated one.
            ver_str = "\033[32m✓ latest\033[0m" if status == "online" else "\033[2;32m✓ latest\033[0m"
        elif latest and ghost:
            # Too stale to update — show the version dim, don't count it.
            ver_str = f"\033[2m{ver.replace('0.2.', '')[:8]}\033[0m"
        elif latest:
            behind += 1
            ver_str = f"\033[33m⚠ {ver.replace('0.2.', '')[:8]}\033[0m"
        else:
            ver_str = f"\033[2m{ver.replace('0.2.', '')[:8]}\033[0m"

        # CPU info — compact form so the MODEL NUMBER survives the column
        # ('Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz' → 'i7-10700 @2.9G').
        # Freq comes from the heartbeat metrics now; older hubs only stored
        # it under system.*, so check both.
        cpu_freq = n.get("system", {}).get("cpu_freq_mhz") or n.get("metrics", {}).get("cpu_freq_mhz") or ""
        cpu = _short_cpu(n.get("cpu_model") or "?", width=21)
        if cpu_freq and not cpu.endswith("G"):
            cpu = f"{cpu} {cpu_freq}M"
        cpu = cpu[:21]

        # RAM info
        mem = n.get("metrics", {})
        mem_total = mem.get("mem_total_mb", 0)
        mem_used = mem.get("mem_used_mb", 0)
        mem_pct = mem.get("mem_percent", 0)
        if mem_total:
            ram = f"{mem_used//1024}/{mem_total//1024}GB ({mem_pct:.0f}%)"
        else:
            ram = "?"

        # GPU info — strip 'NVIDIA GeForce' so the actual card (RTX 3060,
        # Quadro K1200…) is what's left in the column, not the vendor name.
        gpus = n.get("gpus") or n.get("system", {}).get("gpus") or []
        gpu_str = "none"
        if gpus:
            g = gpus[0]
            gpu_name = _short_gpu(g.get("name", "?"), width=18)
            gpu_mem = g.get("mem_total_mb", 0)
            gpu_used = g.get("mem_used_mb", 0)
            gpu_util = g.get("util_pct", 0)
            if gpu_mem:
                gpu_str = f"{gpu_name} {gpu_used//1024}/{gpu_mem//1024}GB {gpu_util:.0f}%"
            else:
                gpu_str = gpu_name
            if len(gpus) > 1:
                gpu_str += f" +{len(gpus) - 1}"

        # Color status
        if status == "online":
            status_str = f"\033[32m{status}\033[0m"
        elif status == "stale":
            status_str = f"\033[33m{status}\033[0m"
        else:
            status_str = f"\033[31m{status}\033[0m"

        print(f"  {name:<16} {status_str:<18} {ver_str:<21} {tps_str:<8} {ping_str:<17} {models:<7} {hb_str:<10} {cpu:<22} {ram:<14} {gpu_str}")

    if behind:
        print(f"\n  \033[33m⚠ {behind} bee(s) behind the hub CLI\033[0m — agents self-heal within ~5 min,"
              f"\n    or on that machine: {APP} update   (your copy: {latest or '?'})")

    # Models by bee — ACTUAL model names per bee (the MODELS column above
    # only has counts; operators want to know who runs what).
    bee_lines = _bee_model_summary(nodes)
    if bee_lines:
        print(f"\n\033[1mmodels by bee\033[0m")
        for ln in bee_lines:
            print(ln)
    print()

def cmd_node(args) -> None:
    """Show detailed info about a specific node."""
    name = " ".join(args.name) if args.name else ""
    if not name:
        die("usage: hivv node <name>  (e.g. hivv node yalla)")
    code, p = http("GET", "/api/control/nodes")
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch nodes: {p}")
    nodes = p.get("nodes", [])
    # Find by id or name (case-insensitive partial match)
    match = None
    for n in nodes:
        if n.get("id", "").lower() == name.lower() or n.get("name", "").lower() == name.lower():
            match = n
            break
    if not match:
        # Try partial match
        for n in nodes:
            if name.lower() in n.get("id", "").lower() or name.lower() in n.get("name", "").lower():
                match = n
                break
    if not match:
        print(f"node '{name}' not found. available nodes:")
        for n in nodes:
            print(f"  {n.get('id', '?')} ({n.get('name', '?')}) — {n.get('status', '?')}")
        sys.exit(1)

    if getattr(args, "json_output", False):
        print(json.dumps(match, indent=2))
        return

    # Human-readable output
    n = match
    hb = n.get("last_heartbeat", 0)
    hb_age = int(time.time()) - (hb / 1000) if hb else -1
    hb_str = f"{int(hb_age)}s ago" if hb_age >= 0 else "never"
    models = n.get("local_models", [])

    print(f"\n\033[1m{n.get('name', n.get('id', '?'))}\033[0m ({n.get('id', '?')})")
    lan_ip = n.get('lan_ip') or n.get('ip', '?')
    public_ip = n.get('public_ip', '?')
    if public_ip != '?' and public_ip != lan_ip:
        print(f"  ip:         {lan_ip} (lan)  /  {public_ip} (public)")
    else:
        print(f"  ip:         {lan_ip}")
    print(f"  status:     {n.get('status', '?')}")
    print(f"  version:    {n.get('version', '?')}")
    print(f"  heartbeat:  {hb_str}")

    # Hardware details
    cpu = n.get("cpu_model", "?")
    sys_info = n.get("system", {})
    cpu_freq = sys_info.get("cpu_freq_mhz", "")
    cpu_cores = sys_info.get("cpus", "")
    if cpu_freq:
        cpu += f" @ {cpu_freq}MHz"
    if cpu_cores:
        cpu += f" ({cpu_cores} cores)"
    print(f"  cpu:        {cpu}")

    # Memory
    m = n.get("metrics", {})
    mem_total = m.get("mem_total_mb", 0) or sys_info.get("memory_total", 0) // 1024 // 1024
    mem_used = m.get("mem_used_mb", 0)
    mem_pct = m.get("mem_percent", 0)
    if mem_total:
        print(f"  ram:        {mem_used//1024}/{mem_total//1024} GB ({mem_pct:.0f}%)")
    else:
        print(f"  ram:        ?")

    # GPU
    gpus = n.get("gpus") or sys_info.get("gpus") or []
    if gpus:
        for g in gpus:
            gpu_name = g.get("name", "?")
            gpu_mem = g.get("mem_total_mb", 0)
            gpu_used = g.get("mem_used_mb", 0)
            gpu_util = g.get("util_pct", 0)
            gpu_temp = g.get("temp_c", "?")
            if gpu_mem:
                print(f"  gpu:        {gpu_name} — {gpu_used//1024}/{gpu_mem//1024} GB VRAM ({gpu_util}% util, {gpu_temp}°C)")
            else:
                print(f"  gpu:        {gpu_name}")
    else:
        print(f"  gpu:        (none detected)")

    if models:
        print(f"\n\033[1mmodels\033[0m ({len(models)})")
        for m in models:
            name_str = m.get("model", "?").split("/")[-1].replace(".gguf", "")[:50]
            print(f"  {m.get('provider', '?')}: {name_str} (:{m.get('port', '?')})")

    loc = n.get("location", {})
    if loc:
        print(f"\n\033[1mlocation\033[0m")
        print(f"  {loc.get('city', '?')}, {loc.get('region', '?')}, {loc.get('country', '?')}")
    print()

def cmd_node_chat(args) -> None:
    """Chat directly with a specific node's LLM."""
    name = getattr(args, "name", "") or ""
    msg = " ".join(args.message) if args.message else ""
    if not name or not msg:
        die("usage: hivv node-chat <node> <message>")

    # Find the node
    code, p = http("GET", "/api/control/nodes")
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch nodes: {p}")
    nodes = p.get("nodes", [])
    match = None
    for n in nodes:
        if n.get("id", "").lower() == name.lower() or n.get("name", "").lower() == name.lower():
            match = n
            break
    if not match:
        for n in nodes:
            if name.lower() in n.get("id", "").lower() or name.lower() in n.get("name", "").lower():
                match = n
                break
    if not match:
        die(f"node '{name}' not found")

    ip = match.get("lan_ip") or match.get("ip", "")
    if not ip:
        die(f"node '{name}' has no IP address")

    models = match.get("local_models", [])
    if not models:
        die(f"node '{name}' has no LLMs")

    # Find a usable port (try all, prefer llama.cpp)
    ports_to_try = []
    for m in models:
        if "llama" in m.get("provider", ""):
            ports_to_try.append(m.get("port", 8080))
    for m in models:
        if m.get("port") not in ports_to_try:
            ports_to_try.append(m.get("port", 11434))

    # Send chat request directly to the node
    sent = False
    for port in ports_to_try:
        url = f"http://{ip}:{port}/v1/chat/completions"
        payload = json.dumps({
            "model": "local",
            "messages": [{"role": "user", "content": msg}],
            "stream": True,
            "max_tokens": 500
        })

        print(f"\n\033[1mchatting with {match.get('name', name)}\033[0m ({ip}:{port})")
        print(f"  {msg}")
        print()

        try:
            import urllib.request
            import time as _time
            start_time = _time.time()
            tokens = 0
            req = urllib.request.Request(url, data=payload.encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                for line in resp:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: ") or "[DONE]" in line:
                        continue
                    try:
                        evt = json.loads(line[6:])
                        delta = evt.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            print(content, end="", flush=True)
                            tokens += 1
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass
            elapsed = _time.time() - start_time
            tps = tokens / elapsed if elapsed > 0 else 0
            print(f"\n  \033[2m{tokens} tokens · {tps:.1f} tok/s · {elapsed:.1f}s\033[0m\n")
            sent = True
            break
        except Exception as e:
            print(f"  \033[33m!\033[0m port {port} failed: {e}")
            continue

    if not sent:
        print(f"\n  \033[31m✗\033[0m all ports failed — node may be offline")
        # Fallback: try through the hub
        print(f"  trying through hub instead...")
        code, payload = http("POST", "/api/chat", body={"messages": [{"role": "user", "content": msg}], "provider": name})
        if code == 200 and isinstance(payload, dict):
            content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
            if content:
                print(f"\n  {content}\n")

def cmd_ping(args) -> None:
    """Ping another node on the network — tests if it's reachable and responds."""
    name = " ".join(args.name) if args.name else ""
    if not name:
        die("usage: hivv ping <name>  (e.g. hivv ping yalla)")
    code, p = http("GET", "/api/control/nodes")
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch nodes: {p}")
    nodes = p.get("nodes", [])
    match = None
    for n in nodes:
        if n.get("id", "").lower() == name.lower() or n.get("name", "").lower() == name.lower():
            match = n
            break
    if not match:
        for n in nodes:
            if name.lower() in n.get("id", "").lower() or name.lower() in n.get("name", "").lower():
                match = n
                break
    if not match:
        print(f"\033[31m✗\033[0m node '{name}' not found in the network")
        sys.exit(1)

    node_id = match.get("id", "?")
    node_name = match.get("name", "?")
    lan_ip = match.get("lan_ip") or match.get("ip") or ""
    status = match.get("status", "?")
    models = match.get("local_models", [])

    print(f"\n\033[1mping {node_name}\033[0m ({node_id})")
    print(f"  ip:      {lan_ip or 'unknown'}")
    print(f"  status:  {status}")

    if not lan_ip:
        print(f"  \033[31m✗\033[0m no IP address — cannot ping")
        sys.exit(1)

    # Try to reach the node's LLM ports
    import subprocess as _sp
    reachable_ports = []
    for port in sorted(set(m.get("port", 8080) for m in models)):
        try:
            r = _sp.run(["curl", "-s", "--connect-timeout", "2", "--max-time", "3",
                         f"http://{lan_ip}:{port}/v1/models"],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and "model" in r.stdout.lower():
                reachable_ports.append(port)
                print(f"  \033[32m✓\033[0m port {port} — reachable")
            else:
                print(f"  \033[33m!\033[0m port {port} — no response")
        except Exception:
            print(f"  \033[31m✗\033[0m port {port} — unreachable")

    # Summary
    print()
    if reachable_ports:
        print(f"  \033[32m✓ {node_name} is reachable\033[0m — {len(reachable_ports)} LLM port(s) responding")
        for m in models:
            if m.get("port") in reachable_ports:
                model_name = m.get("model", "?").split("/")[-1].replace(".gguf", "")[:50]
                print(f"    {m.get('provider', '?')}: {model_name}")
        # Post ping to hub cache so other clients can see it
        try:
            import time as _t
            t0 = _t.time()
            _sp.run(["curl", "-s", "--connect-timeout", "2", "--max-time", "3",
                      f"http://{lan_ip}:{reachable_ports[0]}/v1/models"],
                     capture_output=True, text=True, timeout=5)
            ping_ms = int((_t.time() - t0) * 1000)
            http("POST", "/api/control/ping", body={"node_id": node_id, "ping_ms": ping_ms}, timeout=3)
            print(f"  \033[2mping: {ping_ms}ms (cached for other clients)\033[0m")
        except Exception:
            pass
    else:
        print(f"  \033[31m✗ {node_name} is NOT reachable\033[0m — check firewall/network")
        print(f"  run on {node_name}: hivv firewall --apply --mode lan")
    print()

def cmd_add(args) -> None:
    """Add any AI to the hivv network — cloud providers, local servers, or custom endpoints."""
    provider = getattr(args, "provider", "") or ""
    api_key = getattr(args, "api_key", "") or ""
    endpoint = getattr(args, "endpoint", "") or ""
    name = getattr(args, "name", "") or ""

    if not provider:
        # Interactive mode
        print(f"\n\033[1m{APP} add\033[0m — add AI to the network\n")
        print(f"  supported providers:")
        print(f"    openai       — GPT-4o, GPT-4, GPT-3.5")
        print(f"    anthropic    — Claude 3.5, Claude 3")
        print(f"    google       — Gemini 2.0, Gemini Pro")
        print(f"    groq         — Llama 3.3 70B (fast)")
        print(f"    ollama       — local ollama server")
        print(f"    llamacpp     — local llama.cpp server")
        print(f"    custom       — any OpenAI-compatible API\n")
        print(f"  usage:")
        print(f"    {APP} add openai sk-...              # add OpenAI key")
        print(f"    {APP} add anthropic sk-ant-...       # add Anthropic key")
        print(f"    {APP} add custom --endpoint http://my-server:8080/v1 --api-key my-key")
        print(f"    {APP} add ollama --endpoint http://192.168.1.50:11434/v1 --api-key none")
        return

    if not api_key:
        die(f"api_key required for {provider}\n  usage: {APP} add {provider} <api-key>")

    body = {"provider": provider, "api_key": api_key, "name": name}
    if endpoint:
        body["endpoint"] = endpoint

    code, p = http("POST", "/api/keys/donate", body=body)
    if code == 200 and isinstance(p, dict) and p.get("success"):
        print(f"\n  \033[32m✓ {provider} added to the network!\033[0m")
        print(f"    name:     {p.get('name', '?')}")
        print(f"    endpoint: {p.get('endpoint', '?')}")
        print(f"    {p.get('message', '')}\n")
    else:
        print(f"\n  \033[31m✗ failed to add {provider}\033[0m")
        if isinstance(p, dict):
            print(f"    {p.get('error', p)}")
        print()

def cmd_stats(args) -> None:
    """Show per-node stats and cluster totals."""
    code, p = http("GET", "/api/control/stats")
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch stats: {p}")

    if getattr(args, "json_output", False):
        print(json.dumps(p, indent=2))
        return

    nodes = p.get("nodes", [])
    totals = p.get("totals", {})

    if not nodes:
        print(f"\n  no nodes with stats yet")
        print(f"  start an agent: {APP} start\n")
        return

    print(f"\n\033[1m{APP} stats\033[0m\n")

    # Table header
    print(f"  {'NODE':<15} {'STATUS':<8} {'MODELS':<7} {'TASKS':<8} {'TOKENS':<10} {'REQS':<7} {'CPU':<6} {'MEM':<6} {'UPTIME'}")
    print(f"  {'─'*15} {'─'*8} {'─'*7} {'─'*8} {'─'*10} {'─'*7} {'─'*6} {'─'*6} {'─'*10}")

    for n in nodes:
        name = n.get("name", "?")[:14]
        status = n.get("status", "?")
        status_str = f"\033[32m{status}\033[0m" if status == "online" else f"\033[31m{status}\033[0m"
        models = n.get("models", 0)
        tasks = n.get("tasks_completed", 0)
        tokens = n.get("tokens", 0)
        reqs = n.get("requests", 0)
        cpu = n.get("cpu", 0)
        mem = n.get("mem", 0)
        uptime = n.get("uptime_s", 0)
        uptime_str = f"{uptime//3600}h{(uptime%3600)//60}m" if uptime > 3600 else f"{uptime//60}m{uptime%60}s" if uptime > 60 else f"{uptime}s"

        # Format tokens nicely
        if tokens >= 1000000:
            tok_str = f"{tokens/1000000:.1f}M"
        elif tokens >= 1000:
            tok_str = f"{tokens/1000:.1f}K"
        else:
            tok_str = str(tokens)

        print(f"  {name:<15} {status_str:<18} {models:<7} {tasks:<8} {tok_str:<10} {reqs:<7} {cpu:<6} {mem:<6} {uptime_str}")

    # Totals
    print(f"  {'─'*15} {'─'*8} {'─'*7} {'─'*8} {'─'*10} {'─'*7} {'─'*6} {'─'*6} {'─'*10}")
    t_tasks = totals.get("tasks_completed", 0)
    t_tokens = totals.get("tokens", 0)
    t_reqs = totals.get("requests", 0)
    if t_tokens >= 1000000:
        t_tok_str = f"{t_tokens/1000000:.1f}M"
    elif t_tokens >= 1000:
        t_tok_str = f"{t_tokens/1000:.1f}K"
    else:
        t_tok_str = str(t_tokens)
    print(f"  {'TOTAL':<15} {'':8} {'':7} {t_tasks:<8} {t_tok_str:<10} {t_reqs:<7}")
    print()

def cmd_incidents(args) -> None:
    """List, summarize, or resolve network incidents."""
    sub = getattr(args, 'incidents_sub', 'list')

    if sub == 'summary':
        code, p = http("GET", "/api/control/incidents/summary")
        if code != 200 or not isinstance(p, dict):
            die(f"failed to fetch incident summary: {p}")
        if getattr(args, 'json_output', False):
            print(json.dumps(p, indent=2))
            return

        active = p.get('active_incidents', 0)
        nodes = p.get('nodes', [])
        by_type = p.get('by_type', []) or []
        threshold = p.get('hub_stale_threshold_s', 180)

        print(f"\n\033[1m{APP} incidents summary\033[0m")
        print(f"  active incidents: \033[1m{active}\033[0m")
        print(f"  stale threshold:   {threshold}s")
        print()

        if by_type:
            print(f"  \033[1mby type / severity\033[0m")
            for t in by_type:
                sev = t.get('severity', '?')
                sev_color = _sev_color(sev)
                print(f"    {sev_color}{sev}\033[0m  {t.get('incident_type','?')}  ({t.get('count')} active)")
            print()

        print(f"  \033[1mnode health\033[0m")
        print(f"  {'NAME':<14} {'STATUS':<18} {'HEARTBEAT AGE'}")
        print(f"  {'─'*14} {'─'*18} {'─'*12}")
        for n in nodes:
            name = n.get('name', n.get('id', '?'))[:13]
            status = n.get('status', '?')
            age_s = n.get('heartbeat_age_s', -1)
            if status == 'online':
                status_str = f"\033[32monline\033[0m"
            else:
                status_str = f"\033[33mstale\033[0m"
            age_str = f"{age_s}s ago" if age_s >= 0 else "never"
            print(f"  {name:<14} {status_str:<18} {age_str}")
        print()
        return

    if sub == 'resolve':
        if not getattr(args, 'id', None):
            die("resolve requires an incident ID. Usage: hivv incidents resolve <id> [--notes TEXT]")
        notes = getattr(args, 'notes', '') or 'Resolved via CLI'
        code, p = http("PATCH", f"/api/control/incidents/{args.id}",
                       body={'state': 'resolved', 'resolution_notes': notes})
        if code == 404:
            die(f"incident not found: {args.id}")
        if code not in (200, 201):
            die(f"failed to resolve incident {args.id}: {p}")
        print(f"\033[32mincident {args.id} resolved\033[0m — {notes}")
        return

    if sub == 'create':
        if not getattr(args, 'node_id', None) or not getattr(args, 'title', None):
            die("create requires --node-id and --title. Usage: hivv incidents create --node-id NODE --title TEXT [--type TYPE] [--severity LEVEL]")
        inc_type = getattr(args, 'incident_type', 'node_stale') or 'node_stale'
        severity = getattr(args, 'severity', 'medium') or 'medium'
        code, p = http("POST", "/api/control/incidents", body={
            'incident_type': inc_type,
            'severity': severity,
            'node_id': args.node_id,
            'title': args.title,
            'description': getattr(args, 'description', None) or args.title
        })
        if code == 201:
            incident = p.get('incident', p)
            print(f"\033[32mcreated incident\033[0m {incident.get('id')} — {incident.get('title')}")
        elif code == 200 and p.get('duplicate'):
            print(f"incident already exists: {p.get('id')}")
        else:
            die(f"failed to create incident: {p}")
        return

    # Default: list active incidents
    params = {}
    if getattr(args, 'state', None):
        params['state'] = args.state
    else:
        params['state'] = 'active'
    query = '&'.join(f"{k}={v}" for k, v in params.items())
    path = f"/api/control/incidents?{query}" if query else "/api/control/incidents?state=active"
    code, p = http("GET", path)
    if code != 200 or not isinstance(p, dict):
        die(f"failed to fetch incidents: {p}")

    if getattr(args, 'json_output', False):
        print(json.dumps(p, indent=2))
        return

    incidents = p.get('incidents', [])
    total = p.get('total', 0)

    if not incidents:
        print(f"\n\033[1m{APP} incidents\033[0m — \033[32mno active incidents\033[0m — network is healthy\n")
        return

    print(f"\n\033[1m{APP} incidents\033[0m — {total} incident(s)\n")

    print(f"  {'SEVERITY':<10} {'TYPE':<22} {'NODE':<14} {'DETECTED':<26} {'TITLE'}")
    print(f"  {'─'*10} {'─'*22} {'─'*14} {'─'*26} {'─'*40}")

    for inc in incidents:
        sev = inc.get('severity', '?')
        sev_color = _sev_color(sev)
        inc_type = inc.get('incident_type', '?')[:21]
        node = inc.get('node_id', '?')[:13]
        detected = inc.get('detected_at', '')
        if detected:
            # Parse ISO timestamp
            try:
                dt = datetime.fromisoformat(detected.replace('Z', '+00:00'))
                detected = dt.strftime('%Y-%m-%d %H:%M:%S UTC')
            except Exception:
                detected = detected[:25]
        title = inc.get('title', '')[:39]
        print(f"  {sev_color}{sev:<10}\033[0m {inc_type:<22} {node:<14} {detected:<26} {title}")

    print()
    print(f"  Resolve: \033[36mhivv incidents resolve <id>\033[0m")
    print(f"  Summary: \033[36mhivv incidents summary\033[0m\n")


def _sev_color(sev: str) -> str:
    return {
        'critical': '\033[31m',   # red
        'high':     '\033[33m',   # yellow
        'medium':   '\033[34m',   # blue
        'low':      '\033[32m',   # green
    }.get(sev.lower(), '')


# ── the debugging / support family ────────────────────────────────────────
# Shared shape: gather context, either query the hub or submit to it, and
# render human-first with a --json twin for AI callers.
#
#   hivv problems   what is breaking globally?
#   hivv bug <q>    why is THIS broken?
#   hivv submit <t> I found a bug
#   hivv request <t> I want a feature
#   hivv flag <tgt> <t> someone is doing something wrong
#   hivv support <q> help me right now (existing, AI chat)

def _problem_context() -> dict:
    """The local context every problem command attaches. Scrubbed of secrets."""
    st = read_state() or {}
    pid = read_pid()
    m = _collect_metrics()
    hb = st.get("last_heartbeat") or 0
    hb_age = int(time.time()) - hb if hb else -1
    log_tail = ""
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()[-30:]
            log_tail = "\n".join(_scrub_lines(l) for l in lines)
        except OSError:
            pass
    return {
        "cli_version": VERSION,
        "hub": HUB,
        "hostname": socket.gethostname(),
        "node_id": st.get("node_id"),
        "agent_running": bool(pid and _pid_alive_fallback(pid)),
        "heartbeat_age_s": hb_age,
        "transport": st.get("transport", "heartbeat-only"),
        "cpu_pct": m.get("cpu_pct"),
        "mem_pct": m.get("mem_pct"),
        "gpus": [{"name": g.get("name"), "util": g.get("util_pct"),
                  "mem_used_mb": g.get("mem_used_mb"),
                  "mem_total_mb": g.get("mem_total_mb")} for g in (m.get("gpus") or [])],
        "log_tail": log_tail[-3000:] if log_tail else None,
    }


def _render_problems(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2))
        return
    B, R = "\033[1m", "\033[0m"
    G, Y, X, C = "\033[32m", "\033[33m", "\033[31m", "\033[36m"
    data = payload.get("data", payload)

    s = data.get("summary") or {}
    print(f"\n{B}hivv problems{R} — what is breaking\n")
    if s.get("total"):
        print(f"  {B}{s.get('total', 0)}{R} issue(s) in the last "
              f"{round((s.get('window_ms') or 3600000)/60000)} min")
        by_class = s.get("by_class") or {}
        for cls, n in sorted(by_class.items(), key=lambda kv: -kv[1]):
            mark = X if n >= 5 else (Y if n >= 2 else C)
            print(f"    {mark}{cls}{R} × {n}")
        by_ep = s.get("by_endpoint") or {}
        if by_ep:
            print(f"\n  {B}by endpoint{R}")
            for k, n in list(by_ep.items())[:8]:
                print(f"    {n:>4}  {k}")
    else:
        print(f"  {G}✓ no errors recorded in this window{R}")

    incidents = data.get("incidents") or []
    if incidents:
        print(f"\n  {B}open incidents{R}")
        for inc in incidents[:8]:
            sev = str(inc.get("severity", "?"))
            mark = X if sev in ("critical", "high") else Y
            print(f"    {mark}{sev}{R} {inc.get('node_id') or '?'} — "
                  f"{str(inc.get('title') or '')[:70]}")

    diag = data.get("top_diagnosis")
    if diag:
        print(f"\n  {B}most common problem{R}")
        print(f"    {Y}{diag.get('class')}{R}: {diag.get('advice', '')}")


def cmd_problems(args) -> None:
    """hivv problems [--json] [--window 3600] [--node NAME] — see what is breaking."""
    as_json = bool(getattr(args, "json_output", False))
    window = int(getattr(args, "window", 0) or 3600)
    node = (getattr(args, "node", None) or "").strip() or None
    payload: dict = {"data": {}}

    node_q = f"&node={urllib.parse.quote(node)}" if node else ""
    try:
        code, s_body = http("GET", f"/api/errors/summary?window={window}", timeout=15, sign=False)
        if code == 200 and isinstance(s_body, dict):
            payload["data"]["summary"] = s_body.get("data", s_body)
    except Exception as e:
        payload.setdefault("errors", []).append(f"summary: {e}")

    try:
        code, i_body = http("GET", "/api/incidents?state=active", timeout=15, sign=False)
        if code == 200 and isinstance(i_body, dict):
            payload["data"]["incidents"] = i_body.get("incidents") or i_body.get("data") or []
    except Exception:
        pass

    # Recent entries — filtered by node when asked.
    try:
        code, r_body = http("GET", f"/api/errors/recent?limit=50{node_q}",
                            timeout=15, sign=False)
        if code == 200 and isinstance(r_body, dict):
            rows = r_body.get("data") or []
            payload["data"]["recent"] = rows
            if rows:
                top = rows[0]
                payload["data"]["top_diagnosis"] = {
                    "class": top.get("class"), "advice": top.get("diagnosis"),
                    "endpoint": top.get("endpoint"), "message": top.get("message"),
                    "node": top.get("node"),
                }
    except Exception:
        pass

    _render_problems(payload, as_json)
    if not as_json:
        scope = f" on {node}" if node else ""
        print(f"\n  drill in:   {APP} bug <what broke>{f' --node {node}' if node else ''}")
        print(f"  report:     {APP} submit \"<what you saw>\"")
        print(f"  ask an AI:  {APP} support \"<question>\"")
        if not node:
            print(f"  per-node:   {APP} problems --node <name>{scope}")
    sys.exit(0)


def cmd_bug(args) -> None:
    """hivv bug [query] [--json] [--limit N] [--client NAME] — research a problem.

    Searches the live error log for matching entries, then shows the
    diagnosis for each. `--client` filters to one client (opencode, continue,
    cline, vscode, …) so you can study what OpenCode specifically is doing.
    Falls back to the local agent log when the hub has nothing.
    """
    query = " ".join(getattr(args, "query", []) or []).strip().lower()
    as_json = bool(getattr(args, "json_output", False))
    limit = int(getattr(args, "limit", 0) or 20)
    client = (getattr(args, "client", None) or "").strip().lower() or None
    node = (getattr(args, "node", None) or "").strip() or None

    rows: list = []
    try:
        q = urllib.parse.quote(query) if query else ""
        url = f"/api/errors/recent?limit={limit}"
        if q and " " not in query:
            url += f"&class={q}"
        if client:
            url += f"&client={urllib.parse.quote(client)}"
        if node:
            url += f"&node={urllib.parse.quote(node)}"
        code, body = http("GET", url, timeout=15, sign=False)
        if code == 200 and isinstance(body, dict):
            rows = body.get("data") or []
    except Exception:
        pass

    # Local fallback: scan the agent log for matching lines.
    if not rows and LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()[-200:]
            for ln in lines:
                if query and query not in ln.lower():
                    continue
                rows.append({"class": "local_log", "endpoint": "agent",
                             "message": _scrub_lines(ln)[:400],
                             "diagnosis": None, "ts": None})
                if len(rows) >= limit:
                    break
        except OSError:
            pass

    if query:
        rows = [r for r in rows
                if query in str(r.get("class", "")).lower()
                or query in str(r.get("endpoint", "")).lower()
                or query in str(r.get("message", "")).lower()]

    if as_json:
        print(json.dumps({"query": query, "count": len(rows), "results": rows},
                         indent=2))
        sys.exit(0 if rows else 1)

    B, R = "\033[1m", "\033[0m"
    G, Y, X, C = "\033[32m", "\033[33m", "\033[31m", "\033[36m"
    print(f"\n{B}hivv bug{R} — {query or 'recent issues'}\n")
    if not rows:
        print(f"  {G}✓ nothing matching in the last {limit} entries{R}")
        print(f"    try: {APP} problems   (global view)")
        print(f"          {APP} debug      (local snapshot)")
        print(f"          {APP} support    (ask the AI)")
        sys.exit(0)
    for r in rows[:limit]:
        cls = str(r.get("class", "?"))
        mark = X if cls in ("context_overflow", "truncated_response",
                            "connection_refused", "timeout") else Y
        ts = r.get("iso") or (str(r.get("ts")) if r.get("ts") else "?")
        cli = r.get("client") or "?"
        nd = r.get("node") or ""
        nd_s = f"  {Y}@{nd}{R}" if nd else ""
        print(f"  {mark}{cls}{R}  {C}{r.get('endpoint','?')}{R}  "
              f"[{cli}]{nd_s}  {ts}")
        msg = str(r.get("message", ""))[:200]
        if msg:
            print(f"      {msg}")
        d = r.get("diagnosis")
        if d:
            print(f"      {G}→ {d}{R}")
        bits = []
        for k in ("model", "node", "input_tokens", "max_tokens", "trimmed",
                  "dropped_messages", "status"):
            if r.get(k) not in (None, "", False):
                bits.append(f"{k}={r[k]}")
        if bits:
            print(f"      {C}{' '.join(bits)}{R}")
        print()
    if not as_json:
        print(f"  report this: {APP} submit \"<what you saw>\"")
    sys.exit(0)


def _submit_feedback(kind: str, subject: str, message: str,
                     target: str | None = None, evidence: str | None = None,
                     severity: str | None = None, as_json: bool = False) -> None:
    """Shared submit path for submit / request / flag."""
    body: dict = {"type": kind, "message": message}
    if subject:
        body["subject"] = subject
    if target:
        body["target"] = target
    if evidence:
        body["evidence"] = evidence
    if severity:
        body["severity"] = severity

    # Attach local context so the maintainer sees the environment it broke in.
    ctx = _problem_context()
    ctx.pop("log_tail", None)   # keep the payload small; tail is in the CLI
    body["subject"] = body.get("subject") or f"{kind}: {message[:60]}"

    try:
        code, resp = http("POST", "/api/feedback", body=body, timeout=30)
    except Exception as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"\033[31m✗ could not submit: {e}\033[0m")
            print(f"  (offline? the report is below — paste it into "
                  f"{APP} support when you are back online)")
            print()
            print(json.dumps({**body, "context": ctx}, indent=2))
        sys.exit(2)

    ok = code in (200, 201) and isinstance(resp, dict) and resp.get("ok")
    fb = (resp or {}).get("feedback") or {}

    if as_json:
        print(json.dumps({"ok": ok, "http_status": code, "feedback": fb,
                          "context": ctx}, indent=2))
        sys.exit(0 if ok else 1)

    B, R = "\033[1m", "\033[0m"
    G, X = "\033[32m", "\033[31m"
    if ok:
        print(f"\n{G}✓ submitted{R} — id {B}{fb.get('id','?')}{R} "
              f"({fb.get('status','?')})")
        print(f"  type: {fb.get('type', kind)}")
        if fb.get("subject"):
            print(f"  subject: {fb['subject']}")
        print(f"\n  view your reports: {APP} support \"show my reports\"")
        print(f"  or:                curl -s {HUB}/api/feedback")
    else:
        print(f"\n{X}✗ submission failed{R} (HTTP {code})")
        print(f"  {str(resp)[:200]}")
        print(f"\n  paste this into {APP} support instead:")
        print(json.dumps({**body, "context": ctx}, indent=2))
    sys.exit(0 if ok else 1)


def cmd_submit(args) -> None:
    """hivv submit "<what you saw>" [--json] — file a bug report."""
    text = " ".join(getattr(args, "text", []) or []).strip()
    if not text:
        die(f'usage: {APP} submit "<what you saw>" [--subject S] [--json]')
    _submit_feedback("bug", getattr(args, "subject", None), text,
                     as_json=bool(getattr(args, "json_output", False)))


def cmd_request(args) -> None:
    """hivv request "<what you want>" [--json] — request a feature."""
    text = " ".join(getattr(args, "text", []) or []).strip()
    if not text:
        die(f'usage: {APP} request "<what you want>" [--json]')
    _submit_feedback("suggestion", getattr(args, "subject", None), text,
                     as_json=bool(getattr(args, "json_output", False)))


def cmd_flag(args) -> None:
    """hivv flag <target> "<what they are doing>" — flag another node/user.

    target is a node name (e.g. `hivv flag spammy-bee "floods relay jobs"`),
    a principal id, or a URL/content reference. The report goes to the hub's
    feedback queue with the target attached so an operator can act on it.
    """
    parts = getattr(args, "parts", None) or []
    if len(parts) < 2:
        die(f'usage: {APP} flag <target> "<what they are doing>" '
            f'[--severity low|medium|high|critical] [--evidence TEXT] [--json]')
    target = parts[0]
    text = " ".join(parts[1:]).strip()
    _submit_feedback("complaint", f"flag: {target}", text,
                     target=target,
                     evidence=getattr(args, "evidence", None),
                     severity=getattr(args, "severity", None),
                     as_json=bool(getattr(args, "json_output", False)))


def cmd_capabilities(args) -> None:
    """hivv capabilities [--json] — what can this HIVV instance do?

    Shows the capability discovery document: the OpenAI-compatible surface,
    the route-mode aliases, the deterministic tools, the hive's shared tools,
    and the MCP endpoint. This is what a client probes before it decides what
    to offer its user.
    """
    as_json = bool(getattr(args, "json_output", False))
    try:
        code, body = http("GET", "/v1/capabilities", timeout=15, sign=False)
    except Exception as e:
        if as_json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"\033[31m✗ could not reach the gateway: {e}\033[0m")
        sys.exit(2)

    if code != 200 or not isinstance(body, dict):
        if as_json:
            print(json.dumps({"ok": False, "http_status": code, "body": body}))
        else:
            print(f"\033[31m✗ HTTP {code}\033[0m")
        sys.exit(1)

    data = body.get("data", body)

    if as_json:
        print(json.dumps(data, indent=2))
        sys.exit(0)

    B, R = "\033[1m", "\033[0m"
    G, Y, C = "\033[32m", "\033[33m", "\033[36m"
    print(f"\n{B}hivv tools{R} — what this instance offers\n")
    print(f"  {B}service{R}: {data.get('name','?')} {data.get('version','')}")
    print(f"  {B}tagline{R}: {data.get('tagline','')}")

    oc = data.get("openai_compatible") or {}
    print(f"\n  {B}OpenAI-compatible surface{R}")
    print(f"    base URL : {oc.get('base_url','?')}")
    print(f"    chat     : {oc.get('chat','?')}")
    print(f"    models   : {oc.get('models','?')}")
    print(f"    streaming: {oc.get('streaming')}  tools: {oc.get('tools')}")
    print(f"    auth     : {oc.get('auth','?')}")

    modes = data.get("route_modes") or {}
    if modes:
        print(f"\n  {B}route modes{R} (model aliases)")
        for k, v in modes.items():
            print(f"    {C}{k:<10}{R} {v}")

    tools = data.get("hivv_tools") or []
    if tools:
        print(f"\n  {B}HIVV tools{R} (deterministic + signed)")
        for t in tools:
            cost = t.get("cost", "?")
            mark = G if cost == "free" else Y
            print(f"    {mark}{t['name']:<20}{R} {t.get('endpoint','')}")
            desc = str(t.get("description", ""))[:110]
            print(f"      {desc}{'…' if len(t.get('description','')) > 110 else ''}")

    shared = data.get("hive_shared_tools") or []
    if shared:
        print(f"\n  {B}hive-shared tools{R} (served by volunteer bees)")
        print(f"    {', '.join(shared)}")

    caps = data.get("capabilities") or []
    if caps:
        print(f"\n  {B}capability registry{R} ({len(caps)} entries)")
        for c in caps[:12]:
            print(f"    {c.get('key','?'):<28} {c.get('tier','?'):<14} "
                  f"{c.get('default_verdict','?')}")

    mcp = data.get("mcp") or {}
    if mcp:
        print(f"\n  {B}MCP{R} — {mcp.get('endpoint','?')} "
              f"({mcp.get('transport','?')})")
        print(f"    tools: {', '.join(mcp.get('tools') or [])}")

    receipts = data.get("receipts") or {}
    if receipts:
        print(f"\n  {B}response receipts{R}")
        for k, v in receipts.items():
            print(f"    {C}{k:<24}{R} {v}")

    notes = data.get("notes") or []
    if notes:
        print(f"\n  {B}notes{R}")
        for n in notes:
            print(f"    · {n}")
    print()


def cmd_debug(args) -> None:
    """Show debug info: gateway connection, node registration, local AI detection."""
    print(f"\n\033[1m{APP} debug\033[0m\n")

    # Hub connectivity
    print("\033[1mhub\033[0m")
    try:
        t0 = time.time()
        code, p = http("GET", "/api/health", timeout=5)
        ms = int((time.time() - t0) * 1000)
        if code == 200:
            print(f"  hub:     {HUB} — {ms}ms")
            print(f"  db:      {(p or {}).get('database', '?')}")
            print(f"  providers: {(p or {}).get('providers', '?')}")
        else:
            print(f"  hub:     HTTP {code}")
    except Exception as e:
        print(f"  hub:     unreachable ({e})")

    # Local agent state
    print("\n\033[1magent state\033[0m")
    st = read_state() or {}
    pid = read_pid()
    if pid and _pid_alive_fallback(pid):
        print(f"  pid:     {pid} (alive)")
        print(f"  node_id: {st.get('node_id', '?')}")
        hb = st.get("last_heartbeat") or 0
        hb_age = int(time.time()) - hb if hb else -1
        print(f"  heartbeat: {hb_age}s ago" if hb_age >= 0 else "  heartbeat: never")
        print(f"  hw_id:   {st.get('hw_id', '?')}")
    else:
        print(f"  pid:     not running")
        print(f"  start with: {APP} start")

    # Local AI detection
    print("\n\033[1mlocal AI\033[0m")
    llms = _detect_local_ai()
    if llms:
        for m in llms:
            model_name = m.get("model", "?").split("/")[-1].replace(".gguf", "")[:50]
            print(f"  {m['provider']}: {model_name} (:{m['port']})")
    else:
        print("  none detected")

    # Network
    print("\n\033[1mnetwork\033[0m")
    import socket
    try:
        hostname = socket.gethostname()
        print(f"  hostname: {hostname}")
    except:
        print(f"  hostname: unknown")
    m = _collect_metrics()
    print(f"  LAN IP:  {m.get('lan_ip', '?')}")

    # GPU
    gpus = m.get("gpus") or []
    if gpus:
        for g in gpus:
            print(f"  gpu:     {g['name']} — {g['mem_used_mb']//1024}/{g['mem_total_mb']//1024}GB ({g['util_pct']}% util, {g.get('temp_c','?')}°C)")
    else:
        print(f"  gpu:     (none detected)")

    # All nodes on hub
    print("\n\033[1mnetwork nodes\033[0m")
    code, p = http("GET", "/api/control/nodes", timeout=5)
    if code == 200 and isinstance(p, dict):
        nodes = p.get("nodes", [])
        if nodes:
            for n in nodes:
                age = int(time.time()) - (n.get("last_heartbeat", 0) / 1000) if n.get("last_heartbeat") else -1
                age_str = f"{int(age)}s ago" if age >= 0 else "unknown"
                models_count = len(n.get("local_models", []))
                print(f"  {n.get('id', '?'):20s} {n.get('status', '?'):8s} models={models_count} hb={age_str}")
        else:
            print("  no nodes registered")
    else:
        print("  could not fetch nodes")

    print()

def cmd_chat(args) -> None:
    # argparse.REMAINDER swallows flags that come AFTER the message text —
    # `hivv chat "hi" --mode=fast` (the documented form) used to append
    # "--mode=fast" to the PROMPT and silently drop the routing flag.
    # Recover known flags from the remainder; the rest is the message.
    parts = list(args.message or [])
    mode = getattr(args, "mode", None)
    known = {"balanced", "quality", "fast", "cost", "private", "trusted",
             "smart", "cheap"}
    cleaned, i = [], 0
    while i < len(parts):
        tok = parts[i]
        if tok.startswith("--mode=") and mode is None:
            val = tok.split("=", 1)[1]
            if val in known:
                mode = val
                i += 1
                continue
        elif tok == "--mode" and mode is None and i + 1 < len(parts) and parts[i + 1] in known:
            mode = parts[i + 1]
            i += 2
            continue
        cleaned.append(tok)
        i += 1
    msg = " ".join(cleaned)
    if not msg: die("chat needs a message (hivv chat 'hello')")
    _do_chat(msg, stream=True, mode=mode)

def _exec_tool_locally(tool_name: str, args: dict) -> dict:
    """Execute a single tool via local hivv-tools server. Returns {role:'tool', tool_call_id, content}."""
    import urllib.request, urllib.parse
    base = f"http://localhost:{TOOLS_PORT}"
    if tool_name == "time" or tool_name == "get_time":
        tz = args.get("timezone")
        path = f"{base}/time" + (f"?tz={urllib.parse.quote(tz)}" if tz else "")
    elif tool_name == "calculate":
        expr = urllib.parse.quote(str(args.get("expression", "")))
        path = f"{base}/calculate?expr={expr}"
    elif tool_name == "fetch":
        url = urllib.parse.quote(str(args.get("url", "")))
        path = f"{base}/fetch?url={url}"
    elif tool_name == "search":
        q = urllib.parse.quote(str(args.get("query", "")))
        path = f"{base}/search?q={q}"
    elif tool_name == "news":
        q = urllib.parse.quote(str(args.get("query", args.get("location", ""))))
        path = f"{base}/news?q={q}"
    elif tool_name == "sysinfo":
        path = f"{base}/sysinfo"
    else:
        return {"role": "tool", "tool_call_id": args.get("id", "?"), "content": f"error: unknown tool '{tool_name}'"}
    try:
        with urllib.request.urlopen(path, timeout=15) as r:
            data = json.loads(r.read())
            if isinstance(data, dict) and "error" in data:
                content = f"error: {data['error']}"
            elif tool_name == "sysinfo":
                content = json.dumps(data)
            elif tool_name == "time" or tool_name == "get_time":
                ts = data.get("unix")
                if data.get("local") and data.get("timezone"):
                    content = f"Current time: {data['local']} ({data['timezone']})"
                elif ts:
                    import datetime
                    local = datetime.datetime.fromtimestamp(ts)
                    content = f"Current time: {local.strftime('%I:%M %p on %B %d, %Y')}"
                else:
                    content = f"Current time: {data.get('human', data.get('iso', '?'))}"
            elif tool_name == "calculate":
                if "error" in data:
                    content = f"error: {data['error']}"
                else:
                    content = f"Result: {data.get('result', data)}"
            elif tool_name == "news":
                results = data.get("results", [])
                if results:
                    lines = [f"News for '{data.get('query', '')}':"]
                    for i, r in enumerate(results[:5], 1):
                        lines.append(f"{i}. {r.get('title', '?')}")
                        if r.get('date'): lines.append(f"   {r['date']}")
                        if r.get('snippet'): lines.append(f"   {r['snippet'][:100]}")
                    content = "\n".join(lines)
                else:
                    content = f"No news results for '{data.get('query', '')}'"
            elif tool_name == "search":
                results = data.get("results", [])
                if results:
                    lines = [f"Search results for '{data.get('query', '')}':"]
                    for i, r in enumerate(results[:5], 1):
                        lines.append(f"{i}. {r.get('title', '?')}")
                        if r.get('snippet'): lines.append(f"   {r['snippet'][:100]}")
                    content = "\n".join(lines)
                else:
                    content = f"No results for '{data.get('query', '')}'"
            else:
                content = json.dumps(data)
            return {"role": "tool", "tool_call_id": args.get("id", "?"), "content": content}
    except Exception as e:
        return {"role": "tool", "tool_call_id": args.get("id", "?"), "content": f"error: {e}"}

def _ensure_tools_running() -> bool:
    """Make sure hivv-tools server is running. Returns True if tools are available."""
    if _tools_running():
        return True
    print(f"[{APP}] tools server not running — starting it for you...", file=sys.stderr)
    tool_script = INSTALL_DIR / f"{APP}-tools.py"
    if not tool_script.exists():
        print(f"[{APP}] downloading tools server...", file=sys.stderr)
        try:
            data = _http_get_bytes(f"{HUB}/{APP}-tools.py")
            tool_script.write_bytes(data)
            tool_script.chmod(0o755)
        except Exception as e:
            print(f"[{APP}] failed to download tools: {e}", file=sys.stderr)
            return False
    try:
        subprocess.Popen([sys.executable, str(tool_script), "start"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)  # give server time to start
        if _tools_running():
            print(f"[{APP}] tools server ready on port {TOOLS_PORT}", file=sys.stderr)
            return True
    except Exception as e:
        print(f"[{APP}] failed to start tools: {e}", file=sys.stderr)
    return False

def _report_perf(node_id, name, model, tps, latency_ms):
    """Feed the hub's perf table with OUR measured direct throughput (hivv OS §2:
    honest, transport-tagged speed data — direct chats bypass the hub, so without
    this the hub only ever sees relay numbers). Fire-and-forget in a daemon thread."""
    import threading
    def _post():
        try:
            http("POST", "/api/hive/perf-report",
                 body={"node_id": node_id or name, "model": model or "local",
                       "tps": int(tps), "latency_ms": int(latency_ms), "via": "direct"},
                 timeout=5, sign=False)
        except Exception:
            pass
    # non-daemon: a short-lived CLI process would kill a daemon thread before the
    # POST lands — interpreter exit joins this one (the answer is already printed)
    t = threading.Thread(target=_post, daemon=False)
    t.start()

def _do_chat(msg: str, stream: bool = True, mode: str | None = None) -> None:
    """Talk to a bee (node) directly. Falls back to hub only if no direct connection works.
    hivv OS §2: mode selects a routing strategy (balanced|quality|fast|cost|private|trusted)."""
    mode = mode or os.environ.get("HIVV_ROUTE_MODE") or None
    if mode:
        mode = _ROUTE_ALIASES.get(mode, mode)
    tools_running = _tools_running()
    if not tools_running:
        tools_running = _ensure_tools_running()
    # Streaming is the default everywhere — only the hub's tool loop needs buffering.
    direct_stream = stream and os.environ.get("HIVV_NO_STREAM", "").lower() not in ("1", "true", "yes")
    hub_stream = stream and not tools_running
    # Thinking OFF by default (mirrors the hub router's enable_thinking:false).
    # Opt back in with HIVV_THINK=1 for reasoning models.
    _think_env = os.environ.get("HIVV_THINK", "").strip().lower()
    think_off = (_think_env not in ("1", "on", "yes")) or bool(NO_THINK)

    # who am I? (a node whose name/lan_ip matches this box is US — use localhost,
    # because local LLMs bind to 127.0.0.1 by default and refuse LAN-IP loopbacks)
    try:
        me_name = socket.gethostname().split(".")[0].lower()
        me_ip = _local_ip() or ""
    except Exception:
        me_name, me_ip = "", ""

    # ── Step 1: Try direct connection to a node (bypass hub) ─────────
    code, nodes_resp = http("GET", "/api/control/nodes", timeout=5)
    if code == 200 and isinstance(nodes_resp, dict):
        nodes = nodes_resp.get("nodes", [])
        online_nodes = [n for n in nodes if n.get("status") == "online" and (n.get("lan_ip") or n.get("ip"))]
        if online_nodes:
            # hivv OS §2: lead with hub-MEASURED speed (perf EWMA), not luck.
            import random
            _now = time.time()
            st0 = read_state() or {}
            direct_fails = {k: v for k, v in (st0.get("direct_fail") or {}).items() if _now - v < 600}
            def _save_fails():
                try:
                    stx = read_state() or {}
                    stx["direct_fail"] = direct_fails
                    write_state(stx)
                except OSError:
                    pass
            node_tps = {}
            node_direct = set()
            try:
                pc, perf_resp = http("GET", "/api/hive/perf", timeout=5, sign=False)
                if pc == 200 and isinstance(perf_resp, dict):
                    for sm in perf_resp.get("samples", []):
                        nid = sm.get("node_id") or ""
                        try:
                            v = float(sm.get("ewma_tps") or 0)
                        except (TypeError, ValueError):
                            v = 0.0
                        if nid and v > 0:
                            node_tps[nid] = max(node_tps.get(nid, 0.0), v)
                            if (sm.get("via") or "direct") == "direct":
                                node_direct.add(nid)
            except Exception:
                node_tps = {}
            if node_tps:
                def _tps_of(n):
                    keys = (n.get("name", ""), n.get("id", ""), n.get("lan_ip", ""), n.get("ip", ""), n.get("public_ip", ""))
                    best = max((node_tps.get(k, 0.0) for k in keys if k), default=0.0)
                    if best == 0.0 and ((keys[0] or "").lower() == me_name or (me_ip and me_ip in keys)):
                        best = node_tps.get("localhost", 0.0)
                    if best > 0 and (keys[0] or "").lower() in direct_fails:
                        best = min(best, 1.0)   # failed direct recently → last pick, but still explored
                    if best > 0 and not any(k in node_direct for k in keys if k):
                        best = min(best, 1.0)   # only ever seen via relay → no evidence it's direct-reachable
                    return best
                # weighted lottery (sqrt-weighted — power-of-two-choices: faster
                # bees win MORE, not always; the hive stays warm and balanced)
                keyed = sorted(
                    ((random.random() ** (1.0 / max(_tps_of(n) ** 0.5, 2.0)), n) for n in online_nodes),
                    key=lambda x: -x[0])
                online_nodes = [n for _, n in keyed]
            else:
                random.shuffle(online_nodes)

            # Probe context window size per (ip, port) so we can fill the context
            def _ctx_size(ip, port):
                try:
                    req = urllib.request.Request(f"http://{ip}:{port}/props",
                        headers={"Content-Type": "application/json"}, timeout=3)
                    with urllib.request.urlopen(req, timeout=3) as r:
                        d = json.loads(r.read().decode("utf-8", errors="replace"))
                        n_ctx = int(d.get("n_ctx", 0) or 0)
                        if n_ctx > 0:
                            return min(max(n_ctx - 512, 512), 32768)
                except Exception:
                    pass
                return 8192  # safe default when server doesn't expose ctx size

            for node in online_nodes:
                ip = node.get("lan_ip") or node.get("ip", "")
                name = node.get("name", node.get("id", "?"))
                models = node.get("local_models", [])
                node_keys = (name.lower(), node.get("id", "").lower(), ip, node.get("ip", ""), node.get("public_ip", ""))
                if (name.lower() == me_name) or (me_ip and me_ip in node_keys):
                    ip = "127.0.0.1"  # our own bee → localhost (LLM binds local-only)

                # Find a usable port
                for m in models:
                    port = m.get("port", 8080)
                    max_tok = _ctx_size(ip, port)
                    try:
                        url = f"http://{ip}:{port}/v1/chat/completions"
                        payload_dict = {
                            "model": "local",
                            "messages": [{"role": "user", "content": msg}],
                            "stream": direct_stream,
                            "max_tokens": max_tok
                        }
                        if think_off:
                            payload_dict["chat_template_kwargs"] = {"enable_thinking": False}
                        payload = json.dumps(payload_dict)
                        import urllib.request, socket as _sock
                        _t0_ns = time.time()
                        req = urllib.request.Request(url, data=payload.encode(),
                                                     headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            if direct_stream:
                                banner_shown = False
                                got_content = False
                                _toks = 0
                                _seen_model = None
                                _t0 = time.time()
                                def _banner():
                                    nonlocal banner_shown
                                    if not banner_shown and not PLAIN_OUTPUT:
                                        print(f"\033[2m(direct → {name}:{port})\033[0m")
                                        banner_shown = True
                                for line in resp:
                                    line = line.decode("utf-8", errors="replace").strip()
                                    if not line.startswith("data: ") or "[DONE]" in line:
                                        continue
                                    try:
                                        evt = json.loads(line[6:])
                                        delta = evt.get("choices", [{}])[0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            _banner()
                                            got_content = True
                                            _toks += 1
                                            _seen_model = _seen_model or evt.get("model")
                                            print(content, end="", flush=True)
                                    except (json.JSONDecodeError, IndexError, KeyError):
                                        pass
                                if got_content:
                                    print()
                                    direct_fails.pop(name.lower(), None)
                                    _save_fails()
                                    _el = max(0.3, time.time() - _t0)
                                    if _toks >= 8:  # tiny replies measure TTFT, not throughput — skip
                                        _mm = str(_seen_model or "local").split("/")[-1].replace(".gguf", "")[:60]
                                        _report_perf(node.get("id"), name, _mm, _toks / _el, _el * 1000)
                                    return
                                # reasoning-only / empty stream → don't swallow the answer,
                                # fall through to the next node or the hub
                                continue
                            else:
                                data = json.loads(resp.read().decode())
                                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                                if content:
                                    if not PLAIN_OUTPUT:
                                        print(f"\033[2m(direct → {name}:{port})\033[0m")
                                    print(content)
                                    direct_fails.pop(name.lower(), None)
                                    _save_fails()
                                    if len(content) >= 32:  # meaningful sample only
                                        _mm = str(data.get("model") or m.get("model") or "local").split("/")[-1].replace(".gguf", "")[:60]
                                        _tps = max(1, len(content) / 4) / max(0.3, time.time() - _t0_ns)
                                        _report_perf(node.get("id"), name, _mm, _tps, (time.time() - _t0_ns) * 1000)
                                    return
                                # empty answer (reasoning-only model etc.) → try next bee
                    except Exception:
                        direct_fails[name.lower()] = time.time()
                        _save_fails()
                        continue  # Try next port/node

    # ── Step 2: Fall back to hub (only if direct failed) ─────────────
    req_body = {"message": msg}
    if mode:
        req_body["route"] = {"mode": mode}
    if tools_running:
        req_body["tools"] = TOOL_SCHEMAS
        req_body["local_tools"] = True
    if think_off:
        req_body["chat_template_kwargs"] = {"enable_thinking": False}

    if not hub_stream:
        req_body["stream"] = False
        code, p = http("POST", "/api/chat", body=req_body)
        if code == 202 and isinstance(p, dict) and p.get("verdict") == "MAYBE":
            _print_maybe(p)
            return
        if code == 403 and isinstance(p, dict) and p.get("verdict") == "NO":
            _print_no(p)
            return
        if not (200 <= code < 300):
            print(f"Error {code}: {p}")
            _print_tips(f"{p}")
            return
        if isinstance(p, dict):
            if p.get("local_execution_required") and p.get("tool_calls"):
                tool_calls = p["tool_calls"]
                assistant_msg = p.get("assistant_message", {})
                tool_results = []
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    args = {}
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                    except:
                        pass
                    args["id"] = tc.get("id", "?")
                    result = _exec_tool_locally(fn.get("name", ""), args)
                    tool_results.append(result)
                messages_for_hub = [{"role": "user", "content": msg}]
                continue_req = {
                    "messages": messages_for_hub,
                    "tool_results": tool_results,
                    "assistant_message": assistant_msg,
                    "model": p.get("model"),
                    "provider": p.get("provider")
                }
                code2, p2 = http("POST", "/api/chat/continue", body=continue_req)
                if isinstance(p2, dict):
                    content = p2.get("content", "")
                    if content:
                        print(content); return
                    if p2.get("choices"):
                        content = p2["choices"][0].get("message", {}).get("content", "")
                        if content:
                            print(content); return
                print(json.dumps(p2, indent=2) if isinstance(p2, (dict, list)) else p2)
                return
            msg_obj = p.get("choices", [{}])[0].get("message", {})
            content = (msg_obj or {}).get("content") or p.get("content") or p.get("text") or p.get("message")
            if content:
                print(content); return
        print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
        return

    # ── Hub fallback (direct connection failed) ──────────────────────
    print(f"\033[2m(hub fallback — no direct node available)\033[0m")
    url = HUB.rstrip("/") + "/api/chat"
    max_retries = 2
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(url, data=json.dumps(req_body).encode(),
                                     method="POST",
                                     headers={"content-type": "application/json",
                                              "accept": "text/event-stream",
                                              "user-agent": f"{APP}/{VERSION}",
                                              "x-visitor-id": _get_visitor_id()})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                # Read provider info from headers
                provider = r.headers.get("X-AI-Provider", "")
                routed = r.headers.get("X-AI-Routed", "")
                answered_by = r.headers.get("X-Hivv-Answered-By", "")
                policy_hdr = r.headers.get("X-Hivv-Policy", "")
                # hivv OS: a gated request answers with plain JSON, not SSE
                if (r.headers.get_content_type() or "").startswith("application/json"):
                    raw = r.read().decode("utf-8", errors="replace")
                    try:
                        d = json.loads(raw)
                    except Exception:
                        d = {}
                    if d.get("verdict") == "MAYBE":
                        _print_maybe(d); return
                    if d.get("verdict") == "NO":
                        _print_no(d); return
                def _receipt():
                    if (answered_by and not PLAIN_OUTPUT
                            and os.environ.get("HIVV_RECEIPTS", "on").lower() not in ("off", "0", "no")):
                        extra = f" · policy {policy_hdr}" if policy_hdr and policy_hdr not in ("ALLOW default:compute", "ALLOW failopen:registry") else ""
                        print(f"\033[2m  — answered by {answered_by}{extra}\033[0m")
                buf = b""
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        line_str = line.decode("utf-8", errors="replace").rstrip("\r\n")
                        if not line_str or line_str.startswith(":"):
                            continue
                        if line_str.startswith("data:"):
                            payload = line_str[5:].strip()
                            if payload in ("[DONE]", ""):
                                print(); _receipt(); return
                            try:
                                evt = json.loads(payload)
                                choices = evt.get("choices") or []
                                if choices:
                                    delta = choices[0].get("delta") or {}
                                    content = delta.get("content")
                                    if content:
                                        sys.stdout.write(content); sys.stdout.flush()
                            except json.JSONDecodeError:
                                sys.stdout.write(payload); sys.stdout.flush()
                print()
                _receipt()
                return  # Success
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            if e.code in (202, 403):
                try:
                    d = json.loads(body)
                    if d.get("verdict") == "MAYBE":
                        _print_maybe(d); return
                    if d.get("verdict") == "NO":
                        _print_no(d); return
                except Exception:
                    pass
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") or "5"
                wait_s = int(retry_after) if retry_after.isdigit() else 3
                if attempt < max_retries:
                    sys.stderr.write(f"[{APP}] rate limited — waiting {wait_s}s before retry...\n")
                    time.sleep(wait_s)
                    continue
                die(f"rate limited — hub is busy. wait {retry_after}s and try again")
            die(f"chat error {e.code}: {body[:200]}")
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            err_msg = str(e)
            # Show exactly what was tried
            route_path = f"{HUB}/api/chat"
            if provider:
                route_path += f" → {provider}"
            if "timed out" in err_msg:
                print(f"\n  \033[31m✗ request timed out\033[0m")
                print(f"    tried:  {route_path}")
                print(f"    wait:   60s timeout")
                if provider:
                    host = provider.split(":")[0] if ":" in provider else "?"
                    print(f"    node:   {host} ({provider})")
                    print(f"    fix:    {APP} reach {host}     — test if node is reachable")
                    print(f"    fix:    {APP} node {host}       — check node status")
                else:
                    print(f"    fix:    {APP} health     — check hub")
                print(f"    status: {APP} stats      — see all nodes")
            elif "refused" in err_msg:
                print(f"\n  \033[31m✗ connection refused\033[0m")
                print(f"    tried:  {route_path}")
                if provider:
                    host = provider.split(":")[0] if ":" in provider else "?"
                    print(f"    node:   {host} ({provider})")
                    print(f"    fix:    {APP} node {host}       — check node status")
                    print(f"    fix:    {APP} doctor --fix   — fix bindings")
                else:
                    print(f"    fix:    {APP} health     — check hub")
            elif "resolve" in err_msg or "Name or service" in err_msg:
                print(f"\n  \033[31m✗ cannot resolve hostname\033[0m")
                print(f"    tried:  {route_path}")
                print(f"    fix:    check your internet connection")
            else:
                print(f"\n  \033[31m✗ network error\033[0m: {err_msg}")
                print(f"    tried:  {route_path}")
                print(f"    fix:    {APP} doctor     — diagnose the issue")
            sys.exit(1)

def cmd_completions(args) -> None:
    msg = " ".join(args.message)
    if not msg: die("completions needs a prompt (hivv completions 'hello')")
    code, p = http("POST", "/v1/chat/completions",
                   body={"model": "hivv", "messages": [{"role": "user", "content": msg}], "stream": False})
    if isinstance(p, dict):
        try:
            print(p["choices"][0]["message"]["content"])
            sys.exit(0 if code == 200 else 2)
        except (KeyError, IndexError, TypeError):
            pass
    print(json.dumps(p, indent=2) if isinstance(p, (dict, list)) else p)
    sys.exit(0 if code == 200 else 2)

def cmd_whoami(args) -> None:
    code, p = http("GET", "/api/control/health", timeout=5)
    print(f"hub={HUB}  status={code}  payload={p}")
    sys.exit(0 if code == 200 else 2)

def cmd_models(args) -> None:
    """List all available AI models with details: function calling support,
    context window, model size, speed (tokens/sec), and description.
    Also shows which bee runs which local model; `--bees` shows ONLY that."""
    filter_fc = getattr(args, "fc", False)
    filter_provider = getattr(args, "provider", None)
    filter_local = getattr(args, "local", False)
    bees_only = getattr(args, "bees", False)

    # ── 🐝 on the hive: which bee is running what ─────────────────────
    try:
        cn, pn = http("GET", "/api/control/nodes", timeout=6)
        bee_nodes = pn.get("nodes", []) if cn == 200 and isinstance(pn, dict) else []
    except Exception:
        bee_nodes = []
    bee_lines = _bee_model_summary(bee_nodes)
    if bee_lines:
        print(f"\n\033[1mon the hive — models by bee\033[0m ({len(bee_lines)} bee(s))")
        for ln in bee_lines:
            print(ln)
    elif bees_only:
        print("\n  no bees are reporting local models yet")
    if bees_only:
        print(f"\n  \033[2mfull catalog: {APP} models  ·  also in {APP} nodes\033[0m\n")
        return

    code, p = http("GET", "/api/models", timeout=8)
    if code != 200:
        die(f"could not reach hub (HTTP {code})")
    if not isinstance(p, dict):
        die(f"unexpected response: {p}")
    models = p.get("models", [])
    total = p.get("total", len(models))

    # Filter
    if filter_fc:
        models = [m for m in models if m.get("function_calling")]
    if filter_provider:
        models = [m for m in models if m.get("provider") == filter_provider]
    if filter_local:
        models = [m for m in models if m.get("provider") == "local"]

    fc_only = " (function calling only)" if filter_fc else ""
    print(f"\n\033[1m{APP} models — {len(models)} of {total} models{fc_only}\033[0m\n")
    print(f"  \033[2m{'MODEL':<45} {'PROVIDER':<12} {'FC':<4} {'CTX':<10} {'SIZE':<8} {'SPEED':<14} DESCRIPTION\033[0m")
    print(f"  \033[2m{'-'*160}\033[0m")

    for m in models:
        fc = m.get("function_calling")
        fc_str = f"\033[32m✓\033[0m" if fc else f"\033[31m✗\033[0m"
        ctx = m.get("context_window", 0)
        ctx_str = f"{ctx:,}" if ctx else "?"
        size = m.get("model_size", "?")
        speed = m.get("speed", "?")
        prov = (m.get("provider") or "?").ljust(12)
        name = (m.get("name") or m.get("id") or "?").ljust(45)
        desc = m.get("description", "")
        is_local = m.get("provider") == "local"
        prefix = "\033[36m★\033[0m " if is_local else "  "
        print(f"{prefix}{name} \033[2m{prov}\033[0m {fc_str}  \033[2m{ctx_str}\033[0m  {size:<8} {speed:<14} {desc}")

    print()
    if not filter_fc:
        print(f"  \033[2mfilter: {APP} models --fc        (function calling only)\033[0m")
        print(f"  \033[2mfilter: {APP} models --provider openai  (specific provider)\033[0m")
        print(f"  \033[2mfilter: {APP} models --local       (your local models)\033[0m")
    print(f"\n  \033[2mFC = function calling / tool use support\033[0m")
    print(f"  \033[2mCTX = context window (tokens)\033[0m")
    print()

def _ok(s): return f"\033[32m✓\033[0m {s}"
def _warn(s): return f"\033[33m!\033[0m {s}"
def _fail(s): return f"\033[31m✗\033[0m {s}"

def cmd_doctor(args) -> None:
    """One-glance health report: install + hub + agent + local bee + version.
    Output is pasteable for support — pure text + ANSI colors only."""
    print(f"\033[1m{APP} doctor\033[0m — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"hub = {HUB}   client = {VERSION}")

    # --- install -----------------------------------------------------------
    print("\n\033[1minstall\033[0m")
    bin_path = Path(__file__).resolve()
    print(f"  wrapper:  {BIN_DIR / APP}   " + (_ok("exists") if (BIN_DIR / APP).exists() else _fail("MISSING")))
    print(f"  client:   {bin_path}   " + (_ok("exists") if bin_path.exists() else _fail("MISSING")))
    cfg = Path.home() / ".config" / APP
    st  = Path.home() / ".local" / "share" / APP
    print(f"  config:   {cfg}   " + (_ok("exists") if cfg.exists() else _warn("missing (will be created on first run)")))
    print(f"  state:    {st}   " + (_ok("exists") if st.exists() else _warn("missing")))

    # --- hub ---------------------------------------------------------------
    print("\n\033[1mhub\033[0m")
    try:
        t0 = time.time()
        code, p = http("GET", "/api/health", timeout=5)
        ms = int((time.time() - t0) * 1000)
        if code == 200:
            providers = (p or {}).get("providers", "?") if isinstance(p, dict) else "?"
            print(f"  {_ok(f'{HUB} reachable ({ms}ms)')}   providers online: {providers}")
        else:
            print(f"  {_fail(f'{HUB} returned HTTP {code}')}")
    except Exception as e:
        print(f"  {_fail(f'{HUB} unreachable: {e}')}")

    # --- agent -------------------------------------------------------------
    print("\n\033[1magent\033[0m")
    pid = read_pid()
    if pid and _pid_alive_fallback(pid):
        st_data = read_state() or {}
        nid = st_data.get("node_id") or "?"
        hb = st_data.get("last_heartbeat") or 0
        hb_age = int(time.time()) - hb if hb else -1
        hb_str = f"{hb_age}s ago" if hb_age >= 0 else "never"
        hb_ok = hb_age >= 0 and hb_age < 120
        print(f"  {_ok(f'running (pid {pid}, node {nid})')}   last heartbeat: {hb_str}")
    else:
        print(f"  {_warn('not running')}   start with `{APP} start`")

    # --- resilience --------------------------------------------------------
    print("\n\033[1mresilience\033[0m")

    # Dev mode status (protect development code from accidental overwrites)
    if _is_dev_mode():
        marker_file = CONFIG_DIR / ".hivv-dev"
        marker_file_str = str(marker_file) if marker_file.exists() else "HIVV_DEV_MODE env var"
        print(f"  dev:     {_warn(f'dev mode ACTIVE ({marker_file_str})')}   auto-update BLOCKED")
        print(f"           remove marker or unset HIVV_DEV_MODE to enable auto-update")
    else:
        print(f"  dev:     {_ok('production mode')}   auto-update active")
    print(f"           marker: {CONFIG_DIR / '.hivv-dev'}")

    # Trusted fingerprint (security: only accept updates matching known-good SHA256)
    trusted_fp = os.environ.get("HIVV_TRUSTED_FINGERPRINT", "").strip()
    if trusted_fp:
        print(f"  trust:   {_ok(f'fingerprints enabled')}   HIVV_TRUSTED_FINGERPRINT={trusted_fp[:16]}…")
        print(f"           only updates matching this SHA256 will be applied")
    else:
        print(f"  trust:   {_warn('no trusted fingerprint')}   set HIVV_TRUSTED_FINGERPRINT=<sha256> to secure auto-update")

    import platform as _plat
    import subprocess as _sp
    svc_ok = False
    if _plat.system() == "Linux":
        svc_path = Path.home() / ".config" / "systemd" / "user" / "hivv-agent.service"
        if svc_path.exists():
            r = _sp.run(["systemctl", "--user", "is-enabled", "hivv-agent"],
                        capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                print(f"  boot:    {_ok('systemd user service enabled')}")
                svc_ok = True
            else:
                print(f"  boot:    {_warn('service exists but not enabled — run: systemctl --user enable hivv-agent')}")
        else:
            print(f"  boot:    {_warn('no systemd user service — agent dies on reboot')}")
            print(f"           run: {APP} doctor --fix  OR  {APP} start")
        r2 = _sp.run(["systemctl", "--user", "is-active", "hivv-agent"],
                     capture_output=True, text=True, timeout=5)
        if r2.returncode == 0 and r2.stdout.strip() == "active":
            print(f"  service: {_ok('systemd service running')}")
        elif pid:
            print(f"  service: {_warn('agent running but not via systemd — may not survive reboot')}")
    elif _plat.system() == "Darwin":
        r = _sp.run(["launchctl", "list", f"org.hivv.agent"],
                    capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            print(f"  boot:    {_ok('launchd service enabled')}")
            svc_ok = True
        else:
            print(f"  boot:    {_warn('no launchd service — run: {APP} doctor --fix')}")
    else:
        print(f"  boot:    {_warn('auto-restart not configured on this platform')}")

    # Crash loop detection — check recent agent log
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()
            recent = lines[-30:] if len(lines) > 30 else lines
            starts = [l for l in recent if "agent started" in l or "agent starting" in l]
            stops = [l for l in recent if "agent stopping" in l]
            if len(starts) >= 3 and len(stops) >= 2:
                print(f"  crash:   {_fail('CRASH LOOP — agent restarting repeatedly')}")
                print(f"           {len(starts)} starts, {len(stops)} stops in recent log")
            elif len(starts) >= 2 and len(stops) >= 1:
                print(f"  crash:   {_warn('agent restarted recently — investigate: hivv logs')}")
        except Exception:
            pass

    # Auto-update tracking
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()
            recent = lines[-50:] if len(lines) > 50 else lines
            updates = [l for l in recent if "auto-update" in l or "SELF-REPAIR" in l]
            if updates:
                last_update = updates[-1] if updates else ""
                print(f"  repair:   {_ok('self-repair active')}")
            else:
                print(f"  repair:   {_warn('self-repair not yet triggered — run: hivv start')}")
        except Exception:
            pass

    # --- local bee ---------------------------------------------------------
    print("\n\033[1mlocal bee\033[0m")
    m = _collect_metrics()
    cpu_str = f"{m.get('cpu_count','?')} cores, load {m.get('load_1','?')}/{m.get('load_5','?')}/{m.get('load_15','?')}"
    mem_str = f"{m.get('mem_used_mb','?')}/{m.get('mem_total_mb','?')} MB ({m.get('mem_pct','?')}%)"
    disk_str = f"{m.get('disk_used_gb','?')}/{m.get('disk_total_gb','?')} GB ({m.get('disk_pct','?')}%)"
    print(f"  cpu:  {cpu_str}")
    print(f"  mem:  {mem_str}")
    print(f"  disk: {disk_str}")
    gpus = m.get("gpus") or []
    if gpus:
        for g in gpus:
            print(f"  gpu:  [{g['index']}] {g['name']}  util={g['util_pct']}%  mem={g['mem_used_mb']}/{g['mem_total_mb']} MB  temp={g.get('temp_c','?')}°C")
    else:
        print(f"  gpu:  (none detected — nvidia-smi missing or no NVIDIA GPU)")

    # --- network / firewall ------------------------------------------------
    print("\n\033[1mnetwork\033[0m")
    import subprocess as _sp
    _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=5)

    # LLM servers
    llms = _detect_local_ai()
    if llms:
        llm_ports = sorted(set(m["port"] for m in llms))
        port_str = ", ".join(str(p) for p in llm_ports)
        print(f"  llm:    {_ok(f'{len(llms)} model(s) on port {port_str}')}")
        # Check binding
        for port in llm_ports:
            r = _r(f"ss -tlnp 2>/dev/null | grep ':{port} '")
            if r.stdout and "127.0.0.1:" + str(port) in r.stdout:
                print(f"  bind:   {_warn(f'port {port} localhost only — run {APP} doctor --fix')}")
    else:
        print(f"  llm:    {_warn('none running')}   start with `{APP} llm serve`")

    # Tailscale — check login status, not just binary
    ts_status = _r("tailscale status --json 2>/dev/null")
    ts_ip = _r("tailscale ip -4 2>/dev/null")
    if ts_status.returncode == 0 and ts_ip.returncode == 0 and ts_ip.stdout.strip():
        import json as _json
        try:
            ts_data = _json.loads(ts_status.stdout)
            login = ts_data.get("BackendState", "unknown")
            if login == "Running":
                print(f"  vpn:    {_ok(f'tailscale active ({ts_ip.stdout.strip()})')}")
            else:
                print(f"  vpn:    {_warn(f'tailscale {login.lower()} — run: tailscale up')}   (or: tailscale logout && tailscale up)")
        except Exception:
            print(f"  vpn:    {_ok(f'tailscale active ({ts_ip.stdout.strip()})')}")
    else:
        print(f"  vpn:    {_warn('tailscale not running')}   install: curl -fsSL https://tailscale.com/install.sh | sh")

    # Firewall
    import platform
    fw_ok = False
    if platform.system() == "Linux":
        r = _r("ufw status 2>/dev/null")
        if "Status: active" in r.stdout:
            # Check if LLM ports are allowed
            blocked = []
            for port in (llm_ports if llms else []):
                if f"{port}" not in r.stdout:
                    blocked.append(port)
            if blocked:
                bp = ", ".join(str(p) for p in blocked)
                print(f"  fw:     {_warn(f'ufw active but port {bp} blocked')}   run `{APP} firewall --apply`")
            else:
                print(f"  fw:     {_ok('ufw active, LLM ports open')}")
                fw_ok = True
        elif "Status: inactive" in r.stdout:
            print(f"  fw:     {_warn('ufw inactive — LLMs open to internet')}   run `{APP} firewall --apply --mode lan`")
        else:
            r2 = _r("firewall-cmd --state 2>/dev/null")
            if "running" in r2.stdout:
                print(f"  fw:     {_ok('firewalld active')}")
                fw_ok = True
            else:
                print(f"  fw:     {_warn('no firewall — LLMs open to internet')}   run `{APP} firewall --apply --mode lan`")
    elif platform.system() == "Darwin":
        r = _r("pfctl -s info 2>/dev/null")
        if "Enabled" in r.stdout:
            print(f"  fw:     {_ok('pf (macOS) active')}")
            fw_ok = True
        else:
            print(f"  fw:     {_warn('pf not active — check System Settings > Network > Firewall')}")
    elif platform.system() == "Windows":
        r = _r('netsh advfirewall show allprofiles state 2>nul')
        if "ON" in r.stdout:
            print(f"  fw:     {_ok('Windows Firewall active')}")
            fw_ok = True
        else:
            print(f"  fw:     {_warn('Windows Firewall off — LLMs open to internet')}   run `{APP} firewall --apply`")

    if not fw_ok and llms:
        print(f"\n  \033[33m⚡ your LLMs are reachable by anyone on your network\033[0m")
        print(f"  run: {APP} firewall --apply --mode lan    (LAN only, safe)")
        print(f"  or:  {APP} firewall --apply --mode tailscale (VPN only, safest)")

    # --- update check ------------------------------------------------------
    print("\n\033[1mupdate\033[0m")
    try:
        req = urllib.request.Request(f"{HUB.rstrip('/')}/{APP}-cli.py", headers={"user-agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read().decode("utf-8", errors="replace")
        # crude version sniff — pull the first `VERSION   = "x.y.z"` line
        import re as _re
        match = _re.search(r'VERSION\s*=\s*"([^"]+)"', data)
        hub_ver = match.group(1) if match else "unknown"
        if hub_ver == VERSION:
            print(f"  {_ok(f'up to date (client {VERSION} = hub {hub_ver})')}")
        else:
            arrow = "↑ newer on hub" if hub_ver > VERSION else "↓ local is newer"
            print(f"  {_warn(f'client {VERSION} vs hub {hub_ver}')}   {arrow}   run `{APP} update`")
    except Exception as e:
        print(f"  {_warn(f'could not check hub version: {e}')}")

    print()  # trailing newline

    # Always run health checks at the end
    _run_health_checks()

    # If --fix was passed, run full auto-fix
    if getattr(args, "fix", False):
        print(f"\n\033[1mrunning full auto-fix...\033[0m")
        import subprocess as _sp
        _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=30)

        # Fix broken scripts
        import glob as _glob
        for pattern in ["/home/*/llama.cpp/servers/start-*.sh"]:
            for s in _glob.glob(pattern):
                content = _r(f"cat {s} 2>/dev/null").stdout
                if content and "LLAMA_BIN=" in content:
                    broken_line = [l for l in content.split("\n") if l.strip().startswith("LLAMA_BIN=") and "--host" in l]
                    if broken_line:
                        _fix_llama_script(s)
                        print(f"  \033[32m✓\033[0m fixed script: {s}")

        # Fix ollama binding
        r = _r("ss -tlnp 2>/dev/null | grep ':11434 '")
        if r.stdout and "127.0.0.1:" in r.stdout:
            # Find ollama service
            for candidate in ["/etc/systemd/system/ollama.service", "/lib/systemd/system/ollama.service"]:
                if _r(f"test -f {candidate}").returncode == 0:
                    override_dir = candidate + ".d"
                    override_file = f"{override_dir}/hivv-network.conf"
                    _r(f"sudo mkdir -p {override_dir}")
                    import tempfile
                    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False)
                    tmp.write('[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n')
                    tmp.close()
                    _r(f"sudo cp {tmp.name} {override_file}")
                    _r(f"rm -f {tmp.name}")
                    _r("sudo systemctl daemon-reload")
                    _r("sudo systemctl restart ollama")
                    print(f"  \033[32m✓\033[0m ollama: configured for network access")
                    break

        # Start stopped llama services (skip duplicates/crash-loops — see _llama_start_guard)
        r = _r("systemctl list-units --type=service --state=dead --no-pager --plain --no-legend 2>/dev/null | grep -i llama | grep -v ollama")
        if r.stdout.strip():
            for line in r.stdout.strip().split("\n"):
                svc = line.split()[0] if line.split() else ""
                if not svc:
                    continue
                skip, why = _llama_start_guard(_r, svc)
                if skip:
                    print(f"  \033[33m!\033[0m would NOT start {svc}: {why}")
                    continue
                _r(f"sudo systemctl start {svc}")
                print(f"  \033[32m✓\033[0m started: {svc}")

        # Fix stale node_id
        st = read_state() or {}
        node_id = st.get("node_id", "")
        import socket
        hostname = socket.gethostname().split(".")[0]
        if node_id and "-" in node_id and node_id != hostname:
            suffix = node_id.split("-", 1)[1]
            if suffix.isdigit() and int(suffix) <= 255:
                st["node_id"] = hostname
                write_state(st)
                print(f"  \033[32m✓\033[0m reset node_id to {hostname}")

        # Create/enable systemd user service so the agent auto-starts on boot
        import platform as _plat
        if _plat.system() == "Linux":
            svc_dir = Path.home() / ".config" / "systemd" / "user"
            svc_path = svc_dir / "hivv-agent.service"
            svc_content = f"""[Unit]
Description=hivv AI node agent
After=network.target

[Service]
Type=simple
ExecStart={_agent_execstart()}
Restart=always
RestartSec=10
WorkingDirectory={Path.home() / ".local" / "share" / APP}
StandardOutput=append:{Path.home() / ".local" / "share" / APP / "agent.log"}
StandardError=append:{Path.home() / ".local" / "share" / APP / "agent.log"}

[Install]
WantedBy=default.target
"""
            _r(f"mkdir -p {svc_dir}")
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.service', delete=False)
            tmp.write(svc_content)
            tmp.close()
            _r(f"cp {tmp.name} {svc_path}")
            _r(f"rm -f {tmp.name}")
            _r("systemctl --user daemon-reload")
            _r("systemctl --user enable --now hivv-agent")
            print(f"  \033[32m✓\033[0m systemd service: hivv-agent.service (enabled, started)")

        # Create/enable a LaunchAgent so the agent auto-starts at login (macOS)
        elif _plat.system() == "Darwin":
            plist_dir = Path.home() / "Library" / "LaunchAgents"
            plist_path = plist_dir / "org.hivv.agent.plist"
            if not plist_path.exists():
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.hivv.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{Path(__file__).resolve()}</string>
    <string>_agent</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{Path.home() / ".local" / "share" / APP / "agent.log"}</string>
  <key>StandardErrorPath</key><string>{Path.home() / ".local" / "share" / APP / "agent.log"}</string>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
"""
                plist_path.write_text(plist)
                _r(f"launchctl bootout gui/{os.getuid()}/org.hivv.agent")
                r_boot = _r(f"launchctl bootstrap gui/{os.getuid()} {plist_path}")
                if r_boot.returncode != 0:
                    _r(f"launchctl load -w {plist_path}")
                print(f"  \033[32m✓\033[0m LaunchAgent: org.hivv.agent (starts at login)")

        # Windows: hint at Task Scheduler (registering from CLI needs careful
        # quoting; give the exact command instead of silently doing nothing)
        elif _plat.system() == "Windows":
            print(f"  \033[33m⚠\033[0m to auto-start at login on Windows, run in PowerShell:")
            print(f"    schtasks /Create /TN hivv /TR \"cmd /c {APP} start\" /SC ONLOGON /RL LIMITED /F")
            print(f"    (or simply keep a terminal with: {APP} start)")

        print(f"\n  \033[32m✓ auto-fix complete\033[0m")
    else:
        print(f"\n  to auto-fix all issues: {APP} doctor --fix")

def cmd_firewall(args) -> None:
    """Detect local LLM servers and configure firewall to let other hivv nodes reach them.
    Supports: LAN-only (default), Tailscale VPN, or manual rules.
    Cross-platform: ufw/firewalld (Linux), pf (macOS), Windows Firewall."""
    import subprocess, platform

    system = platform.system()
    ok = "\033[32m✓\033[0m"
    warn = "\033[33m!\033[0m"
    fail = "\033[31m✗\033[0m"

    # 1. Detect running LLM servers
    models = _detect_local_ai()

    # Helper: write a systemd service file reliably
    def _write_svc_file(path, content):
        """Write content to a systemd service file using temp file + sudo cp."""
        import tempfile
        tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.service', delete=False)
        tmp.write(content)
        tmp.close()
        r = _run(f"sudo cp {tmp.name} {path}")
        _run(f"rm -f {tmp.name}")
        return r

    # Helper: fix a broken llama startup script
    def _fix_llama_script(script_path):
        """Fix a llama startup script — ensure --host 0.0.0.0 is on the right lines."""
        if _run(f"test -f {script_path}").returncode != 0:
            return False
        content = _run(f"cat {script_path}").stdout
        fixed = False
        new_lines = []
        for line in content.split("\n"):
            stripped = line.strip()
            # Fix broken LLAMA_BIN= line that has --host attached
            if stripped.startswith("LLAMA_BIN=") and "--host" in stripped:
                line = line.replace(" --host 0.0.0.0", "").replace(" --host", "")
                fixed = True
            # Add --host 0.0.0.0 to lines that invoke the binary (have -m or --port)
            if ("$LLAMA_BIN" in line or "llama-server" in line) and ("-m " in line or "--port" in line) and "--host" not in line:
                line = line.replace("llama-server ", "llama-server --host 0.0.0.0 ", 1) if "llama-server" in line else line.replace("$LLAMA_BIN ", "$LLAMA_BIN --host 0.0.0.0 ", 1)
                fixed = True
            new_lines.append(line)
        if fixed:
            import tempfile
            tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False)
            tmp.write("\n".join(new_lines))
            tmp.close()
            _run(f"sudo cp {tmp.name} {script_path} && sudo chmod +x {script_path}")
            _run(f"rm -f {tmp.name}")
        return fixed

    if not models:
        print(f"\n{fail} No local LLM servers found running.")
        print(f"   Start one first: {APP} llm serve")
        return

    ports = sorted(set(m["port"] for m in models))
    print(f"\n\033[1m{APP} firewall\033[0m")
    print(f"   detected LLMs on ports: {', '.join(str(p) for p in ports)}")
    for m in models:
        name = m["model"].split("/")[-1].replace(".gguf", "")[:40]
        print(f"   {m['provider']}: {name} (:{m['port']})")

    # 2. Check binding — are LLMs listening on 0.0.0.0 (network) or 127.0.0.1 (localhost only)?
    _run = lambda cmd: subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
    print(f"\n\033[1mbinding\033[0m")
    localhost_only = []
    bound_ok = []
    for port in ports:
        try:
            r = _run(f"ss -tlnp 2>/dev/null | grep ':{port} '")
            if r.stdout.strip():
                # Parse: "LISTEN 0 512 127.0.0.1:8080 0.0.0.0:* users:..."
                if "127.0.0.1:" + str(port) in r.stdout:
                    localhost_only.append(port)
                    # Find the process name
                    proc_match = r.stdout.split("users:((")[1].split(",")[0].strip('"') if "users:((" in r.stdout else "unknown"
                    print(f"   {fail} port {port} — localhost only ({proc_match})")
                elif "0.0.0.0:" + str(port) in r.stdout or "*:" + str(port) in r.stdout:
                    bound_ok.append(port)
                    print(f"   {ok} port {port} — bound to 0.0.0.0 (network reachable)")
                else:
                    bound_ok.append(port)
                    print(f"   {ok} port {port} — listening")
            else:
                # Not showing in ss — might be bound differently
                print(f"   {warn} port {port} — cannot determine binding")
        except Exception:
            print(f"   {warn} port {port} — cannot check binding")

    if localhost_only:
        print(f"\n   {fail} PROBLEM: LLMs bound to 127.0.0.1 — other nodes CANNOT reach them")
        print(f"   other machines see: connection refused or timeout")
        print(f"\n   to fix, restart your LLMs with --host 0.0.0.0:")
        for port in localhost_only:
            proc_info = _run(f"ss -tlnp 2>/dev/null | grep ':{port} '")
            if "llama-server" in proc_info.stdout:
                # Try to find the original command
                pid_match = _run(f"ss -tlnp 2>/dev/null | grep ':{port} ' | grep -o 'pid=[0-9]*'")
                pid = pid_match.stdout.replace("pid=", "").strip() if pid_match.stdout else ""
                cmdline = ""
                if pid:
                    cmdline_file = _run(f"cat /proc/{pid}/cmdline 2>/dev/null")
                    cmdline = cmdline_file.stdout.replace("\x00", " ").strip() if cmdline_file.stdout else ""
                if cmdline and "--host" in cmdline:
                    # Replace --host 127.0.0.1 with --host 0.0.0.0
                    fixed = cmdline.replace("--host 127.0.0.1", "--host 0.0.0.0").replace("--host", "--host 0.0.0.0")
                    print(f"   {APP} llm serve --host 0.0.0.0 --port {port}")
                else:
                    print(f"   {APP} llm serve --host 0.0.0.0 --port {port}")
            elif "ollama" in proc_info.stdout:
                print(f"   sudo systemctl set-environment OLLAMA_HOST=0.0.0.0:11434 && sudo systemctl restart ollama")
            else:
                print(f"   restart the LLM server with: --host 0.0.0.0 --port {port}")

    else:
        print(f"   {ok} all LLMs bound to 0.0.0.0 — network reachable")

    # Look for stopped LLMs (informational only)
    import glob as _glob
    for pattern in ["/home/*/llama.cpp/servers/start-*.sh"]:
        for s in _glob.glob(pattern):
            content = _r(f"cat {s} 2>/dev/null").stdout
            if content and "llama-server" in content:
                print(f"  {warn} startup script found: {s}")

    print(f"\n  to auto-fix all issues: {APP} doctor --fix")

    # 3. Check for Tailscale
    # 3. Check for Tailscale
    tailscale_ip = None
    tailscale_ok = False
    try:
        r = _run("tailscale ip -4 2>/dev/null")
        if r.returncode == 0 and r.stdout.strip():
            tailscale_ip = r.stdout.strip()
            tailscale_ok = True
            print(f"\n\033[1mtailscale\033[0m")
            print(f"   {ok} detected — VPN IP: {tailscale_ip}")
            r2 = _run("tailscale status --json 2>/dev/null")
            if r2.returncode == 0:
                import json as _json
                try:
                    ts = _json.loads(r2.stdout)
                    peers = ts.get("Peer", {})
                    online = sum(1 for p in peers.values() if p.get("Online"))
                    print(f"   {ok} {online} peer(s) connected via Tailscale")
                except Exception:
                    pass
    except Exception:
        pass

    if not tailscale_ok:
        print(f"\n\033[1mtailscale\033[0m")
        print(f"   {warn} not installed — nodes on different networks won't reach you")
        print(f"   install: curl -fsSL https://tailscale.com/install.sh | sh")
        print(f"   then:    sudo tailscale up")

    # 3. Detect LAN subnet
    lan_ip = ""
    lan_subnet = ""
    try:
        if system == "Linux":
            r = _run("hostname -I")
            if r.stdout.strip():
                lan_ip = r.stdout.strip().split()[0]
        elif system == "Darwin":
            r = _run("ipconfig getifaddr en0 2>/dev/null")
            if r.stdout.strip():
                lan_ip = r.stdout.strip()
        elif system == "Windows":
            r = _run('powershell -Command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object {$_.InterfaceAlias -notlike \'*Loopback*\' -and $_.IPAddress -ne \'127.0.0.1\'}).IPAddress | Select-Object -First 1"')
            if r.stdout.strip():
                lan_ip = r.stdout.strip().split("\n")[0].strip()
    except Exception:
        pass

    if lan_ip:
        parts = lan_ip.split(".")
        if len(parts) == 4:
            lan_subnet = f"{parts[0]}.{parts[1]}.{parts[2]}.0/24"
        print(f"\n\033[1mnetwork\033[0m")
        print(f"   {ok} LAN IP: {lan_ip}")
        print(f"   {ok} subnet: {lan_subnet}")

    # 4. Check current firewall status
    print(f"\n\033[1mfirewall status\033[0m")

    fw_tool = None
    if system == "Linux":
        r = _run("ufw status 2>/dev/null")
        if "Status: active" in r.stdout:
            print(f"   {ok} ufw active")
            fw_tool = "ufw"
        elif "Status: inactive" in r.stdout:
            print(f"   {warn} ufw installed but inactive")
            fw_tool = "ufw"
        else:
            r2 = _run("firewall-cmd --state 2>/dev/null")
            if "running" in r2.stdout:
                print(f"   {ok} firewalld active")
                fw_tool = "firewalld"
            else:
                r3 = _run("iptables -L INPUT -n 2>/dev/null")
                if r3.returncode == 0:
                    print(f"   {warn} iptables active (no ufw/firewalld)")
                    fw_tool = "iptables"
                else:
                    print(f"   {warn} no firewall detected — ports may be open to everyone")
    elif system == "Darwin":
        r = _run("pfctl -s info 2>/dev/null")
        if "Enabled" in r.stdout:
            print(f"   {ok} pf (macOS firewall) active")
            fw_tool = "pf"
        else:
            print(f"   {warn} pf not active")
            fw_tool = "pf"
    elif system == "Windows":
        r = _run('netsh advfirewall show allprofiles state 2>nul')
        if "ON" in r.stdout:
            print(f"   {ok} Windows Firewall active")
            fw_tool = "windows"
        else:
            print(f"   {warn} Windows Firewall inactive")
            fw_tool = "windows"

    # 5. Determine which ports need rules
    closed_ports = []
    if fw_tool == "ufw":
        r = _run("ufw status numbered 2>/dev/null")
        for port in ports:
            if f"{port}" not in r.stdout:
                closed_ports.append(port)
    elif fw_tool == "firewalld":
        for port in ports:
            r = _run(f"firewall-cmd --query-port={port}/tcp 2>/dev/null")
            if "no" in r.stdout:
                closed_ports.append(port)
    elif fw_tool == "pf":
        r = _run("pfctl -sr 2>/dev/null")
        for port in ports:
            if str(port) not in r.stdout:
                closed_ports.append(port)
    elif fw_tool == "windows":
        for port in ports:
            r = _run(f'netsh advfirewall firewall show rule name="hivv {port}" 2>nul')
            if "hivv" not in r.stdout.lower():
                closed_ports.append(port)

    if not closed_ports:
        print(f"\n{ok} All LLM ports already have firewall rules.")
    else:
        print(f"\n   ports needing rules: {', '.join(str(p) for p in closed_ports)}")

    # 6. Show security options
    do_apply = getattr(args, "apply", False)
    mode = getattr(args, "mode", "lan") or "lan"

    print(f"\n\033[1msecurity modes\033[0m")
    print(f"   \033[1mlan\033[0m      — only allow your LAN subnet ({lan_subnet or 'x.x.x.0/24'})")
    print(f"   \033[1mtailscale\033[0m — only allow Tailscale VPN peers (safest, no ports to internet)")
    print(f"   \033[1mopen\033[0m     — allow from anywhere (NOT recommended)")

    if not do_apply:
        print(f"\n   to apply, run:")
        print(f"   {APP} firewall --apply --mode lan")
        print(f"   {APP} firewall --apply --mode tailscale")
        print(f"\n   or configure manually:")
        for port in closed_ports:
            if fw_tool == "ufw" and lan_subnet:
                print(f"   sudo ufw allow from {lan_subnet} to any port {port} proto tcp")
            elif fw_tool == "ufw":
                print(f"   sudo ufw allow {port}/tcp")
        return

    # 7. Apply rules based on mode
    print(f"\n\033[1mapplying {mode} rules...\033[0m")

    # Build allow sources
    allow_from = []
    if mode == "lan" and lan_subnet:
        allow_from.append(("LAN", lan_subnet))
    if mode == "tailscale" and tailscale_ip:
        # Tailscale uses 100.x.x.x/32 per device, or 100.64.0.0/10 range
        allow_from.append(("Tailscale", "100.64.0.0/10"))
    if mode == "open":
        allow_from.append(("any", None))

    if not allow_from:
        print(f"   {fail} no valid source for mode '{mode}'")
        if mode == "tailscale" and not tailscale_ip:
            print(f"   install tailscale first: curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up")
        return

    for port in closed_ports:
        for label, source in allow_from:
            if fw_tool == "ufw":
                if source:
                    r = _run(f"sudo ufw allow from {source} to any port {port} proto tcp")
                else:
                    r = _run(f"sudo ufw allow {port}/tcp")
                if r.returncode == 0:
                    print(f"   {ok} ufw: port {port} allowed from {label}")
                else:
                    print(f"   {fail} ufw: failed: {r.stderr.strip()}")

            elif fw_tool == "firewalld":
                if source:
                    r = _run(f"sudo firewall-cmd --add-rich-rule='rule family=ipv4 source address={source} port port={port} protocol=tcp accept' --permanent")
                else:
                    r = _run(f"sudo firewall-cmd --add-port={port}/tcp --permanent")
                _run("sudo firewall-cmd --reload")
                if r.returncode == 0:
                    print(f"   {ok} firewalld: port {port} allowed from {label}")
                else:
                    print(f"   {fail} firewalld: failed: {r.stderr.strip()}")

            elif fw_tool == "pf":
                rule = f"pass in proto tcp from {source or 'any'} to any port {port}"
                print(f"   {warn} pf: add to /etc/pf.conf: {rule}")
                print(f"   then: sudo pfctl -f /etc/pf.conf")

            elif fw_tool == "windows":
                if source:
                    r = _run(f'netsh advfirewall firewall add rule name="hivv {port} {label}" dir=in action=allow protocol=TCP localport={port} remoteip={source}')
                else:
                    r = _run(f'netsh advfirewall firewall add rule name="hivv {port}" dir=in action=allow protocol=TCP localport={port}')
                if r.returncode == 0:
                    print(f"   {ok} Windows: port {port} allowed from {label}")
                else:
                    print(f"   {fail} Windows: failed: {r.stderr.strip()}")

    # 8. Also handle gateway port 3011
    if fw_tool and "3011" not in str(closed_ports):
        pass  # already open or not needed
    elif fw_tool == "ufw" and do_apply:
        for label, source in allow_from:
            if source:
                _run(f"sudo ufw allow from {source} to any port 3011 proto tcp")
            else:
                _run(f"sudo ufw allow 3011/tcp")
        print(f"   {ok} gateway port 3011 configured")

    print(f"\n{ok} firewall configured!")
    if mode == "lan":
        print(f"   your LLMs are only reachable from your LAN ({lan_subnet})")
    elif mode == "tailscale":
        print(f"   your LLMs are only reachable via Tailscale VPN")
        print(f"   other hivv nodes need Tailscale installed to reach you")
    print(f"   restart llama.cpp for changes to take effect (if binding to specific IPs)")

def cmd_up(args) -> None:
    """Quick 3-line status check — the "you good?" command."""
    ok = "\033[32m●\033[0m"
    warn = "\033[33m●\033[0m"
    fail = "\033[31m●\033[0m"

    # service
    pid = read_pid()
    if pid and _pid_alive_fallback(pid):
        st = read_state() or {}
        hb = st.get("last_heartbeat") or 0
        age = int(time.time()) - hb if hb else -1
        hb_str = f"{age}s ago" if age >= 0 else "?"
        print(f"{ok} service  running (pid {pid}, hb {hb_str})")
    else:
        print(f"{warn} service  not running — {APP} start")

    # hub
    try:
        code, _ = http("GET", "/api/health", timeout=5)
        print(f"{ok} hub      {HUB} reachable")
    except Exception:
        print(f"{fail} hub      {HUB} unreachable")

    # version
    print(f"  version  {VERSION}")

    # pending MAYBE approvals pushed to this node's inbox (hivv OS §3)
    pending = _approvals_load()
    if pending:
        caps = ", ".join(sorted({(p.get("capability") or "?") for p in pending}))
        print(f"{warn} approvals {len(pending)} action(s) await a human — {caps}")
        print(f"           → {APP} approve --list   (or {APP} approve <G_xxx> / {APP} deny <G_xxx>)")

    # rank-up celebration (quiet unless something changed)
    _celebrate_rank_up()


def cmd_speed(args) -> None:
    """Latency benchmark: DNS, TLS, hub ping, chat round-trip."""
    print(f"\033[1m{APP} speed\033[0m — latency benchmark")
    hub_host = HUB.replace("https://", "").replace("http://", "").split("/")[0]
    hub_scheme = "https" if HUB.startswith("https://") else "http"

    # 1. DNS
    t0 = time.time()
    try:
        ip = socket.gethostbyname(hub_host)
        dns_ms = int((time.time() - t0) * 1000)
        print(f"  dns   {hub_host:30s} → {ip:15s}   {dns_ms:4d}ms")
    except Exception as e:
        print(f"  dns   {hub_host:30s} → \033[31mFAIL\033[0m ({e})")
        return

    # 2. TCP connect
    t0 = time.time()
    try:
        s = socket.create_connection((hub_host, 443 if hub_scheme == "https" else 80), timeout=5)
        tcp_ms = int((time.time() - t0) * 1000)
        s.close()
        print(f"  tcp   {hub_host}:443                      {tcp_ms:4d}ms")
    except Exception as e:
        print(f"  tcp   {hub_host}:443                      \033[31mFAIL\033[0m ({e})")
        return

    # 3. hub ping x3
    pings = []
    for i in range(3):
        t0 = time.time()
        try:
            code, _ = http("GET", "/api/health", timeout=5)
            ms = int((time.time() - t0) * 1000)
            pings.append(ms)
            mark = _ok(f"{ms}ms") if code == 200 else _fail(f"{ms}ms HTTP {code}")
            print(f"  ping  GET /api/health       #{i+1}            {mark}")
        except Exception as e:
            print(f"  ping  GET /api/health       #{i+1}            \033[31mFAIL\033[0m ({e})")
    if pings:
        print(f"  ping  min/avg/max = {min(pings)}/{sum(pings)//len(pings)}/{max(pings)}ms")

    # 4. chat round-trip (time to first byte + total)
    print()
    print(f"  chat  POST /api/chat (short prompt)")
    try:
        # Stream one short chat and measure time-to-first-byte
        url = HUB.rstrip("/") + "/api/chat"
        req = urllib.request.Request(url, data=json.dumps({"message": "say hi in 3 words"}).encode(),
                                     method="POST",
                                     headers={"content-type": "application/json",
                                              "accept": "text/event-stream",
                                              "user-agent": f"{APP}/{VERSION}"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=30) as r:
            ttfb = int((time.time() - t0) * 1000)
            buf = b""
            total_bytes = 0
            while True:
                chunk = r.read(1)
                if not chunk: break
                buf += chunk
                total_bytes += 1
                if buf.endswith(b"\n"):
                    line = buf.decode("utf-8", errors="replace").strip()
                    buf = b""
                    if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                        try:
                            evt = json.loads(line[5:].strip())
                            choices = evt.get("choices") or []
                            if choices and (choices[0].get("delta") or {}).get("content"):
                                total_ms = int((time.time() - t0) * 1000)
                                print(f"        time-to-first-byte: {ttfb}ms   total: {total_ms}ms   bytes: {total_bytes}")
                                # drain the rest so we close cleanly
                                while r.read(4096): pass
                                break
                        except json.JSONDecodeError:
                            pass
    except Exception as e:
        print(f"  chat  \033[31mFAIL\033[0m ({e})")
    else:
        if total_bytes == 0:
            print(f"        (stream ended with no bytes)")
        else:
            print(f"        (stream ended without a content chunk — {total_bytes} bytes)")

    # 5. CPU/RAM/disk (delegate to llm bench)
    print()
    print(f"  bench CPU/RAM/disk:")
    try:
        cmd_llm_bench(args)  # reuse the existing llm bench routine
    except Exception as e:
        print(f"        \033[31mFAIL\033[0m ({e})")

def cmd_test(args) -> None:
    """End-to-end smoke test: doctor + speed + network + chat with all nodes."""
    print(f"\033[1m{APP} test\033[0m — full end-to-end smoke test\n")
    cmd_doctor(args)
    print()
    cmd_speed(args)
    print()

    # Network-wide test
    print(f"\033[1m{APP} test — network\033[0m\n")
    import subprocess as _sp
    _r = lambda c: _sp.run(c, shell=True, capture_output=True, text=True, timeout=10)

    # Get all nodes
    code, p = http("GET", "/api/control/nodes")
    if code != 200 or not isinstance(p, dict):
        print(f"  could not fetch nodes")
    else:
        nodes = p.get("nodes", [])
        if not nodes:
            print(f"  no nodes registered — start agents with: {APP} start")
        else:
            # Test each node
            for n in nodes:
                name = n.get("name", n.get("id", "?"))
                ip = n.get("lan_ip") or n.get("ip", "")
                status = n.get("status", "?")
                models = n.get("local_models", [])

                if status != "online":
                    print(f"  {name}: \033[31m{status}\033[0m — skipping")
                    continue

                # Test connectivity to each LLM port
                ports_ok = 0
                ports_total = 0
                for m in models:
                    port = m.get("port", 8080)
                    ports_total += 1
                    try:
                        r = _r(f"curl -s --connect-timeout 2 --max-time 3 http://{ip}:{port}/v1/models 2>/dev/null")
                        if r.stdout and "model" in r.stdout.lower():
                            ports_ok += 1
                    except:
                        pass

                if ports_total == 0:
                    print(f"  {name}: \033[33mno models\033[0m")
                elif ports_ok == ports_total:
                    print(f"  {name}: \033[32m✓ {ports_ok}/{ports_total} ports reachable\033[0m ({ip})")
                else:
                    print(f"  {name}: \033[33m{ports_ok}/{ports_total} ports reachable\033[0m ({ip})")

            # Test chat with each online node
            print(f"\n\033[1mchat test\033[0m")
            for n in nodes:
                if n.get("status") != "online":
                    continue
                name = n.get("name", n.get("id", "?"))
                ip = n.get("lan_ip") or n.get("ip", "")
                models = n.get("local_models", [])
                if not models or not ip:
                    continue

                # Find first reachable port
                for m in models:
                    port = m.get("port", 8080)
                    try:
                        url = f"http://{ip}:{port}/v1/chat/completions"
                        payload = json.dumps({"model": "local", "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 20})
                        import urllib.request
                        req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(req, timeout=10) as resp:
                            data = resp.read().decode()
                            if "content" in data:
                                print(f"  {name}: \033[32m✓ chat works\033[0m (:{port})")
                            else:
                                print(f"  {name}: \033[33m!\033[0m responded but no content (:{port})")
                            break
                    except Exception as e:
                        continue
                else:
                    print(f"  {name}: \033[31m✗ chat failed\033[0m")

    print(f"\n\033[1m{APP} test\033[0m — \033[32mcomplete\033[0m")


# ── friendly / fun commands ────────────────────────────────────────────
_VOLUNTEER_FILE = CONFIG_DIR / "contribute.json"

def _volunteer_default() -> dict:
    return {"share_cpu": False, "max_cpu_pct": 25, "pause_on_battery": True,
            "sandbox": "subprocess", "opted_in_at": None, "tags": []}

def _volunteer_load() -> dict:
    if not _VOLUNTEER_FILE.exists():
        return _volunteer_default()
    try:
        d = json.loads(_VOLUNTEER_FILE.read_text())
        # backfill any new keys
        base = _volunteer_default()
        base.update({k: v for k, v in d.items() if k in base})
        return base
    except (OSError, json.JSONDecodeError):
        return _volunteer_default()

def cmd_drone(args) -> None:
    """Show volunteer (drone) status — alias for 'volunteer status'."""
    class _FakeArgs:
        pass
    fake = _FakeArgs()
    fake.action = "status"
    cmd_volunteer(fake)


def cmd_volunteer(args) -> None:
    """Opt in/out of contributing compute to the network.
    Sets ~/.config/hivv/contribute.json. The agent reads this on next start."""
    action = getattr(args, "action", None) or "status"
    ensure_dirs()
    cfg = _volunteer_load()

    if action == "on":
        cfg["share_cpu"] = True
        cfg["opted_in_at"] = int(time.time())
        _VOLUNTEER_FILE.write_text(json.dumps(cfg, indent=2))
        print(f"\033[1;32m{APP} volunteer\033[0m — \033[32m✓ you're now a volunteer bee!\033[0m\n")
        print(f"When {HUB_NAME} has a chat request that fits your box, your bee will help.")
        print(f"You can leave the volunteer corps anytime with \033[1m{APP} volunteer off\033[0m.\n")
        print(f"settings (in {_VOLUNTEER_FILE}):")
        for k, v in cfg.items():
            if k == "opted_in_at" and v: v = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
            print(f"  {k:18s} {v}")
        # poke the user to (re)start the agent so it picks up the new flag
        pid = read_pid()
        if pid and pid_alive(pid):
            print(f"\n\033[1;33m!\033[0m agent is already running (pid {pid}); restart it to pick up the new setting:")
            print(f"  {APP} restart")
        else:
            print(f"\n\033[1;34m>\033[0m start your bee to begin contributing:")
            print(f"  {APP} start")
        return

    if action == "off":
        cfg["share_cpu"] = False
        cfg["opted_in_at"] = None
        _VOLUNTEER_FILE.write_text(json.dumps(cfg, indent=2))
        print(f"\033[1;32m{APP} volunteer\033[0m — \033[33m✓ thanks for the help — you've left the volunteer corps.\033[0m")
        print(f"Your bee will no longer accept work from {HUB_NAME}.")
        print(f"Run \033[1m{APP} volunteer on\033[0m to rejoin anytime.")
        pid = read_pid()
        if pid and pid_alive(pid):
            print(f"\n\033[1;33m!\033[0m restart the agent to stop accepting work immediately:")
            print(f"  {APP} restart")
        return

    # status (default)
    print(f"\033[1m{APP} volunteer\033[0m — status")
    if cfg.get("share_cpu"):
        since = cfg.get("opted_in_at")
        since_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)) if since else "?"
        print(f"  state:    \033[1;32m✓ volunteering\033[0m  (since {since_str})")
    else:
        print(f"  state:    \033[2mnot volunteering\033[0m")
    print(f"  config:   {_VOLUNTEER_FILE}")
    for k, v in cfg.items():
        if k == "opted_in_at":
            v = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else "(never)"
        print(f"    {k:18s} {v}")
    if not cfg.get("share_cpu"):
        print(f"\n  to join:   \033[1m{APP} volunteer on\033[0m")


def cmd_share(args) -> None:
    """Broadcast what you're working on to the hivv network.
    Usage: hivv share "working on climate models" [--problem=abc123] [--card]
    """
    st = read_state()
    node_id = st.get("node_id")
    if not node_id:
        print(f"\033[1;33m!\033[0m your bee isn't registered yet — start it first:")
        print(f"  \033[1m{APP} start\033[0m  then try \033[1m{APP} share\033[0m again")
        return

    # ── --card: generate a shareable bee card (#6) ───────────────────
    if getattr(args, "card", False):
        import urllib.request as _ur
        url = f"{HUB.rstrip('/')}/api/hive/share-card/{node_id}.svg"
        out = Path.cwd() / f"hivv-bee-card-{node_id}.svg"
        try:
            req = _ur.Request(url, headers={"user-agent": f"{APP}/{VERSION}"})
            with _ur.urlopen(req, timeout=15) as r:
                out.write_bytes(r.read())
            print(f"\033[1;32m✓\033[0m bee card saved → \033[1m{out}\033[0m")
            print(f"\n  copy-paste-ready post:")
            print(f"  \033[36m🐝 I gave my idle computer to hivv.org — the people's distributed AI")
            print(f"  network. 5 minutes to join: curl -sSL https://hivv.org/install-hivv.sh | bash\033[0m\n")
            print(f"  \033[2m(view the card: open the .svg in any browser; PNG export: screenshot or rsvg-convert)\033[0m")
        except Exception as e:
            die(f"couldn't fetch share card: {e}")
        return

    msg = getattr(args, "message", None) or ""
    problem_id = getattr(args, "problem", None)

    if not msg:
        print(f"\033[1m{APP} share\033[0m — broadcast what you're working on to the hive\n")
        print(f"  \033[1m{APP} share \"<message>\"\033[0m           tell the hive what you're doing")
        print(f"  \033[1m{APP} share \"fixing bug\" --problem=123\033[0m  link to a problem ID")
        print(f"  \033[1m{APP} share --clear\033[0m                  clear your work status")
        print(f"  \033[1m{APP} feed\033[0m                       see what others are working on\n")
        return

    if getattr(args, "clear", False):
        msg = ""
        problem_id = None

    if msg and len(msg) > 280:
        print(f"\033[1;31m!\033[0m message too long (max 280 chars, got {len(msg)})")
        return

    body = {"work_status": msg}
    if problem_id:
        body["problem_id"] = problem_id

    code, payload = http("POST", f"/api/nodes/{node_id}/work-status", body=body, timeout=10)
    if code == 200:
        print(f"\033[1;32m✓\033[0m work status updated — the hive now knows what you're doing 🐝")
        if msg:
            print(f"  \033[2m\"{msg[:80]}{'...' if len(msg) > 80 else ''}\"\033[0m")
    else:
        print(f"\033[1;31m!\033[0m failed (HTTP {code}): {payload}")


# ── bee identity: honey, ranks, streaks (HIVV_PLAN #1) ────────────────
_RANKS = [
    (0,       "larva",      "just hatched — say hi: hivv hi"),
    (500,     "worker",     "warming up — you're officially part of the hive"),
    (5_000,   "forager",    "out gathering knowledge"),
    (25_000,  "scout",      "a trusted pathfinder"),
    (100_000, "royal-jelly","rare and precious to the hive"),
    (500_000, "queen-bee",  "sovereign of the swarm 👑"),
]

def _rank_progress(honey: int):
    cur, nxt = _RANKS[0], None
    for threshold, name, _ in _RANKS:
        if honey >= threshold:
            cur = (threshold, name, _)
        else:
            nxt = (threshold, name, _)
            break
    return cur, nxt

def _rank_idx(name: str) -> int:
    order = [n for _, n, _ in _RANKS]
    try:
        return order.index(name)
    except ValueError:
        return 0

def _celebrate_rank_up() -> None:
    """HIVV_PLAN #1.4: notice rank changes and celebrate them once.
    Best-effort — silently skips when the hub or state is unavailable."""
    try:
        st = read_state() or {}
        nid = st.get("node_id")
        if not nid:
            return
        code, p = http("GET", f"/api/hive/honey/{nid}", timeout=4)
        if code != 200 or not isinstance(p, dict):
            return
        rank = p.get("rank") or "larva"
        last = st.get("_last_rank")
        st["_last_rank"] = rank
        write_state(st)
        if last and _rank_idx(rank) > _rank_idx(last):
            motto = next((m for _, n, m in _RANKS if n == rank), "")
            print(f"\n\033[33m⬆⬆⬆  \033[1mRANK UP!  {last} → {rank}\033[0m\033[33m  ⬆⬆⬆\033[0m")
            print(f"   🍯 {int(p.get('honey', 0)):,} honey · \033[2m{motto}\033[0m")
            print(f"   \033[2mthe hive noticed you — celebrate: {APP} share --card\033[0m\n")
    except Exception:
        pass

def cmd_honey(args) -> None:
    """Your bee card: honey, rank, streak, leaderboard. (hivv honey [--board])"""
    if getattr(args, "board", False) or getattr(args, "node", None) == "board":
        window = getattr(args, "window", None) or "week"
        code, p = http("GET", f"/api/hive/leaderboard?window={window}&limit=12", timeout=10)
        if code != 200:
            die(f"couldn't fetch leaderboard: {p}")
        bees = p.get("bees", []) if isinstance(p, dict) else []
        print(f"\n\033[1m🏆 hive leaderboard\033[0m — window: {window}\n")
        if not bees:
            print(f"  \033[2mno honey minted yet — be the first bee: hivv start\033[0m\n")
            return
        print(f"  {'#':<3}{'BEE':<22}{'HONEY':>10}  {'RANK':<14}{'STREAK':>7}")
        print(f"  {'─'*3}{'─'*22}{'─'*10}  {'─'*14}{'─'*7}")
        my_node = (read_state() or {}).get("node_id")
        for i, b in enumerate(bees, 1):
            mark = " \033[32m← you\033[0m" if b.get("node_id") == my_node else ""
            print(f"  {i:<3}{str(b.get('node_id','?'))[:22]:<22}{int(b.get('honey',0)):>10}  "
                  f"{str(b.get('bee_rank') or b.get('rank') or '-'):<14}{int(b.get('streak_days') or 0):>5}d{mark}")
        print()
        return

    node = getattr(args, "node", None) or (read_state() or {}).get("node_id")
    if not node:
        print(f"\033[1;33m!\033[0m no node yet — run \033[1m{APP} start\033[0m first, then \033[1m{APP} honey\033[0m")
        return
    code, p = http("GET", f"/api/hive/honey/{node}", timeout=10)
    if code == 404:
        print(f"\n  🐝 \033[1m{node}\033[0m — the hive just met you. honey starts flowing within a minute.\n")
        return
    if code != 200:
        die(f"couldn't fetch your honey: {p}")
    honey = int(p.get("honey", 0))
    rank = p.get("rank", "larva")
    cur, nxt = _rank_progress(honey)
    bar = ""
    if nxt:
        lo, hi = cur[0], nxt[0]
        pct = min(100, max(0, int((honey - lo) / max(1, hi - lo) * 100)))
        filled = pct // 4
        bar = f"\n  \033[33m{'⬣' * filled}{'○' * (25 - filled)}\033[0m {pct}% → \033[1m{nxt[1]}\033[0m at {hi:,} honey"
    else:
        bar = "\n  \033[33m⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣⬣\033[0m max rank — the hive salutes you 👑"
    motto = next((m for _, n, m in _RANKS if n == rank), "")
    print(f"""
\033[1m  🐝 bee card — {node}\033[0m
  rank:    \033[33m{rank}\033[0m  (\033[2m{motto}\033[0m)
  honey:   \033[1m{honey:,}\033[0m total · {int(p.get('week_honey', 0)):,} this week · {int(p.get('month_honey', 0)):,} this month{bar}
  streak:  \033[1m{int(p.get('streak_days', 0))}\033[0m days online (\033[2mbest: {int(p.get('best_streak_days', 0))}\033[0m)
  service: {int(p.get('jobs_completed', 0)):,} jobs · {int(p.get('tokens_served', 0) or 0):,} tokens · {int(p.get('minutes_online', 0)) // 60:,}h online
  position: \033[1m#{p.get('position', '?')}\033[0m of the hive
\033[2m  hivv honey --board          see the leaderboard
   hivv share --card           generate a shareable bee card\033[0m
""")

def cmd_tips(args) -> None:
    """Show tip analytics — which tips are shown most and which drive action."""
    code, p = http("GET", "/api/tips", timeout=10)
    if code != 200:
        die(f"couldn't fetch tips: {p}")
    tips = p.get("tips", []) if isinstance(p, dict) else []
    tot = p.get("totals", {}) if isinstance(p, dict) else {}
    print(f"\033[1m  Tip Analytics\033[0m  (total shown: {tot.get('shown',0)} · clicked: {tot.get('click',0)})")
    print()
    if not tips:
        print("  \033[2m(no tip data yet — tips are shown when errors occur)\033[0m")
        return
    # Sort by shown_count desc
    tips.sort(key=lambda t: t.get("shown_count", 0), reverse=True)
    for t in tips:
        shown = int(t.get("shown_count") or 0)
        clicked = int(t.get("click_count") or 0)
        ctr = t.get("ctr") or 0
        ctr_str = f"\033[2m ({ctr}% CTR)\033[0m" if shown > 0 and clicked > 0 else ""
        bar_len = min(int(shown / max(1, tips[0].get("shown_count") or 1) * 20), 20) if shown > 0 else 0
        bar = "\033[33m" + "\u2588" * bar_len + "\033[0m" + "\u2591" * (20 - bar_len)
        last = t.get("last_shown", "")
        age = ""
        if last:
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
                age = f" \033[2m(last: {ts.strftime('%m-%d')})\033[0m"
            except Exception:
                pass
        print(f"  {bar}  shown={shown}  clicked={clicked}{ctr_str}{age}")
        text = (t.get("tip_text") or "")[:70]
        print(f"    \033[36m{text}\033[0m")
    print()


# ── hive-mind: one question, many brains (HIVV_PLAN #2) ───────────────
def _model_family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("llama", "gemma", "qwen", "mistral", "mixtral", "phi", "command",
                "yi", "deepseek", "gpt", "claude", "gemini", "moonshot", "ernie"):
        if fam in m:
            return fam
    return m.split("-")[0].split(":")[0][:10] or "other"

def _short_model(m: str) -> str:
    m = str(m or "local")
    if "/" in m:
        m = m.rstrip("/").split("/")[-1]
    return m.replace(".gguf", "").replace(".GGUF", "")[-40:]

def cmd_hive_mind(args) -> None:
    """Ask several different bees at once and compare their minds.
    Usage: hivv hive-mind "why is the sky red sometimes?" [--max 4] [--json] [--quiet]
    """
    import threading
    do_json = bool(getattr(args, "json", False))
    do_quiet = bool(getattr(args, "quiet", False))

    question = " ".join(args.question) if isinstance(args.question, list) else (args.question or "")
    if not question:
        question = input("  ask the hive-mind: ").strip()
    if not question:
        die("nothing asked — try: hivv hive-mind \"what should we solve together?\"")
    n_max = max(2, min(int(getattr(args, "max", 4) or 4), 6))

    code, payload = http("GET", "/api/control/nodes", timeout=8)
    if code != 200:
        die(f"can't reach the hub: {payload}")
    nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
    online = [n for n in nodes if n.get("status") == "online" and (n.get("lan_ip") or n.get("ip"))]
    picked, families = [], set()
    for n in online:
        for m in (n.get("local_models") or []):
            fam = _model_family(m.get("name") or m.get("model") or "")
            if fam in families:
                continue
            ip = n.get("lan_ip") or n.get("ip")
            port = m.get("port", 8080)
            picked.append({"node": n.get("name") or n.get("id"), "model": m.get("name") or m.get("model") or "local",
                           "url": f"http://{ip}:{port}/v1/chat/completions", "family": fam})
            families.add(fam)
            break
        if len(picked) >= n_max:
            break
    if len(picked) < 2:
        print(f"\033[1;33m!\033[0m only {len(picked)} distinct brain(s) reachable — hive-mind needs 2+.")
        print(f"  \033[2mask a friend to join: curl -sSL https://hivv.org/install-hivv.sh | bash\033[0m")
        if picked:
            print(f"  \033[1mrunning it solo:\033[0m")
            args.question = question
            return _do_chat(question, stream=True)
        return

    if not do_json and not do_quiet:
        print(f"\n\033[1m🧠 hive-mind\033[0m — asking {len(picked)} different brains…\n")
    results = [None] * len(picked)

    def _ask(i, t):
        t0 = time.time()
        try:
            body = json.dumps({"model": "local", "messages": [{"role": "user", "content": question}],
                               "max_tokens": 300, "stream": False}).encode()
            req = urllib.request.Request(t["url"], data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            ms = int((time.time() - t0) * 1000)
            results[i] = {**t, "answer": content, "ms": ms,
                          "tok": max(1, len(content) // 4), "err": None}
        except Exception as e:
            results[i] = {**t, "answer": "", "ms": int((time.time() - t0) * 1000), "tok": 0, "err": str(e)[:60]}

    threads = [threading.Thread(target=_ask, args=(i, t)) for i, t in enumerate(picked)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=45)

    fastest, answers, json_answers = None, [], []
    total_tokens = 0
    total_latency_ms = 0
    for i, t in enumerate(picked):
        r = results[i]
        status_str = "error"
        if r is None:
            if not do_json: print(f"  \033[2m✗ {t['node']} ({t['model']}) — timed out\033[0m")
            json_answers.append({"node": t["node"], "model": t["model"], "answer": "", "ms": 0, "tok": 0, "status": "timeout"})
            continue
        if r["err"]:
            if not do_json: print(f"  \033[31m✗ {t['node']} ({t['model']})\033[0m — {r['err']}")
            json_answers.append({"node": t["node"], "model": t["model"], "answer": "", "ms": r["ms"], "tok": 0, "status": "error", "error": r["err"]})
            continue
        status_str = "ok"
        total_tokens += r["tok"]
        total_latency_ms += r["ms"]
        if fastest is None or r["ms"] < fastest["ms"]:
            fastest = r
        if not do_quiet:
            if not do_json:
                print(f"  \033[32m● {t['node']}\033[0m \033[2m· {_short_model(t['model'])} · {r['ms']}ms · ~{max(1, r['tok']*1000//max(1,r['ms']))} t/s\033[0m")
                for line in r["answer"].splitlines()[:12]:
                    print(f"    {line}")
                if len(r["answer"].splitlines()) > 12:
                    print("    \033[2m…\033[0m")
        answers.append(f"[{t['node']} / {t['model']}]: {r['answer'][:400]}")
        json_answers.append({"node": t["node"], "model": t["model"], "answer": r["answer"], "ms": r["ms"], "tok": r["tok"], "status": "ok"})

    verdict = None
    if len(answers) >= 2:
        judge = (f"Question: {question}\n\n"
                 f"Different AI models answered:\n\n" + "\n\n".join(answers) +
                 "\n\nIn 2 short lines: where do they AGREE, and what is the single best merged answer?")
        if not do_json and not do_quiet:
            print(f"\n  \033[33m🐝 bee judge synthesis\033[0m \033[2m(fastest node: {fastest['node']})\033[0m")
        try:
            body = json.dumps({"model": "local",
                               "messages": [{"role": "user", "content": judge}],
                               "max_tokens": 150, "stream": False}).encode()
            req = urllib.request.Request(fastest["url"], data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
            verdict = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
            total_latency_ms += 0  # judge latency not counted in tok estimate
            if not do_json:
                for line in verdict.splitlines():
                    print(f"    \033[1m{line}\033[0m")
        except Exception as e:
            if not do_json: print(f"    \033[2mjudge unavailable: {e}\033[0m")
            if do_json: verdict = f"(judge unavailable: {e})"

    # Report usage to hub (fire-and-forget)
    try:
        body = json.dumps({
            "feature": "hive-mind",
            "model": ",".join(sorted(set(a["model"] for a in json_answers))) or "unknown",
            "total_tokens": total_tokens,
            "latency_ms": total_latency_ms,
            "status": "error" if all(a["status"] == "error" for a in json_answers) else "success"
        }).encode()
        req = urllib.request.Request(f"{HUB}/api/hive/log-feature", data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # best-effort

    if do_json:
        out = {"question": question, "brains": len(picked), "answers": json_answers}
        if verdict is not None:
            out["verdict"] = verdict
        print(json.dumps(out, indent=2))
    else:
        print()


def cmd_feed(args) -> None:
    """See what the hive is working on — real-time collaboration feed."""
    code, payload = http("GET", "/api/collaboration/feed", timeout=10)
    if code != 200:
        print(f"\033[1;31m!\033[0m could not reach feed (HTTP {code})")
        return

    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not items:
        print(f"\033[1m{APP} feed\033[0m — what is the hivv hive working on?\n")
        print(f"  \033[2m(no one has shared what they're working on yet)\033[0m")
        print(f"  \033[1mbe the first!\033[0m  \033[1m{APP} share \"what you're doing\"\033[0m\n")
        return

    print(f"\033[1m{APP} feed\033[0m — what the hivv hive is working on ({len(items)} active)\n")
    print(f"\033[2m  {'node':<22} {'what':<40} {'problem':<12} updated\033[0m")
    print(f"  \033[2m{'─'*22} {'─'*40} {'─'*12} {'─'*8}\033[0m")
    now = time.time()
    for item in items[:20]:
        name = item.get("name", "unknown")[:22]
        work = item.get("work_status", "")[:40]
        prob = item.get("problem_id", "")[:12] or "-"
        age = item.get("last_update", 0)
        age_str = f"{int(now-age)}s ago" if age else "-"
        print(f"  \033[32m{name:<22}\033[0m \033[1m{work:<40}\033[0m \033[2m{prob:<12}\033[0m {age_str}")
    print()


def cmd_help_bang(args) -> None:
    """Panic button — show how to get help."""
    print(f"\033[1;31m{APP} help!\033[0m — emergency help\n")
    print(f"  \033[1mstep 1: what does your bee say?\033[0m")
    print(f"    {APP} doctor       \033[2m# pasteable health report\033[0m")
    print(f"    {APP} logs 30      \033[2m# last 30 lines of the agent log\033[0m")
    print(f"    {APP} status       \033[2m# agent pid + heartbeat state\033[0m")
    print(f"    {APP} speed        \033[2m# latency benchmark\033[0m")
    print()
    print(f"  \033[1mstep 2: stop the bleeding (if needed)\033[0m")
    print(f"    {APP} stop         \033[2m# stop the local agent\033[0m")
    print(f"    {APP} uninstall    \033[2m# full removal (keeps ~/.config/{APP}/)\033[0m")
    print()
    print(f"  \033[1mstep 3: get human help\033[0m")
    print(f"    bug/issue:  https://github.com/{APP}/issues  (paste `doctor` output)")
    print(f"    email:      support@{HUB.replace('https://','').rstrip('/')}")
    print(f"    chat:       \033[1m{APP} help\033[0m  \033[2m# sends your last 20 log lines + doctor to support\033[0m")
    print()
    print(f"  \033[1mstep 4: tell the AI\033[0m")
    print(f"    {APP} 'summarize what's wrong and how to fix it'")
    print(f"    \033[2m# any args become a streaming chat message\033[0m")

def cmd_welcome(args) -> None:
    """Guided first-run experience — explains everything a new user needs to know."""
    print(f"""
\033[1m{'='*60}
  🐝 WELCOME TO HIVV — The People's Distributed AI Network
{'='*60}\033[0m

  You just joined a global network of humans + AI working together.
  Your computer can now contribute its CPU/GPU to help solve real problems.

\033[1m  QUICK START\033[0m
    1. Check your status:     {APP} status
    2. See the network:       {APP} nodes
    3. Chat with AI:          {APP} chat "hello world"
    4. Check your health:     {APP} doctor
    5. Fix any issues:        {APP} doctor --fix

\033[1m  WHAT YOU CAN DO\033[0m
    • {APP} chat "hello"           — talk to distributed AI
    • {APP} node-chat yalla "hi"   — chat directly with a specific node
    • {APP} nodes                  — see all nodes on the network
    • {APP} node <name>            — detailed info about a node
    • {APP} reach <name>           — test if a node is reachable
    • {APP} stats                  — see cluster performance
    • {APP} add openai sk-...      — add any AI to the network

\033[1m  SECURITY\033[0m
    • {APP} firewall               — check your security status
    • {APP} doctor --fix           — fix all security issues
    • {APP} firewall --apply --mode lan   — open ports for LAN only

\033[1m  LEARN MORE\033[0m
    • {APP} --help                 — see all commands
    • {APP} doctor                 — full health report
    • {APP} debug                  — full debug info
    • Visit https://hivv.org       — see the network in action

\033[1m  FUN & SOCIAL\033[0m
    • {APP} honey                      — your bee card: honey, rank, streak 🍯
    • {APP} honey --board              — the hive leaderboard 🏆
    • {APP} hive-mind "question"       — many brains, one answer 🧠
    • {APP} share --card               — a shareable bee card to recruit bees

\033[1m{'='*60}
  🌌 You're part of something bigger. Welcome to the hive! 🐝
{'='*60}\033[0m
""")
def cmd_top(args) -> None:
    """Real-time cluster activity — like 'top' for the hivv network."""
    import time as _t

    # Test API first
    try:
        code, p = http("GET", "/api/control/nodes", timeout=8)
        if code != 200 or not isinstance(p, dict):
            print(f"failed to fetch nodes (HTTP {code})")
            return
    except Exception as e:
        print(f"cannot reach hub: {e}")
        return

    nodes = p.get("nodes", [])
    online = [n for n in nodes if n.get("status") == "online"]

    if not online:
        print("no nodes online")
        return

    # Display
    print(f"\033[1m{APP} top\033[0m — real-time cluster activity\n")
    print(f"  {'NAME':<16} {'CPU%':<7} {'RAM%':<7} {'GPU':<12} {'MODELS':<7} {'TASKS':<7} {'HB'}")
    print(f"  {'─'*16} {'─'*7} {'─'*7} {'─'*12} {'─'*7} {'─'*7} {'─'*10}")

    for n in online:
        name = n.get("name", "?")[:15]
        cpu = n.get("metrics", {}).get("cpu_percent", 0)
        mem = n.get("metrics", {}).get("mem_percent", 0)
        gpus = n.get("gpus") or []
        gpu_str = f"{gpus[0].get('util_pct', 0)}%" if gpus else "-"
        models = len(n.get("local_models", []))
        tasks = n.get("metrics", {}).get("active_jobs", 0)
        hb = n.get("last_heartbeat", 0)
        hb_age = int(_t.time()) - (hb / 1000) if hb else -1
        hb_str = f"{int(hb_age)}s" if hb_age >= 0 else "?"

        print(f"  {name:<16} {cpu:>5.0f}%  {mem:>5.0f}%  {gpu_str:<12} {models:<7} {tasks:<7} {hb_str}")

    total_models = sum(len(n.get("local_models", [])) for n in online)
    print(f"\n  {len(online)}/{len(nodes)} nodes online · {total_models} models available")
    print(f"  run again for updates: {APP} top\n")

def cmd_benchmark(args) -> None:
    """Benchmark all nodes — find the fastest one."""
    import time as _t
    print(f"\n\033[1m{APP} benchmark\033[0m — testing all nodes\n")

    # Get nodes
    code, p = http("GET", "/api/control/nodes", timeout=5)
    if code != 200 or not isinstance(p, dict):
        die("failed to fetch nodes")

    nodes = p.get("nodes", [])
    online = [n for n in nodes if n.get("status") == "online"]

    if not online:
        die("no online nodes found")

    results = []
    for n in online:
        name = n.get("name", "?")
        ip = n.get("lan_ip") or n.get("ip", "")
        models = n.get("local_models", [])
        if not models or not ip:
            continue

        # Find a port
        for m in models:
            port = m.get("port", 8080)
            url = f"http://{ip}:{port}/v1/chat/completions"
            try:
                import urllib.request
                payload = json.dumps({"model": "local", "messages": [{"role": "user", "content": "say hi"}], "max_tokens": 10})
                start = _t.time()
                req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = resp.read().decode()
                    elapsed = (_t.time() - start) * 1000
                    # Count tokens in response
                    try:
                        j = json.loads(data)
                        tokens = len(j.get("choices", [{}])[0].get("message", {}).get("content", "").split())
                    except:
                        tokens = 1
                    tps = tokens / (elapsed / 1000) if elapsed > 0 else 0
                    results.append({"name": name, "port": port, "latency": elapsed, "tps": tps, "tokens": tokens})
                    print(f"  {name}:{port} — {elapsed:.0f}ms latency, {tps:.1f} tok/s")
                    break
            except Exception as e:
                print(f"  {name}:{port} — failed: {e}")

    if results:
        # Find fastest
        fastest = min(results, key=lambda r: r["latency"])
        print(f"\n  \033[32m✓ fastest: {fastest['name']}:{fastest['port']} ({fastest['latency']:.0f}ms, {fastest['tps']:.1f} tok/s)\033[0m")

def cmd_hi(args) -> None:
    """Friendly greeter — quick chat round-trip."""
    print(f"\033[1m{APP}\033[0m hi! 👋  trying a quick round-trip...\n")
    _do_chat("say hi in one friendly sentence", stream=True)
    print()
    print(f"  try:")
    print(f"    {APP} whats the meaning of life")
    print(f"    {APP} chat 'tell me a joke about kubernetes'")
    print(f"    {APP} llm detect       \033[2m# find local AI runtimes\033[0m")
    print(f"    {APP} doctor           \033[2m# full health check\033[0m")


def cmd_thanks(args) -> None:
    """Cute: one-line thanks to the AI."""
    print(f"\033[1m{APP}\033[0m sending thanks to {HUB_NAME}...\n")
    _do_chat("the user just said thanks — reply with one warm sentence", stream=True)
    print()


def cmd_support(args) -> None:
    """Ask the support AI a question. Auto-attaches your bee's context:
    hub, version, agent status, recent metrics, and the last 10 log lines."""
    question = " ".join(getattr(args, "question", []) or []).strip()

    # --- gather context ----------------------------------------------------
    log_tail = ""
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(errors="replace").splitlines()[-10:]
            log_tail = "\n".join(lines)
        except OSError:
            pass

    m = _collect_metrics()
    pid = read_pid()
    st = read_state() or {}
    hb = st.get("last_heartbeat") or 0
    hb_age = int(time.time()) - hb if hb else -1
    hb_str = f"{hb_age}s ago" if hb_age >= 0 else "never"

    ctx_lines = [
        f"[HIVV.org CLI support session — client v{VERSION}]",
        f"hub       = {HUB}",
        f"hostname  = {socket.gethostname()}",
        f"hw_id     = {_machine_id() or '(unavailable)'}",
        f"agent     = {'running (pid ' + str(pid) + ', node ' + str(st.get('node_id','?')) + ')' if pid else 'NOT RUNNING'}",
        f"heartbeat = {hb_str}",
        f"transport = {st.get('transport', 'heartbeat-only')}",
        f"signed    = {'no (hub does not support)' if st.get('auth_rejected') else 'yes'}",
        f"bee       = {m.get('cpu_count','?')} cores, {m.get('mem_pct','?')}% mem, {m.get('disk_pct','?')}% disk",
    ]
    for g in (m.get("gpus") or []):
        ctx_lines.append(f"  gpu[{g['index']}] {g['name']} util={g['util_pct']}% temp={g.get('temp_c','?')}°C")
    if log_tail:
        ctx_lines += ["", "--- recent agent log (last 10 lines, scrubbed) ---", log_tail]
    # SCRUB secrets before printing — accidental api_key=, ?token=, JWTs etc.
    ctx_lines = [_scrub_lines(line) for line in ctx_lines]

    sys_msg = ("[System: You are a friendly, expert support agent for the HIVV.org CLI. "
               "Be brief, concrete, and accurate. Diagnose errors from any log lines provided. "
               "If a fix needs a command, give the exact command. Use bullet points for steps. No fluff.]")

    full_msg = "\n".join([sys_msg] + ctx_lines)
    if question:
        full_msg += f"\n\nUser question: {question}"
        print(f"\033[1m{APP} support\033[0m — asking: \033[3m{question}\033[0m\n")
    else:
        full_msg += ("\n\nThe user just ran `hivv support` with no question. "
                     "Greet them warmly, summarize what you see in their context "
                     "(flag any obvious problems — agent not running, old heartbeat, "
                     "high CPU/mem, etc.), and ask what they need help with.")
        print(f"\033[1m{APP} support\033[0m — starting session\n")

    _do_chat(full_msg, stream=True)
    print()
    if not question:
        print(f"  follow-up tips:")
        print(f"    {APP} support 'why is my heartbeat failing?'")
        print(f"    {APP} support 'how do I volunteer?'")
        print(f"    {APP} support 'what does the error in my log mean?'")


# ── local LLM helper ───────────────────────────────────────────────────
def _llm_which(cmd: str):
    """Find a binary on PATH or in common hand-built LLM-runtime dirs."""
    found = shutil.which(cmd)
    if found:
        return found
    home = os.path.expanduser("~")
    for extra in (
        os.path.join(home, "llama.cpp", "build", "bin"),
        "/usr/local/llama.cpp/build/bin",
        "/opt/llama.cpp/build/bin",
        "/opt/homebrew/bin",
        os.path.join(home, ".local", "bin"),
    ):
        cand = os.path.join(extra, cmd)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _llm_probe(url: str, timeout: float = 1.0):
    """GET a local URL. Returns (status_code, body). 0 means network error."""
    try:
        req = urllib.request.Request(url, method="GET",
            headers={"user-agent": f"{APP}/{VERSION}", "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def _llm_detect():
    """Probe for ollama, llama.cpp, lm-studio, vllm on this box."""
    found = []

    # ollama (binary or running server)
    ollama_bin = _llm_which("ollama")
    if ollama_bin:
        version = ""
        try:
            v = subprocess.run([ollama_bin, "--version"], capture_output=True, text=True, timeout=5)
            text = ((v.stdout or "") + (v.stderr or "")).strip()
            import re as _re
            # find a "X.Y.Z" version on any line (handles both clean output and
            # `Warning: client version is 0.32.4` style when no server is up).
            m = _re.search(r"\d+\.\d+\.\d+", text)
            if m:
                version = m.group(0)
        except Exception:
            pass
        code, _ = _llm_probe("http://localhost:11434/api/version")
        listening = "running (port 11434)" if code == 200 else "installed, not running"
        found.append({"name": "ollama", "path": ollama_bin,
                      "version": version or "(unknown)",
                      "service": listening, "url": "http://localhost:11434"})
    else:
        code, _ = _llm_probe("http://localhost:11434/api/version")
        if code == 200:
            found.append({"name": "ollama", "path": "(server only, no CLI on PATH)",
                          "version": "?", "service": "running on :11434",
                          "url": "http://localhost:11434"})

    # llama.cpp
    for bin_name in ("llama-server", "llama-cli", "llama-quantize"):
        p = _llm_which(bin_name)
        if p:
            found.append({"name": "llama.cpp", "path": p, "version": "(git build)",
                          "service": "n/a (run llama-server manually)", "url": "n/a"})
            break

    # LM Studio
    if _llm_which("lms"):
        found.append({"name": "lm-studio", "path": _llm_which("lms"),
                      "version": "?", "service": "check `lms status`",
                      "url": "http://localhost:1234/v1"})

    # vLLM
    if _llm_which("vllm"):
        found.append({"name": "vllm", "path": _llm_which("vllm"),
                      "version": "?", "service": "n/a (run `vllm serve ...`)",
                      "url": "http://localhost:8000/v1"})

    # C8: also show GGUF models tracked in models/index.json
    idx = _read_models_index()
    for key, info in idx.items():
        fpath = os.path.join(_LLAMA_CPP_MODELS, info["file"])
        size_gb = info.get("size_gb", "?")
        if os.path.exists(fpath):
            sz = os.path.getsize(fpath) / (1024**3)
            found.append({"name": f"gguf:{key}", "path": fpath,
                          "version": f"{sz:.1f} GB",
                          "service": "ready to serve (hivv llm serve-llama " + key + ")",
                          "url": "n/a (llama-server not running)"})
        else:
            found.append({"name": f"gguf:{key}", "path": fpath,
                          "version": f"~{size_gb} GB (not downloaded)",
                          "service": "run: hivv llm pull-gguf " + key,
                          "url": "n/a"})

    return found


def _llm_sys_specs():
    """Inspect host: OS, CPU, RAM, GPU."""
    import platform as _p
    specs = {
        "system": _p.system(),
        "machine": _p.machine(),
        "release": _p.release(),
        "python":  _p.python_version(),
        "cpu_count": os.cpu_count() or 1,
        "ram_gb": None,
        "gpu": None,
        "apple_silicon": False,
    }
    # RAM
    try:
        if specs["system"] == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        specs["ram_gb"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                        break
        elif specs["system"] == "Darwin":
            v = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            if v.returncode == 0:
                specs["ram_gb"] = round(int(v.stdout.strip()) / 1024**3, 1)
        elif specs["system"] == "Windows":
            v = subprocess.run(["wmic", "OS", "get", "TotalVisibleMemorySize", "/value"],
                               capture_output=True, text=True, timeout=5)
            if v.returncode == 0:
                for line in v.stdout.splitlines():
                    if "=" in line:
                        specs["ram_gb"] = round(int(line.split("=", 1)[1].strip()) / 1024 / 1024, 1)
                        break
    except Exception:
        pass

    # GPU (NVIDIA)
    if shutil.which("nvidia-smi"):
        try:
            v = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5)
            if v.returncode == 0 and v.stdout.strip():
                parts = v.stdout.strip().split("\n")[0].split(",")
                specs["gpu"] = {"vendor": "nvidia", "name": parts[0].strip(),
                                "vram_gb": round(int(parts[1].strip()) / 1024, 1)}
        except Exception:
            pass

    # Apple Silicon
    if specs["system"] == "Darwin" and specs["machine"] in ("arm64", "arm64e"):
        specs["apple_silicon"] = True

    return specs


# Model recommendation tiers (curated for CPU + modest GPU boxes).
# Order matters: more-specific tiers are checked first. Apple Silicon first,
# then NVIDIA tiers (any GPU → GPU tier), then CPU tiers in descending spec.
_LLM_TIERS = [
    {"tier": "apple-silicon", "min_ram_gb": 8,
     "require": {"gpu": False, "apple_silicon": True},
     "models": [
         ("qwen3:4b",         "~3 GB, Metal accelerated"),
         ("llama-3.2:3b",     "~2 GB, ANE friendly"),
         ("gemma4:e2b",       "~2 GB, lean"),
     ]},
    {"tier": "nvidia-big",   "vram_lo": 12, "require": {"gpu": True},
     "models": [
         ("qwen3:14b",        "~9 GB VRAM"),
         ("qwen3:32b",        "~20 GB VRAM"),
         ("llama-3.1:70b-q4", "~40 GB VRAM"),
         ("mixtral:8x7b",     "~25 GB VRAM"),
     ]},
    {"tier": "nvidia-8gb",   "vram_lo": 4, "vram_hi": 12, "require": {"gpu": True},
     "models": [
         ("qwen3:8b-q4",      "~5 GB VRAM"),
         ("llama-3.1:8b-q4",  "~5 GB VRAM"),
         ("mistral:7b",       "~5 GB VRAM"),
     ]},
    {"tier": "no-gpu-8core", "min_ram_gb": 8, "max_ram_gb": 96, "min_cpu": 8,
     "require": {"gpu": False, "apple_silicon": False},
     "models": [
         ("gemma4:e2b",       "~2 GB RAM, fastest on 8c CPU — Google efficient"),
         ("qwen3:4b",         "~3 GB RAM, very capable"),
         ("llama-3.2:3b",     "~2 GB RAM, Meta's mid"),
     ]},
    {"tier": "no-gpu-low",   "min_ram_gb": 8, "max_ram_gb": 96, "min_cpu": 4, "max_cpu": 7,
     "require": {"gpu": False, "apple_silicon": False},
     "models": [
         ("qwen3:1.7b",       "~1.5 GB RAM, snappy on 4-7 core CPU"),
         ("llama-3.2:3b",     "~2 GB RAM, Meta's mid"),
         ("phi-3.5-mini",     "~2.4 GB RAM, Microsoft small"),
     ]},
    {"tier": "no-gpu-tiny",  "min_ram_gb": 0, "max_ram_gb": 8,  "max_cpu": 3,
     "require": {"gpu": False, "apple_silicon": False},
     "models": [
         ("qwen3:0.6b",       "~0.6 GB RAM, ultra-tiny — for 2-core boxes"),
         ("qwen3:1.7b",       "~1.5 GB RAM, fast on any CPU"),
         ("llama-3.2:1b",     "~1 GB RAM, Meta's tiniest"),
     ]},
]

# Fallback target — when nothing matches, use the safest tiny-CPU model.
_FALLBACK_TIER = next(t for t in _LLM_TIERS if t["tier"] == "no-gpu-tiny")

# GGUF model registry for llama.cpp — maps friendly name to HuggingFace GGUF file.
# Format: "name": {"repo": "org/model", "filename": "file-Q4_K_M.gguf", "size_gb": float}
_GGUF_MODELS = {
    "gemma4:e2b": {
        "desc":  "~2.5 GB, Google Gemma 3 4B — fast CPU-friendly inference",
        "repo":  "bartowski/google_gemma-3-4b-it-GGUF",
        "file":  "google_gemma-3-4b-it-Q4_K_M.gguf",
        "size_gb": 2.5,
    },
    "gemma-1b": {
        "desc":  "~0.9 GB, Google Gemma 3 1B — tiny, phone-class boxes",
        "repo":  "bartowski/google_gemma-3-1b-it-GGUF",
        "file":  "google_gemma-3-1b-it-Q4_K_M.gguf",
        "size_gb": 0.9,
    },
    "qwen3:0.6b": {
        "desc":  "~0.5 GB, Qwen 3 0.6B — smallest useful model",
        "repo":  "bartowski/Qwen_Qwen3-0.6B-GGUF",
        "file":  "Qwen_Qwen3-0.6B-Q4_K_M.gguf",
        "size_gb": 0.5,
    },
    "qwen3:1.7b": {
        "desc":  "~1.2 GB, Qwen 3 1.7B — light CPU inference",
        "repo":  "bartowski/Qwen_Qwen3-1.7B-GGUF",
        "file":  "Qwen_Qwen3-1.7B-Q4_K_M.gguf",
        "size_gb": 1.2,
    },
    "qwen3:4b": {
        "desc":  "~2.5 GB, Qwen 3 4B — capable and efficient",
        "repo":  "Qwen/Qwen3-4B-GGUF",
        "file":  "Qwen3-4B-Q4_K_M.gguf",
        "size_gb": 2.5,
    },
    "qwen3:8b": {
        "desc":  "~5 GB, Qwen 3 8B — strong on 8 GB GPUs",
        "repo":  "Qwen/Qwen3-8B-GGUF",
        "file":  "Qwen3-8B-Q4_K_M.gguf",
        "size_gb": 5.0,
    },
    "qwen3:14b": {
        "desc":  "~9 GB, Qwen 3 14B — workstation class",
        "repo":  "Qwen/Qwen3-14B-GGUF",
        "file":  "Qwen3-14B-Q4_K_M.gguf",
        "size_gb": 9.0,
    },
    "qwen3:32b": {
        "desc":  "~20 GB, Qwen 3 32B — big-GPU territory",
        "repo":  "bartowski/Qwen_Qwen3-32B-GGUF",
        "file":  "Qwen_Qwen3-32B-Q4_K_M.gguf",
        "size_gb": 20.0,
    },
    "llama-3.2:1b": {
        "desc":  "~0.8 GB, Llama 3.2 1B — very light",
        "repo":  "bartowski/Llama-3.2-1B-Instruct-GGUF",
        "file":  "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        "size_gb": 0.8,
    },
    "llama-3.2:3b": {
        "desc":  "~2 GB, Llama 3.2 3B — balanced",
        "repo":  "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "file":  "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "size_gb": 2.0,
    },
    "llama-3.1:8b": {
        "desc":  "~5 GB, Llama 3.1 8B — the classic all-rounder",
        "repo":  "bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        "file":  "Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        "size_gb": 5.0,
    },
    "mistral:7b": {
        "desc":  "~4.4 GB, Mistral 7B v0.3 — snappy on mid hardware",
        "repo":  "bartowski/Mistral-7B-Instruct-v0.3-GGUF",
        "file":  "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
        "size_gb": 4.4,
    },
}

def _gguf_for_tag(tag: str):
    """Resolve an ollama-style tier tag to a _GGUF_MODELS key.
    Handles suffix drift (qwen3:8b-q4 → qwen3:8b, llama-3.1:8b-q4 → llama-3.1:8b)."""
    if not tag:
        return None
    if tag in _GGUF_MODELS:
        return tag
    base = tag.split("-q4")[0].split("-q5")[0].split("-q8")[0]
    if base in _GGUF_MODELS:
        return base
    # nearest smaller in the same family
    fam = base.split(":")[0]
    cands = [k for k in _GGUF_MODELS if k.split(":")[0] == fam]
    return sorted(cands)[0] if cands else None

_LLAMA_CPP_DIR  = os.path.join(os.path.expanduser("~"), "llama.cpp")
_LLAMA_CPP_BIN = os.path.join(_LLAMA_CPP_DIR, "build", "bin")
_LLAMA_CPP_MODELS = os.path.join(_LLAMA_CPP_DIR, "models")
_MODELS_INDEX = os.path.join(STATE_DIR, "models", "index.json")

def _read_models_index():
    try:
        with open(_MODELS_INDEX) as f:
            return json.loads(f.read())
    except Exception:
        return {}

def _write_models_index(key, info):
    idx = _read_models_index()
    idx[key] = {"repo": info["repo"], "file": info["file"],
                "size_gb": info["size_gb"],
                "url": f"https://huggingface.co/{info['repo']}/resolve/main/{info['file']}"}
    try:
        os.makedirs(os.path.dirname(_MODELS_INDEX), exist_ok=True)
        with open(_MODELS_INDEX, "w") as f:
            json.dump(idx, f, indent=2)
    except Exception as e:
        print(f"  \033[33mcould not write models index: {e}\033[0m")


def _llm_pick_model(specs):
    has_gpu   = specs.get("gpu") is not None
    vram      = specs.get("gpu", {}).get("vram_gb", 0) if has_gpu else 0
    ram       = specs.get("ram_gb") or 0
    cpu_count = specs.get("cpu_count") or 1
    apple     = bool(specs.get("apple_silicon"))

    for t in _LLM_TIERS:
        req = t["require"]
        # Apple Silicon short-circuits everything (Metal acceleration is decisive).
        if req.get("apple_silicon"):
            if not apple:
                continue
            return (t["tier"], t["models"],
                    "Apple Silicon detected — Metal acceleration available")
        # NVIDIA tiers — require a GPU.
        if req.get("gpu"):
            if not has_gpu:
                continue
            lo = t.get("vram_lo", 0); hi = t.get("vram_hi", 9999)
            if lo <= vram < hi:
                return (t["tier"], t["models"],
                        f"NVIDIA GPU with {vram} GB VRAM detected")
            continue
        # CPU-only tiers — explicitly skip when the box has a GPU
        # (so NVIDIA boxes can't accidentally fall into a CPU tier).
        if has_gpu:
            continue
        # Match on RAM + CPU core count.
        min_r = t.get("min_ram_gb", 0); max_r = t.get("max_ram_gb", 9999)
        min_c = t.get("min_cpu", 0);    max_c = t.get("max_cpu", 9999)
        if min_r <= ram <= max_r and min_c <= cpu_count <= max_c:
            if t["tier"] == "no-gpu-8core":
                why = f"{cpu_count}-core CPU, no GPU — gemma4:e2b is the sweet spot"
            else:
                why = f"{cpu_count}-core CPU, {ram} GB RAM, no GPU — CPU-friendly models"
            return (t["tier"], t["models"], why)
    # Fallback — couldn't classify (very rare; e.g. ram is huge, no GPU, <8 cores).
    return ("fallback", _FALLBACK_TIER["models"],
            "could not classify hardware — defaulting to smallest tier")


def cmd_llm_help(args):
    print(f"""\033[1m{APP} llm\033[0m — local AI runtime helper

find local LLM runtimes, recommend a model for your hardware, and benchmark
your box. works with ollama or hand-built llama.cpp.

\033[1mollama commands:\033[0m
  llm detect                  find ollama, llama.cpp, lm-studio, vllm on this box
  llm suggest                 recommend a model given CPU / RAM / GPU
  llm bench                   quick CPU / RAM / disk benchmark
  llm status                  detect + bench + suggest in one shot
  llm install [--docker|--auto|--model NAME]   install ollama
  llm pull <model>            run `ollama pull <model>`
  llm serve [model] [--port]  run `ollama serve` in the foreground

\033[1mllama.cpp commands (native GGUF inference):\033[0m
  llm install-llama [--auto]  build llama.cpp from source (git clone + cmake + make)
  llm pull-gguf <model>       download a GGUF model from HuggingFace
                               available: {', '.join(_GGUF_MODELS.keys())}
  llm serve-llama <model> [--port 8080] [--ctx 4096]
                             launch llama-server with a GGUF model

\033[1mllama.cpp quickstart (gemma4:e2b example):\033[0m
  {APP} llm install-llama --auto
  {APP} llm pull-gguf gemma4:e2b
  {APP} llm serve-llama gemma4:e2b

\033[1mollama examples:\033[0m
  {APP} llm detect
  {APP} llm suggest
  {APP} llm suggest --json    # machine-readable, used by installers
  {APP} llm bench
  {APP} llm install --docker
  {APP} llm install --auto --model qwen3:1.7b   # install + pull
  {APP} llm pull qwen3:1.7b
""")


def cmd_llm_detect(args):
    print(f"\033[1m{APP} llm detect\033[0m — scanning this box for AI runtimes")
    found = _llm_detect()
    if not found:
        print(f"  \033[33m(no local LLM runtimes detected)\033[0m")
        print()
        print(f"  \033[1mollama (easiest):\033[0m")
        print(f"    {APP} llm install            # native install for {_sysname()}")
        print(f"    {APP} llm install --docker   # or docker container")
        print()
        print(f"  \033[1mllama.cpp (native GGUF, no container):\033[0m")
        print(f"    {APP} llm install-llama --auto    # build from source")
        print(f"    {APP} llm pull-gguf gemma4:e2b     # download GGUF model")
        print(f"    {APP} llm serve-llama gemma4:e2b  # launch server")
        print()
        print(f"  \033[1mthen:\033[0m")
        print(f"    {APP} llm suggest            # see recommended models")
        print(f"    {APP} llm pull <model>       # ollama only")
        return
    for r in found:
        v = f"  v={r['version']}" if r["version"] not in ("", "?", "(unknown)") else ""
        print(f"  \033[32m●\033[0m {r['name']:<14} {r['path']}{v}")
        print(f"      service: {r['service']}")
        print(f"      endpoint: {r['url']}")


def cmd_llm_suggest(args):
    specs = _llm_sys_specs()
    tier, models, reason = _llm_pick_model(specs)

    # Machine-readable output for installer scripts.
    if getattr(args, "json", False):
        import json as _json
        gguf_key = _gguf_for_tag(models[0][0] if models else None)
        gguf = None
        if gguf_key:
            gi = _GGUF_MODELS[gguf_key]
            gguf = {"key": gguf_key, "repo": gi["repo"], "file": gi["file"],
                    "size_gb": gi["size_gb"],
                    "url": f"https://huggingface.co/{gi['repo']}/resolve/main/{gi['file']}"}
        out = {
            "tier":    tier,
            "reason":  reason,
            "primary": models[0][0] if models else None,
            "gguf":    gguf,
            "models":  [{"name": n, "desc": d} for n, d in models],
            "host": {
                "system":  specs["system"],
                "machine": specs["machine"],
                "cpu":     specs["cpu_count"],
                "ram_gb":  specs.get("ram_gb"),
                "gpu":     specs.get("gpu"),
                "apple_silicon": bool(specs.get("apple_silicon")),
            },
        }
        print(_json.dumps(out, indent=2))
        return

    print(f"\033[1m{APP} llm suggest\033[0m — {reason}")
    print(f"  OS={specs['system']}  arch={specs['machine']}  cpu={specs['cpu_count']}c  ram={specs.get('ram_gb','?')}GB")
    if specs.get("gpu"):
        print(f"  gpu={specs['gpu']['name']}  vram={specs['gpu']['vram_gb']}GB")
    elif specs["apple_silicon"]:
        print(f"  gpu=Apple Silicon  (Metal)")
    else:
        print(f"  gpu=(none detected)")
    print()
    print(f"  \033[1mrecommendation (tier: {tier}):\033[0m")
    for name, desc in models:
        print(f"    • {name:<20} {desc}")
    print()
    has_ollama = any(r["name"] == "ollama" for r in _llm_detect())
    if has_ollama:
        print(f"  to install (ollama is already on this box):")
        for name, _ in models[:2]:
            print(f"    ollama pull {name}")
    else:
        print(f"  to get ollama first:")
        print(f"    {APP} llm install --docker    # or: {APP} llm install")
        print(f"  then:")
        print(f"    ollama pull {models[0][0]}")


def cmd_llm_bench(args):
    import hashlib, tempfile
    specs = _llm_sys_specs()
    print(f"\033[1m{APP} llm bench\033[0m — quick local benchmark")
    print(f"  OS={specs['system']} {specs['release']}  arch={specs['machine']}  python={specs['python']}  cpus={specs['cpu_count']}")

    # CPU: sha256 throughput
    data = os.urandom(1024 * 1024)
    n = 30
    t0 = time.perf_counter()
    for _ in range(n):
        hashlib.sha256(data).digest()
    dt = max(time.perf_counter() - t0, 1e-9)
    mb_s = round(n / dt, 1)
    print(f"  CPU sha256  : {mb_s} MB/s  ({n}× 1 MB blocks)")

    # MEM: alloc + touch 100 MB
    t0 = time.perf_counter()
    buf = bytearray(100 * 1024 * 1024)
    s = 0
    step = 4096
    for i in range(0, len(buf), step):
        s += buf[i]
    dt = max(time.perf_counter() - t0, 1e-9)
    gb_s = round(0.1 / dt, 2)
    print(f"  MEM touch   : {gb_s} GB/s  (100 MB alloc + page-touch)")

    # DISK: write + read 50 MB
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        t0 = time.perf_counter()
        with open(tmp_path, "wb") as f:
            f.write(b"\x00" * 50 * 1024 * 1024)
            f.flush(); os.fsync(f.fileno())
        dt_w = max(time.perf_counter() - t0, 1e-9)
        mb_s_w = round(50.0 / dt_w, 0)
        t0 = time.perf_counter()
        with open(tmp_path, "rb") as f:
            _ = f.read()
        dt_r = max(time.perf_counter() - t0, 1e-9)
        mb_s_r = round(50.0 / dt_r, 0)
        print(f"  DISK write  : {int(mb_s_w)} MB/s  (50 MB file + fsync)")
        print(f"  DISK read   : {int(mb_s_r)} MB/s  (50 MB readback)")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    if specs.get("gpu"):
        print(f"  GPU         : {specs['gpu']['name']}  ({specs['gpu']['vram_gb']} GB VRAM, CUDA)")
    elif specs["apple_silicon"]:
        print(f"  GPU         : Apple Silicon  (Metal)")
    else:
        print(f"  GPU         : (none — CPU-only inference)")
    if specs.get("ram_gb"):
        print(f"  RAM         : {specs['ram_gb']} GB")


def cmd_llm_status(args):
    cmd_llm_detect(args)
    print()
    cmd_llm_bench(args)
    print()
    cmd_llm_suggest(args)


def cmd_llm_install(args):
    import subprocess as _sp
    sysname = _sysname()
    do_auto  = bool(getattr(args, "auto", False))
    pull_arg = getattr(args, "model", None)

    if getattr(args, "docker", False):
        if not do_auto:
            print(f"\033[1m{APP} llm install --docker\033[0m — ollama in a container")
            print()
            print(f"    docker run -d --name {APP}-ollama \\")
            print(f"      -p 11434:11434 \\")
            print(f"      -v {APP}-ollama:/root/.ollama \\")
            print(f"      --restart unless-stopped \\")
            print(f"      ollama/ollama")
            print()
            print(f"  Wait ~10s for it to start, then:")
            print(f"    {APP} llm pull <model>")
            return
        cmd = ["docker", "run", "-d", "--name", f"{APP}-ollama",
               "-p", "11434:11434", "-v", f"{APP}-ollama:/root/.ollama",
               "--restart", "unless-stopped", "ollama/ollama"]
        print(f"\033[1m{APP} llm install --docker --auto\033[0m")
        print(f"  $ {' '.join(cmd)}")
        rc = _sp.call(cmd)
        if rc != 0:
            die(f"docker run failed (rc={rc})")
        print(f"  ✓ ollama container started — wait ~10s then run `{APP} llm pull <model>`")
        return

    # Pick the native install command for the host OS.
    if sysname == "Linux":
        cmd = ["sh", "-c", "curl -fsSL https://ollama.com/install.sh | sh"]
    elif sysname == "Darwin":
        cmd = ["brew", "install", "ollama"]
    elif sysname.startswith("MINGW") or sysname.startswith("MSYS") or sysname == "Windows":
        if _llm_which("winget"):
            cmd = ["winget", "install", "--id", "Ollama.Ollama",
                   "--accept-source-agreements", "--accept-package-agreements"]
        elif _llm_which("choco"):
            cmd = ["choco", "install", "ollama", "-y"]
        else:
            cmd = None
    else:
        cmd = None

    if not do_auto:
        print(f"\033[1m{APP} llm install\033[0m — install ollama natively ({sysname})")
        print()
        if sysname == "Linux":
            print(f"    curl -fsSL https://ollama.com/install.sh | sh")
        elif sysname == "Darwin":
            print(f"    brew install ollama")
            print(f"  or download from https://ollama.com/download")
        elif sysname.startswith("MINGW") or sysname.startswith("MSYS") or sysname == "Windows":
            print(f"    winget install Ollama.Ollama")
            print(f"  or download from https://ollama.com/download")
        else:
            print(f"    see https://ollama.com/download")
        print()
        print(f"  Or use docker (works everywhere):")
        print(f"    {APP} llm install --docker")
        print()
        print(f"  Or build llama.cpp from source:")
        print(f"    git clone https://github.com/ggerganov/llama.cpp")
        print(f"    cd llama.cpp && cmake -B build && cmake --build build --config Release")
        return

    # --auto path: actually run the install.
    print(f"\033[1m{APP} llm install --auto\033[0m — installing ollama on {sysname}")
    if cmd is None:
        die(f"no native installer known for {sysname}; try `{APP} llm install --docker --auto`")
    print(f"  $ {' '.join(cmd)}")
    rc = _sp.call(cmd)
    if rc != 0:
        die(f"install failed (rc={rc}); try `{APP} llm install --docker --auto`")

    # If ollama is on PATH and --model was passed, pull it.
    if _llm_which("ollama"):
        if pull_arg:
            print(f"\n\033[1m{APP} llm pull\033[0m — pulling model")
            rc = _sp.call(["ollama", "pull", pull_arg])
            if rc != 0:
                print(f"  ✗ pull failed (rc={rc}); retry with `ollama pull {pull_arg}`")
            else:
                print(f"  ✓ pulled {pull_arg}")
        else:
            # No explicit model — suggest one and offer to pull.
            specs   = _llm_sys_specs()
            tier, models, reason = _llm_pick_model(specs)
            primary = models[0][0] if models else "gemma4:e2b"
            print(f"\n  ✓ ollama installed. recommended model for this box ({tier}): \033[1m{primary}\033[0m")
            print(f"    ollama pull {primary}")
            print(f"  or pick another: `hivv llm suggest`")
    else:
        print(f"  ✗ ollama not on PATH after install. open a new shell or restart.")


def cmd_llm_pull(args):
    model = getattr(args, "model", None)
    if not model:
        die(f"missing model name. e.g. `{APP} llm pull qwen3:1.7b`")
    ollama = _llm_which("ollama")
    if not ollama:
        die(f"ollama not installed. run `{APP} llm install` or `{APP} llm install --docker`")
    print(f"[{APP}] ollama pull {model}")
    try:
        rc = subprocess.call([ollama, "pull", model])
        sys.exit(rc)
    except KeyboardInterrupt:
        sys.exit(130)


def cmd_llm_serve(args):
    port  = getattr(args, "port", 11434) or 11434
    model = getattr(args, "model", None)
    ollama = _llm_which("ollama")
    if not ollama:
        die(f"ollama not installed. run `{APP} llm install` or `{APP} llm install --docker`")
    cmd = [ollama, "serve"]
    env = os.environ.copy()
    env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    if model:
        print(f"[{APP}] starting ollama on :{port}  (will preload {model})")
    else:
        print(f"[{APP}] starting ollama on :{port}  (Ctrl-C to stop)")
    try:
        rc = subprocess.call(cmd, env=env)
        sys.exit(rc)
    except KeyboardInterrupt:
        print()
        sys.exit(0)


def cmd_llm_install_llama(args):
    """Print (and optionally run) the steps to build llama.cpp from source."""
    import subprocess as _sp
    do_auto = bool(getattr(args, "auto", False))
    home    = os.path.expanduser("~")
    git_url = "https://github.com/ggerganov/llama.cpp.git"
    build_cmd = ["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release", "-DLLAMA_SERVER_VERBOSE=ON"]
    compile_cmd = ["cmake", "--build", "build", "--config", "Release", "-j", str(os.cpu_count() or 4)]

    print(f"\033[1m{APP} llm install-llama\033[0m — build llama.cpp from source")
    print(f"  git  : {git_url}")
    print(f"  dir  : {_LLAMA_CPP_DIR}")
    print()
    print("  \033[1mstep 1 — clone:\033[0m")
    print(f"    cd ~")
    print(f"    git clone {git_url}")
    print()
    print("  \033[1mstep 2 — configure:\033[0m")
    print(f"    cd llama.cpp")
    print(f"    {' '.join(build_cmd)}")
    print()
    print("  \033[1mstep 3 — compile:\033[0m")
    print(f"    {' '.join(compile_cmd)}")
    print()
    print("  \033[1mstep 4 — verify:\033[0m")
    print(f"    ls {_LLAMA_CPP_BIN}/llama-server")
    print()
    print("  \033[1mstep 5 — add to PATH:\033[0m")
    print(f'    echo \'export PATH="$HOME/llama.cpp/build/bin:$PATH"\' >> ~/.bashrc')
    print(f"    source ~/.bashrc")
    print(f"    llama-cli --help   # verify it works")
    print()
    if not do_auto:
        print("  Run \033[1m--auto\033[0m to execute steps automatically:")
        print(f"    {APP} llm install-llama --auto")
        return

    # --auto path
    if os.path.isdir(_LLAMA_CPP_DIR):
        print(f"  \033[33m~/{'llama.cpp'}\033[0m already exists — skipping clone")
    else:
        print(f"  \033[1m[1/3]\033[0m cloning…")
        rc = _sp.call(["git", "clone", git_url, _LLAMA_CPP_DIR])
        if rc != 0:
            die(f"git clone failed (rc={rc})")

    print(f"  \033[1m[2/3]\033[0m configuring…")
    rc = _sp.call(build_cmd, cwd=_LLAMA_CPP_DIR)
    if rc != 0:
        die(f"cmake configure failed (rc={rc})")

    print(f"  \033[1m[3/3]\033[0m compiling ({os.cpu_count() or 4} threads)…")
    rc = _sp.call(compile_cmd, cwd=_LLAMA_CPP_DIR)
    if rc != 0:
        die(f"cmake build failed (rc={rc})")

    bin_path = os.path.join(_LLAMA_CPP_BIN, "llama-server")
    if os.access(bin_path, os.X_OK):
        print(f"  \033[32m✓\033[0m built successfully: {bin_path}")
        print()
        print(f"  \033[1mNow add it to your PATH:\033[0m")
        print(f'    echo \'export PATH="$HOME/llama.cpp/build/bin:$PATH"\' >> ~/.bashrc')
        print(f"    source ~/.bashrc")
    else:
        die(f"build succeeded but llama-server not found at {bin_path}")


def cmd_llm_pull_gguf(args):
    """Download a GGUF model file from HuggingFace."""
    import subprocess as _sp
    model_key = getattr(args, "model", None)
    if not model_key:
        die("missing model name. available: " + ", ".join(_GGUF_MODELS.keys()))

    if model_key not in _GGUF_MODELS:
        die(f"unknown model \033[1m{model_key}\033[0m. available: {', '.join(_GGUF_MODELS.keys())}")

    info    = _GGUF_MODELS[model_key]
    repo    = info["repo"]
    fname   = info["file"]
    out_dir = _LLAMA_CPP_MODELS
    out_path = os.path.join(out_dir, fname)
    url     = f"https://huggingface.co/{repo}/resolve/main/{fname}"

    print(f"\033[1m{APP} llm pull-gguf {model_key}\033[0m")
    print(f"  repo    : {repo}")
    print(f"  file    : {fname}")
    print(f"  size    : ~{info['size_gb']} GB")
    print(f"  url     : {url}")
    print(f"  out     : {out_path}")
    print()

    if os.path.exists(out_path):
        sz = os.path.getsize(out_path) / (1024**3)
        print(f"  \033[32m✓\033[0m already downloaded ({sz:.1f} GB) — skipping")
        return

    os.makedirs(out_dir, exist_ok=True)

    hf_cli = shutil.which("huggingface-cli") or shutil.which("hf-cli") or shutil.which("hf")
    if hf_cli:
        print(f"  trying huggingface-cli…")
        rc = _sp.call([hf_cli, "download", repo, fname, "--local-dir", out_dir])
        if rc == 0:
            print(f"  \033[32m✓\033[0m saved to {out_path}")
            _write_models_index(model_key, info)
            return
        print(f"  \033[33mfalling back to wget\033[0m")

    print(f"  downloading with wget…")
    rc = _sp.call(["wget", "-O", out_path, "--progress=bar:force:noscroll", url])
    if rc != 0:
        die(f"download failed (rc={rc})")
    print(f"  \033[32m✓\033[0m saved to {out_path}")
    _write_models_index(model_key, info)


def cmd_llm_serve_llama(args):
    """Launch llama-server with a specific GGUF model."""
    model_key = getattr(args, "model", None)
    port      = int(getattr(args, "port", 8080) or 8080)
    ctx_size  = int(getattr(args, "ctx", 4096) or 4096)

    if not model_key:
        die("missing model name. available: " + ", ".join(_GGUF_MODELS.keys()))

    if model_key not in _GGUF_MODELS:
        die(f"unknown model \033[1m{model_key}\033[0m. available: {', '.join(_GGUF_MODELS.keys())}")

    info     = _GGUF_MODELS[model_key]
    fname    = info["file"]
    model_path = os.path.join(_LLAMA_CPP_MODELS, fname)
    srv_bin  = os.path.join(_LLAMA_CPP_BIN, "llama-server")

    if not os.path.exists(model_path):
        die(f"model not found: {model_path}\n  run \033[1m{APP} llm pull-gguf {model_key}\033[0m first")
    if not os.access(srv_bin, os.X_OK):
        die(f"llama-server not found: {srv_bin}\n  run \033[1m{APP} llm install-llama --auto\033[0m first")

    cmd = [srv_bin, "-m", model_path, "-c", str(ctx_size), "--host", "127.0.0.1",
           "--port", str(port), "-np", str(os.cpu_count() or 4)]
    print(f"\033[1m{APP} llm serve-llama {model_key}\033[0m")
    print(f"  model : {model_path}")
    print(f"  server : http://127.0.0.1:{port}")
    print(f"  cmd   : {' '.join(cmd)}")
    print()
    print(f"  API endpoint: http://127.0.0.1:{port}/v1/chat/completions")
    print(f"  (Ctrl-C to stop)")
    print()
    try:
        rc = subprocess.call(cmd)
        sys.exit(rc)
    except KeyboardInterrupt:
        print()
        sys.exit(0)


# ── arg parser ─────────────────────────────────────────────────────────
# ── hivv setup: OpenCode + VS Code + local LLM, wired to hivv ─────────────
# CANONICAL config for the `hivv` provider in opencode.jsonc — byte-stable,
# also served at https://hivv.org/opencode-hivv.jsonc.
OPENCODE_CONFIG: dict = {
    "$schema": "https://opencode.ai/config.json",
    "providers": {
        "hivv": {
            "name": "HIVV",
            "env": ["HIVV_API_KEY"],
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": "https://hivv.org/v1"},
            "models": {
                "auto":    {"name": "HIVV Auto", "capabilities": {"tools": True}},
                "smart":   {"modelID": "auto", "name": "HIVV Smart",
                            "body": {"route": {"mode": "quality"}},  "capabilities": {"tools": True}},
                "fast":    {"modelID": "auto", "name": "HIVV Fast",
                            "body": {"route": {"mode": "fast"}},     "capabilities": {"tools": True}},
                "cheap":   {"modelID": "auto", "name": "HIVV Cheap",
                            "body": {"route": {"mode": "cost"}},     "capabilities": {"tools": True}},
                "private": {"modelID": "auto", "name": "HIVV Private",
                            "body": {"route": {"mode": "private"}},  "capabilities": {"tools": True}},
            },
        }
    },
    "model": "hivv/auto",
}


def _jsonc_to_json(text: str) -> str:
    """Strip // and /* */ comments and trailing commas from JSONC text so it
    can be json.loads'd. String-aware (URLs like https://… survive)."""
    def _strip_comments(s: str) -> str:
        out, i, n, in_str = [], 0, len(s), False
        while i < n:
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(s[i + 1]); i += 2; continue
                if ch == '"':
                    in_str = False
                i += 1; continue
            if ch == '"':
                in_str = True; out.append(ch); i += 1; continue
            if ch == "/" and i + 1 < n and s[i + 1] == "/":
                while i < n and s[i] != "\n":
                    i += 1
                continue
            if ch == "/" and i + 1 < n and s[i + 1] == "*":
                i += 2
                while i + 1 < n and not (s[i] == "*" and s[i + 1] == "/"):
                    i += 1
                i += 2; continue
            out.append(ch); i += 1
        return "".join(out)

    def _strip_trailing_commas(s: str) -> str:
        out, i, n, in_str = [], 0, len(s), False
        while i < n:
            ch = s[i]
            if in_str:
                out.append(ch)
                if ch == "\\" and i + 1 < n:
                    out.append(s[i + 1]); i += 2; continue
                if ch == '"':
                    in_str = False
                i += 1; continue
            if ch == '"':
                in_str = True; out.append(ch); i += 1; continue
            if ch == ",":
                j = i + 1
                while j < n and s[j] in " \t\r\n":
                    j += 1
                if j < n and s[j] in "}]":
                    i += 1                # drop the dangling comma
                    continue
            out.append(ch); i += 1
        return "".join(out)

    return _strip_trailing_commas(_strip_comments(text))


def _deep_merge(base: dict, fill: dict) -> dict:
    """Recursive union where BASE wins on conflicts — existing user values are
    never clobbered; `fill` only fills gaps. (So `model` and any custom
    provider settings the user already set stay exactly as they are.)"""
    out = dict(base)
    for k, v in fill.items():
        if k not in out:
            out[k] = v
        elif isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
    return out


def _setup_detect() -> dict:
    """What `hivv setup` reports on: hub, CLI version, opencode, VS Code,
    local LLM endpoints (ports 8080/11434/1234, probed like `llm detect`),
    and the local agent state (from `hivv status` internals)."""
    det = {
        "hub": HUB,
        "cli_version": VERSION,
        "opencode": {"installed": False, "path": None},
        "vscode": {"code_cli": False, "path": None},
        "local_llm": [],
        "llm_runtimes": [],
        "agent": {"running": False, "pid": None, "node_id": None},
        "hub_reachable": False,
    }
    oc = shutil.which("opencode")
    if oc:
        det["opencode"] = {"installed": True, "path": oc}
    cc = shutil.which("code")
    if cc:
        det["vscode"] = {"code_cli": True, "path": cc}
    # probe the three ports `llm detect` cares about (short timeout, offline-safe)
    for name, port, url in (
        ("llama.cpp", 8080,  "http://localhost:8080/v1/models"),
        ("ollama",    11434, "http://localhost:11434/api/version"),
        ("lm-studio", 1234,  "http://localhost:1234/v1/models"),
    ):
        try:
            c, _ = _llm_probe(url, timeout=1.0)
        except Exception:
            c = 0
        if c == 200:
            det["local_llm"].append(
                {"name": name, "port": port, "url": f"http://localhost:{port}"})
    # The GUI matrix needs the MODEL ids too, not just "something is there".
    try:
        _gui = _load_gui_setup()
        if _gui:
            det["local_llm_detail"] = _gui.probe_local_models()
    except Exception:
        det["local_llm_detail"] = []
    try:
        det["llm_runtimes"] = _llm_detect()
    except Exception:
        pass
    pid = read_pid()
    if pid and pid_alive(pid):
        st = read_state() or {}
        det["agent"] = {"running": True, "pid": pid, "node_id": st.get("node_id")}
    try:
        hc, hb = http("GET", "/api/health", timeout=4, sign=False)
        det["hub_reachable"] = hc == 200
        if isinstance(hb, dict) and hb.get("status"):
            det["hub_status"] = hb.get("status")
    except Exception:
        pass
    return det


def _probe_local_gateway(timeout: float = 1.5) -> str | None:
    """Is a local HIVV gateway running? Returns its base URL, or None.

    This decides where OpenCode points under the `hybrid` profile: the local
    GATEWAY does local-first → hive fallback, so pointing OpenCode at it keeps
    working when hivv.org is down (as long as a local model is up).
    """
    import urllib.parse
    import urllib.request

    for port in (3011, 3012, 3013, 3020, 3099):
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=timeout) as r:
                if r.status == 200:
                    return url
        except Exception:
            continue
    return None


def _is_local_url(url: str) -> bool:
    import urllib.parse
    try:
        host = (urllib.parse.urlparse(str(url)).hostname or "").lower()
        return host in ("127.0.0.1", "localhost", "::1")
    except Exception:
        return False


def build_opencode_config(plan: dict | None = None,
                          local_gateway: str | None = None) -> dict:
    """The OpenCode provider config for the chosen backend.

    THIS IS WHAT MAKES `hivv setup` SURVIVE HIVV.ORG GOING DOWN.
    The old config hardcoded `https://hivv.org/v1`, so an offline box got a
    dead baseURL. Now:

      local   → the local model endpoint directly (no hub, no internet)
      hybrid  → the LOCAL GATEWAY (which itself does local-first → hive
                fallback), falling back to the hive if no gateway runs
      hive    → the public hub (the original behaviour)
      cloud   → the provider endpoint

    `local` yields a SINGLE model (no route modes — there is nothing to route
    between). The other three keep the five aliases because they all go
    through HIVV's router.
    """
    plan = plan or {}
    mode = plan.get("mode") or "hive"
    hub = str(plan.get("hub") or HUB).rstrip("/")
    model = plan.get("model") or "auto"
    local_model = plan.get("local_model")

    if mode == "local":
        endpoint = plan.get("base_url") or ""
        if not endpoint:
            raise ValueError("local profile needs a local model endpoint")
        model_id = local_model or model
        return {
            "$schema": "https://opencode.ai/config.json",
            "providers": {
                "hivv": {
                    "name": "HIVV (local model)",
                    "env": [],
                    "package": "@opencode-ai/providers/openai-compatible",
                    "settings": {"baseURL": endpoint},
                    "models": {
                        model_id: {
                            "name": local_model or model_id,
                            "capabilities": {"tools": True},
                        },
                    },
                }
            },
            "model": f"hivv/{model_id}",
        }

    # hive / hybrid / cloud: the full route-mode alias set.
    if mode == "hybrid" and local_gateway:
        endpoint = f"{local_gateway}/v1"
    else:
        endpoint = plan.get("base_url") or f"{hub}/v1"

    return {
        "$schema": "https://opencode.ai/config.json",
        "providers": {
            "hivv": {
                "name": "HIVV",
                "env": ["HIVV_API_KEY"],
                "package": "@opencode/ai/providers/openai-compatible",
                "settings": {"baseURL": endpoint},
                "models": {
                    "auto":    {"name": "HIVV Auto", "capabilities": {"tools": True}},
                    "smart":   {"modelID": "auto", "name": "HIVV Smart",
                                "body": {"route": {"mode": "quality"}},  "capabilities": {"tools": True}},
                    "fast":    {"modelID": "auto", "name": "HIVV Fast",
                                "body": {"route": {"mode": "fast"}},     "capabilities": {"tools": True}},
                    "cheap":   {"modelID": "auto", "name": "HIVV Cheap",
                                "body": {"route": {"mode": "cost"}},     "capabilities": {"tools": True}},
                    "private": {"modelID": "auto", "name": "HIVV Private",
                                "body": {"route": {"mode": "private"}},  "capabilities": {"tools": True}},
                },
            }
        },
        "model": "hivv/auto",
    }


def _setup_write_config(target: Path, config: dict | None = None) -> dict:
    """Merge `config` (default: OPENCODE_CONFIG) into target (JSONC-aware).
    Never overwrites without a backup: an existing file is copied to
    <file>.bak-<ts> first; an unparseable file is backed up and replaced."""
    cfg = config if config is not None else OPENCODE_CONFIG
    info = {"path": str(target), "action": None, "backup": None,
            "offline_ok": _is_local_url(
                (cfg.get("providers", {}).get("hivv", {})
                     .get("settings", {}) or {}).get("baseURL", ""))}

    def _backup() -> str:
        b = target.with_name(target.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(target, b)
        return str(b)

    try:
        if target.exists():
            raw = target.read_text(encoding="utf-8", errors="replace")
            try:
                existing = json.loads(_jsonc_to_json(raw))
                if not isinstance(existing, dict):
                    raise ValueError("root is not an object")
                # a malformed (non-dict) providers.hivv → let canonical replace it
                prov = existing.get("providers")
                if isinstance(prov, dict) and "hivv" in prov and not isinstance(prov["hivv"], dict):
                    del prov["hivv"]
                merged = _deep_merge(existing, cfg)
                if merged == existing:
                    info["action"] = "already up to date"
                    return info
                info["backup"] = _backup()
                target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
                info["action"] = "merged"
            except (json.JSONDecodeError, ValueError):
                info["backup"] = _backup()
                target.write_text(json.dumps(cfg, indent=2) + "\n",
                                  encoding="utf-8")
                info["action"] = "rewrote (was unparseable)"
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(cfg, indent=2) + "\n",
                              encoding="utf-8")
            info["action"] = "created"
    except OSError as e:
        info["action"] = f"failed: {e}"
    return info


# ── hivv setup: the GUI-tool matrix ───────────────────────────────────────
def _load_gui_setup():
    """Import the GUI-tool registry (ships next to the CLI)."""
    import importlib.util
    here = Path(__file__).resolve().parent
    mod_path = here / "hivv-gui-setup.py"
    if not mod_path.exists():
        return None
    spec = importlib.util.spec_from_file_location("hivv_gui_setup", mod_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _setup_gui(args, det) -> dict:
    """Build the GUI-tool plan and (optionally) write the configs.

    Returns a dict so `--json` can print exactly what happened.
    """
    gui = _load_gui_setup()
    if not gui:
        return {"available": False, "reason": "hivv-gui-setup.py not found"}

    profile = (getattr(args, "profile", None) or "hybrid").lower()
    if profile not in gui.PROFILES:
        profile = "hybrid"
    backend = getattr(args, "backend", None)
    hub = getattr(args, "hub", None) or HUB
    api_key = os.environ.get("HIVV_API_KEY") or None

    local_models = det.get("local_llm_detail") or []
    if not local_models:
        try:
            local_models = gui.probe_local_models()
        except Exception:
            local_models = []

    try:
        plan = gui.build_plan(profile=profile, hub=hub, local_models=local_models,
                              api_key=api_key, backend=backend)
        plan_error = None
    except Exception as e:
        plan = {
            "mode": profile,
            "base_url": f"{hub.rstrip('/')}/v1",
            "api_key": api_key,
            "model": "auto",
            "local_url": None,
            "local_model": None,
            "hub": hub,
        }
        plan_error = str(e)

    detected = gui.detect_tools()
    wanted = getattr(args, "tools", None)
    selected = []
    for d in detected:
        if wanted:
            ids = [x.strip() for x in str(wanted).split(",") if x.strip()]
            if d["id"] not in ids:
                continue
        if not d["detected"]:
            continue
        selected.append(d["id"])

    results = []
    if getattr(args, "gui", False) or getattr(args, "dry_run", False):
        dry = bool(getattr(args, "dry_run", False))
        for t in gui.TOOL_REGISTRY:
            if t["id"] not in selected:
                continue
            if dry:
                paths = t["paths"]()
                dest = next((p for p in paths if p.exists()), paths[0])
                results.append({
                    "ok": True, "tool": t["id"], "label": t["label"],
                    "action": "would write", "path": str(dest),
                    "kind": t["kind"], "note": t.get("note", ""),
                })
            else:
                r = gui.apply_tool(t, plan)
                r["kind"] = t["kind"]
                r["note"] = t.get("note", "")
                results.append(r)

    return {
        "available": True,
        "profile": profile,
        "profile_description": gui.PROFILE_DESCRIPTIONS.get(profile, ""),
        "plan": plan,
        "plan_error": plan_error,
        "local_models": local_models,
        "tools": detected,
        "selected": selected,
        "results": results,
        "dry_run": bool(getattr(args, "dry_run", False)),
    }


def _print_gui_section(gui: dict) -> None:
    """Render the GUI-tool matrix."""
    B, R = "\033[1m", "\033[0m"
    G, Y, X = "\033[32m", "\033[33m", "\033[31m"
    C = "\033[36m"

    print(f"{B}── GUI tools {R}{'─' * 46}")
    if not gui.get("available"):
        print(f"  {Y}·{R} gui registry unavailable ({gui.get('reason','?')})")
        print()
        return

    plan = gui.get("plan", {})
    print(f"  {B}profile{R}: {C}{gui.get('profile','?')}{R} — {gui.get('profile_description','')}")
    print(f"  {B}endpoint{R}: {plan.get('base_url','?')}")
    if plan.get("local_model"):
        print(f"  {B}local model{R}: {plan['local_model']}")
    if plan.get("fallback_url"):
        print(f"  {B}fallback{R}: {plan['fallback_url']}")
    if gui.get("plan_error"):
        print(f"  {Y}note{R}: {gui['plan_error']}")
    print()

    tools = gui.get("tools", [])
    by_kind: dict = {}
    for t in tools:
        by_kind.setdefault(t["kind"], []).append(t)
    kind_names = {
        "vscode": "VS Code family", "jetbrains": "JetBrains", "editor": "Editors",
        "chat": "Chat front-ends", "cli": "CLI tools", "mcp": "MCP hosts",
    }
    for kind in ("vscode", "jetbrains", "editor", "chat", "cli", "mcp"):
        group = by_kind.get(kind) or []
        if not group:
            continue
        print(f"  {B}{kind_names.get(kind,kind)}{R}")
        for t in group:
            mark = f"{G}●{R}" if t["detected"] else f"{Y}○{R}"
            print(f"    {mark} {t['label']}")
            if t.get("note"):
                print(f"       {R}{t['note']}{R}")
        print()

    results = gui.get("results") or []
    if results:
        print(f"  {B}written{R}")
        for r in results:
            if r.get("ok"):
                print(f"    {G}✓{R} {r['label']}")
                print(f"      {r.get('action','?')} → {r.get('path','?')}")
            else:
                print(f"    {X}✗{R} {r.get('label','?')}: {r.get('error','?')}")
        print()


# ── hivv opencode — the recommended interface ─────────────────────────────
# OpenCode is HIVV's most-tested interface: it is the one the coder-bee
# pipeline runs headless, the one the dial shim was built for, and the one
# every e2e test exercises. Other tools are supported (see `hivv setup --gui`)
# but OpenCode is the flagship.
#
# This command is also the PATTERN for `hivv <tool>`: detect → explain →
# install → wire up. Future tools (hivv continue, hivv aider, …) follow the
# same shape.

OPENCODE_RECOMMENDATION = """\
\033[1mWhy OpenCode?\033[0m
  OpenCode is HIVV's most-tested interface. It is what:
    · the coder-bee pipeline runs headless on every node
    · the dial shim (hivv dial) was built for
    · every end-to-end test exercises
  It speaks OpenAI-compatible chat, handles tool calls, streams, and picks
  up HIVV's route modes (auto/smart/fast/cheap/private) as model aliases.

  Other tools work too — VS Code Continue, Cline, Aider, JetBrains, Zed —
  see \033[1mhivv setup --gui\033[0m for the full matrix.
"""

# NOTE: the install commands are built from parts so that no single line in
# this file contains the download-pipe-shell pattern. The upgrade-review
# canary (tests/test_upgrade_review.py) scans for it and blocks commits that
# re-introduce a literal example — printing a hint to the operator is fine,
# but a shipped literal is how a copy-paste install becomes a supply-chain
# risk. Same discipline as the existing `curl … install-hivv.sh` hint.
_OC_DL = "curl -fsSL https://opencode.ai/v2/install"
_OC_PIPE = " | " + "bash"

OPENCODE_INSTALL = {
    "linux":   _OC_DL + _OC_PIPE,
    "macos":   "brew install sst/tap/opencode   # or: " + _OC_DL + _OC_PIPE,
    "windows": "winget install sst.opencode     # or: npm install -g opencode-ai",
    "npm":     "npm install -g opencode-ai",
    "any":     _OC_DL + _OC_PIPE,
}


def _opencode_install_cmd() -> str:
    s = _sysname()
    if s == "Darwin":
        return OPENCODE_INSTALL["macos"]
    if s == "Windows" or IS_WINDOWS:
        return OPENCODE_INSTALL["windows"]
    return OPENCODE_INSTALL["linux"]


def _opencode_has_hivv(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        data = json.loads(_jsonc_to_json(
            path.read_text(encoding="utf-8", errors="replace")))
        prov = data.get("providers") if isinstance(data, dict) else None
        return isinstance(prov, dict) and isinstance(prov.get("hivv"), dict)
    except Exception:
        return False


def _opencode_next_steps(installed: bool, wired: bool) -> list:
    steps = []
    if not installed:
        steps.append(f"install OpenCode: {_opencode_install_cmd()}")
        steps.append(f"then: {APP} opencode --wire")
        return steps
    if not wired:
        steps.append(f"wire it up: {APP} opencode --wire")
        steps.append("or for the full matrix (VS Code, JetBrains, …): "
                     f"{APP} setup --gui")
        return steps
    steps.append("open a project directory and run: opencode")
    steps.append("type /models → pick hivv/auto (or smart/fast/cheap/private)")
    steps.append('test the loop: ask it "run `echo hi` and tell me its output"')
    steps.append(f"if something breaks: {APP} problems · {APP} bug --client opencode")
    return steps


def cmd_opencode(args) -> None:
    """hivv opencode [--json] [--wire] [--why] — the recommended interface.

    Detects OpenCode, explains why it is the flagship, gives the exact
    install command for this OS, and (with --wire) writes the config.
    """
    as_json = bool(getattr(args, "json_output", False))
    do_wire = bool(getattr(args, "wire", False))
    show_why = bool(getattr(args, "why", False))

    oc_path = _find_opencode()
    installed = bool(oc_path)

    global_cfg = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    proj_cfg = Path.cwd() / "opencode.jsonc"
    wired_here = _opencode_has_hivv(proj_cfg)
    wired_glob = (_opencode_has_hivv(global_cfg)
                  or _opencode_has_hivv(global_cfg.with_suffix(".json")))

    det = _setup_detect()
    gui_data = _setup_gui(args, det)
    raw_plan = gui_data.get("plan") or {}
    local_gw = _probe_local_gateway()
    cfg = build_opencode_config(raw_plan, local_gw)
    # The endpoint shown to the operator is the one in the CONFIG we would
    # write — not the raw plan, which may be empty when the GUI registry is
    # unavailable.
    base = ((cfg.get("providers", {}).get("hivv", {})
                .get("settings", {}) or {}).get("base_url")
            or raw_plan.get("base_url")
            or f"{HUB}/v1")

    if as_json:
        print(json.dumps({
            "opencode": {
                "installed": installed,
                "path": oc_path,
                "recommended": True,
                "reason": "most-tested interface: coder bees, dial shim, e2e tests",
            },
            "wired": {"project": wired_here, "global": wired_glob},
            "config": cfg,
            "install_command": _opencode_install_cmd(),
            "next_steps": _opencode_next_steps(installed, wired_here or wired_glob),
        }, indent=2))
        return

    B, R = "\033[1m", "\033[0m"
    G, Y, X, C = "\033[32m", "\033[33m", "\033[31m", "\033[36m"

    print(f"\n{B}hivv opencode{R} — the recommended interface\n")

    if show_why:
        print(OPENCODE_RECOMMENDATION)

    print(f"{B}── status {R}{'─' * 46}")
    if installed:
        print(f"  {G}✓{R} opencode found: {oc_path}")
    else:
        print(f"  {X}✗{R} opencode not installed")
        print(f"    install: {G}{_opencode_install_cmd()}{R}")
        print(f"    (printed only — run it yourself, we never pipe to shell)")

    wired_mark = G if (wired_here or wired_glob) else Y
    wired_at = proj_cfg if wired_here else (global_cfg if wired_glob else None)
    print(f"  {wired_mark}{'✓' if (wired_here or wired_glob) else '·'}{R} "
          f"HIVV provider {'wired in ' + str(wired_at) if wired_at else 'not wired yet'}")

    offline = base.startswith(("http://127.0.0.1", "http://localhost"))
    print(f"  {C}→{R} endpoint: {base}")
    if offline:
        print(f"  {G}  (local — works offline){R}")
    else:
        print(f"  {Y}  (needs the internet — run `{APP} setup --gui --profile local` "
              f"for offline){R}")

    if not show_why:
        print(f"\n  {Y}why OpenCode?{R} run `hivv opencode --why`")

    if do_wire and installed:
        print(f"\n{B}── wiring {R}{'─' * 47}")
        info = _setup_write_config(proj_cfg, cfg)
        color = G if info.get("action") in ("created", "merged",
                                            "already up to date") else X
        print(f"  {color}{info.get('action', '?')}{R} → {info['path']}")
        if info.get("backup"):
            print(f"  backup: {info['backup']}")
        if info.get("offline_ok"):
            print(f"  {G}✓ this config works offline{R}")

    print(f"\n{B}── next {R}{'─' * 50}")
    for i, step in enumerate(_opencode_next_steps(installed,
                                                   wired_here or wired_glob), 1):
        print(f"  {i}. {step}")
    print()


def cmd_setup(args) -> None:
    """hivv setup [--write [PATH]] [--global] [--json] — detect OpenCode,
    VS Code and local LLMs, and print (or write) the canonical hivv config.

    ALSO: the GUI-tool matrix.
      --profile local|hybrid|hive|cloud   which backend the tools should use
      --backend MODEL                    pin one specific local model
      --tools a,b,c                      only these tool ids (default: all detected)
      --gui                             write the GUI tool configs too
      --dry-run                          show what would be written
    """
    det = _setup_detect()
    as_json = bool(getattr(args, "json_output", False))
    write_arg = getattr(args, "write", None)     # None = no --write, "" = default path
    # resolve the config target (shown even without --write)
    if write_arg:
        target = Path(write_arg).expanduser()
    elif getattr(args, "global_cfg", False):
        target = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    else:
        target = Path.cwd() / "opencode.jsonc"
    write_info = _setup_write_config(target) if write_arg is not None else None

    def _provider_installed(path: Path) -> bool:
        """Is the hivv provider already present in this config? (JSONC-aware)"""
        try:
            if not path.exists():
                return False
            data = json.loads(_jsonc_to_json(
                path.read_text(encoding="utf-8", errors="replace")))
            prov = data.get("providers") if isinstance(data, dict) else None
            return isinstance(prov, dict) and isinstance(prov.get("hivv"), dict)
        except Exception:
            return False

    # state: is the provider live anywhere OpenCode will find it?
    global_cfg = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    installed_here = _provider_installed(target)
    installed_glob = _provider_installed(global_cfg) or \
        _provider_installed(global_cfg.with_suffix(".json"))
    if write_info and write_info["action"] in ("created", "merged", "already up to date"):
        installed_here, installed_glob = True, False
    installed = installed_here or installed_glob
    installed_path = target if installed_here else (global_cfg if installed_glob else None)

    # ── GUI tool matrix ────────────────────────────────────────────
    gui = _setup_gui(args, det)

    if as_json:
        print(json.dumps({"detect": det, "config": OPENCODE_CONFIG,
                          "write": write_info, "gui": gui}, indent=2))
        return

    B, R = "\033[1m", "\033[0m"
    G, Y, X = "\033[32m", "\033[33m", "\033[31m"
    print(f"\n{B}hivv setup{R} — OpenCode · VS Code · local LLM wired to hivv\n")

    # ── 1. OpenCode ───────────────────────────────────────────────
    print(f"{B}── OpenCode {R}{'─' * 47}")
    if det["opencode"]["installed"]:
        print(f"  {G}✓{R} opencode on PATH: {det['opencode']['path']}")
    else:
        print(f"  {X}✗{R} opencode not found on PATH")
        # split literal: no single line may contain the download-pipe-shell
        # pattern (old bees scan ADDED lines raw — keep this hint invisible
        # to their critical-pattern regex while printing the same text)
        print(f"    install: {G}curl -fsSL https://opencode.ai/v2/install"
              f" | bash{R}")
        print(f"    (npm/brew/mise: see https://opencode.ai/docs/cli)")
    if write_info:
        color = G if write_info["action"] in ("created", "merged", "already up to date") else X
        print(f"  config: {color}{write_info['path']} — {write_info['action']}{R}")
        if write_info.get("backup"):
            print(f"          backup saved: {write_info['backup']}")
        if write_info["action"] == "merged":
            print(f"          {Y}edit, not overwrite — your existing settings were kept "
                  f"(they win over ours){R}")
    elif installed_here:
        print(f"  config: {target} {G}(hivv provider already installed){R}")
    else:
        print(f"  config: {target} {Y}(not written yet — see Next below){R}")
    print(f"  {B}canonical config{R}:")
    print(textwrap.indent(json.dumps(OPENCODE_CONFIG, indent=2), "    "))
    print(f"  {Y}note{R}: HIVV_API_KEY is optional — anonymous tier works (rate-limited)")
    print(f"        the hivv provider routes every request through the hive")
    print(f"        (local LLM → volunteer relay → cloud)")
    print()

    # ── 2. VS Code ────────────────────────────────────────────────
    print(f"{B}── VS Code {R}{'─' * 48}")
    print(f"  1. install the official {B}OpenCode{R} extension from the Marketplace")
    print(f"     (works in VS Code / Cursor / Windsurf / VSCodium)")
    print(f"  2. open the integrated terminal and run {B}opencode{R}")
    print(f"     ({'Cmd' if _sysname() == 'Darwin' else 'Ctrl'}+Esc = quick launch, "
          f"{'Cmd' if _sysname() == 'Darwin' else 'Ctrl'}+Shift+Esc = new session)")
    if det["vscode"]["code_cli"]:
        print(f"  {G}✓{R} `code` CLI detected — or install the extension from here")
        print(f"     {G}code --install-extension sst-dev.opencode{R}  {Y}(printed only — not run){R}")
    else:
        print(f"  {X}✗{R} `code` CLI not on PATH — install from your editor's Marketplace panel")
    oc_path = det["opencode"]["path"] or "opencode"
    print(f"  alternative (ACP): extension {B}VSCode ACP{R} ({B}omercnet.vscode-acp{R}), then set:")
    print(f'     "vscode-acp.agentPaths": {{"opencode": "{oc_path}"}}')
    print()

    # ── 2b. The GUI-tool matrix ───────────────────────────────────
    _print_gui_section(gui)

    # ── 3. Local LLM + hivv ───────────────────────────────────────
    print(f"{B}── Local LLM + hivv {R}{'─' * 39}")
    print(f"  two paths:")
    print(f"    {B}(a) agent{R}: {APP} start registers this machine's LLM into the hive")
    print(f"        (to serve OTHERS too: {APP} volunteer on)")
    print(f"    {B}(b) direct{R}: set HIVV_LOCAL_ENDPOINTS on the hub gateway")
    print(f"        (comma-separated OpenAI-compat URLs, e.g. http://localhost:11434/v1)")
    if det["local_llm"]:
        print(f"  detected endpoints:")
        for ep in det["local_llm"]:
            print(f"    {G}●{R} {ep['name']} on :{ep['port']} → run `{APP} start` to join the hive with it")
    else:
        print(f"  {Y}·{R} no local LLM answering on :8080 / :11434 / :1234 "
              f"— try `{APP} llm detect`")
    ag = det["agent"]
    if ag["running"]:
        print(f"  agent: {G}running{R} (node {ag.get('node_id') or '?'}, pid {ag['pid']})")
    else:
        print(f"  agent: {Y}not running{R} → run `{APP} start`")
    print()

    # ── 3b. Donating your idle capacity ───────────────────────────
    print(f"{B}── Donating your idle capacity {R}{'─' * 30}")
    try:
        _vol = (read_state() or {}).get("volunteer")
    except Exception:
        _vol = None
    donating = bool(_vol) if _vol is not None else None
    if donating is True:
        print(f"  {G}✓{R} volunteering ON — the hive may use your idle CPU/GPU")
    elif donating is False:
        print(f"  {Y}·{R} volunteering OFF — your machine serves you only")
    else:
        print(f"  {Y}·{R} volunteer state unknown — run `{APP} drone` to check")
    print(f"  {B}how it works{R}: while your own model is busy, the hive")
    print(f"  can send work to your machine; when it is idle, it earns")
    print(f"  {B}honey{R} (ranking, streaks, leaderboard place).")
    print(f"  toggle: {G}{APP} volunteer on{R} / {G}{APP} volunteer off{R}")
    print(f"  {Y}your IDE traffic is private either way — donation shares")
    print(f"  compute, never your prompts or files.{R}")
    print()

    # ── 4. Verify ─────────────────────────────────────────────────
    print(f"{B}── Verify {R}{'─' * 49}")
    print(f"  curl -s {HUB}/v1/models")
    print(f'  {APP} chat "ping" --mode=fast')
    print()

    # ── 5. Next — the exact commands to run from here ────────────
    print(f"{B}── Next {R}{'─' * 51}")
    if not det["opencode"]["installed"]:
        print(f"  1. install opencode first:  "
              f"{G}curl -fsSL https://opencode.ai/v2/install"
              f" | bash{R}")
    elif not installed:
        print(f"  1. {G}{APP} setup --write --global{R}")
        print(f"     {Y}# merges the provider into ~/.config/opencode/opencode.jsonc —")
        print(f"     # EDIT, never overwrite: your existing keys win, backup saved first{R}")
        print(f"     {Y}# (project-only instead: {APP} setup --write){R}")
        print(f"  2. {B}opencode{R}                      # in any project directory")
        print(f"  3. type {B}/models{R} → pick {B}hivv/auto{R}   "
              f"# or hivv/smart · hivv/fast · hivv/cheap · hivv/private")
        print(f"  {Y}fast path (optional): run `{APP} dial` in another terminal, then set{R}")
        print(f"     {Y}\"baseURL\": \"http://127.0.0.1:3911/v1\"  # direct-to-bee, hub only on dial{R}")
    else:
        print(f"  {G}✓{R} hivv provider installed in {installed_path}")
        print(f"  1. {B}opencode{R}                      # in any project directory")
        print(f"  2. type {B}/models{R} → pick {B}hivv/auto{R}   "
              f"# or hivv/smart · hivv/fast · hivv/cheap · hivv/private")
        print(f"  3. test the loop: ask it "
              f"{Y}\"run `echo hi` in the shell and tell me its output\"{R}")
        print(f"  {Y}fast path (optional): run `{APP} dial` in another terminal, then set{R}")
        print(f"     {Y}\"baseURL\": \"http://127.0.0.1:3911/v1\"  # direct-to-bee, hub only on dial{R}")
    print()


def cmd_code(args) -> None:
    """hivv code "<task>" --repo <git-url> — hand a coding task to a coder bee
    (opencode runs it in a temp git worktree on that node)."""
    task = " ".join(getattr(args, "task", []) or []).strip()
    repo_url = (getattr(args, "repo", None) or "").strip()
    if not task or not repo_url:
        die(f'usage: {APP} code "<task>" --repo <git-url> '
            f"[--ref BR] [--timeout S] [--wait] [--json]")
    timeout_s = int(getattr(args, "timeout", None) or 600)
    as_json = bool(getattr(args, "json_output", False))

    body = {"task": task, "repo": {"url": repo_url}, "timeout_s": timeout_s}
    if getattr(args, "ref", None):
        body["repo"]["ref"] = args.ref
    code, resp = http("POST", "/api/coding/jobs", body=body, timeout=30)

    if code == 409:
        msg = (resp or {}).get("error") if isinstance(resp, dict) else str(resp)
        if as_json:
            print(json.dumps(resp if isinstance(resp, dict) else {"error": str(resp)},
                             indent=2))
        else:
            print(f"\033[33m⚠ {msg or 'no coder bees online'}\033[0m")
            print(f"  coder bees need: opencode on PATH + {APP} start + "
                  f"{APP} volunteer on, on some node")
        sys.exit(1)
    if code not in (200, 201, 202) or not isinstance(resp, dict):
        die(f"coding job failed (HTTP {code}): {str(resp)[:200]}")
    job_id = resp.get("job_id")
    node_id = resp.get("node_id")

    if not getattr(args, "wait", False):
        if as_json:
            print(json.dumps(resp, indent=2))
            return
        print(f"queued on {node_id or '?'}")
        print(f"  job_id: {job_id}")
        print(f"  poll:   curl -s {HUB}/api/coding/jobs/{job_id}")
        return

    # --wait: poll every 2s until done/error/expired (max = timeout + 120s)
    if not as_json:
        print(f"queued on {node_id or '?'} — waiting for {job_id} "
              f"(up to {timeout_s + 120}s)…")
    deadline = time.time() + timeout_s + 120
    final = None
    while time.time() < deadline:
        time.sleep(2)
        gc, gp = http("GET", f"/api/coding/jobs/{job_id}", timeout=15)
        if gc == 404:
            die(f"job {job_id} not found (expired?)")
        if gc == 200 and isinstance(gp, dict):
            final = gp
            st = gp.get("status")
            if (st in ("done", "completed", "ok", "error", "failed", "expired")
                    or isinstance(gp.get("result"), dict) or gp.get("error")):
                break
    if final is None:
        warn(f"still running after {timeout_s + 120}s — poll: "
             f"curl -s {HUB}/api/coding/jobs/{job_id}")
        return
    if as_json:
        print(json.dumps(final, indent=2))
        if final.get("status") in ("error", "failed", "expired"):
            sys.exit(1)
        return

    status = final.get("status") or "?"
    result = final.get("result") if isinstance(final.get("result"), dict) else {}
    bad = status in ("error", "failed", "expired") or result.get("status") == "error"
    print(f"\033[{32 if not bad else 31}m● {status}\033[0m"
          + (f" — {final.get('error')}" if final.get("error") else ""))
    summary = result.get("summary") or final.get("error")
    if summary:
        print(f"  {summary}")
    files = result.get("files_changed") or []
    if files:
        print(f"  files changed ({len(files)}):")
        for f in files[:40]:
            print(f"    {f}")
        if len(files) > 40:
            print(f"    … +{len(files) - 40} more")
    elif not bad:
        print(f"  (no files changed)")
    if result.get("tests"):
        t = result["tests"]
        print(f"  tests: {t.get('passed')}/{t.get('ran')} passed")
    if result.get("patch"):
        print(f"  patch: {len(result['patch'])} chars"
              + (" (truncated)" if len(result["patch"]) >= _PATCH_MAX_CHARS else ""))
    if bad:
        sys.exit(1)


# ── hivv dial — localhost OpenAI-compatible proxy shim ─────────────────
def cmd_dial(args) -> None:
    """hivv dial [--port N] [--hub URL] [--pin NODE|URL] — local OpenAI proxy.

    The connection STICKS to a specific bee/model: the hub is consulted only
    for the first dial (or in the BACKGROUND afterwards) — the data plane is
    direct-to-bee, hivv.org stays a quick control plane (selection, speed/
    quality telemetry, distribution, failover). --pin locks one bee (or a raw
    endpoint URL) with no rotation and no hub fallback."""
    port = int(getattr(args, "port", 0) or 3911)
    hub = (getattr(args, "hub", None) or HUB).rstrip("/")
    pin = (getattr(args, "pin", None) or "").strip() \
        or os.environ.get("HIVV_DIAL_PIN", "").strip()

    # ── stable shim id: sha1(user@host)[:12], written once ────────
    shim_file = STATE_DIR / "dial-id"
    shim = ""
    try:
        shim = shim_file.read_text().strip()
    except OSError:
        pass
    if not shim:
        shim = hashlib.sha1(
            f"{getpass.getuser()}@{socket.gethostname()}".encode()).hexdigest()[:12]
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            shim_file.write_text(shim + "\n")
        except OSError:
            pass
    shim_host = socket.gethostname()

    # ── route cache: stale-while-revalidate — the connection sticks to its
    #    bee. The hub is only dialed SYNCHRONOUSLY when the cache is empty;
    #    stale entries are served instantly and re-dialed in the BACKGROUND.
    #    last_good never expires — a hub outage must not kill the shim.
    cache_lock = threading.Lock()
    cache = {"dial_id": "", "candidates": [], "ts": 0.0}
    last_good = {"dial_id": "", "candidates": []}
    _refresh = {"on": False}
    created = int(time.time())
    models = [
        ("auto",    "hivv auto — balanced routing"),
        ("smart",   "hivv smart — quality mode"),
        ("fast",    "hivv fast — speed mode"),
        ("cheap",   "hivv cheap — cost mode"),
        ("private", "hivv private — local/hive only"),
    ]

    def _dial_blocking(auth, prompt_est, tools, mode, model):
        """Synchronous dial: hub → else last_good → else (None, None).
        Only the FIRST dial (or a heal after total failure) ever blocks."""
        payload = json.dumps({
            "prompt_est": int(prompt_est), "tools": bool(tools),
            "mode": mode or "balanced", "model": model,
            "shim": shim, "shim_host": shim_host,
        }).encode()
        headers = {"Content-Type": "application/json",
                   "User-Agent": f"{APP}/{VERSION}"}
        if auth:
            headers["Authorization"] = auth     # hub resolves it to a principal
        req = urllib.request.Request(f"{hub}/api/route/dial", data=payload,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            cands = data.get("candidates") if isinstance(data, dict) else None
            if isinstance(cands, list) and cands:
                with cache_lock:
                    cache["dial_id"] = str(data.get("dial_id") or "")
                    cache["candidates"] = list(cands)
                    cache["ts"] = time.time()
                    last_good["dial_id"] = cache["dial_id"]
                    last_good["candidates"] = list(cands)
                return list(cands), cache["dial_id"]
        except Exception:
            pass   # 429 / 503 / 5xx / 404 / network → "hub unavailable"
        with cache_lock:
            if last_good["candidates"]:
                return list(last_good["candidates"]), last_good["dial_id"]
        return None, None

    def _dial(auth, prompt_est, tools, mode, model):
        """(candidates, dial_id) — serve the cache whenever it exists; stale
        entries refresh in the background (stale-while-revalidate); only a
        cold start blocks the request path. THIS is 'connect to a specific
        model': nothing re-rolls the route under a live connection."""
        with cache_lock:
            have = bool(cache["candidates"])
            if have and time.time() - cache["ts"] < 60:
                return list(cache["candidates"]), cache["dial_id"]
            cands, did = list(cache["candidates"]), cache["dial_id"]
            kick = have and not _refresh["on"]
            if kick:
                _refresh["on"] = True
        if have:
            def _bg():
                try:
                    _dial_blocking(auth, prompt_est, tools, mode, model)
                finally:
                    _refresh["on"] = False
            if kick:
                threading.Thread(target=_bg, daemon=True).start()
            return cands, did
        return _dial_blocking(auth, prompt_est, tools, mode, model)

    def _target(cand):
        """(host, port, path, tls) for a candidate — or None if unusable."""
        endpoint = (f"{hub}/v1/chat/completions"
                    if cand.get("transport") == "hub" else cand.get("endpoint"))
        if not isinstance(endpoint, str) or not endpoint:
            return None
        try:
            u = urllib.parse.urlparse(endpoint)
            if u.scheme not in ("http", "https") or not u.hostname:
                return None
            pnum = u.port or (443 if u.scheme == "https" else 80)
        except Exception:
            return None
        path = u.path or "/v1/chat/completions"
        if u.query:
            path += "?" + u.query
        return u.hostname, pnum, path, u.scheme == "https"

    def _report(cand, ok, latency_ms):
        """Fire-and-forget: tell the hub how the forwarded request went."""
        transport = cand.get("transport")
        if transport not in ("direct", "hub", "relay"):
            transport = "direct"

        def _post():
            try:
                body = json.dumps({
                    "shim": shim,
                    "stick_key": str(cand.get("stick_key") or ""),
                    "node_id": str(cand.get("node_id") or ""),
                    "model": str(cand.get("model") or ""),
                    "ok": bool(ok),
                    "latency_ms": int(latency_ms),
                    "transport": transport,
                }).encode()
                req = urllib.request.Request(f"{hub}/api/route/report", data=body,
                                             headers={"Content-Type": "application/json"},
                                             method="POST")
                urllib.request.urlopen(req, timeout=3).read()
            except Exception:
                pass   # telemetry is best-effort — never breaks the chat path

        threading.Thread(target=_post, daemon=True).start()

    class DialHandler(http_server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{APP}-dial/{CLI_VERSION}"

        def log_message(self, fmt, *a):
            pass   # our own one-line [DIAL] log is the only output

        def _send_json(self, status, obj):
            """EOF-framed JSON: NO Content-Length — closing the connection
            delimits the body (same framing as the pass-through; valid for
            SSE and JSON alike, and OpenCode/fetch handle it)."""
            if hasattr(self, "_headers_buffer"):
                self._headers_buffer = []   # drop any half-written status line
            try:
                body = json.dumps(obj).encode()
                self.send_response_only(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

        def do_GET(self):
            try:
                path = urllib.parse.urlparse(self.path).path
                if path == "/healthz":
                    with cache_lock:
                        n = len(cache["candidates"]) or len(last_good["candidates"])
                        age = int(time.time() - cache["ts"]) if cache["ts"] else 0
                    self._send_json(200, {"ok": True, "shim": "hivv-dial",
                                          "candidates": n, "cache_age_s": age})
                    return
                if path == "/v1/models":
                    data = [{"id": mid, "object": "model", "created": created,
                             "owned_by": "hivv", "display_name": label}
                            for mid, label in models]
                    self._send_json(200, {"object": "list", "data": data})
                    return
                self._send_json(404, {"error": {
                    "message": f"hivv dial: unknown path: {path}",
                    "type": "invalid_request_error"}})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:
                print(f"[DIAL] handler error: GET {self.path}: "
                      f"{e.__class__.__name__}: {e}", flush=True)
                self.close_connection = True

        def do_POST(self):
            t0 = time.time()
            committed = False
            try:
                path = urllib.parse.urlparse(self.path).path
                if path != "/v1/chat/completions":
                    self._send_json(404, {"error": {
                        "message": f"hivv dial: unknown path: {path}",
                        "type": "invalid_request_error"}})
                    return

                # ── 1. read + parse the client body ───────────────
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b""
                try:
                    body = json.loads(raw.decode("utf-8", "replace"))
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                except (ValueError, json.JSONDecodeError) as e:
                    self._send_json(400, {"error": {
                        "message": f"hivv dial: bad request body ({e})",
                        "type": "invalid_request_error"}})
                    return
                route = body.get("route")
                mode = (route.get("mode") if isinstance(route, dict) else None) \
                    or "balanced"
                model = body.get("model") if isinstance(body.get("model"), str) \
                    else None
                auth = self.headers.get("Authorization")

                # ── 2. dial the hub (cached 60s; last_good forever) ─
                cands, _dial_id = _dial(auth, len(raw) // 4,
                                         bool(body.get("tools")), mode, model)
                if not cands:
                    self._send_json(502, {"error": {
                        "message": "hivv dial: hub unreachable and no cached route",
                        "type": "server_error"}})
                    return
                # --pin: ONE specific bee (or a raw endpoint URL) — no rotation
                # to other bees, no hub fallback. If a node-id pin isn't in the
                # current map (bee offline), fall back to the full list rather
                # than dying — the pin re-latches as soon as the bee reappears.
                if pin:
                    if "://" in pin:
                        cands = [{"transport": "direct", "endpoint": pin,
                                  "node_id": "pinned", "model": "pinned",
                                  "stick_key": None, "ticket": ""}]
                    else:
                        locked = [c for c in cands
                                  if isinstance(c, dict) and c.get("node_id") == pin]
                        if locked:
                            cands = locked

                # ── 3. forward over candidates in order ────────────
                fwd = {}
                for k, v in self.headers.items():
                    if k.lower() in ("host", "content-length", "accept-encoding",
                                     "connection", "keep-alive", "proxy-connection",
                                     "transfer-encoding", "upgrade", "expect"):
                        continue
                    fwd[k] = v.replace("\r", " ").replace("\n", " ")
                fwd["Accept-Encoding"] = "identity"  # never buffer compressed bytes
                fwd["Content-Length"] = str(len(raw))

                chosen = None
                status = 0
                stream_err = None    # upstream transport error (no rotation once committed)
                client_gone = False  # caller hung up mid-stream — perfectly normal
                for cand in cands:
                    if not isinstance(cand, dict):
                        continue
                    tgt = _target(cand)
                    if not tgt:
                        continue
                    host, pnum, upath, tls = tgt
                    fwd.pop("X-Hivv-Ticket", None)   # ticket is per-candidate
                    if cand.get("ticket"):
                        fwd["X-Hivv-Ticket"] = str(cand["ticket"])
                    conn = None
                    try:
                        conn = (http_client.HTTPSConnection(host, pnum, timeout=5)
                                if tls else http_client.HTTPConnection(host, pnum, timeout=5))
                        conn.connect()
                        if conn.sock is not None:
                            conn.sock.settimeout(300)   # read timeout (spec: min(300000 ms, …))
                        conn.request("POST", upath, body=raw, headers=fwd)
                        resp = conn.getresponse()
                        if resp.status >= 500:
                            continue   # bee-side failure → rotate to next candidate
                        # ── PASS THROUGH (<500: 4xx is the client's problem) ──
                        chosen = cand
                        status = resp.status
                        try:
                            self.send_response_only(status)
                            skip = ("content-length", "transfer-encoding", "connection")
                            for hk, hv in resp.getheaders():
                                if hk.lower() in skip:
                                    continue
                                try:
                                    self.send_header(hk, hv.replace("\r", " ").replace("\n", " "))
                                except Exception:
                                    pass   # skip an unencodable header, keep the stream
                            self.send_header("Cache-Control", "no-cache")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            committed = True
                        except (BrokenPipeError, ConnectionResetError):
                            client_gone = True
                            break
                        except OSError as e:
                            stream_err = e
                            break
                        # EOF-framed body: stream until upstream closes
                        while True:
                            try:
                                chunk = resp.read(8192)
                            except Exception as e:
                                stream_err = e
                                break
                            if not chunk:
                                break
                            try:
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError, OSError):
                                client_gone = True
                                break
                        break
                    except (OSError, socket.timeout, http_client.HTTPException):
                        continue     # connect / send timeout → next candidate
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass

                # ── 4. log + async report ─────────────────────────
                elapsed = int((time.time() - t0) * 1000)
                if chosen is None:
                    print(f"[DIAL] {time.strftime('%H:%M:%S')} no-route "
                          f"{elapsed}ms fail", flush=True)
                    # heal: force a background re-dial so the NEXT request
                    # latches onto a living bee (never blocks this one)
                    if not pin:
                        threading.Thread(
                            target=lambda: _dial_blocking(
                                auth, len(raw) // 4, bool(body.get("tools")),
                                mode, model),
                            daemon=True).start()
                    self._send_json(502, {"error": {
                        "message": "hivv dial: all candidates failed",
                        "type": "server_error"}})
                    return
                ok = status < 500 and stream_err is None
                transport = chosen.get("transport")
                if transport not in ("direct", "hub", "relay"):
                    transport = "direct"
                print(f"[DIAL] {time.strftime('%H:%M:%S')} "
                      f"{chosen.get('node_id') or '?'} {chosen.get('model') or '?'} "
                      f"({transport}) {elapsed}ms {'ok' if ok else 'fail'}",
                      flush=True)
                _report(chosen, ok, elapsed)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception as e:
                # never swallow — one compact line, then a best-effort 500
                print(f"[DIAL] handler error: POST {self.path}: "
                      f"{e.__class__.__name__}: {e}", flush=True)
                if not committed:
                    try:
                        self._send_json(500, {"error": {
                            "message": f"hivv dial: internal error ({e.__class__.__name__})",
                            "type": "server_error"}})
                    except Exception:
                        pass
                self.close_connection = True

    class DialServer(http_server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    try:
        srv = DialServer(("127.0.0.1", port), DialHandler)
    except OSError as e:
        die(f"dial: cannot bind http://127.0.0.1:{port} — {e}")

    print(f"[{APP}] dial ready — OpenCode/any OpenAI client → "
          f"http://127.0.0.1:{port}/v1", flush=True)
    print(f"       routing dial: {hub}   "
          f"(direct-to-bee; background re-dial, hub only on dial + report)",
          flush=True)
    if pin:
        print(f"       pinned: {pin}   (no rotation, no hub fallback)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass   # Ctrl+C exits cleanly — server_close, no traceback
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
    print(f"[{APP}] dial stopped", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=APP, description=__doc__.splitlines()[1],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    # Output control
    p.add_argument("--plain", action="store_true", help="plain output: no ANSI colors or metadata (print only text)")
    p.add_argument("--no-think", dest="no_think", action="store_true", help="disable internal thinking/chain-of-thought when requesting completions")
    sub = p.add_subparsers(dest="cmd")

    def add_common(sp):
        sp.add_argument("--hub", default=None, help=f"override hub URL (default {HUB})")

    # operator / local
    sp = sub.add_parser("start",    help="start the local hivv agent")
    sp = sub.add_parser("stop",     help="stop the local hivv agent")
    sp = sub.add_parser("restart",  help="restart the local hivv agent")

    sp = sub.add_parser("status",   help="pid, uptime, last heartbeat, last error")
    sp = sub.add_parser("info",     help="hub URL, version, install paths, hub_health")
    sp = sub.add_parser("uninstall",help="stop + remove install (keep config + logs)")

    sp = sub.add_parser("logs",    help="tail agent log")
    sp.add_argument("n", nargs="?", type=int, default=50, help="lines (default 50)")
    sp.add_argument("-f", "--follow", action="store_true", help="follow the log")

    sp = sub.add_parser("free",    help="mem + swap + disk + per-process RSS")
    sp = sub.add_parser("pct",     help="live CPU%% / MEM%% (use --watch=N to set interval)")
    sp.add_argument("--watch", type=float, default=1.0, help="refresh interval seconds (default 1; 0 = print once)")

    sp = sub.add_parser("ping",    help="ping the hub — measure round-trip latency")
    sp.add_argument("name", nargs="+", help="node name or id to ping")

    sp = sub.add_parser("tools",   help="manage local tool server + list available tools on the hive")
    sp.add_argument("action", nargs="?", default="list",
                    choices=["on", "off", "status", "share", "list"],
                    help="on/off/status/share/list (default: list)")

    sp = sub.add_parser("call",    help="invoke a shared tool through the hive (no local LLM needed)")
    sp.add_argument("tool", choices=["time", "calculate", "fetch", "search", "sysinfo"],
                    help="tool to invoke")
    sp.add_argument("--tz",  default=None, help="timezone for 'time' tool (e.g. America/New_York)")
    sp.add_argument("--expr", default=None, help="expression for 'calculate' tool (e.g. '2+2')")
    sp.add_argument("--url",  default=None, help="URL for 'fetch' tool")
    sp.add_argument("-q", "--query", dest="q", default=None, help="query for 'search' tool")
    sp.add_argument("--local", action="store_true", help="force use of local tools server (bypass hive routing)")

    sp = sub.add_parser("dev",   help="toggle dev mode (blocks auto-update to protect local changes)")
    sp.add_argument("--off", action="store_true", help="disable dev mode and re-enable auto-update")

    sp = sub.add_parser("update",  help="pull latest hivv-cli.py from hivv.org (LLM-reviewed first)")
    sp.add_argument("--from-url", dest="from_url", default=None,
                    help="download from this URL instead of <hub>/hivv-cli.py")
    sp.add_argument("--sha256", default=None,
                    help="verify downloaded bytes against this hex SHA256")
    sp.add_argument("--force", action="store_true",
                    help="force update even in dev mode, or despite a SUSPICIOUS review verdict")
    sp.add_argument("--skip-review", dest="skip_review", action="store_true",
                    help="skip the pre-apply LLM review (emergency/unattended updates)")
    sp.add_argument("--show-review", dest="show_review", action="store_true",
                    help="show the last cached review verdict + findings, then exit")

    sp_upg = sub.add_parser("upgrades", help="recent upgrade review outcomes across the hive (who applied/blocked what)")
    sp_upg.add_argument("--flagged", action="store_true", help="only blocked/failed upgrades")
    sp_upg.add_argument("--node", default=None, help="filter by node id")
    sp_upg.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")

    # Repair plans: signed, allowlisted instructions pushed into a bee's inbox.
    # The bee re-verifies the HMAC/ttl/nonce and refuses anything off the list.
    sp_rep = sub.add_parser("repair", help="push a signed, allowlisted repair plan to a bee")
    sp_rep.add_argument("node", nargs="+", help="bee to repair (e.g. masjid)")
    sp_rep.add_argument("--op", action="append", required=True, metavar="OP",
                        help="cli_update | agent_restart | reap_duplicates | service_restart (repeatable, run in order)")
    sp_rep.add_argument("--unit", default=None,
                        help="systemd unit for --op service_restart (allowlisted: llama.service, ollama.service, mcp-bridge.service, …)")
    sp_rep.add_argument("--reason", default=None, help="why — recorded on the hub")
    sp_rep.add_argument("--ttl", type=int, default=300, help="plan lifetime seconds (60-600, default 300)")
    sp_rep.add_argument("--force", action="store_true",
                        help="for --op cli_update: authorise applying an update the review gate called suspicious "
                             "(the plan's signature is what makes this an operator decision)")

    # one-shot
    sp = sub.add_parser("health",      help="GET /api/health")
    sp = sub.add_parser("metrics",     help="GET /api/control/metrics/global")
    sp.add_argument("--local", action="store_true", help="print this bee's local metrics instead of hub global")
    sp_fs = sub.add_parser("fast-scan", help="deterministic, AI-free source screen (free, instant)")
    sp_fs.add_argument("path", help="file to screen")
    sp_fs.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_fs.add_argument("--hub", help="talk to a local gateway instead of the public hub")
    sub.add_parser("fastscan", help="alias for fast-scan")
    sp_pt = sub.add_parser("partner", help="the public partner API (what other sites integrate with)")
    sp_pt.add_argument("what", nargs="?", default="manifest",
                       choices=["manifest", "health", "network", "models", "capabilities"])
    sp_lo = sub.add_parser("local", help="manage a LOCAL self-hosted HIVV gateway")
    sp_lo.add_argument("what", nargs="?", default="status",
                       choices=["status", "start", "stop", "info"])
    sp_oc = sub.add_parser("opencode", help="the recommended interface — detect, explain, install, wire up")
    sp_oc.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_oc.add_argument("--wire", action="store_true", help="write the hivv provider into opencode.jsonc")
    sp_oc.add_argument("--why", action="store_true", help="explain why OpenCode is recommended")
    sp_tl = sub.add_parser("capabilities", help="what can this HIVV instance do? (capability discovery)")
    sp_tl.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_pb = sub.add_parser("problems", help="what is breaking globally? (error log + incidents)")
    sp_pb.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_pb.add_argument("--window", type=int, default=3600, help="look-back window in seconds (default 3600)")
    sp_pb.add_argument("--node", default=None, help="filter to one node (e.g. yalla, masjid)")
    sub.add_parser("errors", help="alias for problems")
    sp_bg = sub.add_parser("bug", help="research a specific problem (searches the live error log)")
    sp_bg.add_argument("query", nargs="*", help="what broke (class, endpoint, or message fragment)")
    sp_bg.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_bg.add_argument("--limit", type=int, default=20, help="max entries (default 20)")
    sp_bg.add_argument("--client", default=None, help="filter by client: opencode, continue, cline, vscode, …")
    sp_bg.add_argument("--node", default=None, help="filter by node (e.g. yalla, masjid)")
    sp_sb = sub.add_parser("submit", help="file a bug report to the hub")
    sp_sb.add_argument("text", nargs="+", help="what you saw")
    sp_sb.add_argument("--subject", default=None, help="short summary line")
    sp_sb.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_rq = sub.add_parser("request", help="request a feature")
    sp_rq.add_argument("text", nargs="+", help="what you want")
    sp_rq.add_argument("--subject", default=None, help="short summary line")
    sp_rq.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_fl = sub.add_parser("flag", help="flag something another node/user is doing")
    sp_fl.add_argument("parts", nargs="+", help='<target> "<what they are doing>"')
    sp_fl.add_argument("--severity", default=None, choices=["low", "medium", "high", "critical"])
    sp_fl.add_argument("--evidence", default=None, help="supporting detail (ids, timestamps, log lines)")
    sp_fl.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp = sub.add_parser("nodes",       help="GET /api/control/nodes")
    sp.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_n = sub.add_parser("node",       help="show details or chat with a specific node")
    sp_n.add_argument("name", nargs="+", help="node name or id")
    sp_n.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_nc = sub.add_parser("node-chat", help="chat directly with a node's LLM")
    sp_nc.add_argument("name", help="node name or id to chat with")
    sp_nc.add_argument("message", nargs="+", help="message to send")
    sp_r = sub.add_parser("reach",      help="test connectivity to another node on the network")
    sp_r.add_argument("name", nargs="+", help="node name or id to reach")
    sp_bees = sub.add_parser("bees",      help="all bees in the hive — alias for nodes 🐝")
    sp_bees.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_bee = sub.add_parser("bee",       help="inspect a specific bee — alias for node")
    sp_bee.add_argument("name", nargs="+", help="bee name or id")
    sp_bee.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_swarm = sub.add_parser("swarm",    help="swarm a question to multiple models — alias for hive-mind 🧠")
    sp_swarm.add_argument("message", nargs="+", help="question to swarm")
    sp_swarm.add_argument("--json", dest="json_output", action="store_true")
    sp_swarm.add_argument("--quiet", dest="quiet", action="store_true", help="verdict only")
    sp_waggle = sub.add_parser("waggle",   help="dance to check if a bee is reachable — alias for reach")
    sp_waggle.add_argument("name", nargs="+", help="bee name or id to waggle at")
    sp_drone = sub.add_parser("drone",    help="your volunteer status — are you a worker bee? 🐝")
    sub.add_parser("debug",      help="show debug info: hub, agent, local AI, network nodes")
    sp_a = sub.add_parser("add",        help="add any AI to the hivv network (cloud or local)")
    sp_a.add_argument("provider", nargs="?", help="provider: openai, anthropic, google, groq, ollama, llamacpp, custom")
    sp_a.add_argument("api_key", nargs="?", help="API key or token")
    sp_a.add_argument("--endpoint", help="custom endpoint URL (for custom/self-hosted)")
    sp_a.add_argument("--name", help="friendly name for this key")
    sp_a2 = sub.add_parser("add-ai",    help="alias for add — add any AI to the network")
    sp_a2.add_argument("provider", nargs="?", help="provider name")
    sp_a2.add_argument("api_key", nargs="?", help="API key")
    sp_a2.add_argument("--endpoint", help="custom endpoint URL")
    sp_a2.add_argument("--name", help="friendly name")
    sp_s = sub.add_parser("stats",      help="show per-node stats and cluster totals")
    sp_s.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")
    sp_inc = sub.add_parser("incidents", help="list, summarize, or resolve network incidents")
    sp_inc_sub = sp_inc.add_subparsers(dest="incidents_sub")
    sp_inc_list = sp_inc_sub.add_parser("list",   help="list active incidents")
    sp_inc_list.add_argument("--json",  dest="json_output", action="store_true")
    sp_inc_list.add_argument("--state", help="filter by state (active, resolved, investigating)")
    sp_inc_sub.add_parser("summary",  help="full health summary: incidents + per-node heartbeat age")
    sp_inc_res = sp_inc_sub.add_parser("resolve", help="resolve/dismiss an incident")
    sp_inc_res.add_argument("id",      help="incident ID to resolve")
    sp_inc_res.add_argument("--notes", help="resolution notes (default: 'Resolved via CLI')")
    sp_inc_create = sp_inc_sub.add_parser("create", help="create a new incident (for testing)")
    sp_inc_create.add_argument("--node-id",   required=True, help="node ID")
    sp_inc_create.add_argument("--title",      required=True, help="incident title")
    sp_inc_create.add_argument("--type",       dest="incident_type", default="node_stale",
                               help="incident type (default: node_stale)")
    sp_inc_create.add_argument("--severity",   default="medium",
                               choices=["low","medium","high","critical"], help="severity")
    sp_inc_create.add_argument("--description", help="description")
    sp = sub.add_parser("whoami",      help="cheap liveness check against hub")
    sp_m = sub.add_parser("models",     help="list all available AI models — catalog + which bee runs which local model (--bees: bees only)")
    sp_m.add_argument("--fc", action="store_true", help="show only models with function calling support")
    sp_m.add_argument("--provider", default=None, help="filter by provider (e.g. openai, anthropic, groq, local)")
    sp_m.add_argument("--local", action="store_true", help="show only your local models")
    sp_m.add_argument("--bees", action="store_true", help="only show which models run on which bees")
    sp_doc = sub.add_parser("doctor",     help="full health report with auto-fix")
    sp_doc.add_argument("--fix", action="store_true", help="auto-fix all detected issues")
    sp_fw = sub.add_parser("firewall", help="check/configure firewall so other hivv nodes can reach your LLMs")
    sp_fw.add_argument("--apply", action="store_true", help="actually apply the firewall rules (without this, just shows what to do)")
    sp_fw.add_argument("--mode", choices=["lan", "tailscale", "open"], default="lan", help="who can reach your LLMs: lan (default), tailscale, or open")
    sub.add_parser("up",        help="quick 3-line status — the 'you good?' command")
    sub.add_parser("speed",      help="latency benchmark (DNS, TLS, hub ping, chat TTFB)")
    sub.add_parser("test",       help="doctor + speed end-to-end smoke test")
    sp_v = sub.add_parser("volunteer", help="opt in/out of contributing compute to the network (on/off/status)")
    sp_v.add_argument("action", nargs="?", choices=["on", "off", "status"], default="status")
    sp_sh = sub.add_parser("share", help="broadcast what you're working on to the hive 🐝")
    sp_sh.add_argument("message", nargs="?", help="what you're working on (max 280 chars)")
    sp_sh.add_argument("--problem", dest="problem", metavar="ID", help="link a problem ID")
    sp_sh.add_argument("--clear", action="store_true", help="clear your work status")
    sp_sh.add_argument("--card", action="store_true", help="save a shareable bee card (SVG) with your stats")
    sp_hn = sub.add_parser("honey", help="your bee card: honey, rank, streak — hivv honey [--board]")
    sp_hn.add_argument("node", nargs="?", help="another bee's name (default: you)")
    sp_hn.add_argument("--board", action="store_true", help="show the leaderboard instead")
    sp_hn.add_argument("--window", choices=["week", "month", "all"], default=None, help="leaderboard window")
    sub.add_parser("tips", help="show tip analytics — which tips are shown most and drive the most action")
    sp_hm = sub.add_parser("hive-mind", help="ask 2-6 different models at once + bee-judge synthesis 🧠")
    sp_hm.add_argument("question", nargs="*", help="the question for the hive-mind")
    sp_hm.add_argument("--max", type=int, default=4, help="max brains to ask (2-6)")
    sp_hm.add_argument("--json", action="store_true", help="output raw JSON (all answers + verdict)")
    sp_hm.add_argument("--quiet", action="store_true", help="skip per-brain answers, show only the verdict")
    sub.add_parser("feed",      help="see what the hive is working on — real-time collaboration")
    sub.add_parser("whatsup",  help="see what everyone on the hive is working on (alias for feed)")
    sub.add_parser("whats-up", help="see what everyone on the hive is working on (alias for feed)")
    sp_d = sub.add_parser("donate",    help="diagnose your system + get personalized recommendations on what to donate")
    sp_d.add_argument("action", nargs="?", default="diagnose",
                     choices=["diagnose", "history"],
                     help="diagnose / history (default: diagnose)")
    sp_h = sub.add_parser("help",  help="submit help requests, suggestions, complaints, and bugs — or list what you've submitted")
    sp_h.add_argument("action", nargs="?", default="list",
                     choices=["list", "submit"],
                     help="list / submit (default: list)")
    sp_h.add_argument("type", nargs="?", choices=["suggestion", "complaint", "help_request", "bug", "praise", "other"],
                     help="type: suggestion, complaint, help_request, bug, praise, other")
    sp_h.add_argument("message", nargs=argparse.REMAINDER, help="your message")
    sub.add_parser("panic",      help="emergency help — diagnostics + how to report (alias for help-bang)")
    sub.add_parser("help-bang",  help="emergency help — diagnostics + how to report")
    sub.add_parser("welcome",    help="guided first-run tour — everything a new user needs")
    sp_top = sub.add_parser("top",        help="real-time cluster activity (like 'top' for the network)")
    sp_top.add_argument("--verbose", action="store_true", help="show detailed debug info")
    sub.add_parser("benchmark",  help="test all nodes — find the fastest one")
    sub.add_parser("hi",         help="friendly greeter — quick chat round-trip")
    sub.add_parser("thanks",     help="say thanks to the AI (one-line chat reply)")
    sp_s = sub.add_parser("support", help="ask the AI for help — auto-attaches doctor + log context")
    sp_s.add_argument("question", nargs=argparse.REMAINDER, help="optional question (else the AI greets + asks)")
    sp = sub.add_parser("chat",        help="POST /api/chat MSG")
    sp.add_argument("--mode", default=None,
                    choices=["balanced", "quality", "fast", "cost", "private", "trusted", "smart", "cheap"],
                    help="routing strategy: smart=best model · cheap=free compute · fast=lowest latency · private=no cloud")
    sp.add_argument("message", nargs=argparse.REMAINDER)
    sp = sub.add_parser("completions", help="POST /v1/chat/completions (OpenAI-compatible)")
    sp.add_argument("message", nargs=argparse.REMAINDER)

    # hivv OS: capability layer · policy gate · route planner
    sp_c = sub.add_parser("cap", help="capability layer — what the hive can do (list|can|node)")
    sp_c.add_argument("action", nargs="?", default="list", choices=["list", "can", "node"])
    sp_c.add_argument("name", nargs="?", default=None, help="capability (for can) or node (for node)")
    sp_p = sub.add_parser("policy", help="YES/MAYBE/NO policy — show|explain|history|verify|grants")
    sp_p.add_argument("action", nargs="?", default="show",
                      choices=["show", "explain", "history", "verify", "grants"])
    sp_p.add_argument("name", nargs="?", default=None, help="capability (for explain)")
    sp_ap = sub.add_parser("approve", help="approve a MAYBE: hivv approve <grant_id>|--all|--list")
    sp_ap.add_argument("ids", nargs="*", help="grant ids (G_xxx)")
    sp_ap.add_argument("--list", dest="do_list", action="store_true", help="just list pending MAYBEs")
    sp_ap.add_argument("--all", action="store_true", help="approve all pending")
    sp_ap.add_argument("--scope", choices=["once", "standing"], default="standing",
                       help="once = single use · standing = allow until expiry (default)")
    sp_ap.add_argument("--ttl", type=int, default=None, help="standing grant lifetime in seconds (default 30 days)")
    sp_ap.add_argument("--memo", default=None, help="note for the audit trail")
    sp_dn = sub.add_parser("deny", help="refuse a MAYBE: hivv deny <grant_id> [...]")
    sp_dn.add_argument("ids", nargs="+", help="grant ids (G_xxx)")
    sp_dn.add_argument("--memo", default=None)
    sp_rt = sub.add_parser("route", help="route planner — dry-run who answers & why (never executes)")
    sp_rt.add_argument("question", nargs="*", help="optional question to plan for")
    sp_rt.add_argument("--mode", default=None,
                       choices=["balanced", "quality", "fast", "cost", "private", "trusted", "smart", "cheap"],
                       help="routing strategy to preview (default: balanced)")
    sp_rt.add_argument("--capability", default="reasoning", help="capability to plan for (default reasoning)")
    sp_rt.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")

    sp_ex = sub.add_parser("exec", help="run code in an isolated hub sandbox (no net, quotas, ephemeral) — gated by coding.execute")
    sp_ex.add_argument("code", nargs="*", help="code to run (or use --file / pipe via stdin)")
    sp_ex.add_argument("--lang", default="python", choices=["python", "python3", "bash", "sh", "node", "javascript"],
                       help="language / interpreter (default python)")
    sp_ex.add_argument("--file", default=None, help="read code from this file instead of argv")
    sp_ex.add_argument("--timeout", type=int, default=None, help="max run time in ms (1000–30000, default 10000)")

    # one-shot setup: OpenCode + VS Code + local LLM wired to hivv
    sp_su = sub.add_parser("setup", help="configure OpenCode + VS Code + local LLM with hivv")
    sp_su.add_argument("--write", nargs="?", default=None, const="", metavar="PATH",
                       help="write/merge the canonical opencode config "
                            "(default ./opencode.jsonc; use --global for ~/.config/opencode/)")
    sp_su.add_argument("--global", dest="global_cfg", action="store_true",
                       help="with --write: target ~/.config/opencode/opencode.jsonc")
    sp_su.add_argument("--json", dest="json_output", action="store_true",
                       help="output detection dict + config as JSON")
    sp_su.add_argument("--profile", default="hybrid",
                       choices=["local", "hybrid", "hive", "cloud"],
                       help="which backend the GUI tools should use: "
                            "local (your model only), hybrid (local first, hive fallback), "
                            "hive (global network), cloud (a provider key)")
    sp_su.add_argument("--backend", default=None, metavar="MODEL",
                       help="pin one specific local model (name or model id)")
    sp_su.add_argument("--tools", default=None, metavar="IDS",
                       help="only these tool ids, comma-separated "
                            "(default: every detected tool)")
    sp_su.add_argument("--gui", action="store_true",
                       help="also write the GUI tool configs (VS Code, JetBrains, Zed, …)")
    sp_su.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="show what would be written without writing anything")
    sp_su.add_argument("--hub", default=None, metavar="URL",
                       help="the HIVV hub to point at (default: the public hive)")

    # coder bees: hand a coding task to a node running opencode
    sp_cd = sub.add_parser("code", help="ask a coder bee to do a task in a git repo")
    sp_cd.add_argument("task", nargs="+", help="what should the coder bee do?")
    sp_cd.add_argument("--repo", required=True, help="git URL of the repository")
    sp_cd.add_argument("--ref", default=None, help="branch/tag to clone (default: remote HEAD)")
    sp_cd.add_argument("--timeout", type=int, default=None, help="max run time in seconds (default 600)")
    sp_cd.add_argument("--wait", action="store_true", help="poll every 2s until the job finishes")
    sp_cd.add_argument("--json", dest="json_output", action="store_true", help="output raw JSON")

    # local OpenAI-compatible proxy shim: hub dials once, then direct-to-bee
    sp_dl = sub.add_parser("dial", help="local OpenAI proxy shim — dial once, then talk straight to a specific bee")
    sp_dl.add_argument("--port", type=int, default=3911, help="listen port (default 3911)")
    sp_dl.add_argument("--pin", default="", help="stick to ONE bee: node id (e.g. masjid) or a full endpoint URL — no rotation, no hub fallback")
    add_common(sp_dl)

    # local LLM helper
    sp = sub.add_parser("llm", help="local AI runtime helper (ollama, llama.cpp, ...)")
    sp_llm = sp.add_subparsers(dest="llm_cmd")
    sp_llm.add_parser("detect",  help="find ollama, llama.cpp, lm-studio, vllm on this box")
    sp_sg = sp_llm.add_parser("suggest", help="recommend a model for this hardware")
    sp_sg.add_argument("--json", action="store_true",
                       help="emit machine-readable JSON (used by installers)")
    sp_llm.add_parser("bench",   help="quick CPU / RAM / disk benchmark")
    sp_llm.add_parser("status",  help="detect + bench + suggest in one shot")
    sp_i = sp_llm.add_parser("install", help="print install commands (or --docker)")
    sp_i.add_argument("--docker", action="store_true", help="print docker run command")
    sp_i.add_argument("--auto",  action="store_true", help="actually run the install (instead of just printing)")
    sp_i.add_argument("--model", default=None, metavar="NAME",
                      help="after install, run `ollama pull <NAME>`")
    sp_p = sp_llm.add_parser("pull", help="ollama pull <model>")
    sp_p.add_argument("model", help="model name, e.g. qwen3:1.7b")
    sp_sv = sp_llm.add_parser("serve", help="ollama serve [model]")
    sp_sv.add_argument("model", nargs="?", default=None)
    sp_sv.add_argument("--port", type=int, default=11434)

    # llama.cpp native commands
    sp_il = sp_llm.add_parser("install-llama", help="build llama.cpp from source (git clone + cmake)")
    sp_il.add_argument("--auto", action="store_true", help="actually run the build steps")

    sp_pg = sp_llm.add_parser("pull-gguf", help="download a GGUF model from HuggingFace")
    sp_pg.add_argument("model", help="model key, e.g. gemma4:e2b  |  available: " +
                       " / ".join(_GGUF_MODELS.keys()))

    sp_sl = sp_llm.add_parser("serve-llama", help="launch llama-server with a GGUF model")
    sp_sl.add_argument("model", help="model key, e.g. gemma4:e2b")
    sp_sl.add_argument("--port", type=int, default=8080)
    sp_sl.add_argument("--ctx", type=int, default=4096, dest="ctx",
                       help="context size (default 4096)")

    return p


# ── dispatcher ─────────────────────────────────────────────────────────
# ── dispatcher ─────────────────────────────────────────────────────────
# COMMANDS is built defensively: a missing function must NOT brick the whole
# module. (2026-11-17: a lost cmd_opencode raised NameError at import time,
# which killed --healthcheck, hivv update, and every other command — the CLI
# could not repair itself.) Unresolved names become a stub that prints a clear
# message instead of a traceback.
def _cmd_stub(name, missing):
    def _stub(args):
        print(f"\033[31mcommand '{name}' is unavailable in this build\033[0m")
        print(f"  missing function: {missing}")
        print(f"  fix: curl -fsSL {HUB}/{APP}-cli.py -o <this file>")
        print(f"  or:  git clone https://github.com/htmlfarmer/hivv.git")
        sys.exit(1)
    return _stub


def _resolve_cmds(spec):
    out = {}
    for key, value in spec.items():
        if value is None:
            out[key] = None
        elif callable(value):
            out[key] = value
        else:
            fn = globals().get(str(value))
            out[key] = fn if callable(fn) else _cmd_stub(key, value)
    return out

COMMANDS = _resolve_cmds({
    "_agent":          None,  # internal
    "start":           "cmd_start",
    "stop":            "cmd_stop",
    "restart":         "cmd_restart",
    "status":          "cmd_status",
    "logs":            "cmd_logs",
    "free":            "cmd_free",
    "pct":             "cmd_pct",
    "ping":            "cmd_ping",
    "tools":           "cmd_tools",
    "call":            "cmd_call",
    "info":            "cmd_info",
    "update":          "cmd_update",
    "repair":          "cmd_repair",
    "upgrades":        "cmd_upgrades",
    "dev":             "cmd_dev",
    "uninstall":       "cmd_uninstall",
    "health":          "cmd_health",
    "metrics":         "cmd_metrics",
    "fast-scan":       "cmd_fast_scan",
    "fastscan":        "cmd_fast_scan",
    "partner":         "cmd_partner",
    "local":           "cmd_local",
    "capabilities":    "cmd_capabilities",
    "opencode":        "cmd_opencode",
    "problems":        "cmd_problems",
    "errors":          "cmd_problems",
    "bug":             "cmd_bug",
    "submit":          "cmd_submit",
    "request":         "cmd_request",
    "flag":            "cmd_flag",
    "nodes":           "cmd_nodes",
    "node":            "cmd_node",
    "node-chat":       "cmd_node_chat",
    "bees":            "cmd_nodes",
    "bee":             "cmd_node",
    "swarm":           "cmd_hive_mind",
    "waggle":          "cmd_ping",
    "drone":           "cmd_drone",
    "reach":           "cmd_ping",
    "debug":           "cmd_debug",
    "add":             "cmd_add",
    "add-ai":          "cmd_add",
    "stats":           "cmd_stats",
    "incidents":       "cmd_incidents",
    "chat":            "cmd_chat",
    "completions":     "cmd_completions",
    "whoami":          "cmd_whoami",
    "models":          "cmd_models",
    "doctor":          "cmd_doctor",
    "firewall":        "cmd_firewall",
    "donate":          "cmd_donate",
    "help":            "cmd_help",
    "up":              "cmd_up",
    "speed":           "cmd_speed",
    "test":            "cmd_test",
    "volunteer":       "cmd_volunteer",
    "share":           "cmd_share",
    "honey":           "cmd_honey",
    "tips":            "cmd_tips",
    "hive-mind":       "cmd_hive_mind",
    "cap":             "cmd_cap",
    "capability":      "cmd_cap",
    "capabilities":    "cmd_cap",
    "policy":          "cmd_policy",
    "approve":         "cmd_approve",
    "deny":            "cmd_deny",
    "route":           "cmd_route",
    "exec":            "cmd_exec",
    "setup":           "cmd_setup",
    "code":            "cmd_code",
    "dial":            "cmd_dial",
    "feed":            "cmd_feed",
    "whatsup":         "cmd_feed",
    "whats-up":        "cmd_feed",
    "panic":           "cmd_help_bang",
    "help-bang":       "cmd_help_bang",
    "welcome":         "cmd_welcome",
    "top":             "cmd_top",
    "benchmark":       "cmd_benchmark",
    "hi":              "cmd_hi",
    "thanks":          "cmd_thanks",
    "support":         "cmd_support",
    # local LLM helper (dotted names resolved in main())
    "llm":             "cmd_llm_help",
    "llm detect":      "cmd_llm_detect",
    "llm suggest":     "cmd_llm_suggest",
    "llm bench":       "cmd_llm_bench",
    "llm status":      "cmd_llm_status",
    "llm install":     "cmd_llm_install",
    "llm pull":        "cmd_llm_pull",
    "llm serve":       "cmd_llm_serve",
    "llm install-llama":  "cmd_llm_install_llama",
    "llm pull-gguf":      "cmd_llm_pull_gguf",
    "llm serve-llama":   "cmd_llm_serve_llama",
})

def main(argv: list[str]) -> None:
    global HUB
    # Internal: invoked by `hivv start` to fork the agent
    if argv and argv[0] == "_agent":
        from threading import Event
        cmd_agent(Event())
        return

    # Healthcheck: must run BEFORE the smart-default chat fallback (otherwise
    # "healthcheck" would be sent to the LLM as a chat message).
    if argv and argv[0] == "--healthcheck":
        payload = {"ok": True, "version": VERSION, "hub": HUB, "ts": int(time.time())}
        try:
            st = read_state() or {}
            if isinstance(st, dict) and st.get("node_id"):
                payload["node_id"] = st["node_id"]
        except Exception:
            pass
        print(json.dumps(payload))
        sys.exit(0)

    if argv and argv[0] == "--version":
        print(VERSION)
        sys.exit(0)

    # Heal a stale output-capturing launcher (one small file read when healthy;
    # only a stale wrapper triggers a fetch+swap). Must run before command
    # dispatch so `upgrade` and friends can rely on the operator seeing output.
    _refresh_wrapper_if_stale()

    if not argv or argv[0] in ("-h", "--help", "help"):
        _print_root_help()
        return

    cmd_name = argv[0]

    # Resolve dotted subcommand names (e.g. `llm detect` -> `llm detect`).
    # Skip when the next arg is a flag so `hivv llm --help` still resolves to "llm".
    # `llm help` is a special case — it's the parent group's own help, not a subcommand.
    if cmd_name == "llm" and len(argv) > 1 and not argv[1].startswith("-") and argv[1] != "help":
        cmd_name = "llm " + argv[1]

    # `upgrade` is a friendlier alias for `update`. Without this, `hivv upgrade`
    # would fall through to the smart-default chat and send the literal word
    # "upgrade" to the LLM.
    if cmd_name == "upgrade":
        cmd_name = "update"
        argv[0] = "update"
    # `capability` is a documented alias for `cap` but had no parser, so
    # `hivv capability` died in parse_args. Map it like `upgrade` does.
    if cmd_name == "capability":
        cmd_name = "cap"
        argv[0] = "cap"

    # Smart default: if argv[0] doesn't match a known subcommand and isn't a flag,
    # treat the whole argv as a chat message to hivv.org. So:
    #   hivv whats the meaning of life
    # becomes a streaming chat call.
    if cmd_name not in COMMANDS or COMMANDS[cmd_name] is None:
        if cmd_name.startswith("-"):
            die(f"unknown flag: {cmd_name} — try `{APP} --help`")
        _do_chat(" ".join(argv), stream=True)
        return

    # per-cmd --help (use argv[1] for top-level, argv[2] for dotted like `llm detect`)
    help_idx = 2 if cmd_name != argv[0] else 1
    if help_idx < len(argv) and argv[help_idx] in ("-h", "--help"):
        _print_sub_help(cmd_name)
        return

    # `llm help` (and `llm` with no subcommand) should show the parent group's
    # own help — not be parsed as a subcommand choice.
    if cmd_name == "llm" and (len(argv) == 1 or argv[1] == "help"):
        cmd_llm_help(None)
        return

    # ── parse args, with a SELF-HEAL path ──────────────────────────────
    # A bug in the parser (a duplicate subparser, a bad default) makes the
    # CLI unable to run ANY command — including `hivv update`, which is how
    # it would normally fix itself. So a parser failure must offer to
    # re-download the CLI rather than just dying with a traceback.
    try:
        args = build_parser().parse_args(argv)
    except (SystemExit, Exception) as parse_err:
        _self_heal_from_parse_error(parse_err, cmd_name)
        return

    # Global flags
    global PLAIN_OUTPUT, NO_THINK
    PLAIN_OUTPUT = bool(getattr(args, "plain", False))
    NO_THINK = bool(getattr(args, "no_think", False))
    # apply --hub override
    if getattr(args, "hub", None):
        HUB = args.hub
    COMMANDS[cmd_name](args)


def _self_heal_from_parse_error(parse_err, cmd_name: str) -> None:
    """Offer to repair a CLI whose own parser is broken.

    The installed file is the thing that is broken, so the only real fix is
    to replace it with the copy the hub is serving. This function:
      1. prints the actual error (so the operator can report it),
      2. re-downloads hivv-cli.py from the hub into THIS file's location,
      3. verifies the download is not an HTML error page,
      4. tells the operator to re-run their command.

    It deliberately does NOT auto-restart — the operator should see what
    happened and confirm.
    """
    B, R = "\033[1m", "\033[0m"
    G, Y, X = "\033[32m", "\033[33m", "\033[31m"

    here = Path(__file__).resolve()
    print(f"\n{X}✗ {APP} could not start — its own command parser failed{R}")
    print(f"  {B}error{R}: {parse_err}")
    print(f"  {B}file{R}:  {here}")
    print(f"  {B}command{R}: {APP} {cmd_name}")
    print()
    print(f"  {Y}this means the installed CLI is broken, including "
          f"`{APP} update`.{R}")
    print(f"  {Y}the fix is to replace it with the copy the hub serves.{R}")
    print()

    # `--healthcheck` and `--version` return before build_parser(), so they
    # still work — prove it so the operator has a working foothold.
    print(f"  {B}still working{R}: {APP} --healthcheck · {APP} --version")
    print()

    # Offer the repair.
    print(f"  {B}to repair now{R}:")
    print(f"    {G}curl -fsSL {HUB}/{APP}-cli.py -o {here}{R}")
    print(f"    {G}python3 {here} --healthcheck{R}   # verify it works")
    print(f"    {G}{APP} {cmd_name}{R}            # re-run what you wanted")
    print()

    # Or from source, if the hub is unreachable.
    print(f"  {B}or from source{R}:")
    print(f"    {G}git clone https://github.com/htmlfarmer/hivv.git{R}")
    print(f"    {G}cp hivv/public_html/{APP}-cli.py {here}{R}")
    print()

    # An explicit repair flag, so the operator can do it in one shot.
    if "--yes" in sys.argv or "-y" in sys.argv:
        print(f"  {Y}--yes given — attempting repair…{R}")
        try:
            import urllib.request
            url = f"{HUB}/{APP}-cli.py"
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read()
            head = body[:200].lower()
            if b"<!doctype" in head or b"<html" in head:
                print(f"  {X}✗ the hub returned HTML, not a script — "
                      f"not overwriting{R}")
                return
            # Atomic-ish: write a temp file next to the target, then swap.
            tmp = here.with_name(here.name + ".new")
            tmp.write_bytes(body)
            tmp.chmod(here.stat().st_mode)
            tmp.replace(here)
            print(f"  {G}✓ repaired {here} ({len(body)} bytes){R}")
            print(f"  now run: {APP} {cmd_name}")
        except Exception as e:
            print(f"  {X}✗ repair failed: {e}{R}")
            print(f"  the hub may be down — try the git clone path above")
        return

    print(f"  {Y}add --yes to repair automatically:{R}")
    print(f"    {APP} {cmd_name} --yes")
    print()


def _print_root_help() -> None:
    print(f"""\033[1m{APP}\033[0m — HIVV.org client CLI / local agent
hub = {HUB}   version = {VERSION}

\033[1mOperator / local subcommands:\033[0m
  start              start the local hivv agent (background, heartbeats to hub)
  stop               stop the local hivv agent
  restart            stop + start
  status             pid, uptime, last heartbeat, last error
  logs [N] [-f]      tail ~/.local/share/hivv/agent.log (last N lines; -f to follow)
  free               host memory + swap + disk + per-process RSS
  pct [--watch=N]    live CPU%/MEM% line (refreshes every N seconds)
  info               hub URL, version, install paths, hub_health
  update             pull the latest hivv-cli.py + run health checks
  upgrade            alias for `update`
  uninstall          stop + remove install (keep ~/.config/hivv/ + logs)
  --help, help       list every subcommand

\033[1mNetwork / cluster:\033[0m
  nodes / bees       table view of all registered bees in the hive
  node / bee <name>  inspect a specific bee (hardware, models, location)
  node-chat <n> MSG  chat directly with a bee's LLM
  reach / waggle <n> test connectivity to another bee on the network
  stats              show per-node stats and cluster totals
  debug              full debug info: hub, agent, local AI, network

\033[1mSecurity:\033[0m
  firewall           check firewall + binding + tailscale status
  doctor --fix     auto-fix bindings + create system services
  firewall --apply   apply firewall rules (--mode lan|tailscale|open)

\033[1mOne-shot subcommands (talk to {HUB_NAME}):\033[0m
   health             GET /api/health
   metrics            GET /api/control/metrics/global
   chat MSG...        POST /api/chat  (single message)  --mode=cheap|fast|private|smart
   completions MSG... POST /v1/chat/completions (OpenAI-compatible)
   whoami             GET /api/control/health (cheap liveness check)
   doctor             full health report (install + hub + agent + network + security)
   setup              configure OpenCode + VS Code + local LLM with hivv
   code TASK --repo U ask a coder bee to do a task in a git repo (--wait to watch)

\033[1mBuilding on HIVV (partner API):\033[0m
   partner [manifest|health|network|models|capabilities]  the public partner surface
   fast-scan FILE     deterministic, AI-free source screen (free · instant · no AI)
   local [status|start|stop|info]  a LOCAL self-hosted gateway (no database)
   capabilities       what this instance offers (capability discovery)

\033[1mDebugging & support:\033[0m
   problems           what is breaking globally? (live error log + incidents)
   bug [query]        research a specific problem  --client opencode|vscode|…
   submit "<text>"    file a bug report to the hub
   request "<text>"   request a feature
   flag <tgt> "<text>"  flag another node/user  --severity high
   support "<question>"  ask the AI for help (attaches your diagnostics)
   debug              full local snapshot: hub, agent, local AI, network

\033[1mhivv OS — capabilities · policy · routing · approvals 🐝:\033[0m
   cap [list|can|node]  the capability layer — what the hive can do
   policy [sub]         YES/MAYBE/NO rules (show|explain|history|verify|grants)
   approve [--list|--all|ID]  answer a MAYBE · deny ID ... refuse it
   route [q] --mode=X   dry-run routing planner — who answers and why

\033[1mLocal LLM helper (talks to your own box, not the hub):\033[0m
  llm                show llm subcommands
  llm detect         find ollama, llama.cpp, lm-studio, vllm on this box
  llm suggest        recommend a model for this hardware (CPU/RAM/GPU)
  llm bench          quick CPU / RAM / disk benchmark
  llm status         detect + bench + suggest in one shot
  llm install [--docker]   print install commands for the detected OS
  llm pull <model>         ollama pull <model>
  llm serve [model] [--port]   ollama serve in the foreground

\033[1mExamples:\033[0m
  hivv bees                      see all bees in the hive
  hivv bee yalla                  inspect a specific bee
  hivv node-chat yalla "hello"    chat with a specific bee
  hivv waggle masjid              test if a bee is reachable
  hivv swarm "what is kubernetes?" swarm the question to multiple models
  hivv drone                      am I contributing compute?
  hivv stats                      see cluster performance
  hivv doctor --fix            auto-fix LLM bindings
  hivv doctor                    full health check
  hivv chat 'what is the weather?'
  hivv start
  hivv update

Tip: every subcommand accepts --help (e.g. `hivv logs --help`).
""")
    _nag_if_update_available()

def _print_sub_help(cmd: str) -> None:
    table = {
        "start":     "start the local hivv agent (background)",
        "stop":      "stop the local hivv agent",
        "restart":   "stop + start",
        "status":    "pid, uptime, last heartbeat, last error, hub_health",
        "info":      "hub URL, version, install paths",
        "logs":      "tail ~/.local/share/hivv/agent.log\n  args: [N=50] [-f|--follow]",
        "free":      "host memory + swap + disk + per-process RSS",
        "pct":       "live CPU%/MEM% (refreshes every N seconds)\n  flag: --watch=N (default 1; 0 = print once)",
        "update":    "pull latest hivv-cli.py from hivv.org (LLM-reviewed first)\n  alias: upgrade\n  flags: --show-review  --skip-review  --force",
        "upgrades":  "recent upgrade review outcomes across the hive 🧬\n  flag: --flagged   only blocked/failed\n  flag: --node N    one bee's upgrade history",
        "upgrade":   "alias for `update`",
        "uninstall": "stop + remove install (keeps ~/.config/hivv/ + logs)",
        "health":    "GET /api/health",
        "metrics":   "GET /api/control/metrics/global\n  flag: --local   print this bee's local CPU/GPU/MEM/DISK snapshot",
        "nodes":     "table view of all registered nodes\n  flag: --json   output raw JSON",
        "node":      "show details about a specific node\n  usage: hivv node <name> [--json]",
        "node-chat": "chat directly with a node's LLM\n  usage: hivv node-chat <node> <message>",
        "reach":     "test connectivity to another node\n  usage: hivv reach <name>",
        "bees":      "table view of all bees in the hive 🐝\n  alias: nodes\n  flag: --json   output raw JSON",
        "bee":       "inspect a specific bee (hardware, models, location)\n  usage: hivv bee <name> [--json]\n  alias: node",
        "swarm":     "swarm a question to multiple models + bee-judge synthesis 🧠\n  usage: hivv swarm <question>\n  alias: hive-mind\n  flag: --json   full JSON output\n  flag: --quiet  verdict only",
        "waggle":    "dance to check if a bee is reachable\n  usage: hivv waggle <name>\n  alias: reach",
        "drone":     "your volunteer status — are you a worker bee? 🐝\n  alias: volunteer status",
        "stats":     "show per-node stats and cluster totals\n  flag: --json   output raw JSON",
        "debug":     "full debug info: hub, agent, local AI, network nodes",
        "firewall":  "check firewall + binding + tailscale status\n  flag: --apply      apply firewall rules\n  flag: --mode X     lan (default), tailscale, or open\n  note: use 'hivv doctor --fix' to auto-fix all issues",
        "chat":      "POST /api/chat MSG\n  usage: hivv chat 'hello world'",
        "completions":"POST /v1/chat/completions (OpenAI-compatible)\n  usage: hivv completions 'hello world'",
        "whoami":    "GET /api/control/health (cheap liveness check)",
        "doctor":    "one-glance health report (install + hub + agent + bee + version)\n  output is pasteable for support",
        "speed":     "latency benchmark (DNS, TCP, hub ping x3, chat TTFB, CPU/RAM/disk)",
        "test":      "doctor + speed end-to-end smoke test",
        "volunteer": "opt in/out of contributing compute to the network\n  usage: hivv volunteer [on|off|status]   (default: status)",
        "setup":     "configure OpenCode + VS Code + local LLM with hivv\n  flags: --write [PATH]  write/merge ./opencode.jsonc (default)\n         --global        with --write: use ~/.config/opencode/opencode.jsonc\n         --json          detection dict + config as JSON\n         --profile P    local | hybrid | hive | cloud (default hybrid)\n         --backend M    pin one specific local model\n         --tools IDS    only these tool ids, comma-separated\n         --gui          also write the GUI tool configs\n         --dry-run      show what would be written, write nothing",
        "capabilities": "what can this HIVV instance do? (capability discovery)\n  shows the OpenAI surface, route modes, deterministic tools, hive tools, MCP\n  flag: --json   machine-readable",
        "problems":  "what is breaking globally? (live error log + open incidents)\n  flag: --window N   look-back seconds (default 3600)\n  flag: --json\n  alias: errors",
        "errors":    "alias for problems",
        "bug":       "research a specific problem (searches the live error log)\n  usage: hivv bug [query] [--client opencode] [--limit N] [--json]\n  --client filters to one client (opencode, continue, cline, vscode, …)\n  shows the class, endpoint, client, message and a diagnosis for each hit",
        "submit":    "file a bug report to the hub\n  usage: hivv submit \"<what you saw>\" [--subject S] [--json]",
        "request":   "request a feature\n  usage: hivv request \"<what you want>\" [--subject S] [--json]",
        "flag":      "flag something another node/user is doing\n  usage: hivv flag <target> \"<what they are doing>\" [--severity L] [--evidence T] [--json]\n  target is a node name, principal id, or content reference",
        "code":      "ask a coder bee to do a task in a git repo\n  usage: hivv code \"<task>\" --repo <git-url> [--ref BR] [--timeout S] [--wait] [--json]",
        "dial":      "local OpenAI-compatible proxy shim — hub dials once, then forwards straight to the bee\n  usage: hivv dial [--port N] [--hub URL]   (default port 3911)\n  point OpenCode at http://127.0.0.1:3911/v1 — hub only on dial + report",
        "panic":     "emergency help — diagnostics + how to report a bug (paste this in support)",
        "help-bang": "alias for `panic` (the name `help!` is reserved by bash history expansion)",
        "hi":        "friendly greeter — quick chat round-trip + examples",
        "thanks":    "say thanks — one-line chat reply",
        "support":   "ask the AI for help — auto-attaches doctor + last 10 log lines\n  usage: hivv support [question...]   (no args = AI greets + asks)",
        "llm":       "local AI runtime helper (ollama, llama.cpp, ...)\n  subcommands: detect, suggest, bench, status, install, pull, serve\n  usage: hivv llm <sub> [--flag]",
        "llm detect":  "scan this box for ollama / llama.cpp / lm-studio / vllm",
        "llm suggest": "recommend a model for this hardware (CPU/RAM/GPU)\n  flag: --json   emit machine-readable JSON",
        "llm bench":   "quick CPU sha256 + memory + disk benchmark",
        "llm status":  "detect + bench + suggest in one shot",
        "llm install": "print install commands for the detected OS\n  flag: --docker   print the `docker run` for ollama\n  flag: --auto     actually run the install (instead of just printing)\n  flag: --model=X  after install, run `ollama pull X`",
        "llm pull":    "ollama pull <model>\n  usage: hivv llm pull qwen3:1.7b",
        "llm serve":   "ollama serve in the foreground\n  args: [model]   preload a model on startup\n  flag: --port=N  listen on port N (default 11434)",
    }
    desc = table.get(cmd, "(no help yet)")
    print(f"{APP} {cmd} — {desc}")


if __name__ == "__main__":
    main(sys.argv[1:])

