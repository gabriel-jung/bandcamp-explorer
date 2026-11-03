"""Search, discover, the discover pager and location lookup: failures must not read as empty."""

import threading

import pytest

from bandcamp_explorer.core import countries
from bandcamp_explorer.core.api import DiscoverWebAPI, SearchAPI
from bandcamp_explorer.core.client import ChallengeError, FetchError
from bandcamp_explorer.core.utils import format_track_time, track_time_to_seconds

from .conftest import FakeResponse, FakeSession, make_client


class ScriptedClient:
    """A client whose post_json answers from a list (dicts, or exceptions to raise)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def post_json(self, url, payload, crawl=False):
        self.calls.append(payload)
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def batch(start, n, cursor="next", total=1000):
    results = [
        {"item_id": start + i + 1, "title": f"R{start + i}", "item_url": f"https://a.bc/album/{start + i}"}
        for i in range(n)
    ]
    return {"results": results, "cursor": cursor, "result_count": total}


# ── post_json ────────────────────────────────────────────────────────────


def test_post_json_raises_fetch_error_on_http_error():
    FakeSession.responses = {"chrome": [FakeResponse(500)]}
    with pytest.raises(FetchError) as excinfo:
        make_client().post_json("https://bandcamp.com/api/x", {})
    assert excinfo.value.url == "https://bandcamp.com/api/x"


def test_post_json_raises_fetch_error_on_non_json_body():
    FakeSession.responses = {"chrome": [FakeResponse(200, "<html>oops</html>")]}
    with pytest.raises(FetchError):
        make_client().post_json("https://bandcamp.com/api/x", {})


def test_challenge_error_is_a_fetch_error():
    assert issubclass(ChallengeError, FetchError)


# ── search and discover ──────────────────────────────────────────────────


def test_search_error_payload_raises():
    api = SearchAPI(ScriptedClient({"error": True, "error_message": "bad function"}))
    with pytest.raises(FetchError, match="bad function"):
        api.search("x")


def test_search_with_no_hits_is_empty():
    api = SearchAPI(ScriptedClient({"auto": {"results": None}}))
    assert api.search("x") == []


def test_discover_error_payload_raises():
    api = DiscoverWebAPI(ScriptedClient({"error": True}))
    with pytest.raises(FetchError):
        api.discover(tags=["dungeon-synth"])


def test_discover_maps_results():
    results, cursor, total = DiscoverWebAPI(ScriptedClient(batch(0, 2))).discover(tags=["x"])
    assert [r["album_id"] for r in results] == ["1", "2"]
    assert (cursor, total) == ("next", 1000)


# ── the pager ────────────────────────────────────────────────────────────


def test_pager_retries_a_failed_batch_instead_of_ending_the_feed():
    client = ScriptedClient(batch(0, 40), FetchError("timeout"), batch(40, 40))
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"])
    assert len(fetch(0, 40)[0]) == 40
    with pytest.raises(FetchError):
        fetch(40, 40)
    items, total = fetch(40, 40)  # retried, not treated as the end
    assert [i["album_id"] for i in items][:2] == ["41", "42"]
    assert total == 1000


def test_pager_estimate_never_shrinks():
    client = ScriptedClient(batch(0, 100, total=0))  # server gives no total
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"], batch_size=100)
    _, first = fetch(60, 40)
    _, second = fetch(0, 40)
    assert second >= first


def test_pager_is_exact_once_exhausted():
    client = ScriptedClient(batch(0, 30, cursor=None, total=500))
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"])
    assert fetch(0, 40)[1] == 30


def test_pager_serializes_concurrent_calls():
    gate = threading.Event()

    class SlowClient(ScriptedClient):
        def post_json(self, url, payload, crawl=False):
            gate.wait(1)
            return super().post_json(url, payload, crawl)

    client = SlowClient(batch(0, 40), batch(40, 40))
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"])
    out = []
    threads = [threading.Thread(target=lambda: out.append(fetch(40, 25))) for _ in range(2)]
    for th in threads:
        th.start()
    gate.set()
    for th in threads:
        th.join()
    assert len(client.calls) == 2  # two batches total, not three or four
    assert out[0][0] == out[1][0]


# ── location lookup and cache ────────────────────────────────────────────


@pytest.fixture
def scratch_cache(tmp_path, monkeypatch):
    path = tmp_path / "geonames.json"
    monkeypatch.setattr(countries, "GEONAME_CACHE_FILE", path)
    countries._MEMORY.clear()
    yield path
    countries._MEMORY.clear()


def test_location_lookup_error_raises(scratch_cache):
    with pytest.raises(FetchError) as excinfo:
        countries.resolve_geoname(
            ScriptedClient({"ok": False, "error": True, "error_message": "boom"}), "paris"
        )
    assert excinfo.value.url == countries.GEONAME_SEARCH_URL


def test_location_not_ok_is_a_failure(scratch_cache):
    # Measured: an unknown place answers ok: true with no results, so ok: false is not "unknown".
    with pytest.raises(FetchError):
        countries.resolve_geoname(ScriptedClient({"ok": False}), "q")


def test_error_message_alone_counts_as_an_error_payload():
    with pytest.raises(FetchError, match="bad function"):
        SearchAPI(ScriptedClient({"error_message": "bad function"})).search("x")


def test_challenge_error_rebuilds_like_any_fetch_error():
    error = ChallengeError("https://x")
    rebuilt = type(error)(str(error), url=error.url)
    assert rebuilt.url == "https://x"


def test_post_json_rejects_json_that_is_not_an_object():
    FakeSession.responses = {"chrome": [FakeResponse(200, "null")]}
    with pytest.raises(FetchError):
        make_client().post_json("https://bandcamp.com/api/x", {})


def test_fetch_error_url_survives_pickling():
    import pickle

    error = pickle.loads(pickle.dumps(FetchError("boom", url="https://x")))
    assert (str(error), error.url) == ("boom", "https://x")


def test_unknown_location_is_none(scratch_cache):
    assert countries.resolve_geoname(ScriptedClient({"ok": True, "results": []}), "nowhere") is None


def test_unwritable_cache_still_returns_the_id(scratch_cache, monkeypatch):
    monkeypatch.setattr(countries, "GEONAME_CACHE_FILE", scratch_cache.parent / "missing" / "x" / "g.json")
    monkeypatch.setattr(countries.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    client = ScriptedClient(
        {"ok": True, "results": [{"id": "2988507", "name": "Paris", "fullname": "Paris, FR"}]}
    )
    assert countries.resolve_geoname(client, "paris") == 2988507


# ── durations: unknown is None, never 0 ──────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("P00H03M45S", 225),
        ("PT3M45S", 225),
        ("P01H02M03S", 3723),
        ("P00H00M00S", 0),  # a real zero stays zero
        ("", None),
        (None, None),
        ("P", None),
        ("PT", None),
        ("3:45", None),
        ("garbage", None),
    ],
)
def test_track_time_unknown_is_none_never_zero(raw, seconds):
    assert track_time_to_seconds(raw) == seconds


def test_format_track_time():
    assert format_track_time("P00H03M45S") == "3:45"
    assert format_track_time("P01H02M03S") == "1:02:03"
    assert format_track_time("PT") is None
