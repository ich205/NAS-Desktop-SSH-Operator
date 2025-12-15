<#
  cleanup_repo.ps1

  One-time cleanup tool to remove accidentally committed generated files
  (e.g. __pycache__/ *.pyc) and PatchPilot artifacts (.patchpilot/, *.patch).

  Why this exists:
  - git apply (PatchPilot) cannot reliably delete binary files (pyc) without
    exact blob indices in the patch.
  - This script uses git commands to remove tracked junk safely.

  Usage (from repo root):
    powershell -ExecutionPolicy Bypass -File .\tools\cleanup_repo.ps1 -DryRun
    powershell -ExecutionPolicy Bypass -File .\tools\cleanup_repo.ps1 -DoIt

  After -DoIt:
    git status
    git commit -m "chore: repo cleanup (remove caches/patch artifacts)"
    git push
#>

param(
  [switch]$DryRun,
  [switch]$DoIt,
  [switch]$DeleteWorkingTreeFiles
)

function Fail($msg) {
  Write-Host "[cleanup] ERROR: $msg" -ForegroundColor Red
  exit 1
}

function Run([string]$cmd) {
  Write-Host "[cleanup] $cmd" -ForegroundColor DarkGray
  & powershell -NoProfile -Command $cmd
  if ($LASTEXITCODE -ne 0) { Fail "Command failed (exit=$LASTEXITCODE): $cmd" }
}

if (-not (Test-Path ".git")) {
  Fail "Bitte im Repo-Root ausführen ('.git' nicht gefunden)."
}

if (($DryRun -and $DoIt) -or (-not $DryRun -and -not $DoIt)) {
  Write-Host ""
  Write-Host "Usage:" -ForegroundColor Yellow
  Write-Host "  .\tools\cleanup_repo.ps1 -DryRun"
  Write-Host "  .\tools\cleanup_repo.ps1 -DoIt [-DeleteWorkingTreeFiles]"
  Write-Host ""
  exit 0
}

Write-Host "[cleanup] Scanning tracked files…" -ForegroundColor Cyan

# Use git pathspec with glob support:
$specs = @(
  ':(glob)**/__pycache__/*',
  ':(glob)**/*.pyc',
  ':(glob)**/*.pyo',
  '.patchpilot',
  ':(glob).patchpilot/*',
  ':(glob)**/*.patch',
  'demo.patch',
  'issue12.patch'
)

$tracked = @()
foreach ($s in $specs) {
  try {
    $out = & git ls-files -- $s 2>$null
    if ($out) { $tracked += $out }
  } catch {
    # ignore
  }
}

$tracked = $tracked | Sort-Object -Unique

if ($tracked.Count -eq 0) {
  Write-Host "[cleanup] Nothing to remove. Repo looks clean." -ForegroundColor Green
  exit 0
}

Write-Host ""
Write-Host "[cleanup] Tracked junk candidates:" -ForegroundColor Yellow
$tracked | ForEach-Object { Write-Host "  - $_" }
Write-Host ""

if ($DryRun) {
  Write-Host "[cleanup] DryRun only. No changes made." -ForegroundColor Green
  exit 0
}

Write-Host "[cleanup] Removing from Git index (git rm --cached) …" -ForegroundColor Cyan

# git rm supports multiple paths; we pass them via PowerShell splatting
& git rm -r --cached --force -- $tracked
if ($LASTEXITCODE -ne 0) { Fail "git rm failed (exit=$LASTEXITCODE)" }

if ($DeleteWorkingTreeFiles) {
  Write-Host "[cleanup] Deleting files from working tree as well …" -ForegroundColor Cyan
  foreach ($p in $tracked) {
    if (Test-Path $p) {
      try {
        Remove-Item -LiteralPath $p -Force -Recurse -ErrorAction Stop
      } catch {
        Write-Host "[cleanup] WARN: Could not delete $p : $($_.Exception.Message)" -ForegroundColor DarkYellow
      }
    }
  }
}

Write-Host ""
Write-Host "[cleanup] Done. Next steps:" -ForegroundColor Green
Write-Host "  git status"
Write-Host "  git commit -m ""chore: repo cleanup (remove caches/patch artifacts)"""
Write-Host "  git push"
Write-Host ""

