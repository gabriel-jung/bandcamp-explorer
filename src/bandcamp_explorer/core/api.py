"""API layer for Bandcamp.

Each entity type (album, artist, discover, search) has a dedicated API class.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

import threading
from typing import Literal

from bs4 import BeautifulSoup
from loguru import logger

from .client import BandcampClient, FetchError, NotFoundError
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


def _discover_field(data: dict, snake_name: str):
    """Read a ``discover_web`` field under its camelCase name, else its snake_case one.

    The endpoint has served both spellings. Only ``None`` falls through, so a
    real ``false`` or ``0`` under the camelCase name is kept.
    """
    head, *rest = snake_name.split("_")
    value = data.get(head + "".join(word.capitalize() for word in rest))
    return data.get(snake_name) if value is None else value


class _DiscoverPager:
    """Offset-addressable view of the cursor-paged discover feed.

    Built by :meth:`DiscoverWebAPI.make_page_fetcher`. Calling it with
    ``(start, count)`` returns ``(items, total)``, following cursors as far as
    needed. A lock serializes calls, since the bot runs each button press in
    its own thread and two presses must not follow the same cursor twice. A
    failed batch raises (``FetchError``) and leaves the state as it was, so
    the next call retries instead of treating the feed as finished.

    ``total`` is an estimate one page past what was asked for until the feed
    is exhausted, then exact; while estimating it never shrinks across calls.
    The feed's own count is deliberately not used: navigators offer a jump to
    the last page, and a cursor can only get there by walking every batch.
    """

    def __init__(self, api: "DiscoverWebAPI", tags: list[str], slice_: str, geoname_id: int, batch_size: int):
        self._api = api
        self._query = {"tags": tags, "slice_": slice_, "geoname_id": geoname_id}
        self._batch_size = batch_size
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._cursor: str | None = None
        self._exhausted = False
        self._estimate = 0

    def __call__(self, start: int, count: int) -> tuple[list[dict], int]:
        with self._lock:
            while len(self._items) < start + count and not self._exhausted:
                results, cursor, _ = self._api.discover(
                    cursor=self._cursor, size=max(count, self._batch_size), **self._query
                )
                self._items.extend(results)
                self._cursor = cursor
                if not cursor or not results:
                    self._exhausted = True

            items = self._items[start : start + count]
            if self._exhausted:
                return items, len(self._items)
            self._estimate = max(self._estimate, start + len(items) + count)
            return items, self._estimate


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

        Raises:
            FetchError: If the request failed or Bandcamp answered with an
                error payload (``ChallengeError`` for a bot-defence page).
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
        raise_for_error_payload(DISCOVER_WEB_URL, data)

        items = data.get("results") or []
        results = []
        for item in items:
            image = _discover_field(item, "primary_image") or {}
            art_id = _discover_field(image, "image_id")
            item_id = _discover_field(item, "item_id")
            band_id = _discover_field(item, "band_id")
            results.append(
                {
                    "_type": "album",
                    "album_id": str(item_id) if item_id else None,
                    "artist_name": _discover_field(item, "band_name"),
                    "album_artist": _discover_field(item, "album_artist"),
                    "title": item.get("title"),
                    "url": strip_tracker(_discover_field(item, "item_url")),
                    "artist_url": strip_tracker(_discover_field(item, "band_url")),
                    "artist_id": str(band_id) if band_id else None,
                    "art_id": str(art_id) if art_id else None,
                    "genre": "",
                    "item_type": _discover_field(item, "item_type"),
                    "release_date": _discover_field(item, "release_date"),
                    "location": _discover_field(item, "band_location"),
                    "track_count": _discover_field(item, "track_count"),
                    "duration": item.get("duration"),
                    "price": item.get("price"),
                    "is_preorder": _discover_field(item, "is_album_preorder"),
                }
            )

        # A renamed key would otherwise read as a page of blank rows, not a failure.
        if results and not any(result["album_id"] for result in results):
            raise FetchError(f"Discover items carry no id (keys: {sorted(items[0])})", url=DISCOVER_WEB_URL)

        return results, data.get("cursor"), _discover_field(data, "result_count") or 0

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

        Raises:
            FetchError: If any batch fails; batches already fetched are not
                returned as if they were the whole feed.
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
        """Build a ``(start, count) -> (items, total)`` fetcher over the feed.

        Lets callers address items by offset, as both the CLI pager and the
        Discord navigator do. Safe to call from several threads. ``total`` is
        an estimate one page ahead until the feed is exhausted, then exact;
        while estimating it never shrinks. A failed batch raises
        ``FetchError`` and the next call retries it.
        """
        return _DiscoverPager(self, tags, slice_, geoname_id, batch_size)


def raise_for_error_payload(url: str, data: dict) -> None:
    """Raise ``FetchError`` if a JSON body reports an error instead of results.

    Bandcamp's JSON endpoints answer 200 with ``{"error": true,
    "error_message": ...}`` (either key alone counts) when they refuse a call.
    ``discover_web`` rejects invalid parameters with a different 200 body,
    ``{"__api_special__": "exception", "error_type": ...}``.
    """
    if data.get("error") or data.get("error_message") or data.get("__api_special__") == "exception":
        message = data.get("error_message") or data.get("error_type") or "no message"
        raise FetchError(f"Bandcamp answered with an error: {message}", url=url)


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
    interstitial that answers HTTP 200 with no results in it, so parsing it
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

        Raises:
            FetchError: If the request failed or Bandcamp answered with an
                error payload (``ChallengeError`` for a bot-defence page).
        """
        payload = {
            "search_text": query,
            "search_filter": SEARCH_ITEM_TYPES.get(item_type, ""),
            "full_page": True,
            "fan_id": None,
        }

        data = self._client.post_json(SEARCH_URL, payload)
        raise_for_error_payload(SEARCH_URL, data)

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
        ``/music`` is asked and a 404 there means the host is gone.

        A live host can name nobody on its root too: observed on live labels
        whose root is set to their subscription page. Since 0.8.3 the ``/music`` page fetched for the
        confirmation is parsed when it answered 2xx, and if it names the artist
        it is used, ``url`` still being the root the caller asked for. A
        ``/music`` that names nobody either, or a failed fetch, returns
        ``None``. A challenged one is re-asked through the client, so it gets
        the same failover, backoff and ``ChallengeError`` as any other page.
        """
        html, artist = self._fetch_page(artist_url, ArtistPageParser)
        if html is None:
            return None

        if _looks_like_a_dead_host_root(artist):
            return self._artist_from_music_page(artist_url, fetch_art)

        if not artist:
            return None

        if fetch_art:
            self._attach_image(artist)

        if not artist.get("discography"):
            artist["discography"] = self._fetch_discography(artist_url)

        return artist

    def _artist_from_music_page(self, artist_url: str, fetch_art: bool) -> dict | None:
        """Read the artist off ``/music`` for a root that named nobody.

        A 404 there means the host is gone. Otherwise the body the confirmation
        already fetched is used, so no request is repeated; its discography is
        all that page has, so ``/music`` is not asked again for it either.

        Only a challenge costs a second request. The host is known live by then,
        so the challenge is an ordinary one and goes through ``client.get``,
        which arms the backoff and raises ``ChallengeError``. Returned as
        ``None`` it would read as a page that parsed to nothing.

        Parsed against the root URL so the artist's ``url`` is the one callers
        asked for and key on. ``/music`` sits at the host root, so relative
        release links resolve the same against either URL.
        """
        check = self._client.check_host_root(artist_url)
        if check.gone:
            logger.info(f"Artist root {artist_url} names no artist and /music is 404: host is gone.")
            raise NotFoundError(artist_url)
        html = check.music_html
        if check.challenged:
            try:
                html = self._client.get(artist_url.rstrip("/") + "/music")
            except NotFoundError:
                # Same evidence as the confirmation's own 404, reported under the
                # URL the caller asked for and keys on.
                raise NotFoundError(artist_url) from None
        if not html:
            logger.warning(f"Artist root {artist_url} names no artist, and /music gave no page to read.")
            return None
        artist = ArtistPageParser(BeautifulSoup(html, "html.parser"), artist_url, music_page=True).parse()
        if artist is None or _looks_like_a_dead_host_root(artist):
            logger.warning(f"Artist root {artist_url} names no artist, and neither does /music.")
            return None
        if fetch_art:
            self._attach_image(artist)
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
            music_page = self._get_page(music_url, ArtistPageParser, discography_only=True, music_page=True)
        except NotFoundError:
            logger.debug(f"No /music subpage for {artist_url}")
            return []
        if not music_page:
            return []
        return music_page.get("discography") or []
