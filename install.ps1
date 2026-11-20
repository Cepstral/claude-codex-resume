# =============================================================================
#  install.ps1 - one-shot installer for ccr (claude-codex-resume)
#
#  From a clone:      .\install.ps1
#  Without cloning:   irm https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.ps1 | iex
#  Private repo:      gh api repos/Cepstral/claude-codex-resume/contents/install.ps1 -H "Accept: application/vnd.github.raw" | Out-String | iex
#
#  What it does (idempotent, no admin rights needed):
#    1. puts Resume-CcSessions.ps1 next to your PowerShell profile
#    2. adds a small block to the profile that loads it the first time ccr
#       runs (skipped if already there; the plain load line older installs
#       wrote is replaced by it)
#  Re-running updates the installed file in place. If ccr is already loaded
#  from somewhere else, nothing is installed (a second copy would shadow it).
# =============================================================================

$ErrorActionPreference = 'Stop'

$repo = 'Cepstral/claude-codex-resume'
$toolFile = 'Resume-CcSessions.ps1'
$rawUrl = "https://raw.githubusercontent.com/$repo/main/$toolFile"

# --- already installed some other way? --------------------------------------
$loaded = Get-Command Resume-CcSessions -ErrorAction SilentlyContinue
$loadedFrom = if ($loaded) { $loaded.ScriptBlock.File } else { $null }
if ($loadedFrom -and ((Split-Path -Parent $loadedFrom) -ne (Split-Path -Parent $PROFILE))) {
    Write-Host "ccr is already loaded from $loadedFrom - nothing to install." -ForegroundColor Yellow
    Write-Host 'Update that copy instead; a second one next to the profile would shadow it.'
    return
}

# --- locate or fetch the tool file ------------------------------------------
$sourcePath = if ($PSScriptRoot) { Join-Path $PSScriptRoot $toolFile } else { $null }
$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path -LiteralPath $profileDir)) {
    New-Item -ItemType Directory -Path $profileDir -Force | Out-Null
}
$targetPath = Join-Path $profileDir $toolFile

if ($sourcePath -and (Test-Path -LiteralPath $sourcePath)) {
    Copy-Item -LiteralPath $sourcePath -Destination $targetPath -Force
    Write-Host "copied $toolFile -> $profileDir"
}
else {
    # Running via `irm | iex` (no script root): download the current version.
    # The anonymous raw URL 404s while the repo is private; fall back to an
    # authenticated fetch through the GitHub CLI when it is available.
    try {
        Invoke-WebRequest -Uri $rawUrl -OutFile $targetPath -UseBasicParsing
        Write-Host "downloaded $toolFile -> $profileDir"
    }
    catch {
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
            throw "could not download $rawUrl ($($_.Exception.Message)). If the repo is private, install the GitHub CLI and run 'gh auth login' first."
        }
        $content = & gh api "repos/$repo/contents/$toolFile" -H 'Accept: application/vnd.github.raw' | Out-String
        if ($LASTEXITCODE -ne 0 -or -not $content) {
            throw "gh could not fetch $toolFile from $repo - run 'gh auth login' with an account that can read it."
        }
        Set-Content -LiteralPath $targetPath -Value $content -Encoding utf8NoBOM -NoNewline
        Write-Host "downloaded $toolFile via gh -> $profileDir"
    }
}
if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    Unblock-File -LiteralPath $targetPath -ErrorAction SilentlyContinue
}

