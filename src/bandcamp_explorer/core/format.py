"""Display formatters shared between the CLI and Discord bot.

These turn raw API fields into human-readable strings. Kept in core
so both apps render identically.
"""

from datetime import datetime


def format_date(raw: str | None) -> str:
    """Format a Bandcamp date to '03 Mar 2026'.

    Handles both the album-page format ('03 Mar 2026 00:00:00 GMT') and
    the discover_web format ('2026-03-03 12:34:56 UTC').
    """
    if not raw:
        return ""
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%Y-%m-%d %H:%M:%S %Z"):
        try:
            return datetime.strptime(raw, fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    return raw


def format_duration_pretty(seconds: float | int | None) -> str:
    """Format a duration in seconds as 'Mm SSs' or 'Hh MMm'.

    Distinct from ``core.utils.format_duration`` which returns ``M:SS``
    — this variant is meant for inline summary display.
    """
    if not seconds:
        return ""
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def album_title_with_duration(entity: dict, prefix: str = "") -> str:
    """Render an album title with duration in parens when available."""
    title = entity.get("title") or ""
    if not title:
        return ""
    dur = format_duration_pretty(entity.get("duration"))
    base = f"{prefix}{title}"
    return f"{base} ({dur})" if dur else base


def album_summary_extras(entity: dict) -> str:
    """Inline date / location / preorder, empty when fields are absent."""
    parts = []
    date = format_date(entity.get("release_date"))
    if date:
        parts.append(date)
    loc = entity.get("location")
    if loc:
        parts.append(loc)
    if entity.get("is_preorder"):
        parts.append("preorder")
    return " · ".join(parts)


def release_type(raw: str | None) -> str:
    """Strip 'Release' suffix from release types."""
    if not raw:
        return ""
    return raw.replace("Release", "").strip()


def album_host(entity: dict) -> str:
    """Host name, only when it differs from the byArtist name."""
    host = entity.get("artist", {}).get("name", "")
    artist = entity.get("artist_name", "")
    return host if host and host != artist else ""


def prepare_album(album: dict, *, lyrics_as_text: bool = False) -> None:
    """Precompute app-friendly derived fields on an album entity.

    Adds ``_host_label`` (labelled host/artist line) and, when any track
    has lyrics, ``_lyrics`` — either a list of track dicts (for custom
    rendering) or a single joined string when ``lyrics_as_text=True``.
    """
    host = album_host(album)
    artist = album.get("artist_name", "")
    album["_host_label"] = f"Host: {host}" if host else f"Artist: {artist}"

    lyrics = [t for t in album.get("tracks", []) if t.get("lyrics")]
    if not lyrics:
        return
    if lyrics_as_text:
        album["_lyrics"] = "\n\n".join(f"**{t.get('title', '')}**\n{t['lyrics'].strip()}" for t in lyrics)
    else:
        album["_lyrics"] = lyrics
