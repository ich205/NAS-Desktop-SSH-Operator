#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  python3 JellyfinOrganizer.pyw
  exit 0
fi
if command -v python >/dev/null 2>&1; then
  python JellyfinOrganizer.pyw
  exit 0
fi

echo "Python 3 wurde nicht gefunden. Bitte installiere Python 3.10+ und starte erneut."
exit 1
