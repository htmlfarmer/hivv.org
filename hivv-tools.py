#!/usr/bin/env python3
"""
hivv-tools — the bee-side tool server.

WHAT THIS IS
────────────
Every member of the hive runs this on its own machine (port 18789, loopback
only). It serves two kinds of tool:

  1. LOCAL — the deterministic, cheap ones: the clock, the calculator, the
     timezone converter, the text parser, the machine snapshot. They work
     with no network at all, which matters when the hub is unreachable.

  2. PROXIED — everything that needs the hub: web search, web fetch, HTTP
     calls, code execution (the hub has the sandbox), the document store and
     the shared memory. The bee forwards the call to the hub and returns the
     answer. The hub is authoritative for those (HIVV_TOOLKIT_PLAN.md §3).

The catalog lives on the hub (api/lib/toolkit.js). This file mirrors the
light half of it so a bee is useful on its own; anything it does not
implement is proxied, so a bee can answer for EVERY tool in the catalog.

ENDPOINTS
─────────
  GET  /health                 — server health
  GET  /tools                  — the tool catalog this bee serves
  GET  /time[?tz=]             — current time (local)
  GET  /calculate?expr=        — math (local)
  GET  /timezone?tz=           — timezone conversion (local)
  GET  /parse?input=           — JSON/CSV/XML/YAML parsing (local)
  GET  /fetch?url=             — read a URL (proxied)
  GET  /search?q=              — web search (proxied)
  GET  /news?q=                — news (proxied)
  GET  /sysinfo                — machine snapshot (local)
  POST /tool/<name>            — call ANY tool in the catalog (local or proxied)

The old GET endpoints above stay exactly as they were, so nothing that
talks to this server today breaks.

SECURITY
────────
Binds to loopback by default and has no auth — it can fetch arbitrary URLs,
so it must not be reachable from outside the box. Override with
HIVV_TOOLS_HOST=0.0.0.0 only when the node's tools must be callable from
another host. All outbound fetches refuse private/internal addresses.
"""
import json, os, sys, re, socket, datetime, urllib.parse, urllib.request

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import threading

PORT = int(os.environ.get("HIVV_TOOLS_PORT", "18789"))
HOST = os.environ.get("HIVV_TOOLS_HOST", "127.0.0.1")
APP = "hivv-tools"
VERSION = "0.2.0"
HUB = os.environ.get("HIVV_HUB", "https://hivv.org").rstrip("/")

TOOL_PORT_FILE = os.path.expanduser("~/.config/hivv/tool-port")
PID_FILE = os.path.expanduser("~/.local/share/hivv/tools.pid")
VISITOR_FILE = os.path.expanduser("~/.local/share/hivv/visitor-id")

# The catalog this bee serves. `local` = computed here, `hub` = proxied to
# the hub. Mirrors api/lib/toolkit.js — keep the two in step.
TOOLS = [
    {"name": "web_search",        "mode": "hub",  "aliases": ["search", "news"]},
    {"name": "current_time",      "mode": "local", "aliases": ["time", "get_time", "now"]},
    {"name": "calculator",        "mode": "local", "aliases": ["calculate", "calc"]},
    {"name": "web_fetch",         "mode": "hub",  "aliases": ["fetch", "reader"]},
    {"name": "http_request",      "mode": "hub",  "aliases": ["request", "curl"]},
    {"name": "code_interpreter",  "mode": "hub",  "aliases": ["exec", "run_code"]},
    {"name": "file_read",         "mode": "hub",  "aliases": ["read_file"]},
    {"name": "file_search",       "mode": "hub",  "aliases": ["search_files", "grep"]},
    {"name": "memory",            "mode": "hub",  "aliases": ["remember", "recall"]},
    {"name": "text_parser",       "mode": "local", "aliases": ["parse"]},
    {"name": "timezone_convert",  "mode": "local", "aliases": ["tz", "convert_time"]},
    {"name": "translate",         "mode": "hub",  "aliases": ["translation"]},
    {"name": "sysinfo",           "mode": "local", "aliases": ["system_info"]},
]

