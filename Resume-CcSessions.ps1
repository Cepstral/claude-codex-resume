# =============================================================================
#  Resume-CcSessions.ps1  -  multi-select resume picker for Claude Code + Codex
#
#  Dot-sourced from Microsoft.PowerShell_profile.ps1 (same folder). Defines the
#  Resume-CcSessions function (alias: ccr) plus Ccr-prefixed helpers.
#
#  Cross-platform: Windows (tabs via Windows Terminal), macOS/Linux (windows
#  via tmux; without tmux one session at a time, in the current terminal).
#
#  Data sources are the same files each tool's own resume feature reads - no
#  databases, no external modules - so this keeps working across tool updates:
#    claude : <config dir>\projects\<slug>\<uuid>.jsonl (bounded head/tail reads;
#             config dir = $env:CLAUDE_CONFIG_DIR, falling back to ~\.claude)
#    codex  : ~\.codex\sessions\**\rollout-*.jsonl     (bounded head reads)
#  Parsing is defensive: an unrecognized file degrades to a worse title,
#  never to a crash of the listing.
# =============================================================================

# =============================================================================
#  shared helpers
# =============================================================================

# Shown in the picker hint line; bump on every change so a stale function
# loaded by an old tab is immediately recognizable.
$script:CcrVersion = '0.40'

# Optional multi-account config: ccr.json next to this file (or the file named
# by $env:CCR_CONFIG). Captured at load time - $PSScriptRoot is only set while
# the file is being dot-sourced.
$script:CcrConfigPath = if ($env:CCR_CONFIG) { $env:CCR_CONFIG }
elseif ($PSScriptRoot) { Join-Path $PSScriptRoot 'ccr.json' }
else { $null }

# PowerShell 5.1 has no $IsWindows automatic variable (and only runs on
# Windows). pwsh 6+ provides it read-only.
if ($PSVersionTable.PSVersion.Major -lt 6) { $script:IsWindows = $true }

# Read the first/last $Bytes of a file as UTF-8 text. Opened with a permissive
# share mode because claude/codex may be appending to the file right now.
function Read-CcrFileWindow {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][long]$Bytes,
        [Parameter(Mandatory)][ValidateSet('Head', 'Tail')][string]$From
    )
    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
    try {
        $take = [int][Math]::Min($Bytes, $fs.Length)
        if ($From -eq 'Tail') { $null = $fs.Seek(-$take, [System.IO.SeekOrigin]::End) }
        $buf = [byte[]]::new($take)
        $read = 0
        while ($read -lt $take) {
            $n = $fs.Read($buf, $read, $take - $read)
            if ($n -le 0) { break }
            $read += $n
        }
        [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
    }
    finally { $fs.Dispose() }
}

# Read up to $MaxLines lines / ~$MaxBytes bytes from the start of a file.
# Same permissive share mode as above; the byte cap is approximate (buffered).
function Read-CcrHeadLines {
    param(
        [Parameter(Mandatory)][string]$Path,
        [int]$MaxLines = [int]::MaxValue,
        [long]$MaxBytes = [long]::MaxValue
    )
    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
    try {
        $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8, $true)
        try {
            $lines = [System.Collections.Generic.List[string]]::new()
            while ($lines.Count -lt $MaxLines -and $fs.Position -lt $MaxBytes) {
                $line = $sr.ReadLine()
                if ($null -eq $line) { break }
                $lines.Add($line)
            }
            , $lines.ToArray()
        }
        finally { $sr.Dispose() }
    }
    finally { $fs.Dispose() }
}

# Unescape the inner text of a JSON string literal captured by regex.
function ConvertFrom-CcrJsonString {
    param([string]$Raw)
    if ($null -eq $Raw) { return $null }
    try { ('"' + $Raw + '"') | ConvertFrom-Json } catch { $Raw }
}

# One-line, control-char-free, length-capped display title (or $null).
function ConvertTo-CcrTitle {
    param([string]$Raw)
    if (-not $Raw) { return $null }
    $t = (($Raw -replace '\p{C}', ' ') -replace '\s+', ' ').Trim()
    if (-not $t) { return $null }
    if ($t.Length -gt 100) { $t = $t.Substring(0, 99) + [char]0x2026 }
    $t
}

# First-wins table of one parsed JSONL object per id (accelerator for titles).
function Get-CcrHistoryTable {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$IdName)
    $table = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $table }
    try {
        foreach ($line in (Read-CcrHeadLines -Path $Path)) {
            if (-not $line) { continue }
            try { $o = $line | ConvertFrom-Json } catch { continue }
            $id = $o.$IdName
            if ($id -and -not $table.ContainsKey($id)) { $table[$id] = $o }
        }
    }
    catch { }
    $table
}

function Format-CcrAge {
    param([datetime]$Utc)
    $span = [datetime]::UtcNow - $Utc
    if ($span.TotalMinutes -lt 1) { 'now' }
    elseif ($span.TotalMinutes -lt 60) { '{0}m' -f [int][Math]::Floor($span.TotalMinutes) }
    elseif ($span.TotalHours -lt 24) { '{0}h' -f [int][Math]::Floor($span.TotalHours) }
    elseif ($span.TotalDays -lt 14) { '{0}d' -f [int][Math]::Floor($span.TotalDays) }
    else { $Utc.ToLocalTime().ToString('MMM d', [cultureinfo]::InvariantCulture) }
}

function Format-CcrCwd {
    param([string]$Path, [int]$Max)
    if (-not $Path) { return '' }
    $sep = [System.IO.Path]::DirectorySeparatorChar
    $p = $Path
    if ($p.StartsWith($HOME, [StringComparison]::OrdinalIgnoreCase)) { $p = '~' + $p.Substring($HOME.Length) }
    if ($p.Length -gt $Max) {
        $segs = $p -split '[\\/]' | Where-Object { $_ }
        if ($segs.Count -ge 2) { $p = [string][char]0x2026 + $sep + ($segs[-2..-1] -join $sep) }
    }
    if ($p.Length -gt $Max -and $Max -ge 2) { $p = $p.Substring(0, $Max - 1) + [char]0x2026 }
    $p
}

# =============================================================================
#  claude enumerator
# =============================================================================

# Claude Code's data dir: CLAUDE_CONFIG_DIR when set (e.g. relocated to a
# synced folder), else the stock ~\.claude.
function Get-CcrClaudeRoot {
    if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
}

# Codex's data dir: CODEX_HOME when set, else the stock ~\.codex. Holds
# auth.json, config.toml, sessions/ and the thread catalog.
function Get-CcrCodexRoot {
    if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
}

# Parsed ccr.json (or $null when absent/unreadable).
function Get-CcrConfig {
    $cfgPath = $script:CcrConfigPath
    if (-not $cfgPath -or -not (Test-Path -LiteralPath $cfgPath)) { return $null }
    try { Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json }
    catch { Write-Warning "ccr: cannot read $cfgPath ($_) - using the single default config dirs"; $null }
}

function Expand-CcrPath([string]$Path) {
    if ($Path -match '^~([\\/]|$)') { $Path = $HOME + $Path.Substring(1) }
    $Path = [System.Environment]::ExpandEnvironmentVariables($Path)
    try { [System.IO.Path]::GetFullPath($Path) } catch { $Path }   # normalizes separators
}

# All config dirs ccr should scan for one tool, as @{ Label; Path; Default }.
# One account (no ccr.json, or no entry for the tool): a single unlabeled
# root = the tool's default dir, so behavior is unchanged. Several accounts:
# ccr.json lists one config dir per account label, each with its own login
# inside (`claude auth login` / `codex login` run with the dir selected):
#   { "claudeRoots": { "personal": "~/.claude",  "work": "~/.claude-work" },
#     "codexRoots":  { "personal": "~/.codex",   "work": "~/.codex-work" },
#     "defaultRoot": "personal" }
# "~" and %VAR% expand; a missing dir is kept (it may exist on another PC).
function Get-CcrRoots {
    param([Parameter(Mandatory)][ValidateSet('claude', 'codex')][string]$Tool)
    $defaultPath = if ($Tool -eq 'claude') { Get-CcrClaudeRoot } else { Get-CcrCodexRoot }
    $single = @([pscustomobject]@{ Label = ''; Path = $defaultPath; Default = $true })
    $cfg = Get-CcrConfig
    if (-not $cfg) { return $single }
    $map = if ($Tool -eq 'claude') { $cfg.claudeRoots } else { $cfg.codexRoots }
    if (-not $map) { return $single }
    $roots = @(foreach ($p in $map.PSObject.Properties) {
            [pscustomobject]@{ Label = $p.Name; Path = (Expand-CcrPath ([string]$p.Value)); Default = ($p.Name -eq [string]$cfg.defaultRoot) }
        })
    if ($roots.Count -eq 0) { return $single }
    if (-not ($roots | Where-Object Default)) { $roots[0].Default = $true }
    $roots
}
function Get-CcrClaudeRoots { Get-CcrRoots -Tool claude }
function Get-CcrCodexRoots { Get-CcrRoots -Tool codex }

# --- account management (ccr -Accounts / -AddAccount / -RemoveAccount) -------

# Who is logged in inside a config dir, without touching the live default
# dir: run the tool's own status command with the dir selected.
function Get-CcrLoginIdentity {
    param([Parameter(Mandatory)][ValidateSet('claude', 'codex')][string]$Tool, [Parameter(Mandatory)][string]$RootPath)
    if (-not (Test-Path -LiteralPath $RootPath)) { return '(dir missing)' }
    $var = if ($Tool -eq 'claude') { 'CLAUDE_CONFIG_DIR' } else { 'CODEX_HOME' }
    $prev = [System.Environment]::GetEnvironmentVariable($var)
    [System.Environment]::SetEnvironmentVariable($var, $RootPath)
    try {
        if ($Tool -eq 'claude') {
            $raw = & claude auth status --json 2>$null | Out-String
            try { $j = $raw | ConvertFrom-Json } catch { return '(unknown)' }
            if (-not $j.loggedIn) { return 'not logged in' }
            $who = if ($j.email) { $j.email } else { $j.authMethod }
            if ($j.subscriptionType) { "$who ($($j.subscriptionType))" } else { "$who" }
        }
        else {
            # codex writes its status line to stderr.
            $line = (& codex login status 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -and $_ -notmatch '^WARNING' } | Select-Object -First 1)
            if ($line) { "$line".Trim() }
            elseif (Test-Path -LiteralPath (Join-Path $RootPath 'auth.json')) { 'logged in (auth.json present)' }
            else { 'not logged in' }
        }
    }
    catch { '(unknown)' }
    finally { [System.Environment]::SetEnvironmentVariable($var, $prev) }
}

# The logged-in email of a config dir, read from the files the tools keep
# there - no process spawned, so it is cheap enough for every picker start.
# Claude: .claude.json oauthAccount.emailAddress. Codex: the email claim of
# the OpenID token in auth.json (its payload is plain base64url JSON).
# '' when there is no login there.
function Get-CcrQuickIdentity {
    param([Parameter(Mandatory)][ValidateSet('claude', 'codex')][string]$Tool, [Parameter(Mandatory)][string]$RootPath)
    try {
        if ($Tool -eq 'claude') {
            $f = Join-Path $RootPath '.claude.json'
            if (-not (Test-Path -LiteralPath $f)) { return '' }
            return "$((Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).oauthAccount.emailAddress)"
        }
        $f = Join-Path $RootPath 'auth.json'
        if (-not (Test-Path -LiteralPath $f)) { return '' }
        $jwt = "$((Get-Content -LiteralPath $f -Raw | ConvertFrom-Json).tokens.id_token)"
        if (-not $jwt) { return '' }
        $b = $jwt.Split('.')[1].Replace('-', '+').Replace('_', '/')
        $b += '=' * ((4 - $b.Length % 4) % 4)
        return "$(([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)) | ConvertFrom-Json).email)"
    }
    catch { '' }
}

function Show-CcrAccounts {
    $cfg = Get-CcrConfig
    if (-not $cfg -or (-not $cfg.claudeRoots -and -not $cfg.codexRoots)) {
        Write-Host "ccr: no accounts configured (single default config dirs). Add one with: ccr -AddAccount <label>"
        Write-Host "  config file: $($script:CcrConfigPath)"
        return
    }
    Write-Host "accounts in $($script:CcrConfigPath)  (default: $($cfg.defaultRoot))"
    foreach ($tool in 'claude', 'codex') {
        $roots = @(Get-CcrRoots -Tool $tool)
        if ($roots.Count -eq 1 -and -not $roots[0].Label) { Write-Host "  ${tool}: single default dir $($roots[0].Path)"; continue }
        foreach ($r in $roots) {
            $who = Get-CcrLoginIdentity -Tool $tool -RootPath $r.Path
            Write-Host ("  {0,-6} {1,-12} {2,-45} {3}" -f $tool, $r.Label, (Format-CcrCwd $r.Path 45), $who)
        }
    }
}

# Write ccr.json back. Only the keys ccr owns are touched.
function Save-CcrConfig([object]$Config) {
    if (-not $script:CcrConfigPath) { throw 'ccr: no config path (load the script from a file, or set $env:CCR_CONFIG)' }
    $Config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $script:CcrConfigPath -Encoding utf8NoBOM
}

# Multi-account mode starts by giving the dirs the tools use today a label,
# so the sessions already there keep an account name: that label is fixed,
# "default", and nothing moves.
$script:CcrDefaultLabel = 'default'

# How an account label is shown: the default account always in parentheses.
function Format-CcrAcctLabel([string]$Label, [bool]$IsDefault) { if ($IsDefault) { "($Label)" } else { $Label } }

function Get-CcrMapCount([object]$Map) { if ($Map) { @($Map.PSObject.Properties).Count } else { 0 } }

# Is multi-account mode on (ccr.json lists at least one account)?
function Test-CcrMultiAccount {
    $cfg = Get-CcrConfig
    [bool]($cfg -and ((Get-CcrMapCount $cfg.claudeRoots) -or (Get-CcrMapCount $cfg.codexRoots)))
}

# Turn multi-account mode on: record the current default dir of each tool
# under the "default" label. Returns the config; no-op when already on.
function Enable-CcrMultiAccount {
    $cfg = Get-CcrConfig
    if (-not $cfg) { $cfg = [pscustomobject]@{} }
    foreach ($key in 'claudeRoots', 'codexRoots') {
        if (-not $cfg.PSObject.Properties[$key] -or -not $cfg.$key) { $cfg | Add-Member -NotePropertyName $key -NotePropertyValue ([pscustomobject]@{}) -Force }
    }
    if ((Get-CcrMapCount $cfg.claudeRoots) -or (Get-CcrMapCount $cfg.codexRoots)) { return $cfg }
    $cfg.claudeRoots | Add-Member -NotePropertyName $script:CcrDefaultLabel -NotePropertyValue (Get-CcrClaudeRoot)
    $cfg.codexRoots | Add-Member -NotePropertyName $script:CcrDefaultLabel -NotePropertyValue (Get-CcrCodexRoot)
    $cfg | Add-Member -NotePropertyName defaultRoot -NotePropertyValue $script:CcrDefaultLabel -Force
    Save-CcrConfig $cfg
    Write-Host "ccr: multi-account mode is on - the dirs claude and codex use today are the '$($script:CcrDefaultLabel)' account (nothing moved)." -ForegroundColor Green
    $cfg
}

