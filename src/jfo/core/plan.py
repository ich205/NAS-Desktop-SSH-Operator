from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Dict

from .operations import Operation


@dataclass
class Plan:
    """A plan is an ordered list of operations plus derived warnings.

    The plan is immutable-by-convention: tabs create a new Plan when inputs change.
    """

    title: str
    operations: List[Operation] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def selected_operations(self) -> List[Operation]:
        return [op for op in self.operations if op.selected]

    def count_selected(self) -> int:
        return sum(1 for op in self.operations if op.selected)

    def detect_destination_collisions(self) -> Dict[str, List[Operation]]:
        """Detect collisions where multiple selected operations target the same dst."""
        by_dst: Dict[str, List[Operation]] = {}
        for op in self.selected_operations():
            if not op.dst:
                continue
            by_dst.setdefault(op.dst, []).append(op)
        return {dst: ops for dst, ops in by_dst.items() if len(ops) > 1}

    def apply_collision_warnings(self) -> None:
        """Annotate operations with warnings when dst collides within the plan."""
        collisions = self.detect_destination_collisions()
        for dst, ops in collisions.items():
            for op in ops:
                op.warning = (op.warning + "; " if op.warning else "") + f"Collision: multiple ops target {dst}"
        if collisions:
            self.warnings.append(f"{len(collisions)} destination collision(s) detected inside the plan.")

    def add_warning(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def extend(self, ops: Iterable[Operation]) -> None:
        self.operations.extend(list(ops))
