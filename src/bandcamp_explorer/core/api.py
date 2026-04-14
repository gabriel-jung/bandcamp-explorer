"""API layer for Bandcamp.

Each entity type (album, artist, discover, search) has a dedicated API class.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

from typing import Literal

from bs4 import BeautifulSoup
from loguru import logger

from .client import BandcampClient
from .parsers import AlbumPageParser, ArtistPageParser, SearchPageParser
from .utils import art_url

DIG_DEEPER_URL = "https://bandcamp.com/api/hub/2/dig_deeper"
DISCOVER_WEB_URL = "https://bandcamp.com/api/discover/1/discover_web"
SEARCH_URL = "https://bandcamp.com/search"

Slice = Literal["new", "top", "rand"]
ItemType = Literal["all", "band", "album", "track"]
Sort = Literal["date", "pop", "top"]

# Maps CLI filter flags to Bandcamp item_type query parameter values
SEARCH_ITEM_TYPES: dict[ItemType, str] = {
    "all": "",
    "band": "b",
    "album": "a",
    "track": "t",
}


class BaseAPI:
    """Base class providing shared fetch/parse helpers."""

    def __init__(self, client: BandcampClient):
        self._client = client

    def _get_page(self, url: str, parser_class, params: dict | None = None, **kwargs) -> dict | None:
        """Fetch a page and parse it into a structured dict.

        Downloads the HTML, builds a BeautifulSoup tree, and passes it
        to the given parser class. Extra kwargs are forwarded to the
        parser constructor.
        """
        html = self._client.get(url, params=params)
        if not html:
            return None
        parser = parser_class(BeautifulSoup(html, "html.parser"), url, **kwargs)
        return parser.parse()

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
        """
        album = self._get_page(album_url, AlbumPageParser)
        if not album:
            return None
        if fetch_art:
            self._attach_image(album)
        return album


class DiscoverAPI(BaseAPI):
    """Browse Bandcamp releases via the legacy ``dig_deeper`` hub endpoint.

    This is the old discover API. It caps around ~600 items per tag and
    returns a leaner payload (no inline ``release_date``, ``location``,
    ``track_count``, etc.). Prefer :class:`DiscoverWebAPI` for new work —
    this class is kept for reference and for callers that specifically
    need the hub's ordering or tag semantics.
    """

    def discover(
        self,
        tags: list,
        sort: Sort = "date",
        page: int = 1,
        media_format: str = "all",
    ) -> tuple[list[dict], bool]:
        """Fetch a single page of releases.

        Args:
            tags: Genre and/or location tags. Country tag_ids should be
                appended to this list for location filtering
                (e.g. ``["dungeon-synth", 1309675381]``).
            sort: Sort mode — "date", "pop", or "top".
            page: Page number (1-indexed).
            media_format: Format filter (default "all").

        Returns:
            Tuple of (list of release_summary dicts, has_more).
        """
        payload = {
            "filters": {
                "tags": tags,
                "format": media_format,
                "location": 0,
                "sort": sort,
            },
            "page": page,
        }

        data = self._client.post_json(DIG_DEEPER_URL, payload, crawl=True)
        if not data:
            return [], False

        items = data.get("items", [])
        results = [
            {
                "_type": "album",
                "album_id": item.get("tralbum_id"),
                "artist_name": item.get("artist"),
                "title": item.get("title"),
                "url": item.get("tralbum_url"),
                "artist_url": item.get("band_url"),
                "artist_id": item.get("band_id"),
                "art_id": str(item["art_id"]) if item.get("art_id") else None,
                "genre": item.get("genre", ""),
            }
            for item in items
        ]

        return results, data.get("more_available", False)

    def discover_all(
        self,
        tags: list,
        sort: Sort = "date",
        max_pages: int = 10,
    ) -> list[dict]:
        """Fetch multiple pages of releases and combine them.

        Keeps fetching until there are no more results or ``max_pages``
        is reached. Crawl delay is applied between pages.
        """
        all_results = []
        pages_fetched = 0
        for page in range(1, max_pages + 1):
            results, has_more = self.discover(tags=tags, sort=sort, page=page)
            all_results.extend(results)
            pages_fetched = page
            logger.debug(f"Page {page}: {len(results)} releases")
            if not has_more:
                break

        logger.info(f"Discovered {len(all_results)} releases across {pages_fetched} pages.")
        return all_results


