from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, messagebox, ttk

import threading
from pathlib import PurePosixPath
from typing import Callable, Optional

from jfo.core.validators import Sandbox, SandboxViolation
from jfo.infra.remote_fs import Mountpoint, list_mountpoints, list_directories, normalize_posix_path, parent_dir


def ask_trust_hostkey(master: tk.Misc, host_id: str, fingerprint: str) -> bool:
    msg = (
        "Unbekannter Host-Key.\n\n"
        f"Host: {host_id}\n"
        f"Fingerprint: {fingerprint}\n\n"
        "Nur akzeptieren, wenn du den Fingerprint separat verifiziert hast (z.B. per Router/NAS-UI).\n\n"
        "Trust und speichern?"
    )
    return messagebox.askyesno("Host-Key Trust", msg, parent=master)


def ask_password(master: tk.Misc, title: str, prompt: str) -> str | None:
    return simpledialog.askstring(title, prompt, parent=master, show="*")


def ask_text_confirm(master: tk.Misc, title: str, prompt: str, expected: str) -> bool:
    value = simpledialog.askstring(title, prompt + f"\n\nTippe exakt: {expected}", parent=master)
    return (value or "").strip() == expected


def pick_remote_directory(
    master: tk.Misc,
    *,
    ssh,  # SshManager
    allowed_roots: list[str],
    initial_path: str = "/",
    title: str = "Remote Ordner auswählen",
    allow_set_allowed_root: bool = True,
    on_allowed_roots_updated: Optional[Callable[[list[str]], None]] = None,
) -> str | None:
    """Interactive remote folder picker (via SSH).

    - Shows mountpoints (df -h) + directory navigation
    - Enforces sandbox: selected path must be within allowed_roots
    - If allow_set_allowed_root is True, user can add the chosen path to allowed roots

    Returns the chosen path or None.
    """

    if not ssh.is_connected():
        messagebox.showerror("SSH", "Nicht verbunden. (Tab Main)", parent=master)
        return None

    dlg = tk.Toplevel(master)
    dlg.title(title)
    dlg.geometry("900x520")
    dlg.transient(master)
    dlg.grab_set()

    result: dict[str, str | None] = {"path": None}

    path_var = tk.StringVar(value=normalize_posix_path(initial_path) or "/")
    status_var = tk.StringVar(value="")
    add_allowed_var = tk.BooleanVar(value=False)

    # --- Top: current path + navigation ---
    top = ttk.Frame(dlg)
    top.pack(fill=tk.X, padx=10, pady=8)

    ttk.Label(top, text="Aktueller Pfad:").pack(side=tk.LEFT)
    path_entry = ttk.Entry(top, textvariable=path_var)
    path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

    def _go_up() -> None:
        path_var.set(parent_dir(path_var.get()))
        _refresh_dirs()

    ttk.Button(top, text="⬆ Up", command=_go_up).pack(side=tk.LEFT)

    ttk.Button(top, text="Refresh", command=lambda: _refresh_dirs()).pack(side=tk.LEFT, padx=(6, 0))

    # --- Middle: mountpoints + directories ---
    mid = ttk.Panedwindow(dlg, orient=tk.HORIZONTAL)
    mid.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 8))

    left = ttk.Labelframe(mid, text="Volumes / Mountpoints")
    right = ttk.Labelframe(mid, text="Ordner")
    mid.add(left, weight=2)
    mid.add(right, weight=5)

    mp_tree = ttk.Treeview(left, columns=["target", "size", "used", "avail", "use"], show="headings", height=16)
    for cid, txt, w in (
        ("target", "Mount", 260),
        ("size", "Size", 70),
        ("used", "Used", 70),
        ("avail", "Avail", 70),
        ("use", "Use%", 60),
    ):
        mp_tree.heading(cid, text=txt)
        mp_tree.column(cid, width=w, stretch=(cid == "target"))
    mp_tree.pack(fill=tk.BOTH, expand=True)

    mp_loading = ttk.Label(left, text="(lädt …)")
    mp_loading.pack(anchor=tk.W, padx=6, pady=4)

    dir_list = tk.Listbox(right, height=16)
    dir_list.pack(fill=tk.BOTH, expand=True)

    # --- Bottom: status + actions ---
    bottom = ttk.Frame(dlg)
    bottom.pack(fill=tk.X, padx=10, pady=(0, 10))

    ttk.Label(bottom, textvariable=status_var).pack(side=tk.LEFT, fill=tk.X, expand=True)

    if allow_set_allowed_root:
        ttk.Checkbutton(bottom, text="Als Allowed Root hinzufügen", variable=add_allowed_var).pack(side=tk.LEFT, padx=10)

    btn_use = ttk.Button(bottom, text="Diesen Ordner verwenden")
    btn_use.pack(side=tk.RIGHT)
    ttk.Button(bottom, text="Abbrechen", command=lambda: dlg.destroy()).pack(side=tk.RIGHT, padx=6)

    # Helpers
    def _sandbox_allows(p: str) -> tuple[bool, str]:
        try:
            Sandbox(allowed_roots).assert_path_allowed(p)
            return True, ""
        except SandboxViolation as exc:
            return False, str(exc)

    def _update_status() -> None:
        cur = normalize_posix_path(path_var.get())
        if not cur:
            cur = "/"
        ok, reason = _sandbox_allows(cur)
        if ok:
            status_var.set("Sandbox: OK")
            btn_use.state(["!disabled"])
        else:
            # If allowed_roots is empty, we can guide user to add one.
            if "No allowed roots" in reason and allow_set_allowed_root:
                status_var.set(
                    "Sandbox: Keine Allowed Roots gesetzt. Bitte Haken bei 'Als Allowed Root hinzufügen' setzen und dann übernehmen."
                )
                # Only enable when user explicitly opted-in to add an allowed root.
                if add_allowed_var.get():
                    btn_use.state(["!disabled"])
                else:
                    btn_use.state(["disabled"])
            else:
                status_var.set("Sandbox: " + reason)
                # allow if user is adding this as allowed root
                if allow_set_allowed_root and add_allowed_var.get():
                    btn_use.state(["!disabled"])
                else:
                    btn_use.state(["disabled"])

    def _set_path(p: str) -> None:
        path_var.set(normalize_posix_path(p))
        _refresh_dirs()

    # Mountpoints loading
    mountpoints: list[Mountpoint] = []

    def _load_mountpoints_worker() -> None:
        try:
            mps = list_mountpoints(ssh)
        except Exception as exc:  # noqa: BLE001
            dlg.after(0, lambda: status_var.set(f"Mountpoints laden fehlgeschlagen: {exc}"))
            mps = []

        def _apply() -> None:
            nonlocal mountpoints
            mountpoints = mps
            for iid in mp_tree.get_children():
                mp_tree.delete(iid)
            for mp in mountpoints:
                # Filter noisy pseudo mounts to improve UX
                if mp.target.startswith(("/proc", "/sys", "/dev", "/run")):
                    continue
                mp_tree.insert("", tk.END, values=(mp.target, mp.size, mp.used, mp.avail, mp.use_percent))
            mp_loading.config(text=f"{len(mp_tree.get_children())} Mountpoints")

            # Prefill path if empty or invalid
            cur = normalize_posix_path(path_var.get())
            if not cur or cur == "/":
                # Prefer /volume* mountpoints, else first usable mount
                preferred = None
                for mp in mountpoints:
                    if mp.target.startswith("/volume"):
                        preferred = mp.target
                        break
                if preferred is None:
                    for mp in mountpoints:
                        if mp.target.startswith(("/proc", "/sys", "/dev", "/run")):
                            continue
                        preferred = mp.target
                        break
                if preferred:
                    path_var.set(preferred)
                    _refresh_dirs()
            _update_status()

        dlg.after(0, _apply)

    threading.Thread(target=_load_mountpoints_worker, daemon=True).start()

    # Directory listing
    def _refresh_dirs() -> None:
        cur = normalize_posix_path(path_var.get())
        if not cur:
            cur = "/"
        path_var.set(cur)

        dir_list.delete(0, tk.END)
        dir_list.insert(tk.END, "(lädt …)")
        _update_status()

        def _worker() -> None:
            try:
                dirs = list_directories(ssh, cur)
            except Exception as exc:  # noqa: BLE001
                dirs = []
                err = str(exc)

                def _apply_err() -> None:
                    dir_list.delete(0, tk.END)
                    dir_list.insert(tk.END, "<Fehler beim Laden>")
                    status_var.set(f"{cur}: {err}")
                    _update_status()

                dlg.after(0, _apply_err)
                return

            def _apply_ok() -> None:
                dir_list.delete(0, tk.END)
                for d in dirs:
                    dir_list.insert(tk.END, d)
                status_var.set(f"{cur}  —  {len(dirs)} Unterordner")
                _update_status()

            dlg.after(0, _apply_ok)

        threading.Thread(target=_worker, daemon=True).start()

    def _enter_pressed(_e=None):  # noqa: ANN001
        _refresh_dirs()

    path_entry.bind("<Return>", _enter_pressed)

    def _on_mp_double(_e=None):  # noqa: ANN001
        sel = mp_tree.selection()
        if not sel:
            return
        iid = sel[0]
        vals = mp_tree.item(iid, "values")
        if not vals:
            return
        target = str(vals[0])
        _set_path(target)

    mp_tree.bind("<Double-1>", _on_mp_double)

    def _on_dir_double(_e=None):  # noqa: ANN001
        sel = dir_list.curselection()
        if not sel:
            return
        name = dir_list.get(sel[0])
        if not name or name.startswith("(") or name.startswith("<"):
            return
        cur = normalize_posix_path(path_var.get()) or "/"
        nxt = str(PurePosixPath(cur) / name)
        _set_path(nxt)

    dir_list.bind("<Double-1>", _on_dir_double)

    def _use_clicked() -> None:
        # If user selected a child directory, use it; else use current path.
        sel = dir_list.curselection()
        if sel:
            name = dir_list.get(sel[0])
            if name and not name.startswith("(") and not name.startswith("<"):
                cur = normalize_posix_path(path_var.get()) or "/"
                chosen = str(PurePosixPath(cur) / name)
            else:
                chosen = normalize_posix_path(path_var.get()) or "/"
        else:
            chosen = normalize_posix_path(path_var.get()) or "/"

        ok, reason = _sandbox_allows(chosen)
        if ok:
            result["path"] = chosen
            dlg.destroy()
            return

        # Outside sandbox / no allowed roots
        if allow_set_allowed_root and add_allowed_var.get():
            # Update allowed roots list in-place; caller decides when/how to persist.
            new_roots = list(dict.fromkeys([*allowed_roots, chosen]))
            allowed_roots[:] = new_roots

            if on_allowed_roots_updated:
                try:
                    on_allowed_roots_updated(new_roots)
                except Exception:
                    pass
            result["path"] = chosen
            dlg.destroy()
            return

        messagebox.showerror("Sandbox", reason, parent=dlg)

    btn_use.config(command=_use_clicked)

    def _on_add_allowed_toggle(*_a):  # noqa: ANN001
        _update_status()

    try:
        add_allowed_var.trace_add("write", _on_add_allowed_toggle)
    except Exception:
        pass

    # Initial load
    _refresh_dirs()

    dlg.wait_window()
    return result["path"]


