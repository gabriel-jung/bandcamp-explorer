"""API layer for Bandcamp.

Each entity type (album, artist, discover, search) has a dedicated API class.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

from typing import Literal

from bs4 import BeautifulSoup
from loguru import logger

from .client import BandcampClient, NotFoundError
from .parsers import AlbumPageParser, ArtistPageParser
from .utils import art_url, parse_tags, strip_tracker

DISCOVER_WEB_URL = "https://bandcamp.com/api/discover/1/discover_web"
SEARCH_URL = "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic"

Slice = Literal["new", "top", "rand"]
ItemType = Literal["all", "band", "album", "track"]

# Maps CLI filter flags to the endpoint's search_filter values
SEARCH_ITEM_TYPES: dict[ItemType, str] = {
    "all": "",
    "band": "b",
    "album": "a",
    "track": "t",
}

# Maps the endpoint's result type codes to our entity types
_SEARCH_TYPE_MAP = {"b": "artist", "a": "album", "t": "track"}


class BaseAPI:
    """Base class providing shared fetch/parse helpers."""

    def __init__(self, client: BandcampClient):
        self._client = client

    def _fetch_page(
        self, url: str, parser_class, params: dict | None = None, **kwargs
    ) -> tuple[str | None, dict | None]:
        """Fetch a page and parse it, returning ``(html, parsed)``.

        Both halves, because ``parsed is None`` alone conflates a page that
        never arrived with one that arrived and made no sense.
        """
        html = self._client.get(url, params=params)
        if not html:
            return None, None
        parser = parser_class(BeautifulSoup(html, "html.parser"), url, **kwargs)
        return html, parser.parse()

    def _get_page(self, url: str, parser_class, params: dict | None = None, **kwargs) -> dict | None:
        """Fetch a page and parse it into a structured dict.

        Downloads the HTML, builds a BeautifulSoup tree, and passes it
        to the given parser class. Extra kwargs are forwarded to the
        parser constructor.
        """
        return self._fetch_page(url, parser_class, params, **kwargs)[1]

    def _attach_image(self, entity: dict) -> None:
        """Fetch cover art / photo bytes and attach as ``_art_data``."""
        image = art_url(entity.get("art_id")) or entity.get("image_url")
        if image:
            entity["_art_data"] = self._client.get_bytes(image)


class AlbumAPI(BaseAPI):
    """Fetch and parse Bandcamp album pages."""

    def get(self, album_url: str, fetch_art: bool = True) -> dict | None:
        """Fetch an album page and return parsed data.

        When ``fetch_art`` is true, cover art bytes are attached as
        ``_art_data`` (useful for terminal rendering). Callers that only
        need the image URL can pass ``False`` to skip the extra request.

        A 404 propagates as ``NotFoundError`` so callers can tell a deleted
        album from a failed fetch; every other transport failure returns None.
        """
        album = self._get_page(album_url, AlbumPageParser)
        if not album:
            return None
        if fetch_art:
            self._attach_image(album)
        return album


class _PageFetcherState:
    """Mutable state for ``DiscoverWebAPI.make_page_fetcher``.

    Holds the cursor, accumulated items, exhaustion flag, and the
    server-reported total. The fetcher closure mutates this instead
    of relying on a dict so attribute access is typed and the total
    estimate stays monotonic.
    """

    __slots__ = ("cursor", "exhausted", "items", "total")

    def __init__(self) -> None:
        self.cursor: str | None = None
        self.items: list[dict] = []
        self.exhausted: bool = False
        self.total: int | None = None


class DiscoverWebAPI(BaseAPI):
    """Browse Bandcamp releases via the ``/discover`` page endpoint.

    Mirrors what the public ``https://bandcamp.com/discover/<tag>?s=<slice>``
    page shows, carrying ``release_date``, ``location``, ``price`` and
    ``track_count`` inline.

    This replaced the older ``dig_deeper`` hub endpoint, which Bandcamp has
    since removed (it now answers ``{"error": true, "error_message": "bad
    function"}``). Pagination is cursor-based, not page-based.
    """

    def discover(
        self,
        tags: list[str],
        slice_: Slice = "new",
        cursor: str | None = None,
        size: int = 40,
        category_id: int = 0,
        geoname_id: int = 0,
        time_facet_id: int | None = None,
        include_result_types: list[str] | None = None,
    ) -> tuple[list[dict], str | None, int]:
        """Fetch a single batch of releases.

        Args:
            tags: Tag slugs (e.g. ``["dungeon-synth"]``, up to 5).
            slice_: ``"top"`` (best-selling, the site default), ``"new"``
                (new arrivals), or ``"rand"`` (surprise me).
            cursor: Opaque pagination cursor from the previous response.
                Pass ``None`` to start from the first batch.
            size: Batch size (default matches the web UI).
            category_id: 0 = all categories, or an id from the category list.
            geoname_id: 0 = anywhere, or a geoname id.
            time_facet_id: Optional time window id; ``None`` = no restriction.
            include_result_types: Result types to include, e.g. ``["a"]`` for
                albums only. Defaults to ``["a"]``.

        Returns:
            Tuple of (results, next_cursor, total_count). ``next_cursor`` is
            ``None`` when there are no more pages.
        """
        payload = {
            "category_id": category_id,
            "tag_norm_names": tags,
            "geoname_id": geoname_id,
            "slice": slice_,
            "time_facet_id": time_facet_id,
            "cursor": cursor,
            "size": size,
            "include_result_types": include_result_types or ["a"],
        }

        data = self._client.post_json(DISCOVER_WEB_URL, payload, crawl=True)
        if not data:
            return [], None, 0

        items = data.get("results", [])
        results = []
        for item in items:
            image = item.get("primary_image") or {}
            art_id = image.get("image_id")
            item_id = item.get("item_id")
            band_id = item.get("band_id")
            results.append(
                {
                    "_type": "album",
                    "album_id": str(item_id) if item_id else None,
                    "artist_name": item.get("band_name"),
                    "album_artist": item.get("album_artist"),
                    "title": item.get("title"),
                    "url": strip_tracker(item.get("item_url")),
                    "artist_url": strip_tracker(item.get("band_url")),
                    "artist_id": str(band_id) if band_id else None,
                    "art_id": str(art_id) if art_id else None,
                    "genre": "",
                    "item_type": item.get("item_type"),
                    "release_date": item.get("release_date"),
                    "location": item.get("band_location"),
                    "track_count": item.get("track_count"),
                    "duration": item.get("duration"),
                    "price": item.get("price"),
                    "is_preorder": item.get("is_album_preorder"),
                }
            )

        return results, data.get("cursor"), data.get("result_count", 0)

    def discover_all(
        self,
        tags: list[str],
        slice_: Slice = "new",
        max_pages: int = 10,
        size: int = 40,
        geoname_id: int = 0,
    ) -> list[dict]:
        """Fetch multiple batches by following cursors.

        Stops when the server stops returning a cursor, when a batch is
        empty, or when ``max_pages`` is reached.
        """
        all_results: list[dict] = []
        cursor: str | None = None
        pages_fetched = 0
        for page in range(1, max_pages + 1):
            results, next_cursor, _ = self.discover(
                tags=tags, slice_=slice_, cursor=cursor, size=size, geoname_id=geoname_id
            )
            pages_fetched = page
            all_results.extend(results)
            logger.debug(f"Batch {page}: {len(results)} releases")
            if not results or not next_cursor:
                break
            cursor = next_cursor

        logger.info(
            f"Discovered {len(all_results)} releases via discover_web across {pages_fetched} batches."
        )
        return all_results

    def make_page_fetcher(
        self,
        tags: list[str],
        slice_: Slice = "new",
        geoname_id: int = 0,
        batch_size: int = 40,
    ):
        """Build a ``(start, count) -> (items, total)`` fetcher.

        Wraps cursor pagination in a closure so callers can address items
        by offset, as both the CLI pager and the Discord navigator do.
        ``total`` is an estimate until the feed is exhausted, then exact.
        Estimates never shrink across calls.
        """
        state = _PageFetcherState()

        def fetch(start: int, count: int) -> tuple[list[dict], int]:
            while len(state.items) < start + count and not state.exhausted:
                results, cursor, total = self.discover(
                    tags=tags,
                    slice_=slice_,
                    cursor=state.cursor,
                    size=max(count, batch_size),
                    geoname_id=geoname_id,
                )
                state.items.extend(results)
                state.cursor = cursor
                if total and (state.total is None or total > state.total):
                    state.total = total
                if not cursor or not results:
                    state.exhausted = True

            items = state.items[start : start + count]
            if state.exhausted:
                total_out = len(state.items)
            else:
                total_out = max(state.total or 0, start + len(items) + count)
            return items, total_out

        return fetch


def _search_result(item: dict) -> dict | None:
    """Map one raw search hit onto the entity shape the display layer expects."""
    entity_type = _SEARCH_TYPE_MAP.get(item.get("type"))
    if not entity_type:
        return None

    item_id = item.get("id")
    band_id = item.get("band_id")
    art_id = item.get("art_id")

    result = {
        "_type": entity_type,
        # Bands carry only a root URL; albums and tracks carry a full path.
        "url": strip_tracker(item.get("item_url_path") or item.get("item_url_root")),
        "genre": item.get("genre_name") or "",
        "tags": parse_tags(item.get("tag_names")),
        "image_url": item.get("img") or None,
        "art_id": str(art_id) if art_id else None,
    }

    if entity_type == "artist":
        result["artist_id"] = str(item_id) if item_id else None
        result["name"] = item.get("name")
        result["location"] = item.get("location")
        result["is_label"] = item.get("is_label")
    elif entity_type == "album":
        result["album_id"] = str(item_id) if item_id else None
        result["artist_id"] = str(band_id) if band_id else None
        result["title"] = item.get("name")
        result["artist_name"] = item.get("band_name")
    else:
        album_id = item.get("album_id")
        result["track_id"] = str(item_id) if item_id else None
        result["album_id"] = str(album_id) if album_id else None
        result["artist_id"] = str(band_id) if band_id else None
        result["title"] = item.get("name")
        result["artist"] = item.get("band_name")
        result["album_name"] = item.get("album_name")

    return result


class SearchAPI(BaseAPI):
    """Search Bandcamp via the site's own search JSON endpoint.

    The HTML search page at ``/search`` is fronted by a bot-defence
    interstitial that answers HTTP 200 with no results in it, so scraping it
    silently returns nothing. This endpoint is what the site's own search box
    calls, and it is not gated.

    It returns the full result set in one response with no cursor or offset,
    so there is no pagination to expose.
    """

    def search(self, query: str, item_type: ItemType = "all") -> list[dict]:
        """Search Bandcamp and return all results in one call.

        Args:
            query: Free-text search query.
            item_type: Filter by type (one of "all", "band", "album", "track").

        Returns:
            List of entity dicts (artist, album, and/or track).
        """
        payload = {
            "search_text": query,
            "search_filter": SEARCH_ITEM_TYPES.get(item_type, ""),
            "full_page": True,
            "fan_id": None,
        }

        data = self._client.post_json(SEARCH_URL, payload)
        if not data:
            return []

        items = (data.get("auto") or {}).get("results") or []
        results = [mapped for item in items if (mapped := _search_result(item))]
        logger.debug(f"Search '{query}' ({item_type}): {len(results)} results")
        return results


def _looks_like_a_dead_host_root(artist: dict | None) -> bool:
    """True for a root page that parsed cleanly and named no artist.

    The signup page a dead host serves parses without error into a dict whose
    ``artist_id`` and ``name`` are both empty; every live page measured carries
    at least one. Either alone counts as alive, so losing one field is never
    read as death.

    ``artist is None`` is deliberately not this case: the parser returns None
    when profile extraction raised, which is markup that moved or a truncated
    body. Unknown, worth retrying, not evidence.
    """
    if artist is None:
        return False
    return not (artist.get("artist_id") or artist.get("name"))


class ArtistAPI(BaseAPI):
    """Fetch and parse Bandcamp artist/label pages."""

    def get(self, artist_url: str, fetch_art: bool = True) -> dict | None:
        """Fetch an artist page and return parsed data.

        Fetches the root page for profile info. Most artist pages already
        render the same music grid as ``/music``, including the overflow items
        held in ``data-client-items``, so the subpage is only fetched when the
        root turned up no discography (typically an artist whose landing page
        is a single release rather than a grid). Skip the artist-photo download
        by passing ``fetch_art=False``.

        A 404 on the root page propagates as ``NotFoundError`` so callers can
        tell a deleted artist from a failed fetch.

        Since 0.8.0 it also covers what a 404 cannot express: a dead host's root
        answers 200 with Bandcamp's signup page, which parses into an artist
        with no id and no name. When the root parses and names nobody,
        ``/music`` is asked and a 404 there means the host is gone. Anything
        else returns ``None``.
        """
        html, artist = self._fetch_page(artist_url, ArtistPageParser)
        if html is None:
            return None

        if _looks_like_a_dead_host_root(artist):
            if self._client.host_root_is_gone(artist_url):
                logger.info(f"Artist root {artist_url} names no artist and /music is 404: host is gone.")
                raise NotFoundError(artist_url)
            # Falling through would re-fetch the /music URL just probed, this
            # time through client.get, where a challenge arms the backoff the
            # confirmation exists to get ahead of.
            logger.warning(f"Artist root {artist_url} names no artist, and /music did not 404.")
            return None

        if not artist:
            return None

        if fetch_art:
            self._attach_image(artist)

        if not artist.get("discography"):
            artist["discography"] = self._fetch_discography(artist_url)

        return artist

    def _fetch_discography(self, artist_url: str) -> list[dict]:
        """Fetch the ``/music`` subpage discography, or [] if unavailable.

        Supplementary: the root already yielded the profile, so the host is live
        by construction and a 404 here must not surface as ``NotFoundError`` --
        callers read that as deleted and would flag a live one. The 404 actually
        observed is ``/music/music``, from a caller passing a URL already ending
        in ``/music``; every live host measured serves ``/music`` with 200.
        """
        base = artist_url.rstrip("/")
        music_url = base if base.endswith("/music") else base + "/music"
        try:
            music_page = self._get_page(music_url, ArtistPageParser, discography_only=True)
        except NotFoundError:
            logger.debug(f"No /music subpage for {artist_url}")
            return []
        if not music_page:
            return []
        return music_page.get("discography") or []
