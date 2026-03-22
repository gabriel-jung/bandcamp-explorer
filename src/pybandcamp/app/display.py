"""Display functions for rendering Bandcamp entities in the terminal.

Each entity type has display levels:
- **summary**: one-line format for search result and browse lists
- **details**: full info panel with tracklist, bio, and cover art

Dispatch dicts (``SUMMARY``, ``SEARCH_SUMMARY``, ``DETAILS``) map ``_type``
strings to the corresponding display function.
"""

import sys
from collections.abc import Callable
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..core.utils import format_duration, track_time_to_seconds
from .images import get_image_escape

console = Console()

IMAGE_ROWS = 8
IMAGE_COLS = 22
MIN_PANEL_WIDTH = 40


def _show_image_beside(image_data: bytes | None, panel: Panel) -> None:
    """Display image and panel side by side, or panel only if unsupported."""
    escape = (
        get_image_escape(image_data, height=IMAGE_ROWS, width=IMAGE_COLS)
        if image_data
        else None
    )

    wide_enough = escape and console.width >= IMAGE_COLS + 2 + MIN_PANEL_WIDTH

    if not wide_enough:
        if escape:
            sys.stdout.write(escape + "\n")
            sys.stdout.flush()
        console.print(panel)
        return

    # Render panel to fit beside image
    panel_width = console.width - IMAGE_COLS - 2
    temp = Console(width=panel_width, force_terminal=True, highlight=False)
    with temp.capture() as cap:
        temp.print(panel)
    panel_lines = cap.get().rstrip("\n").split("\n")

    # Print image (cursor ends up below it)
    sys.stdout.write(escape + "\n")

    # Move cursor back up to the top of the image
    sys.stdout.write(f"\033[{IMAGE_ROWS}A")

    # Print panel lines beside image
    total_lines = max(IMAGE_ROWS, len(panel_lines))
    for i in range(total_lines):
        sys.stdout.write(f"\r\033[{IMAGE_COLS + 2}C")
        if i < len(panel_lines):
            sys.stdout.write(panel_lines[i])
        sys.stdout.write("\n")

    sys.stdout.flush()


