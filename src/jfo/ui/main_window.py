from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from jfo.infra.settings import load_settings, save_settings, AppSettings
from jfo.infra.ssh_client import SshManager
from jfo.infra.sqlite_index import init_db

from jfo.ui.tabs.tab_connection import ConnectionTab
from jfo.ui.tabs.tab_analysis import AnalysisTab
from jfo.ui.tabs.tab_create_dirs import CreateDirsTab
from jfo.ui.tabs.tab_move import MoveTab
from jfo.ui.tabs.tab_rename import RenameTab
from jfo.ui.tabs.tab_swap import SwapTab
from jfo.ui.tabs.tab_hardlinks import HardlinksTab
from jfo.ui.tabs.tab_history import HistoryTab


class MainWindow(ttk.Frame):
    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.master = master
        self.settings: AppSettings = load_settings()
        self.ssh = SshManager()

        init_db()

        # Notebook
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill=tk.BOTH, expand=True)

        self.tab_connection = ConnectionTab(self.nb, app=self)
        self.tab_analysis = AnalysisTab(self.nb, app=self)
        self.tab_create = CreateDirsTab(self.nb, app=self)
        self.tab_move = MoveTab(self.nb, app=self)
        self.tab_rename = RenameTab(self.nb, app=self)
        self.tab_swap = SwapTab(self.nb, app=self)
        self.tab_hard = HardlinksTab(self.nb, app=self)
        self.tab_history = HistoryTab(self.nb, app=self)

        self.nb.add(self.tab_connection, text="Main (Verbindung)")
        self.nb.add(self.tab_analysis, text="Scan / Index")
        self.nb.add(self.tab_create, text="Erstellen")
        self.nb.add(self.tab_move, text="Verschieben")
        self.nb.add(self.tab_rename, text="Umbenennen")
        self.nb.add(self.tab_swap, text="Tauschen")
        self.nb.add(self.tab_hard, text="Hardlinks / Libraries")
        self.nb.add(self.tab_history, text="History / Undo")

        # Save settings on close
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self) -> None:
        try:
            save_settings(self.settings)
        finally:
            try:
                self.ssh.disconnect()
            finally:
                self.master.destroy()
