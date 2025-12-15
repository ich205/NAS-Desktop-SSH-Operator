from __future__ import annotations

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET


_IMDB_RE = re.compile(r"tt\d{3,10}")


@dataclass(frozen=True)
class NfoInfo:
    # Prefer original_title for naming when present.
    title: str | None
    original_title: str | None
    year: int | None
    imdbid: str | None


def _text(root: ET.Element, tag: str) -> str | None:
    el = root.find(tag)
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t if t else None


def parse_nfo(xml_text: str) -> NfoInfo:
    """Parse a Kodi-style .nfo file.

    Works for movie + tvshow nfo variants. We only extract fields we need for naming.
    """

    # Some NFOs start with BOM or whitespace.
    xml_text = xml_text.lstrip("\ufeff\n\r\t ")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        # Some NFOs contain multiple top-level nodes; try best-effort.
        # Wrap into a dummy node.
        wrapped = f"<root>{xml_text}</root>"
        root = ET.fromstring(wrapped)

    # Movie root is often <movie>. TV episodes can be <episodedetails>.
    # If we wrapped, root is <root>.
    if root.tag == "root":
        # Try to find first likely node.
        for child in root:
            if child.tag in {"movie", "tvshow", "episodedetails"}:
                root = child
                break

    title = _text(root, "title")
    original_title = _text(root, "originaltitle")

    year_raw = _text(root, "year")
    year: int | None = None
    if year_raw:
        try:
            year = int(re.findall(r"\d{4}", year_raw)[0])
        except Exception:
            year = None

    # Fallbacks: some NFOs use <premiered>YYYY-MM-DD</premiered> or similar.
    if year is None:
        for tag in ("premiered", "releasedate", "released", "dateadded"):
            v = _text(root, tag)
            if not v:
                continue
            m = re.search(r"\b(19\d{2}|20\d{2})\b", v)
            if m:
                try:
                    year = int(m.group(1))
                    break
                except Exception:
                    pass

    imdb_raw = (
        _text(root, "imdbid")
        or _text(root, "imdb_id")
        or _text(root, "imdb")
        or _text(root, "id")
        or _text(root, "uniqueid")
    )

    imdbid: str | None = None
    if imdb_raw:
        m = _IMDB_RE.search(imdb_raw)
        if m:
            imdbid = m.group(0)

    return NfoInfo(title=title, original_title=original_title, year=year, imdbid=imdbid)
