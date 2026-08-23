"""Shared HTTP fakes for the client and API tests.

Nothing here touches the network. Responses are keyed either by impersonate
name (a queue, for the fingerprint ladder) or by URL (for tests where the point
is which URL was asked, not in what order).
"""

from typing import ClassVar

import pytest
from curl_cffi import requests as curl_requests

from bandcamp_explorer.core.client import BandcampClient


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
    #: url -> response, consulted before the per-fingerprint queue.
    by_url: ClassVar[dict[str, FakeResponse]] = {}
    #: every (impersonate, url) pair requested, across all sessions.
    calls: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, impersonate: str):
        self.impersonate = impersonate
        self.closed = False

    def get(self, url, params=None, timeout=None):
        FakeSession.calls.append((self.impersonate, url))
        if url in FakeSession.by_url:
            return FakeSession.by_url[url]
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
    FakeSession.by_url = {}
    FakeSession.calls = []
    monkeypatch.setattr(curl_requests, "Session", lambda impersonate=None, **kw: FakeSession(impersonate))
    return FakeSession


def make_client(**kwargs) -> BandcampClient:
    client = BandcampClient(**kwargs)
    client.rate_limit_seconds = 0.0
    client.crawl_delay = 0.0
    return client