_ALIASES = {}
for _t in TOOLS:
    _ALIASES[_t["name"]] = _t["name"]
    for _a in _t.get("aliases", []):
        _ALIASES[_a] = _t["name"]


def canonical(name):
    return _ALIASES.get(str(name or "").strip().lower())


# ── the calculator: a real parser, not eval() ────────────────────────────────
# The old implementation used eval() on a regex-cleaned string. This one
# parses the expression and evaluates it over plain numbers — there is no
# eval anywhere, so a model cannot smuggle code through the calculator.


def _math_consts():
    import math
    return {"pi": math.pi, "e": math.e, "tau": math.pi * 2, "phi": (1 + math.sqrt(5)) / 2}


def _math_funcs():
    import math
    return {
        "abs": abs, "sign": lambda x: (x > 0) - (x < 0), "floor": math.floor,
        "ceil": math.ceil, "round": round, "trunc": math.trunc,
        "min": min, "max": max, "sum": lambda *a: sum(a), "prod": lambda *a: math.prod(a),
        "hypot": math.hypot,
        "gcd": lambda *a: math.gcd(*[int(x) for x in a]),
        "lcm": lambda *a: math.lcm(*[int(x) for x in a]),
        "factorial": lambda n: math.factorial(int(n)), "sqrt": math.sqrt, "cbrt": lambda x: math.copysign(abs(x) ** (1 / 3), x),
        "pow": pow, "exp": math.exp, "ln": math.log,
        "log": lambda x, b=None: math.log(x) if b is None else math.log(x, b),
        "log10": math.log10, "log2": math.log2,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
        "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
        "deg": math.degrees, "rad": math.radians,
        "clamp": lambda x, lo, hi: min(max(x, lo), hi),
    }


class _CalcError(Exception):
    pass


class _Parser:
    """Recursive-descent parser for arithmetic. No eval, no attribute access."""

    def __init__(self, tokens):
        self.toks = tokens
        self.pos = 0
        self.consts = _math_consts()
        self.funcs = _math_funcs()

    def peek(self):
        return self.toks[self.pos]

    def next(self):
        tok = self.toks[self.pos]
        self.pos += 1
        return tok

    def expect(self, t):
        tok = self.next()
        if tok[0] != t:
            raise _CalcError("expected '%s' but found '%s'" % (t, tok[1]))
        return tok

    def expr(self):
        left = self.term()
        while self.peek()[0] in ("+", "-"):
            op = self.next()[0]
            right = self.term()
            left = left + right if op == "+" else left - right
        return left

    def term(self):
        left = self.unary()
        while self.peek()[0] in ("*", "/", "%"):
            op = self.next()[0]
            right = self.unary()
            if op == "*":
                left *= right
            elif op == "/":
                if right == 0:
                    raise _CalcError("division by zero")
                left /= right
            else:
                if right == 0:
                    raise _CalcError("modulo by zero")
                left %= right
        return left

    def unary(self):
        tok = self.peek()
        if tok[0] == "-":
            self.next()
            return -self.unary()
        if tok[0] == "+":
            self.next()
            return self.unary()
        return self.power()

    def power(self):
        base = self.atom()
        if self.peek()[0] == "^":
            self.next()
            return base ** self.unary()
        return base

    def atom(self):
        tok = self.next()
        kind, val = tok[0], tok[1]
        if kind == "num":
            return val
        if kind == "(":
            v = self.expr()
            self.expect(")")
            return v
        if kind == "id":
            if self.peek()[0] == "(":
                self.next()
                args = []
                if self.peek()[0] != ")":
                    while True:
                        args.append(self.expr())
                        if self.peek()[0] == ",":
                            self.next()
                            continue
                        break
                self.expect(")")
                fn = self.funcs.get(val.lower())
                if fn is None:
                    raise _CalcError("unknown function '%s'" % val)
                return fn(*args)
            if val in self.consts:
                return self.consts[val]
            if val.lower() in self.consts:
                return self.consts[val.lower()]
            raise _CalcError("unknown name '%s'" % val)
        raise _CalcError("unexpected '%s'" % val)


