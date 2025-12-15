from __future__ import annotations

import base64
import hashlib
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple

import paramiko
from platformdirs import user_config_dir

from jfo.infra.settings import APP_NAME, ConnectionProfile


def _known_hosts_path() -> Path:
    base = Path(user_config_dir(APP_NAME))
    base.mkdir(parents=True, exist_ok=True)
    return base / "known_hosts"


def _host_id(host: str, port: int) -> str:
    """OpenSSH-compatible host key identifier.

    For non-standard ports, OpenSSH stores keys as: [host]:port
    """

    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def _fingerprint_sha256(key: paramiko.PKey) -> str:
    # OpenSSH-like SHA256 fingerprint
    h = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(h).decode("ascii").rstrip("=")


class HostKeyNotTrusted(Exception):
    def __init__(self, host: str, fingerprint: str):
        super().__init__(f"Host key for {host} not trusted. Fingerprint: {fingerprint}")
        self.host = host
        self.fingerprint = fingerprint


@dataclass
class ExecResult:
    exit_status: int
    stdout: str
    stderr: str


class SshManager:
    """Stateful SSH manager.

    - Loads/saves a dedicated known_hosts file under the app config dir.
    - Can perform interactive trust-first connection (GUI must confirm).
    """

    def __init__(self) -> None:
        self._client: Optional[paramiko.SSHClient] = None
        self._profile: Optional[ConnectionProfile] = None

    def is_connected(self) -> bool:
        return self._client is not None

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._profile = None

    def get_connected_profile(self) -> Optional[ConnectionProfile]:
        return self._profile

    def ensure_host_trusted(
        self,
        profile: ConnectionProfile,
        *,
        password: Optional[str] = None,
        key_passphrase: Optional[str] = None,
        trust_callback: Optional[Callable[[str, str], bool]] = None,
        timeout: float = 10.0,
    ) -> None:
        """Ensure the host key is in known_hosts.

        If unknown, fetch fingerprint and ask trust_callback(host, fingerprint)->bool.
        """

        host = profile.host
        port = int(profile.port)
        hid = _host_id(host, port)

        hostkeys = paramiko.HostKeys()
        kh_path = _known_hosts_path()
        if kh_path.exists():
            hostkeys.load(str(kh_path))

        # Host is known already?
        if hid in hostkeys:
            return

        # Fetch remote server key (no trust yet)
        transport = paramiko.Transport((host, port))
        try:
            transport.start_client(timeout=timeout)
            key = transport.get_remote_server_key()
            fp = _fingerprint_sha256(key)
        finally:
            try:
                transport.close()
            except Exception:
                pass

        if trust_callback is None:
            raise HostKeyNotTrusted(hid, fp)

        if not trust_callback(hid, fp):
            raise HostKeyNotTrusted(hid, fp)

        # Persist
        hostkeys.add(hid, key.get_name(), key)
        hostkeys.save(str(kh_path))

    def connect(
        self,
        profile: ConnectionProfile,
        *,
        password: Optional[str] = None,
        key_passphrase: Optional[str] = None,
        trust_callback: Optional[Callable[[str, str], bool]] = None,
        timeout: float = 10.0,
    ) -> None:
        self.disconnect()

        self.ensure_host_trusted(
            profile,
            password=password,
            key_passphrase=key_passphrase,
            trust_callback=trust_callback,
            timeout=timeout,
        )

        kh_path = _known_hosts_path()

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        if kh_path.exists():
            client.load_host_keys(str(kh_path))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

        kwargs = {
            "hostname": profile.host,
            "port": int(profile.port),
            "username": profile.username,
            "timeout": timeout,
            "banner_timeout": timeout,
            "auth_timeout": timeout,
            "look_for_keys": False,
            "allow_agent": True,
        }

        if profile.auth_mode == "password":
            if not password:
                raise ValueError("Password auth selected but no password provided.")
            # When the user explicitly chose password auth, do not try agent keys first.
            # This avoids confusion and speeds up login on NAS devices with no keys configured.
            kwargs["allow_agent"] = False
            kwargs["password"] = password
        else:
            if profile.key_path:
                kwargs["key_filename"] = profile.key_path
                if key_passphrase:
                    kwargs["passphrase"] = key_passphrase
            else:
                # Allow agent / default keys
                kwargs["look_for_keys"] = True

        client.connect(**kwargs)

        self._client = client
        self._profile = profile

    def exec_command(self, command: str, *, timeout: float = 60.0) -> ExecResult:
        if self._client is None:
            raise RuntimeError("Not connected")
        stdin, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_status = stdout.channel.recv_exit_status()
        return ExecResult(exit_status=exit_status, stdout=out, stderr=err)

    def open_sftp(self) -> paramiko.SFTPClient:
        if self._client is None:
            raise RuntimeError("Not connected")
        return self._client.open_sftp()

    def exec_bash_script_streaming(
        self,
        script_text: str,
        *,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
        timeout: float = 3600.0,
        cancel_event: Optional[threading.Event] = None,
    ) -> int:
        """Execute `bash -s` and stream stdout/stderr line-by-line.

        Returns remote exit status.
        """

        if self._client is None:
            raise RuntimeError("Not connected")

        transport = self._client.get_transport()
        if transport is None:
            raise RuntimeError("SSH transport not available")

        chan = transport.open_session(timeout=timeout)
        # No pty by default (deterministic). You can enable if you want different buffering.
        chan.exec_command("bash -s")

        # Send script via stdin
        chan.sendall(script_text.encode("utf-8"))
        chan.shutdown_write()

        def _pump(kind: str) -> None:
            buf = b""
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        chan.close()
                    except Exception:
                        pass
                    return

                # Decide which stream to read
                if kind == "stdout":
                    if chan.recv_ready():
                        chunk = chan.recv(4096)
                    elif chan.exit_status_ready():
                        chunk = b""
                    else:
                        time.sleep(0.05)
                        continue
                else:
                    if chan.recv_stderr_ready():
                        chunk = chan.recv_stderr(4096)
                    elif chan.exit_status_ready():
                        chunk = b""
                    else:
                        time.sleep(0.05)
                        continue

                if not chunk:
                    # Flush remaining lines
                    if buf:
                        text = buf.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            (on_stdout if kind == "stdout" else on_stderr)(line)
                        buf = b""
                    return

                buf += chunk
                # Emit complete lines
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace")
                    (on_stdout if kind == "stdout" else on_stderr)(text)

        t_out = threading.Thread(target=_pump, args=("stdout",), daemon=True)
        t_err = threading.Thread(target=_pump, args=("stderr",), daemon=True)
        t_out.start()
        t_err.start()

        # Wait for completion
        while not chan.exit_status_ready():
            if cancel_event is not None and cancel_event.is_set():
                try:
                    chan.close()
                except Exception:
                    pass
                break
            time.sleep(0.05)

        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)

        return chan.recv_exit_status() if chan.exit_status_ready() else 255
