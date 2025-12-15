from __future__ import annotations

import threading
from dataclasses import dataclass
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from jfo.core.quoting import bash_quote
from jfo.core.validators import Sandbox, SandboxViolation
from jfo.infra.sqlite_index import (
    upsert_paths,
    db_path,
    distinct_roots,
    distinct_dirs_for_root,
    files_in_dir_for_root,
    search_files_for_root,
    export_root_to_csv,
    export_root_to_jsonl,
)
from jfo.ui.widgets import LabeledEntry, LabeledCombobox, ReadonlyText, LogText, PlanTable
from jfo.ui.dialogs import pick_remote_directory


@dataclass
class FileHitVM:
    path: str
    dir: str
    name: str
    ext: str
    root: str
    selected: bool = True


class AnalysisTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._script: str = ""
        self._scan_root: str = ""
        self._exts: list[str] = []
        self._active_root: str = ""
        self._dir_cache: list[str] = []
        self._file_hits: list[FileHitVM] = []

        # Inputs
        in_frm = ttk.LabelFrame(self, text="Festplatte scannen / Index aufbauen")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        # Remote root + remote browser (user-friendly)
        root_row = ttk.Frame(in_frm)
        root_row.pack(fill=tk.X, pady=2)
        self.root_entry = LabeledEntry(root_row, "Remote Root:", width=80)
        self.root_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(root_row, text="Browse…", command=self._browse_remote_root).pack(side=tk.LEFT, padx=6)
        ttk.Button(root_row, text="Test", command=self._test_remote_root).pack(side=tk.LEFT)

        ttk.Label(
            in_frm,
            text="Tipp: nutze 'Browse…' um Volumes/Ordner per Klick auszuwählen (kein Tippen nötig).",
        ).pack(anchor=tk.W, pady=(0, 2))

        self.ext_entry = LabeledEntry(in_frm, "Extensions (comma):", width=80)
        self.ext_entry.pack(fill=tk.X, pady=2)
        self.ext_entry.set("mkv,mp4,nfo,jpg,png,srt,ass")

        opt_scan = ttk.Frame(in_frm)
        opt_scan.pack(fill=tk.X, pady=2)
        self.all_files = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_scan, text="Alle Dateien (ignoriert Extension-Filter)", variable=self.all_files).pack(side=tk.LEFT)

        btn_frm = ttk.Frame(in_frm)
        btn_frm.pack(fill=tk.X, pady=(6, 2))
        ttk.Button(btn_frm, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT)
        ttk.Button(btn_frm, text="Ausführen", command=self._execute).pack(side=tk.LEFT, padx=6)

        ttk.Separator(self).pack(fill=tk.X, padx=10, pady=(0, 10))

        # Index browser / search
        browse_frm = ttk.LabelFrame(self, text="Index Browser (aus Analyse-DB)")
        browse_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        top = ttk.Frame(browse_frm)
        top.pack(fill=tk.X, pady=2)
        self.root_combo = LabeledCombobox(top, "Aktiver Root:", values=[], width=60)
        self.root_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(top, text="Roots aktualisieren", command=self._refresh_roots).pack(side=tk.LEFT, padx=6)

        exp_frm = ttk.Frame(browse_frm)
        exp_frm.pack(fill=tk.X, pady=2)
        ttk.Button(exp_frm, text="Export CSV…", command=self._export_csv).pack(side=tk.LEFT)
        ttk.Button(exp_frm, text="Export JSONL…", command=self._export_jsonl).pack(side=tk.LEFT, padx=6)

        # Dir + file panes
        panes = ttk.Panedwindow(browse_frm, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, pady=(6, 2))

        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=3)

        # Directories
        dir_top = ttk.Frame(left)
        dir_top.pack(fill=tk.X)
        ttk.Label(dir_top, text="Ordnerfilter (Prefix):").pack(side=tk.LEFT)
        self.dir_prefix = tk.StringVar(value="")
        de = ttk.Entry(dir_top, textvariable=self.dir_prefix)
        de.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(dir_top, text="Ordner laden", command=self._load_dirs).pack(side=tk.LEFT)

        self.dir_list = tk.Listbox(left, height=12)
        self.dir_list.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.dir_list.bind("<<ListboxSelect>>", lambda _e: self._load_files_for_selected_dir())

        # Files / search
        search_top = ttk.Frame(right)
        search_top.pack(fill=tk.X)
        ttk.Label(search_top, text="Suche (Name/Path):").pack(side=tk.LEFT)
        self.search_term = tk.StringVar(value="")
        se = ttk.Entry(search_top, textvariable=self.search_term)
        se.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        self.filter_video_only = tk.BooleanVar(value=True)
        ttk.Checkbutton(search_top, text="nur Videos", variable=self.filter_video_only).pack(side=tk.LEFT)
        ttk.Button(search_top, text="Suchen", command=self._search_files).pack(side=tk.LEFT, padx=6)

        self.files_table = PlanTable(right, columns=["Name", "Ext", "Dir", "Path", "Root"], on_toggle=None)
        self.files_table.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # Plan/Preview
        prev_frm = ttk.LabelFrame(self, text="Scan-Plan (Preview)")
        prev_frm.pack(fill=tk.BOTH, expand=False, padx=10, pady=(0, 10))
        self.table = PlanTable(prev_frm, columns=["Action", "Details"])
        self.table.pack(fill=tk.BOTH, expand=True)

        # Output
        out_frm = ttk.LabelFrame(self, text="Generiertes Scan-Script")
        out_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.out = ReadonlyText(out_frm, height=10)
        self.out.pack(fill=tk.BOTH, expand=True)

        log_frm = ttk.LabelFrame(self, text="Log")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = LogText(log_frm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        # Populate root dropdown from existing index (if any)
        self._refresh_roots()

        # Prefill scan root with the first allowed root (if configured)
        if not self.root_entry.get() and self.app.settings.allowed_roots:
            self.root_entry.set(self.app.settings.allowed_roots[0])

    def _allowed_roots_updated(self, roots: list[str]) -> None:
        """Sync allowed roots into settings + (if available) the Main tab widget."""

        self.app.settings.allowed_roots = list(roots)
        # Update the visible text widget in Main tab so user immediately sees the change.
        try:
            txt = self.app.tab_connection.allowed_roots
            txt.delete("1.0", tk.END)
            txt.insert("1.0", "\n".join(roots))
        except Exception:
            pass
        self.log.append_line(f"[local] Allowed Roots updated: {', '.join(roots)}")

    def _browse_remote_root(self) -> None:
        initial = self.root_entry.get() or (self.app.settings.allowed_roots[0] if self.app.settings.allowed_roots else "/")
        chosen = pick_remote_directory(
            self,
            ssh=self.app.ssh,
            allowed_roots=self.app.settings.allowed_roots,
            initial_path=initial,
            title="Remote Root auswählen",
            allow_set_allowed_root=True,
            on_allowed_roots_updated=self._allowed_roots_updated,
        )
        if chosen:
            self.root_entry.set(chosen)

    def _test_remote_root(self) -> None:
        root = self.root_entry.get()
        if not root:
            messagebox.showinfo("Test", "Bitte erst einen Remote Root setzen (oder Browse…).", parent=self)
            return
        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return
        try:
            res = self.app.ssh.exec_command(f"test -d -- {bash_quote(root)}")
            if res.exit_status == 0:
                messagebox.showinfo("Test", f"OK: Ordner existiert auf dem NAS:\n{root}", parent=self)
            else:
                messagebox.showerror("Test", f"Nicht gefunden oder kein Ordner:\n{root}\n\n{res.stderr.strip()}", parent=self)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Test", str(exc), parent=self)

    def _build_plan(self) -> None:
        root = self.root_entry.get()
        exts = [x.strip().lstrip(".") for x in self.ext_entry.get().split(",") if x.strip()]
        if not root:
            messagebox.showwarning("Eingabe", "Remote Root fehlt.", parent=self)
            return

        # Client-side sandbox check (preventive)
        try:
            Sandbox(self.app.settings.allowed_roots).assert_path_allowed(root)
        except SandboxViolation as exc:
            messagebox.showerror(
                "Sandbox",
                str(exc)
                + "\n\nTipp: Klicke auf 'Browse…' und wähle den Ordner per Klick."
                + "\nFalls noch keine Allowed Roots gesetzt sind: im Browse-Dialog Haken bei 'Als Allowed Root hinzufügen' setzen.",
                parent=self,
            )
            return

        self._scan_root = root
        self._exts = exts

        all_files = bool(self.all_files.get())
        find_parts = []
        for e in exts:
            find_parts.append(f"-iname '*.{e}'")

        # Build the find expression.
        # IMPORTANT: use quoted parentheses '(' and ')' to avoid shell parsing issues.
        # (We previously emitted '\\(' which can be interpreted by bash as an unescaped '(' token.)
        expr = " -o ".join(find_parts) if find_parts else "-true"

        script = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "IFS=$'\\n\\t'",
            "set -f",
            "",
            f"ROOT={bash_quote(root)}",
            f"echo '[jfo] scan root='\"$ROOT\"",
            "echo 'JFO_SCAN_BEGIN'",
            (
                f"find \"$ROOT\" -type f -print"
                if all_files
                else f"find \"$ROOT\" -type f '(' {expr} ')' -print"
            ),
            "echo 'JFO_SCAN_END'",
        ]
        self._script = "\n".join(script) + "\n"

        self.table.bind_operations(
            [object()],
            row_getter=lambda _: ("SCAN", f"{root} | exts={','.join(exts)}"),
        )
        self.out.set_text(self._script)
        self.log.append_line(f"[local] Plan ready. (DB: {db_path()})")

    def _execute(self) -> None:
        if not self._script:
            self._build_plan()
            if not self._script:
                return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        self.log.append_line("[local] executing scan...")
        t = threading.Thread(target=self._worker_exec, daemon=True)
        t.start()

    def _worker_exec(self) -> None:
        paths: list[str] = []
        in_list = {"active": False}

        def on_out(line: str) -> None:
            if line.strip() == "JFO_SCAN_BEGIN":
                in_list["active"] = True
                return
            if line.strip() == "JFO_SCAN_END":
                in_list["active"] = False
                return
            if in_list["active"]:
                p = line.strip("\r")
                if p:
                    paths.append(p)
                    # avoid flooding UI
                    if len(paths) % 500 == 0:
                        self.after(0, lambda n=len(paths): self.log.append_line(f"[scan] {n} files..."))
            else:
                self.after(0, lambda l=line: self.log.append_line(l))

        def on_err(line: str) -> None:
            self.after(0, lambda l=line: self.log.append_line("STDERR: " + l))

        try:
            exit_code = self.app.ssh.exec_bash_script_streaming(self._script, on_stdout=on_out, on_stderr=on_err)
            if exit_code != 0:
                self.after(0, lambda: self.log.append_line(f"[local] scan exit={exit_code}"))
                return

            # Persist
            count = upsert_paths(paths, root=self._scan_root)
            self.after(0, lambda: self._after_scan_ok(count))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] scan ERROR: {exc}"))

    def _after_scan_ok(self, count: int) -> None:
        self.log.append_line(f"[local] scan OK. indexed {count} paths (root marker='{self._scan_root}')")
        self._refresh_roots(select=self._scan_root)

    def _refresh_roots(self, select: str | None = None) -> None:
        roots = distinct_roots(limit=500)
        self.root_combo.combo["values"] = roots
        if select:
            self.root_combo.set(select)
            self._active_root = select
        elif roots and not self.root_combo.get():
            self.root_combo.set(roots[0])
            self._active_root = roots[0]
        elif self.root_combo.get():
            self._active_root = self.root_combo.get()

    def _ensure_active_root(self) -> str | None:
        root = self.root_combo.get()
        if root:
            self._active_root = root
            return root
        self._refresh_roots()
        root = self.root_combo.get()
        if not root:
            messagebox.showinfo("Analyse", "Keine Roots im Index. Bitte zuerst scannen.", parent=self)
            return None
        self._active_root = root
        return root

    def _load_dirs(self) -> None:
        root = self._ensure_active_root()
        if not root:
            return
        prefix = self.dir_prefix.get().strip()
        dirs = distinct_dirs_for_root(root, prefix=prefix, limit=2000)
        self._dir_cache = dirs
        self.dir_list.delete(0, tk.END)
        for d in dirs:
            self.dir_list.insert(tk.END, d)
        self.log.append_line(f"[local] loaded {len(dirs)} dirs for root '{root}'")

    def _load_files_for_selected_dir(self) -> None:
        root = self._ensure_active_root()
        if not root:
            return
        sel = self.dir_list.curselection()
        if not sel:
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._dir_cache):
            return
        d = self._dir_cache[idx]
        rows = files_in_dir_for_root(root, d, exts=None, limit=5000)
        hits = [FileHitVM(path=r[0], dir=r[1], name=r[2], ext=r[3], root=root) for r in rows]
        self._file_hits = hits
        self.files_table.bind_operations(
            hits,
            row_getter=lambda h: (h.name, h.ext, h.dir, h.path, h.root),
        )

    def _search_files(self) -> None:
        root = self._ensure_active_root()
        if not root:
            return
        term = self.search_term.get().strip()
        if not term:
            messagebox.showwarning("Suche", "Bitte Suchbegriff eingeben.", parent=self)
            return

        exts = None
        if self.filter_video_only.get():
            exts = self.app.settings.video_exts

        rows = search_files_for_root(root, term, exts=exts, limit=500)
        hits = [FileHitVM(path=r[0], dir=r[1], name=r[2], ext=r[3], root=root) for r in rows]
        self._file_hits = hits
        self.files_table.bind_operations(
            hits,
            row_getter=lambda h: (h.name, h.ext, h.dir, h.path, h.root),
        )
        self.log.append_line(f"[local] search '{term}' -> {len(hits)} hits (root='{root}')")

    def _export_csv(self) -> None:
        root = self._ensure_active_root()
        if not root:
            return
        path = filedialog.asksaveasfilename(
            title="Export CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*")],
        )
        if not path:
            return
        n = export_root_to_csv(root, path)
        self.log.append_line(f"[local] exported {n} rows to CSV: {path}")

    def _export_jsonl(self) -> None:
        root = self._ensure_active_root()
        if not root:
            return
        path = filedialog.asksaveasfilename(
            title="Export JSONL",
            defaultextension=".jsonl",
            filetypes=[("JSONL", "*.jsonl"), ("All files", "*")],
        )
        if not path:
            return
        n = export_root_to_jsonl(root, path)
        self.log.append_line(f"[local] exported {n} rows to JSONL: {path}")
