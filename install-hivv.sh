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
HUB="${HIVV_HUB:-https://hivv.org}"   # env-overridable (offline tests, self-host)
# THE FALLBACK SOURCE. hivv.org serves the CLI and the wrapper, so it is a
# single point of failure for the install itself. The GitHub repo has the
# same files under public_html/ — ONE extra request, only when the hub does
# not answer, only because a human ran the installer. The automatic update
# path never touches GitHub: that is the flood law.
FALLBACK_RAW="${HIVV_FALLBACK_RAW:-https://raw.githubusercontent.com/htmlfarmer/hivv/main/public_html}"
PREFIX="/usr/local"
DO_UNINSTALL=0
DO_EXPERIMENT=0
NO_PATH=0
ASSUME_YES=0
HIVV_MODE="auto"   # auto | system | user — explicit install scope (2026-11-18 rethink)
QUIET=0
WITH_LLM=0   # 0 = auto-prompt if interactive, 1 = yes, -1 = no
WITH_GIT=0   # 0 = auto-prompt if interactive, 1 = yes, -1 = no (git + gh)
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
        --with-git) WITH_GIT=1        ; shift ;;
        --no-git)   WITH_GIT=-1       ; shift ;;
        --mode=*)  HIVV_MODE="${1#*=}"  ; shift ;;
        --mode)    HIVV_MODE="$2"      ; shift 2 ;;
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
              "  --mode=X       X = auto (default) | system (sudo, all users) | user (~/.local)" \
              "  --yes          no prompts; auto-add BIN_DIR to PATH (for scripts)" \
              "  --with-llm     also install llama.cpp + a recommended open model" \
              "  --no-llm       skip the LLM prompt" \
              "  --no-service   skip the boot-time background service" \
              "  --with-git     also install git + the GitHub CLI (to contribute)" \
              "  --no-git       skip the contributor-tools prompt" \
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
# need <tool> — is it on PATH? "cxx" is VIRTUAL: ANY C++ compiler counts
# (g++/c++/clang++), because the PACKAGE differs per manager while the
# ability to compile does not (wahi 2026-11-25: configure died with
# "No CMAKE_CXX_COMPILER could be found" — git+cmake had passed the gate).
need() {
    case "$1" in
        cxx) command -v g++ >/dev/null 2>&1 || command -v c++ >/dev/null 2>&1 \
             || command -v clang++ >/dev/null 2>&1 ;;
        *)   command -v "$1" >/dev/null 2>&1 ;;
    esac
}

# _pkg_of <pm> <tool> — package name for a tool key on this manager
_pkg_of() {
    if [[ "$2" == "cxx" ]]; then
        case "$1" in
            dnf|yum|zypper) echo "gcc-c++" ;;
            pacman|brew)    echo "gcc" ;;
            *)              echo "g++" ;;
        esac
    elif [[ "$2" == "gh" ]]; then
        # The GitHub CLI is spelled "github-cli" on some distros.
        case "$1" in
            pacman|apk|zypper) echo "github-cli" ;;
            winget) echo "GitHub.cli" ;;
            *) echo "gh" ;;
        esac
    elif [[ "$2" == "gh" ]]; then
        # The GitHub CLI is spelled "github-cli" on some distros.
        case "$1" in
            pacman|apk|zypper) echo "github-cli" ;;
            winget) echo "GitHub.cli" ;;
            *) echo "gh" ;;
        esac
    else
        echo "$2"
    fi
}

# _tool_label <tool> — human name for a tool key
_tool_label() {
    case "$1" in
        cxx) echo "c++ compiler" ;;
        gh)  echo "GitHub CLI" ;;
        *)   echo "$1" ;;
    esac
}
_can_sudo() { sudo -n true 2>/dev/null; }

# _can_ask_sudo — a HUMAN is watching this install: safe to let sudo ask
# THEM for their password (that's how apt/dnf installs normally work).
# Watch STDOUT, not stdin: the canonical invocation is `curl … | bash`,
# where stdin is the pipe but a person watches stdout. Never in CI or
# with HIVV_NO_SUDO_PROMPT=1 — a prompt with nobody behind it hangs.
# sudo writes its prompt to /dev/tty (never our streams) and fails fast
# with no tty, so an unattended run can't stall here either way.
_can_prompt_sudo() {
    [[ -z "${CI:-}" && -z "${HIVV_NO_SUDO_PROMPT:-}" ]] || return 1
    [[ -t 1 ]] || return 1
    command -v sudo >/dev/null 2>&1
}

# ── build tools (git + cmake) — NEVER a dead end ──────────────────────
# THE FAILURE THIS FIXES
# ----------------------
# 2026-11-21 (asher@wahi): `hivv install llm` on a box with no git died with
#   FileNotFoundError: [Errno 2] No such file or directory: 'git'
# — a Python traceback mid-install with no way forward. A missing tool must
# be AUTO-INSTALLED when we safely can, and otherwise the EXACT copy-paste
# command must be printed. Same contract as scripts/lib/deps.sh.
#
# Never prompts for a password (`sudo -n` only) — a prompt hangs CI and
# unattended installs.
_pkg_mgr() {
    if   need apt-get; then echo apt
    elif need dnf;     then echo dnf
    elif need yum;     then echo yum
    elif need pacman;  then echo pacman
    elif need apk;     then echo apk
    elif need zypper;  then echo zypper
    elif need brew;    then echo brew
    else echo none
    fi
}

