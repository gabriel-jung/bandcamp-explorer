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
from pathlib import Path

from loguru import logger

CACHE_DIR = Path.home() / ".cache" / "pybandcamp"
CACHE_FILE = CACHE_DIR / "locations.json"

DISCOVER_URL = "https://bandcamp.com/discover/{slug}"


def _load_cache() -> dict[str, dict]:
    """Load cached location data from disk."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(data: dict[str, dict]) -> None:
    """Save location data to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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
        _save_cache(cache)
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
    cache = _load_cache()

    if not force:
        # Check cache by slug
        if slug in cache:
            return cache[slug]["tag_id"]

        # Check cache by label (e.g. "France" matches slug "france")
        for entry in cache.values():
            if entry.get("label", "").lower() == normalized:
                return entry["tag_id"]

    return _fetch_and_save(client, slug, cache)


def clear_cache() -> None:
    """Delete the location cache file."""
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        logger.info("Location cache cleared.")


def list_cached_locations() -> dict[str, dict]:
    """Return all cached locations."""
    return _load_cache()
