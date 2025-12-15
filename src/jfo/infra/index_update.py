from __future__ import annotations

"""SQLite analysis-index updater.

The GUI uses a local SQLite index (built by "Scan / Index") for fast search and
selection. After executing plans (rename/move/link/copy) on the NAS, the index
would otherwise still contain stale paths until the next scan.

This module updates the index *optimistically* when a plan completed successfully.

Design goals:
- Never block execution of the main workflow (index update is best-effort).
- Handle common cases efficiently (tens of thousands of moved files).
- Support directory renames (prefix rewrite) in addition to file renames.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Iterable, Sequence

from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan

from .sqlite_index import connect, distinct_roots, init_db


def _split_remote_path(path: str) -> tuple[str, str, str]:
    """Return (dir, name, ext) for a POSIX path."""

    parts = path.rsplit("/", 1)
    if len(parts) == 1:
        d = "/"
        name = parts[0]
    else:
        d, name = parts
        if not d:
            d = "/"
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return d, name, ext


def _normalize_root_marker(root: str) -> str:
    r = str(PurePosixPath(root)).rstrip("/")
    return r if r else "/"


def _pick_root_for_path(path: str, roots: Sequence[str]) -> str | None:
    """Pick the best (longest) scanned-root marker that contains the given path."""

    p = str(PurePosixPath(path))
    for r in roots:
        rr = _normalize_root_marker(r)
        if p == rr or p.startswith(rr + "/"):
            return r
    return None


@dataclass
class IndexUpdateStats:
    inserted: int = 0
    deleted: int = 0
    updated_prefix: int = 0


def apply_plan_to_index(
    plan: Plan,
    *,
    roots_hint: Sequence[str] | None = None,
) -> IndexUpdateStats:
    """Apply selected operations to the local analysis index.

    This is best-effort and should only be called after a *successful real run*
    (exit_code == 0 and dry_run == False).
    """

    init_db()
    ts = int(datetime.now(timezone.utc).timestamp())

    # Root markers: prefer the current DB roots; fall back to hints.
    roots = list(distinct_roots(limit=5000))
    if roots_hint:
        for r in roots_hint:
            if r and r not in roots:
                roots.append(r)
    # Prefer longer roots first.
    roots.sort(key=lambda x: len(_normalize_root_marker(x)), reverse=True)

    stats = IndexUpdateStats()

    conn = connect()
    try:
        conn.execute("BEGIN")

        # Prepared statements
        sel_root = "SELECT root FROM files WHERE path=?"
        sel_any_prefix = "SELECT 1 FROM files WHERE path LIKE ? LIMIT 1"
        del_path = "DELETE FROM files WHERE path=?"
        upsert = (
            "INSERT INTO files(path, dir, name, ext, root, scanned_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET dir=excluded.dir, name=excluded.name, ext=excluded.ext, root=excluded.root, scanned_at=excluded.scanned_at"
        )
        upd_prefix = (
            "UPDATE files "
            "SET "
            "  path = REPLACE(path, ?, ?), "
            "  dir  = CASE WHEN dir = ? THEN ? ELSE REPLACE(dir, ?, ?) END, "
            "  scanned_at = ? "
            "WHERE path LIKE ?"
        )

        for op in (o for o in plan.operations if o.selected):
            kind = op.kind
            src = op.src
            dst = op.dst

            if kind in (OperationKind.MOVE, OperationKind.RENAME):
                if not src or not dst:
                    continue

                # 1) If src is a known file path in the index, treat as file move/rename.
                cur = conn.execute(sel_root, (src,))
                row = cur.fetchone()
                if row is not None:
                    old_root = row[0]

                    conn.execute(del_path, (src,))
                    stats.deleted += 1

                    root_for_dst = _pick_root_for_path(dst, roots) or old_root
                    d, name, ext = _split_remote_path(dst)
                    conn.execute(upsert, (dst, d, name, ext, root_for_dst, ts))
                    stats.inserted += 1
                    continue

                # 2) If src is a directory rename, rewrite prefix for all contained files.
                src_dir = str(PurePosixPath(src)).rstrip("/")
                dst_dir = str(PurePosixPath(dst)).rstrip("/")
                if src_dir and dst_dir and src_dir != dst_dir:
                    src_prefix = src_dir + "/"
                    dst_prefix = dst_dir + "/"
                    # Only apply if there are any indexed files under that prefix.
                    cur2 = conn.execute(sel_any_prefix, (src_prefix + "%",))
                    if cur2.fetchone() is not None:
                        res = conn.execute(
                            upd_prefix,
                            (src_prefix, dst_prefix, src_dir, dst_dir, src_prefix, dst_prefix, ts, src_prefix + "%"),
                        )
                        # rowcount can be -1 on some drivers, but with sqlite it should be fine.
                        try:
                            stats.updated_prefix += int(res.rowcount or 0)
                        except Exception:
                            pass
                continue

            if kind in (OperationKind.COPY, OperationKind.LINK):
                if not dst:
                    continue
                # For copy/link we insert the destination path.
                root_for_dst = _pick_root_for_path(dst, roots) or (_pick_root_for_path(src or "", roots) if src else None) or ""
                d, name, ext = _split_remote_path(dst)
                conn.execute(upsert, (dst, d, name, ext, root_for_dst, ts))
                stats.inserted += 1
                continue

            # MKDIR does not affect the file index.

        conn.commit()
        return stats
    finally:
        try:
            conn.close()
        except Exception:
            pass
