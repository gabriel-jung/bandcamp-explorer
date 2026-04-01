"""Discord bot for browsing Bandcamp interactively.

Uses discord-metadata to render entity embeds with select-menu navigation.
Run with the ``bandcamp-discord`` entry point.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from discord import app_commands
from discord_metadata import (
    BaseNavigator,
    DisplayEngine,
    EntityDef,
    HeaderField,
    HeaderLink,
    MetadataBot,
    SectionDef,
    SummaryField,
    SyncAPI,
    TableColumn,
)

from ..core.api import AlbumAPI, ArtistAPI, DiscoverAPI, SearchAPI
from ..core.client import BandcampClient
from ..core.countries import resolve_location

# ─── Bandcamp branding ─────────────────────────────────────────────────────

BC_COLOR = 0x1DA0C3
BC_FOOTER = "Bandcamp"

# ─── Display transforms ───────────────────────────────────────────────────


def _format_date(raw):
    """Format '03 Mar 2026 00:00:00 GMT' → '03 Mar 2026'."""
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, "%d %b %Y %H:%M:%S %Z")
        return dt.strftime("%d %b %Y")
    except ValueError:
        return raw


def _release_type(raw):
    """Strip 'Release' suffix from release types."""
    if not raw:
        return ""
    return raw.replace("Release", "").strip()


def _album_title(d: dict) -> str:
    return f"{d.get('title', '')} by {d.get('artist_name', 'Unknown')}"


def _album_host(entity):
    """Show host name only if different from byArtist name."""
    host = entity.get("artist", {}).get("name", "")
    artist = entity.get("artist_name", "")
    return host if host and host != artist else ""


# ─── Entity definitions (Discord-adapted) ─────────────────────────────────

track_def = EntityDef(
    type_name="track",
    summary=[
        SummaryField(key="title", bold=True),
        SummaryField(key="artist"),
        SummaryField(key="duration"),
    ],
    header_fields=[
        HeaderField("Artist", key="artist"),
        HeaderField("Duration", key="duration"),
        HeaderField("Track", key="position", transform=lambda v: str(v) if v else ""),
    ],
    sections=[
        SectionDef("lyrics"),
    ],
    title_key="title",
    color=BC_COLOR,
    footer=BC_FOOTER,
    url_key="track_url",
    auto_full=True,
)

album_def = EntityDef(
    type_name="album",
    summary=[
        SummaryField(key="artist_name", bold=True),
        SummaryField(key="title"),
        SummaryField(
            transform=lambda d: ", ".join(d.get("tags", [])[:3]),
        ),
    ],
    header_title=_album_title,
    header_fields=[
        HeaderField("Artist", key="artist_name"),
        HeaderField("Host", transform=_album_host),
        HeaderField("Label", key="label"),
        HeaderField("Date", key="release_date", transform=_format_date),
        HeaderField("Type", key="release_type", transform=_release_type),
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
        HeaderField(
            "Supporters",
            transform=lambda d: str(d["num_supporters"]) if d.get("num_supporters") else "",
        ),
    ],
    image_url_key="image_url",
    sections=[
        SectionDef(
            "tracks",
            label="Tracklist",
            navigable=True,
            numbered=False,
            duration_key="duration",
            columns=[
                TableColumn("#", "position"),
                TableColumn("Title", "title"),
                TableColumn("Artist", "artist"),
                TableColumn("Duration", "duration"),
            ],
        ),
        SectionDef("description"),
        SectionDef("_lyrics", label="Lyrics"),
    ],
    header_links=[
        HeaderLink(
            "Artist: {artist_name}",
            "artist",
            ref_fn=lambda d: d.get("artist", {}).get("url"),
        ),
    ],
    title_key="title",
    color=BC_COLOR,
    footer=BC_FOOTER,
    url_key="url",
)

artist_def = EntityDef(
    type_name="artist",
    summary=[
        SummaryField(key="name", bold=True),
        SummaryField(key="location"),
    ],
    header_fields=[
        HeaderField("Location", key="location"),
        HeaderField("Label", key="label"),
    ],
    thumbnail_url_key="image_url",
    sections=[
        SectionDef("bio", label="Bio"),
        SectionDef(
            "discography",
            navigable=True,
            columns=[
                TableColumn("Title", "title", transform=lambda v: f"**{v}**" if v else ""),
                TableColumn("Artist", "artist_name"),
            ],
        ),
    ],
    header_links=[
        HeaderLink("Label: {label}", "artist", ref_key="label_url"),
    ],
    color=BC_COLOR,
    footer=BC_FOOTER,
    url_key="url",
)


# ─── API adapter ──────────────────────────────────────────────────────────


class _AlbumFetcher:
    """Wraps AlbumAPI to precompute derived fields after fetching."""

    def __init__(self, client: BandcampClient):
        self._api = AlbumAPI(client)

    def get(self, ref, **kwargs):
        entity = self._api.get(ref, **kwargs)
        if entity:
            # Precompute host label
            host = entity.get("artist", {}).get("name", "")
            artist = entity.get("artist_name", "")
            entity["_host_label"] = f"Host: {host}" if host and host != artist else f"Artist: {artist}"
            # Extract lyrics from tracks
            lyrics = [t for t in entity.get("tracks", []) if t.get("lyrics")]
            if lyrics:
                entity["_lyrics"] = "\n\n".join(
                    f"**{t.get('title', '')}**\n{t['lyrics'].strip()}" for t in lyrics
                )
        return entity


class _SearchAdapter:
    """Adapts SearchAPI to the expected async search(query) -> list interface."""

    def __init__(self, client: BandcampClient, item_type: str = "all"):
        self._api = SearchAPI(client)
        self._item_type = item_type

    def search(self, query: str, **kwargs) -> list[dict]:
        results, _ = self._api.search(query=query, page=1, item_type=self._item_type)
        return results


# ─── Engine & bot setup ───────────────────────────────────────────────────

_client = BandcampClient()

engine = DisplayEngine()
engine.register(track_def, album_def, artist_def)

_apis = {
    "album": SyncAPI(_AlbumFetcher(_client)),
    "artist": SyncAPI(ArtistAPI(_client)),
    "search": SyncAPI(_SearchAdapter(_client)),
    "album_search": SyncAPI(_SearchAdapter(_client, "album")),
    "artist_search": SyncAPI(_SearchAdapter(_client, "band")),
    "track_search": SyncAPI(_SearchAdapter(_client, "track")),
}

navigator = BaseNavigator(
    engine,
    apis=_apis,
    ephemeral=False,
    placeholder="Browse sections & navigate\u2026",
)

bot = MetadataBot(navigator, on_close=_client.close)


# ─── Slash commands (grouped under /bandcamp) ─────────────────────────────

bandcamp = app_commands.Group(name="bandcamp", description="Browse Bandcamp")


@bandcamp.command(name="search", description="Search Bandcamp")
@app_commands.describe(query="Search query")
async def cmd_search(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["search"])


@bandcamp.command(name="album", description="Search for an album")
@app_commands.describe(query="Album title")
async def cmd_album(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["album_search"])


@bandcamp.command(name="artist", description="Search for an artist")
@app_commands.describe(query="Artist or label name")
async def cmd_artist(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["artist_search"])


@bandcamp.command(name="track", description="Search for a track")
@app_commands.describe(query="Track title")
async def cmd_track(interaction: discord.Interaction, query: str):
    await bot.navigator.search_and_navigate(interaction, query, ["track_search"])


_SORT_CHOICES = [
    app_commands.Choice(name="Newest", value="date"),
    app_commands.Choice(name="Popular", value="pop"),
]


@bandcamp.command(name="discover", description="Browse releases by tag")
@app_commands.describe(
    tag="Tag to browse (e.g. 'dungeon-synth', 'black-metal')",
    sort="Sort mode (default: newest)",
    location="Filter by location (e.g. 'france', 'paris')",
)
@app_commands.choices(sort=_SORT_CHOICES)
async def cmd_discover(
    interaction: discord.Interaction,
    tag: str,
    sort: app_commands.Choice[str] | None = None,
    location: str | None = None,
):
    await interaction.response.defer()
    discover = DiscoverAPI(_client)
    tags = [t.strip().replace(" ", "-") for t in tag.split(",")]
    sort_val = sort.value if sort else "date"

    if location:
        tag_id = await asyncio.to_thread(resolve_location, _client, location)
        if tag_id is None:
            await interaction.followup.send(f"Unknown location: **{location}**")
            return
        tags.append(tag_id)

    await bot.navigator.browse(
        interaction,
        lambda s, c: _discover_page(discover, tags, sort_val, s, c),
        title=f"Tag: {', '.join(tags)}",
    )


def _discover_page(
    discover: DiscoverAPI,
    tags: list,
    sort: str,
    start: int,
    count: int,
) -> tuple[list[dict], int]:
    """Adapt DiscoverAPI's page-based interface to offset-based."""
    page = start // count + 1 if count else 1
    results, has_more = discover.discover(tags=tags, sort=sort, page=page)
    # Estimate total: if has_more, assume at least one more page
    total = start + len(results) + (count if has_more else 0)
    return results, total


bot.tree.add_command(bandcamp)

# ─── Entry point ─────────────────────────────────────────────────────────


def main():
    """Run the Bandcamp Discord bot."""
    bot.run_with_args("DISCORD_TOKEN")


if __name__ == "__main__":
    main()
