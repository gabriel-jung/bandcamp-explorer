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
from bandcamp_explorer.core.client import ChallengeBackoffError, ChallengeError, NotFoundError
from bandcamp_explorer.core.parsers import ArtistPageParser

from .conftest import FakeResponse, FakeSession, make_client
from .test_artist_parser import INDEX_BODY, INDEX_RELEASE_URLS, INDEX_URL, RELEASE_BODY, RELEASE_URL
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


# A live label whose root is set to its subscription page: no band id, no name,
# the same parse as a dead host's signup page.
SUBSCRIBE_BODY = (
    "<html><head><title>Subscribe to Live Label | Live Label</title></head>"
    "<body><div id='subscribe-form'><input name='email'></div></body></html>"
)

# The same label's /music page, which carries the whole artist.
LIVE_MUSIC_BODY = LIVE_BODY.replace("</body>", MUSIC_GRID_BODY.split("<body>")[1])


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
    """Only a real 404 concludes anything. A /music page that names nobody
    either gives no usable artist, so this is a failed fetch to retry. It must
    also not fall through to the discography fetch: that would send a second
    request to the /music URL just fetched, this time through ``client.get``,
    where a challenge arms the backoff the confirmation exists to get ahead
    of."""
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


def test_an_index_page_artist_is_not_a_deleted_host():
    """The custom index-page theme has neither of the standard layout's
    markers, so before it was parsed its root yielded no id and no name: the
    signature this module reads as gone, from a live artist with releases."""
    FakeSession.by_url = {INDEX_URL: FakeResponse(200, INDEX_BODY)}

    artist = artist_api().get(INDEX_URL, fetch_art=False)

    assert artist is not None
    assert artist["name"] == "wizard ashdod"
    assert [item["url"] for item in artist["discography"]] == INDEX_RELEASE_URLS
    assert FakeSession.calls == [("chrome", INDEX_URL)]


def test_a_subscribe_page_root_takes_the_artist_from_the_music_page():
    """A live root can name nobody, and the /music page fetched to confirm the
    host then holds everything. Returning None there made callers count a live
    label as unparseable until they retired it."""
    FakeSession.by_url = {
        LIVE: FakeResponse(200, SUBSCRIBE_BODY),
        LIVE_MUSIC: FakeResponse(200, LIVE_MUSIC_BODY),
    }

    artist = artist_api().get(LIVE, fetch_art=False)

    assert artist["artist_id"] == "266336502"
    assert artist["name"] == "Live Artist"
    assert artist["url"] == LIVE
    assert [item["title"] for item in artist["discography"]] == ["One"]
    assert FakeSession.calls == [("chrome", LIVE), ("chrome", LIVE_MUSIC)]


def test_a_subscribe_page_root_with_an_empty_music_grid_fetches_music_once():
    """An artist read off /music already has that page's discography, empty or
    not, so the supplementary fetch would only ask for the same page again."""
    FakeSession.by_url = {LIVE: FakeResponse(200, SUBSCRIBE_BODY), LIVE_MUSIC: FakeResponse(200, LIVE_BODY)}

    artist = artist_api().get(LIVE, fetch_art=False)

    assert artist["name"] == "Live Artist"
    assert artist["discography"] == []
    assert FakeSession.calls == [("chrome", LIVE), ("chrome", LIVE_MUSIC)]


def test_a_subscribe_page_root_with_a_challenged_music_page_raises_challenge():
    """The interstitial is "ask again later". Returned as None it would read as
    a page that parsed to nothing, the outcome that got live labels retired, so
    it goes through ``client.get`` like any challenged page: the backoff arms
    and ``ChallengeError`` is raised. Never parsed as an artist."""
    FakeSession.by_url = {
        LIVE: FakeResponse(200, SUBSCRIBE_BODY),
        LIVE_MUSIC: FakeResponse(200, CHALLENGE_BODY),
    }
    api = artist_api()

    with pytest.raises(ChallengeError):
        api.get(LIVE, fetch_art=False)

    assert FakeSession.calls == [("chrome", LIVE), ("chrome", LIVE_MUSIC), ("chrome", LIVE_MUSIC)]
    with pytest.raises(ChallengeBackoffError):
        api.get(LIVE, fetch_art=False)


def test_a_subscribe_page_root_with_a_404_music_page_is_still_a_deleted_host():
    FakeSession.by_url = {LIVE: FakeResponse(200, SUBSCRIBE_BODY), LIVE_MUSIC: FakeResponse(404)}

    with pytest.raises(NotFoundError):
        artist_api().get(LIVE, fetch_art=False)


def test_a_subscribe_page_root_whose_music_page_fails_is_a_failed_fetch():
    FakeSession.by_url = {LIVE: FakeResponse(200, SUBSCRIBE_BODY), LIVE_MUSIC: FakeResponse(503)}

    assert artist_api().get(LIVE, fetch_art=False) is None
    assert FakeSession.calls == [("chrome", LIVE), ("chrome", LIVE_MUSIC)]


def test_a_single_release_artist_lists_the_release_its_music_page_redirects_to():
    """Bandcamp redirects both the root and /music of a single-release artist to
    that release. The root names the artist; /music is where the release is
    read from, as the one item of the discography."""
    FakeSession.by_url = {LIVE: FakeResponse(200, RELEASE_BODY), LIVE_MUSIC: FakeResponse(200, RELEASE_BODY)}

    artist = artist_api().get(LIVE, fetch_art=False)

    assert artist["name"] == "Solo"
    assert [item["url"] for item in artist["discography"]] == [RELEASE_URL]


def test_a_subscribe_page_root_of_a_single_release_artist_lists_its_release():
    FakeSession.by_url = {
        LIVE: FakeResponse(200, SUBSCRIBE_BODY),
        LIVE_MUSIC: FakeResponse(200, RELEASE_BODY),
    }

    artist = artist_api().get(LIVE, fetch_art=False)

    assert artist["url"] == LIVE
    assert [item["url"] for item in artist["discography"]] == [RELEASE_URL]
    assert FakeSession.calls == [("chrome", LIVE), ("chrome", LIVE_MUSIC)]


def test_a_404_on_the_re_asked_music_page_names_the_root():
    """A challenged confirmation is re-asked through the client. A 404 there is
    the same evidence as the confirmation's own, and callers key on the root."""
    FakeSession.by_url = {LIVE: FakeResponse(200, SUBSCRIBE_BODY)}
    # Not keyed by URL: the two /music answers come off the queue in order.
    FakeSession.responses = {"chrome": [FakeResponse(200, CHALLENGE_BODY), FakeResponse(404)]}

    with pytest.raises(NotFoundError) as excinfo:
        artist_api().get(LIVE, fetch_art=False)

    assert excinfo.value.url == LIVE