# Does this claude config dir carry a status line (statusLine in its
# settings.json)?
function Test-CcrStatusline([string]$RootPath) {
    $sj = Join-Path $RootPath 'settings.json'
    if (-not (Test-Path -LiteralPath $sj)) { return $false }
    try { [bool]((Get-Content -LiteralPath $sj -Raw | ConvertFrom-Json).PSObject.Properties['statusLine']) } catch { $false }
}

# Give a claude account the status line of another one: the statusLine
# entry is merged into the target's settings.json (other keys untouched)
# and the statusline* script files next to it are copied over (logs
# excluded). Claude resolves the script through CLAUDE_CONFIG_DIR at run
# time, so the copy works unchanged in the new dir.
function Copy-CcrStatusline {
    param([Parameter(Mandatory)][string]$FromPath, [Parameter(Mandatory)][string]$ToPath)
    $src = Join-Path $FromPath 'settings.json'
    if (-not (Test-CcrStatusline $FromPath)) { throw "ccr: no statusLine in $src - nothing to copy" }
    $entry = (Get-Content -LiteralPath $src -Raw | ConvertFrom-Json).statusLine
    New-Item -ItemType Directory -Path $ToPath -Force | Out-Null
    $dst = Join-Path $ToPath 'settings.json'
    $cfg = if (Test-Path -LiteralPath $dst) { Get-Content -LiteralPath $dst -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
    $cfg | Add-Member -NotePropertyName statusLine -NotePropertyValue $entry -Force
    $cfg | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $dst -Encoding utf8NoBOM
    $files = @(Get-ChildItem -LiteralPath $FromPath -File -Filter 'statusline*' -ErrorAction SilentlyContinue | Where-Object { $_.Extension -notin '.log', '.txt' })
    foreach ($f in $files) { Copy-Item -LiteralPath $f.FullName -Destination (Join-Path $ToPath $f.Name) -Force }
    Write-Host "ccr: status line copied to $(Format-CcrCwd $ToPath 50): statusLine in settings.json$(if ($files.Count) { " + $($files.Name -join ', ')" })"
}

# Register a new account for one tool (or both): a config dir NEXT TO the
# tool's default dir (so a default in OneDrive\.claude gets
# OneDrive\.claude-<label>, and ~\.codex gets ~\.codex-<label>), the
# tool's own interactive login run inside it - skipped when the dir already
# holds a login from an earlier life - and the entry recorded in ccr.json.
# Turns multi-account mode on first when needed.
function Add-CcrAccount {
    param(
        [Parameter(Mandatory)][string]$Label,
        [ValidateSet('claude', 'codex', 'all')][string]$Tool = 'all',
        # Claude only: give the new dir the default account's status line.
        [switch]$CopyStatusline
    )
    if ($Label -notmatch '^[A-Za-z0-9_-]{1,12}$') { throw "ccr: account label must be 1-12 letters/digits/_/- (got '$Label')" }
    if ($Label -eq $script:CcrDefaultLabel) { throw "ccr: '$Label' is the label of the dirs in use today - pick another one" }
    $cfg = Enable-CcrMultiAccount
    $tools = if ($Tool -eq 'all') { @('claude', 'codex') } else { @($Tool) }
    foreach ($t in $tools) {
        $map = if ($t -eq 'claude') { $cfg.claudeRoots } else { $cfg.codexRoots }
        if ($map.PSObject.Properties[$Label]) { Write-Warning "ccr: $t account '$Label' already configured - skipping"; continue }
        $defPath = @(Get-CcrRoots -Tool $t | Where-Object Default)[0].Path
        $dir = Join-Path (Split-Path -Parent $defPath) "$(Split-Path -Leaf $defPath)-$Label"
        $reused = Test-Path -LiteralPath $dir
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        $map | Add-Member -NotePropertyName $Label -NotePropertyValue $dir
        Save-CcrConfig $cfg   # record first, so an aborted login still leaves a usable entry
        if ($t -eq 'claude') {
            $from = @(Get-CcrRoots -Tool claude | Where-Object Default)[0].Path
            # A fresh dir has no .claude.json, and the first interactive
            # claude there runs the first-start wizard (theme, login...)
            # even though `claude auth login` already stored credentials.
            # Seed the flag it checks, plus the default account's theme.
            $cj = Join-Path $dir '.claude.json'
            if (-not (Test-Path -LiteralPath $cj)) {
                $seed = [ordered]@{ hasCompletedOnboarding = $true }
                try {
                    $defCj = Join-Path $from '.claude.json'
                    if (Test-Path -LiteralPath $defCj) {
                        $dj = Get-Content -LiteralPath $defCj -Raw | ConvertFrom-Json
                        foreach ($k in 'theme', 'lastOnboardingVersion') { if ($dj.PSObject.Properties[$k]) { $seed[$k] = $dj.$k } }
                    }
                }
                catch { }
                $seed | ConvertTo-Json | Set-Content -LiteralPath $cj -Encoding utf8NoBOM
            }
            if ($CopyStatusline) { try { Copy-CcrStatusline -FromPath $from -ToPath $dir } catch { Write-Warning "$_" } }
        }
        $hasLogin = Test-Path -LiteralPath (Join-Path $dir $(if ($t -eq 'claude') { '.credentials.json' } else { 'auth.json' }))
        if ($reused -and $hasLogin) {
            $already = Get-CcrQuickIdentity -Tool $t -RootPath $dir
            Write-Host "ccr: $t account '$Label' -> $dir  - existing dir, already logged in$(if ($already) { " as $already" }); no login needed" -ForegroundColor Yellow
            continue
        }
        Write-Host "ccr: $t account '$Label' -> $dir  - starting the $t login flow in that dir" -ForegroundColor Yellow
        $var = if ($t -eq 'claude') { 'CLAUDE_CONFIG_DIR' } else { 'CODEX_HOME' }
        $prev = [System.Environment]::GetEnvironmentVariable($var)
        [System.Environment]::SetEnvironmentVariable($var, $dir)
        try { if ($t -eq 'claude') { & claude auth login } else { & codex login } }
        finally { [System.Environment]::SetEnvironmentVariable($var, $prev) }
    }
    Write-Host ''
    Show-CcrAccounts
}

# Forget an account: every session it holds moves into the tool's default
# account first (claude: transcript + sidecar into the same project slug;
# codex: rollout into the same sessions/YYYY/MM/DD path), then the entry
# leaves ccr.json. The dir and its login stay on disk. Refused while one of
# its sessions is running, and for the default account itself.
function Remove-CcrAccount {
    param(
        [Parameter(Mandatory)][string]$Label,
        [ValidateSet('claude', 'codex', 'all')][string]$Tool = 'all'
    )
    $cfg = Get-CcrConfig
    if (-not $cfg) { throw 'ccr: no accounts configured' }
    $tools = if ($Tool -eq 'all') { @('claude', 'codex') } else { @($Tool) }
    # Plan everything before moving anything.
    $plan = foreach ($t in $tools) {
        $roots = @(Get-CcrRoots -Tool $t)
        $src = @($roots | Where-Object { $_.Label -eq $Label })
        if ($src.Count -eq 0) { continue }
        $dst = @($roots | Where-Object Default)[0]
        if ($dst.Label -eq $Label) { throw "ccr: '$Label' is the default $t account - it cannot be removed; turn multi-account mode off instead" }
        $sessions = @(if ($t -eq 'claude') { Get-CcrClaudeSession -Root $src[0] } else { Get-CcrCodexSession -Root $src[0] })
        $running = @($sessions | Where-Object Running)
        if ($running.Count) { throw "ccr: $($running.Count) $t session(s) of '$Label' are running - close them first" }
        [pscustomobject]@{ Tool = $t; Src = $src[0]; Dst = $dst; Sessions = $sessions }
    }
    if (-not $plan) { throw "ccr: no account '$Label' configured" }
    foreach ($step in $plan) {
        foreach ($sess in $step.Sessions) { [void](Move-CcrSessionToRoot -Session $sess -TargetRoot $step.Dst) }
        $map = if ($step.Tool -eq 'claude') { $cfg.claudeRoots } else { $cfg.codexRoots }
        $map.PSObject.Properties.Remove($Label)
        Write-Host "ccr: $($step.Tool) account '$Label' removed - $($step.Sessions.Count) session(s) moved to '$($step.Dst.Label)' ($(Format-CcrCwd $step.Dst.Path 50)); the dir $(Format-CcrCwd $step.Src.Path 50) and its login stay on disk."
    }
    Save-CcrConfig $cfg
}

# Turn multi-account mode off: every other account's sessions move into the
# default account of its tool (see Remove-CcrAccount), then the account keys
# leave ccr.json (the file goes too when nothing else is in it), so both
# tools are back to their single default dir. Other dirs and their logins
# stay on disk.
function Disable-CcrMultiAccount {
    $cfg = Get-CcrConfig
    if (-not (Test-CcrMultiAccount)) { Write-Host 'ccr: multi-account mode is not on.'; return }
    foreach ($t in 'claude', 'codex') {
        $roots = @(Get-CcrRoots -Tool $t)
        $dst = @($roots | Where-Object Default)[0]
        $plain = if ($t -eq 'claude') { Get-CcrClaudeRoot } else { Get-CcrCodexRoot }
        if ($dst.Label -and (Expand-CcrPath $dst.Path) -ne (Expand-CcrPath $plain)) {
            throw "ccr: the default $t account lives in $($dst.Path), but $t itself uses $plain - the sessions would disappear from ccr. Make them the same dir first."
        }
    }
    $labels = @(@(Get-CcrClaudeRoots) + @(Get-CcrCodexRoots) | Where-Object { $_.Label -and -not $_.Default } | ForEach-Object Label | Select-Object -Unique)
    foreach ($lbl in $labels) { Remove-CcrAccount -Label $lbl }
    $cfg = Get-CcrConfig
    foreach ($key in 'claudeRoots', 'codexRoots', 'defaultRoot') { if ($cfg.PSObject.Properties[$key]) { $cfg.PSObject.Properties.Remove($key) } }
    if (@($cfg.PSObject.Properties).Count -eq 0) { Remove-Item -LiteralPath $script:CcrConfigPath -Force } else { Save-CcrConfig $cfg }
    Write-Host 'ccr: multi-account mode is off - claude and codex are back to their single default dirs.' -ForegroundColor Green
}

# sessionId -> live claude process id (stale pid files filtered out).
function Get-CcrClaudeRunningMap {
    param([string]$RootPath = (Get-CcrClaudeRoot))
    $map = @{}
    $dir = Join-Path $RootPath 'sessions'
    if (-not (Test-Path -LiteralPath $dir)) { return $map }
    foreach ($f in Get-ChildItem -LiteralPath $dir -Filter *.json -File -ErrorAction SilentlyContinue) {
        if ($f.BaseName -notmatch '^\d+$') { continue }
        try {
            $o = Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json
            if ($o.sessionId -and (Get-Process -Id $o.pid -ErrorAction SilentlyContinue)) {
                $map[$o.sessionId] = [int]$o.pid
            }
        }
        catch { }
    }
    $map
}

function Get-CcrClaudeSession {
    [CmdletBinding()]
    param(
        # One entry from Get-CcrClaudeRoots; omitted = the single default root.
        [object]$Root = $null
    )
    if (-not $Root) { $Root = [pscustomobject]@{ Label = ''; Path = (Get-CcrClaudeRoot); Default = $true } }
    $projRoot = Join-Path $Root.Path 'projects'
    if (-not (Test-Path -LiteralPath $projRoot)) { return @() }
    $running = Get-CcrClaudeRunningMap -RootPath $Root.Path
    $hist = $null   # <root>\history.jsonl, loaded lazily only if a fallback is needed

    # Depth 1 only: subfolders hold subagent transcripts, never resumable.
    $files = Get-ChildItem -LiteralPath $projRoot -Directory -ErrorAction SilentlyContinue |
        Get-ChildItem -Filter *.jsonl -File -ErrorAction SilentlyContinue

    $out = foreach ($file in $files) {
        if ($file.BaseName -notmatch '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$') { continue }
        $id = $file.BaseName
        try {
            $head = Read-CcrFileWindow -Path $file.FullName -Bytes 16KB -From Head

            # Exclude what the real /resume picker excludes (only on positive markers).
            $m = [regex]::Match($head, '"isSidechain":(true|false)')
            if ($m.Success -and $m.Groups[1].Value -eq 'true') { continue }
            if ($head -match '"entrypoint":"daemon"') { continue }

            $tail = Read-CcrFileWindow -Path $file.FullName -Bytes 128KB -From Tail

            # Cwd: first structural "cwd" in the head (metadata preamble has none).
            $cwd = $null
            $m = [regex]::Match($head, '"cwd":"((?:[^"\\]|\\.)*)"')
            if ($m.Success) { $cwd = ConvertFrom-CcrJsonString $m.Groups[1].Value }
            if (-not $cwd) {
                if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path $Root.Path 'history.jsonl') -IdName 'sessionId' }
                $cwd = $hist[$id].project
            }
            if (-not $cwd) { $cwd = $HOME }

            # Title precedence of the real picker: customTitle, aiTitle (last
            # occurrence wins - these lines are appended), then first prompt.
            $title = $null
            foreach ($prop in 'customTitle', 'aiTitle') {
                $pat = '"' + $prop + '":"((?:[^"\\]|\\.)*)"'
                $mt = [regex]::Matches($tail, $pat)
                $mh = [regex]::Matches($head, $pat)
                $raw = if ($mt.Count) { $mt[$mt.Count - 1].Groups[1].Value }
                elseif ($mh.Count) { $mh[$mh.Count - 1].Groups[1].Value }
                if ($raw) { $title = ConvertTo-CcrTitle (ConvertFrom-CcrJsonString $raw); if ($title) { break } }
            }
            if (-not $title) {
                if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path $Root.Path 'history.jsonl') -IdName 'sessionId' }
                $title = ConvertTo-CcrTitle $hist[$id].display
            }
            if (-not $title) {
                # Last resort: first real user message from the head window.
                # Covers sessions whose prompts arrived queued/piped - claude
                # generates no ai-title for those and history.jsonl never sees
                # them, so its own picker shows a bare "(session)".
                foreach ($line in ($head -split "`n")) {
                    if ($line -notlike '*"type":"user"*') { continue }
                    try {
                        $o = $line | ConvertFrom-Json
                        if ($o.type -ne 'user' -or $o.isMeta) { continue }
                        $c = $o.message.content
                        $t = if ($c -is [string]) { $c }
                        else { (@($c) | Where-Object { $_.type -eq 'text' } | Select-Object -First 1).text }
                        if (-not $t) { continue }
                        if ($t.StartsWith('<')) {
                            # Session started via a slash command: use the
                            # command itself as the title (e.g. /scan-archive).
                            $cm = [regex]::Match($t, '<command-name>([^<]+)</command-name>')
                            if ($cm.Success) { $title = ConvertTo-CcrTitle $cm.Groups[1].Value }
                        }
                        else { $title = ConvertTo-CcrTitle $t }
                        if ($title) { break }
                    }
                    catch { }
                }
            }
            if (-not $title) { $title = '(session)' }

            # --resume sort rule: min(last message timestamp, file mtime).
            # mtime alone over-reports (metadata-only appends bump it).
            $last = $file.LastWriteTimeUtc
            $tm = [regex]::Matches($tail, '"timestamp":"([^"]+)"')
            if ($tm.Count) {
                try {
                    $ts = [datetime]::Parse($tm[$tm.Count - 1].Groups[1].Value, [cultureinfo]::InvariantCulture,
                        [System.Globalization.DateTimeStyles]::AdjustToUniversal)
                    if ($ts -lt $last) { $last = $ts }
                }
                catch { }
            }

            # /clear bookkeeping. A session begun by /clear records, near its top,
            # the Remote Control bridge id of the terminal process it was cleared
            # in; the conversation it replaced ends with that same bridge id.
            $bh = [regex]::Matches($head, '"bridgeSessionId":"([^"]+)"')
            $bt = [regex]::Matches($tail, '"bridgeSessionId":"([^"]+)"')
            $headBridge = if ($bh.Count) { $bh[0].Groups[1].Value } else { $null }
            $tailBridge = if ($bt.Count) { $bt[$bt.Count - 1].Groups[1].Value }
            elseif ($bh.Count) { $bh[$bh.Count - 1].Groups[1].Value }
            else { $null }
            $startedByClear = $false
            foreach ($line in ($head -split "`n")) {
                if ($line -notlike '*"type":"user"*') { continue }
                try { $o = $line | ConvertFrom-Json } catch { continue }
                if ($o.type -ne 'user' -or $o.isMeta) { continue }
                $c = $o.message.content
                $t = if ($c -is [string]) { $c }
                else { (@($c) | Where-Object { $_.type -eq 'text' } | Select-Object -First 1).text }
                if ($t) { $startedByClear = $t.Contains('<command-name>/clear</command-name>'); break }
            }
            $startedAt = $null
            $fm = [regex]::Match($head, '"timestamp":"([^"]+)"')
            if ($fm.Success) {
                try {
                    $startedAt = [datetime]::Parse($fm.Groups[1].Value, [cultureinfo]::InvariantCulture,
                        [System.Globalization.DateTimeStyles]::AdjustToUniversal)
                }
                catch { }
            }

            [pscustomobject]@{
                Tool           = 'claude'
                SessionId      = $id
                Title          = $title
                Cwd            = $cwd
                LastActivity   = $last
                Running        = $running.ContainsKey($id)
                ProcessId      = $running[$id]
                Source         = $file.FullName
                Root           = $Root.Label
                RootPath       = $Root.Path
                StartedAt      = $startedAt
                StartedByClear = $startedByClear
                HeadBridge     = $headBridge
                TailBridge     = $tailBridge
                Cleared        = $false
            }
        }
        catch { Write-Verbose "ccr: skipping $($file.FullName): $_" }
    }
    $list = @($out)

    # Mark conversations replaced by a /clear. Exact link: the new session's
    # first bridge id equals the old one's last. Sessions from before bridge ids
    # were recorded fall back to same folder + same name (the name carries over
    # across /clear); a session with a bridge id but no match is never guessed.
    foreach ($n in @($list | Where-Object { $_.StartedByClear -and $_.StartedAt })) {
        $limit = $n.StartedAt.AddMinutes(1)
        $cands = if ($n.HeadBridge) {
            @($list | Where-Object { $_.SessionId -ne $n.SessionId -and $_.TailBridge -eq $n.HeadBridge -and $_.LastActivity -le $limit })
        }
        else {
            @($list | Where-Object { $_.SessionId -ne $n.SessionId -and $_.Title -eq $n.Title -and $_.Cwd -eq $n.Cwd -and $_.LastActivity -le $limit })
        }
        $prev = $cands | Sort-Object LastActivity -Descending | Select-Object -First 1
        if ($prev) { $prev.Cleared = $true }
    }
    $list
}

