# =============================================================================
#  install.ps1 - one-shot installer for ccr (claude-codex-resume)
#
#  From a clone:      .\install.ps1
#  Without cloning:   irm https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/install.ps1 | iex
#
#  What it does (idempotent, no admin rights needed):
#    1. puts Resume-CcSessions.ps1 next to your PowerShell profile
#    2. adds one dot-source line to the profile (skipped if already there)
#  Re-running updates the installed file in place.
# =============================================================================

$ErrorActionPreference = 'Stop'

$toolFile = 'Resume-CcSessions.ps1'
$rawUrl = "https://raw.githubusercontent.com/Cepstral/claude-codex-resume/main/$toolFile"

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
    Invoke-WebRequest -Uri $rawUrl -OutFile $targetPath -UseBasicParsing
    Write-Host "downloaded $toolFile -> $profileDir"
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
