"""Geoname id discovery and local caching.

``DiscoverWebAPI`` filters by location using a geonames.org id, which Bandcamp
resolves through the same autocomplete endpoint its discover page uses. Those
ids are undocumented, so they are looked up at runtime and cached on disk.

This module used to also resolve Bandcamp's internal *location tag ids*, a
different scheme that only the ``dig_deeper`` hub endpoint accepted. Bandcamp
removed that endpoint, leaving those ids with nothing to consume them, so that
half was dropped in 0.6.0.
"""

import contextlib
import json
import os
import threading
from pathlib import Path

from loguru import logger

from .api import raise_for_error_payload
from .client import FetchError

CACHE_DIR = Path.home() / ".cache" / "bandcamp-explorer"
GEONAME_CACHE_FILE = CACHE_DIR / "geonames.json"

GEONAME_SEARCH_URL = "https://bandcamp.com/api/location/1/geoname_search"

# Process-lifetime in-memory mirror so long-running bots don't re-read
# the cache file on every location lookup. The lock guards concurrent
# resolve_geoname calls from the Discord bot's worker threads.
_MEMORY: dict[Path, dict[str, dict]] = {}
_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()  # file writes, so cache reads never wait on disk I/O


def _load_json_cache(path: Path) -> dict[str, dict]:
    """Load a JSON cache file, memoised per-process."""
    with _LOCK:
        cached = _MEMORY.get(path)
        if cached is not None:
            return cached
        try:
            _MEMORY[path] = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            _MEMORY[path] = {}
        return _MEMORY[path]


def _store_json_cache(path: Path, key: str, entry: dict) -> None:
    """Add one entry to a JSON cache file and its in-memory mirror.

    The entry is added under the lock, so concurrent lookups (the bot's worker
    threads) cannot change the dict while it is being written. Best effort:
    the cache only saves a lookup next time, so a home directory that cannot
    be written (a container, a locked-down account) is logged and the
    in-memory copy is kept for this process.
    """
    with _LOCK:
        data = {**_MEMORY.get(path, {}), key: entry}
        _MEMORY[path] = data
        payload = json.dumps(data, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    with _WRITE_LOCK:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload)
            tmp.replace(path)
        except OSError as e:
            logger.warning(f"Location cache not saved ({e}): {path}")
            with contextlib.suppress(OSError):
                tmp.unlink()


def resolve_geoname(client, value: str, force: bool = False) -> int | None:
    """Resolve a place name to a geonames.org id for ``discover_web``.

    Uses Bandcamp's ``/api/location/1/geoname_search`` endpoint (the one the
    discover page hits for its location autocomplete) and picks the top match.
    Cached on disk so repeat lookups are free.

    Args:
        client: A ``BandcampClient`` instance.
        value: Place name (e.g. "france", "Paris", "new york").
        force: If True, bypass the cache and re-fetch.

    Returns:
        The geoname id, or None if Bandcamp knows no such place.

    Raises:
        FetchError: If the lookup request failed or Bandcamp answered with an error.
    """
    normalized = value.lower().strip()
    cache = _load_json_cache(GEONAME_CACHE_FILE)

    if not force and normalized in cache:
        return cache[normalized]["id"]

    data = client.post_json(GEONAME_SEARCH_URL, {"q": value})
    raise_for_error_payload(GEONAME_SEARCH_URL, data)
    if not data.get("ok"):
        # An unknown place answers ok: true with no results; ok: false is a failure.
        raise FetchError("Location lookup was refused (ok: false)", url=GEONAME_SEARCH_URL)

    results = data.get("results") or []
    if not results:
        return None

    top = results[0]
    try:
        gid = int(top["id"])
    except (KeyError, ValueError, TypeError):
        return None

    _store_json_cache(
        GEONAME_CACHE_FILE, normalized, {"id": gid, "name": top.get("name"), "fullname": top.get("fullname")}
    )
    logger.info(f"Cached geoname: {top.get('fullname')} (id={gid})")
    return gid