def _tokenize(src):
    tokens = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c.isspace():
            i += 1
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            start = i
            while i < n and src[i].isdigit():
                i += 1
            if i < n and src[i] == ".":
                i += 1
                while i < n and src[i].isdigit():
                    i += 1
            if i < n and src[i] in "eE":
                save = i
                i += 1
                if i < n and src[i] in "+-":
                    i += 1
                if i < n and src[i].isdigit():
                    while i < n and src[i].isdigit():
                        i += 1
                else:
                    i = save
            tokens.append(("num", float(src[start:i])))
            continue
        if c.isalpha() or c == "_":
            start = i
            while i < n and (src[i].isalnum() or src[i] == "_"):
                i += 1
            tokens.append(("id", src[start:i]))
            continue
        if c in "+-*/%^(),":
            tokens.append((c, c))
            i += 1
            continue
        raise _CalcError("unexpected character '%s'" % c)
    tokens.append(("end", None))
    return tokens


def tool_calculate(expr):
    """Evaluate a math expression with a real parser. No eval()."""
    src = str(expr if expr is not None else "").strip()
    if not src:
        return {"error": "expr parameter required"}
    try:
        parser = _Parser(_tokenize(src))
        value = parser.expr()
        if parser.peek()[0] != "end":
            raise _CalcError("unexpected '%s'" % parser.peek()[1])
        if value != value or value in (float("inf"), float("-inf")):
            return {"expression": src, "error": "the result is not a finite number"}
        if value == int(value) and abs(value) < 1e15:
            value = int(value)
        else:
            value = round(value, 12)
        return {"expression": src, "result": value}
    except _CalcError as e:
        return {"expression": src, "error": str(e)}
    except Exception as e:  # pragma: no cover - defensive
        return {"expression": src, "error": str(e)}


# ── time ────────────────────────────────────────────────────────────────────
def tool_time(tz=None):
    """Return the current time in several formats, optionally in a timezone."""
    now = datetime.datetime.now(datetime.timezone.utc)
    result = {
        "unix": int(now.timestamp()),
        "iso": now.isoformat(),
        "utc": now.isoformat(),
        "human": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if tz:
        try:
            import zoneinfo
            local = now.astimezone(zoneinfo.ZoneInfo(tz))
            result["timezone"] = tz
            result["local"] = local.strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            result["error"] = "unknown timezone: %s" % tz
    return result


def tool_timezone(args):
    """Convert a moment between IANA timezones (DST-correct)."""
    to = args.get("timezone") or args.get("to") or args.get("tz")
    if not to:
        return {"error": "a target timezone is required (e.g. Asia/Karachi)"}
    frm = args.get("from_timezone") or args.get("from")
    raw_dt = args.get("datetime")
    try:
        if raw_dt:
            when = datetime.datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=datetime.timezone.utc)
        else:
            when = datetime.datetime.now(datetime.timezone.utc)
    except Exception:
        return {"error": "'%s' is not a parseable date-time" % raw_dt}
    try:
        import zoneinfo
        target = when.astimezone(zoneinfo.ZoneInfo(to))
    except Exception:
        return {"error": "'%s' is not a valid IANA timezone" % to}
    out = {
        "source_datetime": when.isoformat(),
        "timezone": to,
        "local": target.strftime("%Y-%m-%d %H:%M:%S"),
        "date": target.strftime("%Y-%m-%d"),
        "time": target.strftime("%H:%M:%S"),
        "day_of_week": target.strftime("%a"),
        "utc_offset": target.strftime("%z") or None,
    }
    if frm:
        try:
            origin = when.astimezone(zoneinfo.ZoneInfo(frm))
            out["from_timezone"] = frm
            out["from_local"] = origin.strftime("%Y-%m-%d %H:%M:%S")
            out["from_utc_offset"] = origin.strftime("%z") or None
            diff = (target.utcoffset() - origin.utcoffset()).total_seconds() / 3600.0
            out["difference"] = "%+.2f hours" % diff
        except Exception:
            out["from_timezone_error"] = "'%s' is not a valid IANA timezone" % frm
    return out


