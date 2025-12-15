Repo Cleanup (One-time)
======================

If your repo accidentally contains:
  - __pycache__/ or *.pyc (Python bytecode)
  - .patchpilot/ (PatchPilot local state)
  - *.patch files in the repo root

…run this from the repo root:

  powershell -ExecutionPolicy Bypass -File .\tools\cleanup_repo.ps1 -DryRun
  powershell -ExecutionPolicy Bypass -File .\tools\cleanup_repo.ps1 -DoIt -DeleteWorkingTreeFiles

Then:
  git commit -m "chore: repo cleanup (remove caches/patch artifacts)"
  git push

Afterwards .gitignore will keep the repo clean.
