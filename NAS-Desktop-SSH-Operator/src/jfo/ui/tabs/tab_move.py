from __future__ import annotations

from pathlib import PurePosixPath
import threading
import tkinter as tk
from tkinter import ttk, messagebox

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


class MoveTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._plan: Plan | None = None
        self._script: str = ""

        in_frm = ttk.LabelFrame(self, text="Massives Verschieben")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        # Source / Dest with remote browser buttons (prevents path typos)
        src_row = ttk.Frame(in_frm)
        src_row.pack(fill=tk.X, pady=2)
        self.src_entry = LabeledEntry(src_row, "Quelle (remote dir):", width=80)
        self.src_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(src_row, text="Browse…", command=lambda: self._browse_into(self.src_entry)).pack(side=tk.LEFT, padx=6)

        dst_row = ttk.Frame(in_frm)
        dst_row.pack(fill=tk.X, pady=2)
        self.dst_entry = LabeledEntry(dst_row, "Ziel (remote dir):", width=80)
        self.dst_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(dst_row, text="Browse…", command=lambda: self._browse_into(self.dst_entry)).pack(side=tk.LEFT, padx=6)

        opt_frm = ttk.Frame(in_frm)
        opt_frm.pack(fill=tk.X, pady=(6, 2))
        self.dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        self.skip_existing = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_frm,
            text="Testlauf (ändert nichts)",
            variable=self.dry_run,
            command=self._dry_run_changed,
        ).pack(side=tk.LEFT)
        ttk.Checkbutton(opt_frm, text="Skip wenn Ziel existiert", variable=self.skip_existing, command=self._regen_script).pack(side=tk.LEFT, padx=10)

        ttk.Button(opt_frm, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT, padx=6)
        self.exec_btn = ttk.Button(opt_frm, text="Ausführen", command=self._execute)
        self.exec_btn.pack(side=tk.LEFT, padx=6)

        prev_frm = ttk.LabelFrame(self, text="Preview (Doppelklick toggelt Sel)")
        prev_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.table = PlanTable(prev_frm, columns=["Type", "Source", "Dest"], on_toggle=self._regen_script)
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

    def _build_plan(self) -> None:
        src = self.src_entry.get()
        dst = self.dst_entry.get()
        if not src or not dst:
            messagebox.showwarning("Eingabe", "Quelle und Ziel müssen gesetzt sein.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)
        try:
            sandbox.assert_path_allowed(src)
            sandbox.assert_path_allowed(dst)
        except SandboxViolation as exc:
            messagebox.showerror("Sandbox", str(exc), parent=self)
            return

        self.log.append_line("[local] building move plan from analysis index...")
        t = threading.Thread(target=self._worker_build_plan, args=(src, dst), daemon=True)
        t.start()

    def _worker_build_plan(self, src: str, dst: str) -> None:
        # Use analysis index to expand the move set.
        exts = set([e.lower().lstrip(".") for e in (self.app.settings.video_exts + self.app.settings.sidecar_exts)])
        paths = files_under_dir_recursive(src, exts=exts, limit=50000)
        if not paths:
            self.after(0, lambda: messagebox.showinfo("Scan/Index", "Keine Dateien im Analyse-Index gefunden. Bitte zuerst Tab 'Scan / Index' ausführen.", parent=self))
            return

        ops: list[Operation] = []
        src_prefix = src.rstrip("/")
        dst_prefix = dst.rstrip("/")

        for p in paths:
            if not p.startswith(src_prefix + "/"):
                continue
            rel = p[len(src_prefix) + 1 :]
            dest = str(PurePosixPath(dst_prefix) / PurePosixPath(rel))
            ops.append(Operation(kind=OperationKind.MOVE, src=p, dst=dest))

        plan = Plan(title="Move")
        plan.extend(ops)
        plan.apply_collision_warnings()

        def _apply() -> None:
            self._plan = plan
            self.table.bind_operations(plan.operations, row_getter=lambda op: (op.kind.value, op.src or "", op.dst or ""))
            self._regen_script()
            self.log.append_line(f"[local] Plan ready: {plan.count_selected()} ops selected (from analysis index)")

        self.after(0, _apply)

    def _regen_script(self) -> None:
        if not self._plan:
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
            on_exists=("skip" if self.skip_existing.get() else "error"),
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
                    "tab": "move",
                    "plan_title": self._plan.title,
                    "host": self.app.settings.get_active_profile().host,
                    "username": self.app.settings.get_active_profile().username,
                    "dry_run": bool(self.dry_run.get()),
                    "skip_existing": bool(self.skip_existing.get()),
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