def ask_execute_with_dry_run(master: tk.Misc, *, ops_count: int) -> str | None:
    """Ask the user how to execute when Dry-Run is enabled.

    Returns:
      - "dry"  : execute in Dry-Run mode (no changes)
      - "real" : execute for real (changes files)
      - None   : cancel

    Rationale: Many users assume that clicking "Ausführen" will change files.
    When DRY_RUN=1, the script prints 'DRY:' lines and does not modify anything.
    """

    dlg = tk.Toplevel(master)
    dlg.title("Ausführen")
    dlg.geometry("620x240")
    dlg.transient(master)
    dlg.grab_set()

    result: dict[str, str | None] = {"mode": None}

    msg = (
        "Testlauf (Dry-Run) ist aktiv.\n\n"
        "• Es werden KEINE Dateien verändert.\n"
        "• Im Log steht dann z.B. 'DRY: mv ...'\n\n"
        f"Geplante Operationen: {ops_count}\n\n"
        "Was möchtest du tun?"
    )

    body = ttk.Frame(dlg)
    body.pack(fill=tk.BOTH, expand=True, padx=14, pady=14)

    ttk.Label(body, text=msg, justify=tk.LEFT, wraplength=560).pack(anchor=tk.W, fill=tk.X)

    btns = ttk.Frame(body)
    btns.pack(side=tk.BOTTOM, fill=tk.X, pady=(18, 0))

    def _set(mode: str | None) -> None:
        result["mode"] = mode
        try:
            dlg.destroy()
        except Exception:
            pass

    ttk.Button(btns, text="Testlauf starten", command=lambda: _set("dry")).pack(side=tk.LEFT)
    ttk.Button(btns, text="Echt ausführen", command=lambda: _set("real")).pack(side=tk.LEFT, padx=8)
    ttk.Button(btns, text="Abbrechen", command=lambda: _set(None)).pack(side=tk.RIGHT)

    dlg.wait_window()
    return result["mode"]
