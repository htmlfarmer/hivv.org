#!/bin/sh
# install-tailscale.sh — install Tailscale on a box that
# tailscale.com/install.sh refuses ("Couldn't determine what kind of Linux
# is running.").
#
# WHY THIS EXISTS (ashy, 2026-11-27)
# -----------------------------------
# The official installer cases ONLY on ID= from /etc/os-release. BOSS
# GNU/Linux (C-DAC's Debian fork) says ID=boss, VERSION_CODENAME=pragya —
# not in its list, so it sets OS_UNSUPPORTED and exits 1 BEFORE installing
# anything. Nothing is wrong with the box: it is a Debian 12 (bookworm)
# system wearing an ID the installer never heard of, and their script has
# no env override to opt out of detection.
#
# THREE TIERS — first one that wins:
#   1. the OFFICIAL installer — still the right answer on every OS it
#      recognizes; it exits fast and harmlessly on the ones it does not.
#   2. APT — when this really is Debian-family: either a tailscale repo is
#      already configured (ashy), or VERSION_CODENAME names a suite
#      pkgs.tailscale.com actually serves.
#   3. the STATIC tarball + a systemd unit — no distro knowledge at all.
#      This is the tier that saves BOSS and every other unknown ID.
#
# LAWS THIS SCRIPT KEEPS
#   * A download is written to a FILE and the file is run. Never pipe a
#     download into a shell — the repo's fast-scan calls that critical,
#     and it is right to.
#   * Never prompt for a sudo password nobody can answer (CI, cron, a
#     piped download): print the exact command instead.
#   * Idempotent: already installed -> say so, change nothing.
#   * --dry-run prints the plan and touches nothing (and needs no root).
#
# USAGE
#   sh install-tailscale.sh              install
#   sh install-tailscale.sh --dry-run    show the plan, change nothing
#   sh install-tailscale.sh --verbose    show the official installer's own
#                                        output instead of one line
#   HIVV_TS_SKIP_OFFICIAL=1              skip tier 1
#   HIVV_TS_APT_OS=debian HIVV_TS_APT_SUITE=bookworm
#                                        force tier 2's repo coordinates
#                                        (skips the reachability probe)
#   HIVV_TS_SOURCED=1                    source this file for its functions
#                                        without running main (tests)

DRY=0
VERBOSE=0
SOURCED="$HIVV_TS_SOURCED"
[ -n "$SOURCED" ] || SOURCED=0
TRACK="$HIVV_TS_TRACK"
[ -n "$TRACK" ] || TRACK=stable

# --- output -------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m\342\234\223\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail() { printf '  \033[31m\342\234\227\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- download to a FILE, never into a shell ----------------------------
fetch() {
    _url="$1"
    _out="$2"
    if have curl; then
        curl -fsSL --connect-timeout 10 --max-time 600 "$_url" -o "$_out"
    elif have wget; then
        wget -q -T 600 -O "$_out" "$_url"
    else
        return 127
    fi
}

tmpfile() { mktemp 2>/dev/null || printf '/tmp/hivv-ts.%s.%s\n' "$$" "$1"; }

# --- privilege ladder (same law as scripts/lib/deps.sh) -----------------
# root -> plain . passwordless sudo -> sudo -n . a human at a TTY -> plain
# sudo (it asks THEM) . nobody watching -> NEVER prompt, print the command.
PRIV=""
can_prompt_sudo() {
    [ -z "$CI" ] || return 1
    [ -z "$HIVV_NO_SUDO_PROMPT" ] || return 1
    [ -z "$PYTEST_CURRENT_TEST" ] || return 1
    [ -t 1 ] || return 1
    [ -t 0 ] || return 1
    return 0
}
setup_priv() {
    if [ "$(id -u 2>/dev/null || echo 1)" = 0 ]; then
        PRIV=plain
    elif have sudo && sudo -n true 2>/dev/null; then
        PRIV=sudon
    elif can_prompt_sudo; then
        PRIV=sudo
    else
        PRIV=none
    fi
}
# run a command with root. In --dry-run, echo it instead of running it.
priv() {
    if [ "$DRY" = 1 ]; then
        printf '    would run: %s\n' "$*"
        return 0
    fi
    case "$PRIV" in
        plain) "$@" ;;
        sudon) sudo -n "$@" ;;
        sudo)  sudo "$@" ;;
        *)     return 127 ;;
    esac
}

