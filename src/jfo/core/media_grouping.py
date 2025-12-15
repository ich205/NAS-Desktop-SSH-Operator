from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class MediaFile:
    path: str

    @property
    def p(self) -> PurePosixPath:
        return PurePosixPath(self.path)

    @property
    def dir(self) -> str:
        return str(self.p.parent)

    @property
    def name(self) -> str:
        return self.p.name

    @property
    def suffix(self) -> str:
        return self.p.suffix.lower().lstrip(".")

    @property
    def stem(self) -> str:
        # PurePosixPath.stem only removes last suffix; we want full stem for video.
        return self.p.stem


@dataclass
class MediaGroup:
    """Group of related files (video + sidecars) in the same directory."""

    directory: str
    base_stem: str
    video: Optional[MediaFile] = None
    sidecars: List[MediaFile] = field(default_factory=list)
    # Optional: the NFO file (a sidecar) for convenience
    nfo: Optional[MediaFile] = None

    def all_files(self) -> List[MediaFile]:
        files: List[MediaFile] = []
        if self.video:
            files.append(self.video)
        files.extend(self.sidecars)
        # Ensure unique by path
        uniq: Dict[str, MediaFile] = {f.path: f for f in files}
        return list(uniq.values())

    def display_name(self) -> str:
        if self.video:
            return self.video.name
        return self.base_stem


DEFAULT_VIDEO_EXTS: Set[str] = {"mkv", "mp4", "avi", "mov"}
DEFAULT_SIDECAR_EXTS: Set[str] = {
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
}


# Jellyfin/Kodi folder-level artwork naming (common when each movie has its own folder).
# We only attach these to a video group when the directory contains exactly ONE video file.
FOLDER_LEVEL_SIDECAR_NAMES: Set[str] = {
    # artwork
    "poster.jpg",
    "poster.jpeg",
    "poster.png",
    "poster.webp",
    "fanart.jpg",
    "fanart.jpeg",
    "fanart.png",
    "backdrop.jpg",
    "backdrop.jpeg",
    "backdrop.png",
    "landscape.jpg",
    "landscape.jpeg",
    "landscape.png",
    "banner.jpg",
    "banner.jpeg",
    "banner.png",
    "logo.png",
    "logo.webp",
    "clearlogo.png",
    "clearlogo.webp",
    "clearart.png",
    "clearart.webp",
    "disc.png",
    "disc.webp",
    "thumb.jpg",
    "thumb.jpeg",
    "thumb.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
}


FOLDER_LEVEL_NFO_NAMES: Set[str] = {
    # common Kodi/Jellyfin folder-level nfo name for movies
    "movie.nfo",
}


def _stem_and_suffix(name: str) -> Tuple[str, str]:
    p = PurePosixPath(name)
    suf = p.suffix.lower().lstrip(".")
    return p.stem, suf


def group_media_files(
    paths: Sequence[str],
    *,
    video_exts: Set[str] = DEFAULT_VIDEO_EXTS,
    sidecar_exts: Set[str] = DEFAULT_SIDECAR_EXTS,
) -> List[MediaGroup]:
    """Best-effort grouping.

    Grouping rule (pragmatic for large libraries):

    - Only groups within the same directory.
    - Pick one *video* file as the primary per stem.
    - Any file whose name starts with `<video_stem>.` is treated as a sidecar and renamed together.
      Example: `Movie.en.srt`, `Movie-fanart.jpg`, `Movie.poster.jpg` etc.

    Note: This is intentionally conservative. It won't try to guess cross-folder relations.
    """

    # 1) Bucket by directory
    by_dir: Dict[str, List[MediaFile]] = {}
    for p in paths:
        mf = MediaFile(p)
        by_dir.setdefault(mf.dir, []).append(mf)

    groups: List[MediaGroup] = []

    for d, files in by_dir.items():
        # Find video candidates
        videos: List[MediaFile] = [f for f in files if f.suffix in video_exts]
        # If multiple videos share same stem, we still treat as separate groups (rare for movies).
        for v in sorted(videos, key=lambda x: x.name.lower()):
            g = MediaGroup(directory=d, base_stem=v.stem, video=v)

            prefix = v.stem + "."
            for f in files:
                if f.path == v.path:
                    continue
                if f.suffix not in sidecar_exts:
                    continue
                if f.name == v.stem + ".nfo" or f.suffix == "nfo" and f.stem == v.stem:
                    g.nfo = f
                    g.sidecars.append(f)
                elif f.name.startswith(prefix) or f.name.startswith(v.stem + "-"):
                    g.sidecars.append(f)

            # Folder-level artwork/NFO (only safe when exactly one video in this directory).
            if len(videos) == 1:
                for f in files:
                    if f.path == v.path:
                        continue
                    if f.suffix not in sidecar_exts:
                        continue
                    nlow = f.name.lower()
                    if nlow in FOLDER_LEVEL_NFO_NAMES and f.suffix == "nfo":
                        if g.nfo is None:
                            g.nfo = f
                        if f not in g.sidecars:
                            g.sidecars.append(f)
                    elif nlow in FOLDER_LEVEL_SIDECAR_NAMES:
                        if f not in g.sidecars:
                            g.sidecars.append(f)

            groups.append(g)

    # Add orphan NFO-only groups (folder-level metadata) if needed.
    # For MVP we skip.

    return groups
