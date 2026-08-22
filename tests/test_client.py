"""Tests for the HTTP client's fingerprint selection and 404 fallback ladder.

Bandcamp soft-blocks some TLS fingerprints by answering HTTP 404 to pages a
different fingerprint fetches fine, so a bare 404 is not proof of deletion.
These tests pin the two behaviours that keep live pages from being flagged
deleted: the caller can pick a fingerprint, and the client re-checks a 404
against known-good fallbacks before letting ``NotFoundError`` escape.
"""

from typing import ClassVar

import pytest
from curl_cffi import requests as curl_requests

from bandcamp_explorer.core.client import (
    FALLBACK_IMPERSONATE,
    SUGGESTED_FALLBACK_IMPERSONATE,
    BandcampClient,
    ChallengeError,
    NotFoundError,
)

CHALLENGE_BODY = "<html><head><title>Client Challenge</title></head></html>"


class FakeResponse:
    def __init__(self, status_code: int, text: str = "ok"):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise curl_requests.exceptions.HTTPError(f"status {self.status_code}")


class FakeSession:
    """Stands in for ``curl_cffi.requests.Session``, keyed by impersonate name."""

    #: impersonate name -> list of responses to hand out, in order.
    responses: ClassVar[dict[str, list[FakeResponse]]] = {}
    #: every (impersonate, url) pair requested, across all sessions.
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, impersonate: str):
        self.impersonate = impersonate
        self.closed = False

    def get(self, url, params=None, timeout=None):
        FakeSession.calls.append((self.impersonate, url))
        queue = FakeSession.responses.get(self.impersonate)
        if not queue:
            raise AssertionError(f"unexpected GET on {self.impersonate}: {url}")
        return queue.pop(0)

    def post(self, url, json=None, timeout=None):
        FakeSession.calls.append((self.impersonate, url))
        return FakeSession.responses[self.impersonate].pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_session(monkeypatch):
    FakeSession.responses = {}
    FakeSession.calls = []
    monkeypatch.setattr(curl_requests, "Session", lambda impersonate=None, **kw: FakeSession(impersonate))
    return FakeSession


def make_client(**kwargs) -> BandcampClient:
    client = BandcampClient(**kwargs)
    client.rate_limit_seconds = 0.0
    client.crawl_delay = 0.0
    return client


def test_default_impersonate_is_the_floating_chrome_alias():
    assert make_client().impersonate == "chrome"


def test_impersonate_argument_selects_the_session_fingerprint():
    client = make_client(impersonate="chrome124")

    assert client.impersonate == "chrome124"
    assert client._session.impersonate == "chrome124"


def test_a_404_is_rechecked_on_the_fallback_fingerprint():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome124": [FakeResponse(200, "the real page")],
    }
    client = make_client(fallback_impersonate=("chrome124",))

    assert client.get("https://x.bandcamp.com/album/a") == "the real page"


def test_a_promoted_fingerprint_takes_over_and_the_blocked_one_is_closed():
    FakeSession.responses = {
        "chrome": [FakeResponse(404), FakeResponse(404)],
        "chrome124": [FakeResponse(200, "first"), FakeResponse(200, "second"), FakeResponse(200, "third")],
    }
    client = make_client(fallback_impersonate=("chrome124",))
    blocked_session = client._session

    client.get("https://x.bandcamp.com/album/a")
    client.get("https://x.bandcamp.com/album/b")
    client.get("https://x.bandcamp.com/album/c")

    assert client.impersonate == "chrome124"
    assert blocked_session.closed
    assert FakeSession.calls[-1] == ("chrome124", "https://x.bandcamp.com/album/c")


def test_a_404_from_every_fingerprint_still_raises_not_found():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome131": [FakeResponse(404)],
        "chrome124": [FakeResponse(404)],
    }
    client = make_client(fallback_impersonate=("chrome131", "chrome124"))

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert client.impersonate == "chrome"


def test_an_empty_fallback_list_makes_no_extra_request():
    FakeSession.responses = {"chrome": [FakeResponse(404)]}
    client = make_client(fallback_impersonate=())

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert FakeSession.calls == [("chrome", "https://x.bandcamp.com/album/gone")]


def test_the_primary_fingerprint_is_dropped_from_its_own_fallback_list():
    FakeSession.responses = {"chrome124": [FakeResponse(404)]}
    client = make_client(impersonate="chrome124", fallback_impersonate=("chrome124",))

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert FakeSession.calls == [("chrome124", "https://x.bandcamp.com/album/gone")]


def test_fallback_targets_curl_cffi_does_not_know_are_skipped():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome124": [FakeResponse(200, "the real page")],
    }
    client = make_client(fallback_impersonate=("chrome_from_the_future", "chrome124"))

    assert client.get("https://x.bandcamp.com/album/a") == "the real page"


def test_a_challenged_fallback_is_skipped_for_the_next_one():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome131": [FakeResponse(200, CHALLENGE_BODY)],
        "chrome124": [FakeResponse(200, "the real page")],
    }
    client = make_client(fallback_impersonate=("chrome131", "chrome124"))

    assert client.get("https://x.bandcamp.com/album/a") == "the real page"


