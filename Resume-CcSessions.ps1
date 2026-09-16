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
$script:CcrVersion = '0.25'

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

# sessionId -> live claude process id (stale pid files filtered out).
function Get-CcrClaudeRunningMap {
    $map = @{}
    $dir = Join-Path (Get-CcrClaudeRoot) 'sessions'
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
    param()
    $projRoot = Join-Path (Get-CcrClaudeRoot) 'projects'
    if (-not (Test-Path -LiteralPath $projRoot)) { return @() }
    $running = Get-CcrClaudeRunningMap
    $hist = $null   # ~\.claude\history.jsonl, loaded lazily only if a fallback is needed

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
                if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path (Get-CcrClaudeRoot) 'history.jsonl') -IdName 'sessionId' }
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
                if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path (Get-CcrClaudeRoot) 'history.jsonl') -IdName 'sessionId' }
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
    $map = @{}
    try {
        $stateDb = Get-ChildItem -LiteralPath (Join-Path $HOME '.codex') -Filter 'state_*.sqlite' -File -ErrorAction Stop |
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
        foreach ($line in (Read-CcrHeadLines -Path (Join-Path $HOME '.codex/session_index.jsonl'))) {
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
    param()
    $root = Join-Path $HOME '.codex/sessions'
    if (-not (Test-Path -LiteralPath $root)) { return @() }
    $hist = $null   # ~\.codex\history.jsonl, loaded lazily only if a fallback is needed
    $running = Get-CcrCodexRunningMap
    $curated = Get-CcrCodexTitleMap

    $files = Get-ChildItem -LiteralPath $root -Recurse -Filter 'rollout-*.jsonl' -File -ErrorAction SilentlyContinue
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
            if ($null -eq $hist) { $hist = Get-CcrHistoryTable -Path (Join-Path $HOME '.codex/history.jsonl') -IdName 'session_id' }
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

# Tool chooser for a NEW conversation: claude or codex. Returns the tool
# name, or $null on Esc. Runs inside the caller's alt buffer.
function Select-CcrTool {
    $tools = @(
        [pscustomobject]@{ Name = 'claude'; Color = "`e[38;5;208m"; Note = 'asks for a session name' },
        [pscustomobject]@{ Name = 'codex'; Color = "`e[36m"; Note = 'no start name - /rename inside' }
    )
    $cursor = 0
    while ($true) {
        $sb = [System.Text.StringBuilder]::new()
        [void]$sb.Append("`e[H").Append('tool for the new conversation').Append("`e[K`n")
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
# flag - /rename inside). Returns @{ Path; Tool; Name } or $null to go back.
# Runs inside the caller's alt buffer.
function Select-CcrPath {
    param(
        [Parameter(Mandatory)][object[]]$Sessions,
        [string]$InitialName = ''
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
                    # Folder -> tool -> (claude only) name. Esc at any step comes
                    # back here and repaints the folder list.
                    $tool = Select-CcrTool
                    if ($tool -eq 'codex') {
                        return [pscustomobject]@{ Path = $view[$cursor].Path; Tool = 'codex'; Name = '' }
                    }
                    if ($tool -eq 'claude') {
                        $name = Read-CcrInput -Prompt 'name> ' -Text $InitialName `
                            -Hint "session name for claude $([char]0x00B7) Enter confirm (empty = auto title) $([char]0x00B7) Esc back"
                        if ($null -ne $name) {
                            return [pscustomobject]@{ Path = $view[$cursor].Path; Tool = 'claude'; Name = $name.Trim() }
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
        [switch]$NoMultiOpen
    )

    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        Write-Error 'ccr: needs an interactive console.'
        return $null
    }

    $sel = [System.Collections.Generic.HashSet[string]]::new()
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
                        ("$($_.Tool) $($_.Title) $($_.Cwd)$(if ($_.Cleared) { ' cleared' })").IndexOf($filter, [StringComparison]::OrdinalIgnoreCase) -ge 0
                    })
            }
            else { $Sessions }

            # --- layout ---
            $w = [Console]::WindowWidth
            $h = [Console]::WindowHeight
            $viewH = [Math]::Max(1, $h - 2)
            if ($cursor -gt $view.Count - 1) { $cursor = [Math]::Max(0, $view.Count - 1) }
            if ($cursor -lt $top) { $top = $cursor }
            elseif ($cursor -ge $top + $viewH) { $top = $cursor - $viewH + 1 }
            if ($top -gt [Math]::Max(0, $view.Count - $viewH)) { $top = [Math]::Max(0, $view.Count - $viewH) }

            # row = status(1) sp tool(6) sp age(6) 2sp title 2sp cwd
            $cwdW = [Math]::Min(35, [Math]::Max(12, [int]($w * 0.35)))
            $titleW = $w - 21 - $cwdW
            if ($titleW -lt 10) { $cwdW = [Math]::Max(8, $cwdW + $titleW - 10); $titleW = [Math]::Max(1, $w - 23 - $cwdW) }

            # --- render one full frame ---
            $sb = [System.Text.StringBuilder]::new()
            [void]$sb.Append("`e[H")
            $counts = "$($view.Count)/$($Sessions.Count)"
            if ($sel.Count) { $counts += " $([char]0x00B7) $($sel.Count) marked" }
            if ($NoMultiOpen -and $sel.Count -gt 1) { $counts += " ! only the first will open (no tmux)" }
            $hdr = "filter> $filter"
            $pad = $w - 1 - $hdr.Length - $counts.Length - 2
            if ($pad -lt 1) { $pad = 1 }
            $line1 = $hdr + (' ' * $pad) + $counts
            if ($line1.Length -gt $w - 1) { $line1 = $line1.Substring(0, $w - 1) }
            [void]$sb.Append($line1).Append("`e[K`n")
            $hint = "$([char]0x2191)$([char]0x2193) move $([char]0x00B7) Space mark $([char]0x00B7) Enter open $([char]0x00B7) Ctrl+N new $([char]0x00B7) Del delete $([char]0x00B7) Esc cancel $([char]0x00B7) type to filter $([char]0x00B7) v$script:CcrVersion"
            if ($hint.Length -gt $w - 1) { $hint = $hint.Substring(0, $w - 1) }
            [void]$sb.Append("`e[2m").Append($hint).Append("`e[22m`e[K")

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
                    $mark = if ($sel.Contains($key)) { "`e[32m$([char]0x25CF)`e[39m" } else { ' ' }
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
                    $row = "$mark $toolColor$($s.Tool.PadRight(6))`e[39m$age  $titleTxt  `e[2m$cwdTxt`e[22m"
                    if ($i -eq $cursor) { $row = "`e[7m$row`e[27m" }
                    [void]$sb.Append("`n").Append($row).Append("`e[K")
                }
            }
            [void]$sb.Append("`e[J")
            [Console]::Write($sb.ToString())

            # --- input ---
            $k = [Console]::ReadKey($true)
            if ($k.Key -eq [ConsoleKey]::C -and ($k.Modifiers -band [ConsoleModifiers]::Control)) { return $null }
            if ($k.Key -eq [ConsoleKey]::N -and ($k.Modifiers -band [ConsoleModifiers]::Control)) {
                # Ctrl+N: pick a folder (and tool) for a brand-new conversation.
                $newPick = Select-CcrPath -Sessions $Sessions
                if ($newPick) { return [pscustomobject]@{ NewSessionPath = $newPick.Path; NewSessionTool = $newPick.Tool; NewSessionName = $newPick.Name } }
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
                        $key = "$($view[$cursor].Tool)|$($view[$cursor].SessionId)"
                        if (-not $sel.Add($key)) { [void]$sel.Remove($key) }
                    }
                }
                'Enter' {
                    $marked = @($Sessions | Where-Object { $sel.Contains("$($_.Tool)|$($_.SessionId)") })
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
    # Cache-buster: GitHub's raw CDN can serve a minutes-old file after a push.
    $url = "https://raw.githubusercontent.com/Cepstral/claude-codex-resume/$branch/Resume-CcSessions.ps1?t=$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    if ($WhatIf) { "would download branch '$branch' -> $TargetPath"; return }
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
        Write-Host "ccr: channel '$Channel' (branch $branch) v$had -> v$newVer at $TargetPath. Run: $cmd" -ForegroundColor Green
        if ($newVer -eq $had) { Write-Host "ccr: same version as before - nothing newer on that branch yet." }
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
        # Self-update: -Channel stable|test downloads that branch's script over
        # the loaded copy and remembers the channel; -Update refreshes the
        # remembered channel (stable if none was ever chosen).
        [ValidateSet('stable', 'test')][string]$Channel = '',
        [switch]$Update
    )
    $filterText = if ($Filter) { ($Filter -join ' ').Trim() } else { '' }

    # --- self-update from a channel (before the self-heal, on purpose) ------
    if ($Update -or $Channel) {
        $selfPath = $MyInvocation.MyCommand.ScriptBlock.File
        if (-not $selfPath) { $selfPath = Join-Path (Split-Path -Parent $PROFILE) 'Resume-CcSessions.ps1' }
        $ch = $Channel
        if (-not $ch) {
            $chFile = Join-Path (Split-Path -Parent $selfPath) 'ccr.channel'
            $ch = if (Test-Path -LiteralPath $chFile) { (Get-Content -LiteralPath $chFile -Raw).Trim() } else { 'stable' }
        }
        Update-CcrSelf -Channel $ch -SelfPath $selfPath -WhatIf:$WhatIfPreference
        return
    }

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
            if ($WhatIfPreference) { $inv += ' -WhatIf' }
            & ([scriptblock]::Create($inv))
            return
        }
    }
    catch { }

    $sessions = [System.Collections.Generic.List[object]]::new()
    if ($Tool -in 'claude', 'all') { foreach ($s in Get-CcrClaudeSession) { $sessions.Add($s) } }
    if ($Tool -in 'codex', 'all') { foreach ($s in Get-CcrCodexSession) { $sessions.Add($s) } }
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
        try { $newPick = Select-CcrPath -Sessions $sorted -InitialName $filterText }
        finally {
            [Console]::Write("`e[?25h`e[?1049l")
            [Console]::OutputEncoding = $prevEnc
            [Console]::TreatControlCAsInput = $prevCtrlC
        }
        $picked = if ($newPick) { [pscustomobject]@{ NewSessionPath = $newPick.Path; NewSessionTool = $newPick.Tool; NewSessionName = $newPick.Name } } else { $null }
    }
    else {
        $canMultiOpen = $IsWindows -or [bool]$env:TMUX
    $picked = Select-CcrSession -Sessions $sorted -InitialFilter $filterText -NoMultiOpen:(-not $canMultiOpen)
    }
    if ($null -eq $picked) { Write-Host 'ccr: cancelled.'; return }

    # New-conversation flow (Ctrl+N or -n): take over this tab, cc-style.
    $single = if ($picked -is [System.Array]) { $null } else { $picked }
    if ($single -and $single.PSObject.Properties['NewSessionPath']) {
        $dir = $single.NewSessionPath
        $newTool = if ($single.NewSessionTool -eq 'codex') { 'codex' } else { 'claude' }
        $newName = if ($single.PSObject.Properties['NewSessionName']) { "$($single.NewSessionName)".Trim() } else { '' }
        if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
            Write-Warning "ccr: folder no longer exists: $dir"
            return
        }
        if ($PSCmdlet.ShouldProcess($dir, "start a new $newTool session here")) {
            Set-Location -LiteralPath $dir
            $tabTitle = if ($newName) { "$newTool $([char]0x00B7) $newName" } else { "$newTool $([char]0x00B7) new" }
            try { $Host.UI.RawUI.WindowTitle = $tabTitle } catch { }
            if ($newTool -eq 'claude' -and $newName) { & claude --name $newName }
            else { & $newTool }
        }
        else {
            $nameArg = if ($newTool -eq 'claude' -and $newName) { " --name '$newName'" } else { '' }
            "this tab: $newTool$nameArg   (cd $dir)"
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
        $launch.Add([pscustomobject]@{ Tool = $s.Tool; Title = $s.Title; Cwd = $cwd; Command = $cmd })
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
            $wtArgs += @('new-tab', '-d', $cwd, '--title', $title, 'pwsh.exe', '-NoExit', '-Command', $s.Command)
            $first = $false
        }
    }

    $what = ($launch | ForEach-Object { "$($_.Tool):$($_.Title)" }) -join ', '
    if ($PSCmdlet.ShouldProcess($what, 'open as terminal tabs')) {
        if ($tabs.Count -gt 0) {
            if ($IsWindows) { wt.exe @wtArgs }
            else {
                # tmux runs the command via sh -c; the window closes when the
                # agent exits. Command text is fixed words + a validated uuid.
                foreach ($s in $tabs) { & tmux new-window -c $s.Cwd $s.Command }
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
            & $exe @exeArgs
        }
    }
    else {
        if ($inline) { "this tab: $($inline.Command)   (cd $($inline.Cwd))" }
        if ($tabs.Count -gt 0) {
            if ($IsWindows) { "wt.exe $($wtArgs -join ' ')" }
            else { foreach ($s in $tabs) { "tmux new-window -c $($s.Cwd) '$($s.Command)'" } }
        }
    }
}
