"""Tests for the HTTP client: fingerprint selection, the 404 fallback ladder,
and the dead-host confirmation on ``/music``.

Two separate traps live here. Bandcamp can answer one TLS fingerprint a 404 on
a page another fetches fine, so a bare 404 is not proof of deletion; and the
root of a host that no longer exists never answers 404 at all, so a bare 200 is
not proof of life either. The tests pin both directions: a live page must never
raise ``NotFoundError``, and a host that is gone must not read as alive.
"""

import contextlib

import pytest
from curl_cffi import requests as curl_requests

from bandcamp_explorer.core.client import (
    CHALLENGE_FALLBACK_IMPERSONATE,
    CHALLENGE_PROMOTE_AFTER_RESCUES,
    FALLBACK_IMPERSONATE,
    SUGGESTED_FALLBACK_IMPERSONATE,
    ChallengeBackoffError,
    ChallengeError,
    HostRootCheck,
    NotFoundError,
)

from .conftest import FakeResponse, FakeSession, make_client

CHALLENGE_BODY = "<html><head><title>Client Challenge</title></head></html>"


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
    same as a 404 two working fingerprints agreed on. A caller needs the
    difference to tell "gone" from "could not tell"."""
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


# --- dead-host confirmation on /music -------------------------------------
#
# A subdomain that no longer exists does not 404 on its root. It answers 200
# with either the bot-defence interstitial or Bandcamp's signup page, depending
# on the address and session doing the asking. Only ``/music`` on that same
# host answers a plain 404, so that is what "gone" has to be read from.

ROOT = "https://gone.bandcamp.com"
ROOT_MUSIC = "https://gone.bandcamp.com/music"


def test_a_challenged_host_root_whose_music_404s_raises_not_found():
    """The reason this cannot live in a caller: ``_raise_challenge`` arms the
    backoff the instant it sees the interstitial, and every later URL is then
    refused locally without touching the network. Confirming has to happen
    before that, which only the client can do."""
    FakeSession.by_url = {
        ROOT: FakeResponse(200, CHALLENGE_BODY),
        ROOT_MUSIC: FakeResponse(404),
    }
    client = make_client()

    with pytest.raises(NotFoundError):
        client.get(ROOT)

    assert FakeSession.calls == [("chrome", ROOT), ("chrome", ROOT_MUSIC)]


def test_confirming_a_dead_host_does_not_arm_the_challenge_backoff():
    """A dead host is not an upstream block. Stalling the whole client for two
    minutes on every one of them is what froze the crawl in the first place."""
    FakeSession.by_url = {
        ROOT: FakeResponse(200, CHALLENGE_BODY),
        ROOT_MUSIC: FakeResponse(404),
        "https://live.bandcamp.com": FakeResponse(200, "a live page"),
    }
    client = make_client()

    with pytest.raises(NotFoundError):
        client.get(ROOT)

    assert client.get("https://live.bandcamp.com") == "a live page"


def test_a_challenged_host_root_whose_music_answers_200_still_challenges():
    """A live host being challenged is the case the backoff exists for, and it
    has to keep working. Only a real 404 means gone."""
    FakeSession.by_url = {
        ROOT: FakeResponse(200, CHALLENGE_BODY),
        ROOT_MUSIC: FakeResponse(200, "the music page"),
    }
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get(ROOT)


def test_a_challenged_host_root_whose_music_is_also_challenged_still_challenges():
    FakeSession.by_url = {
        ROOT: FakeResponse(200, CHALLENGE_BODY),
        ROOT_MUSIC: FakeResponse(200, CHALLENGE_BODY),
    }
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get(ROOT)


def test_a_challenge_on_a_url_with_a_path_is_not_confirmed():
    """Appending ``/music`` to a path invents a page nobody asked for, and the
    404 it would return says nothing about the host."""
    FakeSession.by_url = {"https://x.bandcamp.com/album/a": FakeResponse(200, CHALLENGE_BODY)}
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")

    assert FakeSession.calls == [("chrome", "https://x.bandcamp.com/album/a")]


def test_a_challenge_on_a_music_url_is_not_confirmed():
    """``/music/music`` is a genuine 404 on a perfectly live host, so probing it
    would report every challenged ``/music`` page as a deleted host."""
    FakeSession.by_url = {ROOT_MUSIC: FakeResponse(200, CHALLENGE_BODY)}
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get(ROOT_MUSIC)

    assert FakeSession.calls == [("chrome", ROOT_MUSIC)]


def test_an_unchallenged_page_costs_no_confirmation_request():
    FakeSession.by_url = {ROOT: FakeResponse(200, "a live page")}
    client = make_client()

    assert client.get(ROOT) == "a live page"
    assert FakeSession.calls == [("chrome", ROOT)]


def test_host_root_is_gone_refuses_anything_that_is_not_a_bare_root():
    client = make_client()

    assert not client.host_root_is_gone("https://x.bandcamp.com/album/a")
    assert not client.host_root_is_gone("https://x.bandcamp.com/music")
    assert not client.host_root_is_gone("https://x.bandcamp.com/?x=1")
    assert FakeSession.calls == []


def test_check_host_root_hands_back_a_live_music_page():
    """The caller whose root named nobody reads the artist from this body, so
    discarding it would cost a second request for a page already downloaded."""
    FakeSession.by_url = {ROOT_MUSIC: FakeResponse(200, "the music page")}
    client = make_client()

    check = client.check_host_root(ROOT)

    assert check == HostRootCheck(gone=False, music_html="the music page")
    assert FakeSession.calls == [("chrome", ROOT_MUSIC)]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (FakeResponse(404), HostRootCheck(gone=True)),
        (FakeResponse(200, CHALLENGE_BODY), HostRootCheck(gone=False, challenged=True)),
        (FakeResponse(503), HostRootCheck(gone=False)),
    ],
)
def test_check_host_root_hands_back_no_body_unless_it_is_a_real_page(response, expected):
    """A challenge body in particular must never reach a parser as an artist."""
    FakeSession.by_url = {ROOT_MUSIC: response}
    client = make_client()

    assert client.check_host_root(ROOT) == expected


def test_a_challenged_music_page_during_confirmation_does_not_arm_the_backoff():
    FakeSession.by_url = {ROOT_MUSIC: FakeResponse(200, CHALLENGE_BODY), ROOT: FakeResponse(200, "a page")}
    client = make_client()

    client.check_host_root(ROOT)

    assert client.get(ROOT) == "a page"


def test_host_root_is_gone_accepts_a_trailing_slash_without_doubling_it():
    FakeSession.by_url = {ROOT_MUSIC: FakeResponse(404)}
    client = make_client()

    assert client.host_root_is_gone(ROOT + "/")
    assert FakeSession.calls == [("chrome", ROOT_MUSIC)]


def test_a_transport_failure_on_the_confirmation_is_not_a_deletion():
    FakeSession.by_url = {ROOT: FakeResponse(200, CHALLENGE_BODY)}
    client = make_client()

    def explode(url, params=None, timeout=None):
        raise curl_requests.exceptions.ConnectionError("boom")

    original = client._session.get

    def routed(url, params=None, timeout=None):
        return explode(url) if url == ROOT_MUSIC else original(url, params, timeout)

    client._session.get = routed

    with pytest.raises(ChallengeError):
        client.get(ROOT)


def test_a_promoted_fingerprint_leaves_its_own_fallback_list():
    """Otherwise the new primary re-checks its own 404s: one wasted request on
    every genuine 404 forever, and ``confirmed_by`` naming the primary as if a
    second fingerprint had independently agreed."""
    FakeSession.responses = {
        "chrome": [FakeResponse(404), FakeResponse(404)],
        "firefox144": [
            FakeResponse(200, "first"),
            FakeResponse(200, "second"),
            FakeResponse(404),
        ],
    }
    client = make_client(fallback_impersonate=("firefox144",))

    client.get("https://x.bandcamp.com/album/a")
    client.get("https://x.bandcamp.com/album/b")
    assert client.impersonate == "firefox144"

    with pytest.raises(NotFoundError) as excinfo:
        client.get("https://x.bandcamp.com/album/gone")

    assert client._fallback_impersonate == ()
    assert excinfo.value.confirmed_by == ()
    assert FakeSession.calls[-1] == ("firefox144", "https://x.bandcamp.com/album/gone")


def test_a_parameterised_request_is_not_confirmed_as_a_host_root():
    """The URL is a bare root but the request is not: the confirmation would
    fetch /music without the params and answer a question nobody asked."""
    FakeSession.by_url = {ROOT: FakeResponse(200, CHALLENGE_BODY), ROOT_MUSIC: FakeResponse(404)}
    client = make_client()

    with pytest.raises(ChallengeError):
        client.get(ROOT, params={"page": 2})

    assert FakeSession.calls == [("chrome", ROOT)]


# --- challenge failover ---------------------------------------------------
#
# A bot-defence block can land on one fingerprint while others are served, and
# some pages are challenged whatever the fingerprint. The failover has to rescue
# the first case without promoting on the second, and cost one extra request per
# challenge at most.

CHALLENGE = FakeResponse(200, CHALLENGE_BODY)


def _pages(n):
    return [f"https://x.bandcamp.com/album/{i}" for i in range(n)]


def test_challenge_failover_is_off_by_default():
    FakeSession.responses = {"chrome": [CHALLENGE]}
    client = make_client()

    assert CHALLENGE_FALLBACK_IMPERSONATE == ()
    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")
    assert FakeSession.calls == [("chrome", "https://x.bandcamp.com/album/a")]


def test_a_challenge_is_rescued_by_the_fallback_fingerprint():
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [FakeResponse(200, "the real page")]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    assert client.get("https://x.bandcamp.com/album/a") == "the real page"
    assert client.impersonate == "chrome"


def test_a_rescue_does_not_arm_the_backoff():
    FakeSession.responses = {
        "chrome": [CHALLENGE, FakeResponse(200, "next page")],
        "firefox144": [FakeResponse(200, "the real page")],
    }
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    client.get("https://x.bandcamp.com/album/a")

    assert client.get("https://x.bandcamp.com/album/b") == "next page"


def test_consecutive_rescues_promote_the_fallback():
    n = CHALLENGE_PROMOTE_AFTER_RESCUES
    FakeSession.responses = {
        "chrome": [CHALLENGE] * n,
        "firefox144": [FakeResponse(200, f"page {i}") for i in range(n + 1)],
    }
    client = make_client(challenge_fallback_impersonate=("firefox144",))
    blocked_session = client._session

    for url in _pages(n):
        client.get(url)

    assert client.impersonate == "firefox144"
    assert blocked_session.closed
    assert client.get("https://x.bandcamp.com/album/next") == f"page {n}"
    assert FakeSession.calls[-1] == ("firefox144", "https://x.bandcamp.com/album/next")


def test_one_rescue_short_of_the_threshold_does_not_promote():
    n = CHALLENGE_PROMOTE_AFTER_RESCUES - 1
    FakeSession.responses = {"chrome": [CHALLENGE] * n, "firefox144": [FakeResponse(200)] * n}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    for url in _pages(n):
        client.get(url)

    assert client.impersonate == "chrome"


def test_a_page_the_primary_serves_clears_the_rescues():
    """Scattered per-page challenges on a working fingerprint must not add up
    to a promotion over a long run."""
    n = CHALLENGE_PROMOTE_AFTER_RESCUES
    FakeSession.responses = {
        "chrome": [CHALLENGE] * (n - 1) + [FakeResponse(200, "served")] + [CHALLENGE],
        "firefox144": [FakeResponse(200)] * n,
    }
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    for url in _pages(n + 1):
        client.get(url)

    assert client.impersonate == "chrome"


def test_a_page_challenged_on_every_fingerprint_raises_and_does_not_promote():
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [CHALLENGE]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    with pytest.raises(ChallengeError) as excinfo:
        client.get("https://x.bandcamp.com/album/challenged-everywhere")

    assert not isinstance(excinfo.value, ChallengeBackoffError)
    assert client.impersonate == "chrome"


def test_pages_challenged_everywhere_do_not_stop_a_blocked_primary_being_replaced():
    """Otherwise a block on the primary, mixed with pages no fingerprint gets
    through, would reset the count forever and never promote the fallback."""
    n = CHALLENGE_PROMOTE_AFTER_RESCUES
    FakeSession.responses = {
        "chrome": [CHALLENGE] * (2 * n - 1),
        "firefox144": [r for _ in range(n - 1) for r in (FakeResponse(200), CHALLENGE)] + [FakeResponse(200)],
    }
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    for url in _pages(2 * n - 1):
        client._challenge_until = 0.0
        with contextlib.suppress(ChallengeError):
            client.get(url)

    assert client.impersonate == "firefox144"


def test_only_one_fallback_is_asked_per_challenge():
    """A page challenged whatever the fingerprint is common, so each one costs
    a single extra request, not one per fallback."""
    FakeSession.responses = {
        "chrome": [CHALLENGE],
        "firefox144": [CHALLENGE],
        "safari184": [FakeResponse(200)],
    }
    client = make_client(challenge_fallback_impersonate=("firefox144", "safari184"))

    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")

    assert [name for name, _ in FakeSession.calls] == ["chrome", "firefox144"]


def test_a_challenged_fallback_hands_the_next_challenge_to_the_next_one():
    """A block covering the first fallback as well must not stall the failover."""
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [CHALLENGE]}
    client = make_client(challenge_fallback_impersonate=("firefox144", "safari184"))
    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")

    client._challenge_until = 0.0
    FakeSession.responses = {"chrome": [CHALLENGE], "safari184": [FakeResponse(200, "via safari")]}

    assert client.get("https://x.bandcamp.com/album/b") == "via safari"


def test_a_404_on_the_fallback_does_not_turn_a_challenge_into_a_deletion():
    """One fingerprint's 404 is not enough to call a page gone, and counting it
    as a rescue could promote a fingerprint that 404s everything."""
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [FakeResponse(404)]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")

    assert client._challenge_rescues == {}


def test_a_failed_fallback_leaves_the_challenge_standing():
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [FakeResponse(503)]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    with pytest.raises(ChallengeError):
        client.get("https://x.bandcamp.com/album/a")
    assert client._challenge_rescues == {}


def test_the_404_ladder_is_untouched_by_the_challenge_fallbacks():
    FakeSession.responses = {"chrome": [FakeResponse(404)]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    with pytest.raises(NotFoundError):
        client.get("https://x.bandcamp.com/album/gone")

    assert FakeSession.calls == [("chrome", "https://x.bandcamp.com/album/gone")]


def test_a_promotion_removes_the_fingerprint_from_both_lists():
    n = CHALLENGE_PROMOTE_AFTER_RESCUES
    FakeSession.responses = {"chrome": [CHALLENGE] * n, "firefox144": [FakeResponse(200)] * n}
    client = make_client(
        fallback_impersonate=("firefox144", "safari184"),
        challenge_fallback_impersonate=("firefox144", "safari184"),
    )

    for url in _pages(n):
        client.get(url)

    assert client._fallback_impersonate == ("safari184",)
    assert client._challenge_fallbacks == ("safari184",)


def test_post_json_is_rescued_too():
    FakeSession.responses = {"chrome": [CHALLENGE], "firefox144": [FakeResponse(200, '{"ok": true}')]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    assert client.post_json("https://bandcamp.com/api/x", {}) == {"ok": True}


def test_a_dead_host_is_still_confirmed_on_music_before_any_failover():
    FakeSession.by_url = {ROOT: CHALLENGE, ROOT_MUSIC: FakeResponse(404)}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    with pytest.raises(NotFoundError):
        client.get(ROOT)

    assert FakeSession.calls == [("chrome", ROOT), ("chrome", ROOT_MUSIC)]


def test_a_rescued_host_root_is_served_but_not_counted():
    """A host root that passed the /music check can still be a dead host whose
    /music could not be read, and its signup page would count as a rescue."""
    FakeSession.responses = {"chrome": [CHALLENGE, CHALLENGE], "firefox144": [FakeResponse(200, "root page")]}
    client = make_client(challenge_fallback_impersonate=("firefox144",))

    assert client.get(ROOT) == "root page"
    assert FakeSession.calls == [("chrome", ROOT), ("chrome", ROOT_MUSIC), ("firefox144", ROOT)]
    assert client._challenge_rescues == {}


# --- local backoff refusals -----------------------------------------------


def test_a_backoff_refusal_is_distinguishable_from_a_real_challenge():
    FakeSession.responses = {"chrome": [CHALLENGE]}
    client = make_client()

    with pytest.raises(ChallengeError) as first:
        client.get("https://x.bandcamp.com/album/a")
    with pytest.raises(ChallengeBackoffError) as second:
        client.get("https://x.bandcamp.com/album/b")

    assert not isinstance(first.value, ChallengeBackoffError)
    assert second.value.retry_after > 0
    assert second.value.url == "https://x.bandcamp.com/album/b"
    assert len(FakeSession.calls) == 1


def test_a_backoff_refusal_is_still_a_challenge_error():
    assert issubclass(ChallengeBackoffError, ChallengeError)


def test_a_backoff_refusal_survives_pickling():
    import pickle

    error = pickle.loads(pickle.dumps(ChallengeBackoffError("https://x.bandcamp.com", 42.0)))

    assert (error.url, error.retry_after) == ("https://x.bandcamp.com", 42.0)
    assert "42s left" in str(error)