_pkg_install_cmd() {
    local pm="$1"; shift
    case "$pm" in
        apt)    echo "apt-get install -y $*" ;;
        dnf)    echo "dnf install -y $*" ;;
        yum)    echo "yum install -y $*" ;;
        pacman) echo "pacman -S --noconfirm $*" ;;
        apk)    echo "apk add --no-cache $*" ;;
        zypper) echo "zypper --non-interactive install $*" ;;
        brew)   echo "brew install $*" ;;
        *)      echo "" ;;
    esac
}

_pkg_manual_hint() {
    local pm="$1"; shift
    case "$pm" in
        apt)    echo "sudo apt-get update && sudo apt-get install -y $*" ;;
        dnf)    echo "sudo dnf install -y $*" ;;
        yum)    echo "sudo yum install -y $*" ;;
        pacman) echo "sudo pacman -S --noconfirm $*" ;;
        apk)    echo "sudo apk add $*" ;;
        zypper) echo "sudo zypper --non-interactive install $*" ;;
        brew)   echo "brew install $*" ;;
        *)      echo "" ;;
    esac
}

# ensure_build_tools <tool> [tool...]
#   Returns 0 when every tool is present afterwards.
#   Auto-installs via the detected package manager; on failure prints the
#   EXACT command to run and returns 1. NEVER exits, NEVER hangs.
ensure_build_tools() {
    local missing=() pkgs=() names=() all_pkgs=() t pm cmd hint sudo_cmd="" note="" attempt=0
    # The label names the KIND of tool, so the same machinery installs the
    # compiler toolchain ("build tool") and the contributor tools
    # ("contributor tool") without a second copy of the escalation ladder.
    local label="${_TOOLS_LABEL:-build tool}"
    for t in "$@"; do
        need "$t" || missing+=("$t")
    done
    [[ ${#missing[@]} -eq 0 ]] && return 0

    pm="$(_pkg_mgr)"
    # tool keys -> package names (cxx: g++/gcc-c++/gcc) and -> human labels
    # (cxx: "c++ compiler") — the user is told what's missing in words, and
    # the package manager in ITS spelling (wahi 2026-11-25: configure died
    # with "No CMAKE_CXX_COMPILER could be found" — git+cmake had passed).
    for t in "${missing[@]}"; do
        pkgs+=("$(_pkg_of "$pm" "$t")")
        names+=("$(_tool_label "$t")")
    done
    for t in "$@"; do
        all_pkgs+=("$(_pkg_of "$pm" "$t")")
    done
    if [[ "$pm" != "none" ]]; then
        # escalation ladder — decided BEFORE printing "installing…", so the
        # claim and the attempt can never disagree (wahi 2026-11-24: the old
        # code printed the claim, then password-only sudo made the attempt a
        # silent no-op and the user saw "installing…" → "✗ missing"):
        #   brew/root → plain · passwordless sudo → sudo -n ·
        #   a human watching → plain sudo (it asks THEM) · otherwise → hint
        if [[ "$pm" == "brew" || "$(id -u)" -eq 0 ]]; then
            attempt=1
        elif _can_sudo; then
            sudo_cmd="sudo -n"; attempt=1
        elif _can_prompt_sudo; then sudo_cmd="sudo"; attempt=1; note=" — sudo will ask for your password"
        fi

        if [[ "$attempt" -eq 1 ]]; then
            printf '  %s→%s installing %s(s): %s (via %s)%s…\n' "$BLU" "$RST" "$label" "${names[*]}" "$pm" "$note"
            if [[ "$pm" == "apt" ]]; then $sudo_cmd apt-get update >/dev/null 2>&1 || true; fi
            cmd="$(_pkg_install_cmd "$pm" "${pkgs[@]}")"
            if [[ -n "$cmd" ]]; then
                if $sudo_cmd $cmd >/dev/null 2>&1; then
                    missing=()
                    for t in "$@"; do need "$t" || missing+=("$t"); done
                    if [[ ${#missing[@]} -eq 0 ]]; then
                        printf '  %s✓%s installed %s(s): %s\n' "$GRN" "$RST" "$label" "$*"
                        return 0
                    fi
                fi
            fi
        fi
    fi

    hint="$(_pkg_manual_hint "$pm" "${all_pkgs[@]}")"
    printf '  %s!%s missing %s(s): %s\n' "$YLW" "$RST" "$label" "${names[*]}" >&2
    if [[ -n "$hint" ]]; then
        printf '    %sinstall them:%s %s\n' "$DIM" "$RST" "$hint" >&2
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        # A Mac with NO Homebrew has no package manager to name, so "with your
        # package manager" below is a dead end — and it asks for git, a C++
        # compiler and make, which this Mac already has (Xcode Command Line
        # Tools). Only cmake was missing, and there was no way to get it.
        # Same wall the CLI hit (2026-11-28).
        printf '    %sthis Mac needs cmake — git, your C++ compiler and make are already installed%s\n' "$DIM" "$RST" >&2
        printf '    %sinstall Homebrew, then cmake:%s\n' "$DIM" "$RST" >&2
        printf '      %s/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"%s\n' "$CYN" "$RST" >&2
        printf '      %sbrew install cmake%s\n' "$CYN" "$RST" >&2
        printf '    %s…or skip the build entirely — llama.cpp ships prebuilt macOS binaries:%s\n' "$DIM" "$RST" >&2
        printf '      %shivv llm install-llama --auto%s\n' "$CYN" "$RST" >&2
    else
        printf '    %sinstall %s with your package manager, then re-run this command%s\n' "$DIM" "${names[*]}" "$RST" >&2
    fi
    return 1
}

# ensure_dev_tools — git + the GitHub CLI, for CONTRIBUTING to the source.
# Neither is needed to RUN hivv; both are needed to open a pull request.
# Same contract as the build tools: auto-install when we safely can, else
# print the EXACT command. Never exits, never hangs.
ensure_dev_tools() {
    local have_git=0 have_gh=0
    need git && have_git=1
    need gh   && have_gh=1
    if [[ "$have_git" -eq 1 && "$have_gh" -eq 1 ]]; then
        printf '  %s\u2713%s contributor tools present (git, gh)\n' "$GRN" "$RST"
        return 0
    fi
    _TOOLS_LABEL="contributor tool"
    ensure_build_tools git gh
    local rc=$?
    _TOOLS_LABEL=""
    return $rc
}

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
# ── Python is required for the CLI. Rather than dying with "install it
#    first", we try to install it ourselves for the detected OS and, if that
#    fails, print the exact command to type for THAT machine.
PYTHON_BIN=""
for p in python3 python; do
    if need "$p"; then PYTHON_BIN="$p"; break; fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    _log "python3 not found — installing it for you…"
    _install_python_auto() {
        local os pkg
        os="$(uname -s 2>/dev/null || echo unknown)"
        pkg="none"
        case "$os" in
            Linux)
                if   need apt-get; then pkg="apt"
                elif need dnf;     then pkg="dnf"
                elif need yum;     then pkg="yum"
                elif need pacman;  then pkg="pacman"
                elif need apk;     then pkg="apk"
                elif need zypper;  then pkg="zypper"
                fi
                ;;
            Darwin) pkg="brew" ;;
        esac
        local SUDO=""
        if [[ "$(id -u)" -ne 0 ]] && need sudo && sudo -n true 2>/dev/null; then SUDO="sudo"; fi
        case "$pkg" in
            apt)
                $SUDO apt-get update >/dev/null 2>&1 || true
                $SUDO apt-get install -y python3 >/dev/null 2>&1 ;;
            dnf)    $SUDO dnf install -y python3 >/dev/null 2>&1 ;;
            yum)    $SUDO yum install -y python3 >/dev/null 2>&1 ;;
            pacman) $SUDO pacman -S --noconfirm python >/dev/null 2>&1 ;;
            apk)    $SUDO apk add --no-cache python3 >/dev/null 2>&1 ;;
            zypper) $SUDO zypper --non-interactive install python3 >/dev/null 2>&1 ;;
            brew)   brew install python3 >/dev/null 2>&1 ;;
        esac
    }
    _install_python_auto || true
    for p in python3 python; do
        if need "$p"; then PYTHON_BIN="$p"; break; fi
    done
