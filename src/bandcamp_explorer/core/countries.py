"""Location tag_id discovery and local caching.

Bandcamp assigns internal tag IDs to locations (countries, cities, states,
continents). Rather than shipping these IDs (which are undocumented), this
module discovers them at runtime by fetching Bandcamp's discover pages and
caches them locally as JSON.

Cached entries persist until a discover request fails with the cached
value — the CLI then force-refreshes and retries.
"""

import html
import json
import re
import threading
from pathlib import Path

from loguru import logger

CACHE_DIR = Path.home() / ".cache" / "bandcamp-explorer"
CACHE_FILE = CACHE_DIR / "locations.json"
GEONAME_CACHE_FILE = CACHE_DIR / "geonames.json"

DISCOVER_URL = "https://bandcamp.com/discover/{slug}"
GEONAME_SEARCH_URL = "https://bandcamp.com/api/location/1/geoname_search"

# Process-lifetime in-memory mirror so long-running bots don't re-read
# the cache file on every location lookup. The lock guards concurrent
# resolve_* calls from the Discord bot's worker threads.
_MEMORY: dict[Path, dict[str, dict]] = {}
_LOCK = threading.Lock()


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


def _save_json_cache(path: Path, data: dict[str, dict]) -> None:
    """Atomically write a JSON cache file and refresh the in-memory mirror."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with _LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload)
        tmp.replace(path)
        _MEMORY[path] = data


def _fetch_location_tag(client, slug: str) -> dict | None:
    """Fetch a location's tag_id from Bandcamp's discover page.

    Parses the ``data-blob`` attribute embedded in the page HTML to
    extract the location's custom tag info.

    Args:
        client: A ``BandcampClient`` instance.
        slug: Location slug (e.g. "france", "paris", "california").

    Returns:
        Dict with "slug", "label", "tag_id" keys, or None on failure.
    """
    url = DISCOVER_URL.format(slug=slug)
    text = client.get(url)
    if not text:
        return None

    blob_match = re.search(r'data-blob="([^"]+)"', text)
    if not blob_match:
        logger.warning(f"No data-blob found for {slug}")
        return None

    try:
        blob_json = html.unescape(blob_match.group(1))
        data = json.loads(blob_json)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"Failed to parse data-blob for {slug}: {e}")
        return None

    custom_tags = data.get("appData", {}).get("initialState", {}).get("customTags", [])
    if not custom_tags:
        logger.warning(f"No custom tags found for {slug}")
        return None

    tag = custom_tags[0]
    return {
        "slug": slug,
        "label": tag.get("label", slug),
        "tag_id": tag["id"],
    }


def _fetch_and_save(client, slug: str, cache: dict) -> int | None:
    """Fetch a location tag, update cache, return tag_id or None."""
    result = _fetch_location_tag(client, slug)
    if result:
        cache[slug] = result
        _save_json_cache(CACHE_FILE, cache)
        logger.info(f"Cached location: {result['label']} (tag_id={result['tag_id']})")
        return result["tag_id"]
    return None


def resolve_location(client, value: str, force: bool = False) -> int | None:
    """Resolve a location name/slug to its Bandcamp tag_id.

    Works with countries (france), cities (paris), states (california),
    and continents (europe).

    Checks the local cache first (unless ``force=True``), then fetches
    from Bandcamp if not cached.

    Args:
        client: A ``BandcampClient`` instance (used for fetching if not cached).
        value: Location name or slug (e.g. "france", "Paris", "new-york").
        force: If True, bypass cache and re-fetch from Bandcamp.

    Returns:
        The tag_id integer, or None if the location cannot be resolved.
    """
    normalized = value.lower().strip()
    slug = normalized.replace(" ", "-")
    cache = _load_json_cache(CACHE_FILE)

    if not force:
        # Check cache by slug
        if slug in cache:
            return cache[slug]["tag_id"]

        # Check cache by label (e.g. "France" matches slug "france")
        for entry in cache.values():
            if entry.get("label", "").lower() == normalized:
                return entry["tag_id"]

    return _fetch_and_save(client, slug, cache)


def resolve_geoname(client, value: str, force: bool = False) -> int | None:
    """Resolve a place name to a geonames.org id for ``discover_web``.

    Uses Bandcamp's ``/api/location/1/geoname_search`` endpoint (the one the
    discover page hits for its location autocomplete) and picks the top match.
    Cached on disk so repeat lookups are free.
    """
    normalized = value.lower().strip()
    cache = _load_json_cache(GEONAME_CACHE_FILE)

    if not force and normalized in cache:
        return cache[normalized]["id"]

    data = client.post_json(GEONAME_SEARCH_URL, {"q": value})
    if not data or not data.get("ok"):
        return None

    results = data.get("results") or []
    if not results:
        return None

    top = results[0]
    try:
        gid = int(top["id"])
    except (KeyError, ValueError, TypeError):
        return None

    cache[normalized] = {
        "id": gid,
        "name": top.get("name"),
        "fullname": top.get("fullname"),
    }
    _save_json_cache(GEONAME_CACHE_FILE, cache)
    logger.info(f"Cached geoname: {top.get('fullname')} (id={gid})")
    return gid


def clear_cache() -> None:
    """Delete both location caches (on disk and in-memory)."""
    with _LOCK:
        for path in (CACHE_FILE, GEONAME_CACHE_FILE):
            _MEMORY.pop(path, None)
            path.unlink(missing_ok=True)
            logger.info(f"Cleared cache: {path.name}")


def list_cached_locations() -> dict[str, dict]:
    """Return all cached locations."""
    return _load_json_cache(CACHE_FILE)
