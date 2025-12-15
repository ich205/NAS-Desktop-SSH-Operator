from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, Optional

from platformdirs import user_data_dir

from jfo.infra.settings import APP_NAME


def _journal_path() -> Path:
    base = Path(user_data_dir(APP_NAME))
    base.mkdir(parents=True, exist_ok=True)
    return base / "journal.jsonl"


def append_journal(record: Dict[str, Any]) -> None:
    path = _journal_path()
    record = dict(record)
    record.setdefault("timestamp_utc", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def journal_path() -> str:
    return str(_journal_path())
