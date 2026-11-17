#!/usr/bin/env bash
# install-hivv.sh — drop the hivv CLI anywhere, instantly.
#
# Usage:
#   curl -sSL https://hivv.org/install-hivv.sh | bash
#   curl -sSL https://hivv.org/install-hivv.sh | bash -s -- --prefix=$HOME/.local
#   curl -sSL https://hivv.org/install-hivv.sh | bash -s -- --uninstall
#
# Works on: Linux, macOS, WSL, Git-Bash/MSYS (on Windows, use the PowerShell
# installer instead:  iwr -useb https://hivv.org/install-hivv.ps1 | iex)
#
# Flags:
#   --prefix=DIR     install location (default /usr/local — use $HOME/.local for user)
#   --no-path        skip the "add to PATH" message if prefix is non-standard
#   --yes            skip prompts + auto-add BIN_DIR to PATH (non-interactive friendly)
#   --with-llm       also install llama.cpp + a model recommended for your hardware
#   --no-llm         skip the LLM install (default: asks if interactive, skips if piped)
#   --no-service     skip installing the background service (runs by default with sudo)
#   --experiment     dry-run pre-install check — validates everything without modifying system
#   --uninstall      remove what we installed
#   --quiet          less output
#   -h, --help       this help

set -euo pipefail

# ── colors ─────────────────────────────────────────────────────────────
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    RST=$'\033[0m'; BLD=$'\033[1m'; DIM=$'\033[2m'; UND=$'\033[4m'
    RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; BLU=$'\033[34m'
    MAG=$'\033[35m'; CYN=$'\033[36m'; WHT=$'\033[37m'
    # brand: hivv = amber/honeycomb
    HIVE=$'\033[38;5;214m'; COMB=$'\033[38;5;220m'
else
    RST=""; BLD=""; DIM=""; UND=""; RED=""; GRN=""; YLW=""; BLU=""; MAG=""; CYN=""; WHT=""
    HIVE=""; COMB=""
fi

# ── globals ────────────────────────────────────────────────────────────
APP="hivv"
HUB="https://hivv.org"
PREFIX="/usr/local"
DO_UNINSTALL=0
DO_EXPERIMENT=0
NO_PATH=0
ASSUME_YES=0
QUIET=0
WITH_LLM=0   # 0 = auto-prompt if interactive, 1 = yes, -1 = no
WITH_SERVICE=1  # 1 = install service by default when sudo available, 0 = skip
LLM_MODEL=""

# ── banner ─────────────────────────────────────────────────────────────
banner() {
    printf '\n'
    printf '  %s%s' "$HIVE" "$BLD"
    cat <<'BANNER'

      ██╗  ██╗██╗██╗   ██╗██╗   ██╗
      ██║  ██║██║██║   ██║██║   ██║
      ███████║██║██║   ██║██║   ██║
      ██╔══██║██║╚██╗ ██╔╝╚██╗ ██╔╝
      ██║  ██║██║ ╚████╔╝  ╚████╔╝
      ╚═╝  ╚═╝╚═╝  ╚═══╝    ╚═══╝

BANNER
    printf '%s' "$RST"
    printf '\n'
    printf '  %s🌌  hivv — the people'\''s distributed AI network\n'
    printf '  %sall of us · together · helping solve Earth'\''s hardest problems%s\n\n' "$DIM" "$RST"
    printf '  %s⚡ open models · open weights · open source · no big-tech lock-in%s\n' "$COMB"
}

