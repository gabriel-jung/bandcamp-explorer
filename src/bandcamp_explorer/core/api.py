"""API layer for Bandcamp.

Each entity type (album, artist, discover, search) has a dedicated API class.
All data is returned as plain dicts with a ``_type`` discriminator key.
"""

from bs4 import BeautifulSoup
from loguru import logger

from .client import BandcampClient
from .parsers import AlbumPageParser, ArtistPageParser, SearchPageParser
from .utils import art_url

DIG_DEEPER_URL = "https://bandcamp.com/api/hub/2/dig_deeper"
SEARCH_URL = "https://bandcamp.com/search"

# Maps CLI filter flags to Bandcamp item_type query parameter values
SEARCH_ITEM_TYPES = {
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

    def get(self, album_url: str) -> dict | None:
        """Fetch an album page and return parsed data.

        Returns album dict with embedded artist, tracks, and cover art
        image bytes (``_art_data``) for terminal display.
        """
        album = self._get_page(album_url, AlbumPageParser)
        if not album:
            return None
        self._attach_image(album)
        return album


class DiscoverAPI(BaseAPI):
    """Browse Bandcamp releases via the dig_deeper POST endpoint."""

    def discover(
        self,
        tags: list,
        sort: str = "date",
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
        sort: str = "date",
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


class SearchAPI(BaseAPI):
    """Search Bandcamp via the HTML search page."""

    def search(
        self,
        query: str,
        page: int = 1,
        item_type: str = "all",
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

    def get(self, artist_url: str) -> dict | None:
        """Fetch an artist page and return parsed data.

        Fetches the root page for profile info, then the ``/music`` subpage
        for the discography grid.
        """
        artist = self._get_page(artist_url, ArtistPageParser)
        if not artist:
            return None

        self._attach_image(artist)

        # Discography lives on /music subpage
        music_url = artist_url.rstrip("/") + "/music"
        music_page = self._get_page(music_url, ArtistPageParser)
        if music_page and music_page.get("discography"):
            artist["discography"] = music_page["discography"]

        return artist