# ── text parsing ────────────────────────────────────────────────────────────
def tool_parse(args):
    """Parse JSON / CSV / XML / YAML into structure."""
    raw = args.get("input")
    if raw is None:
        raw = args.get("text")
    if raw is None:
        raw = args.get("json")
    if raw is None:
        return {"error": "input text is required"}
    if not isinstance(raw, str):
        raw = json.dumps(raw)
    fmt = str(args.get("format") or "auto").lower()
    operation = str(args.get("operation") or "parse").lower()

    detected = fmt if fmt != "auto" else _detect_format(raw)
    if operation == "extract":
        return {"operation": "extract", "detected_format": detected,
                "extracted": _extract_entities(raw)}
    if operation == "validate":
        if detected != "json":
            return {"error": "validation is only implemented for JSON (detected '%s')" % detected}
        try:
            json.loads(raw)
            return {"operation": "validate", "valid": True, "detected_format": "json"}
        except Exception as e:
            return {"error": "invalid JSON: %s" % e, "operation": "validate", "valid": False}
    try:
        parsed = _parse_as(detected, raw)
    except Exception as e:
        return {"error": "could not parse as %s: %s" % (detected, e)}
    if parsed is None:
        return {"error": "could not parse the input as %s" % detected}
    if operation == "pretty":
        return {"operation": "pretty", "detected_format": detected, "text": json.dumps(parsed, indent=2)}
    summary = {
        "type": "array" if isinstance(parsed, list) else type(parsed).__name__,
        "keys": list(parsed.keys())[:50] if isinstance(parsed, dict) else None,
        "length": len(parsed) if isinstance(parsed, list) else None,
    }
    return {"operation": "parse", "detected_format": detected, "summary": summary, "parsed": parsed}


def _detect_format(text):
    s = text.strip()
    if not s:
        return "text"
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            json.loads(s)
            return "json"
        except Exception:
            pass
    if s.startswith("<?xml") or (s.startswith("<") and s.rstrip().endswith(">")):
        return "xml"
    first = s.splitlines()[0] if s.splitlines() else ""
    if re.match(r"^[ \t]*[-\w.\"']+:(\s|$)", first) and not s.lstrip().startswith(("[", "{")):
        return "yaml"
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) >= 2:
        if sum(1 for ln in lines[:5] if "," in ln) >= max(2, len(lines) * 0.6):
            return "csv"
        if sum(1 for ln in lines[:5] if "\t" in ln) >= max(2, len(lines) * 0.6):
            return "csv"
    return "text"


def _parse_as(fmt, raw):
    if fmt == "json":
        return json.loads(raw)
    if fmt == "csv":
        import csv as _csv
        import io
        delim = "\t" if ("\t" in raw and "," not in raw) else ","
        rows = list(_csv.reader(io.StringIO(raw), delimiter=delim))
        if not rows:
            return []
        header = [h.strip() for h in rows[0]]
        return [dict(zip(header, r)) for r in rows[1:]]
    if fmt == "xml":
        import xml.etree.ElementTree as ET
        root = ET.fromstring(raw)

        def node(el):
            kids = list(el)
            if not kids:
                return (el.text or "").strip()
            out = {}
            for k in kids:
                val = node(k)
                if k.tag in out:
                    if not isinstance(out[k.tag], list):
                        out[k.tag] = [out[k.tag]]
                    out[k.tag].append(val)
                else:
                    out[k.tag] = val
            if (el.text or "").strip():
                out["#text"] = el.text.strip()
            return out

        return {root.tag: node(root)}
    if fmt == "yaml":
        return _parse_yaml(raw)
    return raw