# =============================================================================
#  codex enumerator
# =============================================================================

# sessionId -> live codex process id. Codex has no pid registry; resumed
# sessions carry their uuid on the command line. Freshly started ones can't be
# mapped and simply won't show as running.
function Get-CcrCodexRunningMap {
    $map = @{}
    try {
        $procs = if ($IsWindows) {
            # CIM gives the full command line reliably on Windows.
            Get-CimInstance Win32_Process -Filter "Name='codex.exe'" -ErrorAction Stop |
                ForEach-Object { [pscustomobject]@{ Id = [int]$_.ProcessId; CommandLine = $_.CommandLine } }
        }
        else {
            Get-Process -Name codex -ErrorAction Stop |
                ForEach-Object { [pscustomobject]@{ Id = $_.Id; CommandLine = $_.CommandLine } }
        }
        foreach ($p in $procs) {
            if ($p.CommandLine -match '(?i)resume\s+([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})') {
                $map[$Matches[1]] = $p.Id
            }
        }
    }
    catch { }
    $map
}

# Best-effort curated-title overlay from codex's own catalog (state_N.sqlite,
# table threads). Codex 0.144+ does not auto-title conversations, but /rename
# fills `name`, and a few older versions filled `title` - pick those up when
# the DB is readable, fall back silently to first-prompt titles when not.
# Reads a private temp copy via winsqlite3.dll (ships with Windows 10/11).
function Initialize-CcrSqlite {
    if ('CcrSqlite' -as [type]) { return }
    if (-not [Environment]::Is64BitProcess) { throw 'ccr: 32-bit host, sqlite disabled' }
    # Windows ships winsqlite3.dll in System32; on macOS/Linux "sqlite3"
    # resolves to libsqlite3.dylib / libsqlite3.so (if absent, the overlay
    # silently degrades to first-prompt titles).
    $lib = if ($IsWindows) { 'winsqlite3.dll' } else { 'sqlite3' }
    Add-Type -TypeDefinition (@'
using System;
using System.Runtime.InteropServices;
public static class CcrSqlite {
    const string Dll = "__CCRSQLITELIB__";
    [DllImport(Dll)] public static extern int sqlite3_open_v2([MarshalAs(UnmanagedType.LPUTF8Str)] string filename, out IntPtr db, int flags, IntPtr vfs);
    [DllImport(Dll)] public static extern int sqlite3_prepare_v2(IntPtr db, [MarshalAs(UnmanagedType.LPUTF8Str)] string sql, int nByte, out IntPtr stmt, IntPtr tail);
    [DllImport(Dll)] public static extern int    sqlite3_step(IntPtr stmt);
    [DllImport(Dll)] public static extern IntPtr sqlite3_column_text(IntPtr stmt, int col);
    [DllImport(Dll)] public static extern int    sqlite3_finalize(IntPtr stmt);
    [DllImport(Dll)] public static extern int    sqlite3_close_v2(IntPtr db);
}
'@ -replace '__CCRSQLITELIB__', $lib)
}

function Get-CcrCodexTitleMap {
    param([string]$RootPath = (Get-CcrCodexRoot))
    $map = @{}
    try {
        $stateDb = Get-ChildItem -LiteralPath $RootPath -Filter 'state_*.sqlite' -File -ErrorAction Stop |
            Where-Object { $_.BaseName -match '^state_\d+$' } |
            Sort-Object { [int]($_.BaseName -replace '^state_', '') } |
            Select-Object -Last 1
        if (-not $stateDb) { return $map }
        Initialize-CcrSqlite

        # Snapshot db + wal/shm so the live catalog is never touched; the copy
        # is opened read-write so sqlite can replay a torn WAL tail.
        # -WhatIf:$false: these are internal temp snapshots, not user-visible
        # state - they must happen even when the caller runs with -WhatIf.
        $tmp = Join-Path ([IO.Path]::GetTempPath()) "ccr-$PID-$($stateDb.Name)"
        Copy-Item -LiteralPath $stateDb.FullName -Destination $tmp -Force -WhatIf:$false -Confirm:$false
        foreach ($ext in '-wal', '-shm') {
            $side = $stateDb.FullName + $ext
            if (Test-Path -LiteralPath $side) { Copy-Item -LiteralPath $side -Destination ($tmp + $ext) -Force -WhatIf:$false -Confirm:$false }
            else { Remove-Item -LiteralPath ($tmp + $ext) -Force -ErrorAction SilentlyContinue -WhatIf:$false -Confirm:$false }
        }
        try {
            $db = [IntPtr]::Zero
            if ([CcrSqlite]::sqlite3_open_v2($tmp, [ref]$db, 2, [IntPtr]::Zero) -ne 0) { return $map }
            try {
                $sql = "SELECT id, COALESCE(NULLIF(TRIM(name),''), CASE WHEN TRIM(title) <> '' AND title <> first_user_message THEN title END) FROM threads"
                $stmt = [IntPtr]::Zero
                if ([CcrSqlite]::sqlite3_prepare_v2($db, $sql, -1, [ref]$stmt, [IntPtr]::Zero) -ne 0) { return $map }
                try {
                    while ([CcrSqlite]::sqlite3_step($stmt) -eq 100) {
                        $id = [Runtime.InteropServices.Marshal]::PtrToStringUTF8([CcrSqlite]::sqlite3_column_text($stmt, 0))
                        $t = [Runtime.InteropServices.Marshal]::PtrToStringUTF8([CcrSqlite]::sqlite3_column_text($stmt, 1))
                        if ($id -and $t) { $map[$id] = $t }
                    }
                }
                finally { [void][CcrSqlite]::sqlite3_finalize($stmt) }
            }
            finally { [void][CcrSqlite]::sqlite3_close_v2($db) }
        }
        finally {
            foreach ($ext in '', '-wal', '-shm') { Remove-Item -LiteralPath ($tmp + $ext) -Force -ErrorAction SilentlyContinue -WhatIf:$false -Confirm:$false }
        }
    }
    catch { Write-Verbose "ccr: codex title overlay unavailable: $_" }

    # Historical rename names: codex <= 0.14x wrote them to session_index.jsonl
    # and current versions still honor them, but never migrated them into the
    # catalog. Catalog values win; the index fills the gaps (last entry per id).
    try {
        $idx = @{}
        foreach ($line in (Read-CcrHeadLines -Path (Join-Path $RootPath 'session_index.jsonl'))) {
            if (-not $line) { continue }
            try { $o = $line | ConvertFrom-Json } catch { continue }
            if ($o.id -and $o.thread_name) { $idx[$o.id] = $o.thread_name }
        }
        foreach ($k in $idx.Keys) { if (-not $map.ContainsKey($k)) { $map[$k] = $idx[$k] } }
    }
    catch { }
    $map
}

function Get-CcrCodexSession {
    [CmdletBinding()]
    param(
        # One entry from Get-CcrCodexRoots; omitted = the single default root.
        [object]$Root = $null
    )
    if (-not $Root) { $Root = [pscustomobject]@{ Label = ''; Path = (Get-CcrCodexRoot); Default = $true } }
    $sessRoot = Join-Path $Root.Path 'sessions'
    if (-not (Test-Path -LiteralPath $sessRoot)) { return @() }
    $hist = $null   # <root>\history.jsonl, loaded lazily only if a fallback is needed
    $running = Get-CcrCodexRunningMap
    $curated = Get-CcrCodexTitleMap -RootPath $Root.Path

    $files = Get-ChildItem -LiteralPath $sessRoot -Recurse -Filter 'rollout-*.jsonl' -File -ErrorAction SilentlyContinue
    $out = foreach ($file in $files) {
        $m = [regex]::Match($file.Name, '-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$')
        if (-not $m.Success) { continue }
        $id = $m.Groups[1].Value
        $cwd = $null
        $title = $null
        $skip = $false
        try {
            # Bounded, streaming head read - rollout files reach hundreds of MB,
            # and the title usually sits within the first ~10 lines.
            $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
            $fs = [System.IO.File]::Open($file.FullName, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $share)
            try {
                $sr = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8, $true)
                $i = 0
                while ($i -lt 60 -and $fs.Position -lt 512KB) {
                    $line = $sr.ReadLine()
                    if ($null -eq $line) { break }
                    $i++
                    if ($i -eq 1) {
                        # session_meta: cwd, canonical id, thread_source filter.
                        try {
                            $meta = ($line | ConvertFrom-Json).payload
                            if ($meta.thread_source -and $meta.thread_source -ne 'user') { $skip = $true; break }
                            if ($meta.cwd) { $cwd = $meta.cwd }
                            if ($meta.session_id) { $id = $meta.session_id } elseif ($meta.id) { $id = $meta.id }
                        }
                        catch { }
                    }
                    elseif ($line.Contains('"response_item"') -and $line.Contains('"role":"user"')) {
                        # First real user prompt = title; the first user message is an
                        # <environment_context> wrapper, so skip anything starting with '<'.
                        try {
                            $p = ($line | ConvertFrom-Json).payload
                            if ($p.type -eq 'message' -and $p.role -eq 'user') {
                                foreach ($c in @($p.content)) {
                                    if ($c.text) {
                                        if (-not $c.text.StartsWith('<')) { $title = ConvertTo-CcrTitle $c.text }
                                        break
                                    }
                                }
                                if ($title) { break }
                            }
                        }
                        catch { }
                    }
                }
                $sr.Dispose()
            }
            finally { $fs.Dispose() }
        }
        catch { Write-Verbose "ccr: skipping $($file.FullName): $_"; continue }
        if ($skip) { continue }

        if ($cwd) { $cwd = $cwd -replace '^\\\\\?\\UNC\\', '\\' -replace '^\\\\\?\\', '' }
        else { $cwd = $HOME }
        if ($curated.ContainsKey($id)) { $title = ConvertTo-CcrTitle $curated[$id] }   # /rename wins
        if (-not $title) {
            if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path $Root.Path 'history.jsonl') -IdName 'session_id' }
            $title = ConvertTo-CcrTitle $hist[$id].text
        }
        if (-not $title) { $title = '(session)' }

        [pscustomobject]@{
            Tool         = 'codex'
            SessionId    = $id
            Title        = $title
            Cwd          = $cwd
            LastActivity = $file.LastWriteTimeUtc   # resume appends to the original file
            Running      = $running.ContainsKey($id)
            ProcessId    = $running[$id]
            Source       = $file.FullName
            Root         = $Root.Label
            RootPath     = $Root.Path
        }
    }
    @($out)
}

# =============================================================================
#  deletion
# =============================================================================

# Permanently delete one conversation's on-disk data. Returns $true on success.
function Remove-CcrSessionData {
    param([Parameter(Mandatory)][object]$Session)
    if ($Session.Tool -eq 'codex') {
        # Codex keeps a catalog besides the rollout file, so let its own CLI do
        # the delete; fall back to removing the rollout directly.
        $ok = $false
        try {
            $null = & codex delete $Session.SessionId 2>&1
            $ok = ($LASTEXITCODE -eq 0)
        }
        catch { $ok = $false }
        if (-not $ok -and $Session.Source -and (Test-Path -LiteralPath $Session.Source)) {
            Remove-Item -LiteralPath $Session.Source -Force
            $ok = $true
        }
        return $ok
    }
    # Claude: the transcript plus its optional sidecar dir (custom title,
    # subagent transcripts, stored tool results).
    if (-not $Session.Source -or -not (Test-Path -LiteralPath $Session.Source)) { return $false }
    Remove-Item -LiteralPath $Session.Source -Force
    $sidecar = Join-Path (Split-Path -Parent $Session.Source) $Session.SessionId
    if (Test-Path -LiteralPath $sidecar) { Remove-Item -LiteralPath $sidecar -Recurse -Force }
    $true
}