# ── experiment: pre-install validation (dry-run) ─────────────────────
experiment() {
    banner
    printf '  %spre-install experiment — validating system without modifying anything%s\n\n' "$COMB" "$RST"
    printf "  This will run every check the installer does — but change nothing.\n"
    printf "  A detailed report will show what will happen when you install.\n\n"

    local EXP_TMPDIR
    EXP_TMPDIR=$(mktemp -d "/tmp/hivv-experiment.XXXXXX") || return 1
    local EXP_CLI="$EXP_TMPDIR/hivv-cli.py"
    local PASS=0 FAIL=0 WARN=0

    _pass() { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "$GRN" "$RST" "$1"; }
    _fail() { FAIL=$((FAIL+1)); printf '  %s✗%s %s\n' "$RED" "$RST" "$1"; }
    _warn() { WARN=$((WARN+1)); printf '  %s!%s %s\n' "$YLW" "$RST" "$1"; }
    _info() { printf '  %s%s%s %s\n' "$BLU" "→" "$RST" "$1"; }

    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 1: HIVV HUB REACHABILITY%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    _info "testing connection to ${HUB}..."
    local hub_reply hub_ms
    hub_reply=$(curl -fsSL --connect-timeout 10 --max-time 30 \
                -w "%{http_code}|%{time_total}" \
                -o /dev/null "${HUB}/api/health" 2>/dev/null || echo "000|0")
    hub_ms=$(echo "$hub_reply" | cut -d'|' -f2)
    local hub_code
    hub_code=$(echo "$hub_reply" | cut -d'|' -f1)
    if [[ "$hub_code" == "200" ]]; then
        _pass "HIVV hub is reachable (${hub_ms}s)"
    else
        _fail "HIVV hub unreachable (HTTP $hub_code) — check network connection"
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 2: SYSTEM REQUIREMENTS%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"

    _info "checking python3..."
    local py_bin py_ver
    for p in python3 python; do
        if command -v "$p" >/dev/null 2>&1; then
            py_bin="$p"
            py_ver=$("$p" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)
            break
        fi
    done
    if [[ -n "$py_bin" ]]; then
        local py_ok
        py_ok=$(echo "$py_ver" | awk -F. '{if($1>=3 && ($1>3 || $2>=8)) print "yes"; else print "no"}')
        if [[ "$py_ok" == "yes" ]]; then
            _pass "python $py_ver found ($py_bin)"
        else
            _fail "python $py_ver found but need 3.8+"
        fi
    else
        _fail "python3 not found — install it first"
    fi

    _info "checking downloader..."
    if command -v curl >/dev/null 2>&1; then
        _pass "curl available"
    elif command -v wget >/dev/null 2>&1; then
        _pass "wget available"
    else
        _fail "neither curl nor wget found"
    fi

    _info "checking for existing hivv agent..."
    if pgrep -f "hivv.*_agent" >/dev/null 2>&1; then
        _warn "hivv agent already running — install will update it"
    else
        _pass "no existing hivv agent running"
    fi

    _info "checking systemd user session..."
    if systemctl --user show-environment >/dev/null 2>&1; then
        _pass "systemd user session available (services auto-start on boot)"
    else
        _warn "no systemd user session — agent won't auto-start on reboot"
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 3: HIVV CLI DOWNLOAD TEST%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    _info "downloading hivv-cli.py to temp dir (no install)..."
    local dl_rc=0
    curl -fsSL --connect-timeout 15 --max-time 60 \
         -o "$EXP_CLI" "${HUB}/${APP}-cli.py" 2>/dev/null || dl_rc=1
    if [[ "$dl_rc" -eq 0 ]] && [[ -s "$EXP_CLI" ]]; then
        local cli_size
        cli_size=$(wc -c < "$EXP_CLI")
        if head -1 "$EXP_CLI" 2>/dev/null | grep -qiE "<!DOCTYPE|<html"; then
            _fail "downloaded file is HTML (server misconfigured)"
        else
            _pass "hivv-cli.py downloaded successfully (${cli_size} bytes)"
        fi
    else
        _fail "download failed — check network/proxy"
    fi

    if [[ -f "$EXP_CLI" ]] && [[ -s "$EXP_CLI" ]]; then
        local sha256
        sha256=$(sha256sum "$EXP_CLI" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$EXP_CLI" 2>/dev/null | cut -d' ' -f1)
        if [[ -n "$sha256" ]]; then
            printf '  %s%s%s %sSHA256:%s %s%s%s\n' "$BLU" "→" "$RST" "$DIM" "$RST" "$CYN" "$sha256" "$RST"
            printf '    %sSet HIVV_TRUSTED_FINGERPRINT=%s to lock auto-updates to this version%s\n' "$DIM" "$sha256" "$RST"
        fi
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 4: HIVV CLI SMOKE TEST%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    if [[ -f "$EXP_CLI" ]] && [[ -s "$EXP_CLI" ]]; then
        _info "running: python3 hivv-cli.py doctor..."
        local doctor_out
        doctor_out=$(python3 "$EXP_CLI" doctor --no-color 2>&1 || true)
        if echo "$doctor_out" | grep -qiE "unreachable|failed|cannot"; then
            _warn "some doctor checks reported issues"
        else
            _pass "hivv doctor ran successfully"
        fi

        _info "testing hub health via CLI..."
        local health_out
        health_out=$(python3 "$EXP_CLI" health 2>&1 || true)
        if echo "$health_out" | grep -q "healthy"; then
            _pass "hub health check passed"
        else
            _fail "hub health check failed"
        fi
    else
        _warn "skipping CLI smoke test (download failed)"
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 5: INSTALL PATH PERMISSIONS%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    local test_prefix="$PREFIX"
    [[ "$test_prefix" == "/usr/local" ]] && test_prefix="$HOME/.local"
    local test_bindir="$test_prefix/bin"
    local test_libdir="$test_prefix/lib/$APP"
    _info "testing write access to $test_bindir..."
    if mkdir -p "$test_bindir" 2>/dev/null; then
        _pass "install directory writable ($test_bindir)"
    else
        _fail "cannot write to $test_bindir — need sudo or --prefix=\$HOME/.local"
    fi
    _info "testing write access to $test_libdir..."
    if mkdir -p "$test_libdir" 2>/dev/null; then
        _pass "library directory writable ($test_libdir)"
    else
        _fail "cannot write to $test_libdir — need sudo or --prefix=\$HOME/.local"
    fi

    _info "checking sudo availability..."
    if sudo -n true 2>/dev/null; then
        _pass "sudo available (system-wide install possible)"
    else
        _warn "sudo not available — user-local install will be used"
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sCHECK 6: PORTS & SERVICES STATUS%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    for port in 3011 8080 8081 11434; do
        _info "checking port $port..."
        if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
            local port_info
            port_info=$(ss -tlnp 2>/dev/null | grep ":${port} " | head -1 | cut -d' ' -f4-)
            printf '  %s!%s port %s already in use: %s%s%s\n' "$YLW" "$RST" "$port" "$DIM" "$port_info" "$RST"
        else
            _pass "port $port is free"
        fi
    done

    _info "checking for ollama..."
    if command -v ollama >/dev/null 2>&1; then
        _pass "ollama found — will be configured for network access"
    else
        _warn "ollama not found — LLM ports won't be shared (optional)"
    fi

    _info "checking for tailscale..."
    if command -v tailscale >/dev/null 2>&1; then
        local ts_ip
        ts_ip=$(tailscale ip -4 2>/dev/null || true)
        if [[ -n "$ts_ip" ]]; then
            _pass "tailscale active (VPN IP: $ts_ip)"
        else
            _warn "tailscale installed but not logged in — run: sudo tailscale up"
        fi
    else
        _warn "tailscale not installed (optional — for cross-network nodes)"
    fi

    printf '\n%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %sEXPERIMENT SUMMARY%s\n' "$BLD" "$RST"
    printf '%s───────────────────────────────────────────────────────────────%s\n' "$DIM" "$RST"
    printf '  %s%3d passed   %s%3d warnings   %s%3d failed%s\n' \
        "$GRN" "$PASS" "$YLW" "$WARN" "$RED" "$FAIL" "$RST"
    printf '\n'

    rm -rf "$EXP_TMPDIR" 2>/dev/null || true

    if [[ "$FAIL" -eq 0 ]]; then
        printf '  %s✓ All critical checks passed — ready to install!%s\n' "$GRN" "$RST"
        printf '\n  To install:\n'
        printf '    %scurl -sSL %s/install-%s.sh | bash%s\n\n' "$CYN" "$HUB" "$APP" "$RST"
        exit 0
    else
        printf '  %s✗ Some checks failed — fix issues before installing.%s\n' "$RED" "$RST"
        printf '\n  For help: hivv support  or  https://hivv.org\n\n'
        exit 1
    fi
}

# ── arg parsing ────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix=*) PREFIX="${1#*=}"   ; shift ;;
        --prefix)   PREFIX="$2"       ; shift 2 ;;
        --no-path)  NO_PATH=1         ; shift ;;
        --yes|-y)   ASSUME_YES=1      ; shift ;;
        --with-llm) WITH_LLM=1        ; shift ;;
        --no-llm)   WITH_LLM=-1       ; shift ;;
        --no-service)  WITH_SERVICE=0  ; shift ;;
        --quiet|-q) QUIET=1           ; shift ;;
        --experiment) DO_EXPERIMENT=1 ; shift ;;
        --uninstall) DO_UNINSTALL=1   ; shift ;;
        -h|--help)
            printf '%s\n' \
              "usage: curl -sSL https://hivv.org/install-hivv.sh | bash [flags]" \
              "" \
              "  --prefix=DIR   install location (default /usr/local; \$HOME/.local for user-only)" \
              "  --no-path      skip the add-to-PATH note" \
              "  --yes          no prompts; auto-add BIN_DIR to PATH (for scripts)" \
              "  --with-llm     also install llama.cpp + a recommended open model" \
              "  --no-llm       skip the LLM prompt" \
              "  --no-service   skip the boot-time background service" \
              "  --experiment   dry-run pre-flight check — changes nothing" \
              "  --uninstall    remove what we installed" \
              "  --quiet        less output" \
              "" \
              "Windows (PowerShell):  iwr -useb https://hivv.org/install-hivv.ps1 | iex"
            exit 0
            ;;
        *) printf '%s%s%s\n' "$RED" "unknown flag: $1 (try --help)" "$RST" >&2; exit 1 ;;
    esac
