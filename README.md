# Jellyfin Organizer (JFO) — Plan-First NAS Organizer für Jellyfin

## Schnellstart (ohne IDE)

**Windows (empfohlen):** Doppelklick auf `Start_JellyfinOrganizer.bat`  
**Windows (alternativ):** `Start_JellyfinOrganizer.ps1`  
**macOS:** Doppelklick auf `Start_JellyfinOrganizer.command`  
**Linux/macOS Terminal:** `./start.sh`

Beim ersten Start wird automatisch eine lokale Virtualenv (`.venv`) im Projektordner erstellt und die Abhängigkeiten installiert.
Danach startet die GUI direkt.

> Voraussetzung: **Python 3.10+** (auf Windows am besten 3.11/3.12 inkl. Tkinter).

Ein Desktop-Tool mit GUI, das **Shell/SSH-Skripte** zur Organisation einer großen Jellyfin-Mediathek auf einem NAS generiert.

**Kernprinzip:**

1. **Plan erstellen** (Operationenliste)
2. **Preview prüfen** (Tabelle Alt → Neu + Warnungen)
3. **Script anzeigen** (genau das, was ausgeführt wird)
4. **Ausführen** (erst nach explizitem Klick + optionaler Extra-Bestätigung)

Das Tool ist **absichtlich konservativ**:

- Standardmäßig **Dry-Run**
- Standardmäßig **kein Überschreiben**
- **Root-Sandbox** (Operationen außerhalb erlaubter Root-Pfade werden blockiert)
- Vollständiges **Journal (JSONL)** jeder Ausführung (Script + Exitcode + stdout/stderr)

> Hinweis zu Jellyfin & Hardlinks: Hardlinks werden in Jellyfin nicht zuverlässig dedupliziert. Nutze Hardlink-Libraries als "Sichten" und vermeide verschachtelte Library-Pfade.

---

## Tech-Stack

- **Python 3.11+**
- **Tkinter/ttk** (GUI, ohne extra GUI-Dependencies)
- **Paramiko** (SSH + Host-Key-Trust + optional SFTP)
- **SQLite** (Analyse-Index)

Warum Tkinter statt WPF/Avalonia/PySide6?

- Läuft ohne zusätzliche GUI-Runtime.
- Sehr stabil, leicht paketierbar.
- Für Funktionalität-first (Tabs, Tabellen, Output, Logs) vollkommen ausreichend.

---

## Repository-Struktur

```
.
├─ src/jfo/
│  ├─ app.py
│  ├─ core/
│  │  ├─ operations.py
│  │  ├─ plan.py
│  │  ├─ scriptgen.py
│  │  ├─ quoting.py
│  │  ├─ validators.py
│  │  ├─ nfo.py
│  │  ├─ media_grouping.py
│  │  ├─ history.py
│  │  └─ categories.py
│  ├─ infra/
│  │  ├─ settings.py
│  │  ├─ journal.py
│  │  ├─ sqlite_index.py
│  │  └─ ssh_client.py
│  └─ ui/
│     ├─ main_window.py
│     ├─ widgets.py
│     ├─ dialogs.py
│     └─ tabs/
│        ├─ tab_connection.py
│        ├─ tab_analysis.py
│        ├─ tab_create_dirs.py
│        ├─ tab_move.py
│        ├─ tab_rename.py
│        ├─ tab_hardlinks.py
│        └─ tab_history.py
├─ tests/
│  ├─ test_quoting.py
│  ├─ test_nfo.py
│  └─ test_collisions.py
└─ scripts/
   ├─ sample_paths.txt
   └─ sample_paths.csv
```

---

## Setup & Run (Entwicklung)

### 1) Virtualenv

```bash
python -m venv .venv
# Windows:
.venv\\Scripts\\activate
# Linux/macOS:
source .venv/bin/activate
```

### 2) Install

```bash
pip install -e .
```

### 3) Start

```bash
jfo
# alternativ:
# python -m jfo
```

---