def _parse_yaml(text):
    """A practical YAML subset: nested maps, block lists, plain scalars."""
    lines = []
    for ln in text.splitlines():
        ln = ln.replace("\t", "    ")
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        lines.append(ln)
    pos = [0]

    def indent_of(ln):
        return len(ln) - len(ln.lstrip())

    def scalar(v):
        v = v.strip()
        if v in ("", "~", "null", "Null", "NULL"):
            return None
        if v.lower() in ("true", "yes", "on"):
            return True
        if v.lower() in ("false", "no", "off"):
            return False
        if re.match(r"^-?\d+$", v):
            return int(v)
        if re.match(r"^-?\d*\.\d+$", v):
            return float(v)
        if len(v) > 1 and v[0] == v[-1] and v[0] in "\"'":
            return v[1:-1]
        return v

    def find_colon(t):
        quote = None
        for i, c in enumerate(t):
            if quote:
                if c == quote:
                    quote = None
                continue
            if c in "\"'":
                quote = c
                continue
            if c == ":" and (i + 1 == len(t) or t[i + 1].isspace()):
                return i
        return -1

    def block(min_indent):
        if pos[0] >= len(lines):
            return None
        ind = indent_of(lines[pos[0]])
        if ind < min_indent:
            return None
        if lines[pos[0]].strip().startswith("- "):
            arr = []
            while pos[0] < len(lines):
                ln = lines[pos[0]]
                if indent_of(ln) < ind:
                    break
                t = ln.strip()
                if not (t.startswith("- ") or t == "-"):
                    break
                pos[0] += 1
                rest = t[1:].strip()
                arr.append(scalar(rest) if rest else block(ind + 1))
            return arr
        out = {}
        while pos[0] < len(lines):
            ln = lines[pos[0]]
            li = indent_of(ln)
            if li < ind:
                break
            if li > ind:
                pos[0] += 1
                continue
            t = ln.strip()
            c = find_colon(t)
            if c == -1:
                pos[0] += 1
                continue
            key = t[:c].strip().strip("\"'")
            rest = t[c + 1:].strip()
            pos[0] += 1
            out[key] = scalar(rest) if rest else block(ind + 1)
        return out

    result = block(0)
    return result if result is not None else {}


def _extract_entities(text):
    urls = sorted(set(re.findall(r"\bhttps?://[^\s\"'<>()\[\]{}]+", text)))
    emails = sorted(set(re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", text)))
    dates = sorted(set(re.findall(
        r"\b(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:?\d{2})?|\d{1,2}/\d{1,2}/\d{2,4})\b", text)))
    stripped = re.sub(
        r"\b(?:\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?(?:Z|[+-]\d{2}:?\d{2})?|\d{1,2}/\d{1,2}/\d{2,4})\b", " ", text)
    nums = []
    for m in re.findall(r"-?\d+(?:\.\d+)?", stripped):
        try:
            nums.append(float(m))
        except ValueError:
            pass
    return {
        "urls": urls,
        "emails": emails,
        "dates": dates,
        "numbers": {
            "count": len(nums),
            "sum": sum(nums),
            "min": min(nums) if nums else None,
            "max": max(nums) if nums else None,
        },
    }


# ── SSRF guard ──────────────────────────────────────────────────────────────
def _is_private_ip(ip):
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified
            or (hasattr(addr, "ipv4_mapped") and addr.ipv4_mapped is not None
                and addr.ipv4_mapped.is_private))


def _host_is_internal(hostname):
    """Resolve a hostname and refuse it if it points anywhere internal."""
    if not hostname:
        return True
    hostname = hostname.lower().strip("[]")
    if hostname == "localhost" or hostname.endswith((".internal", ".local", ".localhost")):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return True
    for info in infos:
        if _is_private_ip(info[4][0]):
            return True
    return False


def tool_fetch(url, timeout=10):
    """HTTP GET any URL. Returns status + body snippet. Blocks internal hosts."""
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"url": url, "error": "only http/https allowed"}
        if _host_is_internal(parsed.hostname or ""):
            return {"url": url, "error": "internal hosts not allowed"}
        req = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (APP, VERSION)})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return {
                "url": url,
                "status": r.status,
                "headers": dict(r.headers),
                "body": body[:2000],
                "truncated": len(body) > 2000,
            }
    except Exception as e:
        return {"url": url, "error": str(e)}