done

# Run experiment mode if requested (before any install checks)
if [[ "$DO_EXPERIMENT" == "1" ]]; then
    experiment
    exit $?
fi
_log()  { [[ "$QUIET" -eq 1 ]] || printf '  %s%s%s\n' "$BLU" "$*" "$RST"; }
_say()  { printf '  %s✓%s %s\n' "$GRN" "$RST" "$*"; }
_warn() { printf '  %s!%s %s\n' "$YLW" "$RST" "$*" >&2; }
_die()  { printf '  %s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# ── spinner (runs in foreground while $1 sleeps) ──────────────────────
_spin() {
    local msg="$1"; shift
    local frames='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
    local i=0
    "$@" >/dev/null 2>&1 &
    local pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        printf '\r  %s%s%s %s' "$HIVE" "${frames:i++%${#frames}:1}" "$RST" "$msg"
        sleep 0.08
    done
    wait "$pid"
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        printf '\r  %s✗%s %s failed%s\n' "$RED" "$RST" "$msg" "$RST"
        printf '  %scheck your network connection and try again%s\n' "$DIM" "$RST"
        exit 1
    fi
    printf '\r  %s✓%s %s\n' "$GRN" "$RST" "$msg"
}

# ── uninstall path ────────────────────────────────────────────────────
need() { command -v "$1" >/dev/null 2>&1; }
_can_sudo() { sudo -n true 2>/dev/null; }

if [[ "$DO_UNINSTALL" == "1" ]]; then
    BIN_DIR="$PREFIX/bin"
    LIB_DIR="$PREFIX/lib/$APP"
    _log "${BLD}uninstalling${RST}"

    # stop the agent first (best effort, no sudo needed for your own process)
    if need "$APP"; then "$APP" stop 2>/dev/null || true; fi

    USUDO=""
    if [[ ! -w "$BIN_DIR" ]] || [[ ! -w "$(dirname "$LIB_DIR")" ]]; then
        _can_sudo && USUDO="sudo"
    fi
    $USUDO rm -f "$BIN_DIR/$APP" "$LIB_DIR/${APP}-cli.py" 2>/dev/null || true
    $USUDO rmdir "$LIB_DIR" 2>/dev/null || true
    _say "removed ${DIM}$BIN_DIR/$APP${RST} and ${DIM}$LIB_DIR/${APP}-cli.py${RST}"

    # remove system services created by past installs (both naming eras)
    if need systemctl; then
        for svc in ${APP}-agent ${APP}-website; do
            if [[ -f "/etc/systemd/system/${svc}.service" ]]; then
                $USUDO systemctl disable --now "$svc" 2>/dev/null || true
                $USUDO rm -f "/etc/systemd/system/${svc}.service"
                $USUDO systemctl daemon-reload 2>/dev/null || true
                _say "removed systemd service: ${svc}.service"
            fi
            if [[ -f "$HOME/.config/systemd/user/${svc}.service" ]]; then
                systemctl --user disable --now "$svc" 2>/dev/null || true
                rm -f "$HOME/.config/systemd/user/${svc}.service"
                systemctl --user daemon-reload 2>/dev/null || true
                _say "removed user service: ${svc}.service"
            fi
        done
    fi
    # remove macOS LaunchAgent if present
    if [[ "$(uname -s)" == "Darwin" ]] && [[ -f "$HOME/Library/LaunchAgents/org.hivv.agent.plist" ]]; then
        launchctl bootout "gui/$(id -u)/org.hivv.agent" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/org.hivv.agent.plist"
        _say "removed LaunchAgent: org.hivv.agent"
    fi
    _log "${DIM}(your data in ~/.config/$APP/ and ~/.local/share/$APP/ was kept)${RST}"
    exit 0
fi

# ── preflight ─────────────────────────────────────────────────────────

# Windows shell → suggest PowerShell installer
case "${OSTYPE:-$(uname -s 2>/dev/null)}" in
    msys*|cygwin*|mingw*|win32)
        printf '%sWindows shell detected (%s).\n' "$RED$BLD" "${OSTYPE:-Windows}" >&2
        printf '   Use the PowerShell installer (native, no bash needed):\n%s\n' "$RST" >&2
        printf '       %siwr -useb %s/install-%s.ps1 | iex%s\n' "$CYN" "$HUB" "$APP" "$RST" >&2
        printf '   Or, if you prefer Linux tools on Windows, run this script inside %sWSL%s:\n' "$BLD" "$RST" >&2
        printf '       %swsl --install%s   then paste the curl one-liner there.\n\n' "$CYN" "$RST" >&2
        exit 1
        ;;
esac

# Runtime deps
PYTHON_BIN=""
for p in python3 python; do
    if need "$p"; then PYTHON_BIN="$p"; break; fi
done
[[ -n "$PYTHON_BIN" ]] || _die "python3 not found. install it first (apt install python3 / brew install python3)"
PY_VER="$("$PYTHON_BIN" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' 2>/dev/null || echo unknown)"

if ! need curl && ! need wget; then
    _die "need curl or wget to download the client"
fi

# print banner + env
banner
printf '  %senvironment%s\n' "$BLD" "$RST"
printf '  %s✓%s python     %s\n' "$GRN" "$RST" "$PY_VER"
printf '  %s✓%s downloader %s%s%s\n' "$GRN" "$RST" "$DIM" "$(need curl && echo curl || echo wget)" "$RST"
printf '  %s✓%s os         %s\n' "$GRN" "$RST" "$(uname -srm 2>/dev/null || echo unknown)"
printf '  %s✓%s prefix     %s%s%s\n' "$GRN" "$RST" "$BLU" "$PREFIX" "$RST"
printf '\n'

# ── install ───────────────────────────────────────────────────────────
BIN_DIR="$PREFIX/bin"
LIB_DIR="$PREFIX/lib/$APP"
CLIENT_FILE="$LIB_DIR/${APP}-cli.py"
WRAPPER="$BIN_DIR/$APP"

SUDO=""

