from __future__ import annotations

import os
import platform
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, ttk, messagebox

from jfo.infra.settings import ConnectionProfile
from jfo.ui.widgets import LabeledEntry, ReadonlyText, LogText
from jfo.ui.dialogs import ask_trust_hostkey


class ConnectionTab(ttk.Frame):
    def __init__(self, master, *, app):
        super().__init__(master)
        self.app = app
        self._cancel_event = threading.Event()

        profile = app.settings.get_active_profile()

        # --- Connection inputs ---
        frm = ttk.LabelFrame(self, text="NAS Verbindung")
        frm.pack(fill=tk.X, padx=10, pady=10)

        # IMPORTANT: create the variable connection fields in the visible UI.
        # We intentionally create widgets *inside* their row frames (not via pack(in_=...)),
        # because some Tk builds/themes can behave oddly when packing a widget into a
        # different container than its master.
        #
        # These fields must always exist because they are required to generate the
        # SSH command (Host/IP + Port + Username).

        # Row 1: Host/IP + Port
        row1 = ttk.Frame(frm)
        row1.pack(fill=tk.X, pady=2)
        self.host = LabeledEntry(row1, "Host/IP:", width=30)
        self.host.pack(side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        self.port = LabeledEntry(row1, "Port:", width=6)
        self.port.pack(side=tk.LEFT)

        # Row 2: User + (password widget will be attached here for password auth)
        row2 = ttk.Frame(frm)
        row2.pack(fill=tk.X, pady=2)
        self.user = LabeledEntry(row2, "User:", width=20)
        self.user.pack(side=tk.LEFT, padx=(0, 8), fill=tk.X, expand=True)
        # Password is never persisted. It's only held in-memory for the current session.
        self.password_var = tk.StringVar(value="")
        self.show_password_var = tk.BooleanVar(value=False)
        self.auth_mode = tk.StringVar(value=profile.auth_mode)
        self.key_path_var = tk.StringVar(value=profile.key_path)
        self.key_passphrase_var = tk.StringVar(value="")
        self.show_key_passphrase_var = tk.BooleanVar(value=False)

        # Password input (shown/hidden depending on auth mode)
        self.pw_frm = ttk.Frame(row2)
        self.pw_entry = ttk.Entry(self.pw_frm, textvariable=self.password_var, show="*", width=28)
        ttk.Label(self.pw_frm, text="Passwort:").pack(side=tk.LEFT, padx=(0, 6))
        self.pw_entry.pack(side=tk.LEFT)
        ttk.Checkbutton(
            self.pw_frm,
            text="anzeigen",
            variable=self.show_password_var,
            command=self._toggle_password_visibility,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(self.pw_frm, text="(wird nicht gespeichert)").pack(side=tk.LEFT, padx=(6, 0))
        self.pw_frm.pack(side=tk.LEFT)

        auth_frm = ttk.Frame(frm)
        auth_frm.pack(fill=tk.X, pady=2)
        ttk.Label(auth_frm, text="Auth:").pack(side=tk.LEFT)
        ttk.Radiobutton(
            auth_frm,
            text="Passwort",
            variable=self.auth_mode,
            value="password",
            command=self._update_cmd,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Radiobutton(
            auth_frm,
            text="SSH-Key / Agent",
            variable=self.auth_mode,
            value="key",
            command=self._update_cmd,
        ).pack(side=tk.LEFT, padx=6)

        # SSH key options (only relevant when auth_mode == 'key')
        self.key_frm = ttk.Frame(frm)
        self.key_frm.pack(fill=tk.X, pady=2)
        ttk.Label(self.key_frm, text="Keyfile (optional):").pack(side=tk.LEFT)
        self.key_entry = ttk.Entry(self.key_frm, textvariable=self.key_path_var)
        self.key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(self.key_frm, text="Browse", command=self._browse_key).pack(side=tk.LEFT)
        ttk.Button(self.key_frm, text="Key erstellen…", command=self._generate_key_clicked).pack(side=tk.LEFT, padx=6)

        ttk.Label(self.key_frm, text="(leer = SSH-Agent/Standardkeys)").pack(side=tk.LEFT, padx=(6, 0))

        self.key2_frm = ttk.Frame(frm)
        self.key2_frm.pack(fill=tk.X, pady=2)
        ttk.Label(self.key2_frm, text="Key-Passphrase:").pack(side=tk.LEFT)
        self.key_pass_entry = ttk.Entry(self.key2_frm, textvariable=self.key_passphrase_var, show="*", width=28)
        self.key_pass_entry.pack(side=tk.LEFT, padx=6)
        ttk.Checkbutton(
            self.key2_frm,
            text="anzeigen",
            variable=self.show_key_passphrase_var,
            command=self._toggle_key_passphrase_visibility,
        ).pack(side=tk.LEFT)
        ttk.Button(self.key2_frm, text="Public Key auf NAS installieren", command=self._install_pubkey_clicked).pack(
            side=tk.LEFT,
            padx=10,
        )
        ttk.Label(self.key2_frm, text="(benötigt Passwort-Login)").pack(side=tk.LEFT, padx=(6, 0))

        btn_frm = ttk.Frame(frm)
        btn_frm.pack(fill=tk.X, pady=(6, 2))
        ttk.Button(btn_frm, text="Verbindung testen", command=self._test_connection).pack(side=tk.LEFT)
        ttk.Button(btn_frm, text="Verbinden (integriert)", command=self._connect).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frm, text="SSH Terminal öffnen", command=self._open_powershell).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frm, text="Trennen", command=self._disconnect).pack(side=tk.RIGHT)

        # --- Settings ---
        sfrm = ttk.LabelFrame(self, text="Sicherheit / Settings")
        sfrm.pack(fill=tk.X, padx=10, pady=(0, 10))

        ttk.Label(sfrm, text="Allowed Root Paths (eine pro Zeile):").pack(anchor=tk.W)
        self.allowed_roots = tk.Text(sfrm, height=3)
        self.allowed_roots.pack(fill=tk.X, pady=4)

        # Expose additional variable settings that affect command generation in other tabs.
        # This prevents "hidden defaults" and avoids having to edit JSON by hand.
        self.naming_template_entry = LabeledEntry(sfrm, "Naming-Template:", width=80)
        self.naming_template_entry.pack(fill=tk.X, pady=2)

        self.video_exts_entry = LabeledEntry(sfrm, "Video-Extensions (comma):", width=80)
        self.video_exts_entry.pack(fill=tk.X, pady=2)

        self.sidecar_exts_entry = LabeledEntry(sfrm, "Sidecar-Extensions (comma):", width=80)
        self.sidecar_exts_entry.pack(fill=tk.X, pady=2)

        opt_frm = ttk.Frame(sfrm)
        opt_frm.pack(fill=tk.X)
        self.dry_run_default = tk.BooleanVar(value=app.settings.default_dry_run)
        self.no_overwrite = tk.BooleanVar(value=app.settings.no_overwrite)
        ttk.Checkbutton(opt_frm, text="Dry-Run default", variable=self.dry_run_default).pack(side=tk.LEFT)
        ttk.Checkbutton(opt_frm, text="Kein Überschreiben", variable=self.no_overwrite).pack(side=tk.LEFT, padx=10)

        self.mass_threshold_var = tk.StringVar(value=str(app.settings.mass_confirm_threshold))
        ttk.Label(opt_frm, text="Mass-Confirm ab #Ops:").pack(side=tk.LEFT, padx=(20, 4))
        ttk.Entry(opt_frm, textvariable=self.mass_threshold_var, width=6).pack(side=tk.LEFT)

        ttk.Button(opt_frm, text="Settings speichern", command=self._save_settings).pack(side=tk.RIGHT)

        # --- Output ---
        outfrm = ttk.LabelFrame(self, text="Generierter SSH-Befehl (ohne Secrets)")
        outfrm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.out = ReadonlyText(outfrm, height=6)
        self.out.pack(fill=tk.BOTH, expand=True)

        logfrm = ttk.LabelFrame(self, text="Log")
        logfrm.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.log = LogText(logfrm, height=10)
        self.log.pack(fill=tk.BOTH, expand=True)

        # Populate fields
        self.host.set(profile.host)
        self.port.set(str(profile.port))
        self.user.set(profile.username)
        self.allowed_roots.insert("1.0", "\n".join(app.settings.allowed_roots))

        # Settings (editable in UI)
        self.naming_template_entry.set(app.settings.naming_template)
        self.video_exts_entry.set(",".join(app.settings.video_exts))
        self.sidecar_exts_entry.set(",".join(app.settings.sidecar_exts))

        for w in (
            self.host.var,
            self.port.var,
            self.user.var,
            self.key_path_var,
            self.auth_mode,
            self.password_var,
            self.key_passphrase_var,
        ):
            try:
                w.trace_add("write", lambda *_: self._update_cmd())
            except Exception:
                pass

        self._update_cmd()

    def _toggle_password_visibility(self) -> None:
        self.pw_entry.config(show="" if self.show_password_var.get() else "*")

    def _toggle_key_passphrase_visibility(self) -> None:
        self.key_pass_entry.config(show="" if self.show_key_passphrase_var.get() else "*")

    def _save_settings(self) -> None:
        # Connection profile
        p = self.app.settings.get_active_profile()
        p.host = self.host.get()
        try:
            p.port = int(self.port.get() or "22")
        except ValueError:
            p.port = 22
        p.username = self.user.get()
        p.auth_mode = self.auth_mode.get()
        p.key_path = self.key_path_var.get().strip()

        # App settings
        roots = [line.strip() for line in self.allowed_roots.get("1.0", tk.END).splitlines() if line.strip()]
        self.app.settings.allowed_roots = roots
        self.app.settings.default_dry_run = bool(self.dry_run_default.get())
        self.app.settings.no_overwrite = bool(self.no_overwrite.get())

        # Naming + grouping related settings (affect Rename/Hardlinks/Move tabs)
        tmpl = self.naming_template_entry.get().strip()
        if tmpl:
            self.app.settings.naming_template = tmpl

        def _split_exts(raw: str) -> list[str]:
            return [x.strip().lstrip(".").lower() for x in raw.split(",") if x.strip()]

        ve = _split_exts(self.video_exts_entry.get())
        se = _split_exts(self.sidecar_exts_entry.get())
        if ve:
            self.app.settings.video_exts = ve
        if se:
            self.app.settings.sidecar_exts = se
        try:
            self.app.settings.mass_confirm_threshold = int(self.mass_threshold_var.get())
        except ValueError:
            self.app.settings.mass_confirm_threshold = 200

        from jfo.infra.settings import save_settings

        save_settings(self.app.settings)
        messagebox.showinfo("Settings", "Gespeichert.", parent=self)

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(title="SSH Private Key auswählen")
        if path:
            self.key_path_var.set(path)
            self.auth_mode.set("key")
            self._update_cmd()

    def _generate_key_clicked(self) -> None:
        """Generate an SSH keypair locally (optional helper).

        This is intentionally simple: RSA 4096 via Paramiko.
        """

        # Ask where to save private key
        initial = os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa_jellyfin_organizer")
        path = filedialog.asksaveasfilename(
            title="Private Key speichern als",
            initialfile=os.path.basename(initial),
            initialdir=os.path.dirname(initial),
        )
        if not path:
            return

        def _worker() -> None:
            try:
                import paramiko

                self.after(0, lambda: self.log.append_line("[local] generating RSA key (4096)…"))
                key = paramiko.RSAKey.generate(4096)

                # Ensure parent dir
                os.makedirs(os.path.dirname(path), exist_ok=True)

                # Write private key
                key.write_private_key_file(path)
                try:
                    os.chmod(path, 0o600)
                except Exception:
                    pass

                # Write public key
                pub = f"{key.get_name()} {key.get_base64()} jellyfin-organizer"
                pub_path = path + ".pub"
                with open(pub_path, "w", encoding="utf-8") as f:
                    f.write(pub + "\n")

                self.after(0, lambda: self.key_path_var.set(path))
                self.after(0, lambda: self.auth_mode.set("key"))
                self.after(0, lambda: self._update_cmd())
                self.after(0, lambda: self.log.append_line(f"[local] key generated: {path} (+ .pub)"))
                self.after(0, lambda: messagebox.showinfo(
                    "SSH Key erstellt",
                    f"Privater Key: {path}\nPublic Key: {pub_path}\n\nTipp: Nutze 'Public Key auf NAS installieren', um Key-Login zu aktivieren.",
                    parent=self,
                ))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self.log.append_line(f"[local] key generation ERROR: {exc}"))
                self.after(0, lambda: messagebox.showerror("Fehler", str(exc), parent=self))

        threading.Thread(target=_worker, daemon=True).start()

    def _install_pubkey_clicked(self) -> None:
        """Install the public key into ~/.ssh/authorized_keys on the NAS.

        Safe helper:
        - Requires password auth (local password field)
        - Uses SFTP to append the key if missing
        """

        self._save_settings()
        profile = self.app.settings.get_active_profile()

        key_path = self.key_path_var.get().strip()
        if not key_path or not os.path.exists(key_path):
            messagebox.showwarning("Key fehlt", "Bitte zuerst einen Key auswählen oder erstellen.", parent=self)
            return

        password = self.password_var.get()
        if not password:
            messagebox.showwarning("Passwort fehlt", "Bitte Passwort eingeben (wird nicht gespeichert).", parent=self)
            return

        # We'll connect temporarily with password even if the profile is currently 'key'.
        tmp = ConnectionProfile(
            name=profile.name,
            host=profile.host,
            port=profile.port,
            username=profile.username,
            auth_mode="password",
            key_path="",
        )

        event = threading.Event()
        decision = {"ok": False}

        def trust_cb_ui(host_id: str, fp: str) -> bool:
            def _ask() -> None:
                decision["ok"] = ask_trust_hostkey(self, host_id, fp)
                event.set()

            self.after(0, _ask)
            event.wait()
            return decision["ok"]

        def _worker() -> None:
            try:
                import paramiko

                self.after(0, lambda: self.log.append_line("[local] connecting (password) to install public key…"))
                self.app.ssh.connect(tmp, password=password, trust_callback=trust_cb_ui)

                # Load key and construct public line
                key = paramiko.RSAKey.from_private_key_file(key_path)
                pub_line = f"{key.get_name()} {key.get_base64()} jellyfin-organizer"

                # Prepare remote paths
                home_res = self.app.ssh.exec_command('printf "%s" "$HOME"')
                home = (home_res.stdout or "").strip() or "/"  # fallback
                ssh_dir = f"{home}/.ssh"
                auth_keys = f"{ssh_dir}/authorized_keys"

                # Ensure directory and permissions
                self.app.ssh.exec_command("mkdir -p ~/.ssh && chmod 700 ~/.ssh")

                sftp = self.app.ssh.open_sftp()
                existing = ""
                try:
                    with sftp.open(auth_keys, "r") as f:
                        existing = f.read().decode("utf-8", errors="replace")
                except Exception:
                    existing = ""

                if pub_line in existing:
                    self.after(0, lambda: self.log.append_line("[local] public key already present in authorized_keys"))
                else:
                    new_content = existing
                    if new_content and not new_content.endswith("\n"):
                        new_content += "\n"
                    new_content += pub_line + "\n"
                    with sftp.open(auth_keys, "w") as f:
                        f.write(new_content.encode("utf-8"))
                    try:
                        sftp.chmod(auth_keys, 0o600)
                    except Exception:
                        pass
                    self.after(0, lambda: self.log.append_line("[local] public key appended to authorized_keys"))

                try:
                    sftp.close()
                except Exception:
                    pass

                self.after(0, lambda: messagebox.showinfo(
                    "Public Key installiert",
                    "Der Public Key wurde in ~/.ssh/authorized_keys installiert (falls nicht vorhanden).\n\nDu kannst jetzt auf 'SSH-Key / Agent' umstellen.",
                    parent=self,
                ))
            except Exception as exc:  # noqa: BLE001
                self.after(0, lambda: self.log.append_line(f"[local] install pubkey ERROR: {exc}"))
                self.after(0, lambda: messagebox.showerror("Fehler", str(exc), parent=self))
            finally:
                try:
                    self.app.ssh.disconnect()
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    def _ssh_cmd_preview(self) -> str:
        host = self.host.get()
        user = self.user.get()
        port = self.port.get() or "22"
        if not host or not user:
            return "(Bitte Host und Username setzen)"

        cmd = f"ssh {user}@{host} -p {port}"
        if self.auth_mode.get() == "key":
            kp = self.key_path_var.get().strip()
            if kp:
                # Use double quotes to be more compatible with PowerShell.
                cmd += f" -i \"{kp}\""
        return cmd

    def _update_cmd(self) -> None:
        # Show/hide auth-specific widgets (for clarity)
        mode = self.auth_mode.get()
        if mode == "password":
            # Password fields visible
            try:
                self.pw_frm.pack(side=tk.LEFT)
            except Exception:
                pass
            # Hide key widgets
            try:
                self.key_frm.pack_forget()
                self.key2_frm.pack_forget()
            except Exception:
                pass
        else:
            # Key widgets visible
            try:
                self.key_frm.pack(fill=tk.X, pady=2)
                self.key2_frm.pack(fill=tk.X, pady=2)
            except Exception:
                pass
            # Hide password widget
            try:
                self.pw_frm.pack_forget()
            except Exception:
                pass
        self.out.set_text(self._ssh_cmd_preview())

    def _disconnect(self) -> None:
        self.app.ssh.disconnect()
        self.log.append_line("[local] disconnected")

    def _open_powershell(self) -> None:
        cmd = self._ssh_cmd_preview()
        if cmd.startswith("("):
            return

        if platform.system().lower() == "windows":
            try:
                subprocess.Popen(["powershell.exe", "-NoExit", "-Command", cmd])
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Fehler", str(exc), parent=self)
        else:
            # Cross-platform: show the command
            messagebox.showinfo("SSH Command", cmd, parent=self)

    def _test_connection(self) -> None:
        self._save_settings()
        self.log.append_line("[local] testing connection...")
        t = threading.Thread(target=self._worker_test, daemon=True)
        t.start()

    def _connect(self) -> None:
        self._save_settings()
        self.log.append_line("[local] connecting...")
        t = threading.Thread(target=self._worker_connect, daemon=True)
        t.start()

    def _worker_connect(self) -> None:
        profile = self.app.settings.get_active_profile()

        def trust_cb(host_id: str, fp: str) -> bool:
            # Must run in UI thread
            result_box = {"ok": False}

            def _ask():
                result_box["ok"] = ask_trust_hostkey(self, host_id, fp)

            self.after(0, _ask)
            # wait
            while "ok" not in result_box:
                pass
            # The above doesn't work; we do a simple event instead.
            return result_box.get("ok", False)

        # UI thread callback via event
        event = threading.Event()
        decision = {"ok": False}

        def trust_cb_ui(host_id: str, fp: str) -> bool:
            def _ask() -> None:
                decision["ok"] = ask_trust_hostkey(self, host_id, fp)
                event.set()

            self.after(0, _ask)
            event.wait()
            return decision["ok"]

        password = None
        key_passphrase = None
        if profile.auth_mode == "password":
            password = self.password_var.get()
            if not password:
                self.after(0, lambda: self.log.append_line("[local] connect cancelled: missing password"))
                return
        else:
            key_passphrase = self.key_passphrase_var.get().strip() or None

        try:
            self.app.ssh.connect(profile, password=password, key_passphrase=key_passphrase, trust_callback=trust_cb_ui)
            self.after(0, lambda: self.log.append_line("[local] connected"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] connect ERROR: {exc}"))

    def _worker_test(self) -> None:
        profile = self.app.settings.get_active_profile()

        event = threading.Event()
        decision = {"ok": False}

        def trust_cb_ui(host_id: str, fp: str) -> bool:
            def _ask() -> None:
                decision["ok"] = ask_trust_hostkey(self, host_id, fp)
                event.set()

            self.after(0, _ask)
            event.wait()
            return decision["ok"]

        password = None
        key_passphrase = None
        if profile.auth_mode == "password":
            password = self.password_var.get()
            if not password:
                self.after(0, lambda: self.log.append_line("[local] test cancelled: missing password"))
                return
        else:
            key_passphrase = self.key_passphrase_var.get().strip() or None

        try:
            self.app.ssh.connect(profile, password=password, key_passphrase=key_passphrase, trust_callback=trust_cb_ui)
            res = self.app.ssh.exec_command("uname -a && id")
            self.after(0, lambda: self.log.append_line(res.stdout.strip() or "(no stdout)"))
            if res.stderr.strip():
                self.after(0, lambda: self.log.append_line("STDERR: " + res.stderr.strip()))
            self.after(0, lambda: self.log.append_line(f"Exit: {res.exit_status}"))
        except Exception as exc:  # noqa: BLE001
            self.after(0, lambda: self.log.append_line(f"[local] test ERROR: {exc}"))