## Packaging (Windows) — PyInstaller

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --name JellyfinOrganizer src/jfo/app.py
```

> Für SSH per Key solltest du OpenSSH/ssh (Windows Feature) installiert haben, ist aber **nicht zwingend**, weil JFO Paramiko integriert.

---

## Bedienung pro Tab (Kurz)

### Tab 1: Main (Verbindung)

- NAS Host/IP, Port, Username
- Auth: Passwort **oder** SSH-Key (Key empfohlen)
- **Verbindung testen**: führt `uname -a` + `id` aus.
- Zeigt beim ersten Kontakt den **Host-Key-Fingerprint** und fragt "Trust?".
- Settings-Bereich:
  - **Allowed Root Paths** (Root-Sandbox): z.B. `/volume1/media` (eine pro Zeile)
  - Dry-Run Default (global)

### Tab 2: Scan / Index

- Remote Root-Pfad angeben (z.B. `/volume2/medien`)
- Extensions (z.B. `mkv,mp4,nfo,jpg,png,srt,ass`) oder **Alle Dateien**
- **Plan erstellen** erzeugt ein Scan-Script (find).
- **Ausführen** scannt remote und speichert Pfade in SQLite.
- **Index Browser**:
  - Root auswählen
  - Ordnerliste laden (Prefix-Filter)
  - Dateien pro Ordner anzeigen
  - Suche (optional **nur Videos**)
  - Export CSV/JSONL (lokal)

### Tab 3: Erstellen (Ordnerstruktur aus Datei)

- Lokale Input-Datei wählen:
  - Option A: jede Zeile ein relativer Pfad
  - Option B: CSV `path;type`
- Remote Ziel-Root wählen
- Plan zeigt alle `mkdir -p` Operationen
- Ausführen erstellt Ordner (idempotent)

### Tab 4: Verschieben

- Quelle und Ziel (remote)
- Optionen: Skip wenn Ziel existiert / Suffix-Strategie
- Plan erstellt `mv`-Operationen
- Ausführen führt Script aus

### Tab 5: Umbenennen

- **Schnell-Suche im Analyse-Index**:
  - alten Titel/Teilstring eingeben (z.B. `Fast and Furios`)
  - Treffer auswählen → Ordnerpfad wird automatisch gesetzt
  - Gruppe wird geladen und (best-effort) aus NFO **vorausgefüllt** (Titel/Jahr/IMDb), bleibt editierbar
- Ordner manuell wählen ist weiterhin möglich
- Gruppen werden erkannt (Video + Sidecars)
- Modus "Aus NFO":
  - liest `*.nfo` (XML) und erzeugt `Title (Year) [imdbid-tt1234567]`
- Modus "Manuell": Titel/Jahr/IMDb (nur für **1 Gruppe** gleichzeitig, zur Sicherheit)
- Optional: Ordner umbenennen (nur wenn Ordnername == altes Video-Stem)

### Tab 6: Hardlinks / Bibliotheken

- Master-Movies Root (z.B. `/media/_MASTER/Movies`)
- Library Root (z.B. `/media/_LIBRARIES/Movies`)
- Filme auswählen (aus Analyse-DB, Filter)
- Kategorien auswählen (Fixliste aus Spec)
- Sidecar-Policy:
  - nur Video hardlinken (default)
  - oder Sidecars hardlinken
  - oder Sidecars kopieren
- Plan erstellt `mkdir` + `ln`/`cp` Operationen
- Script prüft pro Link:
  - Quelle/Ziel in erlaubten Roots
  - Ziel existiert nicht (standard)
  - Quelle/Ziel auf gleichem Dateisystem (Device-ID via `stat`)

---

## Sicherheit / Guardrails (Wichtig)

- **Dry-Run**: Standard `DRY_RUN=1` → Script schreibt nur vor, führt aber nicht aus.
- **No Overwrite**: existierende Ziele werden standardmäßig als Fehler behandelt.
- **Root-Sandbox**: jede Operation muss innerhalb der konfigurierten Roots liegen.
- **Mass-Confirm**: ab `N` Operationen muss zusätzlich "JA" getippt werden.
- **Journal**: jede Ausführung wird als JSONL in `.../JellyfinOrganizer/journal.jsonl` gespeichert.

---

## Tests

```bash
pytest -q
```