# --- tier 1: the official installer ------------------------------------
OFFICIAL_OUT=""
tier_official() {
    say ""
    say "tier 1 — the official installer"
    if [ "$HIVV_TS_SKIP_OFFICIAL" = 1 ]; then
        warn "skipped (HIVV_TS_SKIP_OFFICIAL=1)"
        return 1
    fi
    if ! have curl && ! have wget; then
        warn "no curl and no wget — cannot fetch it"
        return 1
    fi
    if [ "$DRY" = 1 ]; then
        # a dry run must not fetch anything — show the plan and move on
        printf '    would fetch: tailscale.com/install.sh, then run it\n'
        return 0
    fi
    _tmp="$(tmpfile official)"
    if ! fetch "https://tailscale.com/install.sh" "$_tmp"; then
        warn "could not download the official installer"
        rm -f "$_tmp"
        return 1
    fi
    if [ "$DRY" = 1 ]; then
        printf '    would run: sh %s\n' "$_tmp"
        rm -f "$_tmp"
        return 0
    fi
    OFFICIAL_OUT="$(sh "$_tmp" 2>&1)"
    _rc=$?
    rm -f "$_tmp"
    if [ "$_rc" = 0 ] && have tailscale; then
        ok "installed by the official installer"
        return 0
    fi
    if [ "$VERBOSE" = 1 ]; then
        printf '%s\n' "$OFFICIAL_OUT"
    else
        warn "the official installer does not recognize this OS (use --verbose to see why)"
    fi
    return 1
}