fi

if [[ -z "$PYTHON_BIN" ]]; then
    printf '\n%sPython 3 is required for the hivv CLI and could not be installed automatically.%s\n\n' "$RED$BLD" "$RST" >&2
    printf '  Install it with one of these, then re-run:\n\n' >&2
    case "$(uname -s 2>/dev/null)" in
        Linux)
            if   need apt-get; then printf '    %ssudo apt-get update && sudo apt-get install -y python3%s\n' "$CYN" "$RST" >&2
            elif need dnf;     then printf '    %ssudo dnf install -y python3%s\n' "$CYN" "$RST" >&2
            elif need yum;     then printf '    %ssudo yum install -y python3%s\n' "$CYN" "$RST" >&2
            elif need pacman;  then printf '    %ssudo pacman -S --noconfirm python%s\n' "$CYN" "$RST" >&2
            elif need apk;     then printf '    %ssudo apk add python3%s\n' "$CYN" "$RST" >&2
            else                    printf '    %s# install Python 3.8+ from https://www.python.org/downloads/%s\n' "$CYN" "$RST" >&2
            fi ;;
        Darwin)
            printf '    %sbrew install python3%s\n' "$CYN" "$RST" >&2 ;;
        *)
            printf '    %s# install Python 3.8+ from https://www.python.org/downloads/%s\n' "$CYN" "$RST" >&2 ;;
    esac
    printf '\n'
    exit 1
fi
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

# ── install mode (first-class) ───────────────────────────────────────
case "$HIVV_MODE" in
  system)
    PREFIX="/usr/local"; BIN_DIR="$PREFIX/bin"; LIB_DIR="$PREFIX/lib/$APP"
    CLIENT_FILE="$LIB_DIR/${APP}-cli.py"; WRAPPER="$BIN_DIR/$APP"
    if [[ "$EUID" -eq 0 ]]; then SUDO=""
    elif _can_sudo; then SUDO="sudo"
    else _die "--mode=system needs sudo — rerun with: sudo curl -sSL $HUB/install-$APP.sh | bash -s -- --mode=system"
    fi
    WITH_SERVICE=1
    ;;
  user)
    # user mode = ~/.local + ALWAYS a per-user autostart service (the rethink:
    # user installs used to get NO service → second step via doctor --fix)
    PREFIX="$HOME/.local"; BIN_DIR="$PREFIX/bin"; LIB_DIR="$PREFIX/lib/$APP"
    CLIENT_FILE="$LIB_DIR/${APP}-cli.py"; WRAPPER="$BIN_DIR/$APP"
    SUDO=""
    WITH_SERVICE=1
    ;;
  auto) ;;
  *) _die "--mode must be auto, system or user (got: $HIVV_MODE)" ;;
