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

from .. import __version__
from ..core.api import AlbumAPI, ArtistAPI, DiscoverAPI, SearchAPI
from ..core.client import BandcampClient
from ..core.countries import resolve_location
from .display import console
from .navigator import Navigator


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
        version=f"%(prog)s {__version__}",
    )

    return parser


def _resolve_item_type(args) -> str:
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


def _run_url(nav: Navigator, args) -> None:
    """Fetch and display a Bandcamp URL directly."""
    url = " ".join(args.query)

    if "/album/" in url or "/track/" in url:
        with console.status("Fetching album..."):
            entity = AlbumAPI(nav.client).get(url)
    else:
        with console.status("Fetching artist..."):
            entity = ArtistAPI(nav.client).get(url)

    if not entity:
        console.print("[red]Failed to fetch page.[/red]")
        return

    if args.json:
        nav._show_detail(entity)
        return

    nav.navigate_entity(entity, top_level=True)


def _run_search(nav: Navigator, args) -> None:
    """Text search with paginated results."""
    api = SearchAPI(nav.client)
    query = " ".join(args.query)
    item_type = _resolve_item_type(args)

    current_page = 1
    with console.status("Searching..."):
        results, has_more = api.search(
            query=query, page=current_page, item_type=item_type
        )

    if not results:
        console.print("[dim]No results found.[/dim]")
        return

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

    nav.paginate(
        results,
        has_more,
        current_page,
        fetch_page=lambda p: api.search(query=query, page=p, item_type=item_type),
        summary_key="search_result",
        title=f'Search: "{query}"',
    )


def _run_tag_browse(nav: Navigator, args) -> None:
    """Browse releases by tag with paginated results."""
    api = DiscoverAPI(nav.client)

    tags = [t.replace(" ", "-") for t in args.tag]
    location_used = False
    if args.location:
        with console.status(f"Looking up location [bold]{args.location}[/bold]..."):
            tag_id = resolve_location(
                nav.client, args.location, force=args.refresh_location
            )
        if tag_id is None:
            console.print(f"[red]Unknown location: {args.location}[/red]")
            sys.exit(1)
        tags.append(tag_id)
        location_used = True

    current_page = 1
    with console.status("Searching..."):
        results, has_more = api.discover(tags=tags, sort=args.sort, page=current_page)

    if not results and location_used:
        logger.info("No results with location tag, refreshing...")
        with console.status("Refreshing location tag..."):
            tag_id = resolve_location(nav.client, args.location, force=True)
        if tag_id is not None:
            tags = list(args.tag) + [tag_id]
            with console.status("Searching..."):
                results, has_more = api.discover(
                    tags=tags, sort=args.sort, page=current_page
                )

    if not results:
        console.print("[dim]No releases found.[/dim]")
        return

    if args.json:
        all_results = list(results)
        while has_more:
            current_page += 1
            more, has_more = api.discover(tags=tags, sort=args.sort, page=current_page)
            all_results.extend(more)
        print(json.dumps(all_results, indent=2, ensure_ascii=False))
        return

    tag_label = " + ".join(str(t) for t in args.tag)
    nav.paginate(
        results,
        has_more,
        current_page,
        fetch_page=lambda p: api.discover(tags=tags, sort=args.sort, page=p),
        summary_key="release_summary",
        title=f"Tag: {tag_label}",
    )


def main():
    """CLI entry point — parse args, configure logging, dispatch."""
    parser = _build_parser()
    args = parser.parse_args()

    logger.remove()
    level = "DEBUG" if args.verbose else "WARNING"
    logger.add(
        sys.stderr,
        level=level,
        format="<dim>{time:HH:mm:ss}</dim> | <level>{level: <8}</level> | {message}",
    )

    has_query = bool(args.query)
    has_tag = bool(args.tag)

    if not has_query and not has_tag:
        parser.error("provide a search query or use --tag for tag browsing")

    with BandcampClient() as client:
        nav = Navigator(client, args)
        try:
            if has_tag:
                _run_tag_browse(nav, args)
            elif has_query and _is_bandcamp_url(" ".join(args.query)):
                _run_url(nav, args)
            else:
                _run_search(nav, args)
        except KeyboardInterrupt:
            pass
