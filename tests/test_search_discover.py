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


def test_discover_api_exception_payload_raises():
    payload = {"__api_special__": "exception", "error_type": "Endpoints::BadParamValueError"}
    with pytest.raises(FetchError, match="BadParamValueError"):
        DiscoverWebAPI(ScriptedClient(payload)).discover(tags=["x"])


def test_discover_maps_results():
    results, cursor, total = DiscoverWebAPI(ScriptedClient(batch(0, 2))).discover(tags=["x"])
    assert [r["album_id"] for r in results] == ["1", "2"]
    assert (cursor, total) == ("next", 1000)


CAMEL_ITEM = {
    "itemId": 989872500,
    "itemType": "a",
    "resultType": "a",
    "title": "A Witch's Remixes",
    "itemUrl": "https://tinywitch.bandcamp.com/album/a-witchs-remixes?from=discover_page",
    "primaryImage": {"imageId": 1133450919, "isArt": True},
    "bandId": 393163998,
    "albumArtist": None,
    "bandName": "Tiny Witch",
    "bandUrl": "https://tinywitch.bandcamp.com?from=discover_page",
    "price": {"amount": 50, "currency": "EUR", "is_money": True},
    "bandLocation": "Veneto, Italy",
    "isAlbumPreorder": False,
    "releaseDate": "2026-11-06 00:00:00 UTC",
    "trackCount": 7,
    "duration": 1534.0,
}

CAMEL_RESPONSE = {
    "results": [CAMEL_ITEM],
    "resultCount": 39773,
    "batchResultCount": 40,
    "discoverSpecId": 1045,
    "isFollowingDiscoverSpec": None,
    "cursor": "AoNwmML92KEDCDyTdLwrYTIwMjIwNjQxMTc=",
}

SNAKE_ITEM = {
    "item_id": 989872500,
    "item_type": "a",
    "title": "A Witch's Remixes",
    "item_url": "https://tinywitch.bandcamp.com/album/a-witchs-remixes?from=discover_page",
    "primary_image": {"image_id": 1133450919, "is_art": True},
    "band_id": 393163998,
    "album_artist": None,
    "band_name": "Tiny Witch",
    "band_url": "https://tinywitch.bandcamp.com?from=discover_page",
    "price": {"amount": 50, "currency": "EUR", "is_money": True},
    "band_location": "Veneto, Italy",
    "is_album_preorder": False,
    "release_date": "2026-11-06 00:00:00 UTC",
    "track_count": 7,
    "duration": 1534.0,
}

EXPECTED_DISCOVER_RESULT = {
    "_type": "album",
    "album_id": "989872500",
    "artist_name": "Tiny Witch",
    "album_artist": None,
    "title": "A Witch's Remixes",
    "url": "https://tinywitch.bandcamp.com/album/a-witchs-remixes",
    "artist_url": "https://tinywitch.bandcamp.com",
    "artist_id": "393163998",
    "art_id": "1133450919",
    "genre": "",
    "item_type": "a",
    "release_date": "2026-11-06 00:00:00 UTC",
    "location": "Veneto, Italy",
    "track_count": 7,
    "duration": 1534.0,
    "price": {"amount": 50, "currency": "EUR", "is_money": True},
    "is_preorder": False,
}


def test_discover_parses_camel_case():
    results, cursor, total = DiscoverWebAPI(ScriptedClient(CAMEL_RESPONSE)).discover(tags=["x"])
    assert results == [EXPECTED_DISCOVER_RESULT]
    assert cursor == CAMEL_RESPONSE["cursor"]
    assert total == 39773


def test_discover_still_parses_snake_case():
    response = {"results": [SNAKE_ITEM], "result_count": 39773, "cursor": "c"}
    results, cursor, total = DiscoverWebAPI(ScriptedClient(response)).discover(tags=["x"])
    assert results == [EXPECTED_DISCOVER_RESULT]
    assert (cursor, total) == ("c", 39773)


def test_discover_total_comes_from_result_count_not_the_batch_count():
    assert DiscoverWebAPI(ScriptedClient(CAMEL_RESPONSE)).discover(tags=["x"])[2] == 39773


def test_discover_keeps_false_and_zero_under_camel_case():
    item = {**CAMEL_ITEM, "trackCount": 0, "is_album_preorder": True, "track_count": 9}
    response = {**CAMEL_RESPONSE, "results": [item]}
    result = DiscoverWebAPI(ScriptedClient(response)).discover(tags=["x"])[0][0]
    assert result["is_preorder"] is False
    assert result["track_count"] == 0


def test_discover_falls_back_per_field():
    item = {"itemId": 1, "band_id": 2, "primaryImage": {"image_id": 3}, "band_name": "B"}
    results, _, total = DiscoverWebAPI(ScriptedClient({"results": [item], "result_count": 5})).discover(
        tags=["x"]
    )
    assert (results[0]["album_id"], results[0]["artist_id"], results[0]["art_id"]) == ("1", "2", "3")
    assert results[0]["artist_name"] == "B"
    assert total == 5


def test_discover_batch_with_no_ids_raises():
    response = {"results": [{"title": "A"}, {"title": "B"}], "cursor": "c", "result_count": 2}
    with pytest.raises(FetchError, match="no id"):
        DiscoverWebAPI(ScriptedClient(response)).discover(tags=["x"])


def test_discover_one_item_without_an_id_is_not_a_failure():
    response = {"results": [{"title": "A"}, {"itemId": 7, "title": "B"}], "cursor": "c"}
    results = DiscoverWebAPI(ScriptedClient(response)).discover(tags=["x"])[0]
    assert [r["album_id"] for r in results] == [None, "7"]


# ── the pager ────────────────────────────────────────────────────────────


def test_pager_retries_a_failed_batch_instead_of_ending_the_feed():
    client = ScriptedClient(batch(0, 40), FetchError("timeout"), batch(40, 40))
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"])
    assert len(fetch(0, 40)[0]) == 40
    with pytest.raises(FetchError):
        fetch(40, 40)
    items, total = fetch(40, 40)  # retried, not treated as the end
    assert [i["album_id"] for i in items][:2] == ["41", "42"]
    assert total == 120


def test_pager_offers_one_page_ahead_rather_than_the_feed_total():
    client = ScriptedClient(batch(0, 40, total=39773))
    fetch = DiscoverWebAPI(client).make_page_fetcher(tags=["x"])
    assert fetch(0, 40)[1] == 80  # a jump to the "last" page costs one batch, not the whole feed
    assert len(client.calls) == 1


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
