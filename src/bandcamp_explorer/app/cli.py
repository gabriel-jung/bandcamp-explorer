"""CLI entry point for bandcamp, a Bandcamp data browser.

Usage::

    bandcamp "caladan brood"
    bandcamp "erang" --artist
    bandcamp --tag dungeon-synth --sort pop
    bandcamp --tag black-metal --location paris
    bandcamp https://erang.bandcamp.com/album/tome-iv
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich_metadata import (
    BaseNavigator,
    DisplayEngine,
    EntityDef,
    HeaderField,
    HeaderLink,
    QuitSignal,
    SectionDef,
    SummaryField,
    TableColumn,
    configure_logging,
    escape,
    links_line,
    list_fetcher,
)

from .. import __version__
from ..core.api import DiscoverWebAPI, SearchAPI
from ..core.client import BandcampClient, ChallengeError, FetchError
from ..core.countries import resolve_geoname
from ..core.format import (
    album_host,
    album_summary_extras,
    album_title_with_duration,
    format_date,
    release_type,
    supporters_label,
)
from .common import AlbumFetcher, ArtistFetcher


def _text(fn):
    """Wrap a plain-text transform so remote text cannot be read as Rich markup."""
    return lambda value: escape(fn(value) or "")


def _render_lyrics(console, entity):
    """Custom section renderer for album lyrics."""
    tracks = entity.get("_lyrics_tracks", [])
    if not tracks:
        console.print("\n[dim]No lyrics available.[/dim]")
        return
    console.print(f"\n[dim]{len(tracks)} track(s) with lyrics[/dim]")
    for track in tracks:
        title = track.get("title", "")
        artist = track.get("artist")
        header = f"{title} ({artist})" if artist else title
        console.print()
        console.print(
            Panel(
                escape(track["lyrics"].strip()),
                title=escape(header),
                border_style="dim",
                width=min(80, console.width),
            )
        )


# ─── Entity definitions ──────────────────────────────────────────────────────

track_def = EntityDef(
    type_name="track",
    summary=[
        SummaryField(key="title", style="bold"),
        SummaryField(key="artist", style="dim"),
        SummaryField(key="duration", style="dim"),
    ],
    header_title=lambda d: f"[bold]{escape(d.get('title', ''))}[/bold]",
    header_fields=[
        HeaderField("Artist", key="artist"),
        HeaderField("Album", key="album_name"),
        HeaderField("Duration", key="duration"),
        HeaderField("Track", key="position", transform=lambda v: str(v) if v else ""),
    ],
    sections=[
        SectionDef("lyrics"),
    ],
    footer=[lambda d: links_line(("page", d.get("track_url")))],
    auto_full=True,
)

album_def = EntityDef(
    type_name="album",
    summary=[
        SummaryField(key="artist_name", style="bold"),
        SummaryField(transform=_text(lambda d: album_title_with_duration(d, prefix="- "))),
        SummaryField(
            transform=_text(lambda d: ", ".join(d.get("tags", [])[:3])),
            style="dim",
        ),
        SummaryField(transform=_text(album_summary_extras), style="dim"),
    ],
    header_title=lambda d: f"[bold]{escape(d.get('title', ''))}[/bold]",
    header_image_key="_art_data",
    header_fields=[
        HeaderField("Artist", key="artist_name"),
        HeaderField("Host", transform=_text(album_host)),
        HeaderField("Label", key="label"),
        HeaderField("Date", key="release_date", transform=_text(format_date)),
        HeaderField("Type", key="release_type", transform=_text(release_type)),
        HeaderField(
            "Formats",
            transform=_text(lambda d: ", ".join(d.get("formats", []))),
        ),
        HeaderField("Catalog", key="catalog"),
        HeaderField("Tags", transform=_text(lambda d: ", ".join(d.get("tags", [])))),
        HeaderField(
            "Location",
            transform=_text(lambda d: d.get("artist", {}).get("location", "")),
        ),
        HeaderField("URL", key="url"),
        HeaderField("Supporters", transform=_text(supporters_label)),
    ],
    sections=[
        SectionDef(
            "tracks",
            label="Tracklist",
            navigable=True,
            numbered=False,
            duration_key="duration",
            columns=[
                TableColumn(
                    "#",
                    "position",
                    justify="right",
                    style="dim",
                    width=4,
                ),
                TableColumn("Title", "title"),
                TableColumn("Artist", "artist", style="dim"),
                TableColumn(
                    "Duration",
                    "duration",
                    justify="right",
                    style="dim",
                ),
            ],
        ),
        SectionDef("description"),
        SectionDef("_lyrics_tracks", label="Lyrics", custom_render=_render_lyrics),
    ],
    header_links=[
        HeaderLink(
            "{_host_label}",
            "artist",
            ref_fn=lambda d: d.get("artist", {}).get("url"),
        ),
    ],
    footer=[
        lambda d: links_line(
            ("page", d.get("url")),
            ("cover", d.get("image_url")),
        ),
    ],
)

artist_def = EntityDef(
    type_name="artist",
    summary=[
        SummaryField(key="name", style="bold"),
        SummaryField(key="location", style="dim"),
    ],
    header_image_key="_art_data",
    header_fields=[
        HeaderField("Location", key="location"),
        HeaderField("Type", transform=lambda d: "Label" if d.get("is_label") else ""),
        HeaderField("Label", key="label"),
        HeaderField("URL", key="url"),
    ],
    sections=[
        SectionDef("bio", label="Bio"),
        SectionDef(
            "discography",
            navigable=True,
            columns=[
                TableColumn("Title", "title", style="bold"),
                TableColumn("Artist", "artist_name", style="dim"),
            ],
        ),
    ],
    header_links=[
        HeaderLink("Label: {label}", "artist", ref_key="label_url"),
    ],
    footer=[
        lambda d: links_line(
            ("page", d.get("url")),
            ("photo", d.get("image_url")),
        ),
    ],
)

engine = DisplayEngine()
engine.register(track_def, album_def, artist_def)
console = engine.console
err_console = Console(stderr=True)  # messages, so --json output on stdout stays parseable

# Exit statuses
EXIT_OK, EXIT_FAILED, EXIT_INTERRUPTED = 0, 1, 130  # invalid flags exit 2 via parser.error


def _public(obj):
    """Drop internal ``_`` keys (image bytes, render helpers) but keep ``_type``."""
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if k == "_type" or not k.startswith("_")}
    if isinstance(obj, list):
        return [_public(item) for item in obj]
    return obj


def _print_json(data) -> None:
    """Print data as JSON on stdout. ``_type`` stays, so mixed search results stay tellable apart."""
    sys.stdout.write(json.dumps(_public(data), indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()  # a closed pipe raises here, inside main(), not at interpreter exit


# ─── Engine & navigator setup ────────────────────────────────────────────────


def _make_navigator(client: BandcampClient, *, fetch_art: bool = True) -> BaseNavigator:
    """Create a navigator wired to all Bandcamp APIs (``fetch_art=False`` skips image bytes)."""
    apis = {
        "album": AlbumFetcher(client, fetch_art=fetch_art),
        # Track search results carry a /track/ URL, which parses as an album
        # entity flagged is_track. Items inside an album's tracklist have no
        # "url" key, so they still render inline without a fetch.
        "track": AlbumFetcher(client, fetch_art=fetch_art),
        "artist": ArtistFetcher(client, fetch_art=fetch_art),
        "search": SearchAPI(client),
        "discover": DiscoverWebAPI(client),
    }
    return BaseNavigator(engine, apis=apis, entity_ref_key="url")


# ─── Parser ──────────────────────────────────────────────────────────────────


def _build_parser():
    """Build the argument parser with all modes and options."""
    parser = argparse.ArgumentParser(
        prog="bandcamp",
        description="Browse and fetch Bandcamp data.",
    )

    parser.add_argument(
        "query",
        nargs="*",
        help="Search query (e.g. 'dungeon synth')",
    )

    parser.add_argument(
        "--tag",
        nargs="+",
        metavar="TAG",
        help="Browse releases by tag(s) (e.g. dungeon-synth black-metal)",
    )

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

    parser.add_argument(
        "--location",
        type=str,
        help="Filter --tag browse by location (resolved via Bandcamp geoname search)",
    )
    parser.add_argument(
        "--refresh-location",
        action="store_true",
        help="Force re-fetch of location tag_id (bypass cache)",
    )

    slice_group = parser.add_mutually_exclusive_group()
    slice_group.add_argument(
        "--new",
        dest="slice",
        action="store_const",
        const="new",
        help="--tag: newest arrivals (default)",
    )
    slice_group.add_argument(
        "--top",
        dest="slice",
        action="store_const",
        const="top",
        help="--tag: best-selling",
    )
    slice_group.add_argument(
        "--rand",
        dest="slice",
        action="store_const",
        const="rand",
        help="--tag: surprise me",
    )

    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of results (default: unlimited)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Non-interactive: show header and all sections, then exit",
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


# ─── Search/browse commands ──────────────────────────────────────────────────


_ITEM_TYPE_FLAGS = {"artist": "band", "album": "album", "track": "track"}


def _resolve_item_type(args):
    """Determine the item_type filter from --artist, --album, --track flags."""
    return next(
        (item_type for flag, item_type in _ITEM_TYPE_FLAGS.items() if getattr(args, flag)),
        "all",
    )


def _is_bandcamp_url(text):
    """Check if text looks like a page URL rather than a search query.

    Artists on Bandcamp Pro serve from their own domain, so matching only
    "bandcamp.com" would send those URLs to the search endpoint instead.
    """
    return text.startswith(("http://", "https://")) or "bandcamp.com/" in text


def _run_url(nav, url, args) -> int:
    """Fetch and display a Bandcamp URL directly."""
    entity_type = "album" if "/album/" in url or "/track/" in url else "artist"
    entity = nav.fetch_entity(entity_type, url)

    if not entity:
        err_console.print("[red]Could not fetch that page (gone, or the request failed).[/red]")
        return EXIT_FAILED

    if args.json:
        _print_json(entity)  # same encoding as the search and tag dumps
    else:
        nav.display_or_navigate(entity, full=args.full)
    return EXIT_OK


def _run_search(nav, query, args) -> int:
    """Text search. The endpoint returns the whole result set in one call."""
    search = nav.apis["search"]
    item_type = _resolve_item_type(args)

    with err_console.status("Searching..."):
        results = search.search(query=query, item_type=item_type)

    if args.limit:
        results = results[: args.limit]

    if args.json:
        if not results:
            err_console.print("[dim]No results found.[/dim]")
        _print_json(results)
        return EXIT_OK

    if not results:
        console.print("[dim]No results found.[/dim]")
        return EXIT_OK

    if args.full:
        entity = nav.fetch_entity(results[0]["_type"], results[0].get("url"))
        if not entity:
            err_console.print("[red]Could not fetch the first result.[/red]")
            return EXIT_FAILED
        engine.details(entity)
        return EXIT_OK

    nav.browse(
        fetch_page=list_fetcher(results),
        title=f'Search: "{query}"',
        page_size=25,
        loop=True,
    )
    return EXIT_OK


def _run_tag_browse(nav, client, args) -> int:
    """Browse releases via the discover_web endpoint (cursor-based)."""
    api: DiscoverWebAPI = nav.apis["discover"]
    tags = [tag.replace(" ", "-") for tag in args.tag]
    slice_ = args.slice or "new"

    geoname_id = 0
    if args.location:
        with err_console.status(f"Looking up location [bold]{escape(args.location)}[/bold]..."):
            geoname_id = resolve_geoname(client, args.location, force=args.refresh_location) or 0
        if not geoname_id:
            err_console.print(f"[red]Unknown location: {escape(args.location)}[/red]")
            return EXIT_FAILED

    # --json dumps in one pass; larger batches → far fewer round-trips.
    batch_size = 200 if args.json else 40
    fetch_page = api.make_page_fetcher(tags=tags, slice_=slice_, geoname_id=geoname_id, batch_size=batch_size)

    with err_console.status("Searching..."):
        first_items, _ = fetch_page(0, batch_size)

    if not first_items:
        err_console.print("[dim]No releases found.[/dim]")
        if args.json:
            print("[]")
        return EXIT_OK

    if args.json:
        cap = args.limit
        items: list[dict] = list(first_items)
        while cap is None or len(items) < cap:
            want = batch_size if cap is None else min(batch_size, cap - len(items))
            next_batch, total = fetch_page(len(items), want)
            if not next_batch:
                break
            items.extend(next_batch)
            if len(items) >= total:
                break
        if cap:
            items = items[:cap]
        _print_json(items)
        return EXIT_OK

    if args.full:
        url = first_items[0].get("url")
        entity = nav.fetch_entity("album", url) if url else None
        if not entity:
            err_console.print("[red]Could not fetch the first release.[/red]")
            return EXIT_FAILED
        engine.details(entity)
        return EXIT_OK

    tag_label = " + ".join(tags)
    nav.browse(
        fetch_page=fetch_page,
        title=f"Tag: {tag_label} [{slice_}]",
        page_size=40,
        loop=True,
    )
    return EXIT_OK


# ─── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    """CLI entry point.

    Returns the exit status: 0 done (including "no results"), 1 a request
    failed or a page is gone, 2 invalid flags, 130 interrupted with --json or --full.
    """
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging(args.verbose)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not args.query and not args.tag:
        parser.error("provide a search query or use --tag for tag browsing")
    if args.json:
        # Prompts and messages from the navigator go to stderr; stdout carries only JSON.
        engine.console = err_console
    query = " ".join(args.query)

    with BandcampClient() as client:
        nav = _make_navigator(client, fetch_art=not args.json)
        try:
            if args.tag:
                return _run_tag_browse(nav, client, args)
            if _is_bandcamp_url(query):
                return _run_url(nav, query, args)
            return _run_search(nav, query, args)
        except (QuitSignal, KeyboardInterrupt):
            # Ctrl+C is how the interactive browser quits; in a --json or
            # --full run it means the output is incomplete.
            return EXIT_INTERRUPTED if args.json or args.full else EXIT_OK
        except BrokenPipeError:
            # The reader closed stdout early (`| head`): stop quietly.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return EXIT_OK
        except ChallengeError as e:
            err_console.print(
                "[red]Bandcamp served a bot-defence challenge and refused the request.[/red]\n"
                f"[dim]{escape(e)}[/dim]\n"
                "[dim]Wait a couple of minutes and try again.[/dim]"
            )
            return EXIT_FAILED
        except FetchError as e:
            err_console.print(f"[red]Request to Bandcamp failed: {escape(e)}[/red]")
            return EXIT_FAILED
