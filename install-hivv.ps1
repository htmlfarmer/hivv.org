# install-hivv.ps1 — drop the hivv CLI on Windows, instantly.
#
# Usage (from any PowerShell):
#   iwr -useb https://hivv.org/install-hivv.ps1 | iex
#   # for flags, bind args to the scriptblock:
#   & ([scriptblock]::Create((iwr -useb https://hivv.org/install-hivv.ps1).Content)) -Uninstall
#   & ([scriptblock]::Create((iwr -useb https://hivv.org/install-hivv.ps1).Content)) -Yes
#
# Flags:
#   -Prefix DIR     install root (default: $env:USERPROFILE\.local)
#   -NoPath         don't touch the user PATH / don't print PATH advice
#   -Yes            don't prompt — add to PATH and register the logon task
#   -WithLlm        also install Ollama + a model recommended for your hardware
#   -NoLlm          skip the Ollama/model prompt (default = prompt if interactive)
#   -NoService      skip the "start hivv at logon" scheduled task
#   -Uninstall      remove what we installed (files + scheduled task)
#   -Quiet          less output
#   -Help           this help
#
# Requires PowerShell 5+ (ships with Windows 10 / 11) and a python3 on PATH.
# Tip: prefer PowerShell 7 (`winget install Microsoft.PowerShell`) for emoji +
# UTF-8 output. Works on Windows Terminal, powershell.exe, and pwsh.

[CmdletBinding()]
param(
    [string]$Prefix = "",
    [switch]$NoPath,
    [switch]$Yes,
    [switch]$WithLlm,
    [switch]$NoLlm,
    [switch]$NoService,
    [switch]$Uninstall,
    [switch]$Quiet,
    [switch]$Help
)

$App = "hivv"
$Hub = "https://hivv.org"
$TaskName = "hivv-agent"

# ── brand colors ───────────────────────────────────────────────────────
$ColorReset     = [ConsoleColor]::Gray
$ColorBold      = [ConsoleColor]::White
$ColorDim       = [ConsoleColor]::DarkGray
$ColorBrand     = [ConsoleColor]::DarkYellow      # hivv = honey/amber
$ColorBrand2    = [ConsoleColor]::Yellow
$ColorAccent    = [ConsoleColor]::Cyan
$ColorSuccess   = [ConsoleColor]::Green
$ColorWarn      = [ConsoleColor]::Yellow
$ColorError     = [ConsoleColor]::Red

