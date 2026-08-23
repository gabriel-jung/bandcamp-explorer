"""Shared adapter classes used by both the CLI and Discord entry points."""

from __future__ import annotations

from loguru import logger

from ..core.api import AlbumAPI, ArtistAPI, SearchAPI
from ..core.client import BandcampClient, NotFoundError
from ..core.transform import prepare_album


class AlbumFetcher:
    """Wrap :class:`AlbumAPI` to attach display-only derived fields.

    ``fetch_art=False`` skips the cover-art byte download (Discord uses the
    image URL directly). ``lyrics_as_text=True`` selects the joined-string
    lyrics representation for Discord; the CLI uses the list-of-tracks form.

    ``NotFoundError`` is absorbed here: the API layer lets a 404 escape so
    callers can tell a deleted album from a failed fetch, but the interactive
    frontends have no use for that distinction and want a plain "nothing to
    show".
    """

    def __init__(
        self,
        client: BandcampClient,
        *,
        fetch_art: bool = True,
        lyrics_as_text: bool = False,
    ):
        self._api = AlbumAPI(client)
        self._fetch_art = fetch_art
        self._lyrics_as_text = lyrics_as_text

    def get(self, ref: str, **kwargs):
        try:
            entity = self._api.get(ref, fetch_art=self._fetch_art, **kwargs)
        except NotFoundError:
            logger.warning(f"Album not found: {ref}")
            return None
        if entity:
            prepare_album(entity, lyrics_as_text=self._lyrics_as_text)
        return entity


class ArtistFetcher:
    """Wrap :class:`ArtistAPI` so callers can opt out of the photo download.

    Absorbs ``NotFoundError`` for the same reason as :class:`AlbumFetcher`.
    """

    def __init__(self, client: BandcampClient, *, fetch_art: bool = True):
        self._api = ArtistAPI(client)
        self._fetch_art = fetch_art

    def get(self, ref: str, **kwargs):
        try:
            return self._api.get(ref, fetch_art=self._fetch_art, **kwargs)
        except NotFoundError:
            logger.warning(f"Artist not found: {ref}")
            return None


class SearchAdapter:
    """Expose :class:`SearchAPI` as the ``search(query) -> list`` shape the
    Discord navigator expects, with an item-type filter baked in."""

    def __init__(self, client: BandcampClient, item_type: str = "all"):
        self._api = SearchAPI(client)
        self._item_type = item_type

    def search(self, query: str, **kwargs) -> list[dict]:
        return self._api.search(query=query, item_type=self._item_type)