# if the prefix exists but dirs don't yet, create them first (so an explicit
# --prefix like $HOME/opt isn't wrongly treated as "not writable")
[[ -w "$PREFIX" || -w "$(dirname "$PREFIX")" ]] && mkdir -p "$BIN_DIR" "$LIB_DIR" 2>/dev/null || true

if [[ ! -w "$BIN_DIR" ]] || [[ ! -w "$(dirname "$LIB_DIR")" ]]; then
    if _can_sudo; then
        printf '  %s⚡ sudo available — system-wide install (helps everyone on this machine)%s\n' "$BLD" "$RST"
        printf '    · installs for ALL users on this computer\n'
        printf '    · sets up background service that runs on boot\n'
        printf '    · no sudo password needed — you already have access\n\n'
        SUDO="sudo"
        WITH_SERVICE=1
    elif [[ "$PREFIX" != "$HOME/.local" ]] && [[ "$PREFIX" != "$HOME"/* ]]; then
        printf '  %s!%s can'\''t write to %s — switching to user-local install (no sudo needed)%s\n' "$YLW" "$RST" "$BIN_DIR" "$RST"
        printf '    · you can try it without sudo — just for you\n'
        printf '    · to install system-wide for everyone later:\n'
        printf '      %ssudo curl -sSL %s/install-%s.sh | bash%s\n\n' "$CYN" "$HUB" "$APP" "$RST"
        PREFIX="$HOME/.local"
        BIN_DIR="$PREFIX/bin"
        LIB_DIR="$PREFIX/lib/$APP"
        CLIENT_FILE="$LIB_DIR/${APP}-cli.py"
        WRAPPER="$BIN_DIR/$APP"
        WITH_SERVICE=0
    fi
fi

if [[ -z "$SUDO" ]]; then
    if [[ ! -w "$BIN_DIR" ]] || [[ ! -w "$(dirname "$LIB_DIR")" ]]; then
        _die "can't write to $BIN_DIR — run with sudo or use --prefix=\$HOME/.local"
    fi
    printf '  %s✓%s user-local install — just for you (no sudo needed)%s\n' "$GRN" "$RST" "$RST"
    printf '    · no sudo password needed — try it risk-free\n'
    printf '    · to help others on this machine later, run with sudo:\n'
    printf '      %ssudo curl -sSL %s/install-%s.sh | bash%s\n\n' "$CYN" "$HUB" "$APP" "$RST"
fi

# ── auto-experiment: pre-flight check before any install steps ─────────
if [[ "$ASSUME_YES" -eq 0 ]]; then
    printf '\n'
    printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"
    printf '  %s🔬 install experiment — pre-flight check before any changes%s\n' "$COMB" "$RST"
    printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"
    printf '\n'
    # run experiment in a subshell so its exit doesn't kill us; capture its rc
    exp_rc=0
    exp_log=$(mktemp "${TMPDIR:-/tmp}/hivv-preflight.XXXXXX") || exp_log=""
    if [[ -n "$exp_log" ]]; then
        ( experiment ) >"$exp_log" 2>&1 || exp_rc=$?
        sed 's/^/  /' "$exp_log"
        rm -f "$exp_log"
    else
        ( experiment ) || exp_rc=$?
    fi
    printf '\n'
    printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"
    if [[ $exp_rc -eq 0 ]]; then
        printf '  %s✓ Ready to install!%s\n' "$GRN" "$RST"
        printf '\n'
        printf '  %sTry the full install? [Y/n]%s ' "$BLD" "$RST"
        read -r ans </dev/tty 2>/dev/null || ans="y"
        [[ "$ans" =~ ^[Nn]$ ]] && { printf '  no worries — run this again anytime with:\n'; printf '    curl -sSL %s/install-%s.sh | bash%s\n\n' "$HUB" "$APP" "$RST"; exit 0; }
    else
        printf '  %s⚠%s Some checks had warnings — the install may still work.%s\n' "$YLW" "$RST" "$RST"
        printf '\n'
        printf '  %sTry the full install anyway? [y/N]%s ' "$BLD" "$RST"
        read -r ans </dev/tty 2>/dev/null || ans="n"
        [[ "$ans" =~ ^[Yy]$ ]] || { printf '  aborted. Fix the issues above, then run again.\n\n'; exit 0; }
    fi
    printf '%s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"
    printf '\n'
fi

$SUDO mkdir -p "$BIN_DIR" "$LIB_DIR"

# download to a temp file, validate it's not an error page, then move into
# place with $SUDO when needed (dirs may be root-owned after sudo mkdir -p)
dl() {
    local url="$1" out="$2" tmp
    tmp=$(mktemp "${TMPDIR:-/tmp}/hivv-dl.XXXXXX") || return 1
    local retry=0
    local max_retry=2
    local cmd=""
    if need curl; then
        cmd="curl -fsSL --connect-timeout 15 --max-time 90 -o '$tmp' '$url'"
    else
        cmd="wget -q --timeout=90 -O '$tmp' '$url'"
    fi
    while [[ $retry -le $max_retry ]]; do
        if bash -c "$cmd" 2>/dev/null && [[ -s "$tmp" ]] \
           && ! head -1 "$tmp" 2>/dev/null | grep -qiE "<!DOCTYPE|<html"; then
            if [[ -n "$SUDO" ]]; then
                $SUDO cp "$tmp" "$out" && $SUDO chmod 0644 "$out" && rm -f "$tmp" && return 0
            else
                mv "$tmp" "$out" && return 0
            fi
        fi
        retry=$((retry + 1))
        if [[ $retry -le $max_retry ]]; then
            sleep 2
        fi
    done
    rm -f "$tmp"
    return 1
}

_spin "downloading ${HIVE}${APP}-cli.py${RST}"      dl "$HUB/${APP}-cli.py" "$CLIENT_FILE"
_spin "downloading ${HIVE}${APP}${RST} wrapper"      dl "$HUB/$APP"          "$WRAPPER"

# Verify downloads aren't HTML (catches CDN/proxy serving wrong content)
_verify_file() {
    local file="$1" label="$2"
    if head -1 "$file" 2>/dev/null | grep -qiE "<!DOCTYPE|<html"; then
        _die "$label is an HTML page, not the expected file. The server may be misconfigured. Try: curl -sSL $HUB/$APP -o $file"
    fi
}
_verify_file "$CLIENT_FILE" "CLI client"
_verify_file "$WRAPPER" "CLI wrapper"

# chmod
_spin "installing" bash -c "$SUDO chmod 0755 '$CLIENT_FILE' '$WRAPPER'"

# ── configure ollama to listen on network (0.0.0.0) if installed ────────
if command -v ollama >/dev/null 2>&1; then
    OVR_DIR="/etc/systemd/system/ollama.service.d"
    OVR_FILE="$OVR_DIR/hivv.conf"
    NEEDS_FIX=0
    if [[ -f /etc/systemd/system/ollama.service ]]; then
        if ! grep -q "OLLAMA_HOST=0.0.0.0" /etc/systemd/system/ollama.service 2>/dev/null; then
            NEEDS_FIX=1
        fi
    elif [[ -d /etc/systemd/system ]]; then
        NEEDS_FIX=1
    fi
    if [[ "$NEEDS_FIX" -eq 1 ]] && [[ -d /etc/systemd/system ]] && _can_sudo; then
        _log "configuring ollama to listen on network (0.0.0.0)..."
        sudo mkdir -p "$OVR_DIR" \
            && printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' | sudo tee "$OVR_FILE" > /dev/null \
            && sudo systemctl daemon-reload 2>/dev/null \
            && sudo systemctl restart ollama 2>/dev/null \
            && _say "ollama configured for network access" \
            || _warn "couldn't configure ollama — do it manually: sudo systemctl edit ollama"
    elif [[ "$NEEDS_FIX" -eq 1 ]] && [[ -d /etc/systemd/system ]]; then
        printf '  %s!%s ollama is localhost-only — to share your model with the hive run:\n' "$YLW" "$RST"
        printf '    %ssudo systemctl set-environment OLLAMA_HOST=0.0.0.0:11434 && sudo systemctl restart ollama%s\n' "$CYN" "$RST"
    fi
fi

# ── Tailscale setup (optional, for cross-network nodes) ────────────────
if ! command -v tailscale >/dev/null 2>&1; then
    if [[ -t 0 ]] || [[ "$ASSUME_YES" -eq 1 ]]; then
        printf '\n  %s🌐 Tailscale%s — secure VPN for nodes on different networks\n' "$BLD" "$RST"
        printf '    lets your masjid computer talk to your home computer safely\n'
        printf '    no ports opened to the internet — only your devices see each other\n\n'
        if [[ "$ASSUME_YES" -eq 1 ]]; then
            INSTALL_TS=1
        else
            printf '  %sInstall Tailscale? [y/N] %s' "$BLD" "$RST"
            read -r ans </dev/tty 2>/dev/null || ans="n"
            INSTALL_TS=0
            [[ "$ans" =~ ^[Yy] ]] && INSTALL_TS=1
        fi
        if [[ "$INSTALL_TS" -eq 1 ]]; then
            if ! _can_sudo && [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
                _warn "Tailscale needs admin rights — run manually: curl -fsSL https://tailscale.com/install.sh | sh"
            else
                _log "installing tailscale..."
                curl -fsSL https://tailscale.com/install.sh | sh 2>/dev/null || true
                if command -v tailscale >/dev/null 2>&1; then
                    _say "tailscale installed"
                    printf '  run: %ssudo tailscale up%s\n' "$CYN" "$RST"
                    printf '  then: %shivv firewall --apply --mode tailscale%s\n\n' "$CYN" "$RST"
                else
                    _warn "tailscale install failed — install manually: curl -fsSL https://tailscale.com/install.sh | sh"
                fi
            fi
        fi
    fi
else
    # Tailscale already installed — check if it's up
    TS_IP=$(tailscale ip -4 2>/dev/null || true)
    if [[ -n "$TS_IP" ]]; then
        printf '\n  %s✓ tailscale active%s — VPN IP: %s%s%s\n' "$GRN" "$RST" "$CYN" "$TS_IP" "$RST"
    else
        printf '\n  %s! tailscale installed but not running%s — run: %ssudo tailscale up%s\n' "$YLW" "$RST" "$CYN" "$RST"
    fi
fi

# ── background service — auto-start your node on boot ─────────────────
# Linux: systemd unit running the agent in the FOREGROUND (Type=simple) as
#        the invoking user, so state/pid land in that user's ~/.local/share.
# macOS: LaunchAgent per-user (no sudo needed).
SERVICE_INSTALLED=0
if [[ "$WITH_SERVICE" -eq 1 ]]; then
    if [[ "$(uname -s)" == "Linux" ]] && [[ -n "$SUDO" ]] && need systemctl && [[ -d /run/systemd/system ]]; then
        _log "spinning up your node — you're joining the hivv network 🐝"
        RUN_USER="${SUDO_USER:-$(id -un)}"
        RUN_HOME="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
        PY_ABS="$(command -v "$PYTHON_BIN")"
        [[ -n "$RUN_HOME" ]] && [[ -n "$PY_ABS" ]] || RUN_HOME=""
        if [[ -n "$RUN_HOME" ]]; then
            $SUDO mkdir -p "$RUN_HOME/.local/share/$APP"
            $SUDO chown "$RUN_USER" "$RUN_HOME/.local/share/$APP" 2>/dev/null || true
            $SUDO tee /etc/systemd/system/${APP}-agent.service > /dev/null <<UNIT
[Unit]
Description=hivv node agent — the people's distributed AI (runs as $RUN_USER)
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Environment=HOME=$RUN_HOME
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=$PY_ABS $CLIENT_FILE _agent
Restart=always
RestartSec=10
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
            $SUDO systemctl daemon-reload
            $SUDO systemctl enable --now ${APP}-agent \
                && _say "you're live 🐝 — your hardware is now part of a global AI" \
                && _say "service: ${APP}-agent.service (starts on boot, runs as $RUN_USER)" \
                && SERVICE_INSTALLED=1 \
                || _warn "service failed to start — check: $SUDO journalctl -u ${APP}-agent -n 50"
        else
            _warn "couldn't determine user home — service skipped. Start manually: $APP start"
        fi
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        # per-user LaunchAgent — no sudo required
        LA_DIR="$HOME/Library/LaunchAgents"
        LA_PLIST="$LA_DIR/org.hivv.agent.plist"
        PY_ABS="$(command -v "$PYTHON_BIN")"
        if [[ -n "$PY_ABS" ]]; then
            mkdir -p "$LA_DIR"
            cat > "$LA_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>org.hivv.agent</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY_ABS</string>
    <string>$CLIENT_FILE</string>
    <string>_agent</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.local/share/$APP/agent.log</string>
  <key>StandardErrorPath</key><string>$HOME/.local/share/$APP/agent.log</string>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
PLIST
            launchctl bootout "gui/$(id -u)/org.hivv.agent" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u)" "$LA_PLIST" 2>/dev/null \
                && _say "you're live 🐝 — LaunchAgent starts your node on login" \
                && SERVICE_INSTALLED=1 \
                || { launchctl load -w "$LA_PLIST" 2>/dev/null && SERVICE_INSTALLED=1 && _say "you're live 🐝 — LaunchAgent starts your node on login"; }
        fi
    elif [[ "$WITH_SERVICE" -eq 1 ]] && [[ "$SERVICE_INSTALLED" -eq 0 ]]; then
        printf '  %s!%s no background service set up — start your node with: %s%s start%s\n' "$YLW" "$RST" "$CYN" "$APP" "$RST"
        printf '    %s(it also works fine to just run hivv start whenever you like)%s\n' "$DIM" "$RST"
    fi
fi

# ── PATH handling ─────────────────────────────────────────────────────
NEEDS_PATH=0
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then NEEDS_PATH=1; fi

# Detect rc file
RC_FILE=""
if [[ "$NEEDS_PATH" -eq 1 ]] && [[ "$NO_PATH" -eq 0 ]]; then
    case "${SHELL:-/bin/bash}" in
        */zsh)  RC_FILE="$HOME/.zshrc" ;;
        */fish) RC_FILE="$HOME/.config/fish/config.fish" ;;
        *)      RC_FILE="$HOME/.bashrc" ;;
    esac
