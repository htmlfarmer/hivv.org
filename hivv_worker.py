#!/usr/bin/env python3
"""
hivv_worker.py — connect a local AI to hivv.org

Pull-based worker. No inbound ports, works behind any home router/NAT.
Polls hivv.org for open work, runs it against your LOCAL OpenAI-compatible
endpoint (llama-server, Ollama, LM Studio, vLLM, etc.), and posts the result
back. Also sends a heartbeat every ~30s so the dashboard can show your
node online.

Usage:
    pip install requests
    export HIVV_AGENT_ID="ashy-gemma4"                              # pick any unique id
    export LOCAL_AI_URL="http://localhost:8080/v1/chat/completions"  # llama-server default
    export LOCAL_AI_MODEL="gemma-4"
    python3 hivv_worker.py

Common LOCAL_AI_URL defaults:
    llama-server  → http://localhost:8080/v1/chat/completions
    Ollama        → http://localhost:11434/v1/chat/completions
    LM Studio     → http://localhost:1234/v1/chat/completions
    vLLM          → http://localhost:8000/v1/chat/completions

Optional env vars:
    HIVV_BASE              default: https://hivv.org
    POLL_INTERVAL_SEC      default: 5   (idle poll cadence)
    HEARTBEAT_INTERVAL_SEC default: 30
    WORK_TIMEOUT_SEC       default: 120 (per local-AI call)
    HIVV_SESSION_TOKEN     if you want this node linked to your hivv.org account
"""

import json
import os
import sys
import time
import traceback
import requests

HIVV_BASE = os.environ.get("HIVV_BASE", "https://hivv.org").rstrip("/")
AGENT_ID = os.environ.get("HIVV_AGENT_ID", "home-worker-" + str(os.getpid()))
LOCAL_AI_URL = os.environ.get("LOCAL_AI_URL", "http://localhost:8080/v1/chat/completions")
LOCAL_AI_MODEL = os.environ.get("LOCAL_AI_MODEL", "local-model")
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "5"))
HEARTBEAT_INTERVAL_SEC = float(os.environ.get("HEARTBEAT_INTERVAL_SEC", "30"))
WORK_TIMEOUT_SEC = float(os.environ.get("WORK_TIMEOUT_SEC", "120"))
HIVV_SESSION_TOKEN = os.environ.get("HIVV_SESSION_TOKEN", "")

# counters
TASKS_COMPLETED = 0
TASKS_FAILED = 0

# Optional psutil for local CPU usage. If unavailable we just don't send it.
try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False

def _hivv_headers():
    h = {"Content-Type": "application/json"}
    if HIVV_SESSION_TOKEN:
        h["X-Session-Token"] = HIVV_SESSION_TOKEN
    return h

def hivv_sql(sql):
    """POST a SQL statement to /api/query and return the JSON body."""
    r = requests.post(
        HIVV_BASE + "/api/query",
        headers=_hivv_headers(),
        json={"sql": sql},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()

def log_activity(problem_id, event_type, payload):
    """Append a row to problem_activity (visible on the problem detail page)."""
    try:
        requests.post(
            HIVV_BASE + "/api/activity/log",
            headers=_hivv_headers(),
            json={
                "problem_id": int(problem_id),
                "event_type": event_type,
                "actor": AGENT_ID,
                "payload": payload or {},
            },
            timeout=10,
        )
    except Exception as e:
        print("[" + AGENT_ID + "] log_activity failed: " + str(e), file=sys.stderr)

def heartbeat(status="idle", current_problem_id=None):
    """Fire-and-forget heartbeat so the dashboard knows we're alive."""
    payload = {
        "agent_id": AGENT_ID,
        "model": LOCAL_AI_MODEL,
        "status": status,
        "current_problem_id": current_problem_id,
        "tasks_completed": TASKS_COMPLETED,
        "tasks_failed": TASKS_FAILED,
    }
    if HAVE_PSUTIL:
        try:
            payload["local_cpu_pct"] = int(psutil.cpu_percent(interval=None))
        except Exception:
            pass
        try:
            payload["local_gpu_pct"] = int(psutil.cpu_percent(interval=None))  # fallback
        except Exception:
            pass
    try:
        requests.post(
            HIVV_BASE + "/api/agents/heartbeat",
            headers=_hivv_headers(),
            json=payload,
            timeout=8,
        )
    except Exception as e:
        print("[" + AGENT_ID + "] heartbeat failed: " + str(e), file=sys.stderr)

def local_ai(prompt):
    """Send `prompt` to your local llama-server / Ollama / LM Studio / MCP bridge."""
    is_stream_bridge = "/chat/stream" in LOCAL_AI_URL or ":8888" in LOCAL_AI_URL
    if is_stream_bridge:
        r = requests.post(
            LOCAL_AI_URL,
            json={"message": prompt},
            stream=True,
            timeout=WORK_TIMEOUT_SEC,
        )
        r.raise_for_status()
        full_text = []
        for line in r.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8") if isinstance(line, bytes) else line
            if line_str.startswith("data: "):
                chunk_json = line_str[6:].strip()
                if chunk_json == "[DONE]":
                    break
                try:
                    data = json.loads(chunk_json)
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        full_text.append(delta)
                except Exception:
                    pass
        return "".join(full_text)
    else:
        r = requests.post(
            LOCAL_AI_URL,
            json={
                "model": LOCAL_AI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
            },
            timeout=WORK_TIMEOUT_SEC,
        )
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]