if ($Help) {
    Write-Host @"

  install-hivv.ps1 — Windows installer for the $App CLI

  install:   iwr -useb $Hub/install-$App.ps1 | iex
  uninstall: & ([scriptblock]::Create((iwr -useb $Hub/install-$App.ps1).Content)) -Uninstall

  flags:
    -Prefix DIR    install root (default: `$env:USERPROFILE\.local)
    -NoPath        don't touch user PATH
    -Yes           no prompts (PATH + logon task) — for scripts
    -WithLlm       also install Ollama + a recommended open model
    -NoLlm         skip the Ollama/model prompt
    -NoService     skip the 'start $App at logon' scheduled task
    -Quiet         less output

  Want the Linux-style experience instead? Install WSL:  wsl --install

"@
    exit 0
}

function _Say([string]$m)   { if (-not $Quiet) { Write-Host "  ✓ $m" -ForegroundColor $ColorSuccess } }
function _Log([string]$m)   { if (-not $Quiet) { Write-Host "  $m" -ForegroundColor $ColorAccent } }
function _Warn([string]$m)  { Write-Host "  ! $m" -ForegroundColor $ColorWarn }
function _Die([string]$m)  { Write-Host "  ✗ $m" -ForegroundColor $ColorError; exit 1 }

# ── banner ─────────────────────────────────────────────────────────────
function Show-Banner {
    Write-Host ""
    Write-Host "      ██╗  ██╗██╗██╗   ██╗██╗   ██╗" -ForegroundColor $ColorBrand
    Write-Host "      ██║  ██║██║██║   ██║██║   ██║" -ForegroundColor $ColorBrand
    Write-Host "      ███████║██║██║   ██║██║   ██║" -ForegroundColor $ColorBrand
    Write-Host "      ██╔══██║██║╚██╗ ██╔╝╚██╗ ██╔╝" -ForegroundColor $ColorBrand
    Write-Host "      ██║  ██║██║ ╚████╔╝  ╚████╔╝ " -ForegroundColor $ColorBrand
    Write-Host "      ╚═╝  ╚═╝╚═╝  ╚═══╝    ╚═══╝ " -ForegroundColor $ColorBrand
    Write-Host ""
    Write-Host "  🐝  the friendly network agent" -ForegroundColor $ColorBrand2
    Write-Host "  talk to AI · contribute CPU · stay safe" -ForegroundColor $ColorDim
    Write-Host ""
}

# ── uninstall ──────────────────────────────────────────────────────────
if (-not $Prefix) {
    $Prefix = Join-Path $env:USERPROFILE ".local"
}
$BinDir     = Join-Path $Prefix "bin"
$LibDir     = Join-Path $Prefix "lib\$App"
$ClientFile = Join-Path $LibDir  "$App-cli.py"
$Wrapper    = Join-Path $BinDir "$App.cmd"

if ($Uninstall) {
    Write-Host "  uninstalling" -ForegroundColor $ColorAccent
    # stop the agent and remove the logon task first
    try { & $Wrapper stop 2>$null } catch {}
    try {
        if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            _Say "removed scheduled task: $TaskName"
        }
    } catch { _Warn "couldn't check scheduled task: $($_.Exception.Message)" }
    foreach ($p in @($Wrapper, $ClientFile)) {
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }
    if (Test-Path -LiteralPath $LibDir) {
        Remove-Item -LiteralPath $LibDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    _Say "removed $Wrapper and $ClientFile"
    _Log "(your data in ~\.config\$App\ and ~\.local\share\$App\ was kept)"
    exit 0
}

# ── preflight ──────────────────────────────────────────────────────────
# Python is required for the CLI. Try to install it automatically rather than
# dying with a bare "install this first" message.
function Update-PathFromRegistry {
    $parts = @()
    foreach ($scope in @('Machine', 'User')) {
        try {
            $p = [Environment]::GetEnvironmentVariable('Path', $scope)
            if ($p) { $parts += $p }
        } catch { }
    }
    if ($parts.Count -gt 0) { $env:Path = ($parts -join ';') + ';' + $env:Path }
}

$py = $null
foreach ($candidate in @("python", "python3", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $py = $candidate; break }
}
if (-not $py) {
    Write-Host "  → python not found — installing it for you..." -ForegroundColor $ColorAccent
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --silent --accept-source-agreements --accept-package-agreements `
            --id Python.Python.3.12 2>&1 | Out-Null
        Update-PathFromRegistry
    } elseif (Get-Command scoop -ErrorAction SilentlyContinue) {
        & scoop install python 2>&1 | Out-Null
    } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
        & choco install python -y 2>&1 | Out-Null
    }
    foreach ($candidate in @("python", "python3", "py")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { $py = $candidate; break }
    }
}
if (-not $py) {
    _Die "python not found on PATH. Install Python 3 and reopen PowerShell:"
    Write-Host "    winget install Python.Python.3.12" -ForegroundColor $ColorAccent
    Write-Host "    or download from https://python.org/downloads/" -ForegroundColor $ColorAccent
}

$pyVersion = & $py -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>$null
if (-not $pyVersion) { $pyVersion = "unknown" }

Show-Banner

Write-Host "  environment" -ForegroundColor $ColorBold
Write-Host "  ✓ python     $pyVersion" -ForegroundColor $ColorSuccess
Write-Host "  ✓ os         $($env:OS) $($env:PROCESSOR_ARCHITECTURE)" -ForegroundColor $ColorSuccess
Write-Host "  ✓ prefix     $Prefix" -ForegroundColor $ColorSuccess
Write-Host ""

# ── ensure dirs ────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $BinDir, $LibDir | Out-Null

# ── download client + wrapper ──────────────────────────────────────────
function Fetch([string]$url, [string]$dest, [string]$label) {
    Write-Host "  ↓ downloading $label ..." -NoNewline -ForegroundColor $ColorAccent
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -ErrorAction Stop
        $head = Get-Content -LiteralPath $dest -TotalCount 1 -ErrorAction SilentlyContinue
        if ($head -match '<!DOCTYPE|<html') { throw "server returned an HTML page instead of $label (CDN misconfigured?)" }
        Write-Host "`r  ✓ $label                    " -ForegroundColor $ColorSuccess
    } catch {
        Write-Host ""
        _Die "download failed: $url`n    $($_.Exception.Message)"
    }
}

