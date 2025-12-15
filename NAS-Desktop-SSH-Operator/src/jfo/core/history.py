from __future__ import annotations

"""History / undo helpers.

This module is used by the History tab to:
- reconstruct executed operations from journal records (prefer stored ops; fall back to script parsing)
- generate an *undo plan* for mv/rename operations

Safety philosophy:
- Undo is best-effort and conservative.
- By default we only undo MOVE/RENAME operations (no deletes).
- Undo operations are generated in reverse order to handle dependent renames
  (e.g. rename files, then rename folder).
"""

import shlex
from typing import Any, Iterable, List, Tuple

from jfo.core.operations import Operation, OperationKind
from jfo.core.plan import Plan


def ops_to_journal_dicts(ops: Iterable[Operation]) -> List[dict[str, Any]]:
    """Serialize operations for storage inside journal.jsonl."""

    out: List[dict[str, Any]] = []
    for op in ops:
        out.append(
            {
                "kind": op.kind.value,
                "src": op.src,
                "dst": op.dst,
                "detail": op.detail,
                "warning": op.warning,
            }
        )
    return out


def ops_from_journal(record: dict[str, Any]) -> List[Operation]:
    """Load executed operations from a journal record.

    - Prefer the explicit 'ops' field (added in newer versions).
    - Fallback to parsing the 'script' for older journal entries.
    """

    raw = record.get("ops")
    if isinstance(raw, list) and raw:
        ops: List[Operation] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind_s = str(item.get("kind") or "")
            try:
                kind = OperationKind(kind_s)
            except Exception:
                # best-effort mapping from script-derived kinds
                if kind_s == "mv":
                    kind = OperationKind.MOVE
                elif kind_s == "mkdir":
                    kind = OperationKind.MKDIR
                elif kind_s == "ln":
                    kind = OperationKind.LINK
                elif kind_s == "cp":
                    kind = OperationKind.COPY
                else:
                    continue
            ops.append(
                Operation(
                    kind=kind,
                    src=item.get("src"),
                    dst=item.get("dst"),
                    detail=str(item.get("detail") or ""),
                    warning=str(item.get("warning") or ""),
                    selected=True,
                )
            )
        return ops

    # Fallback: parse from script
    script = str(record.get("script") or "")
    return parse_ops_from_script(script)


def parse_ops_from_script(script: str) -> List[Operation]:
    """Best-effort parse of selected operations from a generated bash script.

    We intentionally parse only the helper calls we generate:
      - safe_mv SRC DST
      - safe_ln SRC DST
      - safe_cp SRC DST
      - safe_mkdir DIR

    We use shlex.split() to correctly interpret concatenated shell quotes,
    including the common bash-quote pattern: 'foo'"'"'bar'.
    """

    ops: List[Operation] = []
    for raw_line in script.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not (line.startswith("safe_") or line.startswith("run ")):
            continue

        try:
            parts = shlex.split(line, posix=True)
        except Exception:
            continue
        if not parts:
            continue

        cmd = parts[0]
        if cmd == "safe_mv" and len(parts) >= 3:
            ops.append(Operation(kind=OperationKind.MOVE, src=parts[1], dst=parts[2], selected=True))
        elif cmd == "safe_ln" and len(parts) >= 3:
            ops.append(Operation(kind=OperationKind.LINK, src=parts[1], dst=parts[2], selected=True))
        elif cmd == "safe_cp" and len(parts) >= 3:
            ops.append(Operation(kind=OperationKind.COPY, src=parts[1], dst=parts[2], selected=True))
        elif cmd == "safe_mkdir" and len(parts) >= 2:
            ops.append(Operation(kind=OperationKind.MKDIR, src=None, dst=parts[1], selected=True))

    return ops


def build_undo_plan(
    executed_ops: List[Operation],
    *,
    title: str = "Undo",
    only_mv_rename: bool = True,
) -> Tuple[Plan, List[str]]:
    """Create an undo plan from executed operations.

    Returns (plan, skipped_messages).

    - Undo is generated in reverse order.
    - MOVE/RENAME become MOVE with src/dst swapped.
    - Other operations are skipped by default.
    """

    undo_ops: List[Operation] = []
    skipped: List[str] = []

    for op in reversed(executed_ops):
        if op.kind in (OperationKind.MOVE, OperationKind.RENAME):
            if not op.src or not op.dst:
                continue
            undo_ops.append(
                Operation(
                    kind=OperationKind.MOVE,
                    src=op.dst,
                    dst=op.src,
                    detail=("UNDO " + (op.detail or "")).strip(),
                    selected=True,
                )
            )
            continue

        if op.kind == OperationKind.MKDIR:
            # We do not remove directories automatically.
            continue

        if only_mv_rename:
            skipped.append(f"Skip undo for {op.kind.value}: {op.src or ''} -> {op.dst or ''}")
            continue

        # Future extension point (dangerous): could implement safe_rm for LINK/COPY.
        skipped.append(f"Unsupported undo op: {op.kind.value}")

    plan = Plan(title=title)
    plan.extend(undo_ops)
    plan.apply_collision_warnings()
    for m in skipped:
        plan.add_warning(m)
    return plan, skipped