fi

# ── success card ──────────────────────────────────────────────────────
SUCCESS_BAR="${GRN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
printf '\n  %s\n' "$SUCCESS_BAR"
printf '  %s%s✓ %s CLI installed — %s%s\n' "$BLD" "$GRN" "$APP" "$([ -n "$SUDO" ] && echo "SYSTEM-WIDE (helps everyone)" || echo "user-local (just for you)")" "$RST"
printf '  %s\n\n' "$SUCCESS_BAR"

printf '  %s🌌 how hivv works — 30-second tour%s\n' "$BLD" "$RST"
printf '    · the %s%s hub%s is just a phone book — it knows who is online, nothing more%s\n' "$HIVE" "$BLD" "$RST" "$RST"
printf '    · your computer is a %snode%s — the agent heartbeats to the hub, peers talk direct%s\n' "$BLD" "$RST"
printf '    · AI requests route to the best available brain: your local model, a peer, or a donated cloud key%s\n' "$RST"
printf '    · your chats/data stay between nodes — the hub never sees prompt content%s\n' "$RST"
printf '    · opt-in sharing: %s%s volunteer on%s lets the hive queue jobs on your idle CPU/GPU%s\n' "$CYN" "$APP" "$RST" "$RST"
printf '\n'
printf '  %slocation%s\n' "$BLD" "$RST"
printf '    wrapper   %s%s%s\n' "$CYN" "$WRAPPER" "$RST"
printf '    client    %s%s%s\n' "$CYN" "$CLIENT_FILE" "$RST"
printf '    config    %s%s/%s/%s%s\n' "$DIM" "$HOME" ".config" "$APP" "$RST"
printf '    state     %s%s/%s/share/%s%s\n' "$DIM" "$HOME" ".local" "$APP" "$RST"
if [[ "$SERVICE_INSTALLED" -eq 1 ]] && [[ "$(uname -s)" == "Linux" ]]; then
printf '    service   %s/etc/systemd/system/%s-agent.service%s  (journalctl -u %s-agent)\n' "$CYN" "$APP" "$RST" "$APP"
elif [[ "$SERVICE_INSTALLED" -eq 1 ]] && [[ "$(uname -s)" == "Darwin" ]]; then
printf '    service   %s~/Library/LaunchAgents/org.hivv.agent.plist%s  (see: %s logs)\n' "$CYN" "$RST" "$APP"
fi
printf '\n'

