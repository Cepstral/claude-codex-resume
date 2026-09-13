# =============================================================================
#  install.ps1 - one-shot installer for ccr (claude-codex-resume)
#
#  From a clone:      .\install.ps1
#  Without cloning:   irm https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.ps1 | iex
#  Private repo:      gh api repos/Cepstral/claude-codex-resume/contents/install.ps1 -H "Accept: application/vnd.github.raw" | Out-String | iex
#
#  What it does (idempotent, no admin rights needed):
#    1. puts Resume-CcSessions.ps1 next to your PowerShell profile
#    2. adds one dot-source line to the profile (skipped if already there)
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
$loadLine = ". (Join-Path (Split-Path -Parent `$PROFILE) '$toolFile')"
$existing = if (Test-Path -LiteralPath $PROFILE) { Get-Content -LiteralPath $PROFILE -Raw } else { '' }
if ($existing -match [regex]::Escape($toolFile)) {
    Write-Host 'profile already loads ccr - left untouched'
}
else {
    Add-Content -LiteralPath $PROFILE -Value "`n# claude-codex-resume: ccr session picker`n$loadLine"
    Write-Host "added the load line to $PROFILE"
}

Write-Host ''
Write-Host 'done - open a new terminal tab and run: ccr' -ForegroundColor Green
