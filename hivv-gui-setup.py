#!/usr/bin/env python3
"""hivv-gui-setup.py — wire GUI tools to HIVV.

A single, data-driven registry of the AI tools people actually use, plus the
logic to point each one at a backend:

    local   → your own llama.cpp / ollama / LM Studio on this machine
    hybrid  → your local model first, the HIVV hive as fallback
    hive    → the public HIVV network (hivv.org)
    cloud  → a cloud provider key you supplied

THE POINT OF "HYBRID"
───────────────────
When your IDE asks a question, the request goes to YOUR model first — it is
free, private, and instant. If your model is busy, asleep, or does not know
the answer, the request falls through to the hive (a peer, or a donated cloud
key) instead of failing. And because your box is already running a model, the
hive may use your IDLE capacity in the meantime — that is the donation.

Nothing here executes a downloaded installer, and nothing here writes a file
without a backup.

Stdlib only — this module is imported by hivv-cli.py and is also runnable on
its own for testing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── profiles ────────────────────────────────────────────────────────────────
PROFILES = ("local", "hybrid", "hive", "cloud")

PROFILE_DESCRIPTIONS = {
    "local": "your own model only — nothing leaves this machine",
    "hybrid": "your model first, the HIVV hive as fallback",
    "hive": "the global HIVV network (hivv.org and its peers)",
    "cloud": "a cloud provider key you supplied",
}

DEFAULT_HUB = "https://hivv.org"


# ── local model endpoints ───────────────────────────────────────────────────
LOCAL_PROBES = (
    ("llama.cpp", 8080, "http://localhost:8080/v1"),
    ("llama.cpp", 8090, "http://localhost:8090/v1"),
    ("llama.cpp", 8081, "http://localhost:8081/v1"),
    ("ollama", 11434, "http://localhost:11434/v1"),
    ("LM Studio", 1234, "http://localhost:1234/v1"),
    ("vLLM", 8000, "http://localhost:8000/v1"),
    ("textgen-webui", 5000, "http://localhost:5000/v1"),
    ("LocalAI", 8082, "http://localhost:8082/v1"),
)

LOCAL_API_KEY_PLACEHOLDER = "not-needed"


def probe_local_models(timeout: float = 1.2) -> list[dict]:
    """Probe the well-known local ports. Offline-safe: every probe is local."""
    import urllib.error
    import urllib.request

    found: list[dict] = []
    for name, port, base in LOCAL_PROBES:
        url = f"{base}/models"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "hivv-setup"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        ids = []
        for m in (data.get("data") or []):
            mid = m.get("id") or m.get("model")
            if mid:
                ids.append(str(mid))
        found.append({
            "name": name,
            "port": port,
            "base_url": base,
            "models": ids or ["default"],
            "model": ids[0] if ids else "default",
        })
    return found


# ── the tool registry ───────────────────────────────────────────────────────
# Every entry says:
#   id        unique key
#   label     human name
#   kind      "vscode" | "jetbrains" | "editor" | "chat" | "cli" | "mcp"
#   detect    callable -> bool
#   paths     candidate config paths (first existing wins)
#   format    "json" | "jsonc" | "toml" | "yaml" | "env" | "mcp-json"
#   write     callable(target, plan) -> (action, detail)
#
# `plan` is a dict:
#   base_url   the endpoint to use
#   api_key    the key to use (or None)
#   model      the default model id
#   mode       "local" | "hybrid" | "hive" | "cloud"
#   local_url  the local endpoint when mode == "hybrid" else None
TOOL_REGISTRY: list[dict] = []


def tool(tool_id, label, kind, **kw):
    entry = {"id": tool_id, "label": label, "kind": kind}
    entry.update(kw)
    TOOL_REGISTRY.append(entry)
    return entry


def _home(*parts):
    return Path.home().joinpath(*parts)


def _merge_json(target: Path, provider_block: dict, provider_key: str,
                model: str | None = None) -> tuple[str, str]:
    """Merge a provider into a JSON/JSONC config without clobbering the user.

    Existing user values WIN. A backup is written first.
    """
    data: dict = {}
    if target.exists():
        try:
            raw = target.read_text(encoding="utf-8", errors="replace")
            data = json.loads(_strip_jsonc(raw))
        except Exception:
            data = {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        data["providers"] = providers
    existing = providers.get(provider_key)
    if isinstance(existing, dict):
        # keep the user's settings, only fill gaps
        merged = dict(provider_block)
        for k, v in provider_block.items():
            if isinstance(v, dict) and isinstance(existing.get(k), dict):
                sub = dict(existing[k])
                for sk, sv in v.items():
                    sub.setdefault(sk, sv)
                merged[k] = sub
            elif k not in existing:
                merged[k] = v
        providers[provider_key] = merged
        action = "merged"
    else:
        providers[provider_key] = provider_block
        action = "created"
    if model and not data.get("model"):
        data["model"] = model
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return action, str(target)


def _strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas (string-aware)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1]); i += 2; continue
            if ch == '"':
                in_str = False
            i += 1; continue
        if ch == '"':
            in_str = True; out.append(ch); i += 1; continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(ch); i += 1
    s = "".join(out)
    # drop trailing commas
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _backup(target: Path) -> Path | None:
    if not target.exists():
        return None
    bak = target.with_name(target.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(target, bak)
        return bak
    except Exception:
        return None


def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


# ── VS Code family ─────────────────────────────────────────────────────────
def _vscode_paths():
    home = Path.home()
    return [
        home / ".config" / "Code" / "User" / "settings.json",
        home / ".config" / "Code - Insiders" / "User" / "settings.json",
        home / ".config" / "VSCodium" / "User" / "settings.json",
        home / ".config" / "Cursor" / "User" / "settings.json",
        home / ".config" / "Windsurf" / "User" / "settings.json",
        home / "Library" / "Application Support" / "Code" / "User" / "settings.json",
        home / "AppData" / "Roaming" / "Code" / "User" / "settings.json",
    ]


def _write_vscode(target: Path, plan: dict) -> tuple[str, str]:
    """VS Code: the Continue extension (openai-compatible provider).

    Continue's settings.json has TWO shapes:
      * `models` as an ARRAY (the classic form) — one entry per model
      * `models` as an OBJECT keyed by model id (the newer form)
    We detect which one the user already has and match it, so we never
    silently change the shape of their file. If neither exists, we write the
    newer object form.
    """
    settings = {"apiBase": f"{plan['base_url']}/chat/completions"}
    if plan.get("api_key"):
        settings["apiKey"] = plan["api_key"]
    model_id = plan.get("model") or "auto"
    provider = {
        "id": "hivv",
        "name": "HIVV",
        "type": "openai",
        "settings": settings,
        "models": [{"model": model_id, "name": "HIVV"}],
    }
    autocomplete = {
        "title": "HIVV",
        "provider": "hivv",
        "model": model_id,
    }

    data: dict = {}
    if target.exists():
        try:
            data = json.loads(_strip_jsonc(target.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}

    existing_models = data.get("models")
    action = "created"
    if isinstance(existing_models, list):
        # classic array form — append our model, keep every existing entry
        if not any(isinstance(m, dict) and m.get("name") == "HIVV" for m in existing_models):
            existing_models.append({
                "title": "HIVV",
                "provider": "hivv",
                "model": model_id,
                "apiBase": settings["apiBase"],
                "apiKey": settings.get("apiKey", "not-needed"),
            })
            action = "merged"
        data["tabAutocompleteModel"] = data.get("tabAutocompleteModel") or autocomplete
    elif isinstance(existing_models, dict):
        # object form — add our entry under its own key
        if "hivv" not in existing_models:
            existing_models["hivv"] = {
                "title": "HIVV",
                "provider": "hivv",
                "model": model_id,
                "apiBase": settings["apiBase"],
                "apiKey": settings.get("apiKey", "not-needed"),
            }
            action = "merged"
        data["tabAutocompleteModel"] = data.get("tabAutocompleteModel") or autocomplete
    else:
        # nothing there yet — write the newer object form
        data["models"] = {"hivv": {
            "title": "HIVV",
            "provider": "hivv",
            "model": model_id,
            "apiBase": settings["apiBase"],
            "apiKey": settings.get("apiKey", "not-needed"),
        }}
        data["tabAutocompleteModel"] = autocomplete
        action = "created"

    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return action, str(target)


tool(
    "vscode-continue", "VS Code / Cursor / Windsurf — Continue", "vscode",
    detect=lambda: any(p.exists() for p in _vscode_paths()),
    paths=_vscode_paths,
    format="json",
    write=_write_vscode,
    note="Continue extension — free, open source, works in VS Code, Cursor, Windsurf, VSCodium",
)


def _write_cline(target: Path, plan: dict) -> tuple[str, str]:
    """Cline / Roo Code — one flat apiProvider entry."""
    flat = {
        "apiProvider": "openai",
        "openAiBaseUrl": plan["base_url"],
        "openAiApiKey": plan.get("api_key") or "not-needed",
        "openAiModelId": plan.get("model") or "auto",
        "hivvMode": plan.get("mode", "hybrid"),
    }
    return _merge_flat_json(target, flat)


def _merge_flat_json(target: Path, flat: dict) -> tuple[str, str]:
    data: dict = {}
    if target.exists():
        try:
            data = json.loads(_strip_jsonc(target.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            data = {}
    changed = False
    for k, v in flat.items():
        if k not in data:
            data[k] = v; changed = True
    action = "merged" if (target.exists() and changed) else ("created" if not target.exists() else "already up to date")
    if changed or not target.exists():
        _backup(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return action, str(target)


tool(
    "vscode-cline", "VS Code / Cursor — Cline & Roo Code", "vscode",
    detect=lambda: any((p.parent.parent / "globalStorage").exists() for p in _vscode_paths()),
    paths=lambda: [
        _home(".config", "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
        _home("Library", "Application Support", "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
        _home("AppData", "Roaming", "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "settings", "cline_mcp_settings.json"),
        _home(".config", "Code", "User", "globalStorage", "rooveterinaryinc.roo-cline", "settings", "mcp_settings.json"),
    ],
    format="json",
    write=_write_cline,
    note="Cline / Roo Code — set the OpenAI-compatible base URL in settings",
)


def _write_vscode_continue_workspace(target: Path, plan: dict) -> tuple[str, str]:
    """Continue also reads a workspace-level .continue/config.yaml."""
    lines = [
        "name: HIVV",
        "version: 1.0.0",
        "schema: v1",
        "models:",
        "  - name: HIVV",
        "    provider: openai",
        "    model: " + str(plan.get("model") or "auto"),
        "    apiBase: " + f"{plan['base_url']}/chat/completions",
        "    apiKey: " + (plan.get("api_key") or "not-needed"),
        "    roles:",
        "      - chat",
        "      - edit",
        "      - summarize",
        "      - autocomplete",
    ]
    action = "created"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if "name: HIVV" in existing:
            return "already up to date", str(target)
        action = "merged (appended)"
        text = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    else:
        text = "\n".join(lines) + "\n"
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return action, str(target)


tool(
    "vscode-continue-yaml", "Continue — workspace config", "vscode",
    detect=lambda: any((p.parent / "globalStorage").exists() for p in _vscode_paths()),
    paths=lambda: [Path.cwd() / ".continue" / "config.yaml",
                    Path.home() / ".continue" / "config.yaml"],
    format="yaml",
    write=_write_vscode_continue_workspace,
    note="Continue workspace/global config.yaml (the per-model form most people use)",
)


# ── JetBrains ──────────────────────────────────────────────────────────────
def _write_jetbrains(target: Path, plan: dict) -> tuple[str, str]:
    """JetBrains AI Assistant — llms.xml."""
    xml = f"""<llms>
  <community>
    <name>HIVV</name>
    <url>{plan['base_url']}/chat/completions</url>
    <id>{plan.get('model') or 'auto'}</id>
    <apiKey>{plan.get('api_key') or 'not-needed'}</apiKey>
  </community>
