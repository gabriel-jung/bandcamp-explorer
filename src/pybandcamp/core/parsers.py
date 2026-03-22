"""Page parsers for Bandcamp album, artist, and search pages."""

import json
from abc import ABC, abstractmethod
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from loguru import logger

from .utils import clean_text, find_property, format_track_time, parse_tags


class BasePageParser(ABC):
    """Base class for all Bandcamp page parsers.

    Each subclass receives a BeautifulSoup tree and the page URL, extracts
    structured data, and returns it as a plain dict with a ``_type`` key.
    """

    def __init__(self, soup: BeautifulSoup, url: str):
        self.soup = soup
        self.url = url

    @abstractmethod
    def parse(self) -> dict | None:
        """Parse the page and return a structured dict, or None on failure."""


class AlbumPageParser(BasePageParser):
    """Parse a Bandcamp album page.

    Extracts album metadata, artist info, and tracklist from the JSON-LD
    block embedded in the page HTML.
    """

    def parse(self) -> dict | None:
        data = self._extract_json_ld()
        if not data:
            return None

        album = self._parse_album(data)
        album["artist"] = self._parse_artist(data)
        album["tracks"] = self._parse_tracks(data, album["album_id"])
        return album

    def _extract_json_ld(self) -> dict | None:
        """Find and parse the JSON-LD script block from the page."""
        tag = self.soup.find("script", type="application/ld+json")
        if not tag or not tag.string:
            logger.warning(f"No JSON-LD found on {self.url}")
            return None
        try:
            return json.loads(tag.string)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON-LD from {self.url}: {e}")
            return None

    def _parse_album(self, data: dict) -> dict:
        """Extract album-level fields from JSON-LD data.

        Handles both MusicAlbum (album pages) and MusicRecording (track pages)
        where albumRelease lives inside ``inAlbum``.
        """
        publisher = data.get("publisher", {})
        # Track pages nest albumRelease inside inAlbum
        releases = data.get("albumRelease") or (
            data.get("inAlbum", {}).get("albumRelease")
        )
        release = (
            (releases or [{}])[0] if isinstance(releases, list) else (releases or {})
        )
        is_track = data.get("@type") == "MusicRecording"

        # Label from recordLabel (distinct from publisher/host)
        label = release.get("recordLabel", {})
        label_name = label.get("name") if isinstance(label, dict) else None

        # Media formats (Digital, Cassette, Vinyl, CD)
        formats = self._parse_formats(releases if isinstance(releases, list) else [])

        # Catalog number from creditText or release description
        catalog = data.get("creditText") or ""
        # creditText often contains copyright, not catalog — only use if short
        if len(catalog) > 30 or catalog.startswith("©"):
            catalog = ""

        # Supporters count
        sponsors = data.get("sponsor", [])
        num_supporters = len(sponsors) if isinstance(sponsors, list) else 0

        return {
            "_type": "album",
            "album_id": find_property(release.get("additionalProperty", []), "item_id"),
            "artist_id": find_property(
                publisher.get("additionalProperty", []), "band_id"
            ),
            "artist_name": data.get("byArtist", {}).get("name"),
            "title": data.get("name"),
            "release_date": data.get("datePublished"),
            "release_type": data.get("albumReleaseType"),
            "url": data.get("mainEntityOfPage"),
            "description": data.get("description"),
            "art_id": find_property(release.get("additionalProperty", []), "art_id"),
            "image_url": data.get("image"),
            "tags": parse_tags(data.get("keywords")),
            "is_track": is_track,
            "label": label_name,
            "formats": formats,
            "catalog": catalog or None,
            "num_supporters": num_supporters,
        }

    @staticmethod
    def _parse_formats(releases: list) -> list[str]:
        """Extract unique media format names from albumRelease entries."""
        _FORMAT_MAP = {
            "DigitalFormat": "Digital",
            "VinylFormat": "Vinyl",
            "CDFormat": "CD",
            "CassetteFormat": "Cassette",
            "DVDFormat": "DVD",
        }
        seen = []
        for r in releases:
            fmt = r.get("musicReleaseFormat")
            if fmt and fmt in _FORMAT_MAP:
                name = _FORMAT_MAP[fmt]
                if name not in seen:
                    seen.append(name)
        return seen

    def _parse_artist(self, data: dict) -> dict:
        """Extract the publisher (artist/label) info embedded in album JSON-LD."""
        publisher = data.get("publisher", {})
        return {
            "_type": "artist",
            "artist_id": find_property(
                publisher.get("additionalProperty", []), "band_id"
            ),
            "name": publisher.get("name"),
            "url": publisher.get("@id"),
            "location": publisher.get("foundingLocation", {}).get("name"),
            "bio": publisher.get("description"),
        }

    def _parse_tracks(self, data: dict, album_id: str | None) -> list[dict]:
        """Extract the tracklist from JSON-LD itemListElement entries."""
        tracks = []
        for entry in data.get("track", {}).get("itemListElement", []):
            track = entry.get("item", {})
            duration_raw = track.get("duration")
            # Per-track artist (for compilations/VA)
            by_artist = track.get("byArtist")
            track_artist = (
                by_artist.get("name") if isinstance(by_artist, dict) else None
            )

            # Lyrics
            recording_of = track.get("recordingOf", {})
            lyrics_obj = (
                recording_of.get("lyrics", {}) if isinstance(recording_of, dict) else {}
            )
            lyrics = lyrics_obj.get("text") if isinstance(lyrics_obj, dict) else None

            tracks.append(
                {
                    "_type": "track",
                    "track_id": find_property(
                        track.get("additionalProperty", []), "track_id"
                    ),
                    "album_id": album_id,
                    "position": entry.get("position"),
                    "title": track.get("name"),
                    "url": track.get("@id"),
                    "artist": track_artist,
                    "duration": format_track_time(duration_raw),
                    "duration_raw": duration_raw,
                    "lyrics": lyrics,
                }
            )
        return tracks