# --- tier 2: apt, Debian family ----------------------------------------
apt_has_tailscale_repo() {
    grep -ls "pkgs\.tailscale\.com" \
        /etc/apt/sources.list \
        /etc/apt/sources.list.d/*.list \
        /etc/apt/sources.list.d/*.sources 2>/dev/null | head -1 >/dev/null
}
# Does pkgs.tailscale.com actually serve this suite? Read-only probe.
probe_suite() {
    _p="$(tmpfile probe)"
    if fetch "https://pkgs.tailscale.com/$TRACK/$1/dists/$2/InRelease" "$_p" 2>/dev/null && [ -s "$_p" ]; then
        rm -f "$_p"
        return 0
    fi
    rm -f "$_p"
    return 1
}
# uname -m -> the suffix pkgs.tailscale.com uses
arch_to_tarch() {
    case "$1" in
        x86_64|amd64)          printf 'amd64\n' ;;
        aarch64|arm64)         printf 'arm64\n' ;;
        armv7l|armv7|armhf)    printf 'armv7\n' ;;
        i386|i486|i586|i686)   printf '386\n' ;;
        ppc64le)               printf 'ppc64le\n' ;;
        riscv64)               printf 'riscv64\n' ;;
        *)                     return 1 ;;
    esac
}
apt_install_tailscale() {
    if [ "$DRY" = 1 ]; then
        printf '    would run: apt-get update && apt-get install -y tailscale\n'
        return 0
    fi
    priv env DEBIAN_FRONTEND=noninteractive apt-get update -qq || return 1
    priv env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tailscale || return 1
    have tailscale
}
tier_apt() {
    # announce FIRST, then explain a skip: a dry-run plan that silently
    # omits a tier leaves the reader wondering what happened to it
    say ""
    say "tier 2 — apt (Debian family)"
    if ! have apt-get || [ ! -r /etc/debian_version ]; then
        warn "no apt-get, or not a Debian-family box — skipping"
        return 1
    fi

    # 2a. a tailscale repo is already configured — just use it.
    if apt_has_tailscale_repo; then
        ok "a tailscale apt repository is already configured"
        if apt_install_tailscale; then
            # a dry run has not installed anything — do not claim it did
            [ "$DRY" = 1 ] || ok "installed from the configured repository"
            return 0
        fi
        warn "apt install failed — falling through"
        return 1
    fi

    # 2b. add the official repo — but only with coordinates we can trust.
    _os="$HIVV_TS_APT_OS"
    _suite="$HIVV_TS_APT_SUITE"
    if [ -z "$_os" ] || [ -z "$_suite" ]; then
        # shellcheck disable=SC1091
        . /etc/os-release 2>/dev/null
        if [ -z "$_os" ]; then
            case "$ID" in
                debian) _os=debian ;;
                ubuntu) _os=ubuntu ;;
                *)
                    case " $ID_LIKE " in
                        *" debian "*) _os=debian ;;
                        *" ubuntu "*) _os=ubuntu ;;
                    esac
                    ;;
            esac
        fi
        if [ -z "$_suite" ]; then
            _cand="$UBUNTU_CODENAME"
            [ -n "$_cand" ] || _cand="$VERSION_CODENAME"
            if [ -n "$_os" ] && [ -n "$_cand" ]; then
                if probe_suite "$_os" "$_cand"; then
                    _suite="$_cand"
                else
                    warn "the repo does not serve suite '$_cand' — no guesswork, trying the static tier"
                fi
            fi
        fi
    fi
    if [ -z "$_os" ] || [ -z "$_suite" ]; then
        warn "cannot name a suite this distro is based on — skipping apt"
        return 1
    fi
    ok "adding the official repository ($_os $_suite)"
    if [ "$DRY" = 1 ]; then
        printf '    would run: keyring + /etc/apt/sources.list.d/tailscale.list\n'
        printf '    would run: apt-get update && apt-get install -y tailscale\n'
        return 0
    fi
    priv mkdir -p --mode=0755 /usr/share/keyrings || return 1
    _key="$(tmpfile key)"
    if ! fetch "https://pkgs.tailscale.com/$TRACK/$_os/$_suite.noarmor.gpg" "$_key" || [ ! -s "$_key" ]; then
        warn "could not fetch the repository signing key"
        rm -f "$_key"
        return 1
    fi
    priv cp "$_key" /usr/share/keyrings/tailscale-archive-keyring.gpg
    priv chmod 0644 /usr/share/keyrings/tailscale-archive-keyring.gpg
    rm -f "$_key"
    _list="$(tmpfile list)"
    printf 'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/%s/%s %s main\n' \
        "$TRACK" "$_os" "$_suite" > "$_list"
    priv cp "$_list" /etc/apt/sources.list.d/tailscale.list
    priv chmod 0644 /etc/apt/sources.list.d/tailscale.list
    rm -f "$_list"
    if apt_install_tailscale; then
        [ "$DRY" = 1 ] || ok "installed from the official repository"
        return 0
    fi
    warn "apt install failed — falling through"
    return 1
}

# --- tier 3: static tarball + systemd unit -----------------------------
tier_static() {
    say ""
    say "tier 3 — static binaries (works on any Linux)"
    if ! have tar; then
        warn "tar is not available — skipping"
        return 1
    fi
    if ! have uname; then
        warn "uname is not available — cannot tell the architecture"
        return 1
    fi
    if ! _tarch="$(arch_to_tarch "$(uname -m)")"; then
        warn "unsupported architecture: $(uname -m) — skipping"
        return 1
    fi

    if [ "$DRY" = 1 ]; then
        printf '    would fetch: pkgs.tailscale.com/%s/tailscale_*_%s.tgz\n' "$TRACK" "$_tarch"
        printf '    would run:   install tailscale -> /usr/local/bin, tailscaled -> /usr/local/sbin\n'
        printf '    would run:   systemd unit + /etc/default/tailscaled + enable --now\n'
        return 0
    fi

    _idx="$(tmpfile index)"
    if ! fetch "https://pkgs.tailscale.com/$TRACK/" "$_idx" || [ ! -s "$_idx" ]; then
        warn "cannot reach pkgs.tailscale.com"
        rm -f "$_idx"
        return 1
    fi
    _ver="$(sed -n "s/.*tailscale_\([0-9][0-9.]*\)_$_tarch\.tgz.*/\1/p" "$_idx" | head -1)"
    rm -f "$_idx"
    if [ -z "$_ver" ]; then
        warn "could not determine the newest version for $_tarch"
        return 1
    fi
    ok "tailscale $_ver ($_tarch)"

    _tgz="$(tmpfile tgz)"
    if ! fetch "https://pkgs.tailscale.com/$TRACK/tailscale_${_ver}_${_tarch}.tgz" "$_tgz"; then
        warn "could not download the tarball"
        rm -f "$_tgz"
        return 1
    fi
    _wd="$(mktemp -d 2>/dev/null || printf '/tmp/hivv-ts-work.%s' "$$")"
    if ! tar xzf "$_tgz" -C "$_wd"; then
        warn "could not extract the tarball"
        rm -f "$_tgz"
        return 1
    fi
    rm -f "$_tgz"
    _src="$_wd/tailscale_${_ver}_${_tarch}"
    if [ ! -x "$_src/tailscale" ] || [ ! -x "$_src/tailscaled" ]; then
        warn "unexpected tarball layout"
        return 1
    fi

    priv mkdir -p /usr/local/bin /usr/local/sbin || return 1
    priv install -m 0755 "$_src/tailscale"  /usr/local/bin/tailscale  || return 1
    priv install -m 0755 "$_src/tailscaled" /usr/local/sbin/tailscaled || return 1
    ok "binaries installed"

    if have systemctl && [ -d /etc/systemd/system ] && [ -r "$_src/systemd/tailscaled.service" ]; then
        _unit="$(tmpfile unit)"
        # point the unit at where WE put the binary (the shipped one says
        # /usr/sbin, which is where the .deb puts it, not us)
        sed 's#/usr/sbin/tailscaled#/usr/local/sbin/tailscaled#g' \
            "$_src/systemd/tailscaled.service" > "$_unit"
        priv cp "$_unit" /etc/systemd/system/tailscaled.service
        priv chmod 0644 /etc/systemd/system/tailscaled.service
        rm -f "$_unit"
        # EnvironmentFile= is NOT optional — a missing /etc/default/tailscaled
        # makes systemd fail the unit before tailscaled ever starts.
        if [ ! -e /etc/default/tailscaled ] && [ -r "$_src/systemd/tailscaled.defaults" ]; then
            priv mkdir -p /etc/default
            priv cp "$_src/systemd/tailscaled.defaults" /etc/default/tailscaled
        fi
        priv systemctl daemon-reload
        if priv systemctl enable --now tailscaled; then
            ok "tailscaled enabled and started"
        fi
    else
        warn "no systemd — the binaries are in place, start tailscaled yourself"
    fi
    rm -rf "$_wd"
    have tailscale
}