def tool_search(query, limit=5):
    """Wikipedia search — the same fallback the hub uses."""
    try:
        q = urllib.parse.quote(query)
        url = ("https://en.wikipedia.org/w/api.php?action=query&list=search"
               "&srsearch=%s&srlimit=%d&format=json&origin=*" % (q, limit))
        req = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (APP, VERSION)})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        out = []
        for hit in data.get("query", {}).get("search", []):
            title = hit.get("title", "?")
            out.append({
                "title": title,
                "url": "https://en.wikipedia.org/wiki/%s" % urllib.parse.quote(title.replace(" ", "_")),
                "snippet": re.sub(r"<[^>]+>", "", hit.get("snippet", "")),
            })
        return {"query": query, "results": out, "count": len(out)}
    except Exception as e:
        return {"query": query, "error": str(e)}


def tool_news(query, limit=8):
    """Google News RSS — the same fallback the hub uses."""
    try:
        q = urllib.parse.quote(query)
        url = ("https://news.google.com/rss/search?q=%s&hl=en-US&gl=US&ceid=US:en" % q)
        req = urllib.request.Request(url, headers={"User-Agent": "%s/%s" % (APP, VERSION)})
        with urllib.request.urlopen(req, timeout=10) as r:
            xml = r.read().decode("utf-8", errors="replace")
        items = re.findall(r"<item[^>]*>([\s\S]*?)</item>", xml, re.I)
        results = []
        for item in items[:limit]:
            tm = re.search(r"<title>(.*?)</title>", item, re.I)
            lm = re.search(r"<link>(.*?)</link>", item, re.I)
            pm = re.search(r"<pubDate>(.*?)</pubDate>", item, re.I)
            dm = re.search(r"<description>(.*?)</description>", item, re.I)
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1)).strip() if tm else ""
            link = re.sub(r"<!\[CDATA\[|\]\]>", "", lm.group(1)).strip() if lm else ""
            pub = pm.group(1).strip() if pm else ""
            desc = re.sub(r"<[^>]+>", "", re.sub(r"<!\[CDATA\[|\]\]>", "", dm.group(1) if dm else "")).strip()
            if title and title != "Google News":
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
    except Exception:
        pass
    try:
        m["load"] = os.getloadavg() if hasattr(os, "getloadavg") else None
        m["cpu_count"] = os.cpu_count()
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        m["disk_free_gb"] = round(st.f_bavail * st.f_frsize / 1e9, 1)
        m["disk_total_gb"] = round(st.f_blocks * st.f_frsize / 1e9, 1)
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            m["uptime_s"] = int(float(f.read().split()[0]))
    except Exception:
        pass
    return m


# ── the hub proxy ───────────────────────────────────────────────────────────
def _visitor_id() -> str:
    """A STABLE identity for this machine, so a policy grant given to the
    bee is still valid on the next call.

    Without it every proxied call would look like a brand-new caller (the
    hub would mint a fresh MAYBE each time and a human could never approve
    anything once). Falls back to the machine-id, then to a persisted
    random id.
    """
    env = os.environ.get("HIVV_VISITOR_ID")
    if env:
        return env
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path) as f:
                mid = f.read().strip()
            if mid:
                return "cli_" + mid
        except OSError:
            continue
    try:
        if VISITOR_FILE.exists():
            v = VISITOR_FILE.read_text().strip()
            if v:
                return v
    except OSError:
        pass
    import uuid
    v = "cli_" + uuid.uuid4().hex[:16]
    try:
        VISITOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        VISITOR_FILE.write_text(v)
    except OSError:
        pass
    return v


def hub_call(tool, args, timeout=30):
    """Ask the hub to run a tool. The hub is authoritative for these."""
    url = "%s/api/tools/%s/invoke" % (HUB, urllib.parse.quote(tool))
    body = json.dumps(args or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "%s/%s" % (APP, VERSION),
                 # a stable identity, so an approved grant stays approved
                 "x-visitor-id": _visitor_id()})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw}
    except Exception as e:
        return {"error": "the hub could not run '%s': %s" % (tool, e), "tool": tool}


