"""Utility functions for Bandcamp data extraction."""

import re

_TRACK_TIME_RE = re.compile(r"P(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def find_property(prop_list: list[dict], name: str) -> str | None:
    """Find a named property in Bandcamp's additionalProperty arrays.

    Bandcamp stores IDs and metadata in JSON-LD as lists of
    {"name": ..., "value": ...} dicts.
    """
    val = next((p["value"] for p in prop_list if p["name"] == name), None)
    return str(val) if val is not None else None


def clean_text(text: str) -> str:
    """Collapse whitespace to single spaces and strip."""
    return " ".join(text.split())


def _parse_track_time(raw: str | None) -> tuple[int, int, int] | None:
    """Parse Bandcamp track duration (e.g. "P00H03M45S") into (h, m, s)."""
    if not raw:
        return None
    match = _TRACK_TIME_RE.match(raw)
    if not match:
        return None
    return int(match.group(1) or 0), int(match.group(2) or 0), int(match.group(3) or 0)


def format_track_time(raw: str | None) -> str | None:
    """Convert Bandcamp track duration (e.g. "P00H03M45S") to "3:45"."""
    parts = _parse_track_time(raw)
    if parts is None:
        return None
    return format_duration(parts[0] * 3600 + parts[1] * 60 + parts[2])


def track_time_to_seconds(raw: str | None) -> int:
    """Convert Bandcamp track duration to total seconds. Returns 0 on failure."""
    parts = _parse_track_time(raw)
    if parts is None:
        return 0
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def format_duration(total_seconds: int) -> str:
    """Format seconds as "M:SS" or "H:MM:SS"."""
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def art_url(art_id: str | None, size: int = 2) -> str | None:
    """Build a Bandcamp album art URL from an art_id.

    Square sizes: 3=100, 7=150, 9=210, 4/23/24=300, 2=350, 13=380,
    5/16/25=700, 20=1024, 10=1200, 0/1=1400.
    Non-square: 26=800x600, 27=715x402, 28=768x432, 29=100x75.
    Other: 6=100, 8=124, 11=172, 12=138, 14=368, 15=135, 21=120, 22=25.
    Default is 2 (350px), good for terminal display.
    """
    if not art_id:
        return None
    return f"https://f4.bcbits.com/img/a{art_id}_{size}.jpg"


def parse_tags(raw_tags) -> list[str]:
    """Turn Bandcamp keywords into a clean list of lowercase tags.

    Keywords can be a comma-separated string or a list.
    """
    if not raw_tags:
        return []
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in raw_tags.split(",")]
    else:
        tags = list(raw_tags)
    return [t.lower().strip() for t in tags if t.strip()]
