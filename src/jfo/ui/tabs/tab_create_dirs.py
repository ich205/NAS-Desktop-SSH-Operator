from __future__ import annotations

import csv
from pathlib import PurePosixPath
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan
from jfo.core.history import ops_to_journal_dicts
from jfo.core.scriptgen import ScriptOptions, generate_bash_script
from jfo.core.validators import Sandbox, SandboxViolation
from jfo.infra.journal import append_journal
from jfo.ui.dialogs import ask_text_confirm, pick_remote_directory, ask_execute_with_dry_run
from jfo.ui.widgets import LabeledEntry, ReadonlyText, LogText, PlanTable


class CreateDirsTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._plan: Plan | None = None
        self._script: str = ""

        in_frm = ttk.LabelFrame(self, text="Ordnerstruktur aus Datei")
        in_frm.pack(fill=tk.X, padx=10, pady=10)

        self.file_var = tk.StringVar()
        file_frm = ttk.Frame(in_frm)
        file_frm.pack(fill=tk.X, pady=2)
        ttk.Label(file_frm, text="Input-Datei:").pack(side=tk.LEFT)
        ttk.Entry(file_frm, textvariable=self.file_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(file_frm, text="Browse", command=self._browse).pack(side=tk.LEFT)

        # Remote target root + browse to prevent typos
        rr_row = ttk.Frame(in_frm)
        rr_row.pack(fill=tk.X, pady=2)
        self.remote_root = LabeledEntry(rr_row, "Remote Ziel-Root:", width=80)
        self.remote_root.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(rr_row, text="Browse…", command=lambda: self._browse_remote_root()).pack(side=tk.LEFT, padx=6)

        opt_frm = ttk.Frame(in_frm)
        opt_frm.pack(fill=tk.X, pady=(6, 2))
        self.dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        ttk.Checkbutton(
            opt_frm,
            text="Testlauf (ändert nichts)",
            variable=self.dry_run,
            command=self._dry_run_changed,
        ).pack(side=tk.LEFT)
        ttk.Button(opt_frm, text="Plan erstellen", command=self._build_plan).pack(side=tk.LEFT, padx=6)
        self.exec_btn = ttk.Button(opt_frm, text="Ausführen", command=self._execute)
        self.exec_btn.pack(side=tk.LEFT, padx=6)

        prev_frm = ttk.LabelFrame(self, text="Preview (Doppelklick toggelt Sel)")
        prev_frm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.table = PlanTable(prev_frm, columns=["Type", "Path"], on_toggle=self._regen_script)
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

        # Prefill with first allowed root (if available)
        if not self.remote_root.get() and self.app.settings.allowed_roots:
            self.remote_root.set(self.app.settings.allowed_roots[0])

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
        self.exec_btn.config(text=("Testlauf ausführen" if bool(self.dry_run.get()) else "Echt ausführen"))

    def _dry_run_changed(self) -> None:
        self._update_execute_label()
        self._regen_script()

    def _browse_remote_root(self) -> None:
        initial = self.remote_root.get() or (self.app.settings.allowed_roots[0] if self.app.settings.allowed_roots else "/")
        chosen = pick_remote_directory(
            self,
            ssh=self.app.ssh,
            allowed_roots=self.app.settings.allowed_roots,
            initial_path=initial,
            title="Remote Ziel-Root auswählen",
            allow_set_allowed_root=True,
            on_allowed_roots_updated=self._allowed_roots_updated,
        )
        if chosen:
            self.remote_root.set(chosen)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(title="Input-Datei wählen")
        if path:
            self.file_var.set(path)

    def _read_input_paths(self) -> list[str]:
        path = self.file_var.get().strip()
        if not path:
            raise ValueError("Keine Input-Datei gewählt")

        text = open(path, "r", encoding="utf-8", errors="replace").read().splitlines()
        entries: list[str] = []

        # CSV heuristics: header contains ';' or first non-empty line contains ';'
        is_csv = any(";" in ln for ln in text[:5] if ln.strip())

        if is_csv:
            # path;type
            for ln in text:
                if not ln.strip() or ln.strip().startswith("#"):
                    continue
                parts = ln.split(";", 1)
                rel = parts[0].strip()
                if rel:
                    entries.append(rel)
        else:
            for ln in text:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                entries.append(ln)

        return entries

    def _build_plan(self) -> None:
        try:
            entries = self._read_input_paths()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Input", str(exc), parent=self)
            return

        remote_root = self.remote_root.get()
        if not remote_root:
            messagebox.showwarning("Eingabe", "Remote Ziel-Root fehlt.", parent=self)
            return

        sandbox = Sandbox(self.app.settings.allowed_roots)

        ops: list[Operation] = []
        for rel in entries:
            rel_clean = rel.lstrip("/")
            p = PurePosixPath(remote_root) / PurePosixPath(rel_clean)
            dst = str(p)
            try:
                sandbox.assert_path_allowed(dst)
            except SandboxViolation as exc:
                ops.append(Operation(kind=OperationKind.MKDIR, dst=dst, warning=str(exc)))
                continue
            ops.append(Operation(kind=OperationKind.MKDIR, dst=dst))

        plan = Plan(title="Create directories")
        plan.extend(ops)
        plan.apply_collision_warnings()

        self._plan = plan
        self._regen_script()

        self.table.bind_operations(
            plan.operations,
            row_getter=lambda op: (op.kind.value, op.dst or ""),
        )

        self.log.append_line(f"[local] Plan ready: {plan.count_selected()} ops selected")

    def _regen_script(self) -> None:
        if not self._plan:
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
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
                    "tab": "create_dirs",
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
            self.after(0, lambda: self.log.append_line(f"[local] exit={exit_code} (journal written)"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] ERROR: {exc}"))
