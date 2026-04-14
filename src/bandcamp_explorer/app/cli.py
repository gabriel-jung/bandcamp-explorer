"""CLI entry point for bandcamp — a Bandcamp data browser.

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
import sys

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
    page_fetcher,
    strip_internal_keys,
)

from .. import __version__
from ..core.api import AlbumAPI, ArtistAPI, DiscoverWebAPI, SearchAPI
from ..core.client import BandcampClient
from ..core.countries import resolve_geoname
from ..core.format import (
    album_host,
    album_summary_extras,
    album_title_with_duration,
    format_date,
    prepare_album,
    release_type,
)


def _render_lyrics(console, entity):
    """Custom section renderer for album lyrics."""
    tracks = entity.get("_lyrics", [])
    if not tracks:
        console.print("\n[dim]No lyrics available.[/dim]")
        return
    console.print(f"\n[dim]{len(tracks)} track(s) with lyrics[/dim]")
    for track in tracks:
        title = track.get("title", "")
        artist = track.get("artist")
        header = f"{title} — {artist}" if artist else title
        console.print()
        console.print(
            Panel(
                track["lyrics"].strip(),
                title=header,
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
    header_title=lambda d: f"[bold]{d.get('title', '')}[/bold]",
    header_fields=[
        HeaderField("Artist", key="artist"),
        HeaderField("Duration", key="duration"),
        HeaderField("Track", key="position", transform=lambda v: str(v) if v else ""),
    ],
    sections=[
        SectionDef("lyrics"),
    ],
    footer=["track_url"],
    auto_full=True,
)

album_def = EntityDef(
    type_name="album",
    summary=[
        SummaryField(key="artist_name", style="bold"),
        SummaryField(transform=lambda d: album_title_with_duration(d, prefix="- ")),
        SummaryField(
            transform=lambda d: ", ".join(d.get("tags", [])[:3]),
            style="dim",
        ),
        SummaryField(transform=album_summary_extras, style="dim"),
    ],
    header_title=lambda d: f"[bold]{d.get('title', '')}[/bold]",
    header_image_key="_art_data",
    header_fields=[
        HeaderField("Artist", key="artist_name"),
        HeaderField("Host", transform=album_host),
        HeaderField("Label", key="label"),
        HeaderField("Date", key="release_date", transform=format_date),
        HeaderField("Type", key="release_type", transform=release_type),
        HeaderField(
            "Formats",
            transform=lambda d: ", ".join(d.get("formats", [])),
        ),
        HeaderField("Catalog", key="catalog"),
        HeaderField("Tags", transform=lambda d: ", ".join(d.get("tags", []))),
        HeaderField(
            "Location",
            transform=lambda d: d.get("artist", {}).get("location", ""),
        ),
        HeaderField("URL", key="url"),
        HeaderField(
            "Supporters",
            transform=lambda d: str(d["num_supporters"]) if d.get("num_supporters") else "",
        ),
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
        SectionDef("_lyrics", label="Lyrics", custom_render=_render_lyrics),
    ],
    header_links=[
        HeaderLink(
            "{_host_label}",
            "artist",
            ref_fn=lambda d: d.get("artist", {}).get("url"),
        ),
    ],
    footer=["image_url"],
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
    footer=["image_url"],
)

engine = DisplayEngine()
engine.register(track_def, album_def, artist_def)
console = engine.console


# ─── Engine & navigator setup ────────────────────────────────────────────────


class _AlbumFetcher:
    """Wraps AlbumAPI to apply display transforms after fetching."""

    def __init__(self, client: BandcampClient):
        self._api = AlbumAPI(client)

    def get(self, ref, **kwargs):
        entity = self._api.get(ref, **kwargs)
        if entity:
            prepare_album(entity)
        return entity


def _make_navigator(client: BandcampClient) -> BaseNavigator:
    """Create a navigator wired to all Bandcamp APIs."""
    album_fetcher = _AlbumFetcher(client)
    apis = {
        "album": album_fetcher,
        "artist": ArtistAPI(client),
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
        help="Cap results for --tag --json (default: unlimited)",
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


def _resolve_item_type(args):
    """Determine the item_type filter from --artist, --album, --track flags."""
    if args.artist:
        return "band"
    if args.album:
        return "album"
    if args.track:
        return "track"
    return "all"


def _is_bandcamp_url(text):
    """Check if text looks like a Bandcamp URL."""
    return "bandcamp.com" in text


def _run_url(nav, args):
    """Fetch and display a Bandcamp URL directly."""
    url = " ".join(args.query)
    entity_type = "album" if "/album/" in url or "/track/" in url else "artist"
    entity = nav.fetch_entity(entity_type, url)

    if not entity:
        console.print("[red]Failed to fetch page.[/red]")
        return

    nav.display_or_navigate(entity, json_output=args.json, full=args.full)


def _run_search(nav, args):
    """Text search with paginated results."""
    search = nav.apis["search"]
    query = " ".join(args.query)
    item_type = _resolve_item_type(args)

    with console.status("Searching..."):
        results, has_more = search.search(
            query=query,
            page=1,
            item_type=item_type,
        )

    if not results:
        console.print("[dim]No results found.[/dim]")
        return

    if args.json:
        all_results = list(results)
        page = 1
        while has_more:
            page += 1
            more, has_more = search.search(
                query=query,
                page=page,
                item_type=item_type,
            )
            all_results.extend(more)
        print(json.dumps(strip_internal_keys(all_results), indent=2, ensure_ascii=False))
        return

    if args.full:
        entity = nav.fetch_entity(results[0]["_type"], results[0].get("url"))
        if entity:
            engine.details(entity)
        return

    nav.browse(
        fetch_page=page_fetcher(
            lambda p: search.search(query=query, page=p, item_type=item_type),
            first_page=(results, has_more),
        ),
        title=f'Search: "{query}"',
        page_size=len(results),
        loop=True,
    )


def _run_tag_browse(nav, client, args):
    """Browse releases via the discover_web endpoint (cursor-based)."""
    api: DiscoverWebAPI = nav.apis["discover"]
    tags = [tag.replace(" ", "-") for tag in args.tag]
    slice_ = args.slice or "new"

    geoname_id = 0
    if args.location:
        with console.status(f"Looking up location [bold]{args.location}[/bold]..."):
            geoname_id = resolve_geoname(client, args.location, force=args.refresh_location) or 0
        if not geoname_id:
            console.print(f"[red]Unknown location: {args.location}[/red]")
            sys.exit(1)

    # --json dumps in one pass; larger batches → far fewer round-trips.
    batch_size = 200 if args.json else 40
    fetch_page = api.make_page_fetcher(tags=tags, slice_=slice_, geoname_id=geoname_id, batch_size=batch_size)

    with console.status("Searching..."):
        first_items, _ = fetch_page(0, batch_size)

    if not first_items:
        console.print("[dim]No releases found.[/dim]")
        return

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
        print(json.dumps(strip_internal_keys(items), indent=2, ensure_ascii=False))
        return

    if args.full:
        url = first_items[0].get("url")
        if url:
            entity = nav.fetch_entity("album", url)
            if entity:
                engine.details(entity)
        return

    tag_label = " + ".join(tags)
    nav.browse(
        fetch_page=fetch_page,
        title=f"Tag: {tag_label} [{slice_}]",
        page_size=40,
        loop=True,
    )


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    """CLI entry point — parse args, configure logging, dispatch."""
    parser = _build_parser()
    args = parser.parse_args()

    configure_logging(args.verbose)

    has_query = bool(args.query)
    has_tag = bool(args.tag)

    if not has_query and not has_tag:
        parser.error("provide a search query or use --tag for tag browsing")

    with BandcampClient() as client:
        nav = _make_navigator(client)
        try:
            if has_tag:
                _run_tag_browse(nav, client, args)
            elif has_query and _is_bandcamp_url(" ".join(args.query)):
                _run_url(nav, args)
            else:
                _run_search(nav, args)
        except (QuitSignal, KeyboardInterrupt):
            pass
