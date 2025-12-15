from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, List, Optional


class LabeledEntry(ttk.Frame):
    def __init__(self, master, label: str, *, width: int = 40, show: str | None = None):
        super().__init__(master)
        ttk.Label(self, text=label).pack(side=tk.LEFT, padx=(0, 6))
        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var, width=width)
        if show is not None:
            self.entry.config(show=show)
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value)


class LabeledCombobox(ttk.Frame):
    def __init__(self, master, label: str, values: list[str], *, width: int = 40):
        super().__init__(master)
        ttk.Label(self, text=label).pack(side=tk.LEFT, padx=(0, 6))
        self.var = tk.StringVar()
        self.combo = ttk.Combobox(self, textvariable=self.var, values=values, width=width)
        self.combo.pack(side=tk.LEFT, fill=tk.X, expand=True)

    def get(self) -> str:
        return self.var.get().strip()

    def set(self, value: str) -> None:
        self.var.set(value)


class ReadonlyText(ttk.Frame):
    def __init__(self, master, *, height: int = 12):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap=tk.NONE)
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.config(state=tk.DISABLED)

    def set_text(self, value: str) -> None:
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.insert(tk.END, value)
        self.text.config(state=tk.DISABLED)

    def get_text(self) -> str:
        return self.text.get("1.0", tk.END)


class LogText(ttk.Frame):
    def __init__(self, master, *, height: int = 10):
        super().__init__(master)
        self.text = tk.Text(self, height=height, wrap=tk.WORD)
        self.text.pack(fill=tk.BOTH, expand=True)
        self.text.config(state=tk.DISABLED)

    def append_line(self, line: str) -> None:
        self.text.config(state=tk.NORMAL)
        self.text.insert(tk.END, line + "\n")
        self.text.see(tk.END)
        self.text.config(state=tk.DISABLED)

    def clear(self) -> None:
        self.text.config(state=tk.NORMAL)
        self.text.delete("1.0", tk.END)
        self.text.config(state=tk.DISABLED)


class PlanTable(ttk.Frame):
    """Operation preview table with a lightweight per-row toggle.

    ttk.Treeview has no native checkbox, so we use a first column "Sel" that shows "✓".
    Double-click toggles the row selection flag.
    """

    def __init__(self, master, *, columns: list[str], on_toggle: Callable[[], None] | None = None):
        super().__init__(master)
        self._on_toggle = on_toggle
        self._ops_by_iid: dict[str, object] = {}

        cols = ["Sel"] + columns
        self.tree = ttk.Treeview(self, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("Sel", text="Sel")
        self.tree.column("Sel", width=40, stretch=False, anchor=tk.CENTER)

        for c in columns:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=220, stretch=True)
        ysb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscroll=ysb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ysb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Double-1>", self._on_double_click)

    def clear(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._ops_by_iid.clear()

    def bind_operations(self, operations: list[object], *, row_getter: Callable[[object], tuple[str, ...]]):
        """Populate rows and remember operation objects.

        row_getter(op) must return a tuple matching the provided 'columns'.
        The op object must have a boolean attribute 'selected'.
        """

        self.clear()
        for op in operations:
            sel = "✓" if getattr(op, "selected", True) else ""
            row = (sel,) + row_getter(op)
            iid = self.tree.insert("", tk.END, values=row)
            self._ops_by_iid[iid] = op

    def selected_objects(self) -> list[object]:
        """Return objects for currently selected rows (Treeview selection)."""
        result: list[object] = []
        for iid in self.tree.selection():
            op = self._ops_by_iid.get(iid)
            if op is not None:
                result.append(op)
        return result

    def _on_double_click(self, event) -> None:  # noqa: ANN001
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        op = self._ops_by_iid.get(iid)
        if op is None:
            return
        current = bool(getattr(op, "selected", True))
        setattr(op, "selected", not current)

        # Update row
        values = list(self.tree.item(iid, "values"))
        if values:
            values[0] = "✓" if getattr(op, "selected", True) else ""
            self.tree.item(iid, values=values)
        if self._on_toggle:
            self._on_toggle()