Fetch "$Hub/$App-cli.py" $ClientFile "$App-cli.py"
Fetch "$Hub/$App.cmd"    $Wrapper    "$App.cmd"

# chmod (not really needed on Windows but harmless)
# ── success card ──────────────────────────────────────────────────────
$Bar = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
Write-Host ""
Write-Host "  $Bar" -ForegroundColor $ColorSuccess
Write-Host "  ✓ $App CLI installed" -ForegroundColor $ColorBold
Write-Host "  $Bar" -ForegroundColor $ColorSuccess
Write-Host ""

Write-Host "  location" -ForegroundColor $ColorBold
Write-Host "    wrapper   $Wrapper" -ForegroundColor $ColorAccent
Write-Host "    client    $ClientFile" -ForegroundColor $ColorAccent
Write-Host "    config    ~\.config\$App\" -ForegroundColor $ColorDim
Write-Host "    state     ~\.local\share\$App\" -ForegroundColor $ColorDim
Write-Host ""

Write-Host "  🌌 how $App works — 30-second tour" -ForegroundColor $ColorBold
Write-Host "    · the $Hub hub is just a phone book — it knows who's online, nothing more"
Write-Host "    · your PC is a node — the agent heartbeats to the hub; peers talk direct"
Write-Host "    · AI requests route to the best brain: your local model, a peer, or a donated cloud key"
Write-Host "    · your chats stay between nodes — the hub never sees prompt content"
Write-Host "    · sharing is opt-in: ``$App volunteer on`` lets the hive queue jobs on idle CPU/GPU"
Write-Host ""

Write-Host "  location" -ForegroundColor $ColorBold
Write-Host "    wrapper   $Wrapper" -ForegroundColor $ColorAccent
Write-Host "    client    $ClientFile" -ForegroundColor $ColorAccent
Write-Host "    config    ~\.config\$App\" -ForegroundColor $ColorDim
Write-Host "    state     ~\.local\share\$App\  (agent.log, agent.pid)" -ForegroundColor $ColorDim
Write-Host ""

Write-Host "  first steps — try these right now" -ForegroundColor $ColorBold
Write-Host "    `$App up                 quick status check (is my node alive?)" -ForegroundColor $ColorDim
Write-Host "    `$App hi                 say hello to the hive 🐝" -ForegroundColor $ColorDim
Write-Host "    `$App chat `"hello`"     talk to the distributed AI" -ForegroundColor $ColorDim
Write-Host "    `$App nodes              see every bee online right now" -ForegroundColor $ColorDim
Write-Host "    `$App welcome            guided tour of everything" -ForegroundColor $ColorDim
Write-Host ""

Write-Host "  running your node 🐝" -ForegroundColor $ColorBold
Write-Host "    `$App start              go live — your CPU/GPU joins the hive" -ForegroundColor $ColorDim
Write-Host "    `$App stop / restart     control the agent" -ForegroundColor $ColorDim
Write-Host "    `$App volunteer on       enable running AI jobs from the hive (opt-in)" -ForegroundColor $ColorDim
Write-Host "    `$App llm status         check your local AI setup (open weights)" -ForegroundColor $ColorDim
Write-Host "    `$App firewall           see who can reach your node (localhost by default)" -ForegroundColor $ColorDim
Write-Host ""

Write-Host "  when you need help" -ForegroundColor $ColorBold
Write-Host "    `$App doctor             full health report        `$App support    ask the AI (attaches diagnostics)" -ForegroundColor $ColorDim
Write-Host "    `$App doctor --fix       auto-fix common issues    `$App debug      everything at once for bug reports" -ForegroundColor $ColorDim
Write-Host ""

# ── optional: install Ollama + a recommended model ───────────────────
# Prompts the user at the end of the install unless -WithLlm / -NoLlm
# is set. Uses the just-installed `${App} llm suggest --json` as the source
# of truth for which model to pull — PowerShell doesn't re-implement tier logic.
$shouldInstallLlm = $false
if ($WithLlm)                              { $shouldInstallLlm = $true }
elseif (-not $NoLlm -and [Environment]::UserInteractive) {
    $ans = Read-Host " Install Ollama + a model recommended for your hardware? [y/N]"
    if ($ans -match '^[Yy]') { $shouldInstallLlm = $true }
}