def _strip_tracker(url: str | None) -> str | None:
    """Remove the ``?from=discover_page`` tracker param Bandcamp appends."""
    if not url:
        return url
    return url.split("?from=")[0]


class DiscoverWebAPI(BaseAPI):
    """Browse Bandcamp releases via the new ``/discover`` page endpoint.

    Mirrors what the public ``https://bandcamp.com/discover/<tag>?s=<slice>``
    page shows. Returns a broader set of releases than :class:`DiscoverAPI`
    (which uses the older ``dig_deeper`` hub), and carries extra fields
    inline — ``release_date``, ``location``, ``price``, ``track_count``.

    Pagination is cursor-based, not page-based.
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
            results.append(
                {
                    "_type": "album",
                    "album_id": str(item["item_id"]) if item.get("item_id") else None,
                    "artist_name": item.get("band_name"),
                    "album_artist": item.get("album_artist"),
                    "title": item.get("title"),
                    "url": _strip_tracker(item.get("item_url")),
                    "artist_url": _strip_tracker(item.get("band_url")),
                    "artist_id": str(item["band_id"]) if item.get("band_id") else None,
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
        """
        state = {"cursor": None, "items": [], "exhausted": False, "total": None}

        def fetch(start: int, count: int) -> tuple[list[dict], int]:
            while len(state["items"]) < start + count and not state["exhausted"]:
                results, cursor, total = self.discover(
                    tags=tags,
                    slice_=slice_,
                    cursor=state["cursor"],
                    size=max(count, batch_size),
                    geoname_id=geoname_id,
                )
                state["items"].extend(results)
                state["cursor"] = cursor
                if state["total"] is None:
                    state["total"] = total
                if not cursor or not results:
                    state["exhausted"] = True

            items = state["items"][start : start + count]
            if state["exhausted"]:
                total = len(state["items"])
            else:
                total = state["total"] or (start + len(items) + count)
            return items, total

        return fetch


class SearchAPI(BaseAPI):
    """Search Bandcamp via the HTML search page."""

    def search(
        self,
        query: str,
        page: int = 1,
        item_type: ItemType = "all",
    ) -> tuple[list[dict], bool]:
        """Search Bandcamp and return one page of results.

        Args:
            query: Free-text search query.
            page: Page number (1-indexed).
            item_type: Filter by type — "all", "band", "album", or "track".

        Returns:
            Tuple of (list of search_result dicts, has_more).
        """
        params = {"q": query, "page": page}
        type_code = SEARCH_ITEM_TYPES.get(item_type, "")
        if type_code:
            params["item_type"] = type_code

        data = self._get_page(SEARCH_URL, SearchPageParser, params=params)
        if not data:
            return [], False

        return data["results"], data["has_more"]


class ArtistAPI(BaseAPI):
    """Fetch and parse Bandcamp artist/label pages."""

    def get(self, artist_url: str, fetch_art: bool = True) -> dict | None:
        """Fetch an artist page and return parsed data.

        Fetches the root page for profile info, then the ``/music`` subpage
        for the discography grid. Skip the artist-photo download by
        passing ``fetch_art=False``.
        """
        artist = self._get_page(artist_url, ArtistPageParser)
        if not artist:
            return None

        if fetch_art:
            self._attach_image(artist)

        # Discography lives on /music subpage
        music_url = artist_url.rstrip("/") + "/music"
        music_page = self._get_page(music_url, ArtistPageParser)
        if music_page and music_page.get("discography"):
            artist["discography"] = music_page["discography"]

        return artist
