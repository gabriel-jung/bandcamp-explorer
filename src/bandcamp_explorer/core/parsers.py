"""Page parsers for Bandcamp album and artist pages.

Search is not parsed from HTML: Bandcamp fronts ``/search`` with a
bot-defence interstitial, so :class:`~bandcamp_explorer.core.api.SearchAPI`
uses the site's JSON search endpoint instead.
"""

import json
from abc import ABC, abstractmethod
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from .utils import (
    clean_text,
    find_property,
    format_track_time,
    parse_tags,
    strip_tracker,
    track_time_to_seconds,
)


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


_FORMAT_MAP = {
    "DigitalFormat": "Digital",
    "VinylFormat": "Vinyl",
    "CDFormat": "CD",
    "CassetteFormat": "Cassette",
    "DVDFormat": "DVD",
}


class AlbumPageParser(BasePageParser):
    """Parse a Bandcamp album page.

    Extracts album metadata, artist info, and tracklist from the JSON-LD
    block embedded in the page HTML.
    """

    def parse(self) -> dict | None:
        data = self._extract_json_ld()
        if not data:
            return None

        try:
            album = self._parse_album(data)
        except Exception as e:
            logger.error(f"Failed to parse album block on {self.url}: {e}")
            return None

        try:
            album["artist"] = self._parse_artist(data)
        except Exception as e:
            logger.warning(f"Failed to parse artist block on {self.url}: {e}")
            album["artist"] = {"_type": "artist"}

        try:
            album["tracks"] = self._parse_tracks(data, album.get("album_id"))
        except Exception as e:
            logger.warning(f"Failed to parse tracks block on {self.url}: {e}")
            album["tracks"] = []

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
        releases = data.get("albumRelease") or (data.get("inAlbum", {}).get("albumRelease"))
        release = (releases or [{}])[0] if isinstance(releases, list) else (releases or {})
        is_track = data.get("@type") == "MusicRecording"

        label = release.get("recordLabel", {})
        label_name = label.get("name") if isinstance(label, dict) else None

        formats = self._parse_formats(releases if isinstance(releases, list) else [])

        # creditText often holds copyright rather than a catalog number;
        # keep only short, non-copyright strings.
        catalog = data.get("creditText") or ""
        if len(catalog) > 30 or catalog.startswith("©"):
            catalog = ""

        # JSON-LD lists only the supporters the page renders (80 at the time of
        # writing) and offers a "more" link for the rest, so this is a floor,
        # not a total. num_supporters_capped says which one you are looking at.
        sponsors = data.get("sponsor", [])
        num_supporters = len(sponsors) if isinstance(sponsors, list) else 0
        supporters_capped = bool(self.soup.select_one(".collected-by .more-thumbs"))

        return {
            "_type": "album",
            "album_id": find_property(release.get("additionalProperty", []), "item_id"),
            "artist_id": find_property(publisher.get("additionalProperty", []), "band_id"),
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
            "num_supporters_capped": supporters_capped,
            # Track pages describe a single recording, so the album-level
            # duration is that track's; album pages have no duration of
            # their own and leave this None.
            "duration": track_time_to_seconds(data.get("duration")) if is_track else None,
        }

    @staticmethod
    def _parse_formats(releases: list) -> list[str]:
        """Extract unique media format names from albumRelease entries."""
        seen = []
        for release in releases:
            format_type = release.get("musicReleaseFormat")
            name = _FORMAT_MAP.get(format_type)
            if name and name not in seen:
                seen.append(name)
        return seen

    def _parse_artist(self, data: dict) -> dict:
        """Extract the publisher (artist/label) info embedded in album JSON-LD."""
        publisher = data.get("publisher", {})
        return {
            "_type": "artist",
            "artist_id": find_property(publisher.get("additionalProperty", []), "band_id"),
            "name": publisher.get("name"),
            "url": publisher.get("@id"),
            "location": publisher.get("foundingLocation", {}).get("name"),
            "bio": publisher.get("description"),
        }

    def _parse_tracks(self, data: dict, album_id: str | None) -> list[dict]:
        """Extract the tracklist from JSON-LD itemListElement entries.

        Track pages are ``MusicRecording`` and carry no itemListElement: they
        describe one recording, so the tracklist is that single track. Without
        this fallback, opening a ``/track/`` URL yields an empty tracklist.
        """
        track_list = data.get("track")
        entries = track_list.get("itemListElement", []) if isinstance(track_list, dict) else []
        if entries:
            return [
                self._parse_track(entry.get("item", {}), album_id, entry.get("position")) for entry in entries
            ]
        if data.get("@type") == "MusicRecording":
            return [self._parse_track(data, album_id, None)]
        return []

    @staticmethod
    def _parse_track(track: dict, album_id: str | None, position) -> dict:
        """Build one track dict from a JSON-LD recording node."""
        duration_raw = track.get("duration")
        by_artist = track.get("byArtist")
        track_artist = by_artist.get("name") if isinstance(by_artist, dict) else None

        recording_of = track.get("recordingOf", {})
        lyrics_obj = recording_of.get("lyrics", {}) if isinstance(recording_of, dict) else {}
        lyrics = lyrics_obj.get("text") if isinstance(lyrics_obj, dict) else None

        properties = track.get("additionalProperty", [])
        if position is None:
            # Track pages carry the number as a property instead of a position.
            position = find_property(properties, "tracknum")

        return {
            "_type": "track",
            "track_id": find_property(properties, "track_id"),
            "album_id": album_id,
            "position": position,
            "title": track.get("name"),
            "track_url": strip_tracker(track.get("@id")),
            "artist": track_artist,
            "duration": format_track_time(duration_raw),
            "duration_raw": duration_raw,
            "lyrics": lyrics,
        }