if ($shouldInstallLlm) {
    # Put the just-installed bin first so a stale `$App` shadowing it on PATH
    # doesn't break the just-installed `${App} llm suggest` call.
    if (($env:Path -split ';') -notcontains $BinDir) {
        $env:Path = "$BinDir;$env:Path"
    }

    Write-Host ""
    Write-Host "  🐝 $App llm setup" -ForegroundColor $ColorBold
    Write-Host ""

    $haveOllama = $null -ne (Get-Command ollama -ErrorAction SilentlyContinue)
    if ($haveOllama) {
        _Say "ollama already installed — skipping install"
    } else {
        _Log "ollama not found — installing..."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            & winget install --id Ollama.Ollama --accept-source-agreements --accept-package-agreements | Out-Host
        } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
            & choco install ollama -y | Out-Host
        } else {
            _Warn "install winget or chocolatey, or download from https://ollama.com/download"
        }
        $haveOllama = $null -ne (Get-Command ollama -ErrorAction SilentlyContinue)
    }

    if ($haveOllama) {
        # Ask the just-installed CLI to recommend — falls back to gemma4:e2b
        # if JSON parse fails for any reason.
        $picked = & $App llm suggest --json 2>$null | & $py -c "import json,sys; print(json.load(sys.stdin).get('primary','gemma4:e2b'))" 2>$null
        if (-not $picked) { $picked = 'gemma4:e2b' }

        Write-Host ""
        Write-Host "  recommended model: $picked" -ForegroundColor $ColorAccent
        Write-Host ""
        & ollama pull $picked | Out-Host
        if ($LASTEXITCODE -eq 0) {
            _Say "pulled $picked"
        } else {
            _Warn "pull failed — you can retry with: ollama pull $picked"
        }
        Write-Host ""
        Write-Host "  ✓ try it:     ${App} chat `"hello`" --local" -ForegroundColor $ColorSuccess
        Write-Host "  ✓ more info:  ${App} llm status" -ForegroundColor $ColorSuccess
        Write-Host ""
    } else {
        _Warn "ollama not on PATH after install — reopen PowerShell and run ``ollama --version``"
    }
}

# ── logon task — start your node automatically at sign-in ─────────────
$taskInstalled = $false
if (-not $NoService) {
    $register = [bool]$Yes
    if (-not $register -and [Environment]::UserInteractive) {
        $ans = Read-Host " Start $App automatically at logon? [Y/n]"
        if ($ans -notmatch '^[Nn]') { $register = $true }
    }
    if ($register) {
        try {
            if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            }
            $action   = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Wrapper`" start"
            $trigger  = New-ScheduledTaskTrigger -AtLogOn
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                -Description "$App node agent — contributes this PC to the people's distributed AI network ($Hub)" | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            _Say "you're live 🐝 — '$TaskName' starts your node at every logon"
            $taskInstalled = $true
        } catch {
            _Warn "couldn't register the logon task — start manually instead: $App start"
        }
    } else {
        _Log "auto-start skipped — run '$App start' whenever you want your node live (add -Yes to install it silently)"
    }
}

# ── PATH advice ──────────────────────────────────────────────────────
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathDirs = if ($userPath) { $userPath -split ";" } else { @() }
$needsPath = $pathDirs -notcontains $BinDir

if ($needsPath) {
    if ($NoPath) {
        # user said don't mention it
    } elseif ($Yes) {
        $newUserPath = if ($userPath) { "$BinDir;$userPath" } else { $BinDir }
        [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
        _Say "added $BinDir to user PATH"
        Write-Host "       (restart your PowerShell window to pick up)" -ForegroundColor $ColorDim
    } else {
        Write-Host "  note" -ForegroundColor ($ColorWarn)
        Write-Host "    $BinDir is not on your PATH" -ForegroundColor $ColorWarn
        Write-Host "    Add it for this user (reopen terminal after):" -ForegroundColor $ColorDim
        Write-Host "      `$env:Path = `"$BinDir;`$env:Path`"" -ForegroundColor $ColorAccent
        Write-Host "      [Environment]::SetEnvironmentVariable('Path', `"$BinDir;`" + [Environment]::GetEnvironmentVariable('Path','User'), 'User')" -ForegroundColor $ColorAccent
        Write-Host ""
        Write-Host "    Or invoke directly while you're here:" -ForegroundColor $ColorDim
        Write-Host "      & `"$Wrapper`" --help" -ForegroundColor $ColorAccent
    }
}

# ── footer ────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "  ⭐  github.com/$App  ·  ${App}.org  ·  support@${App}.org" -ForegroundColor $ColorBrand2
Write-Host ""
