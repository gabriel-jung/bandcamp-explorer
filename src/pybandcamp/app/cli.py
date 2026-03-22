"""CLI entry point for bandcamp — a Bandcamp data browser.

Usage::

    bandcamp "caladan brood"
    bandcamp "erang" --artist
    bandcamp --tag dungeon-synth --sort pop
    bandcamp --tag black-metal --location paris
    bandcamp https://erang.bandcamp.com/album/tome-iv
"""

import argparse
import json
import sys

from loguru import logger

from ..core.api import AlbumAPI, ArtistAPI, DiscoverAPI, SearchAPI
from ..core.client import BandcampClient
from ..core.countries import resolve_location
from .display import (
    DETAILS,
    SEARCH_SUMMARY,
    SUMMARY,
    console,
)


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with all modes and options."""
    parser = argparse.ArgumentParser(
        prog="bandcamp",
        description="Browse and fetch Bandcamp data.",
    )

    # Primary: text search
    parser.add_argument(
        "query",
        nargs="*",
        help="Search query (e.g. 'dungeon synth')",
    )

    # Secondary: tag-based browse
    parser.add_argument(
        "--tag",
        nargs="+",
        metavar="TAG",
        help="Browse releases by tag(s) (e.g. dungeon-synth black-metal)",
    )

    # Search type filters
    parser.add_argument(
        "--artist",
        action="store_true",
        help="Search artists/labels only",
    )
    parser.add_argument(
        "--album",
        action="store_true",
        help="Search albums only",
    )
    parser.add_argument(
        "--track",
        action="store_true",
        help="Search tracks only",
    )

    # Tag browse filters
    parser.add_argument(
        "--sort",
        choices=["date", "pop"],
        default="date",
        help="Sort mode for tag browse (default: date)",
    )
    parser.add_argument(
        "--location",
        type=str,
        help="Filter by location (e.g. france, paris, california, europe)",
    )
    parser.add_argument(
        "--refresh-location",
        action="store_true",
        help="Force re-fetch of location tag_id (bypass cache)",
    )

    # Output modes
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Debug logging",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 0.1.0",
    )

    return parser


def _resolve_search_type(args) -> str:
    """Determine the item_type filter from --artist, --album, --track flags."""
    if args.artist:
        return "band"
    if args.album:
        return "album"
    if args.track:
        return "track"
    return "all"


def _is_bandcamp_url(text: str) -> bool:
    """Check if text looks like a Bandcamp URL."""
    return "bandcamp.com" in text


def _run_url(client: BandcampClient, args) -> None:
    """Fetch and display a Bandcamp URL directly."""
    url = " ".join(args.query)

    if "/album/" in url or "/track/" in url:
        with console.status("Fetching album..."):
            entity = AlbumAPI(client).get(url)
    else:
        with console.status("Fetching artist..."):
            entity = ArtistAPI(client).get(url)

    if not entity:
        console.print("[red]Failed to fetch page.[/red]")
        return

    if args.json:
        _show_detail(entity, args)
        return

    _navigate_entity(entity, client, args, top_level=True)


def _run_search(client: BandcampClient, args) -> None:
    """Text search with paginated results."""
    api = SearchAPI(client)
    query = " ".join(args.query)
    item_type = _resolve_search_type(args)

    # Fetch first page
    current_page = 1
    with console.status("Searching..."):
        results, has_more = api.search(
            query=query, page=current_page, item_type=item_type
        )

    if not results:
        console.print("[dim]No results found.[/dim]")
        return

    # JSON mode: dump all pages
    if args.json:
        all_results = list(results)
        while has_more:
            current_page += 1
            more, has_more = api.search(
                query=query, page=current_page, item_type=item_type
            )
            all_results.extend(more)
        print(json.dumps(all_results, indent=2, ensure_ascii=False))
        return

    # Interactive pagination loop
    _paginate(
        results,
        has_more,
        current_page,
        fetch_page=lambda p: api.search(query=query, page=p, item_type=item_type),
        summary_key="search_result",
        title=f'Search: "{query}"',
        client=client,
        args=args,
    )


def _run_tag_browse(client: BandcampClient, args) -> None:
    """Browse releases by tag with paginated results."""
    api = DiscoverAPI(client)

    # Build tags list: genre tags + optional location tag
    # Auto-convert spaces to hyphens (e.g. "dungeon synth" → "dungeon-synth")
    tags = [t.replace(" ", "-") for t in args.tag]
    location_used = False
    if args.location:
        with console.status(f"Looking up location [bold]{args.location}[/bold]..."):
            tag_id = resolve_location(
                client, args.location, force=args.refresh_location
            )
        if tag_id is None:
            console.print(f"[red]Unknown location: {args.location}[/red]")
            sys.exit(1)
        tags.append(tag_id)
        location_used = True

    # Fetch first page
    current_page = 1
    with console.status("Searching..."):
        results, has_more = api.discover(tags=tags, sort=args.sort, page=current_page)

    # If no results and we used a location tag, it may be stale — retry with fresh
    if not results and location_used:
        logger.info("No results with location tag, refreshing...")
        with console.status("Refreshing location tag..."):
            tag_id = resolve_location(client, args.location, force=True)
        if tag_id is not None:
            tags = list(args.tag) + [tag_id]
            with console.status("Searching..."):
                results, has_more = api.discover(
                    tags=tags, sort=args.sort, page=current_page
                )

    if not results:
        console.print("[dim]No releases found.[/dim]")
        return

    # JSON mode: dump all pages
    if args.json:
        all_results = list(results)
        while has_more:
            current_page += 1
            more, has_more = api.discover(tags=tags, sort=args.sort, page=current_page)
            all_results.extend(more)
        print(json.dumps(all_results, indent=2, ensure_ascii=False))
        return

    # Interactive pagination loop
    tag_label = " + ".join(str(t) for t in args.tag)
    _paginate(
        results,
        has_more,
        current_page,
        fetch_page=lambda p: api.discover(tags=tags, sort=args.sort, page=p),
        summary_key="release_summary",
        title=f"Tag: {tag_label}",
        client=client,
        args=args,
    )


def _display_result(r: dict, summary_key: str) -> None:
    """Display a single result using the appropriate summary function."""
    if summary_key == "search_result":
        result_type = r.get("result_type", "band")
        SEARCH_SUMMARY.get(result_type, SEARCH_SUMMARY["band"])(r)
    else:
        SUMMARY[summary_key](r)


def _paginate(
    results, has_more, current_page, fetch_page, summary_key, title, client, args
):
    """Shared pagination loop for search and tag browse."""
    page_size = len(results)
    while True:
        # Continuous numbering across pages
        offset = (current_page - 1) * page_size

        # Title and result count
        console.print(f"\n[bold]{title}[/bold]")
        console.print(f"[dim]Page {current_page} — {len(results)} results[/dim]\n")

        # Display numbered list
        for i, r in enumerate(results, 1):
            console.print(f"  [bold cyan]\\[{offset + i}][/bold cyan] ", end="")
            _display_result(r, summary_key)

        # Navigation hints
        console.print()
        hints = []
        if current_page > 1:
            hints.append("[bold]p[/bold]rev page")
        if has_more:
            hints.append("[bold]n[/bold]ext page")
        hints.append("enter number to select")
        console.print(f"[dim]{' | '.join(hints)}[/dim]")
        prompt = "[bold]>[/bold] "

        try:
            choice = console.input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return

        if not choice:
            continue

        # Navigation
        if choice.lower() == "n" and has_more:
            current_page += 1
            with console.status("Loading..."):
                results, has_more = fetch_page(current_page)
            if not results:
                console.print("[dim]No more results.[/dim]")
                return
            continue

        if choice.lower() == "p" and current_page > 1:
            current_page -= 1
            with console.status("Loading..."):
                results, has_more = fetch_page(current_page)
            continue

        # Selection — fetch album details for the selected result
        try:
            num = int(choice)
            idx = num - 1 - offset
            if 0 <= idx < len(results):
                _handle_selection(results[idx], client, args)
            else:
                console.print("[red]Invalid selection.[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")


def _show_detail(entity: dict, args) -> None:
    """Show full details for a selected entity, with JSON support."""
    entity_type = entity["_type"]
    if args.json:
        clean = {k: v for k, v in entity.items() if not k.startswith("_")}
        print(json.dumps(clean, indent=2, ensure_ascii=False))
    else:
        DETAILS[entity_type](entity)


def _handle_selection(result: dict, client: BandcampClient, args) -> None:
    """Fetch and display details for a selected result."""
    entity = _fetch_selection(result, client)
    if not entity:
        return
    _navigate_entity(entity, client, args)


def _navigate_entity(
    entity: dict,
    client: BandcampClient,
    args,
    *,
    skip_shortcut: bool = False,
    top_level: bool = False,
) -> None:
    """Display an entity and let the user navigate to related pages."""
    entity_type = entity["_type"]

    if entity_type == "artist":
        discography = entity.get("discography", [])
        if not skip_shortcut and len(discography) == 1:
            # Single album — go straight to it
            url = discography[0].get("url")
            if url:
                with console.status("Fetching album..."):
                    album = AlbumAPI(client).get(url)
                if album:
                    _navigate_entity(album, client, args, top_level=top_level)
                    return
        _show_detail(entity, args)
        if discography:
            _browse_discography(
                discography,
                client,
                args,
                top_level=top_level,
                label_name=entity.get("label"),
                label_url=entity.get("label_url"),
            )
        elif not top_level:
            _wait_for_back()
    else:
        _show_detail(entity, args)
        _album_prompt(entity, client, args, top_level=top_level)


def _album_prompt(
    album: dict, client: BandcampClient, args, *, top_level: bool = False
) -> None:
    """After showing an album, let the user navigate to host or track pages."""
    host = album.get("artist", {})
    host_url = host.get("url")
    tracks = album.get("tracks", [])

    if not host_url:
        if not top_level:
            _wait_for_back()
        return

    artist_name = album.get("artist_name", "")
    host_name = host.get("name", "")
    if host_name and host_name != artist_name:
        nav_hint = f"[bold]h[/bold]ost page ({host_name})"
        nav_key = "h"
    else:
        nav_hint = f"[bold]a[/bold]rtist page ({artist_name})"
        nav_key = "a"

    console.print()
    hints = [nav_hint]
    if tracks:
        hints.append("track number to open")
    if not top_level:
        hints.append("[bold]0[/bold] to go back")
    console.print(f"[dim]{' | '.join(hints)}[/dim]")

    try:
        choice = console.input("[bold]>[/bold] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return

    if not choice or choice == "0":
        return

    if choice == nav_key:
        with console.status(f"Fetching {host_name or artist_name}..."):
            artist = ArtistAPI(client).get(host_url)
        if artist:
            _navigate_entity(artist, client, args, skip_shortcut=True)
        else:
            console.print("[red]Failed to fetch page.[/red]")
        return

    # Track selection by position number
    try:
        num = int(choice)
        track = next((t for t in tracks if t.get("position") == num), None)
        if track and track.get("url"):
            with console.status("Fetching track..."):
                entity = AlbumAPI(client).get(track["url"])
            if entity:
                _show_detail(entity, args)
                _album_prompt(entity, client, args)
            else:
                console.print("[red]Failed to fetch track.[/red]")
        else:
            console.print("[red]Invalid track number.[/red]")
    except ValueError:
        console.print("[red]Invalid input.[/red]")


def _wait_for_back() -> None:
    """Wait for user to press 0 to go back."""
    console.print("\n[dim][bold]0[/bold] to go back[/dim]")
    while True:
        try:
            choice = console.input("[bold]>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0" or not choice:
            return


_DISCO_PAGE_SIZE = 50


def _browse_discography(
    items: list[dict],
    client: BandcampClient,
    args,
    *,
    top_level: bool = False,
    label_name: str | None = None,
    label_url: str | None = None,
) -> None:
    """Interactive discography browser with pagination."""
    current_page = 1
    total_pages = (len(items) + _DISCO_PAGE_SIZE - 1) // _DISCO_PAGE_SIZE

    while True:
        start = (current_page - 1) * _DISCO_PAGE_SIZE
        end = min(start + _DISCO_PAGE_SIZE, len(items))
        page_items = items[start:end]

        # Display numbered list with continuous numbering
        console.print()
        console.print(
            f"[bold]Discography[/bold]  "
            f"[dim]{len(items)} releases — page {current_page}/{total_pages}[/dim]"
        )
        console.print()
        for i, item in enumerate(page_items, start + 1):
            artist = item.get("artist_name")
            suffix = f"  [dim]{artist}[/dim]" if artist else ""
            console.print(
                f"  [bold cyan]\\[{i}][/bold cyan] {item.get('title', '')}{suffix}"
            )

        # Navigation hints
        console.print()
        hints = []
        if current_page > 1:
            hints.append("[bold]p[/bold]rev page")
        if current_page < total_pages:
            hints.append("[bold]n[/bold]ext page")
        hints.append("enter number to select")
        if label_url:
            hints.append(f"[bold]l[/bold]abel ({label_name})")
        if not top_level:
            hints.append("[bold]0[/bold] to go back")
        console.print(f"[dim]{' | '.join(hints)}[/dim]")

        try:
            choice = console.input("[bold]>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if choice == "0" and not top_level:
            return

        if not choice:
            continue

        if choice.lower() == "l" and label_url:
            with console.status(f"Fetching {label_name}..."):
                label = ArtistAPI(client).get(label_url)
            if label:
                _navigate_entity(label, client, args)
            else:
                console.print("[red]Failed to fetch label.[/red]")
            continue

        if choice.lower() == "n" and current_page < total_pages:
            current_page += 1
            continue

        if choice.lower() == "p" and current_page > 1:
            current_page -= 1
            continue

        try:
            num = int(choice)
            idx = num - 1
            if 0 <= idx < len(items):
                url = items[idx].get("url")
                if url:
                    with console.status("Fetching album..."):
                        album = AlbumAPI(client).get(url)
                    if album:
                        _show_detail(album, args)
                        _album_prompt(album, client, args)
                    else:
                        console.print("[red]Failed to fetch album.[/red]")
                else:
                    console.print("[red]No URL available.[/red]")
            else:
                console.print("[red]Invalid selection.[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")


def _fetch_selection(result: dict, client: BandcampClient) -> dict | None:
    """Fetch the full entity for a selected search/browse result."""
    result_type = result.get("_type")

    if result_type == "release_summary":
        url = result.get("album_url")
        if url:
            with console.status("Fetching album..."):
                return AlbumAPI(client).get(url)

    elif result_type == "search_result":
        search_type = result.get("result_type")
        url = result.get("url")
        if not url:
            console.print("[red]No URL available.[/red]")
            return None

        if search_type in ("album", "track"):
            with console.status("Fetching album..."):
                return AlbumAPI(client).get(url)
        elif search_type == "band":
            with console.status("Fetching artist..."):
                return ArtistAPI(client).get(url)
        else:
            console.print(f"[dim]{url}[/dim]")
            return None

    console.print("[red]Failed to fetch details.[/red]")
    return None


def main():
    """CLI entry point — parse args, configure logging, dispatch."""
    parser = _build_parser()
    args = parser.parse_args()

    # Configure logging
    logger.remove()
    level = "DEBUG" if args.verbose else "WARNING"
    logger.add(
        sys.stderr,
        level=level,
        format="<dim>{time:HH:mm:ss}</dim> | <level>{level: <8}</level> | {message}",
    )

    # Determine mode
    has_query = bool(args.query)
    has_tag = bool(args.tag)

    if not has_query and not has_tag:
        parser.error("provide a search query or use --tag for tag browsing")

    with BandcampClient() as client:
        try:
            if has_tag:
                _run_tag_browse(client, args)
            elif has_query and _is_bandcamp_url(" ".join(args.query)):
                _run_url(client, args)
            else:
                _run_search(client, args)
        except KeyboardInterrupt:
            pass
