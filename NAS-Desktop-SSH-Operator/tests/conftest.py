from __future__ import annotations

"""Pytest configuration.

The application code lives in the local "src" folder. For development and CI we want
`pytest` to work without requiring a prior `pip install -e .`.
"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