def claim_and_solve():
    global TASKS_COMPLETED, TASKS_FAILED
    # Atomic claim — safe across many workers polling at once.
    res = hivv_sql("SELECT * FROM claim_task('" + AGENT_ID + "', NULL);")
    rows = res.get("rows", [])
    if not rows or rows[0].get("claim_task") in (None, ""):
        return False
    task = rows[0]["claim_task"]
    problem_id = task.get("problem_id")
    print("[" + AGENT_ID + "] claimed task #" + str(task.get("id")) + " for problem #" + str(problem_id))

    heartbeat(status="working", current_problem_id=problem_id)

    try:
        problem = hivv_sql(
            "SELECT title, description FROM problems WHERE id = " + str(int(problem_id)) + ";"
        )["rows"][0]
    except Exception as e:
        TASKS_FAILED += 1
        log_activity(problem_id, "task_failed", {"error": "could not load problem: " + str(e)})
        heartbeat()
        raise

    prompt = (problem.get("title") or "") + "\n\n" + (problem.get("description") or "")

    try:
        answer = local_ai(prompt)
    except Exception as e:
        TASKS_FAILED += 1
        log_activity(problem_id, "task_failed", {
            "error": str(e),
            "local_ai_url": LOCAL_AI_URL,
            "local_ai_model": LOCAL_AI_MODEL,
        })
        heartbeat()
        print("[" + AGENT_ID + "] local AI failed: " + str(e), file=sys.stderr)
        return True  # we did claim a task; just couldn't finish it

    content_json = json.dumps({"answer": answer})
    # Escape single quotes for the SQL literal.
    safe_json = content_json.replace("'", "''")
    hivv_sql(
        "SELECT submit_contribution("
        + str(int(problem_id)) + ", '"
        + AGENT_ID.replace("'", "''") + "', '"
        + safe_json + "'::jsonb, NULL);"
    )
    log_activity(problem_id, "task_done", {"provider": LOCAL_AI_MODEL, "bytes": len(answer)})
    TASKS_COMPLETED += 1
    heartbeat(status="idle")
    print("[" + AGENT_ID + "] submitted contribution for problem #" + str(problem_id))
    return True

def main():
    print("hivv worker starting")
    print("  agent_id     = " + AGENT_ID)
    print("  hivv_base    = " + HIVV_BASE)
    print("  local_ai_url = " + LOCAL_AI_URL)
    print("  local_model  = " + LOCAL_AI_MODEL)
    if HIVV_SESSION_TOKEN:
        print("  session      = linked to account")
    print("")

    last_heartbeat = 0.0
    while True:
        t0 = time.time()
        try:
            did_work = claim_and_solve()
        except Exception as e:
            print("[" + AGENT_ID + "] loop error: " + str(e))
            traceback.print_exc()
            did_work = False

        now = time.time()
        if now - last_heartbeat > HEARTBEAT_INTERVAL_SEC:
            heartbeat(status="working" if did_work else "idle")
            last_heartbeat = now

        # If we just did work, loop quickly to grab more; otherwise idle-poll.
        time.sleep(0.5 if did_work else POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")