printf '  %sfirst steps — try these right now%s\n' "$BLD" "$RST"
printf '    %s$%s %s%s up%s                   quick status check (is my node alive?)\n'   "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s hi%s                   say hello to the hive 🐝\n'                  "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s chat "hello"%s         talk to the distributed AI\n'                "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s nodes%s                see every bee online right now\n'            "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s welcome%s              guided tour of everything hivv can do\n\n'    "$DIM" "$RST" "$BLD" "$APP" "$RST"

printf '  %srunning your node 🐝%s\n' "$BLD" "$RST"
if [[ "$SERVICE_INSTALLED" -eq 1 ]]; then
printf '    ✓ your node %salready started%s and will come back after reboot — nothing to do.\n' "$GRN" "$RST"
else
printf '    %s$%s %s%s start%s                go live — your CPU/GPU joins the hive\n' "$DIM" "$RST" "$BLD" "$APP" "$RST"
case "$(uname -s)" in
  Linux)
printf '      %s(want it to start on boot? %s doctor --fix — it sets up a systemd service)%s\n' "$DIM" "$APP" "$RST" ;;
  Darwin)
printf '      %s(macOS has no systemd: %s doctor --fix creates a LaunchAgent, or add `hivv start` to Login Items)%s\n' "$DIM" "$APP" "$RST" ;;
  *)
printf '      %s(want auto-start on this OS? %s doctor --fix does its best)%s\n' "$DIM" "$APP" "$RST" ;;
esac
fi
printf '    %s$%s %s%s volunteer on%s         enable running AI jobs from the hive (opt-in)\n' "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s llm status%s           check your local AI setup (open weights)\n' "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s firewall%s              see who can reach your node (localhost by default)\n\n' "$DIM" "$RST" "$BLD" "$APP" "$RST"

printf '  %swhen you need help%s\n' "$BLD" "$RST"
printf '    %s$%s %s%s doctor%s                  full health report      %s$%s %s%s support%s     ask the AI (attaches diagnostics)\n' "$DIM" "$RST" "$BLD" "$APP" "$RST" "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s doctor --fix%s            auto-fix common issues  %s$%s %s%s debug%s      everything at once for bug reports\n\n' "$DIM" "$RST" "$BLD" "$APP" "$RST" "$DIM" "$RST" "$BLD" "$APP" "$RST"

printf '  %ssee the network%s\n' "$BLD" "$RST"
printf '    view all bees · watch the hive collaborate in real-time\n'
printf '    → %shivv.org%s (live command center: map, graph, charts, chat)\n\n' "$CYN" "$RST"

# ── optional: install LLM backend (llama.cpp preferred, ollama alternative) ─
should_install_llm() {
  [[ "$WITH_LLM" -eq  1 ]] && return 0
  [[ "$WITH_LLM" -eq -1 ]] && return 1
  [[ -t 0 ]] || return 1
  printf '  %sAlso install a local LLM backend? [y/N] %s' "$BLD" "$RST"
  local ans=""
  read -r ans </dev/tty || return 1
  [[ "$ans" =~ ^[Yy] ]]
}

