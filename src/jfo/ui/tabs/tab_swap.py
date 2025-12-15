from __future__ import annotations

"""Swap tab: exchange names of two movie folders (and their related files).

Use-case:
- Folder A contains Movie B but is named as Movie A (and files share that wrong stem)
- Folder B contains Movie A but is named as Movie B

This tab swaps the *names* of both folders and (optionally) renames the contained
movie file + sidecars so that folder name and file base name match again.

Safety:
- Plan-first with preview.
- Uses a temporary folder name to avoid collisions.
- Default is Dry-Run.
- Enforces sandbox (allowed roots).
"""

import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from jfo.core.media_grouping import (
    group_media_files,
    FOLDER_LEVEL_SIDECAR_NAMES,
    FOLDER_LEVEL_NFO_NAMES,
)
from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan
from jfo.core.scriptgen import ScriptOptions, generate_bash_script
from jfo.core.validators import Sandbox, SandboxViolation
from jfo.core.quoting import bash_quote
from jfo.core.history import ops_to_journal_dicts
from jfo.infra.journal import append_journal
from jfo.infra.index_update import apply_plan_to_index
from jfo.infra.sqlite_index import files_in_dir
from jfo.ui.dialogs import ask_text_confirm, pick_remote_directory, ask_execute_with_dry_run
from jfo.ui.widgets import LabeledEntry, ReadonlyText, LogText, PlanTable


@dataclass
class FolderInfo:
    path: str
    name: str
    video_stem: str
    files_count: int
    nfo_path: str | None = None


class SwapTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._plan: Plan | None = None
        self._script: str = ""
        self._info_a: FolderInfo | None = None
        self._info_b: FolderInfo | None = None

        in_frm = ttk.LabelFrame(self, text="Tauschen (2 Film-Ordner inkl. Dateinamen)")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        row_a = ttk.Frame(in_frm)
        row_a.pack(fill=tk.X, pady=2)
        self.a_entry = LabeledEntry(row_a, "Ordner A (remote):", width=80)
        self.a_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row_a, text="Browse…", command=lambda: self._browse_into(self.a_entry)).pack(side=tk.LEFT, padx=6)

        row_b = ttk.Frame(in_frm)
        row_b.pack(fill=tk.X, pady=2)
        self.b_entry = LabeledEntry(row_b, "Ordner B (remote):", width=80)
        self.b_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row_b, text="Browse…", command=lambda: self._browse_into(self.b_entry)).pack(side=tk.LEFT, padx=6)

        hint = (
            "Tipp: Wähle zwei falsch benannte Film-Ordner.\n"
            "Das Tool tauscht die Ordnernamen und benennt (optional) die enthaltenen Dateien so um, "
            "dass alles wieder zusammenpasst."
        )
        ttk.Label(in_frm, text=hint).pack(anchor=tk.W, pady=(2, 0))

        opt = ttk.Frame(in_frm)
        opt.pack(fill=tk.X, pady=(6, 2))

        self.dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        ttk.Checkbutton(opt, text="Testlauf (ändert nichts)", variable=self.dry_run, command=self._dry_run_changed).pack(
            side=tk.LEFT
        )
        self.swap_files = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Dateinamen tauschen (empfohlen)", variable=self.swap_files, command=self._regen_script).pack(
            side=tk.LEFT, padx=10
        )
        self.swap_folders = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Ordnernamen tauschen", variable=self.swap_folders, command=self._regen_script).pack(
            side=tk.LEFT
        )

        ttk.Button(opt, text="Infos laden", command=self._load_infos).pack(side=tk.LEFT, padx=6)
        ttk.Button(opt, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT, padx=6)
        self.exec_btn = ttk.Button(opt, text="Ausführen", command=self._execute)
        self.exec_btn.pack(side=tk.LEFT, padx=6)

        # Info preview
        info_frm = ttk.LabelFrame(self, text="Erkannte Infos")
        info_frm.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.info_txt = ReadonlyText(info_frm, height=5)
        self.info_txt.pack(fill=tk.BOTH, expand=True)

        prev_frm = ttk.LabelFrame(self, text="Plan (Alt → Neu) (Doppelklick toggelt Sel)")
        prev_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.table = PlanTable(prev_frm, columns=["Type", "Source", "Dest", "Warn"], on_toggle=self._regen_script)
        self.table.pack(fill=tk.BOTH, expand=True)

        out_frm = ttk.LabelFrame(self, text="Generiertes Script")
        out_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.out = ReadonlyText(out_frm, height=10)
        self.out.pack(fill=tk.BOTH, expand=True)

        log_frm = ttk.LabelFrame(self, text="Log")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = LogText(log_frm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        self._update_execute_label()

    # ---------- UI helpers ----------

    def _update_execute_label(self) -> None:
        if getattr(self, "exec_btn", None) is None:
            return
        self.exec_btn.config(text=("Testlauf ausführen" if bool(self.dry_run.get()) else "Echt ausführen"))

    def _dry_run_changed(self) -> None:
        self._update_execute_label()
        self._regen_script()

    def _allowed_roots_updated(self, roots: list[str]) -> None:
        self.app.settings.allowed_roots = list(roots)
        try:
            txt = self.app.tab_connection.allowed_roots
            txt.delete("1.0", tk.END)
            txt.insert("1.0", "\n".join(roots))
        except Exception:
            pass
        self.log.append_line(f"[local] Allowed Roots updated: {', '.join(roots)}")

    def _browse_into(self, entry: LabeledEntry) -> None:
        initial = entry.get() or (self.app.settings.allowed_roots[0] if self.app.settings.allowed_roots else "/")
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
            entry.set(chosen)

    # ---------- Remote / index helpers ----------

    def _list_files_best_effort(self, dir_path: str) -> list[str]:
        """Get immediate file paths inside a directory.

        Prefer the local analysis index (fast), fall back to remote find.
        """

        # From local analysis index
        exts = set([e.lower().lstrip(".") for e in (self.app.settings.video_exts + self.app.settings.sidecar_exts)])
        paths = files_in_dir(dir_path, exts=exts)
        if paths:
            return paths

        # Fallback: remote find (maxdepth 1)
        if not self.app.ssh.is_connected():
            return []
        cmd = f"find {bash_quote(dir_path)} -maxdepth 1 -type f -print"
        res = self.app.ssh.exec_command(cmd)
        if res.exit_status != 0:
            raise RuntimeError(res.stderr.strip() or f"find failed (exit {res.exit_status})")
        out: list[str] = []
        for ln in res.stdout.splitlines():
            ln = ln.strip("\r\n")
            if ln:
                out.append(ln)
        return out

    def _load_infos(self) -> None:
        a = self.a_entry.get()
        b = self.b_entry.get()
        if not a or not b:
            messagebox.showwarning("Eingabe", "Bitte Ordner A und B setzen.", parent=self)
            return
        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return
        self.log.append_line("[local] loading folder infos...")
        t = threading.Thread(target=self._worker_load_infos, args=(a, b), daemon=True)
        t.start()

    def _worker_load_infos(self, a: str, b: str) -> None:
        try:
            info_a = self._inspect_folder(a)
            info_b = self._inspect_folder(b)
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] ERROR: {exc}"))
            return

        def _apply() -> None:
            self._info_a = info_a
            self._info_b = info_b
            self._render_infos()
            self.log.append_line("[local] infos loaded")

        self.after(0, _apply)

    def _inspect_folder(self, path: str) -> FolderInfo:
        # Basic existence check
        res = self.app.ssh.exec_command(f"test -d -- {bash_quote(path)}")
        if res.exit_status != 0:
            raise RuntimeError(f"Folder not found or not a directory: {path}")

        name = PurePosixPath(path).name
        paths = self._list_files_best_effort(path)
        if not paths:
            raise RuntimeError(f"No files found in folder (index empty and find returned nothing): {path}")

        groups = group_media_files(
            paths,
            video_exts=set(self.app.settings.video_exts),
            sidecar_exts=set(self.app.settings.sidecar_exts),
        )
        groups = [g for g in groups if g.video]
        if len(groups) != 1:
            raise RuntimeError(
                f"Swap erwartet genau 1 Film pro Ordner. In '{path}' wurden {len(groups)} Video-Gruppen gefunden. "
                "Nutze ggf. den Tab 'Umbenennen' für komplexere Ordner."
            )
        g = groups[0]
        nfo_path = g.nfo.path if g.nfo else None
        return FolderInfo(path=path, name=name, video_stem=g.video.stem if g.video else "", files_count=len(g.all_files()), nfo_path=nfo_path)

    def _render_infos(self) -> None:
        a = self._info_a
        b = self._info_b
        if not a or not b:
            self.info_txt.set_text("")
            return
        txt = (
            "Ordner A:\n"
            f"  Path: {a.path}\n"
            f"  Folder name: {a.name}\n"
            f"  Video stem: {a.video_stem}\n"
            f"  Files in group: {a.files_count}\n"
            + (f"  NFO: {a.nfo_path}\n" if a.nfo_path else "  NFO: (none)\n")
            + "\n"
            "Ordner B:\n"
            f"  Path: {b.path}\n"
            f"  Folder name: {b.name}\n"
            f"  Video stem: {b.video_stem}\n"
            f"  Files in group: {b.files_count}\n"
            + (f"  NFO: {b.nfo_path}\n" if b.nfo_path else "  NFO: (none)\n")
        )
        self.info_txt.set_text(txt)

    # ---------- Plan building ----------

    def _build_plan(self) -> None:
        a = self.a_entry.get()
        b = self.b_entry.get()
        if not a or not b:
            messagebox.showwarning("Eingabe", "Bitte Ordner A und B setzen.", parent=self)
            return

        if not (self.swap_files.get() or self.swap_folders.get()):
            messagebox.showwarning("Optionen", "Bitte mindestens 'Dateinamen tauschen' oder 'Ordnernamen tauschen' aktivieren.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)
        try:
            sandbox.assert_path_allowed(a)
            sandbox.assert_path_allowed(b)
        except SandboxViolation as exc:
            messagebox.showerror("Sandbox", str(exc), parent=self)
            return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        self.log.append_line("[local] building swap plan...")
        t = threading.Thread(target=self._worker_build_plan, args=(a, b), daemon=True)
        t.start()

    def _worker_build_plan(self, a: str, b: str) -> None:
        try:
            info_a = self._inspect_folder(a)
            info_b = self._inspect_folder(b)
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] ERROR: {exc}"))
            return

        # Safer default: require same parent to truly 'swap names' instead of moving directories between parents.
        parent_a = str(PurePosixPath(info_a.path).parent)
        parent_b = str(PurePosixPath(info_b.path).parent)
        if parent_a != parent_b:
            self.after(
                0,
                lambda: messagebox.showerror(
                    "Swap",
                    "Ordner A und B liegen nicht im selben Parent-Ordner.\n\n"
                    "Für den sicheren Namenstausch erwartet das Tool zwei Ordner im gleichen Verzeichnis.\n"
                    f"A parent: {parent_a}\nB parent: {parent_b}\n\n"
                    "Tipp: Verschiebe sie zuerst in den gleichen Ordner (Tab 'Verschieben'), oder benenne manuell im Tab 'Umbenennen'.",
                    parent=self,
                ),
            )
            return

        name_a = info_a.name
        name_b = info_b.name
        old_stem_a = info_a.video_stem
        old_stem_b = info_b.video_stem

        ops: list[Operation] = []
        plan = Plan(title="Swap")
        plan.add_warning("Hinweis: Swap-Operationen sollten zusammen ausgeführt werden. Das Abwählen einzelner Zeilen kann zu Inkonsistenzen führen.")

        def _build_file_ops(dir_path: str, old_stem: str, new_stem: str) -> list[Operation]:
            # Gather file paths (best effort)
            paths = self._list_files_best_effort(dir_path)
            groups = group_media_files(
                paths,
                video_exts=set(self.app.settings.video_exts),
                sidecar_exts=set(self.app.settings.sidecar_exts),
            )
            groups = [g for g in groups if g.video]
            if len(groups) != 1:
                raise RuntimeError(f"Expected 1 video group in {dir_path}, got {len(groups)}")
            g = groups[0]

            out_ops: list[Operation] = []
            for f in g.all_files():
                src = f.path
                name = f.name
                nlow = name.lower()
                suffix_part = ""

                if name.startswith(old_stem) and len(name) > len(old_stem):
                    rest = name[len(old_stem) :]
                    if rest.startswith(".") or rest.startswith("-"):
                        suffix_part = rest

                if not suffix_part and nlow in FOLDER_LEVEL_NFO_NAMES and nlow.endswith(".nfo"):
                    suffix_part = ".nfo"

                if not suffix_part and nlow in FOLDER_LEVEL_SIDECAR_NAMES:
                    suffix_part = "-" + nlow

                if not suffix_part:
                    out_ops.append(
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
                if dst == src:
                    out_ops.append(
                        Operation(
                            kind=OperationKind.RENAME,
                            src=src,
                            dst=dst,
                            warning="Already matches; skipped",
                            selected=False,
                        )
                    )
                    continue
                out_ops.append(Operation(kind=OperationKind.RENAME, src=src, dst=dst))
            return out_ops

        try:
            if bool(self.swap_files.get()):
                # Rename contained files first while folder names are still stable.
                ops.extend(_build_file_ops(info_a.path, old_stem_a, name_b))
                ops.extend(_build_file_ops(info_b.path, old_stem_b, name_a))

            if bool(self.swap_folders.get()):
                # Swap the directory names using a temporary name.
                parent = PurePosixPath(parent_a)
                tmp = str(parent / f"{name_a}.__JFO_SWAP_TMP__{secrets.token_hex(3)}")
                # Use explicit RENAME kind for readability (script uses mv anyway).
                ops.append(Operation(kind=OperationKind.RENAME, src=info_a.path, dst=tmp, detail="swap: A -> tmp"))
                ops.append(Operation(kind=OperationKind.RENAME, src=info_b.path, dst=info_a.path, detail="swap: B -> A"))
                ops.append(Operation(kind=OperationKind.RENAME, src=tmp, dst=info_b.path, detail="swap: tmp -> B"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] ERROR building swap ops: {exc}"))
            return

        plan.extend(ops)
        plan.apply_collision_warnings()

        def _apply() -> None:
            self._plan = plan
            self._info_a = info_a
            self._info_b = info_b
            self._render_infos()
            self.table.bind_operations(plan.operations, row_getter=lambda op: (op.kind.value, op.src or "", op.dst or "", op.warning))
            self._regen_script()
            self.log.append_line(f"[local] Plan ready: {plan.count_selected()} ops selected")

        self.after(0, _apply)

    def _regen_script(self) -> None:
        if not self._plan:
            self.out.set_text("")
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
            on_exists="error",
        )
        self._script = generate_bash_script(self._plan, options=opts)
        self.out.set_text(self._script)

    # ---------- Execute ----------

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

        # Mass-confirm only for real runs.
        if (not bool(self.dry_run.get())) and n >= self.app.settings.mass_confirm_threshold:
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
                    "tab": "swap",
                    "plan_title": self._plan.title,
                    "host": self.app.settings.get_active_profile().host,
                    "username": self.app.settings.get_active_profile().username,
                    "dry_run": bool(self.dry_run.get()),
                    "swap_files": bool(self.swap_files.get()),
                    "swap_folders": bool(self.swap_folders.get()),
                    "folder_a": self.a_entry.get(),
                    "folder_b": self.b_entry.get(),
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
