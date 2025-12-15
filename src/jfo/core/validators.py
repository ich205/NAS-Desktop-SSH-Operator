from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable, List


class SandboxViolation(ValueError):
    pass


@dataclass(frozen=True)
class Sandbox:
    """Client-side root sandbox for remote POSIX paths.

    This is a *preventive* check. The generated Bash script re-checks roots at runtime.
    """

    allowed_roots: List[str]

    def normalized_roots(self) -> List[str]:
        roots: List[str] = []
        for r in self.allowed_roots:
            if not r:
                continue
            rp = str(PurePosixPath(r))
            if not rp.startswith("/"):
                # Treat relative roots as invalid
                continue
            if not rp.endswith("/"):
                rp = rp + "/"
            roots.append(rp)
        return roots

    def assert_path_allowed(self, path: str) -> None:
        p = str(PurePosixPath(path))
        if not p.startswith("/"):
            raise SandboxViolation(f"Remote path must be absolute: {path}")

        # Basic traversal guard (still not perfect without realpath)
        parts = PurePosixPath(p).parts
        if ".." in parts:
            raise SandboxViolation(f"Remote path contains '..' traversal: {path}")

        roots = self.normalized_roots()
        if not roots:
            raise SandboxViolation(
                "No allowed roots configured. Set at least one root in Main tab (Root-Sandbox)."
            )
        for root in roots:
            if p.startswith(root):
                return
        raise SandboxViolation(f"Path is outside allowed roots: {path}")

    def assert_all(self, paths: Iterable[str]) -> None:
        for p in paths:
            self.assert_path_allowed(p)
