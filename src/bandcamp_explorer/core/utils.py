"""Utility functions for Bandcamp data extraction."""

import re

from rich_metadata import format_duration

# Bandcamp writes durations as "P00H03M45S", which omits the ISO-8601 "T"
# separator. The optional T keeps the standard "PT3M45S" form working too, so
# a switch upstream would not silently drop every duration. The lookahead
# rejects a bare "P"/"PT" with no components, which must read as unknown
# rather than as a real zero.
_TRACK_TIME_RE = re.compile(r"^P(?=T?\d)T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

__all__ = [
    "art_url",
    "clean_text",
    "find_property",
    "format_duration",
    "format_track_time",
    "parse_tags",
    "strip_tracker",
    "track_time_to_seconds",
]


def strip_tracker(url: str | None) -> str | None:
    """Drop the query string Bandcamp appends to item URLs.

    Covers both the ``?from=discover_page`` tracker on discover results and
    the ``?label=...&tab=music`` referrer on label discography links. Bandcamp
    item URLs carry no meaningful query parameters, so the whole string goes.
    """
    if not url:
        return url
    return url.split("?", 1)[0]


def find_property(prop_list: list[dict], name: str) -> str | None:
    """Find a named property in Bandcamp's additionalProperty arrays.

    Bandcamp stores IDs and metadata in JSON-LD as lists of
    {"name": ..., "value": ...} dicts.
    """
    value = next(
        (prop["value"] for prop in prop_list if prop["name"] == name),
        None,
    )
    return str(value) if value is not None else None


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


def track_time_to_seconds(raw: str | None) -> int | None:
    """Convert Bandcamp track duration (e.g. "P00H03M45S") to seconds."""
    parts = _parse_track_time(raw)
    if parts is None:
        return None
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def format_track_time(raw: str | None) -> str | None:
    """Convert Bandcamp track duration (e.g. "P00H03M45S") to "3:45"."""
    seconds = track_time_to_seconds(raw)
    if seconds is None:
        return None
    return format_duration(seconds)


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
    tags = [tag.strip() for tag in raw_tags.split(",")] if isinstance(raw_tags, str) else list(raw_tags)
    return [tag.lower().strip() for tag in tags if tag.strip()]
