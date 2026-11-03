"""Tests for the frontend adapters in ``app/common.py``."""

from unittest.mock import create_autospec

import pytest

from bandcamp_explorer.app import common
from bandcamp_explorer.app.common import AlbumFetcher, ArtistFetcher
from bandcamp_explorer.core.api import AlbumAPI, ArtistAPI
from bandcamp_explorer.core.client import NotFoundError


@pytest.fixture
def album_api(monkeypatch):
    # autospec enforces the real AlbumAPI.get signature, so forwarding an
    # option it does not take fails here exactly as it would in production.
    api = create_autospec(AlbumAPI, instance=True)
    api.get.return_value = {"_type": "album", "title": "X", "tracks": []}
    monkeypatch.setattr(common, "AlbumAPI", lambda client: api)
    return api


@pytest.fixture
def artist_api(monkeypatch):
    api = create_autospec(ArtistAPI, instance=True)
    api.get.return_value = {"_type": "artist", "name": "Y"}
    monkeypatch.setattr(common, "ArtistAPI", lambda client: api)
    return api


@pytest.mark.parametrize("options", [{}, {"full": True}, {"full": False}])
def test_album_fetcher_accepts_full(album_api, options):
    """Navigators pass ``full=``; AlbumAPI.get does not take it."""
    entity = AlbumFetcher(None, fetch_art=False).get("https://a.bandcamp.com/album/x", **options)
    assert entity["title"] == "X"
    album_api.get.assert_called_once_with("https://a.bandcamp.com/album/x", fetch_art=False)


@pytest.mark.parametrize("options", [{}, {"full": True}])
def test_artist_fetcher_accepts_full(artist_api, options):
    entity = ArtistFetcher(None).get("https://a.bandcamp.com", **options)
    assert entity["name"] == "Y"
    artist_api.get.assert_called_once_with("https://a.bandcamp.com", fetch_art=True)


def test_unknown_options_still_raise(album_api, artist_api):
    with pytest.raises(TypeError):
        AlbumFetcher(None).get("u", fetch_art=True)
    with pytest.raises(TypeError):
        ArtistFetcher(None).get("u", bogus=1)


def test_fetchers_still_absorb_not_found(album_api, artist_api):
    album_api.get.side_effect = NotFoundError("gone")
    artist_api.get.side_effect = NotFoundError("gone")
    assert AlbumFetcher(None).get("u", full=True) is None
    assert ArtistFetcher(None).get("u", full=True) is None
