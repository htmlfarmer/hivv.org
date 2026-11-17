#!/usr/bin/env python3
"""
hivv-tools — lightweight local tool server for the hivv network.
Runs on port 18789 by default. Provides:

  GET /time                    — current unix timestamp + ISO + human
  GET /time?tz=America/New_York — current time in timezone
  GET /calculate?expr=2%2B2   — evaluate math expression
  GET /fetch?url=https://...   — HTTP GET any public URL
  GET /search?q=hello         — Wikipedia search (encyclopaedic)
  GET /sysinfo                — CPU/RAM/disk snapshot
  GET /tools                  — list all available tools
  GET /health                 — server health

All responses are JSON. The server binds to loopback only and has no auth —
it is reachable from the gateway and the CLI on this machine, and nowhere else.
"""
import json, os, sys, re, socket, urllib.parse, datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

PORT = 18789
# Bind to loopback by default. This server has no authentication and can
# fetch arbitrary URLs, so it must not be reachable from outside the box.
# The gateway proxies tool calls to it. Override with HIVV_TOOLS_HOST=0.0.0.0
# only when the node's tools must be callable from another host.
HOST = os.environ.get("HIVV_TOOLS_HOST", "127.0.0.1")
APP = "hivv-tools"
VERSION = "0.1.0"

TOOL_PORT_FILE = os.path.expanduser("~/.config/hivv/tool-port")
PID_FILE = os.path.expanduser("~/.local/share/hivv/tools.pid")

# ── Tool implementations ────────────────────────────────────────────────────────

def tool_time(tz=None):
    """Return current time in multiple formats."""
    now = datetime.datetime.now()
    result = {
        "unix": int(now.timestamp()),
        "iso": now.isoformat(),
        "human": now.strftime("%Y-%m-%d %H:%M:%S"),
        "utc": datetime.datetime.utcnow().isoformat() + "Z",
    }
    if tz:
        try:
            import zoneinfo
            tz_obj = zoneinfo.ZoneInfo(tz)
            local = now.astimezone(tz_obj)
            result["timezone"] = tz
            result["local"] = local.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            result["error"] = f"unknown timezone: {tz}"
    return result

def tool_calculate(expr):
    """Safely evaluate a math expression. Only allows numbers, operators, parens."""
    # Strip anything that isn't a safe math character
    cleaned = re.sub(r"[^0-9+\-*/.()% ]", "", expr)
    try:
        # Use eval with restricted globals
        result = eval(cleaned, {"__builtins__": {}, "abs": abs, "round": round, "min": min, "max": max, "pow": pow}, {})
        return {"expression": expr, "result": result, "safe_result": float(result) if result is not None else 0}
    except Exception as e:
        return {"expression": expr, "error": str(e)}

def _is_private_ip(ip):
    """True when an IP literal points inside a private/internal network."""
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # not an IP literal at all → treat as unsafe
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified
            or (hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped is not None
                and addr.ipv4_mapped.is_private))


def _host_is_internal(hostname):
    """Resolve a hostname and refuse it if it points anywhere internal.

    A plain string-prefix check is not enough: a public name can resolve to
    127.0.0.1, which is exactly the case that turns a fetch tool into an SSRF
    primitive against the services running on this box.
    """
    import socket
    if not hostname:
        return True
    hostname = hostname.lower().strip('[]')
    if hostname == 'localhost' or hostname.endswith(('.internal', '.local', '.localhost')):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return True  # unresolvable → do not fetch
    for info in infos:
        addr = info[4][0]
        if _is_private_ip(addr):
            return True
    return False


def tool_fetch(url, timeout=10):
    """HTTP GET any URL. Returns status + body snippet. Blocks internal/private hosts."""
    try:
        import urllib.request, urllib.parse
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return {"url": url, "error": "only http/https allowed"}
        hostname = parsed.hostname or ''
        if _host_is_internal(hostname):
            return {"url": url, "error": "internal hosts not allowed"}
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return {
                "url": url,
                "status": r.status,
                "headers": dict(r.headers),
                "body": body[:2000],
                "truncated": len(body) > 2000
            }
    except Exception as e:
        return {"url": url, "error": str(e)}