def test_a_challenged_fallback_does_not_arm_the_backoff_on_the_primary():
    """A throwaway fallback session being challenged says nothing about the
    primary. Arming the shared backoff there would turn every genuine 404 into
    a two-minute stall for the whole client."""
    FakeSession.responses = {
        "chrome": [FakeResponse(404), FakeResponse(200, "a later page")],
        "chrome124": [FakeResponse(200, CHALLENGE_BODY)],
    }
    client = make_client(fallback_impersonate=("chrome124",))

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert client.impersonate == "chrome"
    assert client.get("https://x.bandcamp.com/album/b") == "a later page"


def test_a_challenge_on_the_primary_still_raises():
    FakeSession.responses = {"chrome": [FakeResponse(200, CHALLENGE_BODY)]}
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")


def test_a_transport_failure_on_the_fallback_leaves_the_404_standing():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome124": [FakeResponse(503)],
    }
    client = make_client(fallback_impersonate=("chrome124",))

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/a")

    assert client.impersonate == "chrome"


def test_not_found_error_stays_outside_the_request_exception_hierarchy():
    assert not issubclass(NotFoundError, curl_requests.exceptions.RequestException)


def test_the_ladder_is_off_by_default():
    """Nobody pays for the re-check unless they asked for it. The blocking it
    guards against is environment specific, and with it on every genuine 404
    costs an extra request per fallback."""
    assert FALLBACK_IMPERSONATE == ()
    assert make_client()._fallback_impersonate == ()


def test_the_default_makes_no_extra_request_on_a_404():
    FakeSession.responses = {"chrome": [FakeResponse(404)]}
    client = make_client()

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert FakeSession.calls == [("chrome", "https://x.bandcamp.com/album/gone")]


def test_the_suggested_ladder_spans_more_than_one_browser_family():
    """Two Chrome builds share a failure axis: a block that targets a range of
    Chrome builds can catch both fallbacks at once, whichever side it hits.
    Firefox and Safari are off that axis, so they fail independently."""
    families = {"".join(c for c in name if not c.isdigit()) for name in SUGGESTED_FALLBACK_IMPERSONATE}

    assert len(families) > 1, f"all suggestions share one family: {SUGGESTED_FALLBACK_IMPERSONATE}"


def test_one_rescue_serves_the_page_without_promoting():
    """A single 404 on the primary can be a transient hiccup. Adopting another
    fingerprint for the client's whole life on that one observation is too big
    a step to take from one data point."""
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "firefox144": [FakeResponse(200, "the real page")],
    }
    client = make_client(fallback_impersonate=("firefox144",))

    assert client.get("https://x.bandcamp.com/album/a") == "the real page"
    assert client.impersonate == "chrome"


def test_a_second_rescue_by_the_same_fingerprint_promotes_it():
    FakeSession.responses = {
        "chrome": [FakeResponse(404), FakeResponse(404)],
        "firefox144": [FakeResponse(200, "first"), FakeResponse(200, "second"), FakeResponse(200, "third")],
    }
    client = make_client(fallback_impersonate=("firefox144",))

    client.get("https://x.bandcamp.com/album/a")
    client.get("https://x.bandcamp.com/album/b")

    assert client.impersonate == "firefox144"
    assert client.get("https://x.bandcamp.com/album/c") == "third"


def test_a_404_confirmed_by_a_fallback_says_which_one():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome131": [FakeResponse(200, CHALLENGE_BODY)],
        "firefox144": [FakeResponse(404)],
        "safari184": [FakeResponse(404)],
    }
    client = make_client(fallback_impersonate=("chrome131", "firefox144", "safari184"))

    with pytest.raises(NotFoundError) as excinfo:
        client.get("https://x.bandcamp.com/album/gone")

    assert excinfo.value.confirmed_by == ("firefox144", "safari184")


def test_an_unverifiable_404_names_no_confirming_fingerprint():
    """Every fallback challenged means nobody could check, which is not the
    same as a 404 two working fingerprints agreed on. Downstream needs the
    difference to decide between marking a row deleted and retrying later."""
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome131": [FakeResponse(200, CHALLENGE_BODY)],
        "firefox144": [FakeResponse(200, CHALLENGE_BODY)],
        "safari184": [FakeResponse(200, CHALLENGE_BODY)],
    }
    client = make_client(fallback_impersonate=("chrome131", "firefox144", "safari184"))

    with pytest.raises(NotFoundError) as excinfo:
        client.get("https://x.bandcamp.com/album/gone")

    assert excinfo.value.confirmed_by == ()


def test_a_404_with_the_ladder_disabled_confirms_nothing():
    FakeSession.responses = {"chrome": [FakeResponse(404)]}
    client = make_client(fallback_impersonate=())

    with pytest.raises(NotFoundError) as excinfo:
        client.get("https://x.bandcamp.com/album/gone")

    assert excinfo.value.confirmed_by == ()
