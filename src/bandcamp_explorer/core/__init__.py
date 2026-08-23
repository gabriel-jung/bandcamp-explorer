"""Public API surface for the bandcamp_explorer core library.

Parsing Bandcamp's own formats belongs here, not in callers. The duration
helpers are exported for that reason: reach for these rather than a second copy
of the pattern, since two regexes drift and disagree on malformed input. An
unparseable time must read as unknown (``None``), never as a real zero.
"""

from .api import AlbumAPI, ArtistAPI, DiscoverWebAPI, SearchAPI
from .client import (
    SUGGESTED_FALLBACK_IMPERSONATE,
    BandcampClient,
    ChallengeError,
    NotFoundError,
)
from .countries import resolve_geoname
from .utils import format_track_time, track_time_to_seconds

__all__ = [
    "AlbumAPI",
    "ArtistAPI",
    "BandcampClient",
    "ChallengeError",
    "NotFoundError",
    "DiscoverWebAPI",
    "SUGGESTED_FALLBACK_IMPERSONATE",
    "SearchAPI",
    "format_track_time",
    "resolve_geoname",
    "track_time_to_seconds",
]
