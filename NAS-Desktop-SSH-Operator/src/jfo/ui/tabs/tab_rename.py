from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from jfo.core.media_grouping import (
    MediaGroup,
    group_media_files,
    FOLDER_LEVEL_SIDECAR_NAMES,
    FOLDER_LEVEL_NFO_NAMES,
)
from jfo.core.nfo import parse_nfo
from jfo.core.quoting import bash_quote
from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan
from jfo.core.history import ops_to_journal_dicts
from jfo.core.scriptgen import ScriptOptions, generate_bash_script
from jfo.core.validators import Sandbox, SandboxViolation
from jfo.infra.journal import append_journal
from jfo.infra.index_update import apply_plan_to_index
from jfo.infra.sqlite_index import (
    distinct_roots,
    search_files_for_root,
    search_files_any_root,
    files_in_dir,
    files_in_dir_for_root,
    files_under_dir_recursive,
    files_under_dir_recursive_for_root,
)
from jfo.ui.dialogs import ask_text_confirm, pick_remote_directory, ask_execute_with_dry_run
from jfo.ui.widgets import LabeledEntry, LabeledCombobox, ReadonlyText, LogText, PlanTable


_IMDB_RE = re.compile(r"tt\d{3,10}")


def _sanitize_title(value: str) -> str:
    value = value.strip()
    # Replace problematic path separators
    value = value.replace("/", "-").replace("\\\\", "-")
    # Control characters
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f")
    # Collapse whitespace
    value = re.sub(r"\s+", " ", value)
    # Remove trailing dots/spaces (Windows-compat)
    value = value.rstrip(" .")
    return value


@dataclass
class GroupVM:
    group: MediaGroup
    selected: bool = True
    proposed: str = ""
    warning: str = ""

    def video_path(self) -> str:
        return self.group.video.path if self.group.video else ""

    def nfo_path(self) -> str:
        return self.group.nfo.path if self.group.nfo else ""


@dataclass
class IndexHitVM:
    path: str
    dir: str
    name: str
    ext: str
    root: str

    def display(self) -> str:
        # Compact but informative for a listbox
        return f"{self.name}  |  {self.dir}"


class RenameTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._groups: list[GroupVM] = []
        self._plan: Plan | None = None
        self._script: str = ""

        in_frm = ttk.LabelFrame(self, text="Umbenennen (Jellyfin/Kodi-kompatibel)")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        # Quick search in analysis index
        qs_frm = ttk.LabelFrame(in_frm, text="Schnell-Suche im Analyse-Index (alter Titel)")
        qs_frm.pack(fill=tk.X, pady=(0, 6))

        top = ttk.Frame(qs_frm)
        top.pack(fill=tk.X, pady=2)
        self.root_filter = LabeledCombobox(top, "Analyse Root (optional):", values=[""], width=50)
        self.root_filter.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(top, text="Aktualisieren", command=self._refresh_roots).pack(side=tk.LEFT, padx=6)

        sline = ttk.Frame(qs_frm)
        sline.pack(fill=tk.X, pady=2)
        self.search_term = tk.StringVar(value="")
        ttk.Label(sline, text="Alter Titel / Suchbegriff:").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(sline, textvariable=self.search_term)
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(sline, text="Suchen", command=self._search_index).pack(side=tk.LEFT)
        ttk.Button(sline, text="Treffer übernehmen", command=self._use_selected_hit).pack(side=tk.LEFT, padx=6)

        # Search results (bigger + scrollable)
        hit_frm = ttk.Frame(qs_frm)
        hit_frm.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.hit_list = tk.Listbox(hit_frm, height=10)
        hit_ysb = ttk.Scrollbar(hit_frm, orient="vertical", command=self.hit_list.yview)
        self.hit_list.configure(yscrollcommand=hit_ysb.set)
        self.hit_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        hit_ysb.pack(side=tk.RIGHT, fill=tk.Y)

        self._hits: list[IndexHitVM] = []

        # Remote folder + browse button (prevents path typos)
        folder_row = ttk.Frame(in_frm)
        folder_row.pack(fill=tk.X, pady=2)
        self.folder_entry = LabeledEntry(folder_row, "Ordner (remote):", width=80)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(folder_row, text="Browse…", command=self._browse_folder).pack(side=tk.LEFT, padx=6)

        rec_frm = ttk.Frame(in_frm)
        rec_frm.pack(fill=tk.X, pady=2)
        self.recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec_frm, text="Rekursiv (Unterordner mitnehmen)", variable=self.recursive).pack(side=tk.LEFT)

        self.rename_folder = tk.BooleanVar(value=True)
        ttk.Checkbutton(rec_frm, text="Ordner umbenennen (wenn passend)", variable=self.rename_folder).pack(side=tk.LEFT, padx=10)

        mode_frm = ttk.Frame(in_frm)
        mode_frm.pack(fill=tk.X, pady=2)
        self.mode = tk.StringVar(value="nfo")
        ttk.Label(mode_frm, text="Modus:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frm, text="Aus NFO", variable=self.mode, value="nfo").pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(mode_frm, text="Manuell", variable=self.mode, value="manual").pack(side=tk.LEFT, padx=6)

        man_frm = ttk.Frame(in_frm)
        man_frm.pack(fill=tk.X, pady=2)
        self.manual_title = LabeledEntry(man_frm, "Titel:", width=40)
        self.manual_year = LabeledEntry(man_frm, "Jahr:", width=8)
        self.manual_imdb = LabeledEntry(man_frm, "IMDb:", width=14)
        self.manual_title.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)
        self.manual_year.pack(side=tk.LEFT, padx=(0, 10))
        self.manual_imdb.pack(side=tk.LEFT)

        opt_frm = ttk.Frame(in_frm)
        opt_frm.pack(fill=tk.X, pady=(6, 2))
        self.dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        ttk.Checkbutton(
            opt_frm,
            text="Testlauf (ändert nichts)",
            variable=self.dry_run,
            command=self._dry_run_changed,
        ).pack(side=tk.LEFT)
        ttk.Button(opt_frm, text="Vorausfüllen aus NFO", command=self._prefill_from_nfo).pack(side=tk.LEFT, padx=6)
        ttk.Button(opt_frm, text="Gruppen laden", command=self._load_groups).pack(side=tk.LEFT, padx=6)
        ttk.Button(opt_frm, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT, padx=6)
        self.exec_btn = ttk.Button(opt_frm, text="Ausführen", command=self._execute)
        self.exec_btn.pack(side=tk.LEFT, padx=6)

        # Group list
        grp_frm = ttk.LabelFrame(self, text="Datei-Gruppen (Doppelklick toggelt Sel)")
        # Keep this more compact; details are visible in the Plan table.
        grp_frm.pack(fill=tk.X, expand=False, padx=10, pady=(0, 10))
        self.group_table = PlanTable(grp_frm, columns=["Video", "NFO", "Files", "Proposed", "Warn"], on_toggle=None)
        self.group_table.tree.configure(height=6)
        self.group_table.pack(fill=tk.BOTH, expand=True)

        # Plan list
        plan_frm = ttk.LabelFrame(self, text="Plan (Alt → Neu) (Doppelklick toggelt Sel)")
        plan_frm.pack(fill=tk.X, expand=False, padx=10, pady=(0, 10))
        self.plan_table = PlanTable(plan_frm, columns=["Type", "Source", "Dest", "Warn"], on_toggle=self._regen_script)
        self.plan_table.tree.configure(height=8)
        self.plan_table.pack(fill=tk.BOTH, expand=True)

        out_frm = ttk.LabelFrame(self, text="Generiertes Script")
        out_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.out = ReadonlyText(out_frm, height=10)
        self.out.pack(fill=tk.BOTH, expand=True)

        log_frm = ttk.LabelFrame(self, text="Log")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = LogText(log_frm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        self._refresh_roots()

        # Keep the execute button text in sync with the Dry-Run toggle.
        self._update_execute_label()

        # Prefill folder with first allowed root (if configured)
        if not self.folder_entry.get() and self.app.settings.allowed_roots:
            self.folder_entry.set(self.app.settings.allowed_roots[0])

    def _allowed_roots_updated(self, roots: list[str]) -> None:
        self.app.settings.allowed_roots = list(roots)
        try:
            txt = self.app.tab_connection.allowed_roots
            txt.delete("1.0", tk.END)
            txt.insert("1.0", "\n".join(roots))
        except Exception:
            pass
        self.log.append_line(f"[local] Allowed Roots updated: {', '.join(roots)}")

    def _update_execute_label(self) -> None:
        if getattr(self, "exec_btn", None) is None:
            return
        if bool(self.dry_run.get()):
            self.exec_btn.config(text="Testlauf ausführen")
        else:
            self.exec_btn.config(text="Echt ausführen")

    def _dry_run_changed(self) -> None:
        self._update_execute_label()
        self._regen_script()

    def _browse_folder(self) -> None:
        initial = self.folder_entry.get() or (
            self.app.settings.allowed_roots[0] if self.app.settings.allowed_roots else "/"
        )
        chosen = pick_remote_directory(
            self,
            ssh=self.app.ssh,
            allowed_roots=self.app.settings.allowed_roots,
            initial_path=initial,
            title="Remote Ordner auswählen",
            allow_set_allowed_root=True,
            on_allowed_roots_updated=self._allowed_roots_updated,
        )
        if chosen:
            self.folder_entry.set(chosen)

    def _refresh_roots(self) -> None:
        roots = distinct_roots(limit=500)
        # allow empty (= all roots)
        values = [""] + roots
        self.root_filter.combo["values"] = values
        # keep current selection if possible
        if self.root_filter.get() not in values:
            self.root_filter.set("")

    def _current_root_filter(self) -> str | None:
        r = self.root_filter.get().strip()
        return r if r else None

    def _search_index(self) -> None:
        term = self.search_term.get().strip()
        if not term:
            messagebox.showwarning("Suche", "Bitte einen Suchbegriff eingeben.", parent=self)
            return

        root = self._current_root_filter()
        video_exts = self.app.settings.video_exts

        if root:
            rows = search_files_for_root(root, term, exts=video_exts, limit=200)
            hits = [IndexHitVM(path=r[0], dir=r[1], name=r[2], ext=r[3], root=root) for r in rows]
        else:
            rows = search_files_any_root(term, exts=video_exts, limit=200)
            hits = [IndexHitVM(path=r[0], dir=r[1], name=r[2], ext=r[3], root=r[4]) for r in rows]

        self._hits = hits
        self.hit_list.delete(0, tk.END)
        for h in hits:
            self.hit_list.insert(tk.END, f"[{h.root}] {h.name}  ({h.ext})  —  {h.path}")

        self.log.append_line(f"[local] index search '{term}' -> {len(hits)} hits")

    def _use_selected_hit(self) -> None:
        sel = self.hit_list.curselection()
        if not sel:
            messagebox.showinfo("Treffer", "Bitte einen Treffer auswählen.", parent=self)
            return
        idx = int(sel[0])
        if idx < 0 or idx >= len(self._hits):
            return
        h = self._hits[idx]

        # Set filter + folder and load groups
        self.root_filter.set(h.root)
        self.folder_entry.set(h.dir)
        self.recursive.set(False)
        self.mode.set("manual")

        self.log.append_line(f"[local] using hit: {h.path}")
        self._load_groups(focus_video_path=h.path)

    def _prefill_from_nfo(self) -> None:
        """Prefill manual fields from the NFO of the first selected group."""

        if not self._groups:
            messagebox.showinfo("NFO", "Bitte zuerst Gruppen laden.", parent=self)
            return
        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        sel_vm = next((vm for vm in self._groups if vm.selected and vm.group.nfo), None)
        if sel_vm is None:
            messagebox.showinfo("NFO", "Keine selektierte Gruppe mit NFO gefunden.", parent=self)
            return
        nfo_path = sel_vm.group.nfo.path

        self.log.append_line(f"[local] prefill from NFO: {nfo_path}")
        t = threading.Thread(target=self._worker_prefill_from_nfo, args=(nfo_path,), daemon=True)
        t.start()

    def _worker_prefill_from_nfo(self, nfo_path: str) -> None:
        try:
            # NOTE: Some NAS devices restrict the SFTP subsystem (e.g. chroot to home)
            # while the interactive shell can still access absolute /volume paths.
            # Therefore we try SFTP first (fast), but *always* fall back to `cat` on
            # any file-open error.
            xml_text = ""

            sftp = None
            try:
                sftp = self.app.ssh.open_sftp()
            except Exception:
                sftp = None

            if sftp is not None:
                try:
                    with sftp.file(nfo_path, "r") as f:
                        xml_text = f.read().decode("utf-8", errors="replace")
                except Exception:
                    # Fallback below
                    xml_text = ""
                finally:
                    try:
                        sftp.close()
                    except Exception:
                        pass

            if not xml_text:
                # Fallback via shell (works even when SFTP is chrooted)
                res = self.app.ssh.exec_command(f"cat -- {bash_quote(nfo_path)}")
                if res.exit_status != 0:
                    raise RuntimeError((res.stderr or "cat failed").strip())
                xml_text = res.stdout

            info = parse_nfo(xml_text)
            preferred_title = info.original_title or info.title

            def _apply() -> None:
                self.mode.set("manual")
                if preferred_title:
                    self.manual_title.set(_sanitize_title(preferred_title))
                if info.year:
                    self.manual_year.set(str(info.year))
                if info.imdbid:
                    self.manual_imdb.set(info.imdbid)
                self.log.append_line("[local] manual fields prefilled from NFO (editable)")

            self.after(0, _apply)
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] NFO prefill ERROR: {exc}"))

    def _load_groups(self, *, focus_video_path: str | None = None) -> None:
        """Load groups from the analysis index.

        If 'focus_video_path' is provided, only the matching group will be pre-selected and
        the manual fields will be prefilled from NFO if possible.
        """

        folder = self.folder_entry.get()
        if not folder:
            messagebox.showwarning("Eingabe", "Ordner fehlt.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)
        try:
            sandbox.assert_path_allowed(folder)
        except SandboxViolation as exc:
            messagebox.showerror("Sandbox", str(exc), parent=self)
            return

        root_filter = self._current_root_filter()
        recursive = bool(self.recursive.get())

        self.log.append_line(
            f"[local] loading groups from analysis index... (root={root_filter or '*'}, recursive={recursive})"
        )
        t = threading.Thread(
            target=self._worker_load_groups,
            args=(folder, root_filter, recursive, focus_video_path),
            daemon=True,
        )
        t.start()

    def _worker_load_groups(
        self,
        folder: str,
        root_filter: str | None,
        recursive: bool,
        focus_video_path: str | None,
    ) -> None:
        exts = set([e.lower().lstrip(".") for e in (self.app.settings.video_exts + self.app.settings.sidecar_exts)])

        # Pull paths from the local analysis index.
        paths: list[str] = []
        if root_filter:
            if recursive:
                paths = files_under_dir_recursive_for_root(root_filter, folder, exts=exts, limit=50000)
            else:
                rows = files_in_dir_for_root(root_filter, folder, exts=exts, limit=50000)
                paths = [r[0] for r in rows]
        else:
            if recursive:
                paths = files_under_dir_recursive(folder, exts=exts, limit=50000)
            else:
                paths = files_in_dir(folder, exts=exts)

        if not paths:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Analyse",
                    "Keine Dateien im Analyse-Index gefunden. Bitte zuerst Tab 'Scan/Index' ausführen.",
                    parent=self,
                ),
            )
            return

        groups = group_media_files(
            paths,
            video_exts=set(self.app.settings.video_exts),
            sidecar_exts=set(self.app.settings.sidecar_exts),
        )
        vms = [GroupVM(group=g) for g in groups]

        # Safe defaults:
        # - If we have a focus path, select only that.
        # - Otherwise, select only if exactly one group was found (avoid accidental mass manual renames).
        if focus_video_path:
            for vm in vms:
                vm.selected = bool(vm.group.video and vm.group.video.path == focus_video_path)
        else:
            if len(vms) != 1:
                for vm in vms:
                    vm.selected = False

        prefill: tuple[str | None, int | None, str | None] | None = None
        prefill_err: str | None = None

        # Prefill (best-effort) only for focused selection.
        if focus_video_path and self.app.ssh.is_connected():
            sel_vm = next((x for x in vms if x.selected), None)
            if sel_vm and sel_vm.group.nfo:
                try:
                    sftp = None
                    try:
                        sftp = self.app.ssh.open_sftp()
                    except Exception:
                        sftp = None

                    xml_text = ""
                    if sftp is not None:
                        try:
                            with sftp.file(sel_vm.group.nfo.path, "r") as f:
                                xml_text = f.read().decode("utf-8", errors="replace")
                        except Exception:
                            # Fall back to shell below
                            xml_text = ""
                        finally:
                            try:
                                sftp.close()
                            except Exception:
                                pass

                    if not xml_text:
                        res = self.app.ssh.exec_command(f"cat -- {bash_quote(sel_vm.group.nfo.path)}")
                        if res.exit_status != 0:
                            raise RuntimeError((res.stderr or "cat failed").strip())
                        xml_text = res.stdout

                    info = parse_nfo(xml_text)
                    preferred_title = info.original_title or info.title
                    prefill = (preferred_title, info.year, info.imdbid)
                except Exception as exc:  # noqa: BLE001
                    prefill_err = f"NFO prefill error: {exc}"

        def _apply() -> None:
            self._groups = vms
            self.group_table.bind_operations(
                self._groups,
                row_getter=lambda vm: (
                    vm.video_path(),
                    vm.nfo_path(),
                    str(len(vm.group.all_files())),
                    vm.proposed,
                    vm.warning,
                ),
            )
            self.log.append_line(f"[local] loaded {len(self._groups)} groups")

            if prefill_err:
                self.log.append_line(f"[local] {prefill_err}")
            if prefill is not None:
                title, year, imdbid = prefill
                if title:
                    self.manual_title.set(_sanitize_title(title))
                if year:
                    self.manual_year.set(str(year))
                if imdbid:
                    self.manual_imdb.set(imdbid)

        self.after(0, _apply)

    def _build_plan(self) -> None:
        if not self._groups:
            messagebox.showinfo("Gruppen", "Bitte zuerst 'Gruppen laden' ausführen.", parent=self)
            return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        mode = self.mode.get()
        sandbox = Sandbox(self.app.settings.allowed_roots)

        manual_title = _sanitize_title(self.manual_title.get())
        manual_year = self.manual_year.get().strip()
        manual_imdb = self.manual_imdb.get().strip()

        selected_groups = [vm for vm in self._groups if vm.selected and vm.group.video]

        # UX guard: building an empty plan is confusing. Tell the user explicitly.
        if not selected_groups:
            messagebox.showinfo(
                "Plan",
                "Keine Datei-Gruppe selektiert.\n\n"
                "Tipps:\n"
                "• Nutze die Trefferliste und klicke 'Treffer übernehmen' (wählt automatisch die richtige Gruppe).\n"
                "• Oder doppelklicke in der Gruppen-Tabelle auf die gewünschte Zeile (Sel-Spalte).",
                parent=self,
            )
            return

        if mode == "manual":
            # Safety: manual mode should only apply to one group at a time.
            if len(selected_groups) != 1:
                messagebox.showwarning(
                    "Manuell",
                    "Manuell-Modus ist nur für 1 Datei-Gruppe gedacht. Bitte nur eine Gruppe selektieren (Doppelklick), oder 'Aus NFO' verwenden.",
                    parent=self,
                )
                return

            # If user already provided an IMDb-ID, validate early.
            if manual_imdb and not _IMDB_RE.fullmatch(manual_imdb):
                messagebox.showwarning("Manuell", "IMDb-ID ungültig (tt1234567).", parent=self)
                return

        ops: list[Operation] = []
        # directory renames must happen last (otherwise subsequent file paths break)
        dir_ops: list[Operation] = []
        dir_renames_done: set[str] = set()

        # SFTP is much faster than cat per file.
        sftp = None
        try:
            sftp = self.app.ssh.open_sftp()
        except Exception:
            sftp = None

        def _read_nfo_text(nfo_path: str) -> str:
            """Read an .nfo file (prefer SFTP for speed, fall back to cat)."""
            # IMPORTANT: On some NAS devices the SFTP subsystem is restricted (chroot)
            # even though the interactive shell can access absolute /volume paths.
            # Therefore we must *always* fall back to a shell `cat` if SFTP open fails.
            if sftp is not None:
                try:
                    with sftp.file(nfo_path, "r") as f:
                        return f.read().decode("utf-8", errors="replace")
                except Exception:
                    pass

            res = self.app.ssh.exec_command(f"cat -- {bash_quote(nfo_path)}")
            if res.exit_status != 0:
                raise RuntimeError((res.stderr or "cat failed").strip())
            return res.stdout

        def _prompt_imdb_id(title_hint: str | None, year_hint: int | None) -> str | None:
            """Ask user for an IMDb-ID (tt1234567). Returns None on cancel."""
            hint_lines = []
            if title_hint:
                hint_lines.append(f"Titel: {title_hint}")
            if year_hint:
                hint_lines.append(f"Jahr: {year_hint}")
            hint = "\n".join(hint_lines)
            prompt = "Keine IMDb-ID in der NFO gefunden.\n\n" + (hint + "\n\n" if hint else "")
            prompt += "Bitte IMDb-ID eingeben (Format: tt1234567):"

            while True:
                val = simpledialog.askstring("IMDb-ID", prompt, parent=self)
                if val is None:
                    return None
                val = val.strip()
                if _IMDB_RE.fullmatch(val):
                    return val
                messagebox.showwarning("IMDb-ID", "Ungültig. Bitte z.B. tt1234567 eingeben.", parent=self)

        def _prompt_year(title_hint: str | None) -> int | None:
            """Ask user for a 4-digit year. Returns None on cancel."""
            prompt = "Jahr fehlt.\n"
            if title_hint:
                prompt += f"\nTitel: {title_hint}\n"
            prompt += "\nBitte Jahr eingeben (YYYY):"
            while True:
                val = simpledialog.askstring("Jahr", prompt, parent=self)
                if val is None:
                    return None
                val = val.strip()
                m = re.fullmatch(r"(19\d{2}|20\d{2})", val)
                if m:
                    return int(m.group(1))
                messagebox.showwarning("Jahr", "Ungültig. Bitte z.B. 2001 eingeben.", parent=self)

        def _prompt_title() -> str | None:
            val = simpledialog.askstring("Titel", "Titel fehlt. Bitte Titel eingeben:", parent=self)
            if val is None:
                return None
            val = _sanitize_title(val)
            return val if val else None

        for vm in self._groups:
            if not vm.selected:
                continue

            g = vm.group
            if not g.video:
                continue

            title = None
            year = None
            imdbid = None
            warn = ""

            if mode == "nfo":
                if not g.nfo:
                    warn = "Missing NFO"
                else:
                    try:
                        xml_text = _read_nfo_text(g.nfo.path)
                        info = parse_nfo(xml_text)
                        preferred_title = info.original_title or info.title
                        title = _sanitize_title(preferred_title or "")
                        year = info.year
                        imdbid = info.imdbid
                    except Exception as exc:  # noqa: BLE001
                        warn = f"NFO parse error: {exc} (path: {g.nfo.path})"

                # If some metadata is missing, we can interactively ask the user
                # (safe only when a single group is selected).
                if not warn and len(selected_groups) == 1:
                    if not title:
                        title = _prompt_title()
                    if year is None:
                        year = _prompt_year(title)
                    if not imdbid:
                        imdbid = _prompt_imdb_id(title, year)

                # Validate & final missing-check
                if not title or year is None or not imdbid:
                    if not warn:
                        warn = "Missing title/year/imdbid"
                elif not _IMDB_RE.fullmatch(imdbid):
                    warn = "Invalid imdbid"

            else:
                title = manual_title or None
                year = None
                if manual_year:
                    try:
                        year = int(re.findall(r"\d{4}", manual_year)[0])
                    except Exception:
                        year = None
                imdbid = manual_imdb or None

                # Best-effort fill from NFO if user didn't provide all fields.
                if (not title or year is None or not imdbid) and g.nfo:
                    try:
                        xml_text = _read_nfo_text(g.nfo.path)
                        info = parse_nfo(xml_text)
                        preferred_title = info.original_title or info.title
                        if not title:
                            title = _sanitize_title(preferred_title or "") or None
                        if year is None:
                            year = info.year
                        if not imdbid:
                            imdbid = info.imdbid
                    except Exception as exc:  # noqa: BLE001
                        warn = f"NFO parse error: {exc} (path: {g.nfo.path})"

                # Interactive prompts for remaining missing values (manual is single-group only).
                if not warn and len(selected_groups) == 1:
                    if not title:
                        title = _prompt_title()
                    if year is None:
                        year = _prompt_year(title)
                    if not imdbid:
                        imdbid = _prompt_imdb_id(title, year)

                # Update UI fields (so user sees what is used; still editable).
                if len(selected_groups) == 1:
                    if title and not manual_title:
                        self.manual_title.set(title)
                    if year is not None and not manual_year:
                        self.manual_year.set(str(year))
                    if imdbid and not manual_imdb:
                        self.manual_imdb.set(imdbid)

                # Validate & final missing-check
                if not title or year is None or not imdbid:
                    if not warn:
                        warn = "Missing title/year/imdbid"
                elif not _IMDB_RE.fullmatch(imdbid):
                    warn = "Invalid imdbid"

            if warn:
                vm.warning = warn
                vm.proposed = ""
                continue

            assert title and year and imdbid
            new_stem = self.app.settings.naming_template.format(title=title, year=year, imdbid=imdbid)
            new_stem = _sanitize_title(new_stem)
            vm.proposed = new_stem
            vm.warning = ""

            old_stem = g.video.stem
            dir_path = g.video.dir

            # Build rename operations for video + sidecars
            for f in g.all_files():
                src = f.path
                try:
                    sandbox.assert_path_allowed(src)
                except SandboxViolation as exc:
                    ops.append(Operation(kind=OperationKind.RENAME, src=src, dst=src, warning=str(exc), selected=False))
                    continue

                # Determine suffix part to preserve.
                #
                # We support:
                #  - <stem>.<ext>
                #  - <stem>.<lang>.<ext>
                #  - <stem>-poster.jpg / <stem>-backdrop.jpg / ...
                #  - folder-level artwork names (poster.jpg, logo.png, ...)
                #    -> renamed to <new_stem>-poster.jpg etc (only when grouped safely).
                name = f.name
                nlow = name.lower()
                suffix_part = ""

                # 1) Usual stem-based pattern: keep everything after the stem (preserves extension casing).
                if name.startswith(old_stem) and len(name) > len(old_stem):
                    rest = name[len(old_stem) :]
                    if rest.startswith(".") or rest.startswith("-"):
                        suffix_part = rest

                # 2) Folder-level NFO (movie.nfo) -> rename to <new_stem>.nfo
                if not suffix_part and nlow in FOLDER_LEVEL_NFO_NAMES and nlow.endswith(".nfo"):
                    suffix_part = ".nfo"

                # 3) Folder-level artwork (poster.jpg, logo.png, ...)
                if not suffix_part and nlow in FOLDER_LEVEL_SIDECAR_NAMES:
                    suffix_part = "-" + nlow

                if not suffix_part:
                    # Not a recognized sidecar naming; skip conservatively
                    ops.append(
                        Operation(
                            kind=OperationKind.RENAME,
                            src=src,
                            dst=src,
                            warning="Unrecognized sidecar name; skipped",
                            selected=False,
                        )
                    )
                    continue

                dst_name = new_stem + suffix_part
                dst = str(PurePosixPath(dir_path) / dst_name)
                try:
                    sandbox.assert_path_allowed(dst)
                except SandboxViolation as exc:
                    ops.append(Operation(kind=OperationKind.RENAME, src=src, dst=dst, warning=str(exc), selected=False))
                    continue

                ops.append(Operation(kind=OperationKind.RENAME, src=src, dst=dst))

            # Optional: rename the containing folder (safe heuristic).
            # Only rename if the folder name equals the old video stem.
            if self.rename_folder.get():
                try:
                    dir_base = PurePosixPath(dir_path).name
                    dir_low = dir_base.lower()
                    old_low = old_stem.lower()
                    # Safer-but-more-useful heuristic:
                    # - rename if directory name matches the old stem
                    # - OR if one is a prefix of the other (common: folder without " 1", file with " 1")
                    if (
                        (dir_low == old_low or old_low.startswith(dir_low) or dir_low.startswith(old_low))
                        and dir_path not in dir_renames_done
                    ):
                        new_dir = str(PurePosixPath(dir_path).parent / new_stem)
                        if new_dir != dir_path:
                            try:
                                sandbox.assert_path_allowed(dir_path)
                                sandbox.assert_path_allowed(new_dir)
                            except SandboxViolation as exc:
                                dir_ops.append(
                                    Operation(
                                        kind=OperationKind.RENAME,
                                        src=dir_path,
                                        dst=new_dir,
                                        warning=str(exc),
                                        selected=False,
                                    )
                                )
                            else:
                                dir_ops.append(Operation(kind=OperationKind.RENAME, src=dir_path, dst=new_dir))
                            dir_renames_done.add(dir_path)
                except Exception:
                    # Never fail the whole plan because of folder rename heuristics
                    pass

        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                pass

        # Ensure directory renames happen last.
        ops.extend(dir_ops)

        plan = Plan(title="Rename")
        plan.extend(ops)
        plan.apply_collision_warnings()
        self._plan = plan

        # Extra UX guidance: if we ended up with an empty plan, explain why.
        if plan.count_selected() == 0:
            reasons = []
            for vm in self._groups:
                if vm.selected and vm.warning:
                    reasons.append(vm.warning)
            if reasons:
                self.log.append_line("[local] Plan has 0 selected ops. Reasons: " + "; ".join(sorted(set(reasons))))
            else:
                self.log.append_line("[local] Plan has 0 selected ops. (No warnings recorded)")

        self.plan_table.bind_operations(plan.operations, row_getter=lambda op: (op.kind.value, op.src or "", op.dst or "", op.warning))
        self.group_table.bind_operations(
            self._groups,
            row_getter=lambda vm: (vm.video_path(), vm.nfo_path(), str(len(vm.group.all_files())), vm.proposed, vm.warning),
        )

        self._regen_script()
        self.log.append_line(f"[local] Plan ready: {plan.count_selected()} ops selected")

    def _regen_script(self) -> None:
        if not self._plan:
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
            on_exists="error",
        )
        self._script = generate_bash_script(self._plan, options=opts)
        self.out.set_text(self._script)

    def _execute(self) -> None:
        if not self._plan:
            self._build_plan()
            if not self._plan:
                return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        n = self._plan.count_selected()
        if n == 0:
            messagebox.showinfo("Plan", "Keine selektierten Operationen.", parent=self)
            return

        # Make Dry-Run behavior explicit to avoid confusion.
        if bool(self.dry_run.get()):
            choice = ask_execute_with_dry_run(self, ops_count=n)
            if choice is None:
                self.log.append_line("[local] cancelled (dry-run dialog)")
                return
            if choice == "real":
                self.dry_run.set(False)
                self._update_execute_label()
                self._regen_script()
                self.log.append_line("[local] executing REAL run (Dry-Run disabled)")
            else:
                self.log.append_line("[local] executing TEST run (Dry-Run enabled)")

        if n >= self.app.settings.mass_confirm_threshold:
            if not ask_text_confirm(self, "Mass-Confirm", f"Du bist dabei {n} Operationen auszuführen.", "JA"):
                self.log.append_line("[local] cancelled by mass-confirm")
                return

        self.log.append_line("[local] executing script...")
        t = threading.Thread(target=self._worker_exec, daemon=True)
        t.start()

    def _worker_exec(self) -> None:
        assert self._plan is not None
        assert self._script

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def on_out(line: str) -> None:
            stdout_lines.append(line)
            self.after(0, lambda l=line: self.log.append_line(l))

        def on_err(line: str) -> None:
            stderr_lines.append(line)
            self.after(0, lambda l=line: self.log.append_line("STDERR: " + l))

        try:
            exit_code = self.app.ssh.exec_bash_script_streaming(self._script, on_stdout=on_out, on_stderr=on_err)
            append_journal(
                {
                    "tab": "rename",
                    "plan_title": self._plan.title,
                    "host": self.app.settings.get_active_profile().host,
                    "username": self.app.settings.get_active_profile().username,
                    "dry_run": bool(self.dry_run.get()),
                    "no_overwrite": bool(self.app.settings.no_overwrite),
                    "ops_total": len(self._plan.operations),
                    "ops_selected": self._plan.count_selected(),
                    "ops": ops_to_journal_dicts(self._plan.selected_operations()),
                    "script": self._script,
                    "exit_code": exit_code,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": "\n".join(stderr_lines),
                }
            )

            # Keep the local analysis index in sync after a successful REAL run.
            # Without this, subsequent searches still show old paths until the next scan.
            if exit_code == 0 and (not bool(self.dry_run.get())):
                try:
                    stats = apply_plan_to_index(self._plan)
                    self.after(
                        0,
                        lambda s=stats: self.log.append_line(
                            f"[local] analysis index updated: +{s.inserted} -{s.deleted} prefixUpdates={s.updated_prefix}"
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda: self.log.append_line(f"[local] index update WARNING: {exc}"))

            self.after(0, lambda: self.log.append_line(f"[local] exit={exit_code} (journal written)"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] ERROR: {exc}"))
