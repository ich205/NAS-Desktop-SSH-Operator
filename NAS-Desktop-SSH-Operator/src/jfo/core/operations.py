from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class OperationKind(str, Enum):
    MKDIR = "mkdir"
    MOVE = "mv"
    RENAME = "rename"  # implemented via mv
    LINK = "ln"
    COPY = "cp"


@dataclass
class Operation:
    kind: OperationKind
    src: Optional[str] = None
    dst: Optional[str] = None
    # Free-form details for UI / logs
    detail: str = ""
    selected: bool = True
    warning: str = ""
    # A stable identifier (for UI selection persistence)
    op_id: str = field(default_factory=lambda: "")

    def display_src(self) -> str:
        return self.src or ""

    def display_dst(self) -> str:
        return self.dst or ""