class ArtistPageParser(BasePageParser):
    """Parse a Bandcamp artist/label page.

    Extracts profile info (name, location, bio, image) from the root page
    and the discography grid from the ``/music`` subpage. Set
    ``discography_only=True`` to skip profile extraction when the caller
    has already fetched it from the root page.
    """

    def __init__(self, soup: BeautifulSoup, url: str, *, discography_only: bool = False):
        super().__init__(soup, url)
        self._discography_only = discography_only

    def parse(self) -> dict | None:
        if self._discography_only:
            try:
                discography = self._parse_discography()
            except Exception as e:
                logger.warning(f"Failed to parse discography on {self.url}: {e}")
                discography = []
            return {"_type": "artist", "discography": discography}

        try:
            artist = self._parse_profile()
        except Exception as e:
            logger.error(f"Failed to parse artist profile on {self.url}: {e}")
            return None

        try:
            artist["discography"] = self._parse_discography()
        except Exception as e:
            logger.warning(f"Failed to parse discography on {self.url}: {e}")
            artist["discography"] = []

        return artist

    def _parse_profile(self) -> dict:
        """Extract artist profile from the page HTML (name, location, bio, image).

        Selectors rather than ``find(..., class_=...)`` throughout: ``find`` can
        hand back a ``NavigableString``, which is a ``str`` subclass whose own
        ``find`` takes no keyword arguments, so a nested lookup on one raises
        ``TypeError`` and loses the whole profile. ``select_one`` only ever
        returns a tag or ``None``, which removes the failure mode instead of
        catching it downstream.
        """
        name = ""
        location = None

        name_el = self.soup.select_one("p#band-name-location")
        if name_el:
            name_span = name_el.select_one("span.title")
            name = name_span.get_text().strip() if name_span else ""
            loc_span = name_el.select_one("span.location")
            location = loc_span.get_text().strip() if loc_span else None

        # p#bio-text has the full bio (including hidden .peekaboo-text span),
        # but also a .peekaboo-link ("... more") that we strip out
        bio_el = self.soup.select_one("p#bio-text")
        bio = None
        if bio_el:
            link = bio_el.select_one("span.peekaboo-link")
            if link:
                link.decompose()
            bio = clean_text(bio_el.get_text())

        img_el = self.soup.select_one("img.band-photo")
        image_url = img_el.get("src") if img_el else None

        # Extract artist_id from embedded page data
        band_el = self.soup.select_one("[data-band-id]")
        band_id = band_el.get("data-band-id") if band_el else None

        # Label link ("more from Napalm Records" -> label page)
        label_name = None
        label_url = None
        label_link = self.soup.select_one("a.back-to-label-link")
        if label_link:
            label_url = label_link.get("href", "").split("?")[0]
            label_span = label_link.select_one("span.back-link-text")
            if label_span:
                label_text = clean_text(label_span.get_text())
                # Strip "more from" prefix
                if "from" in label_text.lower():
                    label_name = label_text.split("from", 1)[1].strip()
                else:
                    label_name = label_text

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

        items = []

        for grid_item in music_grid.find_all("li"):
            link = grid_item.find("a")
            if not link:
                continue

            title = ""
            artist_name = None
            title_el = grid_item.find("p", class_="title")
            if title_el:
                artist_span = title_el.find("span", class_="artist-override")
                if artist_span:
                    artist_name = clean_text(artist_span.extract().get_text())
                title = clean_text(title_el.get_text())

            data_item_id = grid_item.get("data-item-id", "")
            item_type = data_item_id.split("-", 1)[0] if "-" in data_item_id else None

            art_el = grid_item.find("img")
            artwork_url = art_el.get("src") if art_el else None

            items.append(
                {
                    "_type": "album",
                    "title": title,
                    "artist_name": artist_name,
                    "item_type": item_type,
                    # Label grids link with a ?label=...&tab=music referrer.
                    "url": strip_tracker(urljoin(self.url, link.get("href", ""))),
                    "art_url": artwork_url,
                }
            )

        client_items_json = music_grid.get("data-client-items")
        if client_items_json:
            try:
                extra_items = json.loads(client_items_json)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse data-client-items on {self.url}: {e}")
                extra_items = []
            for entry in extra_items:
                items.append(
                    {
                        "_type": "album",
                        "title": entry.get("title", ""),
                        "artist_name": entry.get("artist"),
                        "item_type": entry.get("type"),
                        "url": strip_tracker(urljoin(self.url, entry.get("page_url", ""))),
                        "art_url": None,
                    }
                )

        return items
