from __future__ import annotations

import json
from pathlib import Path
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Dict, List, Optional

from jfo.core.history import ops_from_journal, build_undo_plan, ops_to_journal_dicts
from jfo.core.plan import Plan
from jfo.core.scriptgen import ScriptOptions, generate_bash_script
from jfo.infra.journal import append_journal, journal_path
from jfo.infra.index_update import apply_plan_to_index
from jfo.ui.dialogs import ask_text_confirm, ask_execute_with_dry_run
from jfo.ui.widgets import ReadonlyText, LogText, PlanTable


class HistoryTab(ttk.Frame):
    """Journal browser + Undo generator.

    The journal is append-only JSONL stored in the user's app data directory.

    Undo strategy (safe + conservative):
    - By default, undo is generated only for MOVE/RENAME operations.
    - Undo operations are executed in reverse order.
    - No automatic deletions (copy/link undo) unless we later add a safe_rm helper.
    """

    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app

        self._records_by_iid: dict[str, dict[str, Any]] = {}
        self._selected_record: dict[str, Any] | None = None

        self._undo_plan: Plan | None = None
        self._undo_script: str = ""

        # --- Top controls ---
        top = ttk.LabelFrame(self, text="History / Journal")
        top.pack(fill=tk.X, padx=10, pady=10)

        path = journal_path()
        ttk.Label(top, text=f"Journal: {path}").pack(side=tk.LEFT, padx=6)
        ttk.Button(top, text="Refresh", command=self._refresh).pack(side=tk.RIGHT, padx=6)

        # --- Main layout ---
        outer = ttk.Panedwindow(self, orient=tk.VERTICAL)
        outer.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # Records list
        list_frm = ttk.Labelframe(outer, text="Runs")
        outer.add(list_frm, weight=2)

        cols = ["time", "tab", "host", "user", "mode", "exit", "ops"]
        self.tree = ttk.Treeview(list_frm, columns=cols, show="headings", height=9, selectmode="browse")
        for cid, text, w in (
            ("time", "Time (UTC)", 170),
            ("tab", "Tab", 110),
            ("host", "Host", 140),
            ("user", "User", 100),
            ("mode", "Mode", 80),
            ("exit", "Exit", 60),
            ("ops", "Ops", 60),
        ):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, stretch=(cid in ("host", "time")))

        ysb = ttk.Scrollbar(list_frm, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # Details + Undo
        details = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        outer.add(details, weight=5)

        left = ttk.Labelframe(details, text="Details")
        right = ttk.Labelframe(details, text="Undo")
        details.add(left, weight=5)
        details.add(right, weight=5)

        # --- Details (left) ---
        meta = ttk.Frame(left)
        meta.pack(fill=tk.X, padx=8, pady=6)
        self.meta_var = tk.StringVar(value="(wähle einen Run)")
        ttk.Label(meta, textvariable=self.meta_var).pack(anchor=tk.W)

        nb = ttk.Notebook(left)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self.script_txt = ReadonlyText(nb, height=14)
        self.stdout_txt = ReadonlyText(nb, height=14)
        self.stderr_txt = ReadonlyText(nb, height=14)

        nb.add(self.script_txt, text="Script")
        nb.add(self.stdout_txt, text="Stdout")
        nb.add(self.stderr_txt, text="Stderr")

        # --- Undo (right) ---
        opt = ttk.Frame(right)
        opt.pack(fill=tk.X, padx=8, pady=6)

        self.only_mv = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Nur mv/rename undo (empfohlen)", variable=self.only_mv).pack(side=tk.LEFT)

        self.undo_dry_run = tk.BooleanVar(value=self.app.settings.default_dry_run)
        ttk.Checkbutton(opt, text="Testlauf (ändert nichts)", variable=self.undo_dry_run, command=self._regen_undo_script).pack(
            side=tk.LEFT, padx=10
        )

        ttk.Button(opt, text="Undo-Plan erzeugen", command=self._build_undo_plan).pack(side=tk.RIGHT)

        self.undo_exec_btn = ttk.Button(opt, text="Ausführen", command=self._execute_undo)
        self.undo_exec_btn.pack(side=tk.RIGHT, padx=6)

        self._update_undo_execute_label()

        prev = ttk.Labelframe(right, text="Undo Preview (Doppelklick toggelt Sel)")
        prev.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.undo_table = PlanTable(prev, columns=["Type", "From", "To"], on_toggle=self._regen_undo_script)
        self.undo_table.pack(fill=tk.BOTH, expand=True)

        out = ttk.Labelframe(right, text="Undo Script")
        out.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.undo_out = ReadonlyText(out, height=10)
        self.undo_out.pack(fill=tk.BOTH, expand=True)

        log_frm = ttk.Labelframe(right, text="Log")
        log_frm.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.log = LogText(log_frm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        self._refresh()

    # ----------------- Journal reading -----------------

    def _read_journal_records(self) -> list[dict[str, Any]]:
        path = Path(journal_path())
        if not path.exists():
            return []
        recs: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(rec, dict):
                        recs.append(rec)
        except Exception as exc:  # noqa: BLE001
            self.log.append_line(f"[local] ERROR reading journal: {exc}")
            return []

        def _key(r: dict[str, Any]) -> str:
            return str(r.get("timestamp_utc") or "")

        recs.sort(key=_key, reverse=True)
        return recs

    def _refresh(self) -> None:
        self._records_by_iid.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)

        recs = self._read_journal_records()
        for idx, r in enumerate(recs):
            ts = str(r.get("timestamp_utc") or "")
            tab = str(r.get("tab") or "")
            host = str(r.get("host") or "")
            user = str(r.get("username") or "")
            dry = bool(r.get("dry_run"))
            mode = "DRY" if dry else "REAL"
            exit_code = r.get("exit_code")
            exit_s = str(exit_code) if exit_code is not None else ""
            ops = r.get("ops_selected")
            ops_s = str(ops) if ops is not None else ""

            iid = f"r{idx}"
            self.tree.insert("", tk.END, iid=iid, values=(ts, tab, host, user, mode, exit_s, ops_s))
            self._records_by_iid[iid] = r

        self.meta_var.set(f"{len(recs)} Runs")
        self._selected_record = None
        self.script_txt.set_text("")
        self.stdout_txt.set_text("")
        self.stderr_txt.set_text("")
        self._clear_undo()

    def _on_select(self, _e=None):  # noqa: ANN001
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        rec = self._records_by_iid.get(iid)
        if not rec:
            return
        self._selected_record = rec

        ts = str(rec.get("timestamp_utc") or "")
        tab = str(rec.get("tab") or "")
        host = str(rec.get("host") or "")
        user = str(rec.get("username") or "")
        dry = bool(rec.get("dry_run"))
        exit_code = rec.get("exit_code")
        ops_sel = rec.get("ops_selected")

        self.meta_var.set(f"{ts} | {tab} | {user}@{host} | {'DRY' if dry else 'REAL'} | exit={exit_code} | ops={ops_sel}")

        self.script_txt.set_text(str(rec.get("script") or ""))
        self.stdout_txt.set_text(str(rec.get("stdout") or ""))
        self.stderr_txt.set_text(str(rec.get("stderr") or ""))

        self._clear_undo()

    # ----------------- Undo generation -----------------

    def _clear_undo(self) -> None:
        self._undo_plan = None
        self._undo_script = ""
        self.undo_table.clear()
        self.undo_out.set_text("")
        self.log.clear()
        self._update_undo_execute_label()

    def _update_undo_execute_label(self) -> None:
        if getattr(self, "undo_exec_btn", None) is None:
            return
        self.undo_exec_btn.config(text=("Testlauf ausführen" if bool(self.undo_dry_run.get()) else "Echt ausführen"))

    def _build_undo_plan(self) -> None:
        rec = self._selected_record
        if not rec:
            messagebox.showinfo("History", "Bitte zuerst einen Run auswählen.", parent=self)
            return

        # Guardrails: Undo is meaningful only for successful REAL runs.
        if bool(rec.get("dry_run")):
            messagebox.showinfo(
                "Undo",
                "Dieser Run war ein Testlauf (Dry-Run). Es wurden keine Dateien verändert – es gibt nichts rückgängig zu machen.",
                parent=self,
            )
            return
        if int(rec.get("exit_code") or 0) != 0:
            if not messagebox.askyesno(
                "Undo",
                "Der ausgewählte Run hatte einen Fehler (exit_code != 0).\n\nTrotzdem einen Undo-Plan erzeugen?",
                parent=self,
            ):
                return

        try:
            executed_ops = ops_from_journal(rec)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Undo", f"Konnte Operationen nicht laden: {exc}", parent=self)
            return

        title = f"Undo {rec.get('tab')} {rec.get('timestamp_utc')}"
        plan, skipped = build_undo_plan(executed_ops, title=title, only_mv_rename=bool(self.only_mv.get()))

        if plan.count_selected() == 0:
            msg = "Keine undo-fähigen Operationen gefunden.\n\nHinweis: Standardmäßig wird nur mv/rename rückgängig gemacht."
            if skipped:
                msg += "\n\nSkipped:\n" + "\n".join(skipped[:20])
            messagebox.showinfo("Undo", msg, parent=self)
            return

        self._undo_plan = plan
        self.undo_table.bind_operations(plan.operations, row_getter=lambda op: (op.kind.value, op.src or "", op.dst or ""))
        self._regen_undo_script()

        self.log.append_line(f"[local] Undo plan ready: {plan.count_selected()} ops selected")
        if skipped:
            self.log.append_line(f"[local] Note: {len(skipped)} ops were skipped (unsupported for undo)")

    def _regen_undo_script(self) -> None:
        self._update_undo_execute_label()
        if not self._undo_plan:
            return
        opts = ScriptOptions(
            allowed_roots=self.app.settings.allowed_roots,
            dry_run=bool(self.undo_dry_run.get()),
            no_overwrite=bool(self.app.settings.no_overwrite),
            on_exists="error",
        )
        self._undo_script = generate_bash_script(self._undo_plan, options=opts)
        self.undo_out.set_text(self._undo_script)

    # ----------------- Execute undo -----------------

    def _execute_undo(self) -> None:
        if not self._undo_plan:
            self._build_undo_plan()
            if not self._undo_plan:
                return

        if not self.app.ssh.is_connected():
            messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=self)
            return

        n = self._undo_plan.count_selected()
        if n == 0:
            messagebox.showinfo("Undo", "Keine selektierten Operationen.", parent=self)
            return

        # Host mismatch warning
        rec = self._selected_record or {}
        rec_host = str(rec.get("host") or "")
        cur_host = str(self.app.settings.get_active_profile().host or "")
        if rec_host and cur_host and rec_host != cur_host:
            if not messagebox.askyesno(
                "Undo",
                f"Du bist aktuell mit Host '{cur_host}' verbunden, aber der Run war auf '{rec_host}'.\n\nTrotzdem ausführen?",
                parent=self,
            ):
                return

        # Make Dry-Run behavior explicit
        if bool(self.undo_dry_run.get()):
            choice = ask_execute_with_dry_run(self, ops_count=n)
            if choice is None:
                self.log.append_line("[local] cancelled (dry-run dialog)")
                return
            if choice == "real":
                self.undo_dry_run.set(False)
                self._regen_undo_script()
                self.log.append_line("[local] executing REAL undo (Dry-Run disabled)")
            else:
                self.log.append_line("[local] executing TEST undo (Dry-Run enabled)")

        # Extra safety: require an explicit confirm for real undo.
        if not bool(self.undo_dry_run.get()):
            if not ask_text_confirm(
                self,
                "Undo bestätigen",
                f"Du bist dabei {n} Operationen rückgängig zu machen.",
                "UNDO",
            ):
                self.log.append_line("[local] cancelled by undo-confirm")
                return

        self.log.append_line("[local] executing undo script...")
        t = threading.Thread(target=self._worker_exec, daemon=True)
        t.start()

    def _worker_exec(self) -> None:
        assert self._undo_plan is not None
        assert self._undo_script

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def on_out(line: str) -> None:
            stdout_lines.append(line)
            self.after(0, lambda l=line: self.log.append_line(l))

        def on_err(line: str) -> None:
            stderr_lines.append(line)
            self.after(0, lambda l=line: self.log.append_line("STDERR: " + l))

        try:
            exit_code = self.app.ssh.exec_bash_script_streaming(self._undo_script, on_stdout=on_out, on_stderr=on_err)

            # Journal entry for undo
            rec = self._selected_record or {}
            append_journal(
                {
                    "tab": "history_undo",
                    "plan_title": self._undo_plan.title,
                    "undo_of": {
                        "timestamp_utc": rec.get("timestamp_utc"),
                        "tab": rec.get("tab"),
                        "host": rec.get("host"),
                        "username": rec.get("username"),
                    },
                    "host": self.app.settings.get_active_profile().host,
                    "username": self.app.settings.get_active_profile().username,
                    "dry_run": bool(self.undo_dry_run.get()),
                    "no_overwrite": bool(self.app.settings.no_overwrite),
                    "ops_total": len(self._undo_plan.operations),
                    "ops_selected": self._undo_plan.count_selected(),
                    "ops": ops_to_journal_dicts(self._undo_plan.selected_operations()),
                    "script": self._undo_script,
                    "exit_code": exit_code,
                    "stdout": "\n".join(stdout_lines),
                    "stderr": "\n".join(stderr_lines),
                }
            )

            # Update analysis index after successful REAL undo.
            if exit_code == 0 and (not bool(self.undo_dry_run.get())):
                try:
                    stats = apply_plan_to_index(self._undo_plan)
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