# Re-home a conversation: move its files into another account's data dir.
# Transcripts are not account-bound and both tools resume a moved file (a
# codex home indexes it on first resume). Returns the new transcript path.
function Move-CcrSessionToRoot {
    param([Parameter(Mandatory)][object]$Session, [Parameter(Mandatory)][object]$TargetRoot)
    $src = Get-Item -LiteralPath $Session.Source
    if ($Session.Tool -eq 'claude') {
        $slug = Split-Path -Leaf $src.DirectoryName
        $dstDir = Join-Path (Join-Path $TargetRoot.Path 'projects') $slug
        New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
        Move-Item -LiteralPath $src.FullName -Destination $dstDir -Force
        $side = Join-Path $src.DirectoryName $Session.SessionId   # custom title, tool results...
        if (Test-Path -LiteralPath $side) { Move-Item -LiteralPath $side -Destination $dstDir -Force }
        return (Join-Path $dstDir $src.Name)
    }
    # codex: keep the sessions/YYYY/MM/DD layout relative to the source root.
    $srcSessions = Join-Path $Session.RootPath 'sessions'
    $rel = $src.FullName.Substring($srcSessions.Length).TrimStart('\', '/')
    $dst = Join-Path (Join-Path $TargetRoot.Path 'sessions') $rel
    New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
    Move-Item -LiteralPath $src.FullName -Destination $dst -Force
    $dst
}

# Full-screen confirmation before deleting; returns $true when the user picked
# yes. Runs inside the picker's alternate screen buffer. Shows what is cheaply
# on hand: title, folder, dates, size on disk, and the last prompt (claude) or
# last agent reply (codex) pulled from the transcript tail.
function Show-CcrDeleteConfirm {
    param([Parameter(Mandatory)][object]$Session)
    $w = [Console]::WindowWidth
    $size = ''
    $preview = $null
    try {
        if ($Session.Source -and (Test-Path -LiteralPath $Session.Source)) {
            $item = Get-Item -LiteralPath $Session.Source
            $size = if ($item.Length -ge 1MB) { '{0:n1} MB' -f ($item.Length / 1MB) }
            elseif ($item.Length -ge 1KB) { '{0:n0} KB' -f ($item.Length / 1KB) }
            else { "$($item.Length) B" }
            $tail = Read-CcrFileWindow -Path $item.FullName -Bytes 64KB -From Tail
            $pat = if ($Session.Tool -eq 'claude') { '"lastPrompt":"((?:[^"\\]|\\.)*)"' }
            else { '"last_agent_message":"((?:[^"\\]|\\.)*)"' }
            $mm = [regex]::Matches($tail, $pat)
            if ($mm.Count) { $preview = ConvertTo-CcrTitle (ConvertFrom-CcrJsonString $mm[$mm.Count - 1].Groups[1].Value) }
        }
    }
    catch { }

    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('')
    $lines.Add("  `e[31mDelete this conversation permanently?`e[39m")
    $lines.Add('')
    $lines.Add("    $($Session.Tool) $([char]0x00B7) `e[1m$($Session.Title)`e[22m")
    $lines.Add("    folder:    $($Session.Cwd)")
    $lines.Add("    last used: $($Session.LastActivity.ToLocalTime().ToString('yyyy-MM-dd HH:mm'))  ($(Format-CcrAge $Session.LastActivity))")
    if ($size) { $lines.Add("    size:      $size") }
    if ($preview) {
        $label = if ($Session.Tool -eq 'claude') { 'last prompt' } else { 'last reply ' }
        $lines.Add("    ${label}: `e[2m$preview`e[22m")
    }
    $lines.Add('')
    $lines.Add("  `e[2mremoved from disk, no undo (codex: via 'codex delete')`e[22m")
    $lines.Add('')
    $lines.Add("  `e[31m[y]`e[39m delete    `e[2many other key: cancel`e[22m")

    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append("`e[H")
    foreach ($l in $lines) {
        $t = $l
        if ($t -notmatch "`e" -and $t.Length -gt $w - 1) { $t = $t.Substring(0, $w - 2) + [char]0x2026 }
        [void]$sb.Append($t).Append("`e[K`n")
    }
    [void]$sb.Append("`e[J")
    [Console]::Write($sb.ToString())
    $k = [Console]::ReadKey($true)
    return ($k.KeyChar -eq 'y' -or $k.KeyChar -eq 'Y')
}

# Draw a whole screen of lines inside the alt buffer (long plain lines are
# cut with an ellipsis; lines carrying escape codes are left alone).
function Write-CcrScreen([string[]]$Lines) {
    $w = [Console]::WindowWidth
    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append("`e[H")
    foreach ($l in $Lines) {
        $t = $l
        if ($t -notmatch "`e" -and $t.Length -gt $w - 1) { $t = $t.Substring(0, $w - 2) + [char]0x2026 }
        [void]$sb.Append($t).Append("`e[K`n")
    }
    [void]$sb.Append("`e[J")
    [Console]::Write($sb.ToString())
}

# A small checklist inside the alt buffer: Space toggles, Enter confirms,
# Esc backs out. Items are @{ Key; Text; Note; Checked }; returns a
# hashtable Key -> [bool], or $null on Esc.
function Select-CcrChecklist {
    param([Parameter(Mandatory)][string]$Title, [Parameter(Mandatory)][object[]]$Items)
    $state = @{}
    foreach ($it in $Items) { $state[$it.Key] = [bool]$it.Checked }
    $cursor = 0
    while ($true) {
        $lines = [System.Collections.Generic.List[string]]::new()
        $lines.Add($Title)
        $lines.Add("`e[2m$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Space toggle $([char]0x00B7) Enter continue $([char]0x00B7) Esc back`e[22m")
        for ($i = 0; $i -lt $Items.Count; $i++) {
            $it = $Items[$i]
            $box = if ($state[$it.Key]) { "`e[32m[x]`e[39m" } else { '[ ]' }
            $row = "  $box $($it.Text)$(if ($it.Note) { "  `e[2m$($it.Note)`e[22m" })"
            if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
            $lines.Add($row)
        }
        Write-CcrScreen $lines
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
        switch ($k.Key) {
            'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
            'DownArrow' { if ($cursor -lt $Items.Count - 1) { $cursor++ } }
            'Spacebar' { $state[$Items[$cursor].Key] = -not $state[$Items[$cursor].Key] }
            'Enter' { return $state }
            'Escape' { return $null }
        }
    }
}

# Account page (Ctrl+M in the picker). The first time it explains what
# turning multi-account mode on does and goes straight to adding the first
# extra account (tool, then label). Afterwards it lists the accounts per
# account - with who is logged in where - and offers: + = add an account,
# Del = remove the highlighted one (its sessions go to the default
# account), X = turn multi-account mode off (every session goes to the
# default account). Runs inside the alt buffer; the work itself is done by
# the caller on the main screen. Returns @{ Action = 'add' | 'remove' |
# 'disable'; Tool; Label } or $null on Esc/Enter.
function Show-CcrAccountPage {
    param([object[]]$ClaudeRoots = @(), [object[]]$CodexRoots = @(), [hashtable]$Identity = @{})
    $dot = [char]0x00B7
    $labelHint = "1-12 letters/digits/_/- $dot Enter $dot Esc back"
    $def = $script:CcrDefaultLabel

    function Read-CcrNewAccount {
        # tool, then label -> @{ Action = 'add'; Tool; Label } or $null
        $tool = Select-CcrTool -Title 'tool for the new account' -ClaudeNote 'fresh dir + claude auth login' -CodexNote 'fresh dir + codex login'
        if (-not $tool) { return $null }
        $taken = @(@(if ($tool -eq 'claude') { $ClaudeRoots } else { $CodexRoots }) | ForEach-Object Label | Where-Object { $_ })
        while ($true) {
            $label = Read-CcrInput -Prompt "label for the new $tool account (e.g. work)> " -Hint $labelHint
            if ($null -eq $label) { return $null }
            if ($label -notmatch '^[A-Za-z0-9_-]{1,12}$') { Show-CcrNotice "ccr: invalid label '$label' ($labelHint)" '33'; continue }
            if ($label -eq $def -or $label -in $taken) { Show-CcrNotice "ccr: $tool account '$label' already exists" '33'; continue }
            $copySl = $false
            if ($tool -eq 'claude') {
                # Options for the new claude dir; only offered when the
                # default account has something to copy.
                $defRoot = @($ClaudeRoots | Where-Object Default)[0]
                $defPath = if ($defRoot) { $defRoot.Path } else { Get-CcrClaudeRoot }
                if (Test-CcrStatusline $defPath) {
                    $opts = Select-CcrChecklist -Title "options for the new claude account '$label'" -Items @(
                        [pscustomobject]@{ Key = 'statusline'; Text = 'Copy statusline from default account'; Note = 'statusLine in settings.json + statusline*.ps1'; Checked = $true })
                    if ($null -eq $opts) { return $null }
                    $copySl = [bool]$opts['statusline']
                }
            }
            return [pscustomobject]@{ Action = 'add'; Tool = $tool; Label = $label; CopyStatusline = $copySl }
        }
    }

    # One row per (account, tool), grouped by account in config order.
    $labels = @(@(@($ClaudeRoots) + @($CodexRoots) | ForEach-Object { "$($_.Label)" } | Where-Object { $_ }) | Select-Object -Unique)
    $rows = @(foreach ($lbl in $labels) {
            foreach ($t in 'claude', 'codex') {
                foreach ($r in @(@(if ($t -eq 'claude') { $ClaudeRoots } else { $CodexRoots }) | Where-Object { $_.Label -eq $lbl })) {
                    [pscustomobject]@{ Tool = $t; Label = $r.Label; Path = $r.Path; Default = $r.Default }
                }
            }
        })

    if ($rows.Count -eq 0) {
        # --- activation page ---
        Write-CcrScreen @(
            "`e[1mMulti-account mode`e[22m",
            '',
            '  You are about to turn multi-account mode on. Nothing is moved or logged out:',
            "  the dirs claude and codex use today become the '$def' account, and every",
            '  session you see now belongs to it.',
            "    claude   $(Format-CcrCwd (Get-CcrClaudeRoot) 60)",
            "    codex    $(Format-CcrCwd (Get-CcrCodexRoot) 60)",
            '',
            '  Next you choose a tool and a label for the additional account. ccr creates a',
            "  fresh dir for it (~\.claude-<label> or ~\.codex-<label>) and runs that tool's",
            '  own login there, so each account keeps its own credentials and settings.',
            '  Afterwards Ctrl+M lists the accounts, adds more, or turns the mode off again.',
            '',
            "  `e[32m[Enter]`e[39m continue    `e[2mEsc: back, nothing changes`e[22m")
        while ($true) {
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq [ConsoleKey]::Enter) { return (Read-CcrNewAccount) }
            if ($k.Key -eq [ConsoleKey]::Escape -or ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control))) { return $null }
        }
    }

    # --- list page ---
    # Who is logged in where comes from the dirs' own files (instant); the
    # slow tool-side check is only for ccr -Accounts.
    foreach ($r in $rows) {
        $ik = "$($r.Tool)|$($r.Label)"
        if (-not $Identity.ContainsKey($ik)) {
            $q = Get-CcrQuickIdentity -Tool $r.Tool -RootPath $r.Path
            $Identity[$ik] = if ($q) { $q } else { 'not logged in' }
        }
    }
    $cursor = 0
    $labelW = [Math]::Max(7, ($rows | ForEach-Object { (Format-CcrAcctLabel $_.Label $_.Default).Length } | Measure-Object -Maximum).Maximum)
    $dirW = [Math]::Min(40, ($rows | ForEach-Object { (Format-CcrCwd $_.Path 40).Length } | Measure-Object -Maximum).Maximum)
    $whoW = ($rows | ForEach-Object { "$($Identity["$($_.Tool)|$($_.Label)"])".Length } | Measure-Object -Maximum).Maximum
    while ($true) {
        $lines = [System.Collections.Generic.List[string]]::new()
        $lines.Add("`e[1mAccounts`e[22m  `e[2m$($script:CcrConfigPath)`e[22m")
        $lines.Add("`e[2m$([char]0x2191)$([char]0x2193) move $dot + add $dot Del remove $dot S copy statusline from default $dot X turn off $dot Esc back`e[22m")
        for ($i = 0; $i -lt $rows.Count; $i++) {
            $r = $rows[$i]
            $toolColor = if ($r.Tool -eq 'claude') { "`e[38;5;208m" } else { "`e[36m" }
            $who = $Identity["$($r.Tool)|$($r.Label)"]
            $row = "  `e[35m$((Format-CcrAcctLabel $r.Label $r.Default).PadRight($labelW))`e[39m  $toolColor$($r.Tool.PadRight(6))`e[39m  $((Format-CcrCwd $r.Path 40).PadRight($dirW))  `e[2m$("$who".PadRight($whoW))`e[22m"
            if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
            $lines.Add($row)
        }
        $lines.Add('')
        $lines.Add("  `e[2mA session always resumes under the account whose dir it lives in. In the picker, Space cycles the account a row opens under.`e[22m")
        Write-CcrScreen $lines
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
        switch ($k.Key) {
            'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
            'DownArrow' { if ($cursor -lt $rows.Count - 1) { $cursor++ } }
            'Enter' { return $null }
            'Escape' { return $null }
            'Insert' { $r = Read-CcrNewAccount; if ($r) { return $r } }
            'Delete' {
                $r = $rows[$cursor]
                if ($r.Default) { Show-CcrNotice "ccr: '$($r.Label)' is the default $($r.Tool) account - turn multi-account mode off (X) instead" '33'; continue }
                Write-CcrScreen @(
                    "`e[1mRemove account`e[22m",
                    '',
                    "    $($r.Tool) $dot `e[1m$($r.Label)`e[22m  $(Format-CcrCwd $r.Path 60)",
                    '',
                    "  Every $($r.Tool) session of this account moves to the '$def' account and",
                    "  keeps working there. The dir and its login stay on disk; ccr just forgets it.",
                    '  Refused while one of its sessions is running.',
                    '',
                    "  `e[31m[y]`e[39m remove    `e[2many other key: cancel`e[22m")
                $c = [Console]::ReadKey($true)
                if ($c.KeyChar -in 'y', 'Y') { return [pscustomobject]@{ Action = 'remove'; Tool = $r.Tool; Label = $r.Label } }
            }
            default {
                if ($k.KeyChar -eq '+') { $r = Read-CcrNewAccount; if ($r) { return $r } }
                elseif ($k.KeyChar -in 's', 'S') {
                    $r = $rows[$cursor]
                    if ($r.Tool -ne 'claude') { Show-CcrNotice "ccr: the status line is a claude setting - pick a claude row" '33'; continue }
                    if ($r.Default) { Show-CcrNotice "ccr: '$($r.Label)' is the default account - it is the source, not a target" '33'; continue }
                    $from = @($rows | Where-Object { $_.Tool -eq 'claude' -and $_.Default })[0]
                    if (-not $from -or -not (Test-CcrStatusline $from.Path)) { Show-CcrNotice "ccr: the default claude account has no statusLine in its settings.json" '33'; continue }
                    Write-CcrScreen @(
                        "`e[1mCopy status line`e[22m",
                        '',
                        "    from  $($from.Label)  $(Format-CcrCwd $from.Path 60)",
                        "    to    $($r.Label)  $(Format-CcrCwd $r.Path 60)",
                        '',
                        "  The statusLine entry is merged into the target's settings.json (other keys stay)",
                        '  and the statusline* script files next to it are copied over.',
                        '',
                        "  `e[32m[y]`e[39m copy    `e[2many other key: cancel`e[22m")
                    $c = [Console]::ReadKey($true)
                    if ($c.KeyChar -in 'y', 'Y') { return [pscustomobject]@{ Action = 'statusline'; Tool = 'claude'; Label = $r.Label; From = $from.Path; To = $r.Path } }
                }
                elseif ($k.KeyChar -in 'x', 'X') {
                    Write-CcrScreen @(
                        "`e[1mTurn multi-account mode off`e[22m",
                        '',
                        "  Every session of every other account moves to the '$def' account (the dirs",
                        '  claude and codex use today) and keeps working there. The other dirs and their',
                        "  logins stay on disk; ccr forgets them and goes back to a single account.",
                        '  Refused while a session of another account is running.',
                        '',
                        "  `e[31m[y]`e[39m turn off    `e[2many other key: cancel`e[22m")
                    $c = [Console]::ReadKey($true)
                    if ($c.KeyChar -in 'y', 'Y') { return [pscustomobject]@{ Action = 'disable' } }
                }
            }
        }
    }
}