esac

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
        # ALWAYS install the per-user service here too — the same rule as
        # --mode=user ("user installs used to get NO service → second step
        # via doctor --fix" was the old gap). The auto-switch lands files in
        # ~/.local just like user mode, so it must behave like user mode:
        # wahi installed fine on 2026-11-23 and was STILL stuck because this
        # branch deferred the service AND the PATH handoff was never warned.
        WITH_SERVICE=1
    fi
fi

if [[ -z "$SUDO" ]]; then
    # A directory that does NOT EXIST yet is not "not writable" — it is
    # "not created yet". `-w` says false for a missing dir, so a fresh
    # ~/.local install died with "can't write to ~/.local/bin" even though
    # ~/.local is perfectly writable (the mkdir above only ran for the
    # pre-mode PREFIX).
    mkdir -p "$BIN_DIR" "$LIB_DIR" 2>/dev/null || true
    if [[ ! -w "$BIN_DIR" ]] || [[ ! -w "$(dirname "$LIB_DIR")" ]]; then
        _die "can't write to $BIN_DIR — run with sudo or use --prefix=\$HOME/.local"
    fi
    # Label from where files actually LAND — not from "sudo was empty".
    # /usr/local without sudo is still a SHARED prefix (every user sees it);
    # only $HOME/.local* is truly "just for you" (Stuarts-iMac, 2026-11-18:
    # the old label said user-local while installing into /usr/local).
    if [[ "$PREFIX" == "$HOME/.local" || "$PREFIX" == "$HOME"/* ]]; then
        printf '  %s✓%s user-local install — just for you (files in %s, no sudo needed)%s\n' "$GRN" "$RST" "$PREFIX" "$RST"
        printf '    · no sudo password needed — try it risk-free\n'
        if [[ "$WITH_SERVICE" -eq 1 ]]; then
            printf '    · background service: installed automatically (per-user, starts on login)\n'
        else
            printf '    · auto-start on login: %shivv doctor --fix%s (per-user service)\n' "$CYN" "$RST"
        fi
        printf '    · to help others on this machine later, run with sudo:\n'
        printf '      %ssudo curl -sSL %s/install-%s.sh | bash%s\n\n' "$CYN" "$HUB" "$APP" "$RST"
    else
        printf '  %s✓%s no-sudo install into %s — SHARED: every user on this machine can run %s%s\n' "$GRN" "$RST" "$PREFIX" "$APP" "$RST"
        printf '    · state + service remain per-user (yours: %s/.local/share/%s)\n' "$HOME" "$APP"
        printf '    · explicit system-wide (sudo) install: sudo curl -sSL %s/install-%s.sh | bash\n\n' "$HUB" "$APP"
    fi
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
    # NEVER write through a symlink: `cp`/`mv` onto a link follows it, so a
    # `hivv link`ed client would have the CLONE it points at overwritten by
    # this download. The link is the user's explicit choice — keep it.
    if [[ -L "$out" ]]; then
        printf '[hivv] kept existing link: %s -> %s (nothing downloaded)\n' \
            "$out" "$(readlink "$out" 2>/dev/null)" >&2
        return 0
    fi
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
    # THE HUB DID NOT ANSWER. One more try — the GitHub copy of the same
    # file (public_html/ mirrors what the site serves, so the basename is
    # the same). ONE extra request, only because a human ran the
    # installer. The automatic update path never touches GitHub: that is
    # the flood law.
    if [[ -n "${FALLBACK_RAW:-}" ]]; then
        local fb_url="$FALLBACK_RAW/$(basename "$url")"
        local fb_cmd=""
        if need curl; then
            fb_cmd="curl -fsSL --connect-timeout 15 --max-time 90 -o '$tmp' '$fb_url'"
        else
            fb_cmd="wget -q --timeout=90 -O '$tmp' '$fb_url'"
        fi
        if bash -c "$fb_cmd" 2>/dev/null && [[ -s "$tmp" ]] \
           && ! head -1 "$tmp" 2>/dev/null | grep -qiE "<!DOCTYPE|<html"; then
            printf '[hivv] hub unreachable — using the GitHub copy (%s)\n' "$fb_url" >&2
            if [[ -n "$SUDO" ]]; then
                $SUDO cp "$tmp" "$out" && $SUDO chmod 0644 "$out" && rm -f "$tmp" && return 0
            else
                mv "$tmp" "$out" && return 0
            fi
        fi
    fi
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
        # `sudo -n` everywhere: never prompt for a password mid-install
        # (a prompt hangs CI and unattended installs).
        sudo -n mkdir -p "$OVR_DIR" \
            && printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' | sudo -n tee "$OVR_FILE" > /dev/null \
            && sudo -n systemctl daemon-reload 2>/dev/null \
            && sudo -n systemctl restart ollama 2>/dev/null \
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
    if [[ "$(uname -s)" == "Linux" ]] && { [[ -n "$SUDO" ]] || [[ "$EUID" -eq 0 ]]; } && need systemctl && [[ -d /run/systemd/system ]]; then
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
# crash-loop bound: 3 starts / 5 min then failed, not an endless restart storm
StartLimitIntervalSec=300
StartLimitBurst=3

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
    elif [[ "$(uname -s)" == "Linux" ]] && need systemctl && [[ "$EUID" -ne 0 ]]; then
        # user mode: per-user systemd unit — no sudo, survives login/logout.
        # (the old gap: user installs skipped the service entirely and needed
        #  a second `hivv doctor --fix` step before the node was on the net)
        mkdir -p "$HOME/.config/systemd/user"
        PY_ABS="$(command -v "$PYTHON_BIN")"
        cat > "$HOME/.config/systemd/user/${APP}-agent.service" <<UNIT
[Unit]
Description=hivv node agent — the people's distributed AI (user mode)
After=network-online.target
# crash-loop bound: 3 starts / 5 min then failed, not an endless restart storm
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
ExecStart=$PY_ABS $CLIENT_FILE _agent
Restart=always
RestartSec=10
Nice=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
UNIT
        systemctl --user daemon-reload 2>/dev/null || true
        was_active=0
        systemctl --user is-active --quiet ${APP}-agent.service 2>/dev/null && was_active=1
        if systemctl --user enable ${APP}-agent.service 2>/dev/null; then
            SERVICE_INSTALLED=1
            if [[ "$was_active" -eq 1 ]]; then
                # An agent was already running: RESTART it so the NEW client
                # is what executes. 'enable --now' alone leaves the old
                # process running the old copy FOREVER — wahi's agent served
                # /usr/local's stale code for 23h after a successful install
                # (2026-11-23) because nothing replaced the live process.
                systemctl --user restart ${APP}-agent.service 2>/dev/null || true
                _say "you're live 🐝 — agent restarted on the new copy, starts on login"
            elif systemctl --user start ${APP}-agent.service 2>/dev/null; then
                _say "you're live 🐝 — systemd --user starts your node on login"
            else
                _warn "unit installed but not started — run: $APP start"
            fi
        else
            _warn "couldn't install the user service — start manually: $APP start"
        fi
        if loginctl enable-linger "$USER" 2>/dev/null; then
            _say "linger enabled — your node survives logout"
        else
            _warn "node stops when you log out — enable: loginctl enable-linger $USER"
        fi
    elif [[ "$(uname -s)" == "Darwin" ]]; then
        # per-user LaunchAgent — no sudo required. Under ROOT (sudo curl|bash),
        # target the REAL user's home: a root gui/0 LaunchAgent would never
        # load for them (2026-11-18 rethink).
        RUN_USER="${SUDO_USER:-$(id -un)}"
        RUN_HOME="$HOME"
        if [[ "$EUID" -eq 0 && -n "$SUDO_USER" ]]; then
            RUN_HOME="$(eval echo ~"$SUDO_USER" 2>/dev/null || echo "$HOME")"
            _warn "tip: on macOS run WITHOUT sudo (user-mode is native) — installing the LaunchAgent for $RUN_USER"
        fi
        LA_DIR="$RUN_HOME/Library/LaunchAgents"
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
  <key>StandardOutPath</key><string>$RUN_HOME/.local/share/$APP/agent.log</string>
  <key>StandardErrorPath</key><string>$RUN_HOME/.local/share/$APP/agent.log</string>
  <key>Nice</key><integer>10</integer>
</dict>
</plist>
PLIST
            launchctl bootout "gui/$(id -u)/org.hivv.agent" 2>/dev/null || true
            [[ "$EUID" -eq 0 ]] && chown "$RUN_USER" "$LA_PLIST" 2>/dev/null || true
            launchctl bootstrap "gui/$(id -u "$RUN_USER")" "$LA_PLIST" 2>/dev/null \
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

# ── proof: the node actually JOINED the network (registered at the hub) ──
# `--healthcheck` only carries node_id AFTER a successful register, so a match
# here means: agent up + hub reachable + identity minted. One command → done.
HIVV_JOINED=""
if [[ "$SERVICE_INSTALLED" -eq 1 ]] && [[ -x "$WRAPPER" ]]; then
    printf '  %s→ joining the hive' "$BLU"
    for _i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        HC_OUT=$("$WRAPPER" --healthcheck 2>/dev/null || true)
        HIVV_JOINED=$(printf '%s' "$HC_OUT" | grep -o '"node_id":"[^"]*"' | head -1 | cut -d'"' -f4)
        [[ -n "$HIVV_JOINED" ]] && break
        printf '.'
        sleep 1
    done
    if [[ -n "$HIVV_JOINED" ]]; then
        printf ' %s✓ registered as %s — your node is ON the network%s\n' "$GRN" "$HIVV_JOINED" "$RST"
    else
        printf ' %s! agent is starting — verify in ~30s: %s up%s\n' "$YLW" "$APP" "$RST"
    fi
fi

# ── success card ──────────────────────────────────────────────────────
# Install-scope label: derived from PREFIX (where files live), not from
# "was sudo empty" — /usr/local without sudo is still shared by everyone.
if [[ "$PREFIX" == "$HOME/.local" || "$PREFIX" == "$HOME"/* ]]; then
    HIVV_INSTALL_LABEL="user-local (just for you)"
elif [[ -n "$SUDO" ]]; then
    HIVV_INSTALL_LABEL="SYSTEM-WIDE (helps everyone)"
else
    HIVV_INSTALL_LABEL="shared prefix $PREFIX (all users)"
fi
SUCCESS_BAR="${GRN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"
printf '\n  %s\n' "$SUCCESS_BAR"
printf '  %s%s✓ %s CLI installed — %s%s\n' "$BLD" "$GRN" "$APP" "$HIVV_INSTALL_LABEL" "$RST"
printf '  %s\n\n' "$SUCCESS_BAR"

printf '  %s🌌 how hivv works — 30-second tour%s\n' "$BLD" "$RST"
printf '    · the %s%s hub%s is just a phone book — it knows who is online, nothing more%s\n' "$HIVE" "$BLD" "$RST" "$RST"
printf '    · your computer is a %snode%s — the agent heartbeats to the hub, peers talk direct%s\n' "$BLD" "$RST"
printf '    · AI requests route to the best available brain: your local model, a peer, or a donated cloud key%s\n' "$RST"
printf '    · your chats/data stay between nodes — the hub never sees prompt content%s\n' "$RST"
printf '    · relay jobs (cross-NAT): ON by default — the hive may queue jobs on your idle CPU/GPU.\n'
printf '      anytime: %sopt out%s = %shivv volunteer off%s   ·   status: hivv volunteer\n' "$CYN" "$RST" "$CYN" "$RST"
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

# PATH HANDOFF — the missing half of an install. If an OLDER hivv still
# wins on PATH (folder order, or bash's cached command path), everything
# just installed is INVISIBLE: wahi's exact post-install state on
# 2026-11-23 — files landed in ~/.local, but 'hivv' kept running
# /usr/local's stale copy and hit the same Permission denied wall. Say
# the fix, loudly, with the exact command.
if command -v "$APP" >/dev/null 2>&1; then
    _resolved="$(command -v "$APP" 2>/dev/null)"
    if [[ -n "$_resolved" && "$_resolved" != "$WRAPPER" ]]; then
        printf '  %s!%s your PATH still resolves hivv to an OLDER copy: %s%s%s\n' "$YLW" "$RST" "$RED" "$_resolved" "$RST"
        printf '    fix THIS shell right now:\n'
        printf '      %sexport PATH="%s:$PATH" && hash -r%s\n' "$CYN" "$BIN_DIR" "$RST"
        printf '    (or simply open a NEW terminal — future shells are already fixed)\n'
        printf '    then check: %shivv doctor%s → its client line must show %s%s%s\n\n' "$CYN" "$RST" "$GRN" "$CLIENT_FILE" "$RST"
    fi
fi

printf '  %sfirst steps — try these right now%s\n' "$BLD" "$RST"
printf '    %s$%s %s%s up%s                   quick status check (is my node alive?)\n'   "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s hi%s                   say hello to the hive 🐝\n'                  "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s chat "hello"%s         talk to the distributed AI\n'                "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s nodes%s                see every bee online right now\n'            "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s welcome%s              guided tour of everything hivv can do\n\n'    "$DIM" "$RST" "$BLD" "$APP" "$RST"
printf '    %s$%s %s%s volunteer off%s        relay jobs across NAT — ON by default (opt out here)\n\n' "$DIM" "$RST" "$BLD" "$APP" "$RST" "$RST"

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
printf '    %s$%s %s%s volunteer%s          relay jobs: ON by default — status/off: hivv volunteer\n' "$DIM" "$RST" "$BLD" "$APP" "$RST" "$RST"
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

  # THE CLI EXPECTS A SPECIFIC LAYOUT — detect it there first:
  #   ~/llama.cpp/build/bin/llama-server   ← what `hivv llm setup` runs
  #   ~/llama.cpp/models/                  ← where the GGUF lives
  # Building anywhere else means `hivv llm setup` reports "not built" and
  # builds it AGAIN. One source of truth, one place.
  LLAMA_DIR="$HOME/llama.cpp"
  # `curl | sudo bash` makes $HOME root's — building there leaves the
  # logged-in user with a model they cannot see or stop. Install FOR the
  # user who ran sudo (same rule as the service section above); a real
  # root login has no SUDO_USER and keeps /root, which is correct for it.
  LLAMA_HOME="$HOME"
  LLAMA_USER=""
  if [[ "$EUID" -eq 0 && -n "$SUDO_USER" && "$SUDO_USER" != "root" ]]; then
    _llama_home="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6)"
    [[ -n "$_llama_home" ]] || _llama_home="$(eval echo ~"$SUDO_USER" 2>/dev/null)"
    if [[ -n "$_llama_home" && "$_llama_home" != "/" ]]; then
      LLAMA_USER="$SUDO_USER"
      LLAMA_HOME="$_llama_home"
      LLAMA_DIR="$LLAMA_HOME/llama.cpp"
      _warn "running as root — building the LLM for $LLAMA_USER ($LLAMA_HOME), not root"
    fi
  fi
  LLAMA_BIN_DIR="$LLAMA_DIR/build/bin"
  LLAMA_MODEL_DIR="$LLAMA_DIR/models"

  if [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
    HAVE_LLAMA=1
    _say "llama.cpp already built → ${CYN}$LLAMA_BIN_DIR/llama-server${RST}"
  elif command -v llama-cli >/dev/null 2>&1 || command -v llama-server >/dev/null 2>&1; then
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

    # LLAMA_DIR / LLAMA_BIN_DIR are defined above (shared with detection) —
    # the CLI expects the binary at ~/llama.cpp/build/bin/llama-server.

    # ── build tools first ────────────────────────────────────────────
    # No git/cmake/C++ compiler/make → the build dies with a raw
    # traceback (asher@wahi 2026-11-21) or mid-configure with
    # "No CMAKE_CXX_COMPILER could be found" (2026-11-25). Install them
    # NOW or print the exact command.
    if ! ensure_build_tools git cmake cxx make; then
      printf '  %s!%s cannot build llama.cpp without git/cmake/C++ compiler/make — skipping the local LLM\n' "$YLW" "$RST" >&2
      printf '    %sthe hivv node still works — it will use the API gateway%s\n' "$DIM" "$RST" >&2
      HAVE_LLAMA=0
    else
      BUILD_OK=0
      if [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
        _say "llama.cpp already built → ${CYN}$LLAMA_BIN_DIR/llama-server${RST}"
        BUILD_OK=1
      elif [[ -f "$LLAMA_DIR/CMakeLists.txt" ]]; then
        # a .git checkout OR a tarball tree — CMakeLists.txt is the truth
        printf '  %s→%s llama.cpp source already present — building (a few minutes)…\n' "$BLU" "$RST"
        if (cd "$LLAMA_DIR" && cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1 \
            && cmake --build build --config Release -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" >/dev/null 2>&1) \
            && [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
          _say "built llama.cpp → ${CYN}$LLAMA_BIN_DIR/llama-server${RST}"
          BUILD_OK=1
        else
          _warn "llama.cpp build failed — try: cd $LLAMA_DIR && cmake -B build && cmake --build build -j"
        fi
      elif [[ -d "$LLAMA_DIR" ]] && [[ -n "$(ls -A "$LLAMA_DIR" 2>/dev/null)" ]] && [[ ! -d "$LLAMA_DIR/.git" ]]; then
        # something that isn't ours — never delete it, say so instead
        _warn "$LLAMA_DIR exists but isn't a llama.cpp checkout — move it aside, then re-run:"
        printf '    %smv %s %s.bak%s\n' "$DIM" "$LLAMA_DIR" "$LLAMA_DIR" "$RST"
      else
        # A clone that died mid-pack leaves the dir behind — clean it, then
        # RETRY: one flaky link must not kill the whole install
        # (asher@wahi 2026-11-24: "RPC failed; curl 56 … early EOF").
        [[ -d "$LLAMA_DIR" ]] && rm -rf "$LLAMA_DIR"
        CLONE_OK=0
        for _try in 1 2 3; do
          printf '  %s→%s cloning llama.cpp (attempt %s/3, shallow)…\n' "$BLU" "$RST" "$_try"
          if git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$LLAMA_DIR" >/dev/null 2>&1; then
            CLONE_OK=1
            break
          fi
          rm -rf "$LLAMA_DIR" 2>/dev/null || true
          [[ "$_try" -lt 3 ]] && sleep 2
        done
        if [[ "$CLONE_OK" -eq 1 ]]; then
          printf '  %s→%s building llama.cpp (this takes a few minutes)…\n' "$BLU" "$RST"
          if (cd "$LLAMA_DIR" && cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1 \
              && cmake --build build --config Release -j "$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)" >/dev/null 2>&1) \
              && [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
            _say "built llama.cpp → ${CYN}$LLAMA_BIN_DIR/llama-server${RST}"
            BUILD_OK=1
          else
            _warn "llama.cpp build failed — try: cd $LLAMA_DIR && cmake -B build && cmake --build build -j"
          fi
        else
          _warn "git clone of llama.cpp failed 3x — check your network, then re-run this installer"
        fi
      fi

      if [[ "$BUILD_OK" -eq 1 ]]; then
        HAVE_LLAMA=1
        # hand the tree back to the logged-in user when we built via sudo
        if [[ -n "$LLAMA_USER" ]]; then
          chown -R "$LLAMA_USER" "$LLAMA_DIR" 2>/dev/null || true
        fi
        # The CLI runs the binary by ABSOLUTE path, so PATH is a convenience
        # for the user only — add it, but never fail the install over it.
        # RIGHT rc file for the TARGET user (under sudo $HOME is root's, and
        # zsh/fish users don't read .bashrc) — added at most once.
        if [[ ":$PATH:" != *":$LLAMA_BIN_DIR:"* ]]; then
          _llama_sh="${SHELL:-/bin/bash}"
          if [[ -n "$LLAMA_USER" ]]; then
            _llama_sh="$(getent passwd "$LLAMA_USER" 2>/dev/null | cut -d: -f7)"
            _llama_sh="${_llama_sh:-/bin/bash}"
          fi
          LLAMA_SHELL_RC="$LLAMA_HOME/.bashrc"
          case "$_llama_sh" in
            */zsh)  LLAMA_SHELL_RC="$LLAMA_HOME/.zshrc" ;;
            */fish) LLAMA_SHELL_RC="$LLAMA_HOME/.config/fish/config.fish" ;;
          esac
          if ! grep -qF "$LLAMA_BIN_DIR" "$LLAMA_SHELL_RC" 2>/dev/null; then
            mkdir -p "$(dirname "$LLAMA_SHELL_RC")" 2>/dev/null || true
            case "$LLAMA_SHELL_RC" in
              */fish) printf 'set -gx PATH "%s" $PATH\n' "$LLAMA_BIN_DIR" >> "$LLAMA_SHELL_RC" 2>/dev/null || true ;;
              *)      printf 'export PATH="%s:$PATH"\n' "$LLAMA_BIN_DIR" >> "$LLAMA_SHELL_RC" 2>/dev/null || true ;;
            esac
            if [[ -n "$LLAMA_USER" ]]; then
              chown "$LLAMA_USER" "$LLAMA_SHELL_RC" 2>/dev/null || true
            fi
          fi
          export PATH="$LLAMA_BIN_DIR:$PATH"
        fi
      fi
    fi
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
    # The CLI looks for GGUFs in ~/llama.cpp/models — use the SAME directory,
    # otherwise `hivv llm setup` re-downloads what we just fetched.
    MODEL_DIR="$LLAMA_MODEL_DIR"
    mkdir -p "$MODEL_DIR"
    MODEL_PATH=""
    if [[ -n "$GGUF_URL" && -n "$GGUF_FILE" ]]; then
      MODEL_PATH="${MODEL_DIR}/${GGUF_FILE}"
      if [[ -s "$MODEL_PATH" ]]; then
        _say "model already on disk — ${CYN}${GGUF_FILE}${RST}"
      elif _spin "downloading ${CYN}${GGUF_FILE}${RST}" curl -fL --connect-timeout 30 --max-time 3600 -C - "$GGUF_URL" -o "$MODEL_PATH"; then
        _say "model ready — ${APP} will use it for local AI tasks"
      else
        _warn "model download failed — retry later: ${APP} llm pull-gguf ${LLM_MODEL}"
        MODEL_PATH=""
      fi
    else
      _warn "no GGUF mapping for ${LLM_MODEL} — pick one: ${APP} llm pull-gguf gemma4:e2b"
    fi

    # hand the model back to the logged-in user when we pulled via sudo
    if [[ -n "$LLAMA_USER" && -n "$MODEL_PATH" ]]; then
      chown -R "$LLAMA_USER" "$LLAMA_MODEL_DIR" 2>/dev/null || true
    fi

    # ── serve it: a full install means a RUNNING local LLM ──────────
    # "installed but not serving" is the same as not installed: the hive
    # cannot borrow compute that nothing is answering on.
    if [[ -n "$MODEL_PATH" ]] && [[ -x "$LLAMA_BIN_DIR/llama-server" ]]; then
      LLM_PORT=8080
      if need curl && curl -fsS --connect-timeout 2 --max-time 4 "http://127.0.0.1:${LLM_PORT}/v1/models" >/dev/null 2>&1; then
        _say "local LLM already serving on :${LLM_PORT}"
      else
        printf '  %s→%s starting the local LLM on :%s…\n' "$BLU" "$RST" "$LLM_PORT"
        LLM_LOG_DIR="$LLAMA_HOME/.local/share/${APP}/logs"
        mkdir -p "$LLM_LOG_DIR"
        if [[ -n "$LLAMA_USER" ]]; then
          # a root-owned server is a server the logged-in user can't stop
          chown -R "$LLAMA_USER" "$LLAMA_HOME/.local/share/${APP}" 2>/dev/null || true
          sudo -u "$LLAMA_USER" -H nohup "$LLAMA_BIN_DIR/llama-server" -m "$MODEL_PATH" -c 4096 \
                --host 127.0.0.1 --port "$LLM_PORT" \
                > "$LLM_LOG_DIR/llama-server.log" 2>&1 &
        else
          nohup "$LLAMA_BIN_DIR/llama-server" -m "$MODEL_PATH" -c 4096 \
                --host 127.0.0.1 --port "$LLM_PORT" \
                > "$LLM_LOG_DIR/llama-server.log" 2>&1 &
        fi
        sleep 3
        if [[ -n "$LLAMA_USER" ]]; then
          chown "$LLAMA_USER" "$LLM_LOG_DIR/llama-server.log" 2>/dev/null || true
        fi
        if need curl && curl -fsS --connect-timeout 2 --max-time 4 "http://127.0.0.1:${LLM_PORT}/v1/models" >/dev/null 2>&1; then
          _say "local LLM is LIVE on :${LLM_PORT} — your box is now a hive worker"
        else
          _warn "the server did not answer yet — check: ${APP} llm status"
          printf '    %slog: %s\n' "$DIM" "$LLM_LOG_DIR/llama-server.log" "$RST"
        fi
      fi
    fi

    printf '\n  %s✓%s try:     %s%s chat "hello" --local%s\n' "$GRN" "$RST" "$CYN" "$APP" "$RST"
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


# ── optional: contributor tools (git + gh) ─────────────────────────────────
# HIVV's source is open and AI agents are explicitly welcome to change it
# (see CONTRIBUTING.md "For AI agents" and /ai.txt). git + gh are NOT needed
# to RUN the network — only to CONTRIBUTE to it — so this is asked, and the
# default answer is "no".
should_install_git() {
  [[ "$WITH_GIT" -eq  1 ]] && return 0
  [[ "$WITH_GIT" -eq -1 ]] && return 1
  [[ -t 0 ]] || return 1
  printf '  %sContribute to HIVV%s (install git + GitHub CLI)? [y/N] %s' "$BLD" "$RST" "$RST"
  local ans=""
  read -r ans </dev/tty || return 1
  [[ "$ans" =~ ^[Yy] ]]
}

if should_install_git; then
  printf '
  %s%s🐝 contributor tools — for changing HIVV itself%s

' "$BLD" "$HIVE" "$RST"
  ensure_dev_tools
  printf '  %s→%s branch, commit, open a PR: see CONTRIBUTING.md%s
' "$DIM" "$RST" "$RST"
  printf '  %s→%s or set it all up later: %s contribute%s

' "$DIM" "$RST" "$APP" "$RST"
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