def _key_value_grid(rows: list[tuple[str, str]]) -> Table:
    """Build a borderless key/value grid for info sections."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", justify="right")
    grid.add_column()
    for label, value in rows:
        if value:
            grid.add_row(label, value)
    return grid


def show_text_panel(text: str | None, title: str) -> None:
    """Show text in a bordered panel, or a 'not available' message."""
    if text:
        console.print()
        console.print(Panel(text, title=title, border_style="dim"))
    else:
        console.print(f"\n[dim]No {title.lower()} available.[/dim]")


# --- Search Result ---


def display_search_band_summary(d: dict) -> None:
    """One-line: type + name + location + genre."""
    location = d.get("location") or ""
    genre = d.get("genre") or ""
    console.print(
        f"[dim]artist[/dim]  [bold]{d.get('name', '')}[/bold]  [dim]{location}[/dim]  {genre}"
    )


def display_search_album_summary(d: dict) -> None:
    """One-line: type + name + by artist."""
    subhead = d.get("subhead") or ""
    console.print(
        f"[dim]album [/dim]  [bold]{d.get('name', '')}[/bold]  [dim]{subhead}[/dim]"
    )


def display_search_track_summary(d: dict) -> None:
    """One-line: type + name + from album by artist."""
    subhead = d.get("subhead") or ""
    console.print(
        f"[dim]track [/dim]  [bold]{d.get('name', '')}[/bold]  [dim]{subhead}[/dim]"
    )


# --- Release Summary (from DiscoverAPI) ---


def display_release_summary(d: dict) -> None:
    """One-line: artist - title + genre."""
    genre = d.get("genre", "")
    console.print(
        f"[bold]{d.get('artist_name', '')}[/bold] - {d.get('title', '')}"
        f"  [dim]{genre}[/dim]"
    )


# --- Album ---


def display_album_summary(d: dict) -> None:
    """One-line: artist - title + first 3 tags."""
    tags = ", ".join(d.get("tags", [])[:3])
    console.print(
        f"[bold]{d.get('artist_name', '')}[/bold] - {d.get('title', '')}"
        f"  [dim]{tags}[/dim]"
    )


def _format_date(raw: str | None) -> str:
    """Format '03 Mar 2026 00:00:00 GMT' → '03 Mar 2026'."""
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%d %b %Y %H:%M:%S %Z")
        return dt.strftime("%d %b %Y")
    except ValueError:
        return raw


def display_album_header(d: dict) -> None:
    """Info panel with artist, date, tags, and location. Shows cover art."""
    artist = d.get("artist", {})
    tags = ", ".join(d.get("tags", []))

    artist_name = d.get("artist_name", "")
    host_name = artist.get("name", "")
    same = not host_name or host_name == artist_name

    rows = [("Artist", artist_name)]
    if not same:
        rows.append(("Host", host_name))
    if d.get("label"):
        rows.append(("Label", d["label"]))

    # Format release type for display
    release_type = d.get("release_type", "")
    if release_type:
        release_type = release_type.replace("Release", "").strip()
    formats = ", ".join(d.get("formats", []))

    rows.extend(
        [
            ("Date", _format_date(d.get("release_date"))),
            ("Type", release_type),
            ("Formats", formats),
            ("Catalog", d.get("catalog", "")),
            ("Tags", tags),
            ("Location", artist.get("location", "")),
            ("URL", d.get("url", "")),
        ]
    )

    if d.get("num_supporters"):
        rows.append(("Supporters", str(d["num_supporters"])))
    info = _key_value_grid(rows)

    console.print()
    title = f"[bold]{d.get('title', '')}[/bold]"
    panel = Panel(info, title=title, border_style="blue")
    _show_image_beside(d.get("_art_data"), panel)


def display_album_details(d: dict) -> None:
    """Full album view: header + tracklist + description + lyrics."""
    display_album_header(d)
    _print_tracklist(d.get("tracks", []))
    show_text_panel(d.get("description"), "Description")
    _print_lyrics(d.get("tracks", []))


def _print_lyrics(tracks: list[dict]) -> None:
    """Show lyrics for tracks that have them."""
    tracks_with_lyrics = [(t, t["lyrics"]) for t in tracks if t.get("lyrics")]
    if not tracks_with_lyrics:
        return
    console.print(f"\n[dim]{len(tracks_with_lyrics)} track(s) with lyrics[/dim]")
    for track, lyrics in tracks_with_lyrics:
        title = track.get("title", "")
        artist = track.get("artist")
        header = f"{title} — {artist}" if artist else title
        console.print()
        console.print(
            Panel(
                lyrics.strip(),
                title=header,
                border_style="dim",
                width=min(80, console.width),
            )
        )


def _print_tracklist(tracks: list[dict]) -> None:
    """Render a numbered tracklist table with durations and total time."""
    if not tracks:
        return

    has_artists = any(track.get("artist") for track in tracks)

    table = Table(title="Tracklist", border_style="dim", show_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Title")
    if has_artists:
        table.add_column("Artist", style="dim")
    table.add_column("Duration", justify="right", style="dim")

    total_seconds = 0
    for track in tracks:
        row = [str(track.get("position", "")), track.get("title", "")]
        if has_artists:
            row.append(track.get("artist") or "")
        row.append(track.get("duration") or "")
        table.add_row(*row)
        total_seconds += track_time_to_seconds(track.get("duration_raw"))

    console.print()
    console.print(table)

    if total_seconds > 0:
        console.print(f"  [dim]Total: {format_duration(total_seconds)}[/dim]")


# --- Artist ---


def display_artist_summary(d: dict) -> None:
    """One-line: artist name + location."""
    location = d.get("location") or ""
    console.print(f"[bold]{d.get('name', '')}[/bold]  [dim]{location}[/dim]")


def display_artist_header(d: dict) -> None:
    """Info panel with location and URL. Shows artist photo."""
    rows = [
        ("Location", d.get("location", "")),
        ("Label", d.get("label", "")),
        ("URL", d.get("url", "")),
    ]
    info = _key_value_grid(rows)

    console.print()
    title = f"[bold]{d.get('name', '')}[/bold]"
    panel = Panel(info, title=title, border_style="blue")
    _show_image_beside(d.get("_art_data"), panel)


def display_artist_details(d: dict) -> None:
    """Full artist view: header + bio."""
    display_artist_header(d)
    show_text_panel(d.get("bio"), "Bio")


# --- Dispatch dicts ---

SEARCH_SUMMARY: dict[str, Callable] = {
    "band": display_search_band_summary,
    "album": display_search_album_summary,
    "track": display_search_track_summary,
}

SUMMARY: dict[str, Callable] = {
    "release_summary": display_release_summary,
    "album": display_album_summary,
    "artist": display_artist_summary,
}

DETAILS: dict[str, Callable] = {
    "album": display_album_details,
    "artist": display_artist_details,
}
