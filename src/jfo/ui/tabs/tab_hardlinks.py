from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from jfo.core.categories import MOVIE_CATEGORIES
from jfo.core.media_grouping import MediaGroup, group_media_files
from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan
from jfo.core.history import ops_to_journal_dicts
from jfo.core.scriptgen import ScriptOptions, generate_bash_script
from jfo.core.validators import Sandbox, SandboxViolation
from jfo.infra.journal import append_journal
from jfo.infra.index_update import apply_plan_to_index
from jfo.infra.sqlite_index import files_under_dir_recursive
from jfo.ui.dialogs import ask_text_confirm, pick_remote_directory, ask_execute_with_dry_run
from jfo.ui.widgets import LabeledEntry, ReadonlyText, LogText, PlanTable


@dataclass
class MovieVM:
    group: MediaGroup
    selected: bool = False

    def display(self, master_root: str) -> str:
        if not self.group.video:
            return "(no video)"
        p = PurePosixPath(self.group.video.path)
        try:
            rel = str(p.relative_to(PurePosixPath(master_root)))
        except Exception:
            rel = self.group.video.path
        return rel


class HardlinksTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._movies: list[MovieVM] = []
        self._filtered_movies: list[MovieVM] = []
        self._plan: Plan | None = None
        self._script: str = ""

        in_frm = ttk.LabelFrame(self, text="Master + Kategorie-Libraries (Hardlinks)")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        # Master root + Libraries root with remote browser buttons
        mr_row = ttk.Frame(in_frm)
        mr_row.pack(fill=tk.X, pady=2)
        self.master_root = LabeledEntry(mr_row, "Master-Movies Root (remote):", width=80)
        self.master_root.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(mr_row, text="Browse…", command=lambda: self._browse_into(self.master_root, title="Master Root auswählen")).pack(side=tk.LEFT, padx=6)

        lr_row = ttk.Frame(in_frm)
        lr_row.pack(fill=tk.X, pady=2)
        self.lib_root = LabeledEntry(lr_row, "Libraries Root (remote):", width=80)
        self.lib_root.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(lr_row, text="Browse…", command=lambda: self._browse_into(self.lib_root, title="Libraries Root auswählen")).pack(side=tk.LEFT, padx=6)

        opt_frm = ttk.Frame(in_frm)
        opt_frm.pack(fill=tk.X, pady=(6, 2))
        self.dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        ttk.Checkbutton(
            opt_frm,
            text="Testlauf (ändert nichts)",
            variable=self.dry_run,
            command=self._dry_run_changed,
        ).pack(side=tk.LEFT)

        self.sidecar_policy = tk.StringVar(value="none")
        ttk.Label(opt_frm, text="Sidecars:").pack(side=tk.LEFT, padx=(20, 4))
        ttk.Radiobutton(opt_frm, text="keine", variable=self.sidecar_policy, value="none", command=self._regen_script).pack(side=tk.LEFT)
        ttk.Radiobutton(opt_frm, text="hardlink", variable=self.sidecar_policy, value="link", command=self._regen_script).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(opt_frm, text="copy", variable=self.sidecar_policy, value="copy", command=self._regen_script).pack(side=tk.LEFT, padx=6)

        ttk.Button(opt_frm, text="Movies laden", command=self._load_movies).pack(side=tk.LEFT, padx=(20, 6))
        ttk.Button(opt_frm, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT, padx=6)
        self.exec_btn = ttk.Button(opt_frm, text="Ausführen", command=self._execute)
        self.exec_btn.pack(side=tk.LEFT, padx=6)

        info = (
            "Hinweise (Jellyfin):\n"
            "• Hardlinks können als separate Einträge angezeigt werden; Jellyfin dedupliziert nicht zuverlässig.\n"
            "• Library-Pfade nicht ineinander verschachteln (keine Library innerhalb einer anderen).\n"
            "• Viele Libraries = mehr Scan-Aufwand.\n"
        )
        ttk.Label(in_frm, text=info, justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))

        sel_frm = ttk.Frame(self)
        sel_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Movies selection
        m_frm = ttk.LabelFrame(sel_frm, text="Movies (Multi-Select via Doppelklick in Liste)")
        m_frm.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        search_frm = ttk.Frame(m_frm)
        search_frm.pack(fill=tk.X, pady=2)
        ttk.Label(search_frm, text="Search:").pack(side=tk.LEFT)
        self.movie_search = tk.StringVar(value="")
        e = ttk.Entry(search_frm, textvariable=self.movie_search)
        e.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        e.bind("<KeyRelease>", lambda _e: self._apply_filter())

        self.movie_list = tk.Listbox(m_frm, selectmode=tk.MULTIPLE)
        self.movie_list.pack(fill=tk.BOTH, expand=True)

        # Categories selection
        c_frm = ttk.LabelFrame(sel_frm, text="Kategorien (Multi-Select)")
        c_frm.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        self.cat_list = tk.Listbox(c_frm, selectmode=tk.MULTIPLE)
        self.cat_list.pack(fill=tk.BOTH, expand=True)
        for cat in MOVIE_CATEGORIES:
            self.cat_list.insert(tk.END, cat)

        # Plan
        plan_frm = ttk.LabelFrame(self, text="Plan (Doppelklick toggelt Sel)")
        plan_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.plan_table = PlanTable(plan_frm, columns=["Type", "Source", "Dest", "Warn"], on_toggle=self._regen_script)
        self.plan_table.pack(fill=tk.BOTH, expand=True)

        out_frm = ttk.LabelFrame(self, text="Generiertes Script")
        out_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.out = ReadonlyText(out_frm, height=10)
        self.out.pack(fill=tk.BOTH, expand=True)

        log_frm = ttk.LabelFrame(self, text="Log")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = LogText(log_frm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        # Prefill defaults based on allowed roots (if configured)
        if self.app.settings.allowed_roots:
            if not self.master_root.get():
                self.master_root.set(self.app.settings.allowed_roots[0])
            if not self.lib_root.get():
                self.lib_root.set(self.app.settings.allowed_roots[0])

        self._update_execute_label()

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

    def _browse_into(self, entry: LabeledEntry, *, title: str) -> None:
        initial = entry.get() or (self.app.settings.allowed_roots[0] if self.app.settings.allowed_roots else "/")
        chosen = pick_remote_directory(
            self,
            ssh=self.app.ssh,
            allowed_roots=self.app.settings.allowed_roots,
            initial_path=initial,
            title=title,
            allow_set_allowed_root=True,
            on_allowed_roots_updated=self._allowed_roots_updated,
        )
        if chosen:
            entry.set(chosen)

    def _load_movies(self) -> None:
        master_root = self.master_root.get()
        if not master_root:
            messagebox.showwarning("Eingabe", "Master-Root fehlt.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)
        try:
            sandbox.assert_path_allowed(master_root)
        except SandboxViolation as exc:
            messagebox.showerror("Sandbox", str(exc), parent=self)
            return

        self.log.append_line("[local] loading movies from analysis index...")
        t = threading.Thread(target=self._worker_load_movies, args=(master_root,), daemon=True)
        t.start()

    def _worker_load_movies(self, master_root: str) -> None:
        exts = set([e.lower().lstrip(".") for e in (self.app.settings.video_exts + self.app.settings.sidecar_exts)])
        paths = files_under_dir_recursive(master_root, exts=exts, limit=200000)
        if not paths:
            self.after(
                0,
                lambda: messagebox.showinfo(
                    "Analyse",
                    "Keine Dateien im Analyse-Index gefunden. Bitte zuerst Tab 'Scan / Index' auf den Master-Root ausführen.",
                    parent=self,
                ),
            )
            return

        groups = group_media_files(
            paths,
            video_exts=set(self.app.settings.video_exts),
            sidecar_exts=set(self.app.settings.sidecar_exts),
        )
        movies = [MovieVM(group=g) for g in groups if g.video]

        def _apply() -> None:
            self._movies = movies
            self._apply_filter()
            self.log.append_line(f"[local] loaded {len(self._movies)} master movie groups")

        self.after(0, _apply)

    def _apply_filter(self) -> None:
        master_root = self.master_root.get()
        term = self.movie_search.get().strip().lower()
        self.movie_list.delete(0, tk.END)

        self._filtered_movies = []
        for vm in self._movies:
            text = vm.display(master_root)
            if term and term not in text.lower():
                continue
            self._filtered_movies.append(vm)
            self.movie_list.insert(tk.END, text)

    def _selected_movies(self) -> list[MovieVM]:
        idxs = list(self.movie_list.curselection())
        # map back to filtered vms
        return [self._filtered_movies[i] for i in idxs if i < len(self._filtered_movies)]

    def _selected_categories(self) -> list[str]:
        idxs = list(self.cat_list.curselection())
        return [MOVIE_CATEGORIES[i] for i in idxs if i < len(MOVIE_CATEGORIES)]

    def _build_plan(self) -> None:
        if not self._movies:
            self._load_movies()
            if not self._movies:
                return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        master_root = self.master_root.get().rstrip("/")
        lib_root = self.lib_root.get().rstrip("/")
        if not lib_root:
            messagebox.showwarning("Eingabe", "Libraries Root fehlt.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)
        try:
            sandbox.assert_path_allowed(master_root)
            sandbox.assert_path_allowed(lib_root)
        except SandboxViolation as exc:
            messagebox.showerror("Sandbox", str(exc), parent=self)
            return

        movies = self._selected_movies()
        cats = self._selected_categories()
        if not movies or not cats:
            messagebox.showwarning("Eingabe", "Bitte mindestens 1 Movie und 1 Kategorie wählen.", parent=self)
            return

        policy = self.sidecar_policy.get()

        ops: list[Operation] = []

        # Ensure top-level category folders exist
        for cat in cats:
            cat_dir = str(PurePosixPath(lib_root) / cat)
            ops.append(Operation(kind=OperationKind.MKDIR, dst=cat_dir))

        for mv in movies:
            g = mv.group
            if not g.video:
                continue
            src_video = g.video.path

            # Compute relative folder + filename to keep stable structure per movie
            vpath = PurePosixPath(src_video)
            try:
                rel_file = vpath.relative_to(PurePosixPath(master_root))
            except Exception:
                # fallback: just basename
                rel_file = PurePosixPath(vpath.name)

            for cat in cats:
                dest_file = PurePosixPath(lib_root) / cat / rel_file
                ops.append(Operation(kind=OperationKind.LINK, src=src_video, dst=str(dest_file)))

                if policy in ("link", "copy"):
                    for sc in g.sidecars:
                        sp = PurePosixPath(sc.path)
                        try:
                            rel_sc = sp.relative_to(PurePosixPath(master_root))
                        except Exception:
                            rel_sc = PurePosixPath(sp.name)
                        dst_sc = PurePosixPath(lib_root) / cat / rel_sc
                        kind = OperationKind.LINK if policy == "link" else OperationKind.COPY
                        ops.append(Operation(kind=kind, src=sc.path, dst=str(dst_sc)))

        plan = Plan(title="Hardlinks")
        plan.extend(ops)
        plan.apply_collision_warnings()
        self._plan = plan

        self.plan_table.bind_operations(plan.operations, row_getter=lambda op: (op.kind.value, op.src or "", op.dst or "", op.warning))
        self._regen_script()
        self.log.append_line(f"[local] Plan ready: {plan.count_selected()} ops selected")

    def _regen_script(self) -> None:
        if not self._plan:
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
            on_exists="skip",  # hardlinks: skip existing links by default
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
                    "tab": "hardlinks",
                    "plan_title": self._plan.title,
                    "host": self.app.settings.get_active_profile().host,
                    "username": self.app.settings.get_active_profile().username,
                    "dry_run": bool(self.dry_run.get()),
                    "sidecar_policy": self.sidecar_policy.get(),
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