# --- wire it into the profile (idempotent) ----------------------------------
# The block loads the file on the first call of ccr, ccrtest or
# Resume-CcSessions, not when a shell starts: 400 KB of script cost every new
# tab ~0.3 s. Import-Ccr dot-sources it and makes its functions, the ccr alias
# and its Ccr* variables global, so from then on everything is as if the
# profile had loaded it. `. $PROFILE` puts the three entry points back, and
# the next call reads the file from disk again.
$blockStart = '# >>> claude-codex-resume: ccr, loaded at its first call >>>'
$blockEnd = '# <<< claude-codex-resume <<<'
$block = @(
    $blockStart
    "`$global:__ccrFile = Join-Path (Split-Path -Parent `$PROFILE) '$toolFile'"
    'function global:Import-Ccr {'
    '    Remove-Item -Path function:global:ccr, function:global:ccrtest, function:global:Resume-CcSessions -ErrorAction SilentlyContinue'
    '    . $global:__ccrFile'
    '    $fns = @(Get-ChildItem function: | Where-Object { $_.ScriptBlock.File -eq $global:__ccrFile })'
    '    foreach ($f in $fns) { Set-Item -LiteralPath "function:global:$($f.Name)" -Value $f.ScriptBlock }'
    '    foreach ($a in Get-ChildItem alias: | Where-Object { $_.ResolvedCommandName -in $fns.Name }) {'
    '        Set-Alias -Name $a.Name -Value $a.Definition -Scope Global -Force'
    '    }'
    '    foreach ($v in Get-Variable -Scope Script | Where-Object Name -like ''Ccr*'') {'
    '        Set-Variable -Name $v.Name -Value $v.Value -Scope Global -Force'
    '    }'
    '}'
    'function global:ccr { Import-Ccr; Resume-CcSessions @args }'
    'function global:ccrtest { Import-Ccr; ccrtest @args }'
    'function global:Resume-CcSessions { Import-Ccr; Resume-CcSessions @args }'
    $blockEnd
)
# What install.ps1 up to ccr v0.100 appended: this comment and this line.
$oldComment = '# claude-codex-resume: ccr session picker'
$oldLine = ". (Join-Path (Split-Path -Parent `$PROFILE) '$toolFile')"

# The profile is rewritten with the encoding it has (a BOM stays a BOM) and
# its own line ends.
$bytes = if (Test-Path -LiteralPath $PROFILE) { [IO.File]::ReadAllBytes($PROFILE) } else { [byte[]]@() }
$enc = if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { [Text.UTF8Encoding]::new($true) }
elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { [Text.UnicodeEncoding]::new($false, $true) }
else { [Text.UTF8Encoding]::new($false) }
$existing = if ($bytes.Length) { [IO.File]::ReadAllText($PROFILE) } else { '' }
$nl = if ($existing -match "`r`n") { "`r`n" } else { "`n" }
$lines = [System.Collections.Generic.List[string]]::new()
if ($existing) { $lines.AddRange([string[]]($existing -split '\r?\n')) }
$from = $lines.IndexOf($blockStart)
$to = if ($from -ge 0) { $lines.IndexOf($blockEnd, $from) } else { -1 }
$at = $lines.IndexOf($oldLine)
$what = $null
if ($from -ge 0 -and $to -gt $from) {
    if ((@($lines.GetRange($from, $to - $from + 1)) -join "`n") -eq ($block -join "`n")) {
        Write-Host 'profile already loads ccr at its first call - left untouched'
    }
    else {
        $lines.RemoveRange($from, $to - $from + 1)
        $lines.InsertRange($from, [string[]]$block)
        $what = "updated the ccr block in $PROFILE"
    }
}
elseif ($at -ge 0) {
    # the exact line an older install.ps1 wrote (and its comment, if still there)
    $first = if ($at -gt 0 -and $lines[$at - 1] -eq $oldComment) { $at - 1 } else { $at }
    $lines.RemoveRange($first, $at - $first + 1)
    $lines.InsertRange($first, [string[]]$block)
    $what = "replaced the load line in $PROFILE with a block that loads ccr at its first call (new shells start faster)"
}
elseif ($existing -match [regex]::Escape($toolFile)) {
    Write-Host 'profile already loads ccr (in a way of its own) - left untouched'
}
else {
    if ($lines.Count -and $lines[$lines.Count - 1] -ne '') { $lines.Add('') }
    $lines.AddRange([string[]]$block)
    $lines.Add('')
    $what = "added the ccr block to $PROFILE"
}
if ($what) {
    $profileDirNow = Split-Path -Parent $PROFILE
    if (-not (Test-Path -LiteralPath $profileDirNow)) { New-Item -ItemType Directory -Path $profileDirNow -Force | Out-Null }
    [IO.File]::WriteAllText($PROFILE, ($lines -join $nl), $enc)
    Write-Host $what
}

Write-Host ''
Write-Host 'done - open a new terminal tab and run: ccr' -ForegroundColor Green
