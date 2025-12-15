from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from platformdirs import user_data_dir

from jfo.infra.settings import APP_NAME


def _db_path() -> Path:
    base = Path(user_data_dir(APP_NAME))
    base.mkdir(parents=True, exist_ok=True)
    return base / "analysis.sqlite"


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    dir TEXT NOT NULL,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    root TEXT NOT NULL,
    scanned_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def upsert_paths(paths: Sequence[str], *, root: str) -> int:
    """Upsert file paths into the index. Returns inserted/updated count."""

    init_db()
    ts = int(datetime.now(timezone.utc).timestamp())

    rows: List[Tuple[str, str, str, str, str, int]] = []
    for p in paths:
        # We store remote POSIX path strings.
        parts = p.rsplit("/", 1)
        if len(parts) == 1:
            d = "/"
            name = parts[0]
        else:
            d, name = parts
            if d == "":
                d = "/"
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        rows.append((p, d, name, ext, root, ts))

    conn = connect()
    try:
        conn.executemany(
            "INSERT INTO files(path, dir, name, ext, root, scanned_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET dir=excluded.dir, name=excluded.name, ext=excluded.ext, root=excluded.root, scanned_at=excluded.scanned_at",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def distinct_dirs(prefix: str = "", *, limit: int = 200) -> List[str]:
    init_db()
    conn = connect()
    try:
        if prefix:
            cur = conn.execute(
                "SELECT DISTINCT dir FROM files WHERE dir LIKE ? ORDER BY dir LIMIT ?",
                (prefix + "%", limit),
            )
        else:
            cur = conn.execute("SELECT DISTINCT dir FROM files ORDER BY dir LIMIT ?", (limit,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def distinct_roots(*, limit: int = 200) -> List[str]:
    """Return a list of scanned root markers."""
    init_db()
    conn = connect()
    try:
        cur = conn.execute("SELECT DISTINCT root FROM files ORDER BY root LIMIT ?", (limit,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def distinct_dirs_for_root(root: str, prefix: str = "", *, limit: int = 500) -> List[str]:
    """Return distinct directories limited to a given root marker."""
    init_db()
    conn = connect()
    try:
        if prefix:
            cur = conn.execute(
                "SELECT DISTINCT dir FROM files WHERE root=? AND dir LIKE ? ORDER BY dir LIMIT ?",
                (root, prefix + "%", limit),
            )
        else:
            cur = conn.execute(
                "SELECT DISTINCT dir FROM files WHERE root=? ORDER BY dir LIMIT ?",
                (root, limit),
            )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def search_files_for_root(
    root: str,
    term: str,
    *,
    exts: Optional[Iterable[str]] = None,
    limit: int = 200,
) -> List[Tuple[str, str, str, str]]:
    """Search files (path, dir, name, ext) for a given root marker."""
    init_db()
    conn = connect()
    try:
        like = "%" + term + "%"
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path, dir, name, ext FROM files WHERE root=? AND (name LIKE ? OR path LIKE ? OR dir LIKE ?) AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (root, like, like, like, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path, dir, name, ext FROM files WHERE root=? AND (name LIKE ? OR path LIKE ? OR dir LIKE ?) ORDER BY path LIMIT ?",
                (root, like, like, like, limit),
            )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    finally:
        conn.close()


def search_files_any_root(
    term: str,
    *,
    exts: Optional[Iterable[str]] = None,
    limit: int = 200,
) -> List[Tuple[str, str, str, str, str]]:
    """Search across all roots.

    Returns (path, dir, name, ext, root).
    """
    init_db()
    conn = connect()
    try:
        like = "%" + term + "%"
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path, dir, name, ext, root FROM files WHERE (name LIKE ? OR path LIKE ? OR dir LIKE ?) AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (like, like, like, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path, dir, name, ext, root FROM files WHERE (name LIKE ? OR path LIKE ? OR dir LIKE ?) ORDER BY path LIMIT ?",
                (like, like, like, limit),
            )
        return [(r[0], r[1], r[2], r[3], r[4]) for r in cur.fetchall()]
    finally:
        conn.close()


def export_root_to_csv(root: str, out_path: str) -> int:
    """Export all indexed rows for a root to a CSV file.

    Returns number of exported rows.
    """
    import csv

    init_db()
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT path, dir, name, ext, root, scanned_at FROM files WHERE root=? ORDER BY path",
            (root,),
        )
        rows = cur.fetchall()
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path", "dir", "name", "ext", "root", "scanned_at"])
            for r in rows:
                w.writerow(r)
        return len(rows)
    finally:
        conn.close()


def export_root_to_jsonl(root: str, out_path: str) -> int:
    """Export all indexed rows for a root to a JSONL file."""
    import json

    init_db()
    conn = connect()
    try:
        cur = conn.execute(
            "SELECT path, dir, name, ext, root, scanned_at FROM files WHERE root=? ORDER BY path",
            (root,),
        )
        rows = cur.fetchall()
        with open(out_path, "w", encoding="utf-8") as f:
            for r in rows:
                obj = {
                    "path": r[0],
                    "dir": r[1],
                    "name": r[2],
                    "ext": r[3],
                    "root": r[4],
                    "scanned_at": r[5],
                }
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        return len(rows)
    finally:
        conn.close()


def search_files(term: str, *, limit: int = 200) -> List[str]:
    """Return file paths matching a substring on name or path."""
    init_db()
    conn = connect()
    try:
        like = "%" + term + "%"
        cur = conn.execute(
            "SELECT path FROM files WHERE name LIKE ? OR path LIKE ? ORDER BY path LIMIT ?",
            (like, like, limit),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def files_in_dir(dir_path: str, *, exts: Optional[Iterable[str]] = None) -> List[str]:
    init_db()
    conn = connect()
    try:
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path FROM files WHERE dir=? AND ext IN ({qmarks}) ORDER BY path",
                (dir_path, *exts_l),
            )
        else:
            cur = conn.execute("SELECT path FROM files WHERE dir=? ORDER BY path", (dir_path,))
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def files_in_dir_for_root(root: str, dir_path: str, *, exts: Optional[Iterable[str]] = None, limit: int = 5000) -> List[Tuple[str, str, str, str]]:
    """Return (path, dir, name, ext) for a directory within a given root marker."""
    init_db()
    conn = connect()
    try:
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path, dir, name, ext FROM files WHERE root=? AND dir=? AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (root, dir_path, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path, dir, name, ext FROM files WHERE root=? AND dir=? ORDER BY path LIMIT ?",
                (root, dir_path, limit),
            )
        return [(r[0], r[1], r[2], r[3]) for r in cur.fetchall()]
    finally:
        conn.close()


def files_under_dir_recursive(dir_path: str, *, exts: Optional[Iterable[str]] = None, limit: int = 20000) -> List[str]:
    """Return file paths under a directory prefix using LIKE.

    Note: This is best-effort; it relies on the analysis index.
    """

    init_db()
    conn = connect()
    try:
        prefix = dir_path.rstrip("/") + "/%"
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path FROM files WHERE path LIKE ? AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (prefix, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path FROM files WHERE path LIKE ? ORDER BY path LIMIT ?",
                (prefix, limit),
            )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def files_under_dir_recursive_for_root(
    root: str,
    dir_path: str,
    *,
    exts: Optional[Iterable[str]] = None,
    limit: int = 20000,
) -> List[str]:
    """Return file paths under a directory prefix for a specific root marker."""
    init_db()
    conn = connect()
    try:
        prefix = dir_path.rstrip("/") + "/%"
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path FROM files WHERE root=? AND path LIKE ? AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (root, prefix, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path FROM files WHERE root=? AND path LIKE ? ORDER BY path LIMIT ?",
                (root, prefix, limit),
            )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def files_under_root(root: str, *, exts: Optional[Iterable[str]] = None, limit: int = 5000) -> List[str]:
    """Return all file paths under a scanned root (best-effort: by root marker)."""
    init_db()
    conn = connect()
    try:
        if exts:
            exts_l = [e.lower().lstrip(".") for e in exts]
            qmarks = ",".join("?" for _ in exts_l)
            cur = conn.execute(
                f"SELECT path FROM files WHERE root=? AND ext IN ({qmarks}) ORDER BY path LIMIT ?",
                (root, *exts_l, limit),
            )
        else:
            cur = conn.execute(
                "SELECT path FROM files WHERE root=? ORDER BY path LIMIT ?",
                (root, limit),
            )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def db_path() -> str:
    return str(_db_path())