def run_tool(name, args):
    """Run any tool in the catalog — locally when it can be, else via the hub."""
    canon = canonical(name)
    if not canon:
        return {"error": "unknown tool '%s'" % name, "tool": name}
    args = args if isinstance(args, dict) else {}
    mode = next((t["mode"] for t in TOOLS if t["name"] == canon), "hub")
    if mode == "local":
        if canon == "current_time":
            return {"tool": canon, "ok": True, "data": tool_time(args.get("timezone") or args.get("tz"))}
        if canon == "calculator":
            expr = args.get("expression")
            if expr is None and args.get("a") is not None and args.get("b") is not None:
                op = str(args.get("operation", "+"))
                if op not in "+-*/%^":
                    return {"tool": canon, "ok": False, "error": "unsupported operation '%s'" % op}
                expr = "(%s) %s (%s)" % (args.get("a"), op, args.get("b"))
            if expr is None:
                expr = args.get("expr")
            if expr is None:
                return {"tool": canon, "ok": False, "error": "an expression is required"}
            res = tool_calculate(expr)
            if "error" in res:
                return {"tool": canon, "ok": False, "error": res["error"]}
            return {"tool": canon, "ok": True, "data": {"expression": res["expression"], "result": res["result"]}}
        if canon == "timezone_convert":
            res = tool_timezone(args)
            if "error" in res:
                return {"tool": canon, "ok": False, "error": res["error"]}
            return {"tool": canon, "ok": True, "data": res}
        if canon == "text_parser":
            res = tool_parse(args)
            if "error" in res:
                return {"tool": canon, "ok": False, "error": res["error"]}
            return {"tool": canon, "ok": True, "data": res}
        if canon == "sysinfo":
            return {"tool": canon, "ok": True, "data": tool_sysinfo()}
    # proxied to the hub
    resp = hub_call(canon, args)
    if isinstance(resp, dict) and resp.get("verdict") == "MAYBE":
        return {"tool": canon, "ok": False,
                "error": "MAYBE — human approval required: %s" % resp.get("message", ""),
                "policy": {"verdict": "ask", "grant_id": resp.get("grant_id")}}
    if isinstance(resp, dict) and resp.get("verdict") == "NO":
        return {"tool": canon, "ok": False, "error": resp.get("error", "policy refused")}
    if isinstance(resp, dict) and resp.get("ok") is True:
        return resp
    return {"tool": canon, "ok": False,
            "error": (resp.get("error") if isinstance(resp, dict) else None) or "the hub did not answer"}


# ── HTTP Server ─────────────────────────────────────────────────────────────
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
        q = lambda k, d=None: (qs.get(k, [d])[0] if qs.get(k) else d)

        if path == "/health":
            return self.send_json({"status": "ok", "app": APP, "version": VERSION, "hub": HUB})

        if path == "/tools":
            return self.send_json({
                "tools": [t["name"] for t in TOOLS],
                "local_tools": [t["name"] for t in TOOLS if t["mode"] == "local"],
                "hub_tools": [t["name"] for t in TOOLS if t["mode"] == "hub"],
                "endpoint": "http://localhost:%d" % PORT,
                "app": APP, "version": VERSION, "hub": HUB,
            })

        # ── the old endpoints, unchanged ────────────────────────────────────
        if path == "/time":
            return self.send_json(tool_time(q("tz")))
        if path == "/calculate":
            expr = q("expr")
            if not expr:
                return self.send_json({"error": "expr parameter required"}, 400)
            return self.send_json(tool_calculate(expr))
        if path == "/fetch":
            url = q("url")
            if not url:
                return self.send_json({"error": "url parameter required"}, 400)
            return self.send_json(tool_fetch(url))
        if path == "/search":
            s = q("q", q("query"))
            if not s:
                return self.send_json({"error": "q parameter required"}, 400)
            return self.send_json(tool_search(s))
        if path == "/news":
            s = q("q", q("query"))
            if not s:
                return self.send_json({"error": "q parameter required"}, 400)
            return self.send_json(tool_news(s))
        if path == "/sysinfo":
            return self.send_json(tool_sysinfo())

        # ── the new local endpoints ─────────────────────────────────────────
        if path == "/timezone":
            return self.send_json(tool_timezone({"timezone": q("tz", q("timezone")), "datetime": q("datetime")}))
        if path == "/parse":
            return self.send_json(tool_parse({"input": q("input", q("text")), "format": q("format"), "operation": q("operation")}))

        # Default: list endpoints
        self.send_json({
            "app": APP, "version": VERSION,
            "endpoints": ["/health", "/tools", "/time", "/time?tz=America/New_York",
                          "/calculate?expr=2%2B2", "/fetch?url=https://example.com",
                          "/search?q=hello", "/news?q=San+Francisco", "/sysinfo",
                          "/timezone?tz=Asia/Karachi", "/parse?input={}",
                          "POST /tool/<name>"],
        })

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            args = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            args = {}
        if not isinstance(args, dict):
            args = {}
        if path.startswith("/tool/"):
            name = path[len("/tool/"):]
            return self.send_json(run_tool(name, args))
        self.send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Server lifecycle ────────────────────────────────────────────────────────