if should_install_llm; then
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) export PATH="$BIN_DIR:$PATH" ;;
  esac

  printf '\n  %s%s🤖 local AI setup — your own open-weight model%s\n\n' "$BLD" "$HIVE" "$RST"
  printf '  %sopen models · open weights · your data stays on your machine · no big-tech required%s\n' "$DIM" "$RST"
  printf '\n'
  printf '    %s1)%s llama.cpp  %s← preferred — open weights · efficient · handles many requests%s\n' "$GRN" "$RST" "$DIM" "$RST"
  printf '    %s2)%s Ollama      %s← alternative — simple but one session at a time%s\n\n' "$CYN" "$RST" "$DIM" "$RST"

  HAVE_LLAMA=0
  HAVE_OLLAMA=0
  LLAMA_BIN=""

  if command -v llama-cli >/dev/null 2>&1 || command -v llama-server >/dev/null 2>&1 || command -v llama >/dev/null 2>&1; then
    HAVE_LLAMA=1
    _say "llama.cpp already installed"
  fi

  if command -v ollama >/dev/null 2>&1; then
    HAVE_OLLAMA=1
    _say "ollama already installed"
  fi

  # If they have ollama but not llama.cpp — warn about limitations
  if [[ "$HAVE_OLLAMA" -eq 1 ]] && [[ "$HAVE_LLAMA" -eq 0 ]]; then
    printf '\n  %s⚠️  ollama detected — note:%s\n' "$YLW$BLD" "$RST"
    printf '    · ollama runs %sone session at a time%s — hivv handles many concurrent requests\n' "$YLW" "$RST"
    printf '    · ollama uses more memory than llama.cpp for the same model\n'
    printf '    · llama.cpp is what ollama runs under the hood anyway\n'
    printf '    · hivv will use ollama if no llama.cpp is found, but switching is easy\n\n'
    printf '  %sWant to install llama.cpp too? [y/N] %s' "$BLD" "$RST"
    ans="n"
    read -r ans </dev/tty || ans="n"
    if [[ ! "$ans" =~ ^[Yy] ]]; then
      HAVE_OLLAMA=1
      HAVE_LLAMA=0
    else
      HAVE_OLLAMA=0
    fi
  fi

  if [[ "$HAVE_LLAMA" -eq 0 ]] && [[ "$HAVE_OLLAMA" -eq 0 ]]; then
    printf '\n  %s📦%s installing llama.cpp for %s%s%s...\n\n' "$BLD" "$HIVE" "$CYN" "$(uname -s)" "$RST"
    ARCH=$(uname -m)
    ASSET=""
    case "$(uname -s)" in
      Linux)
        case "$ARCH" in
          x86_64)  ASSET="llama-linux-x64.tar.gz" ;;
          aarch64|arm64) ASSET="llama-linux-arm64.tar.gz" ;;
          *) _warn "unsupported arch: $ARCH — download from https://github.com/ggerganov/llama.cpp/releases";;
        esac
        if [[ -n "$ASSET" ]]; then
          LLAMA_TMP=$(mktemp -d /tmp/hivv-llama.XXXXXX)
          if _spin "downloading llama.cpp" curl -fsSL --connect-timeout 15 --max-time 300 "https://github.com/ggerganov/llama.cpp/releases/latest/download/$ASSET" -o "$LLAMA_TMP/llama.tar.gz"; then
            tar -xzf "$LLAMA_TMP/llama.tar.gz" -C "$LLAMA_TMP" 2>/dev/null || true
            LLAMA_BIN=$(find "$LLAMA_TMP" -name 'llama-cli' -o -name 'llama-server' 2>/dev/null | head -1)
            if [[ -n "$LLAMA_BIN" ]] && [[ -f "$LLAMA_BIN" ]]; then
              $SUDO cp "$LLAMA_BIN" "$BIN_DIR/llama" && $SUDO chmod 0755 "$BIN_DIR/llama"
              _say "installed ${CYN}llama.cpp${RST} → ${CYN}$BIN_DIR/llama${RST}"
              HAVE_LLAMA=1
            else
              _warn "no llama binary in archive — download from https://github.com/ggerganov/llama.cpp/releases"
            fi
            rm -rf "$LLAMA_TMP"
          else
            rm -rf "$LLAMA_TMP"
          fi
        fi
        ;;
      Darwin)
        if need brew; then
          if brew list --formula 2>/dev/null | grep -qE '^(llama\.cpp|llama| llama\.cpp)$'; then
            HAVE_LLAMA=1
            _say "llama.cpp via homebrew"
          else
            _log "brew install llama.cpp (this can take a minute)..."
            if brew install llama.cpp; then
              HAVE_LLAMA=1
              _say "installed llama.cpp via homebrew"
            else
              _warn "brew install failed — or download from https://github.com/ggerganov/llama.cpp/releases"
            fi
          fi
        else
          printf '  %s!%s on macOS install via Homebrew:\n' "$YLW" "$RST"
          printf '    %sbrew install llama.cpp%s   or download: https://github.com/ggerganov/llama.cpp/releases\n' "$CYN" "$RST"
          printf '    %sno Homebrew? get it: /bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"%s\n' "$DIM" "$RST"
        fi
        ;;
      *)
        _warn "unsupported platform — download from https://github.com/ggerganov/llama.cpp/releases"
        ;;
    esac
  fi

  SUGGEST_JSON=$("$APP" llm suggest --json 2>/dev/null || true)
  LLM_MODEL=$(printf '%s' "$SUGGEST_JSON" \
    | "$PYTHON_BIN" -c 'import json,sys; print(json.load(sys.stdin).get("primary") or "gemma4:e2b")' 2>/dev/null \
    || echo "gemma4:e2b")
  GGUF_URL=$(printf '%s' "$SUGGEST_JSON" \
    | "$PYTHON_BIN" -c 'import json,sys; g=json.load(sys.stdin).get("gguf") or {}; print(g.get("url",""))' 2>/dev/null || true)
  GGUF_FILE=$(printf '%s' "$SUGGEST_JSON" \
    | "$PYTHON_BIN" -c 'import json,sys; g=json.load(sys.stdin).get("gguf") or {}; print(g.get("file",""))' 2>/dev/null || true)

  if [[ "$HAVE_LLAMA" -eq 1 ]]; then
    printf '\n  %s🌐%s downloading %s — your contribution to the hive%s\n\n' "$BLD" "$HIVE" "$LLM_MODEL" "$RST"
    MODEL_DIR="${HOME}/.local/share/${APP}/models"
    mkdir -p "$MODEL_DIR"
    if [[ -n "$GGUF_URL" && -n "$GGUF_FILE" ]]; then
      if _spin "downloading ${CYN}${GGUF_FILE}${RST}" curl -fsSL --connect-timeout 30 --max-time 1800 "$GGUF_URL" -o "${MODEL_DIR}/${GGUF_FILE}"; then
        _say "model ready — ${APP} will use it for local AI tasks"
        printf '\n  %s✓%s serve it:  %s%s llm serve-llama %s%s\n' "$GRN" "$RST" "$CYN" "$APP" "$LLM_MODEL" "$RST"
        printf '  %s✓%s try:     %s%s chat "hello" --local%s\n' "$GRN" "$RST" "$CYN" "$APP" "$RST"
      else
        _warn "model download failed — retry later: ${APP} llm pull-gguf ${LLM_MODEL}"
      fi
    else
      _warn "no GGUF mapping for ${LLM_MODEL} — pick one: ${APP} llm pull-gguf gemma4:e2b"
    fi
    printf '  %s✓%s full check:  %s%s llm status%s\n\n' "$GRN" "$RST" "$CYN" "$APP" "$RST"

  elif [[ "$HAVE_OLLAMA" -eq 1 ]]; then
    printf '\n  %s🌐%s pulling %s via ollama — one moment%s\n\n' "$BLD" "$CYN" "$LLM_MODEL" "$RST"
    if ollama pull "$LLM_MODEL" 2>/dev/null; then
      _say "model ready — ${APP} will use it for local AI tasks"
    else
      _warn "pull failed — retry with: ollama pull ${LLM_MODEL}"
    fi
    printf '\n  %s✓%s try:  %s%s chat "hello" --local%s\n' "$GRN" "$RST" "$CYN" "$APP" "$RST"
    printf '  %s✓%s full check:  %s%s llm status%s\n\n' "$GRN" "$RST" "$CYN" "$APP" "$RST"
  else
    printf '\n  %s💤%s no local AI — ${APP} will use the API gateway (still works great)%s\n\n' "$YLW" "$BLD" "$RST"
    printf '  %sto enable local AI later:%s\n' "$BLD" "$RST"
    printf '    curl -sSL %s/install-%s.sh | bash -s -- --with-llm\n\n' "$HUB" "$APP"
  fi