# --- main ---------------------------------------------------------------
main() {
    for _a in "$@"; do
        case "$_a" in
            --dry-run|-n) DRY=1 ;;
            --verbose|-v) VERBOSE=1 ;;
            --help|-h)
                say "usage: sh install-tailscale.sh [--dry-run] [--verbose]"
                say "  installs Tailscale even on distros tailscale.com refuses."
                return 0
                ;;
            *)
                fail "unknown option: $_a (try --help)"
                return 2
                ;;
        esac
    done

    if [ "$DRY" = 1 ]; then
        # --dry-run means SHOW THE PLAN. It used to fall through the
        # idempotent early-return below, so the output depended on whether
        # THIS box already had tailscale: a machine that had it printed no
        # tiers and no "dry run" line at all (surfaced 2026-11-28 by the
        # door tests after the hub got tailscale installed). A plan is a
        # plan either way — show the cascade, and say if it is redundant.
        if have tailscale; then
            say ""
            ok "tailscale is already installed: $(tailscale version 2>/dev/null | head -1)"
            say "  a real run changes nothing (idempotent) — the plan follows"
        fi
        say ""
        say "hivv — installing Tailscale (plan)"
        if [ -r /etc/os-release ]; then
            # shellcheck disable=SC1091
            . /etc/os-release 2>/dev/null
            say "  os: $PRETTY_NAME  (id=$ID)"
        fi
        setup_priv
        say ""
        tier_official || true
        tier_apt || true
        tier_static || true
        say ""
        say "dry run — nothing was changed. Re-run without --dry-run to install."
        return 0
    fi

    if have tailscale; then
        say ""
        ok "tailscale is already installed: $(tailscale version 2>/dev/null | head -1)"
        if have systemctl && systemctl is-active --quiet tailscaled 2>/dev/null; then
            ok "tailscaled is running: $(tailscale ip -4 2>/dev/null)"
        else
            warn "tailscaled is not running — start it with: sudo systemctl start tailscaled"
        fi
        say ""
        # Same four-step ladder as the CLI and install-hivv.sh: a finished
        # step must name what follows it, or the reader stops at the line
        # they just read (the gap this whole feature exists to close).
        if have tailscale && tailscale ip -4 >/dev/null 2>&1; then
            _next=4
        else
            _next=2
        fi
        say "  tailscale setup — four steps, in order:"
        if [ "$_next" -le 2 ]; then
            say "    1. hivv install tailscale"
            say "  -> 2. sudo tailscale up             — then OPEN the URL it prints"
            say "    3. tailscale ip -4               — expect a 100.x.x.x address"
            say "    4. hivv firewall --apply --mode tailscale"
        else
            say "    1. hivv install tailscale              done"
            say "    2. sudo tailscale up                   done"
            say "    3. tailscale ip -4                     done"
            say "  -> 4. hivv firewall --apply --mode tailscale"
        fi
        say ""
        if [ "$_next" -le 2 ]; then
            say "  next:  sudo tailscale up"
        else
            say "  next:  hivv firewall --apply --mode tailscale"
            say "  then:  hivv reach <this-bee>   (from ANOTHER machine)"
        fi
        return 0
    fi

    say ""
    say "hivv — installing Tailscale"
    if [ -r /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release 2>/dev/null
        say "  os: $PRETTY_NAME  (id=$ID)"
    fi

    setup_priv
    if [ "$DRY" != 1 ] && [ "$PRIV" = none ]; then
        say ""
        fail "installing Tailscale needs root, and I must not prompt for a password here"
        say ""
        say "  run it yourself — sudo asks YOU:"
        say "      sudo sh $0"
        say ""
        say "  (if your sudo is passwordless I would have used it silently)"
        return 1
    fi

    # (--dry-run returns from its own block above — it must run BEFORE the
    # idempotent early-return, or its output depends on the host's state.)

    _done=0
    if tier_official; then _done=1; fi
    if [ "$_done" = 0 ] && tier_apt; then _done=1; fi
    if [ "$_done" = 0 ] && tier_static; then _done=1; fi

    say ""
    if have tailscale; then
        ok "Tailscale installed"
        say ""
        say "  next:"
        say "      sudo tailscale up              — log in (it prints a URL)"
        say "      hivv firewall --apply --mode tailscale"
        return 0
    fi
    fail "all three tiers failed"
    say ""
    say "  the manual fallback — static binaries:"
    say "      https://pkgs.tailscale.com/stable/#static"
    return 1
}

# Tests source this file for its functions; only a real invocation runs main.
if [ "$SOURCED" != 1 ]; then
    main "$@"
fi
