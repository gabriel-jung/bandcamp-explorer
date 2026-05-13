"""Shared adapter classes used by both the CLI and Discord entry points."""

from __future__ import annotations

from ..core.api import AlbumAPI, ArtistAPI, SearchAPI
from ..core.client import BandcampClient
from ..core.transform import prepare_album


class AlbumFetcher:
    """Wrap :class:`AlbumAPI` to attach display-only derived fields.

    ``fetch_art=False`` skips the cover-art byte download (Discord uses the
    image URL directly). ``lyrics_as_text=True`` selects the joined-string
    lyrics representation for Discord; the CLI uses the list-of-tracks form.
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
        entity = self._api.get(ref, fetch_art=self._fetch_art, **kwargs)
        if entity:
            prepare_album(entity, lyrics_as_text=self._lyrics_as_text)
        return entity


class ArtistFetcher:
    """Wrap :class:`ArtistAPI` so callers can opt out of the photo download."""

    def __init__(self, client: BandcampClient, *, fetch_art: bool = True):
        self._api = ArtistAPI(client)
        self._fetch_art = fetch_art

    def get(self, ref: str, **kwargs):
        return self._api.get(ref, fetch_art=self._fetch_art, **kwargs)


class SearchAdapter:
    """Expose :class:`SearchAPI` as the ``search(query) -> list`` shape the
    Discord navigator expects, with an item-type filter baked in."""

    def __init__(self, client: BandcampClient, item_type: str = "all"):
        self._api = SearchAPI(client)
        self._item_type = item_type

    def search(self, query: str, **kwargs) -> list[dict]:
        results, _ = self._api.search(query=query, page=1, item_type=self._item_type)
        return results
