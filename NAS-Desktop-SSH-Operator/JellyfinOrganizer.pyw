# -*- coding: utf-8 -*-
"""Jellyfin Organizer One-Click Launcher (GUI)

Windows:
  - Doppelklick auf `Start_JellyfinOrganizer.bat` oder `JellyfinOrganizer.pyw`

macOS:
  - `Start_JellyfinOrganizer.command`

Linux/macOS Terminal:
  - `./start.sh`
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext
from pathlib import Path

import bootstrap_env


class LauncherApp(tk.Frame):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.master = master
        self.cancel_event = threading.Event()

        self.master.title("Jellyfin Organizer – Setup & Start")
        self.master.geometry("840x560")

        header = tk.Frame(self)
        header.pack(fill=tk.X, padx=10, pady=(10, 5))

        self.status_var = tk.StringVar(value="Starte …")
        tk.Label(header, textvariable=self.status_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.btn_cancel = tk.Button(header, text="Abbrechen", command=self.on_cancel)
        self.btn_cancel.pack(side=tk.RIGHT)

        self.text = scrolledtext.ScrolledText(self, wrap=tk.WORD, height=25)
        self.text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.text.configure(state="disabled")

        footer = tk.Frame(self)
        footer.pack(fill=tk.X, padx=10, pady=(5, 10))
        tk.Label(
            footer,
            text="Dieser Launcher erstellt bei Bedarf eine lokale .venv und installiert Abhängigkeiten. Danach startet die GUI.",
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.pack(fill=tk.BOTH, expand=True)

        # Start bootstrap automatically.
        self.worker = threading.Thread(target=self.bootstrap_and_run, daemon=True)
        self.worker.start()

    def log(self, msg: str) -> None:
        self.text.configure(state="normal")
        self.text.insert(tk.END, msg + "\n")
        self.text.see(tk.END)
        self.text.configure(state="disabled")

    def set_status(self, msg: str) -> None:
        self.status_var.set(msg)

    def on_cancel(self) -> None:
        self.cancel_event.set()
        self.set_status("Abbruch angefordert …")
        self.btn_cancel.configure(state="disabled")

    def bootstrap_and_run(self) -> None:
        try:
            root_dir = Path(__file__).resolve().parent
            venv_dir = root_dir / ".venv"

            def logger(line: str) -> None:
                self.master.after(0, self.log, line)

            self.master.after(0, self.set_status, "Prüfe Python-Version …")
            if sys.version_info < (3, 10):
                raise bootstrap_env.BootstrapError("Python >= 3.10 wird benötigt.")

            self.master.after(0, self.set_status, "Virtualenv prüfen/erstellen …")
            bootstrap_env.ensure_venv(venv_dir, logger=logger)

            if self.cancel_event.is_set():
                raise bootstrap_env.BootstrapError("Abgebrochen.")

            self.master.after(0, self.set_status, "Dependencies prüfen/installieren …")
            bootstrap_env.ensure_installed(
                root_dir=root_dir,
                venv_dir=venv_dir,
                logger=logger,
                cancel_event=self.cancel_event,
                hide_window=True,
            )

            if self.cancel_event.is_set():
                raise bootstrap_env.BootstrapError("Abgebrochen.")

            self.master.after(0, self.set_status, "Starte App …")
            bootstrap_env.run_app(root_dir=root_dir, venv_dir=venv_dir, logger=logger, hide_window=True)

            # Exit launcher after starting the app.
            self.master.after(600, self.master.destroy)
        except bootstrap_env.BootstrapError as exc:
            self.master.after(0, self.set_status, f"Fehler: {exc}")
            self.master.after(0, messagebox.showerror, "Start fehlgeschlagen", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.master.after(0, self.set_status, f"Unerwarteter Fehler: {exc}")
            self.master.after(0, messagebox.showerror, "Unerwarteter Fehler", f"{type(exc).__name__}: {exc}")


def main() -> int:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
