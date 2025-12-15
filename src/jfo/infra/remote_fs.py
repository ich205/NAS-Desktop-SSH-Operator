from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from jfo.core.quoting import bash_quote
from jfo.infra.ssh_client import SshManager


@dataclass(frozen=True)
class Mountpoint:
    filesystem: str
    size: str
    used: str
    avail: str
    use_percent: str
    target: str


def list_mountpoints(ssh: SshManager) -> list[Mountpoint]:
    """Return mountpoints via `df -P -h`.

    We intentionally use `-P` (POSIX) to keep one mount per line.
    """

    res = ssh.exec_command("df -P -h")
    if res.exit_status != 0:
        raise RuntimeError(res.stderr.strip() or f"df failed (exit {res.exit_status})")

    lines = [ln.rstrip("\r") for ln in res.stdout.splitlines() if ln.strip()]
    if not lines:
        return []

    # header: Filesystem Size Used Avail Use% Mounted on
    out: list[Mountpoint] = []
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) < 6:
            continue
        filesystem, size, used, avail, use_percent = parts[0], parts[1], parts[2], parts[3], parts[4]
        target = parts[-1]
        out.append(
            Mountpoint(
                filesystem=filesystem,
                size=size,
                used=used,
                avail=avail,
                use_percent=use_percent,
                target=target,
            )
        )
    return out


def list_directories(ssh: SshManager, path: str) -> list[str]:
    """List directory names (not full paths) inside `path`.

    Uses `ls -1Ap` and filters entries that end with '/'.
    This avoids traversing the full tree and is typically fast enough.
    """

    # -A: no '.' or '..'
    # -p: append '/' to directories
    # -1: one per line
    cmd = f"ls -1Ap -- {bash_quote(path)} 2>/dev/null | sed -n 's:/$::p' | sort"
    res = ssh.exec_command(cmd)
    if res.exit_status != 0:
        # If ls failed, show stderr if present
        msg = res.stderr.strip() or f"ls failed (exit {res.exit_status})"
        raise RuntimeError(msg)

    dirs: list[str] = []
    for ln in res.stdout.splitlines():
        ln = ln.strip("\r\n")
        if not ln:
            continue
        dirs.append(ln)
    return dirs


def is_dir(ssh: SshManager, path: str) -> bool:
    cmd = f"test -d -- {bash_quote(path)}"
    res = ssh.exec_command(cmd)
    return res.exit_status == 0


def normalize_posix_path(raw: str) -> str:
    """Normalize user-typed POSIX paths (best-effort, client-side)."""

    if not raw:
        return "/"
    p = str(PurePosixPath(raw.strip()))
    if not p.startswith("/"):
        # Keep it as-is; caller can reject
        return p
    return p


def parent_dir(path: str) -> str:
    p = PurePosixPath(path)
    parent = str(p.parent)
    return parent if parent else "/"