class ArtistPageParser(BasePageParser):
    """Parse a Bandcamp artist/label page.

    Extracts profile info (name, location, bio, image) from the root page
    and the discography grid from the ``/music`` subpage.
    """

    def parse(self) -> dict | None:
        artist = self._parse_profile()
        artist["discography"] = self._parse_discography()
        return artist

    def _parse_profile(self) -> dict:
        """Extract artist profile from the page HTML (name, location, bio, image)."""
        name = ""
        location = None

        name_el = self.soup.find("p", id="band-name-location")
        if name_el:
            name_span = name_el.find("span", class_="title")
            name = name_span.get_text().strip() if name_span else ""
            loc_span = name_el.find("span", class_="location")
            location = loc_span.get_text().strip() if loc_span else None

        # p#bio-text has the full bio (including hidden .peekaboo-text span),
        # but also a .peekaboo-link ("... more") that we strip out
        bio_el = self.soup.find("p", id="bio-text")
        bio = None
        if bio_el:
            link = bio_el.find("span", class_="peekaboo-link")
            if link:
                link.decompose()
            bio = clean_text(bio_el.get_text())

        img_el = self.soup.find("img", class_="band-photo")
        image_url = img_el.get("src") if img_el else None

        # Extract artist_id from embedded page data
        el = self.soup.find(attrs={"data-band-id": True})
        band_id = el.get("data-band-id") if el else None

        # Label link ("more from Napalm Records" → label page)
        label_name = None
        label_url = None
        label_link = self.soup.find("a", class_="back-to-label-link")
        if label_link:
            label_url = label_link.get("href", "").split("?")[0]
            label_span = label_link.find("span", class_="back-link-text")
            if label_span:
                raw = clean_text(label_span.get_text())
                # Strip "more from" prefix
                if "from" in raw.lower():
                    label_name = raw.split("from", 1)[1].strip()
                else:
                    label_name = raw

        return {
            "_type": "artist",
            "artist_id": str(band_id) if band_id else None,
            "name": name,
            "url": self.url,
            "location": location,
            "bio": bio,
            "image_url": image_url,
            "label": label_name,
            "label_url": label_url,
        }

    def _parse_discography(self) -> list[dict]:
        """Extract release items from the music grid.

        Combines items visible in the HTML grid (``ol#music-grid > li``)
        with overflow items stored in the ``data-client-items`` JSON
        attribute (loaded by JavaScript in the browser).
        """
        music_grid = self.soup.find("ol", id="music-grid")
        if not music_grid:
            return []

        parsed = urlparse(self.url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        items = []

        # Parse visible HTML grid items
        for li in music_grid.find_all("li"):
            link = li.find("a")
            if not link:
                continue

            title = ""
            artist_name = None
            title_el = li.find("p", class_="title")
            if title_el:
                artist_span = title_el.find("span", class_="artist-override")
                if artist_span:
                    artist_name = clean_text(artist_span.extract().get_text())
                title = clean_text(title_el.get_text())

            # Extract item type from data-item-id (e.g. "album-123")
            data_item_id = li.get("data-item-id", "")
            item_type = data_item_id.split("-")[0] if "-" in data_item_id else None

            art_el = li.find("img")
            art_url_val = art_el.get("src") if art_el else None

            href = link.get("href", "")
            if href.startswith("/"):
                href = base_url + href

            items.append(
                {
                    "_type": "discography_item",
                    "title": title,
                    "artist_name": artist_name,
                    "item_type": item_type,
                    "url": href,
                    "art_url": art_url_val,
                }
            )

        # Parse overflow items from data-client-items JSON
        client_items_json = music_grid.get("data-client-items")
        if client_items_json:
            try:
                extra_items = json.loads(client_items_json)
                for entry in extra_items:
                    href = entry.get("page_url", "")
                    if href.startswith("/"):
                        href = base_url + href
                    items.append(
                        {
                            "_type": "discography_item",
                            "title": entry.get("title", ""),
                            "artist_name": entry.get("artist"),
                            "item_type": entry.get("type"),
                            "url": href,
                            "art_url": None,
                        }
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        return items


# Maps Bandcamp search type codes to our type names
_SEARCH_TYPE_MAP = {"b": "band", "a": "album", "t": "track", "f": "fan"}


class SearchPageParser(BasePageParser):
    """Parse a Bandcamp search results page.

    Extracts search results from ``ul.result-items > li.searchresult`` items
    and pagination info from ``div.pager_controls``.
    """

    def parse(self) -> dict | None:
        results = self._parse_results()
        has_more = self._has_next_page()
        return {"results": results, "has_more": has_more}

    def _parse_results(self) -> list[dict]:
        """Extract all search result items from the page."""
        items = []
        for li in self.soup.select("li.searchresult"):
            result = self._parse_result(li)
            if result:
                items.append(result)
        return items

    def _parse_result(self, li) -> dict | None:
        """Parse a single search result ``<li>`` element."""
        # Type and ID from data-search attribute
        data_search = li.get("data-search", "")
        try:
            search_data = json.loads(data_search)
        except (json.JSONDecodeError, TypeError):
            return None

        result_type = _SEARCH_TYPE_MAP.get(search_data.get("type"), "unknown")
        result_id = search_data.get("id")

        # Name from .heading a
        heading = li.select_one(".heading a")
        name = clean_text(heading.get_text()) if heading else ""

        # Clean URL from .itemurl a (strips tracking params)
        url = ""
        url_el = li.select_one(".itemurl a")
        if url_el:
            url = clean_text(url_el.get_text())

        # Subhead — location for bands, "by Artist" for albums/tracks
        subhead = ""
        subhead_el = li.select_one(".subhead")
        if subhead_el:
            subhead = clean_text(subhead_el.get_text())

        # Genre
        genre = ""
        genre_el = li.select_one(".genre")
        if genre_el:
            genre = clean_text(genre_el.get_text())
            # Strip "genre : " prefix (localized, uses &nbsp;)
            if ":" in genre:
                genre = genre.split(":", 1)[1].strip()

        # Tags
        tags = []
        tags_el = li.select_one(".tags")
        if tags_el:
            tags_text = clean_text(tags_el.get_text())
            # Strip localized prefix like "catégories : " or "tags : "
            if ":" in tags_text:
                tags_text = tags_text.split(":", 1)[1].strip()
            tags = [t.strip() for t in tags_text.split(",") if t.strip()]

        # Image URL from .art img
        img_el = li.select_one(".art img")
        image_url = img_el.get("src") if img_el else None

        # Build the result dict — fields vary by type
        result = {
            "_type": "search_result",
            "result_type": result_type,
            "result_id": result_id,
            "name": name,
            "url": url,
            "genre": genre,
            "tags": tags,
            "image_url": image_url,
        }

        # For bands: subhead is location
        if result_type == "band":
            result["location"] = subhead
        else:
            # For albums/tracks: subhead is "by Artist" or "from Album by Artist"
            result["subhead"] = subhead

        return result

    def _has_next_page(self) -> bool:
        """Check if there is a next page link in the pager controls."""
        # Current page is a <span>, next pages are <a> elements
        pager = self.soup.select_one(".pager_controls")
        if not pager:
            return False
        chosen = pager.select_one("span.pagenum.chosen")
        if not chosen:
            return False
        # If there's any <a.pagenum> after the current page, there's more
        next_link = chosen.find_parent("li")
        if next_link:
            next_sibling = next_link.find_next_sibling("li")
            if next_sibling and next_sibling.find("a", class_="pagenum"):
                return True
        return False
