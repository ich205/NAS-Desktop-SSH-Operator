# Jellyfin Organizer launcher (PowerShell)
Set-Location -LiteralPath $PSScriptRoot

function Try-Run($cmd, $args) {
  $p = Get-Command $cmd -ErrorAction SilentlyContinue
  if ($null -ne $p) {
    & $cmd @args
    return $true
  }
  return $false
}

if (Try-Run "pyw" @("-3", "$PSScriptRoot\JellyfinOrganizer.pyw")) { exit 0 }
if (Try-Run "pythonw" @("$PSScriptRoot\JellyfinOrganizer.pyw")) { exit 0 }
if (Try-Run "py" @("-3", "$PSScriptRoot\JellyfinOrganizer.pyw")) { exit 0 }
if (Try-Run "python" @("$PSScriptRoot\JellyfinOrganizer.pyw")) { exit 0 }

Write-Host ""
Write-Host "Python 3 wurde nicht gefunden."
Write-Host "Bitte installiere Python 3.11 oder neuer (inkl. Tkinter) und starte dann erneut."
Read-Host "ENTER zum Beenden"