# One-line notice inside the picker; waits for a key.
function Show-CcrNotice {
    param([string]$Text, [string]$Color = '31')
    [Console]::Write("`e[H`e[$($Color)m$Text`e[39m`e[K  `e[2m(any key)`e[22m`e[K")
    [void][Console]::ReadKey($true)
}

# =============================================================================
#  picker
# =============================================================================

# Minimal one-line text input inside the alt buffer. Returns the typed text
# (possibly ''), or $null on Esc.
function Read-CcrInput {
    param([string]$Prompt, [string]$Text = '', [string]$Hint = '')
    [Console]::Write("`e[?25h")
    try {
        while ($true) {
            $sb = [System.Text.StringBuilder]::new()
            [void]$sb.Append("`e[H").Append($Prompt).Append($Text).Append("`e[K`n")
            [void]$sb.Append("`e[2m").Append($Hint).Append("`e[22m`e[K`e[J")
            [void]$sb.Append("`e[1;$($Prompt.Length + $Text.Length + 1)H")
            [Console]::Write($sb.ToString())
            $k = [Console]::ReadKey($true)
            switch ($k.Key) {
                'Enter' { return $Text }
                'Escape' { return $null }
                'Backspace' { if ($Text) { $Text = $Text.Substring(0, $Text.Length - 1) } }
                default { if ($k.KeyChar -and -not [char]::IsControl($k.KeyChar)) { $Text += $k.KeyChar } }
            }
        }
    }
    finally { [Console]::Write("`e[?25l") }
}

# Account chooser for a NEW conversation when several config dirs are
# configured for that tool. Returns the chosen root's label, or $null on Esc.
function Select-CcrRoot {
    param([Parameter(Mandatory)][object[]]$Roots)
    $cursor = [Math]::Max(0, [array]::IndexOf(@($Roots.Label), @($Roots | Where-Object Default | Select-Object -First 1).Label))
    while ($true) {
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.Append("`e[H").Append('account for the new conversation').Append("`e[K`n")
        [void]$sb.Append("`e[2m$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Enter choose $([char]0x00B7) Esc back`e[22m`e[K")
        for ($i = 0; $i -lt $Roots.Count; $i++) {
            $r = $Roots[$i]
            $row = "  `e[35m$((Format-CcrAcctLabel $r.Label $r.Default).PadRight(14))`e[39m `e[2m$(Format-CcrCwd $r.Path 60)`e[22m"
            if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
            [void]$sb.Append("`n").Append($row).Append("`e[K")
        }
        [void]$sb.Append("`e[J")
        [Console]::Write($sb.ToString())
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
        switch ($k.Key) {
            'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
            'DownArrow' { if ($cursor -lt $Roots.Count - 1) { $cursor++ } }
            'Enter' { return $Roots[$cursor].Label }
            'Escape' { return $null }
        }
    }
}

# Tool chooser for a NEW conversation: claude or codex. Returns the tool
# name, or $null on Esc. Runs inside the caller's alt buffer.
function Select-CcrTool {
    param(
        [string]$Title = 'tool for the new conversation',
        [string]$ClaudeNote = 'asks for a session name',
        [string]$CodexNote = 'no start name - /rename inside'
    )
    $tools = @(
        [pscustomobject]@{ Name = 'claude'; Color = "`e[38;5;208m"; Note = $ClaudeNote },
        [pscustomobject]@{ Name = 'codex'; Color = "`e[36m"; Note = $CodexNote }
    )
    $cursor = 0
    while ($true) {
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.Append("`e[H").Append($Title).Append("`e[K`n")
        [void]$sb.Append("`e[2m$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Enter choose $([char]0x00B7) c / x jump $([char]0x00B7) Esc back`e[22m`e[K")
        for ($i = 0; $i -lt $tools.Count; $i++) {
            $t = $tools[$i]
            $row = "  $($t.Color)$($t.Name.PadRight(8))`e[39m `e[2m$($t.Note)`e[22m"
            if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
            [void]$sb.Append("`n").Append($row).Append("`e[K")
        }
        [void]$sb.Append("`e[J")
        [Console]::Write($sb.ToString())
        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
        switch ($k.Key) {
            'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
            'DownArrow' { if ($cursor -lt $tools.Count - 1) { $cursor++ } }
            'Enter' { return $tools[$cursor].Name }
            'Escape' { return $null }
            default {
                # First-letter shortcuts: c = claude, x = codex.
                if ($k.KeyChar -eq 'c') { return 'claude' }
                if ($k.KeyChar -eq 'x') { return 'codex' }
            }
        }
    }
}