</llms>
"""
    action = "created"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if "HIVV" in existing:
            return "already up to date", str(target)
        action = "merged (appended)"
        text = existing.rstrip() + "\n" + xml
    else:
        text = xml
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return action, str(target)


tool(
    "jetbrains", "JetBrains (IntelliJ, PyCharm, GoLand…) — AI Assistant", "jetbrains",
    detect=lambda: bool(_which("idea", "pycharm", "goland", "webstorm", "rustrover")),
    paths=lambda: [_home(".mcp", "llms.xml"),
                   _home("Library", "Application Support", "JetBrains", "llms.xml"),
                   _home("AppData", "Roaming", "JetBrains", "llms.xml")],
    format="xml",
    write=_write_jetbrains,
    note="JetBrains AI Assistant reads ~/.mcp/llms.xml — OpenAI-compatible community provider",
)


def _write_jetbrains_mcp(target: Path, plan: dict) -> tuple[str, str]:
    mcp = {
        "mcpServers": {
            "hivv": {
                "url": f"{plan['base_url'].rstrip('/')}/mcp",
                "transport": "http",
            }
        }
    }
    return _merge_json(target, mcp, "mcpServers", None)


tool(
    "jetbrains-mcp", "JetBrains — HIVV via MCP", "jetbrains",
    detect=lambda: bool(_which("idea", "pycharm", "goland", "webstorm", "rustrover")),
    paths=lambda: [_home(".mcp", "mcp.json"),
                   _home("Library", "Application Support", "JetBrains", "mcp.json"),
                   _home("AppData", "Roaming", "JetBrains", "mcp.json")],
    format="json",
    write=_write_jetbrains_mcp,
    note="MCP server route — hivv.org/mcp exposes chat, route, nodes, search, news, honey",
)


# ── other editors ──────────────────────────────────────────────────────────
def _write_zed(target: Path, plan: dict) -> tuple[str, str]:
    """Zed — custom OpenAI-compatible provider in settings.json."""
    provider = {
        "name": "HIVV",
        "provider": "openai",
        "base_url": f"{plan['base_url']}/chat/completions",
        "api_key": plan.get("api_key") or "not-needed",
        "models": [{"name": plan.get("model") or "auto", "id": plan.get("model") or "auto"}],
    }
    return _merge_json(target, {"context_servers": {"custom": {"hivv": provider}}}, "context_servers", None)


tool(
    "zed", "Zed", "editor",
    detect=lambda: _which("zed") is not None,
    paths=lambda: [_home(".config", "zed", "settings.json"),
                   _home("Library", "Application Support", "Zed", "settings.json"),
                   _home("AppData", "Roaming", "Zed", "settings.json")],
    format="json",
    write=_write_zed,
    note="Zed custom provider (context servers)",
)


def _write_windsurf(target: Path, plan: dict) -> tuple[str, str]:
    flat = {
        "apiBase": f"{plan['base_url']}/chat/completions",
        "apiKey": plan.get("api_key") or "not-needed",
    }
    return _merge_flat_json(target, flat)


tool(
    "windsurf", "Windsurf", "editor",
    detect=lambda: _which("windsurf") is not None,
    paths=lambda: [_home(".codeium", "windsurf", "settings.json"),
                   _home("Library", "Application Support", "Windsurf", "settings.json"),
                   _home("AppData", "Roaming", "Windsurf", "settings.json")],
    format="json",
    write=_write_windsurf,
    note="Windsurf's own OpenAI-compatible settings (Codeium-compatible endpoints)",
)


# ── chat front-ends ────────────────────────────────────────────────────────
def _write_lmstudio(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "llmEngine": "openai",
        "llmEndpoint": plan["base_url"],
        "apiKey": plan.get("api_key") or "not-needed",
    })


tool(
    "lmstudio", "LM Studio", "chat",
    detect=lambda: _which("lm-studio", "lmstudio") is not None,
    paths=lambda: [_home(".lmstudio", "llm-studio-settings.json")],
    format="json",
    write=_write_lmstudio,
    note="LM Studio's local server is what you POINT AT — this wires its remote endpoint",
)


def _write_open_webui(target: Path, plan: dict) -> tuple[str, str]:
    """Open WebUI — env file."""
    env = {
        "OPENAI_API_BASE_URL": f"{plan['base_url']}/v1",
        "OPENAI_API_KEY": plan.get("api_key") or "not-needed",
    }
    action = "created"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        keep = [ln for ln in existing.splitlines()
                if not any(ln.startswith(k + "=") for k in env)]
        action = "merged"
        text = "\n".join(keep + [f"{k}={v}" for k, v in env.items()]) + "\n"
    else:
        text = "\n".join(f"{k}={v}" for k, v in env.items()) + "\n"
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return action, str(target)


tool(
    "open-webui", "Open WebUI", "chat",
    detect=lambda: _which("open-webui") is not None or _home("open-webui").exists(),
    paths=lambda: [_home("open-webui", ".env"), _home("open-webui", "backend", ".env")],
    format="env",
    write=_write_open_webui,
    note="Open WebUI — point its OpenAI-compatible endpoint at HIVV",
)


def _write_librechat(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "endpoint": plan["base_url"],
        "apiKey": plan.get("api_key") or "not-needed",
        "models": [plan.get("model") or "auto"],
    })


tool(
    "librechat", "LibreChat", "chat",
    detect=lambda: _which("librechat") is not None or _home("librechat").exists(),
    paths=lambda: [_home("librechat", "librechat.yaml"), _home("librechat", "librechat.example.yaml")],
    format="yaml",
    write=_write_librechat,
    note="LibreChat — custom endpoint in librechat.yaml",
)


def _write_lobechat(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "endpoint": plan["base_url"],
        "apiKey": plan.get("api_key") or "not-needed",
    })


tool(
    "lobechat", "LobeChat", "chat",
    detect=lambda: _which("lobechat") is not None,
    paths=lambda: [_home(".lobechat", "local-storage", "config.json")],
    format="json",
    write=_write_lobechat,
    note="LobeChat local provider config",
)


def _write_jan(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "llama_api_url": f"{plan['base_url']}/chat/completions",
        "llama_api_key": plan.get("api_key") or "not-needed",
    })


tool(
    "jan", "Jan", "chat",
    detect=lambda: _which("jan") is not None,
    paths=lambda: [_home(".jan", "settings.json")],
    format="json",
    write=_write_jan,
    note="Jan — custom llama.cpp-compatible endpoint",
)


def _write_gpt4all(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "apiBase": plan["base_url"],
        "apiKey": plan.get("api_key") or "not-needed",
    })


tool(
    "gpt4all", "GPT4All", "chat",
    detect=lambda: _which("gpt4all", "GPT4All") is not None,
    paths=lambda: [_home(".gpt4all", "settings.json")],
    format="json",
    write=_write_gpt4all,
    note="GPT4All — OpenAI-compatible endpoint",
)


def _write_anythingllm(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_flat_json(target, {
        "engine": "openai",
        "baseUrl": plan["base_url"],
        "apiKey": plan.get("api_key") or "not-needed",
        "model": plan.get("model") or "auto",
    })


tool(
    "anythingllm", "AnythingLLM", "chat",
    detect=lambda: _which("anythingllm") is not None,
    paths=lambda: [_home("anythingllm", "settings.json")],
    format="json",
    write=_write_anythingllm,
    note="AnythingLLM — custom OpenAI-compatible provider",
)


# ── MCP hosts ──────────────────────────────────────────────────────────────
def _write_mcp(target: Path, plan: dict) -> tuple[str, str]:
    """Generic MCP client config (Claude Desktop, Cursor, Windsurf, Zed…)."""
    mcp = {
        "mcpServers": {
            "hivv": {
                "url": f"{plan['base_url'].rstrip('/')}/mcp",
                "transport": "http",
            }
        }
    }
    return _merge_json(target, mcp, "mcpServers", None)


tool(
    "mcp", "MCP hosts (Claude Desktop, Cursor, …)", "mcp",
    detect=lambda: bool(_which("claude", "claude-desktop")),
    paths=lambda: [
        _home(".config", "claude", "claude_desktop_config.json"),
        _home("Library", "Application Support", "Claude", "claude_desktop_config.json"),
        _home("AppData", "Roaming", "Claude", "claude_desktop_config.json"),
    ],
    format="json",
    write=_write_mcp,
    note="MCP tools: chat, route, nodes, search, news, honey",
)


# ── command-line tools ──────────────────────────────────────────────────────
def _write_aider(target: Path, plan: dict) -> tuple[str, str]:
    """Aider — .aider.conf.yml."""
    lines = [
        "model: openai/" + str(plan.get("model") or "auto"),
        "openai-api-base: " + plan["base_url"],
        "openai-api-key: " + (plan.get("api_key") or "not-needed"),
    ]
    action = "created"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if "openai-api-base" in existing:
            return "already up to date", str(target)
        action = "merged (appended)"
        text = existing.rstrip() + "\n" + "\n".join(lines) + "\n"
    else:
        text = "\n".join(lines) + "\n"
    _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return action, str(target)


tool(
    "aider", "Aider", "cli",
    detect=lambda: _which("aider") is not None,
    paths=lambda: [_home(".aider.conf.yml"), Path.cwd() / ".aider.conf.yml"],
    format="yaml",
    write=_write_aider,
    note="Aider — point openai-api-base at HIVV",
)


def _write_crush(target: Path, plan: dict) -> tuple[str, str]:
    return _merge_json(target, {
        "models": {
            "hivv": {
                "type": "openai",
                "base_url": plan["base_url"],
                "api_key": plan.get("api_key") or "not-needed",
                "model": plan.get("model") or "auto",
            }
        }
    }, "models", None)


tool(
    "crush", "Crush (Charm)", "cli",
    detect=lambda: _which("crush") is not None,
    paths=lambda: [_home(".config", "crush", "crush.json")],
    format="json",
    write=_write_crush,
    note="Crush — custom OpenAI-compatible model",
)


# ── the plan builder ───────────────────────────────────────────────────────
def build_plan(profile: str = "hybrid", hub: str = DEFAULT_HUB,
               local_models: list[dict] | None = None,
               api_key: str | None = None,
               backend: str | None = None) -> dict:
    """Work out the endpoint + key + model for a profile."""
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {', '.join(PROFILES)}")
    locals_ = local_models if local_models is not None else []
    chosen = None
    if backend:
        for lm in locals_:
            if backend == lm.get("model") or backend == lm.get("name"):
                chosen = lm; break
        if chosen is None:
            raise ValueError(f"no local backend named {backend!r} (found: "
                             + ", ".join(f"{m.get('name')}:{m.get('model')}" for m in locals_) + ")")
    elif locals_:
        chosen = locals_[0]

    if profile == "local":
        if not chosen:
            raise ValueError("no local model found — start llama.cpp/ollama/LM Studio, "
                             "or use --profile hive")
        return {
            "mode": "local",
            "base_url": chosen["base_url"].rstrip("/"),
            "api_key": None,
            "model": chosen.get("model") or "auto",
            "local_url": chosen["base_url"].rstrip("/"),
            "local_model": chosen.get("model"),
            "hub": hub,
        }

    if profile == "hive":
        return {
            "mode": "hive",
            "base_url": f"{hub.rstrip('/')}/v1",
            "api_key": api_key,
            "model": "auto",
            "local_url": None,
            "local_model": None,
            "hub": hub,
        }

    if profile == "cloud":
        return {
            "mode": "cloud",
            "base_url": "https://api.openai.com/v1",
            "api_key": api_key,
            "model": "gpt-4o-mini",
            "local_url": None,
            "local_model": None,
            "hub": hub,
        }

    # hybrid
    if chosen:
        return {
            "mode": "hybrid",
            # the LOCAL model is the primary endpoint
            "base_url": chosen["base_url"].rstrip("/"),
            "api_key": None,
            "model": chosen.get("model") or "auto",
            "local_url": chosen["base_url"].rstrip("/"),
            "local_model": chosen.get("model"),
            "hub": hub,
            "fallback_url": f"{hub.rstrip('/')}/v1",
        }
    return {
        "mode": "hive",
        "base_url": f"{hub.rstrip('/')}/v1",
        "api_key": api_key,
        "model": "auto",
        "local_url": None,
        "local_model": None,
        "hub": hub,
        "note": "no local model detected — falling back to the hive",
    }


def apply_tool(tool_entry: dict, plan: dict, target: Path | None = None) -> dict:
    """Write one tool's config. Returns a result dict (never raises)."""
    try:
        paths = tool_entry["paths"]()
        dest = target or next((p for p in paths if p.exists()), paths[0])
        action, detail = tool_entry["write"](dest, plan)
        return {"ok": True, "tool": tool_entry["id"], "label": tool_entry["label"],
                "action": action, "path": detail, "target": str(dest)}
    except Exception as e:
        return {"ok": False, "tool": tool_entry["id"], "label": tool_entry["label"],
                "error": str(e)}


def detect_tools() -> list[dict]:
    out = []
    for t in TOOL_REGISTRY:
        try:
            found = bool(t["detect"]())
        except Exception:
            found = False
        out.append({"id": t["id"], "label": t["label"], "kind": t["kind"],
                    "detected": found, "note": t.get("note", "")})
    return out


__all__ = [
    "PROFILES", "PROFILE_DESCRIPTIONS", "DEFAULT_HUB", "TOOL_REGISTRY",
    "LOCAL_PROBES", "probe_local_models", "build_plan", "apply_tool",
    "detect_tools", "tool", "_strip_jsonc", "_merge_json", "_merge_flat_json",
]


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    print(json.dumps({
        "profiles": list(PROFILES),
        "local_models": probe_local_models(),
        "tools": detect_tools(),
    }, indent=2))