fi

# ── PATH advice ──────────────────────────────────────────────────────
if [[ "$NEEDS_PATH" -eq 1 ]]; then
    if [[ "$NO_PATH" -eq 1 ]]; then
        :  # user said don't mention it
    elif [[ "$ASSUME_YES" -eq 1 ]] && [[ -n "$RC_FILE" ]] && [[ -w "$(dirname "$RC_FILE")" ]]; then
        printf 'export PATH="%s:$PATH"\n' "$BIN_DIR" >> "$RC_FILE"
        _say "added ${CYN}$BIN_DIR${RST} to ${CYN}$RC_FILE${RST}"
        printf '       %s(restart your shell or: source %s)%s\n' "$DIM" "$RC_FILE" "$RST"
    else
        printf '  %snote%s\n' "$YLW$BLD" "$RST"
        printf '    %s$BIN_DIR is not on your PATH%s\n' "$YLW" "$RST"
        printf '    add it for this shell:\n'
        printf '      %sexport PATH="%s:$PATH"%s\n' "$CYN" "$BIN_DIR" "$RST"
        printf '    or reinstall user-local (no sudo needed):\n'
        printf '      %scurl -sSL %s/install-%s.sh | bash -s -- --prefix=$HOME/.local%s\n' "$CYN" "$HUB" "$APP" "$RST"
    fi
fi

# ── post-install verification ──────────────────────────────────────────
printf '\n'
printf '  %s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"
printf '  %s  🌌 VERIFICATION — making sure everything is connected 🌌%s\n' "$BLD" "$RST"
printf '  %s━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━%s\n' "$COMB" "$RST"

# Check hub is reachable
HUB_OK=0
if need curl; then
    if curl -fsSL --connect-timeout 3 --max-time 5 "$HUB/api/health" > /dev/null 2>&1; then
        HUB_OK=1
    fi
elif need wget; then
    if wget -q --timeout=5 -O - "$HUB/api/health" > /dev/null 2>&1; then
        HUB_OK=1
    fi
fi

if [[ "$HUB_OK" -eq 1 ]]; then
    printf '  %s✓%s hivv.org is reachable — the hivv network is alive!\n' "$GRN" "$RST"
    if [[ "$SERVICE_INSTALLED" -eq 1 ]]; then
        printf '  %s✓%s background service enabled — your node helps even while you sleep\n' "$GRN" "$RST"
    fi
    if [[ -n "$SUDO" ]]; then
        printf '  %s✓%s system-wide install — you are helping ALL users on this machine\n' "$GRN" "$RST"
        printf '  %s🌌 WOW! your computer is now part of the hivv distributed AI network!%s\n' "$GRN$BLD" "$RST"
        printf '  %severyone on this machine benefits · your CPU/GPU is contributing to solving Earth'\''s hardest problems!%s\n\n' "$COMB" "$RST"
    else
        printf '  %s✓%s you are now connected — ready to help on this machine\n' "$GRN" "$RST"
        printf '\n'
        printf '  %s🌌 you'\''re connected — HIVV is live on your machine! 🌌🐝%s\n' "$GRN$BLD" "$RST"
        printf '\n'
        printf '  %s⚡ next steps to do MORE good:%s\n' "$BLD" "$RST"
        printf '  %s1)%s go live now:   %s%s start%s\n' "$YLW" "$RST" "$CYN" "$APP" "$RST"
        printf '  %s2)%s help everyone on this machine — system-wide install:\n' "$YLW" "$RST"
        printf '    %ssudo curl -sSL %s/install-%s.sh | bash%s\n' "$CYN" "$HUB" "$APP" "$RST"
        printf '  %s3)%s bring your own GPU — local open models on your hardware:\n' "$YLW" "$RST"
        printf '    %scurl -sSL %s/install-%s.sh | bash -s -- --with-llm --yes%s\n\n' "$CYN" "$HUB" "$APP" "$RST"
    fi
    printf '  %s🌐 see your node on the network:%s\n' "$BLD" "$RST"
    printf '    %shttps://hivv.org%s\n' "$CYN" "$RST"
    printf '    watch the hive collaborate in real-time · see what everyone is working on\n\n'
else
    printf '  %s!%s hivv.org not reachable — check your internet connection\n' "$YLW" "$RST"
    printf '  %shivv is installed and ready — it will connect automatically when online%s\n' "$DIM" "$RST"
    printf '\n'
fi

# ── footer ────────────────────────────────────────────────────────────
printf '\n'
printf '  %s🌌%s  distributed AI working for Earth — %s.org%s\n' \
    "$COMB" "$RST" "$APP" "$RST"
printf '  %sstar us on github · report bugs · help us build the future 🐝%s\n\n' \
    "$DIM" "$RST"