# Folder chooser for a NEW conversation (Ctrl+N): every path used by any
# listed session, most recently used first, with session counts. Enter on a
# folder asks which tool (claude or codex), then - for claude - a session
# name (claude --name; empty = claude's auto title; codex has no start-name
# flag - /rename inside), then - when several accounts are configured for
# that tool - which account it belongs to. Returns @{ Path; Tool; Name; Root }
# or $null to go back.
# Runs inside the caller's alt buffer.
function Select-CcrPath {
    param(
        [Parameter(Mandatory)][object[]]$Sessions,
        [string]$InitialName = '',
        [object[]]$ClaudeRoots = @(),
        [object[]]$CodexRoots = @()
    )
    $groups = @($Sessions | Group-Object { $_.Cwd.ToLowerInvariant() } | ForEach-Object {
            $latest = ($_.Group | Sort-Object LastActivity -Descending)[0]
            [pscustomobject]@{ Path = $latest.Cwd; LastActivity = $latest.LastActivity; Count = $_.Count }
        } | Sort-Object LastActivity -Descending)
    $filter = ''
    $cursor = 0
    $top = 0
    while ($true) {
        $view = if ($filter) {
            @($groups | Where-Object { $_.Path.IndexOf($filter, [StringComparison]::OrdinalIgnoreCase) -ge 0 })
        }
        else { $groups }

        $w = [Console]::WindowWidth
        $h = [Console]::WindowHeight
        $viewH = [Math]::Max(1, $h - 2)
        if ($cursor -gt $view.Count - 1) { $cursor = [Math]::Max(0, $view.Count - 1) }
        if ($cursor -lt $top) { $top = $cursor }
        elseif ($cursor -ge $top + $viewH) { $top = $cursor - $viewH + 1 }
        if ($top -gt [Math]::Max(0, $view.Count - $viewH)) { $top = [Math]::Max(0, $view.Count - $viewH) }

        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.Append("`e[H")
        $hdr = "new session in> $filter"
        $counts = "$($view.Count)/$($groups.Count)"
        $pad = [Math]::Max(1, $w - 1 - $hdr.Length - $counts.Length - 2)
        $line1 = $hdr + (' ' * $pad) + $counts
        if ($line1.Length -gt $w - 1) { $line1 = $line1.Substring(0, $w - 1) }
        [void]$sb.Append($line1).Append("`e[K`n")
        $hint = "Enter choose folder (then tool, name) $([char]0x00B7) Esc back $([char]0x00B7) type to filter"
        if ($hint.Length -gt $w - 1) { $hint = $hint.Substring(0, $w - 1) }
        [void]$sb.Append("`e[2m").Append($hint).Append("`e[22m`e[K")

        if ($view.Count -eq 0) {
            [void]$sb.Append("`n`e[2m  (no matches)`e[22m`e[K")
        }
        else {
            $end = [Math]::Min($top + $viewH, $view.Count)
            for ($i = $top; $i -lt $end; $i++) {
                $g = $view[$i]
                $age = (Format-CcrAge $g.LastActivity).PadLeft(6)
                $cnt = "$($g.Count)".PadLeft(3)
                $pathTxt = Format-CcrCwd $g.Path ([Math]::Max(10, $w - 16))
                $row = "$age  `e[2m$cnt$([char]0x00D7)`e[22m  $pathTxt"
                if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
                [void]$sb.Append("`n").Append($row).Append("`e[K")
            }
        }
        [void]$sb.Append("`e[J")
        [Console]::Write($sb.ToString())

        $k = [Console]::ReadKey($true)
        if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
        switch ($k.Key) {
            'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
            'DownArrow' { if ($cursor -lt $view.Count - 1) { $cursor++ } }
            'PageUp' { $cursor = [Math]::Max(0, $cursor - $viewH) }
            'PageDown' { $cursor = [Math]::Min([Math]::Max($view.Count - 1, 0), $cursor + $viewH) }
            'Home' { $cursor = 0 }
            'End' { $cursor = [Math]::Max($view.Count - 1, 0) }
            'Enter' {
                if ($view.Count -gt 0) {
                    # Folder -> tool -> (claude only) name -> (multi-account
                    # only) account. Esc at any step comes back here and
                    # repaints the folder list.
                    $tool = Select-CcrTool
                    if ($tool -eq 'codex') {
                        $rootLabel = if ($CodexRoots.Count -gt 1) { Select-CcrRoot -Roots $CodexRoots }
                        elseif ($CodexRoots.Count -eq 1) { $CodexRoots[0].Label } else { $null }
                        if ($CodexRoots.Count -le 1 -or $null -ne $rootLabel) {
                            return [pscustomobject]@{ Path = $view[$cursor].Path; Tool = 'codex'; Name = ''; Root = $rootLabel }
                        }
                    }
                    if ($tool -eq 'claude') {
                        $name = Read-CcrInput -Prompt 'name> ' -Text $InitialName `
                            -Hint "session name for claude $([char]0x00B7) Enter confirm (empty = auto title) $([char]0x00B7) Esc back"
                        if ($null -ne $name) {
                            $rootLabel = if ($ClaudeRoots.Count -gt 1) { Select-CcrRoot -Roots $ClaudeRoots }
                            elseif ($ClaudeRoots.Count -eq 1) { $ClaudeRoots[0].Label } else { $null }
                            if ($ClaudeRoots.Count -le 1 -or $null -ne $rootLabel) {
                                return [pscustomobject]@{ Path = $view[$cursor].Path; Tool = 'claude'; Name = $name.Trim(); Root = $rootLabel }
                            }
                        }
                    }
                }
            }
            'Escape' {
                if ($filter) { $filter = ''; $cursor = 0; $top = 0 }
                else { return $null }
            }
            'Backspace' {
                if ($filter) { $filter = $filter.Substring(0, $filter.Length - 1); $cursor = 0; $top = 0 }
            }
            default {
                if ($k.KeyChar -and -not [char]::IsControl($k.KeyChar)) {
                    $filter += $k.KeyChar
                    $cursor = 0; $top = 0
                }
            }
        }
    }
}

# Interactive multi-select over the merged session list. Returns the chosen
# sessions, or @() on cancel. Runs in the alternate screen buffer.
function Select-CcrSession {
    param(
        [Parameter(Mandatory)][object[]]$Sessions,
        [string]$InitialFilter = '',
        # Set when no backend can open extra terminal surfaces here (not
        # Windows Terminal, not inside tmux): marking a second session then
        # shows a live warning that only the first will open.
        [switch]$NoMultiOpen,
        # Config dirs (accounts) in play per tool; more than one for either
        # tool adds an account column and an account step to Ctrl+N.
        [object[]]$ClaudeRoots = @(),
        [object[]]$CodexRoots = @()
    )
    $multiRoot = ($ClaudeRoots.Count -gt 1) -or ($CodexRoots.Count -gt 1)
    $defLabel = @{ claude = "$(@($ClaudeRoots | Where-Object Default)[0].Label)"; codex = "$(@($CodexRoots | Where-Object Default)[0].Label)" }
    $rootW = if ($multiRoot) {
        [Math]::Min(14, (@($ClaudeRoots) + @($CodexRoots) | ForEach-Object { (Format-CcrAcctLabel $_.Label $_.Default).Length } | Measure-Object -Maximum).Maximum)
    }
    else { 0 }

    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        Write-Error 'ccr: needs an interactive console.'
        return $null
    }

    $sel = [System.Collections.Generic.HashSet[string]]::new()
    # Account mode = multi-account is on (several dirs for a tool): Space
    # cycles the account the row will be opened under - its own first (a
    # plain open, green dot), then the others (magenta digit = re-homed
    # first), then none. Accounts are the union of labels across both tools,
    # numbered in config order. Persistent: it is the config, not a toggle.
    $acct = @{}
    $acctMode = $multiRoot
    $accounts = @(@(@($ClaudeRoots) + @($CodexRoots) | ForEach-Object { "$($_.Label)" } | Where-Object { $_ }) | Select-Object -Unique)
    $acctIdent = @{}   # "tool|label" -> who is logged in there; filled by the account page
    $acctQuick = @{}   # "tool|label" -> email from the dir's own files; filled by the legend
    function Get-CcrAcctAvail([object]$row) {
        $roots = if ($row.Tool -eq 'codex') { $CodexRoots } else { $ClaudeRoots }
        @($accounts | Where-Object { $lbl = $_; @($roots | Where-Object { $_.Label -eq $lbl }).Count -gt 0 })
    }
    $filter = $InitialFilter
    $cursor = 0
    $top = 0
    $prevCtrlC = [Console]::TreatControlCAsInput
    [Console]::TreatControlCAsInput = $true
    # Legacy codepages (e.g. 850/1252 on Italian Windows) have no glyph for the
    # marks/arrows and render "?" - write UTF-8 for the picker's lifetime.
    $prevEnc = [Console]::OutputEncoding
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    [Console]::Write("`e[?1049h`e[?25l")
    try {
        while ($true) {
            # --- refilter (recomputed every pass; cheap at <= 500 rows) ---
            $view = if ($filter) {
                @($Sessions | Where-Object {
                        ("$($_.Tool) $($_.Root) $($_.Title) $($_.Cwd)$(if ($_.Cleared) { ' cleared' })").IndexOf($filter, [StringComparison]::OrdinalIgnoreCase) -ge 0
                    })
            }
            else { $Sessions }

            # --- layout ---
            $w = [Console]::WindowWidth
            $h = [Console]::WindowHeight
            $viewH = [Math]::Max(1, $h - 2 - $(if ($acctMode) { $accounts.Count + 1 } else { 0 }))
            if ($cursor -gt $view.Count - 1) { $cursor = [Math]::Max(0, $view.Count - 1) }
            if ($cursor -lt $top) { $top = $cursor }
            elseif ($cursor -ge $top + $viewH) { $top = $cursor - $viewH + 1 }
            if ($top -gt [Math]::Max(0, $view.Count - $viewH)) { $top = [Math]::Max(0, $view.Count - $viewH) }

            # row = status(1) sp tool(6) sp [account(rootW) sp] age(6) 2sp title 2sp cwd
            $cwdW = [Math]::Min(35, [Math]::Max(12, [int]($w * 0.35)))
            $titleW = $w - 21 - $cwdW - $(if ($multiRoot) { $rootW + 1 } else { 0 })
            if ($titleW -lt 10) { $cwdW = [Math]::Max(8, $cwdW + $titleW - 10); $titleW = [Math]::Max(1, $w - 23 - $cwdW) }

            # --- render one full frame ---
            $sb = [System.Text.StringBuilder]::new()
            [void]$sb.Append("`e[H")
            $counts = "$($view.Count)/$($Sessions.Count)"
            $nMove = 0; $nSame = 0
            foreach ($s in $Sessions) { $k2 = "$($s.Tool)|$($s.SessionId)"; if ($acct.ContainsKey($k2)) { if ($acct[$k2] -eq "$($s.Root)") { $nSame++ } else { $nMove++ } } }
            if ($sel.Count + $nSame) { $counts += " $([char]0x00B7) $($sel.Count + $nSame) marked" }
            if ($nMove) { $counts += " $([char]0x00B7) $nMove re-homed" }
            if ($NoMultiOpen -and ($sel.Count + $acct.Count) -gt 1) { $counts += " ! only the first will open (no tmux)" }
            $hdr = "filter> $filter"
            $pad = $w - 1 - $hdr.Length - $counts.Length - 2
            if ($pad -lt 1) { $pad = 1 }
            $line1 = $hdr + (' ' * $pad) + $counts
            if ($line1.Length -gt $w - 1) { $line1 = $line1.Substring(0, $w - 1) }
            [void]$sb.Append($line1).Append("`e[K`n")
            if ($acctMode) {
                # Legend, one line per account: number, label, and per tool
                # the dir and the email logged in there (from the tools' own
                # files, see Get-CcrQuickIdentity). Columns are aligned across
                # accounts; dirs are dropped when the lines would not fit.
                [void]$sb.Append("`e[1;35mMulti-account mode active.`e[22;39m`e[K`n")
                $isDef = @{}
                foreach ($lbl in $accounts) { $isDef[$lbl] = [bool](@(@($ClaudeRoots) + @($CodexRoots) | Where-Object { $_.Label -eq $lbl -and $_.Default }).Count) }
                $lblW = ($accounts | ForEach-Object { (Format-CcrAcctLabel $_ $isDef[$_]).Length } | Measure-Object -Maximum).Maximum
                $cells = @{}   # "tool|label" -> @{ Dir; Who }
                $colW = @{}    # tool -> @{ Dir; Who } max widths
                foreach ($t in 'claude', 'codex') {
                    $colW[$t] = @{ Dir = 0; Who = 0 }
                    foreach ($lbl in $accounts) {
                        $r = @(@(if ($t -eq 'claude') { $ClaudeRoots } else { $CodexRoots }) | Where-Object { $_.Label -eq $lbl })
                        if ($r.Count -eq 0) { continue }
                        $qk = "$t|$lbl"
                        if (-not $acctQuick.ContainsKey($qk)) { $acctQuick[$qk] = Get-CcrQuickIdentity -Tool $t -RootPath $r[0].Path }
                        $cell = @{ Dir = (Format-CcrCwd $r[0].Path 28); Who = $(if ($acctQuick[$qk]) { $acctQuick[$qk] } else { 'not logged in' }) }
                        $cells[$qk] = $cell
                        if ($cell.Dir.Length -gt $colW[$t].Dir) { $colW[$t].Dir = $cell.Dir.Length }
                        if ($cell.Who.Length -gt $colW[$t].Who) { $colW[$t].Who = $cell.Who.Length }
                    }
                }
                $fullLen = 4 + $lblW + 2 + (('claude', 'codex' | ForEach-Object { if ($colW[$_].Who) { $_.Length + 1 + $colW[$_].Dir + 1 + $colW[$_].Who + 2 } else { 0 } } | Measure-Object -Sum).Sum)
                $withDirs = $fullLen -le $w - 1
                for ($ai = 0; $ai -lt $accounts.Count; $ai++) {
                    $lbl = $accounts[$ai]
                    $parts = foreach ($t in 'claude', 'codex') {
                        if (-not $colW[$t].Who) { continue }
                        $tc = if ($t -eq 'claude') { "`e[38;5;208m" } else { "`e[36m" }
                        $cell = $cells["$t|$lbl"]
                        $dir = if ($cell) { $cell.Dir } else { '' }
                        $who = if ($cell) { $cell.Who } else { '' }
                        if ($withDirs) { "$tc$t`e[39m $($dir.PadRight($colW[$t].Dir)) `e[2m$($who.PadRight($colW[$t].Who))`e[22m" }
                        else { "$tc$t`e[39m `e[2m$($who.PadRight($colW[$t].Who))`e[22m" }
                    }
                    $line = "  `e[1;35m$($ai + 1)`e[22m $((Format-CcrAcctLabel $lbl $isDef[$lbl]).PadRight($lblW))`e[39m  $($parts -join '  ')"
                    [void]$sb.Append($line).Append("`e[K`n")
                }
                $hint = "$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Space cycles the account (dot = as is) $([char]0x00B7) Enter open $([char]0x00B7) Ctrl+N new $([char]0x00B7) Ctrl+M accounts $([char]0x00B7) Del delete $([char]0x00B7) Esc cancel $([char]0x00B7) v$script:CcrVersion"
                if ($hint.Length -gt $w - 1) { $hint = $hint.Substring(0, $w - 1) }
                [void]$sb.Append("`e[2m").Append($hint).Append("`e[22m`e[K")
            }
            else {
                $hint = "$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Space mark $([char]0x00B7) Enter open $([char]0x00B7) Ctrl+N new $([char]0x00B7) Del delete $([char]0x00B7) Ctrl+M accounts $([char]0x00B7) Esc cancel $([char]0x00B7) type to filter $([char]0x00B7) v$script:CcrVersion"
                if ($hint.Length -gt $w - 1) { $hint = $hint.Substring(0, $w - 1) }
                [void]$sb.Append("`e[2m").Append($hint).Append("`e[22m`e[K")
            }

            if ($view.Count -eq 0) {
                [void]$sb.Append("`n`e[2m  (no matches)`e[22m`e[K")
            }
            else {
                $end = [Math]::Min($top + $viewH, $view.Count)
                for ($i = $top; $i -lt $end; $i++) {
                    $s = $view[$i]
                    $key = "$($s.Tool)|$($s.SessionId)"
                    # Green dot = marked for opening. Running sessions show a
                    # red "run" in the age column (informational only).
                    # Green dot = open as is; magenta digit = open under account N
                    # (moving the conversation there first).
                    $mark = if ($acct.ContainsKey($key) -and $acct[$key] -ne "$($s.Root)") { "`e[1;35m$([array]::IndexOf($accounts, $acct[$key]) + 1)`e[22;39m" }
                    elseif ($acct.ContainsKey($key) -or $sel.Contains($key)) { "`e[32m$([char]0x25CF)`e[39m" }
                    else { ' ' }
                    $toolColor = if ($s.Tool -eq 'claude') { "`e[38;5;208m" } else { "`e[36m" }
                    $age = if ($s.Running) { "`e[31m" + 'run'.PadLeft(6) + "`e[39m" } else { (Format-CcrAge $s.LastActivity).PadLeft(6) }
                    # "(cleared)" in yellow after the title when a /clear replaced
                    # this conversation; the title is shortened to make room.
                    $suffix = if ($s.Cleared -and $titleW -ge 20) { ' (cleared)' } else { '' }
                    $titleTxt = $s.Title
                    $room = $titleW - $suffix.Length
                    if ($titleTxt.Length -gt $room) { $titleTxt = $titleTxt.Substring(0, [Math]::Max(0, $room - 1)) + [char]0x2026 }
                    $titleTxt = if ($suffix) {
                        "$titleTxt `e[33m(cleared)`e[39m" + (' ' * [Math]::Max(0, $titleW - $titleTxt.Length - $suffix.Length))
                    }
                    else { $titleTxt.PadRight($titleW) }
                    $cwdTxt = (Format-CcrCwd $s.Cwd $cwdW).PadRight($cwdW)
                    # Account column (multi-root only): the config dir this claude
                    # session lives in; codex rows have none.
                    $rootTxt = if ($multiRoot) {
                        $lbl = Format-CcrAcctLabel "$($s.Root)" ("$($s.Root)" -eq $defLabel[$s.Tool]); if ($lbl.Length -gt $rootW) { $lbl = $lbl.Substring(0, $rootW) }
                        "`e[35m $($lbl.PadRight($rootW))`e[39m"
                    }
                    else { '' }
                    $row = "$mark $toolColor$($s.Tool.PadRight(6))`e[39m$rootTxt$age  $titleTxt  `e[2m$cwdTxt`e[22m"
                    if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
                    [void]$sb.Append("`n").Append($row).Append("`e[K")
                }
            }
            [void]$sb.Append("`e[J")
            [Console]::Write($sb.ToString())

            # --- input ---
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
            $ctrl = [bool]($k.Modifiers -band [ConsoleModifiers]::Control)
            # Ctrl+M: the account page. Some terminals deliver Ctrl+M as Enter
            # with Control set (it is the CR byte); Ctrl+A is an alias that
            # survives every terminal.
            if ($ctrl -and ($k.Key -in [ConsoleKey]::M, [ConsoleKey]::A, [ConsoleKey]::Enter)) {
                $act = Show-CcrAccountPage -ClaudeRoots $ClaudeRoots -CodexRoots $CodexRoots -Identity $acctIdent
                if ($null -eq $act) { continue }
                # add / remove / disable: leave the alt buffer (login flows and
                # progress draw on the main screen), do it, then restart the
                # picker so the listing reflects the new ccr.json.
                [Console]::Write("`e[?25h`e[?1049l")
                [Console]::TreatControlCAsInput = $prevCtrlC
                try {
                    if ($WhatIfPreference) {
                        Write-Host "WhatIf: would $($act.Action) $(if ($act.Label) { "$($act.Tool) account '$($act.Label)'" } else { 'multi-account mode' })$(if ($act.CopyStatusline) { ' (copying the status line)' })." -ForegroundColor Yellow
                    }
                    else {
                        switch ($act.Action) {
                            'add' { Add-CcrAccount -Label $act.Label -Tool $act.Tool -CopyStatusline:([bool]$act.CopyStatusline) }
                            'statusline' { Copy-CcrStatusline -FromPath $act.From -ToPath $act.To }
                            'remove' { Remove-CcrAccount -Label $act.Label -Tool $act.Tool }
                            'disable' { Disable-CcrMultiAccount }
                        }
                    }
                }
                catch { Write-Warning "ccr: $($act.Action) failed: $_" }
                Write-Host ''
                Write-Host 'ccr: press any key to go back to the picker' -ForegroundColor DarkGray
                [void][Console]::ReadKey($true)
                return [pscustomobject]@{ Restart = $true; Filter = $filter }
            }
            if ($k.Key -eq [ConsoleKey]::N -and $ctrl) {
                # Ctrl+N: pick a folder (and tool, and account) for a brand-new conversation.
                $newPick = Select-CcrPath -Sessions $Sessions -ClaudeRoots $ClaudeRoots -CodexRoots $CodexRoots
                if ($newPick) { return [pscustomobject]@{ NewSessionPath = $newPick.Path; NewSessionTool = $newPick.Tool; NewSessionName = $newPick.Name; NewSessionRoot = $newPick.Root } }
                continue
            }
            switch ($k.Key) {
                'UpArrow' { if ($cursor -gt 0) { $cursor-- } }
                'DownArrow' { if ($cursor -lt $view.Count - 1) { $cursor++ } }
                'PageUp' { $cursor = [Math]::Max(0, $cursor - $viewH) }
                'PageDown' { $cursor = [Math]::Min([Math]::Max($view.Count - 1, 0), $cursor + $viewH) }
                'Home' { $cursor = 0 }
                'End' { $cursor = [Math]::Max($view.Count - 1, 0) }
                'Spacebar' {
                    if ($view.Count -gt 0) {
                        $row = $view[$cursor]
                        $key = "$($row.Tool)|$($row.SessionId)"
                        if ($acctMode) {
                            # none -> own account -> the others -> none, over the
                            # accounts that have a dir for this row's tool.
                            $own = "$($row.Root)"
                            $cycle = @(@($own) + @(Get-CcrAcctAvail $row | Where-Object { $_ -ne $own }) | Where-Object { $_ })
                            if ($cycle.Count -eq 0) { Show-CcrNotice "ccr: no account has a $($row.Tool) dir configured" '33' }
                            else {
                                $cur = if ($acct.ContainsKey($key)) { [array]::IndexOf($cycle, $acct[$key]) } else { -1 }
                                if ($cur + 1 -ge $cycle.Count) { $acct.Remove($key) } else { $acct[$key] = $cycle[$cur + 1] }
                                [void]$sel.Remove($key)
                            }
                        }
                        else {
                            if (-not $sel.Add($key)) { [void]$sel.Remove($key) }
                            $acct.Remove($key)
                        }
                    }
                }
                'Enter' {
                    # Marked rows carry TargetRoot: an account label to open under
                    # (re-homing first), or $null for "as is".
                    $marked = @()
                    foreach ($s in $Sessions) {
                        $key = "$($s.Tool)|$($s.SessionId)"
                        if ($acct.ContainsKey($key)) { $s | Add-Member -NotePropertyName TargetRoot -NotePropertyValue $acct[$key] -Force; $marked += $s }
                        elseif ($sel.Contains($key)) { $s | Add-Member -NotePropertyName TargetRoot -NotePropertyValue $null -Force; $marked += $s }
                    }
                    if ($marked.Count -gt 0) { return $marked }
                    if ($view.Count -gt 0) { return @($view[$cursor]) }   # bare Enter: cursor row
                }
                'Delete' {
                    if ($view.Count -gt 0) {
                        $victim = $view[$cursor]
                        if ($victim.Running) {
                            Show-CcrNotice "ccr: '$($victim.Title)' is running right now - close that tab first."
                        }
                        elseif (Show-CcrDeleteConfirm -Session $victim) {
                            if ($WhatIfPreference) {
                                Show-CcrNotice "WhatIf: would delete '$($victim.Title)'." '33'
                            }
                            else {
                                $err = $null
                                try { $ok = Remove-CcrSessionData -Session $victim }
                                catch { $ok = $false; $err = $_ }
                                if ($ok) {
                                    $key = "$($victim.Tool)|$($victim.SessionId)"
                                    [void]$sel.Remove($key)
                                    $Sessions = @($Sessions | Where-Object { "$($_.Tool)|$($_.SessionId)" -ne $key })
                                }
                                else {
                                    Show-CcrNotice "ccr: delete failed$(if ($err) { ": $err" })."
                                }
                            }
                        }
                    }
                }
                'Escape' {
                    if ($filter) { $filter = ''; $cursor = 0; $top = 0 }
                    else { return $null }
                }
                'Backspace' {
                    if ($filter) { $filter = $filter.Substring(0, $filter.Length - 1); $cursor = 0; $top = 0 }
                }
                default {
                    if ($k.KeyChar -and -not [char]::IsControl($k.KeyChar)) {
                        $filter += $k.KeyChar
                        $cursor = 0; $top = 0
                    }
                }
            }
        }
    }
    finally {
        [Console]::Write("`e[?25h`e[?1049l")
        [Console]::OutputEncoding = $prevEnc
        [Console]::TreatControlCAsInput = $prevCtrlC
    }
}

# =============================================================================
#  release channels: ccr (stable) and ccrtest (test), side by side
# =============================================================================

# Channels are branches of the public repo, served raw by GitHub. Two copies
# live next to each other: Resume-CcSessions.ps1 (stable = main, loaded by
# the profile) and Resume-CcSessions.test.ps1 (test branch), and the channel
# of a copy is its file name. `ccr -Update` refreshes the stable copy from
# main, `ccrtest -Update` the test copy from the test branch, and
# `ccr -Channel test` installs/refreshes the test copy in the first place.
$script:CcrChannels = [ordered]@{ stable = 'main'; test = 'test' }

function Get-CcrChannelOf([string]$Path) {
    if ($Path -match '\.test\.ps1$') { 'test' } else { 'stable' }
}
function Get-CcrChannelPath([string]$Channel, [string]$AnyCopyPath) {
    $dir = Split-Path -Parent $AnyCopyPath
    if ($Channel -eq 'test') { Join-Path $dir 'Resume-CcSessions.test.ps1' } else { Join-Path $dir 'Resume-CcSessions.ps1' }
}

function Update-CcrSelf {
    param(
        [Parameter(Mandatory)][string]$Channel,
        [Parameter(Mandatory)][string]$TargetPath,
        [switch]$WhatIf
    )
    $branch = $script:CcrChannels[$Channel]
    if (-not $branch) { throw "ccr: unknown channel '$Channel' (known: $($script:CcrChannels.Keys -join ', '))" }
    # GitHub's raw CDN serves a branch URL from cache for minutes after a
    # push (and ignores query strings), so resolve the branch head through
    # the API - never cached - and fetch the file by commit, which is
    # immutable. Falls back to the branch URL when the API is unreachable.
    $sha = $null
    try {
        $sha = (Invoke-RestMethod -Uri "https://api.github.com/repos/Cepstral/claude-codex-resume/commits/$branch" -Headers @{ 'User-Agent' = 'ccr' } -TimeoutSec 15).sha
    }
    catch { }
    $ref = if ($sha) { $sha } else { $branch }
    $url = "https://raw.githubusercontent.com/Cepstral/claude-codex-resume/$ref/Resume-CcSessions.ps1"
    $at = if ($sha) { "branch $branch @ $($sha.Substring(0, 7))" } else { "branch $branch (head unknown, raw URL may lag)" }
    if ($WhatIf) { "would download $at -> $TargetPath"; return }
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "ccr-update-$PID.ps1"
    try {
        Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($tmp, [ref]$null, [ref]$errs)
        if ($errs) { throw "the downloaded file does not parse ($($errs[0].Message)) - nothing replaced" }
        $m = [regex]::Match(((Get-Content -LiteralPath $tmp -TotalCount 30) -join "`n"), "CcrVersion = '([^']+)'")
        $newVer = if ($m.Success) { $m.Groups[1].Value } else { '?' }
        $had = if (Test-Path -LiteralPath $TargetPath) {
            $mm = [regex]::Match(((Get-Content -LiteralPath $TargetPath -TotalCount 30) -join "`n"), "CcrVersion = '([^']+)'")
            if ($mm.Success) { $mm.Groups[1].Value } else { '?' }
        } else { 'none' }
        Copy-Item -LiteralPath $tmp -Destination $TargetPath -Force
        if ($IsWindows) { Unblock-File -LiteralPath $TargetPath -ErrorAction SilentlyContinue }
        $cmd = if ($Channel -eq 'test') { 'ccrtest' } else { 'ccr' }
        Write-Host "ccr: channel '$Channel' ($at) v$had -> v$newVer at $TargetPath. Run: $cmd" -ForegroundColor Green
        if ($newVer -eq $had) { Write-Host "ccr: same version as before - nothing newer on that branch." }
    }
    finally { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
}

# The test channel, side by side with the stable one: its functions are
# loaded in a child scope for this one call and never replace the stable
# ones. Every ccr parameter passes through (ccrtest -Update, -WhatIf, -n ...).
function ccrtest {
    $stable = $MyInvocation.MyCommand.ScriptBlock.File
    if (-not $stable) { $stable = Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1' }
    $test = Get-CcrChannelPath 'test' $stable
    if (-not (Test-Path -LiteralPath $test)) {
        Write-Host "ccr: no test copy yet - install it with: ccr -Channel test" -ForegroundColor Yellow
        return
    }
    & { param($testFile, $passArgs) . $testFile; Resume-CcSessions @passArgs } $test $args
}

# =============================================================================
#  public entry point
# =============================================================================

function Resume-CcSessions {
    <#
    .SYNOPSIS
        Multi-select picker to reopen past Claude Code and Codex conversations
        as tabs of the current Windows Terminal window.
    .DESCRIPTION
        Lists past claude + codex sessions merged and sorted by last message
        date (matching each tool's own --resume/resume ordering), lets you mark
        several with Space, and resumes each selection ("claude --resume <id>"
        / "codex resume <id>" in the session's recorded folder; no model or
        effort overrides - a resumed session keeps its own settings).
        The FIRST selection takes over the tab ccr runs in, so the launcher
        tab never sits idle; the remaining ones open as Windows Terminal tabs.
        With a single selection ccr simply resumes it right here. -NewWindow
        instead sends every selection to a fresh window and keeps this tab.

        Picker keys: Up/Down/PgUp/PgDn/Home/End move, Space marks/unmarks,
        Enter opens the marked set (or the highlighted row if nothing is
        marked), Del PERMANENTLY deletes the highlighted conversation after a
        full-screen confirmation (title, folder, dates, size, last prompt or
        reply; codex deletions go through 'codex delete' so its catalog stays
        consistent; running sessions are refused), Esc clears the filter then
        cancels, typing filters (spaces can't be typed into the filter -
        Space marks).

        A green dot = marked. Sessions running right now in some terminal show
        a red "run" in the age column (codex ones only when started via "codex
        resume <id>" - fresh ones expose no session id to match). Titles:
        claude custom/AI titles; codex has no auto-titles - /rename a thread
        in codex to name it (ccr reads the catalog and the legacy
        session_index for those names). A claude conversation that a /clear
        replaced shows a yellow "(cleared)" after its title - /clear starts
        a new session under the same name - and "cleared" works as a
        filter word to list them all.
    .EXAMPLE
        ccr
        Pick from the 200 most recent sessions of both tools (-Top 0 for all).
    .EXAMPLE
        ccr Kit
        Open the picker already filtered to "Kit" (Backspace/Esc clear it).
    .EXAMPLE
        ccr -n
        Start a NEW conversation: pick from every folder past sessions used,
        most recently used first. Enter asks for a session name and starts
        claude there in this tab (empty name = claude's auto title); Tab
        Enter on a folder asks which tool (claude or codex; c / x jump), then
        for claude a session name. Same menu as Ctrl+N inside the picker.
    .EXAMPLE
        ccr -n Kitchen renovation
        Same, with the name box prefilled to "Kitchen renovation".
    .EXAMPLE
        ccr -Tool codex -Top 15
        Only codex sessions, 15 most recent.
    .EXAMPLE
        ccr -NewWindow
        Open the selected tabs in a fresh Windows Terminal window.
    .EXAMPLE
        ccr -WhatIf
        Run the picker, then print the wt.exe command line instead of launching.
    .EXAMPLE
        ccr -AddAccount work
        Second account (multi-account mode): creates a config dir per tool
        next to the tool's default dir (OneDrive/.claude-work next to
        OneDrive/.claude, ~/.codex-work next to ~/.codex; an existing dir
        with a login inside is reused as is), runs each tool's own login
        flow inside it, and records both in ccr.json next to this script.
        The first time the dirs in use today become the "default" account
        (nothing moves). -Tool claude / -Tool codex limits it to one tool;
        -CopyStatusline gives the new claude dir the default account's
        status line (statusLine in settings.json + the statusline* files).
        ccr -Accounts shows who is logged in where; ccr -RemoveAccount work
        moves its sessions to the default account and forgets the entry;
        ccr -DisableAccounts does that for every account and turns
        multi-account mode off. Dirs and logins are never deleted.
    .EXAMPLE
        ccr   then Ctrl+M
        The account page. The first time it explains that the dirs in use
        today become the "default" account and asks tool + label for the
        additional one, then runs that tool's login (like ccr -AddAccount).
        Afterwards it lists the accounts (grouped by account) with who is
        logged in where: + adds one (for claude, with a checkbox to copy
        the default account's status line), Del removes the highlighted
        one (its sessions go to the default account), S copies the status
        line from the default account to the highlighted claude account,
        X turns multi-account mode off (every session goes to the default
        account).
        While multi-account mode is on, the picker's Space cycles the
        account a row opens under: its own (green dot = plain open), then
        the others (magenta digit, see the hint line), then none. Enter
        opens each row under the chosen account, moving the conversation
        into that account's dir first when it differs (both claude and
        codex; running sessions are refused). Ctrl+A is an alias for
        terminals that deliver Ctrl+M as Enter.
    .EXAMPLE
        ccr -Root work
        With several accounts configured, list only the "work" account's
        sessions. Without -Root every account is listed, with an account
        column; a session always resumes under the account it belongs to
        (CLAUDE_CONFIG_DIR / CODEX_HOME set per launched process), and
        Ctrl+N asks which account a new conversation goes to.
    .EXAMPLE
        ccr -Update
        Refresh this (stable) copy from the main branch on GitHub. Open tabs
        pick the new version up by themselves on their next ccr.
    .EXAMPLE
        ccr -Channel test   then   ccrtest
        Install the test channel (the repo's test branch) as a side-by-side
        copy, Resume-CcSessions.test.ps1 next to this file, and run it as
        ccrtest - same parameters, same keys, never replacing the stable ccr.
        ccrtest -Update refreshes it from its branch.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [Alias('ccr')]
    param(
        # Words after the command pre-seed the picker's filter: `ccr Kit`
        # opens filtered to "Kit" (Backspace/Esc clear it as usual).
        [Parameter(Position = 0, ValueFromRemainingArguments)][string[]]$Filter,
        [ValidateSet('claude', 'codex', 'all')][string]$Tool = 'all',
        # How many recent sessions the picker gets; 0 = no limit, show all.
        [ValidateRange(0, [int]::MaxValue)][int]$Top = 200,
        # Jump straight to the new-conversation folder menu (same as Ctrl+N
        # inside the picker). The exact alias 'n' avoids the -New/-NewWindow
        # prefix ambiguity: `ccr -n` works.
        [Alias('n')][switch]$New,
        [switch]$NewWindow,
        # Release channels: -Update refreshes THIS copy from its own channel
        # (stable = main for ccr, test branch for ccrtest); -Channel test
        # installs/refreshes the side-by-side test copy (run it as ccrtest),
        # -Channel stable the stable one.
        [ValidateSet('stable', 'test')][string]$Channel = '',
        [switch]$Update,
        # Multi-account: restrict the list to one configured account (label
        # from ccr.json). Also the account Ctrl+N/-n defaults to.
        [string]$Root = '',
        # Multi-account management: -Accounts lists the configured accounts
        # and their login state; -AddAccount <label> creates the config dirs
        # for a new account (per -Tool), runs the tools' own login flows in
        # them, and records them in ccr.json; -RemoveAccount <label> only
        # forgets the entry (no data is deleted).
        [switch]$Accounts,
        [string]$AddAccount = '',
        [string]$RemoveAccount = '',
        [switch]$DisableAccounts,
        # With -AddAccount: give the new claude dir the default account's status line.
        [switch]$CopyStatusline
    )
    $filterText = if ($Filter) { ($Filter -join ' ').Trim() } else { '' }

    # --- release channels (before the self-heal, on purpose) -----------------
    if ($Update -or $Channel) {
        $selfPath = $MyInvocation.MyCommand.ScriptBlock.File
        if (-not $selfPath) { $selfPath = Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1' }
        $ch = if ($Channel) { $Channel } else { Get-CcrChannelOf $selfPath }
        Update-CcrSelf -Channel $ch -TargetPath (Get-CcrChannelPath $ch $selfPath) -WhatIf:$WhatIfPreference
        return
    }

    # --- stale-shell self-heal ---------------------------------------------
    # A shell loads this file once, at startup; after an update every already
    # open tab (including tabs ccr itself opened) keeps the old function. If
    # the file on disk carries a different version, reload it and delegate
    # this invocation to the fresh code. The path comes from the function's
    # own definition, so it follows wherever the profile loads the file from.
    $selfPath = $MyInvocation.MyCommand.ScriptBlock.File
    if (-not $selfPath) { $selfPath = Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1' }
    try {
        $m = [regex]::Match(((Get-Content -LiteralPath $selfPath -TotalCount 30 -ErrorAction Stop) -join "`n"),
            "CcrVersion = '([^']+)'")
        if ($m.Success -and $m.Groups[1].Value -ne $script:CcrVersion) {
            Write-Host "ccr: v$($m.Groups[1].Value) is on disk, this shell had loaded v$($script:CcrVersion) - reloading (run '. `$PROFILE' to make it stick)." -ForegroundColor Yellow
            $inv = ". `"$selfPath`"; Resume-CcSessions -Tool $Tool -Top $Top"
            if ($filterText) { $inv += " -Filter '$($filterText -replace "'", "''")'" }
            if ($New) { $inv += ' -New' }
            if ($NewWindow) { $inv += ' -NewWindow' }
            if ($Accounts) { $inv += ' -Accounts' }
            if ($AddAccount) { $inv += " -AddAccount '$AddAccount'" }
            if ($CopyStatusline) { $inv += ' -CopyStatusline' }
            if ($RemoveAccount) { $inv += " -RemoveAccount '$RemoveAccount'" }
            if ($DisableAccounts) { $inv += ' -DisableAccounts' }
            if ($Root) { $inv += " -Root '$($Root -replace "'", "''")'" }
            if ($WhatIfPreference) { $inv += ' -WhatIf' }
            & ([scriptblock]::Create($inv))
            return
        }
    }
    catch { }

    # --- account management (multi-account) --------------------------------
    if ($Accounts) { Show-CcrAccounts; return }
    if ($AddAccount -or $RemoveAccount -or $DisableAccounts) {
        try {
            if ($AddAccount) { Add-CcrAccount -Label $AddAccount -Tool $Tool -CopyStatusline:$CopyStatusline }
            elseif ($RemoveAccount) { Remove-CcrAccount -Label $RemoveAccount -Tool $Tool }
            else { Disable-CcrMultiAccount }
        }
        catch { Write-Error "$_" }
        return
    }

    # Config dirs (accounts) per tool. One unlabeled root each unless ccr.json
    # says otherwise; -Root narrows the LISTING to a single configured account.
    # Multi-account mode (explicit config dir on every launch) depends on how
    # many accounts are configured for that tool, not on how many are listed:
    # a session must always start under its own account's dir.
    $allClaudeRoots = @(Get-CcrClaudeRoots)
    $allCodexRoots = @(Get-CcrCodexRoots)
    $multiClaude = $allClaudeRoots.Count -gt 1
    $multiCodex = $allCodexRoots.Count -gt 1
    $claudeRoots = $allClaudeRoots
    $codexRoots = $allCodexRoots
    if ($Root) {
        $known = @(@($allClaudeRoots.Label) + @($allCodexRoots.Label) | Where-Object { $_ } | Select-Object -Unique)
        if ($Root -notin $known) {
            Write-Error "ccr: no account '$Root' in $($script:CcrConfigPath) (known: $($known -join ', '))"
            return
        }
        $claudeRoots = @($allClaudeRoots | Where-Object { $_.Label -eq $Root })
        $codexRoots = @($allCodexRoots | Where-Object { $_.Label -eq $Root })
        foreach ($r in @($claudeRoots) + @($codexRoots)) { $r.Default = $true }
    }

    $sessions = [System.Collections.Generic.List[object]]::new()
    if ($Tool -in 'claude', 'all') { foreach ($r in $claudeRoots) { foreach ($s in Get-CcrClaudeSession -Root $r) { $sessions.Add($s) } } }
    if ($Tool -in 'codex', 'all') { foreach ($r in $codexRoots) { foreach ($s in Get-CcrCodexSession -Root $r) { $sessions.Add($s) } } }
    if ($sessions.Count -eq 0) { Write-Warning 'ccr: no sessions found.'; return }

    $sorted = @($sessions | Sort-Object LastActivity -Descending)
    if ($Top -gt 0) { $sorted = @($sorted | Select-Object -First $Top) }

    if ($New) {
        # -n: skip the session picker, go straight to the folder menu.
        if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
            Write-Error 'ccr: needs an interactive console.'
            return
        }
        $prevCtrlC = [Console]::TreatControlCAsInput
        [Console]::TreatControlCAsInput = $true
        $prevEnc = [Console]::OutputEncoding
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        [Console]::Write("`e[?1049h`e[?25l")
        # Any text after `ccr -n` prefills the NAME box, not the folder filter.
        try { $newPick = Select-CcrPath -Sessions $sorted -InitialName $filterText -ClaudeRoots $claudeRoots -CodexRoots $codexRoots }
        finally {
            [Console]::Write("`e[?25h`e[?1049l")
            [Console]::OutputEncoding = $prevEnc
            [Console]::TreatControlCAsInput = $prevCtrlC
        }
        $picked = if ($newPick) { [pscustomobject]@{ NewSessionPath = $newPick.Path; NewSessionTool = $newPick.Tool; NewSessionName = $newPick.Name; NewSessionRoot = $newPick.Root } } else { $null }
    }
    else {
        $canMultiOpen = $IsWindows -or [bool]$env:TMUX
        $picked = Select-CcrSession -Sessions $sorted -InitialFilter $filterText -NoMultiOpen:(-not $canMultiOpen) -ClaudeRoots $claudeRoots -CodexRoots $codexRoots
    }
    if ($null -eq $picked) { Write-Host 'ccr: cancelled.'; return }
    if ($picked -isnot [System.Array] -and $picked.PSObject.Properties['Restart']) {
        # An account was added from inside the picker: run again from the top
        # with the same arguments, so the listing and the account column
        # reflect the new ccr.json.
        $again = @{} + $PSBoundParameters
        if ($picked.Filter) { $again['Filter'] = @($picked.Filter) } else { $again.Remove('Filter') }
        Resume-CcSessions @again
        return
    }

    # With several accounts, a tool process must see the config dir of the
    # account its conversation belongs to (CLAUDE_CONFIG_DIR / CODEX_HOME). In
    # this shell that is a scoped env change around the call; in new tabs it
    # is prefixed to the command. Single-account tools get no override.
    function Get-CcrRootVar([string]$ToolName) {
        if ($ToolName -eq 'codex') { if ($multiCodex) { 'CODEX_HOME' } } elseif ($multiClaude) { 'CLAUDE_CONFIG_DIR' }
    }
    function Invoke-CcrWithRoot([string]$ToolName, [string]$RootPath, [scriptblock]$Body) {
        $var = Get-CcrRootVar $ToolName
        if (-not $var -or -not $RootPath) { & $Body; return }
        $prev = [System.Environment]::GetEnvironmentVariable($var)
        [System.Environment]::SetEnvironmentVariable($var, $RootPath)
        try { & $Body } finally { [System.Environment]::SetEnvironmentVariable($var, $prev) }
    }
    function Get-CcrRootPrefix([string]$ToolName, [string]$RootPath, [string]$Shell) {
        $var = Get-CcrRootVar $ToolName
        if (-not $var -or -not $RootPath) { return '' }
        if ($Shell -eq 'sh') { "$var='$($RootPath -replace "'", "'\''")' " }
        else { "`$env:$var='$($RootPath -replace "'", "''")'; " }
    }

    # New-conversation flow (Ctrl+N or -n): take over this tab, cc-style.
    $single = if ($picked -is [System.Array]) { $null } else { $picked }
    if ($single -and $single.PSObject.Properties['NewSessionPath']) {
        $dir = $single.NewSessionPath
        $newTool = if ($single.NewSessionTool -eq 'codex') { 'codex' } else { 'claude' }
        $newName = if ($single.PSObject.Properties['NewSessionName']) { "$($single.NewSessionName)".Trim() } else { '' }
        $newRoot = $null
        $toolRoots = if ($newTool -eq 'codex') { $codexRoots } else { $claudeRoots }
        if (Get-CcrRootVar $newTool) {
            $lbl = "$($single.NewSessionRoot)"
            $newRoot = @($toolRoots | Where-Object { $_.Label -eq $lbl } | Select-Object -First 1)[0]
            if (-not $newRoot) { $newRoot = @($toolRoots | Where-Object Default | Select-Object -First 1)[0] }
        }
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            Write-Warning "ccr: folder no longer exists: $dir"
            return
        }
        $what = "start a new $newTool session here$(if ($newRoot) { " (account: $($newRoot.Label))" })"
        if ($PSCmdlet.ShouldProcess($dir, $what)) {
            Set-Location -LiteralPath $dir
            $tabTitle = if ($newName) { "$newTool $([char]0x00B7) $newName" } else { "$newTool $([char]0x00B7) new" }
            try { $Host.UI.RawUI.WindowTitle = $tabTitle } catch { }
            Invoke-CcrWithRoot $newTool $newRoot.Path {
                if ($newTool -eq 'claude' -and $newName) { & claude --name $newName }
                else { & $newTool }
            }
        }
        else {
            $nameArg = if ($newTool -eq 'claude' -and $newName) { " --name '$newName'" } else { '' }
            "this tab: $(Get-CcrRootPrefix $newTool $newRoot.Path 'pwsh')$newTool$nameArg   (cd $dir)"
        }
        return
    }
    if (@($picked).Count -eq 0) { Write-Host 'ccr: cancelled.'; return }

    # Validate and build one launch entry per selection. Claude must run from
    # the recorded cwd (resume fails elsewhere); codex resumes from anywhere.
    $launch = [System.Collections.Generic.List[object]]::new()
    foreach ($s in @($picked)) {
        if ($s.SessionId -notmatch '^[0-9a-fA-F-]{36}$') {
            Write-Warning "ccr: skipping '$($s.Title)' - unexpected session id '$($s.SessionId)'"
            continue
        }
        $cwd = $s.Cwd
        if (-not $cwd -or -not (Test-Path -LiteralPath $cwd -PathType Container)) {
            if ($s.Tool -eq 'claude') {
                Write-Warning "ccr: skipping 'claude $([char]0x00B7) $($s.Title)' - recorded folder no longer exists: $cwd"
                continue
            }
            Write-Warning "ccr: 'codex $([char]0x00B7) $($s.Title)' - recorded folder missing ($cwd), starting in $HOME"
            $cwd = $HOME
        }
        # No --effort / --model overrides: a resumed session keeps its own
        # model and follows the user's saved effort default.
        $cmd = if ($s.Tool -eq 'claude') { "claude --resume $($s.SessionId)" }
        else { "codex resume $($s.SessionId)" }
        $rootPath = if ($s.PSObject.Properties['RootPath']) { $s.RootPath } else { $null }
        # Account mode: a TargetRoot label different from the row's own account
        # means "move this conversation there, then open it there".
        $moveTo = $null
        $tgt = if ($s.PSObject.Properties['TargetRoot']) { "$($s.TargetRoot)" } else { '' }
        if ($tgt -and $tgt -ne "$($s.Root)") {
            $pool = if ($s.Tool -eq 'codex') { $allCodexRoots } else { $allClaudeRoots }
            $moveTo = @($pool | Where-Object { $_.Label -eq $tgt } | Select-Object -First 1)[0]
            if (-not $moveTo) { Write-Warning "ccr: no $($s.Tool) dir for account '$tgt' - '$($s.Title)' stays under '$($s.Root)'" }
            elseif ($s.Running) { Write-Warning "ccr: '$($s.Title)' is running - close it before moving it to '$tgt'; skipped"; continue }
            else { $rootPath = $moveTo.Path }
        }
        $launch.Add([pscustomobject]@{ Tool = $s.Tool; Title = $s.Title; Cwd = $cwd; Command = $cmd; RootPath = $rootPath; Session = $s; MoveTo = $moveTo })
    }
    if ($launch.Count -eq 0) { Write-Warning 'ccr: nothing to open.'; return }

    # The first selection takes over THIS tab (which would otherwise sit idle
    # at a prompt); the rest open as new tabs. With -NewWindow (Windows only),
    # or when not on an interactive console, everything goes to the terminal
    # backend instead: Windows Terminal on Windows, tmux windows elsewhere.
    if (-not $IsWindows) { $NewWindow = $false }
    $inline = $null
    $tabs = @($launch)
    if (-not $NewWindow -and -not [Console]::IsOutputRedirected) {
        $inline = $launch[0]
        $tabs = @($launch | Select-Object -Skip 1)
    }
    if ($tabs.Count -gt 0 -and -not $IsWindows -and -not $env:TMUX) {
        Write-Warning 'ccr: opening several sessions outside Windows needs tmux - only the first selection starts (in this tab).'
        $tabs = @()
    }

    # wt.exe args as a flat array; ';' as its own element needs no escaping.
    $wtArgs = @()
    if ($IsWindows -and $tabs.Count -gt 0) {
        $wtArgs = if ($NewWindow) { @('-w', 'new') } else { @('-w', '0') }
        $first = $true
        foreach ($s in $tabs) {
            if (-not $first) { $wtArgs += ';' }
            $cwd = $s.Cwd.TrimEnd('\', '/')
            if ($cwd -match '^[A-Za-z]:$') { $cwd += '\' }   # bare "D:" is drive-relative
            $title = "$($s.Tool) $([char]0x00B7) $($s.Title)" -replace '["\;]', ' '
            if ($title.Length -gt 40) { $title = $title.Substring(0, 39) + [char]0x2026 }
            $wtArgs += @('new-tab', '-d', $cwd, '--title', $title, 'pwsh.exe', '-NoExit', '-Command', ((Get-CcrRootPrefix $s.Tool $s.RootPath 'pwsh') + $s.Command))
            $first = $false
        }
    }

    $moves = @($launch | Where-Object MoveTo)
    $what = ($launch | ForEach-Object { "$($_.Tool):$($_.Title)$(if ($_.MoveTo) { " -> account $($_.MoveTo.Label)" })" }) -join ', '
    if ($PSCmdlet.ShouldProcess($what, "open as terminal tabs$(if ($moves.Count) { ", re-homing $($moves.Count)" })")) {
        # Re-home first, so the resume finds the transcript in its new dir.
        foreach ($m in $moves) {
            try {
                $null = Move-CcrSessionToRoot -Session $m.Session -TargetRoot $m.MoveTo
                Write-Host "ccr: moved '$($m.Title)' to account '$($m.MoveTo.Label)'"
            }
            catch { Write-Warning "ccr: could not move '$($m.Title)' to '$($m.MoveTo.Label)': $_ - it will not resume there" }
        }
        if ($tabs.Count -gt 0) {
            if ($IsWindows) { wt.exe @wtArgs }
            else {
                # tmux runs the command via sh -c; the window closes when the
                # agent exits. Command text is fixed words + a validated uuid.
                foreach ($s in $tabs) { & tmux new-window -c $s.Cwd ((Get-CcrRootPrefix $s.Tool $s.RootPath 'sh') + $s.Command) }
            }
        }
        if ($inline) {
            # Run the first selection in this very shell, like `cc` does:
            # cd to the recorded folder and hand the tab over to the agent.
            Set-Location -LiteralPath $inline.Cwd
            try { $Host.UI.RawUI.WindowTitle = "$($inline.Tool) $([char]0x00B7) $($inline.Title)" } catch { }
            # Split into words and SPLAT: claude resolves to the npm .ps1 shim,
            # which would receive a plain array argument as one value and
            # forward it to node as a single joined string.
            $parts = $inline.Command -split ' '   # fixed words + validated uuid, no quoting needed
            $exe = $parts[0]
            $exeArgs = $parts[1..($parts.Count - 1)]
            Invoke-CcrWithRoot $inline.Tool $inline.RootPath { & $exe @exeArgs }
        }
    }
    else {
        foreach ($m in $moves) { "move: $($m.Tool) $([char]0x00B7) $($m.Title)  ->  account '$($m.MoveTo.Label)' ($($m.MoveTo.Path))" }
        if ($inline) { "this tab: $(Get-CcrRootPrefix $inline.Tool $inline.RootPath 'pwsh')$($inline.Command)   (cd $($inline.Cwd))" }
        if ($tabs.Count -gt 0) {
            if ($IsWindows) { "wt.exe $($wtArgs -join ' ')" }
            else { foreach ($s in $tabs) { "tmux new-window -c $($s.Cwd) '$((Get-CcrRootPrefix $s.Tool $s.RootPath 'sh') + $s.Command)'" } }
        }
    }
}
