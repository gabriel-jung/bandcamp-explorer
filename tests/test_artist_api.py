"""Tests for ``ArtistAPI.get`` telling a deleted host from a live artist.

The root page of a Bandcamp subdomain that no longer exists answers HTTP 200.
Depending on the address and session asking, it is either the bot-defence
interstitial (handled in the client) or Bandcamp's signup page, which parses
cleanly into an artist carrying neither an id nor a name. Neither status code
nor parse success separates it from a live artist, so ``/music`` is asked and a
404 there is what "gone" means.

The bodies below are distilled from real pages: only the markers the parser
reads are kept, since the served pages run to tens of kilobytes of markup that
none of this code looks at.
"""

import pytest

from bandcamp_explorer.core.api import ArtistAPI
from bandcamp_explorer.core.client import NotFoundError
from bandcamp_explorer.core.parsers import ArtistPageParser

from .conftest import FakeResponse, FakeSession, make_client
from .test_client import CHALLENGE_BODY

ROOT = "https://gone.bandcamp.com"
ROOT_MUSIC = "https://gone.bandcamp.com/music"
LIVE = "https://live.bandcamp.com"
LIVE_MUSIC = "https://live.bandcamp.com/music"

# What a dead host's root actually serves: a signup page. No band id, no name.
SIGNUP_BODY = (
    "<html><head><title>Signup | Bandcamp</title></head>"
    "<body><div id='signup-form'><input name='email'></div></body></html>"
)

# A live artist whose landing page is a single release: identity present, no
# music grid, so the discography comes from /music.
LIVE_BODY = (
    "<html><head><title>Live Artist</title></head>"
    "<body><div data-band-id='266336502'>"
    "<p id='band-name-location'><span class='title'>Live Artist</span>"
    "<span class='location'>Calabria, Italy</span></p>"
    "</div></body></html>"
)

MUSIC_GRID_BODY = (
    "<html><body><ol id='music-grid'>"
    "<li data-item-id='album-1'><a href='/album/one'><p class='title'>One</p></a></li>"
    "</ol></body></html>"
)


def artist_api(**kwargs) -> ArtistAPI:
    return ArtistAPI(make_client(**kwargs))


def test_a_signup_page_root_with_a_404_music_page_is_a_deleted_host():
    FakeSession.by_url = {ROOT: FakeResponse(200, SIGNUP_BODY), ROOT_MUSIC: FakeResponse(404)}

    with pytest.raises(NotFoundError):
        artist_api().get(ROOT, fetch_art=False)

    assert FakeSession.calls == [("chrome", ROOT), ("chrome", ROOT_MUSIC)]


def test_a_challenge_page_root_with_a_404_music_page_is_a_deleted_host():
    """The same dead host seen from an address Bandcamp challenges. The client
    confirms this one before arming its backoff, so it surfaces here the same
    way the signup form does."""
    FakeSession.by_url = {ROOT: FakeResponse(200, CHALLENGE_BODY), ROOT_MUSIC: FakeResponse(404)}

    with pytest.raises(NotFoundError):
        artist_api().get(ROOT, fetch_art=False)


def test_a_live_artist_whose_music_page_404s_keeps_an_empty_discography():
    """The regression this whole change must not cause. A live artist can have
    no ``/music`` subpage; the root already produced the profile, so a 404 on
    the supplementary fetch means "no grid", never "deleted". Turning it into
    ``NotFoundError`` would have callers flag a live host."""
    FakeSession.by_url = {LIVE: FakeResponse(200, LIVE_BODY), LIVE_MUSIC: FakeResponse(404)}

    artist = artist_api().get(LIVE, fetch_art=False)

    assert artist is not None
    assert artist["name"] == "Live Artist"
    assert artist["discography"] == []


def test_a_live_artist_takes_its_discography_from_the_music_page():
    FakeSession.by_url = {LIVE: FakeResponse(200, LIVE_BODY), LIVE_MUSIC: FakeResponse(200, MUSIC_GRID_BODY)}

    artist = artist_api().get(LIVE, fetch_art=False)

    assert [item["title"] for item in artist["discography"]] == ["One"]


def test_a_live_artist_with_a_grid_on_the_root_makes_no_confirmation_request():
    body = LIVE_BODY.replace("</body>", MUSIC_GRID_BODY.split("<body>")[1])
    FakeSession.by_url = {LIVE: FakeResponse(200, body)}

    artist = artist_api().get(LIVE, fetch_art=False)

    assert len(artist["discography"]) == 1
    assert FakeSession.calls == [("chrome", LIVE)]


def test_a_signup_page_root_whose_music_page_lives_is_a_failed_fetch_not_a_deletion():
    """Only a real 404 concludes anything. A root that named nobody is not a
    usable artist either, so this is a failed fetch to retry. It must also not
    fall through to the discography fetch: that would send a second request to
    the /music URL just fetched, this time through ``client.get``, where a
    challenge arms the backoff the confirmation exists to get ahead of."""
    FakeSession.by_url = {
        ROOT: FakeResponse(200, SIGNUP_BODY),
        ROOT_MUSIC: FakeResponse(200, MUSIC_GRID_BODY),
    }

    assert artist_api().get(ROOT, fetch_art=False) is None
    assert FakeSession.calls == [("chrome", ROOT), ("chrome", ROOT_MUSIC)]


def test_a_root_that_failed_to_parse_is_never_a_deletion(monkeypatch):
    """``parse()`` returns None when profile extraction raised, which is a live
    page whose markup moved or a truncated body. Reading that as a dead host
    would flag a live one on the next upstream redesign, so it must not even
    cost a confirmation request. Note the /music 404 staged here: if the trigger
    ever widens back to ``artist is None``, this test fails."""
    monkeypatch.setattr(ArtistPageParser, "parse", lambda self: None)
    FakeSession.by_url = {ROOT: FakeResponse(200, LIVE_BODY), ROOT_MUSIC: FakeResponse(404)}

    assert artist_api().get(ROOT, fetch_art=False) is None
    assert FakeSession.calls == [("chrome", ROOT)]


def test_a_404_on_the_root_still_propagates():
    FakeSession.by_url = {ROOT: FakeResponse(404)}

    with pytest.raises(NotFoundError):
        artist_api().get(ROOT, fetch_art=False)


def test_a_root_that_never_arrived_costs_no_confirmation_request():
    """A transport failure is a reason to retry, not evidence about the host,
    and paying a second request for every one of them is waste."""
    FakeSession.by_url = {LIVE: FakeResponse(503)}

    assert artist_api().get(LIVE, fetch_art=False) is None
    assert FakeSession.calls == [("chrome", LIVE)]
