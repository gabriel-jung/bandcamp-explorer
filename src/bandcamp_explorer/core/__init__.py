"""Public API surface for the bandcamp_explorer core library."""

from .api import AlbumAPI, ArtistAPI, DiscoverAPI, DiscoverWebAPI, SearchAPI
from .client import BandcampClient, NotFoundError
from .countries import resolve_geoname, resolve_location

__all__ = [
    "AlbumAPI",
    "ArtistAPI",
    "BandcampClient",
    "NotFoundError",
    "DiscoverAPI",
    "DiscoverWebAPI",
    "SearchAPI",
    "resolve_geoname",
    "resolve_location",
]
