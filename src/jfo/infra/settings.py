from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from platformdirs import user_config_dir


APP_NAME = "JellyfinOrganizer"


def _config_path() -> Path:
    base = Path(user_config_dir(APP_NAME))
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


@dataclass
class ConnectionProfile:
    name: str = "default"
    host: str = ""
    port: int = 22
    username: str = ""
    # Default to password because it's the most common initial setup on consumer NAS.
    # Users can switch to key-based auth (recommended) at any time.
    auth_mode: str = "password"  # 'key' or 'password'
    key_path: str = ""
    # Password is not persisted by default.


@dataclass
class AppSettings:
    profiles: List[ConnectionProfile] = field(default_factory=lambda: [ConnectionProfile()])
    active_profile: str = "default"

    allowed_roots: List[str] = field(default_factory=list)
    default_dry_run: bool = True
    no_overwrite: bool = True
    mass_confirm_threshold: int = 200

    naming_template: str = "{title} ({year}) [imdbid-{imdbid}]"
    video_exts: List[str] = field(default_factory=lambda: ["mkv", "mp4", "avi", "mov"])
    sidecar_exts: List[str] = field(default_factory=lambda: [
        "nfo",
        "jpg",
        "jpeg",
        "png",
        "webp",
        "srt",
        "ass",
        "ssa",
        "sub",
        "idx",
    ])

    def get_active_profile(self) -> ConnectionProfile:
        for p in self.profiles:
            if p.name == self.active_profile:
                return p
        return self.profiles[0]


def load_settings() -> AppSettings:
    path = _config_path()
    if not path.exists():
        s = AppSettings()
        save_settings(s)
        return s

    data = json.loads(path.read_text(encoding="utf-8"))

    profiles = []
    for p in data.get("profiles", []):
        profiles.append(ConnectionProfile(**p))

    s = AppSettings(
        profiles=profiles or [ConnectionProfile()],
        active_profile=data.get("active_profile", "default"),
        allowed_roots=data.get("allowed_roots", []),
        default_dry_run=bool(data.get("default_dry_run", True)),
        no_overwrite=bool(data.get("no_overwrite", True)),
        mass_confirm_threshold=int(data.get("mass_confirm_threshold", 200)),
        naming_template=data.get("naming_template", "{title} ({year}) [imdbid-{imdbid}]"),
        video_exts=data.get("video_exts", ["mkv", "mp4", "avi", "mov"]),
        sidecar_exts=data.get(
            "sidecar_exts",
            ["nfo", "jpg", "jpeg", "png", "webp", "srt", "ass", "ssa", "sub", "idx"],
        ),
    )

    return s


def save_settings(settings: AppSettings) -> None:
    path = _config_path()

    def _profile_to_dict(p: ConnectionProfile) -> Dict[str, Any]:
        d = asdict(p)
        # No secrets
        return d

    payload: Dict[str, Any] = {
        "profiles": [_profile_to_dict(p) for p in settings.profiles],
        "active_profile": settings.active_profile,
        "allowed_roots": settings.allowed_roots,
        "default_dry_run": settings.default_dry_run,
        "no_overwrite": settings.no_overwrite,
        "mass_confirm_threshold": settings.mass_confirm_threshold,
        "naming_template": settings.naming_template,
        "video_exts": settings.video_exts,
        "sidecar_exts": settings.sidecar_exts,
    }

    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