def write_pid():
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass


def read_pid():
    try:
        with open(PID_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


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
        print("[%s] already running (PID %s)" % (APP, existing))
        return False
    try:
        write_pid()
        server = HTTPServer((HOST, port), ToolHandler)
        print("[%s] listening on http://%s:%d" % (APP, HOST, port))
        print("[%s] tools: %s" % (APP, ", ".join(t["name"] for t in TOOLS)))
        print("[%s] press Ctrl+C to stop" % APP)
        server.serve_forever()
    except OSError as e:
        if e.errno == 98:
            print("[%s] port %d in use — is another instance running?" % (APP, port))
        else:
            print("[%s] failed to start: %s" % (APP, e))
        return False
    return True


def stop_server():
    pid = read_pid()
    if not pid:
        print("[%s] not running (no PID file)" % APP)
        return
    try:
        os.kill(pid, 15)
        print("[%s] stopped (PID %s)" % (APP, pid))
    except OSError:
        print("[%s] process %s not found — removing stale PID" % (APP, pid))
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def restart_server(port=PORT):
    stop_server()
    import time
    time.sleep(1)
    start_server(port)


# ── CLI ─────────────────────────────────────────────────────────────────────
def cli():
    import argparse
    p = argparse.ArgumentParser(prog=APP, description="Tool server for the hivv network")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("start", help="start the tool server").add_argument("--port", type=int, default=PORT)
    sub.add_parser("stop", help="stop the tool server")
    sub.add_parser("restart", help="stop + start").add_argument("--port", type=int, default=PORT)
    sub.add_parser("status", help="check if running")
    sub.add_parser("test", help="test all tools locally")

    args = p.parse_args()

    if args.cmd == "start" or args.cmd is None:
        if is_running():
            print("[%s] already running on port %d (PID %s)" % (APP, PORT, is_running()))
            return
        start_server(args.port if args.cmd == "start" else PORT)
    elif args.cmd == "stop":
        stop_server()
    elif args.cmd == "restart":
        restart_server(args.port if hasattr(args, "port") else PORT)
    elif args.cmd == "status":
        pid = is_running()
        if pid:
            print("[%s] running (PID %s) on port %d" % (APP, pid, PORT))
            try:
                import urllib.request
                with urllib.request.urlopen("http://localhost:%d/tools" % PORT, timeout=3) as r:
                    data = json.loads(r.read())
                    print("[%s] available tools: %s" % (APP, ", ".join(data.get("tools", []))))
                    print("[%s] local: %s | via hub: %s" % (
                        APP, ", ".join(data.get("local_tools", [])),
                        ", ".join(data.get("hub_tools", []))))
            except Exception as e:
                print("[%s] health check failed: %s" % (APP, e))
        else:
            print("[%s] not running" % APP)
    elif args.cmd == "test":
        import urllib.request
        base = "http://localhost:%d" % PORT
        checks = [
            ("/time", "current_time"),
            ("/calculate?expr=47*389%2B1024", "calculator"),
            ("/timezone?tz=Asia%2FKarachi", "timezone_convert"),
            ("/sysinfo", "sysinfo"),
        ]
        for path, name in checks:
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    data = json.loads(r.read())
                    print("[%s] %s: OK — %s" % (APP, name, str(data)[:80]))
            except Exception as e:
                print("[%s] %s: FAIL — %s" % (APP, name, e))


if __name__ == "__main__":
    cli()