def tool_search(query, limit=5):
    """Wikipedia API search — returns titles + snippets + URLs."""
    try:
        import urllib.request, urllib.parse, re, json
        q = urllib.parse.quote(query)
        url = f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={q}&srlimit={limit}&format=json&origin=*"
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        results = data.get("query", {}).get("search", [])
        out = []
        for r2 in results:
            title = r2.get("title", "?")
            snippet = re.sub(r'<[^>]+>', '', r2.get("snippet", ""))
            page_url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            out.append({"title": title, "url": page_url, "snippet": snippet})
        return {"query": query, "results": out, "count": len(out)}
    except Exception as e:
        return {"query": query, "error": str(e)}

def tool_news(query, limit=8):
    """Google News RSS — returns top headlines for a query."""
    try:
        import urllib.request, urllib.parse, re
        q = urllib.parse.quote(query)
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        req = urllib.request.Request(url, headers={"User-Agent": f"{APP}/{VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", errors="replace")
        items = re.findall(r'<item[^>]*>([\s\S]*?)</item>', xml, re.I)
        results = []
        for item in items[:limit]:
            title_match = re.search(r'<title>(.*?)</title>', item, re.I)
            link_match = re.search(r'<link>(.*?)</link>', item, re.I)
            pub_match = re.search(r'<pubDate>(.*?)</pubDate>', item, re.I)
            desc_match = re.search(r'<description>(.*?)</description>', item, re.I)
            title = re.sub(r'<!\[CDATA\[|\]\]>', '', title_match.group(1)).strip() if title_match else ''
            link = re.sub(r'<!\[CDATA\[|\]\]>', '', link_match.group(1)).strip() if link_match else ''
            pub = pub_match.group(1).strip() if pub_match else ''
            desc = re.sub(r'<[^>]+>', '', re.sub(r'<!\[CDATA\[|\]\]>', '', desc_match.group(1) if desc_match else '')).strip()
            if title and title != 'Google News':
                results.append({"title": title, "url": link, "date": pub, "snippet": desc[:200]})
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        return {"query": query, "error": str(e)}

def tool_sysinfo():
    """Snapshot of local system resources."""
    m = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable"):
                    m["mem_avail_kb"] = int(line.split()[1])
                if line.startswith("MemTotal"):
                    m["mem_total_kb"] = int(line.split()[1])
        if m.get("mem_total_kb"):
            m["mem_pct"] = round((1 - m["mem_avail_kb"] / m["mem_total_kb"]) * 100, 1)
    except: pass
    try:
        import os
        m["load"] = os.getloadavg() if hasattr(os, "getloadavg") else None
        m["cpu_count"] = os.cpu_count()
    except: pass
    try:
        st = os.statvfs("/")
        m["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        m["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
    except: pass
    try:
        with open("/proc/uptime") as f:
            m["uptime_s"] = int(float(f.read().split()[0]))
    except: pass
    return m

# ── HTTP Server ───────────────────────────────────────────────────────────────

class ToolHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")

        if path == "/health":
            return self.send_json({"status": "ok", "app": APP, "version": VERSION})

        if path == "/tools":
            return self.send_json({
                "tools": ["time", "calculate", "fetch", "search", "sysinfo"],
                "endpoint": f"http://localhost:{PORT}",
                "app": APP, "version": VERSION
            })

        if path == "/time":
            tz = qs.get("tz", [None])[0]
            return self.send_json(tool_time(tz))

        if path == "/calculate":
            expr = qs.get("expr", [""])[0]
            if not expr:
                return self.send_json({"error": "expr parameter required"}, 400)
            return self.send_json(tool_calculate(expr))

        if path == "/fetch":
            url = qs.get("url", [""])[0]
            if not url:
                return self.send_json({"error": "url parameter required"}, 400)
            return self.send_json(tool_fetch(url))

        if path == "/search":
            q = qs.get("q", qs.get("query", [""]))[0]
            if not q:
                return self.send_json({"error": "q parameter required"}, 400)
            return self.send_json(tool_search(q))

        if path == "/news":
            q = qs.get("q", qs.get("query", [""]))[0]
            if not q:
                return self.send_json({"error": "q parameter required"}, 400)
            return self.send_json(tool_news(q))

        if path == "/sysinfo":
            return self.send_json(tool_sysinfo())

        # Default: list endpoints
        self.send_json({
            "app": APP, "version": VERSION,
            "endpoints": ["/health", "/tools", "/time", "/time?tz=America/New_York",
                          "/calculate?expr=2%2B2", "/fetch?url=https://example.com",
                          "/search?q=hello", "/news?q=San+Francisco", "/sysinfo"]
        })

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

# ── Server lifecycle ──────────────────────────────────────────────────────────

def write_pid():
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except: pass

def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except: return None

def is_running():
    pid = read_pid()
    if pid and pid != os.getpid():
        try:
            os.kill(pid, 0)
            return pid
        except OSError:
            pass
    return None

def start_server(port=PORT):
    """Start the tool server in this process (foreground)."""
    existing = is_running()
    if existing:
        print(f"[{APP}] already running (PID {existing})")
        return False
    try:
        write_pid()
        server = HTTPServer((HOST, port), ToolHandler)
        print(f"[{APP}] listening on http://{HOST}:{port}")
        print(f"[{APP}] tools: time, calculate, fetch, search, sysinfo")
        print(f"[{APP}] press Ctrl+C to stop")
        server.serve_forever()
    except OSError as e:
        if e.errno == 98:  # Address in use
            print(f"[{APP}] port {port} in use — is another instance running?")
        else:
            print(f"[{APP}] failed to start: {e}")
        return False
    return True

def stop_server():
    pid = read_pid()
    if not pid:
        print(f"[{APP}] not running (no PID file)")
        return
    try:
        os.kill(pid, 15)
        print(f"[{APP}] stopped (PID {pid})")
    except OSError:
        print(f"[{APP}] process {pid} not found — removing stale PID")
    try:
        os.remove(PID_FILE)
    except: pass

def restart_server(port=PORT):
    stop_server()
    import time; time.sleep(1)
    start_server(port)

# ── CLI ──────────────────────────────────────────────────────────────────────

def cli():
    import argparse
    p = argparse.ArgumentParser(prog=f"{APP}", description="Local tool server for hivv network")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("start", help="start the tool server").add_argument("--port", type=int, default=PORT)
    sub.add_parser("stop", help="stop the tool server")
    sub.add_parser("restart", help="stop + start").add_argument("--port", type=int, default=PORT)
    sub.add_parser("status", help="check if running")
    sub.add_parser("test", help="test all tools locally")

    args = p.parse_args()

    if args.cmd == "start" or args.cmd is None:
        if is_running():
            pid = is_running()
            print(f"[{APP}] already running on port {PORT} (PID {pid})")
            print(f"[{APP}] endpoints: http://localhost:{PORT}/tools")
            return
        start_server(args.port if args.cmd == "start" else PORT)
    elif args.cmd == "stop":
        stop_server()
    elif args.cmd == "restart":
        restart_server(args.port if hasattr(args, "port") else PORT)
    elif args.cmd == "status":
        pid = is_running()
        if pid:
            print(f"[{APP}] running (PID {pid}) on port {PORT}")
            # Quick health check
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://localhost:{PORT}/health", timeout=3) as r:
                    data = json.loads(r.read())
                    print(f"[{APP}] health: {data}")
            except Exception as e:
                print(f"[{APP}] health check failed: {e}")
        else:
            print(f"[{APP}] not running")
    elif args.cmd == "test":
        import urllib.request
        base = f"http://localhost:{PORT}"
        tests = [
            ("/time", "time"),
            ("/calculate?expr=47*389%2B1024", "calculate"),
            ("/sysinfo", "sysinfo"),
        ]
        for path, name in tests:
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    data = json.loads(r.read())
                    print(f"[{APP}] {name}: OK — {str(data)[:80]}")
            except Exception as e:
                print(f"[{APP}] {name}: FAIL — {e}")

if __name__ == "__main__":
    cli()
