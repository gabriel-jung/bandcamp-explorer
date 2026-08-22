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

from bandcamp_explorer.core.client import BandcampClient, ChallengeError, NotFoundError

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


def test_a_fingerprint_that_answers_is_promoted_for_later_requests():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome124": [FakeResponse(200, "first"), FakeResponse(200, "second")],
    }
    client = make_client(fallback_impersonate=("chrome124",))
    blocked_session = client._session

    client.get("https://x.bandcamp.com/album/a")
    client.get("https://x.bandcamp.com/album/b")

    assert client.impersonate == "chrome124"
    assert blocked_session.closed
    assert FakeSession.calls == [
        ("chrome", "https://x.bandcamp.com/album/a"),
        ("chrome124", "https://x.bandcamp.com/album/a"),
        ("chrome124", "https://x.bandcamp.com/album/b"),
    ]


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


def test_a_challenge_on_the_fallback_is_not_reported_as_a_404():
    FakeSession.responses = {
        "chrome": [FakeResponse(404)],
        "chrome124": [FakeResponse(200, CHALLENGE_BODY)],
    }
    client = make_client(fallback_impersonate=("chrome124",))

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
