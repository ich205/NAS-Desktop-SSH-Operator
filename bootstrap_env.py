# -*- coding: utf-8 -*-
"""Jellyfin Organizer Bootstrap (stdlib-only)

Ziel:
  - One-Click Launcher:
    1) erstellt eine lokale Virtualenv (.venv) neben diesem Repo,
    2) installiert/updated die App + Dependencies in diese venv,
    3) startet danach die GUI aus der venv.

Warum so?
  - Du bekommst eine startbare Datei ohne IDE.
  - Erste Ausführung richtet alles ein; danach startet es direkt.

Hinweis:
  - Das ist bewusst nur Standardbibliothek (keine externen Imports),
    damit der Launcher auch ohne installierte Requirements funktioniert.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
import venv


class BootstrapError(RuntimeError):
    pass


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _state_path(venv_dir: Path) -> Path:
    return venv_dir / ".jfo_bootstrap_state.json"


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_pythonw(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "pythonw.exe"
    # On POSIX there's no pythonw; normal python is fine.
    return _venv_python(venv_dir)


def ensure_venv(venv_dir: Path, logger) -> None:
    py = _venv_python(venv_dir)
    if py.exists():
        logger(f"✓ Virtualenv vorhanden: {venv_dir}")
        return

    logger(f"Erstelle Virtualenv: {venv_dir}")
    try:
        builder = venv.EnvBuilder(with_pip=True)
        builder.create(venv_dir)
    except Exception as exc:  # noqa: BLE001
        raise BootstrapError(f"Virtualenv konnte nicht erstellt werden: {exc}") from exc

    if not py.exists():
        raise BootstrapError("Virtualenv erstellt, aber python.exe/python nicht gefunden.")


def _run(
    cmd: list[str],
    cwd: Path,
    logger,
    cancel_event: threading.Event | None = None,
    hide_window: bool = False,
) -> int:
    """Run a subprocess and stream merged stdout/stderr to logger."""
    logger(f"$ {' '.join(cmd)}")
    creationflags = 0
    if hide_window and os.name == "nt":
        # Prevent spawning extra console windows when running from .pyw
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"Programm nicht gefunden: {cmd[0]}") from exc

    assert proc.stdout is not None
    for line in proc.stdout:
        if cancel_event is not None and cancel_event.is_set():
            try:
                proc.terminate()
            except Exception:
                pass
            logger("Abbruch angefordert. Beende Prozess …")
            break
        logger(line.rstrip("\n"))

    try:
        return proc.wait(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return proc.returncode if proc.returncode is not None else 1


def _load_state(venv_dir: Path) -> dict:
    sp = _state_path(venv_dir)
    if not sp.exists():
        return {}
    try:
        return json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(venv_dir: Path, state: dict) -> None:
    sp = _state_path(venv_dir)
    sp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _deps_ok(venv_dir: Path, root_dir: Path, logger, cancel_event=None, hide_window=False) -> bool:
    """Quick import check inside the venv.

    We intentionally import the JFO sources from the *local* "src" folder
    (via PYTHONPATH/sys.path) so that the app code always matches the files
    next to this launcher, even if an older version of the package was
    previously installed in the venv.
    """

    py = _venv_python(venv_dir)
    src_dir = root_dir / "src"
    code = (
        "import sys, pathlib; "
        f"sys.path.insert(0, r'{src_dir.as_posix()}'); "
        "import paramiko, cryptography, platformdirs; "
        "import jfo; "
        "print('imports-ok')"
    )
    rc = _run(
        [str(py), "-c", code],
        cwd=root_dir,
        logger=logger,
        cancel_event=cancel_event,
        hide_window=hide_window,
    )
    return rc == 0


def ensure_installed(root_dir: Path, venv_dir: Path, logger, cancel_event=None, hide_window=False) -> None:
    """Install/update dependencies into the venv (idempotent).

    Design choice:
      - We DO NOT rely on "pip install ." for the app code.
      - Instead we install runtime dependencies (requirements.txt)...
      - ...and run the app directly from the local "src" folder (PYTHONPATH).

    Benefit for your workflow:
      - When you unzip a new version over the old folder, the launcher will
        immediately run the updated code (no stale site-packages problem).
    """

    req = root_dir / "requirements.txt"
    if not req.exists():
        raise BootstrapError(f"requirements.txt nicht gefunden in {root_dir}")

    state = _load_state(venv_dir)
    current_hash = _hash_file(req)
    need_install = state.get("requirements_hash") != current_hash

    if not need_install:
        logger("requirements.txt unverändert – prüfe Imports …")
        if _deps_ok(venv_dir, root_dir, logger, cancel_event=cancel_event, hide_window=hide_window):
            logger("✓ Requirements & App sind verfügbar.")
            return
        logger("✗ Import-Check fehlgeschlagen – führe Reparatur/Installation aus …")
        need_install = True

    py = _venv_python(venv_dir)

    logger("Stelle sicher, dass pip verfügbar ist …")
    _run(
        [str(py), "-m", "ensurepip", "--upgrade"],
        cwd=root_dir,
        logger=logger,
        cancel_event=cancel_event,
        hide_window=hide_window,
    )

    logger("Aktualisiere pip …")
    _run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=root_dir,
        logger=logger,
        cancel_event=cancel_event,
        hide_window=hide_window,
    )

    logger("Installiere/aktualisiere Dependencies …")
    rc = _run(
        [str(py), "-m", "pip", "install", "-r", str(req)],
        cwd=root_dir,
        logger=logger,
        cancel_event=cancel_event,
        hide_window=hide_window,
    )
    if rc != 0:
        raise BootstrapError("pip install -r requirements.txt fehlgeschlagen (siehe Log).")

    if not _deps_ok(venv_dir, root_dir, logger, cancel_event=cancel_event, hide_window=hide_window):
        raise BootstrapError("Installation abgeschlossen, aber Import-Check schlägt weiterhin fehl.")

    _save_state(venv_dir, {"requirements_hash": current_hash})
    logger("✓ Installation abgeschlossen.")


def run_app(root_dir: Path, venv_dir: Path, logger, hide_window: bool = False) -> int:
    """Launch the main GUI using the venv's interpreter.

    We run directly from the local sources in "src" by setting PYTHONPATH.
    """
    pyw = _venv_pythonw(venv_dir)
    py = pyw if pyw.exists() else _venv_python(venv_dir)

    logger("Starte Jellyfin Organizer …")
    try:
        env = os.environ.copy()
        src_dir = str((root_dir / "src").resolve())
        old_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = src_dir + (os.pathsep + old_pp if old_pp else "")
        subprocess.Popen(
            [str(py), "-m", "jfo"],
            cwd=str(root_dir),
            env=env,
            close_fds=(os.name != "nt"),
            creationflags=(0x08000000 if (hide_window and os.name == "nt") else 0),
        )
    except Exception as exc:  # noqa: BLE001
        raise BootstrapError(f"Programmstart fehlgeschlagen: {exc}") from exc
    return 0
