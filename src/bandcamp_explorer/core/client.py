"""HTTP client for Bandcamp with rate limiting."""

import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi import requests as curl_requests
from loguru import logger

REQUEST_TIMEOUT = 15
_HTTP_EXCEPTIONS = curl_requests.exceptions.RequestException

# Bandcamp fronts some endpoints with a bot-defence interstitial served as
# HTTP 200, so raise_for_status() accepts it and the parsers then see a
# well-formed page with nothing on it. Detecting it here is what stops an
# upstream block from being indistinguishable from "no results".
_CHALLENGE_MARKERS = ("<title>Client Challenge</title>", "/_fs-ch-")
_CHALLENGE_SCAN_BYTES = 8192

CHALLENGE_BACKOFF_SECONDS = 120.0

# curl_cffi's "chrome" alias floats to the newest build it ships.
DEFAULT_IMPERSONATE = "chrome"

# Opt-in escape hatch for a host that finds one fingerprint answered a 404 where
# another served the page. That has not been reproduced here, and a probe that
# counts HTTP 200 as "served" will report it wrongly, since the bot-defence
# interstitial is itself a 200. Off by default: it costs an extra request per
# fallback on every genuine 404, which a bulk crawler pays thousands of times.
FALLBACK_IMPERSONATE: tuple[str, ...] = ()

# A starting point, not a claim that any of these is blocked. Several builds of
# one browser share a failure axis, so a block on a build range can catch them
# all at once; different engines fail independently and come first. The Chrome
# entry is a recent build on purpose: older ones have been measured challenged
# on at least one host, and a challenged fallback corroborates nothing while
# still costing a request. Measure with scripts/probe_fingerprints.py.
SUGGESTED_FALLBACK_IMPERSONATE = ("firefox144", "safari184", "chrome136")

# One 404 on the primary can be transient, which is too little to hand the
# session to another fingerprint for the rest of its life.
PROMOTE_AFTER_RESCUES = 2


def _known_impersonate_targets() -> frozenset[str]:
    """Names the installed curl_cffi accepts, or an empty set if unknowable.

    The fallback list is pinned in source while curl_cffi drops old targets as
    it moves forward, so a name here can outlive the build that understands it.
    Filtering against this keeps a stale entry from turning every fallback into
    a ``ValueError`` at the exact moment the ladder is needed. An empty set
    means "could not tell", and the list is then used unfiltered.
    """
    try:
        from typing import get_args

        from curl_cffi.requests.impersonate import BrowserTypeLiteral

        return frozenset(get_args(BrowserTypeLiteral))
    except Exception:
        return frozenset()


class NotFoundError(Exception):
    """Raised when a resource returns HTTP 404.

    Deliberately *not* a subclass of ``curl_requests.exceptions.RequestException``.
    The request helpers below catch that base class and return ``None``, so
    inheriting from it makes this exception swallow itself three lines after it
    is raised. Callers need a 404 to escape in order to tell a deleted entry
    from a failed fetch: a crawler flags the former and retries the latter, and
    collapsing the two means dead pages are re-fetched forever. Keep this
    outside the request-exception hierarchy.

    Since 0.8.0 this can also escape :meth:`BandcampClient.get` where a
    ``ChallengeError`` used to: the root page of a host that no longer exists
    never answers 404, so a bare host root that is challenged is re-checked on
    its ``/music`` subpage before the challenge is believed. See
    :meth:`BandcampClient.host_root_is_gone`.

    ``confirmed_by`` names the fallback fingerprints that independently saw the
    same 404. It means "extra corroboration was obtained", never "this page is
    really gone" nor its negation. It is empty whenever no fallback answered,
    which with the re-check ladder off is always, so gating deletion on a
    non-empty value would stop every deletion from ever being recorded. Only
    callers who enabled the ladder can read anything into it: for them an empty
    tuple means the re-check could not be carried out, which is a reason to
    retry rather than to flag the row.
    """

    def __init__(self, url: str, confirmed_by: tuple[str, ...] = ()):
        super().__init__(url)
        self.url = url
        self.confirmed_by = confirmed_by


class ChallengeError(Exception):
    """Raised when Bandcamp answers with a bot-defence interstitial.

    Kept separate from :class:`NotFoundError` on purpose: a challenge means
    "ask again later", never "this resource is gone". Callers that flag
    deleted entries must not treat it as a 404.
    """


def _is_challenge(text: str) -> bool:
    """Detect Bandcamp's bot-defence interstitial in a response body."""
    head = text[:_CHALLENGE_SCAN_BYTES]
    return any(marker in head for marker in _CHALLENGE_MARKERS)


def _is_bare_host_root(url: str) -> bool:
    """True for ``https://host/`` with nothing after it.

    The ``/music`` re-check only means anything on a host root: appending it to
    a URL that already carries a path invents a page nobody asked for, and
    appending it to a ``/music`` URL yields ``/music/music``, a genuine 404 that
    would then be read as a dead host.
    """
    parts = urlsplit(url)
    return (
        parts.scheme in ("http", "https")
        and bool(parts.netloc)
        and parts.path in ("", "/")
        and not parts.query
        and not parts.fragment
    )


class BandcampClient:
    """HTTP client for Bandcamp with rate limiting.

    Rate limits:
        - Normal requests: 1.0s between calls (interactive use).
        - Crawl requests: 5.0s between calls (bulk discovery/scraping).

    Once a bot-defence challenge is seen, further requests fail fast for
    ``CHALLENGE_BACKOFF_SECONDS`` rather than hammering a blocked endpoint.
    Failing fast (instead of sleeping) keeps the backoff usable from a
    Discord command handler, which cannot block for two minutes.
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        fallback_impersonate: tuple[str, ...] = FALLBACK_IMPERSONATE,
    ):
        """Build a client on a chosen TLS fingerprint.

        Args:
            impersonate: curl_cffi impersonate target for the session. Defaults
                to the floating ``"chrome"`` alias.
            fallback_impersonate: fingerprints to re-check a 404 against, tried
                in order. Pass ``()`` to disable the ladder and let every 404
                raise immediately.
        """
        self.rate_limit_seconds = 1.0
        self.crawl_delay = 5.0
        self._last_request_time = None
        self._challenge_until = 0.0
        self.impersonate = impersonate
        self._fallback_impersonate = self._usable_fallbacks(impersonate, fallback_impersonate)
        self._rescues: dict[str, int] = {}
        self._session = curl_requests.Session(impersonate=impersonate)
        logger.debug(f"HTTP client initialized on {impersonate}.")

    @staticmethod
    def _usable_fallbacks(impersonate: str, candidates: tuple[str, ...]) -> tuple[str, ...]:
        """Drop the primary fingerprint and anything curl_cffi cannot build."""
        known = _known_impersonate_targets()
        usable = []
        for name in candidates:
            if name == impersonate or name in usable:
                continue
            if known and name not in known:
                logger.debug(f"Skipping fallback fingerprint {name}: unknown to curl_cffi.")
                continue
            usable.append(name)
        return tuple(usable)

    def get(self, url: str, params: dict | None = None, crawl: bool = False) -> str | None:
        """GET request, return response text or None on failure.

        When fallback fingerprints are configured, a 404 is re-checked against
        them before it is believed. If one of them serves the page, that
        fingerprint takes over the session for good and its body is returned.
        The ladder is off unless a caller turns it on; see
        ``FALLBACK_IMPERSONATE``.

        Raises ``NotFoundError`` on a 404 no fingerprint could contradict, and
        ``ChallengeError`` on a bot-defence response. Every other failure is
        logged and returns ``None``.

        One challenge does not raise: a challenged *host root* is re-checked on
        ``/music`` first, and a 404 there raises ``NotFoundError`` instead,
        because a dead host's root cannot answer 404 itself. See
        :meth:`host_root_is_gone`.
        """
        self._guard_challenge_backoff(url)
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                logger.warning(f"Not found (404): {url}")
                raise NotFoundError(url)
            response.raise_for_status()
            text = response.text
        except NotFoundError:
            rescued, confirmed_by = self._retry_on_fallbacks(url, params, crawl)
            if rescued is None:
                raise NotFoundError(url, confirmed_by)
            text = rescued
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"GET failed for {url}: {e}")
            return None
        # Outside the except block: a challenge must not be downgraded to None.
        # A parameterised request is not a bare host root even when its URL is:
        # the confirmation would fetch /music without those params and answer a
        # question nobody asked.
        confirmable = not params
        if confirmable and _is_challenge(text) and self.host_root_is_gone(url, crawl=crawl):
            logger.warning(f"Challenged host root {url} is gone: /music answered 404.")
            raise NotFoundError(url)
        self._check_challenge(text, url)
        return text

    def post_json(self, url: str, payload: dict, crawl: bool = False) -> dict | None:
        """POST with JSON body, return parsed JSON response or None on failure.

        Raises ``ChallengeError`` on a bot-defence response.
        """
        self._guard_challenge_backoff(url)
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            text = response.text
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"POST failed for {url}: {e}")
            return None
        self._check_challenge(text, url)
        try:
            return json.loads(text)
        except ValueError as e:
            logger.error(f"POST returned a non-JSON body for {url}: {e}")
            return None

    def get_bytes(self, url: str, crawl: bool = False) -> bytes | None:
        """GET request, return raw bytes or None on failure."""
        self._guard_challenge_backoff(url)
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.content
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"GET bytes failed for {url}: {e}")
            return None

    def download_image(self, url: str, output_dir: str = "./images/") -> str | None:
        """Download an image to a local file, return saved path or None.

        The filename comes from a remote URL, so the resolved destination is
        checked to be inside ``output_dir``.
        """
        if not url:
            return None

        try:
            # Path(...).name drops any directory component the remote URL
            # smuggled in, so the write cannot escape output_dir.
            filename = Path(url.rsplit("/", 1)[-1].split("?")[0]).name
            if not filename or filename in (".", ".."):
                logger.warning(f"Refusing image with no usable filename: {url}")
                return None

            output_root = Path(output_dir).resolve()
            output_path = (output_root / filename).resolve()
            if not output_path.is_relative_to(output_root):
                logger.error(f"Refusing image resolving outside {output_root}: {url}")
                return None

            output_root.mkdir(parents=True, exist_ok=True)

            image_data = self.get_bytes(url, crawl=True)
            if not image_data:
                return None

            output_path.write_bytes(image_data)
            logger.debug(f"Downloaded {filename} -> {output_path}")
            return str(output_path)

        except OSError as e:
            logger.debug(f"Failed to save {url}: {e}")
            return None

    def host_root_is_gone(self, url: str, crawl: bool = False) -> bool:
        """Ask ``url + "/music"`` whether a host root belongs to a dead host.

        The root of a subdomain that no longer exists never answers 404: it
        answers 200 with either the bot-defence interstitial or Bandcamp's
        signup page, depending on the requesting address. ``/music`` on the same
        host does answer a plain 404, and that is the only signal available.
        True on that 404 and nothing else, since neither a 200 nor a challenge
        nor a transport error is evidence of deletion.

        Bypasses the challenge backoff deliberately, and must: the backoff arms
        the moment an interstitial is seen, after which every URL is refused
        locally for two minutes, so a confirmation issued from a caller could
        never reach the network. That is why this lives on the client.

        Costs one request per dead host, once, and only for a bare root. Not the
        dropped fingerprint ladder, which spent one on *every* genuine 404
        forever. Known gap: the root is unparsed here, so a live host serving no
        ``/music`` page would read as deleted while it was only challenged.
        Never observed, and dropping this leaves dead hosts undetectable from
        any challenged address, which is the form they take there.
        """
        if not _is_bare_host_root(url):
            return False
        music_url = url.rstrip("/") + "/music"
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(music_url, timeout=REQUEST_TIMEOUT)
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"Host confirmation GET failed for {music_url}: {e}")
            return False
        if response.status_code == 404:
            return True
        logger.debug(f"Host confirmation for {url}: /music answered {response.status_code}, not gone.")
        return False

    def _retry_on_fallbacks(
        self, url: str, params: dict | None, crawl: bool
    ) -> tuple[str | None, tuple[str, ...]]:
        """Re-fetch a 404 on each fallback fingerprint, newest first.

        Returns ``(body, ())`` as soon as one of them serves the page, having
        first promoted that fingerprint onto the session. Otherwise returns
        ``(None, confirmed_by)``, where ``confirmed_by`` names the fingerprints
        that saw the same 404. An empty tuple there means nobody could check,
        not that the page is more certainly gone: a transport error or a
        challenge proves nothing, so the original 404 is left standing rather
        than downgraded to a retry. This only ever refuses to believe a 404, it
        never invents one.
        """
        confirmed_by: list[str] = []
        for name in self._fallback_impersonate:
            self._wait_between_requests(crawl=crawl)
            session = curl_requests.Session(impersonate=name)
            promoted = False
            try:
                try:
                    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                    if response.status_code == 404:
                        confirmed_by.append(name)
                        continue
                    response.raise_for_status()
                    text = response.text
                except _HTTP_EXCEPTIONS as e:
                    logger.error(f"Fallback GET on {name} failed for {url}: {e}")
                    continue
                # A throwaway fallback session being challenged is not evidence
                # about the primary, so it must not arm the shared backoff: that
                # would turn every genuine 404 into a two-minute stall for the
                # whole client. Skip this fingerprint, keep asking the others.
                if _is_challenge(text):
                    logger.warning(f"Fallback {name} was challenged for {url}; trying the next one.")
                    continue
                self._rescues[name] = self._rescues.get(name, 0) + 1
                if self._rescues[name] < PROMOTE_AFTER_RESCUES:
                    logger.warning(
                        f"Fallback {name} served {url} where {self.impersonate} returned 404. "
                        f"Not switching yet: one 404 can be transient, waiting for a second."
                    )
                    return text, ()
                self._promote(name, session, url)
                promoted = True
                return text, ()
            finally:
                if not promoted:
                    session.close()
        if confirmed_by:
            logger.info(f"404 on {url} confirmed by {', '.join(confirmed_by)}.")
        elif self._fallback_impersonate:
            logger.warning(
                f"404 on {url} could not be checked: every fallback fingerprint was "
                f"challenged or failed. Treating it as gone, but nothing corroborates it."
            )
        return None, tuple(confirmed_by)

    def _promote(self, name: str, session, url: str) -> None:
        """Adopt a fallback fingerprint for the rest of this client's life.

        Called once a fingerprint has rescued ``PROMOTE_AFTER_RESCUES`` separate
        pages, which is what separates a soft-blocked primary from a one-off
        404. Keeping the working one means the extra requests are paid once
        instead of on every later 404, and ``post_json`` (search, discover)
        rides the unblocked session too.
        """
        logger.warning(
            f"Fingerprint {self.impersonate} looks soft-blocked: {name} has now served "
            f"{PROMOTE_AFTER_RESCUES} pages it 404'd, most recently {url}. "
            f"Switching this client to {name}."
        )
        self._session.close()
        self._session = session
        self.impersonate = name
        # Otherwise the new primary stays in its own fallback list: every later
        # 404 pays a second request to the same fingerprint, and the answer is
        # reported in ``confirmed_by`` as if another fingerprint had agreed.
        self._fallback_impersonate = tuple(n for n in self._fallback_impersonate if n != name)

    def _guard_challenge_backoff(self, url: str) -> None:
        """Fail fast while a previously seen challenge is still cooling down."""
        if not self._challenge_until:
            return
        remaining = self._challenge_until - time.monotonic()
        if remaining <= 0:
            self._challenge_until = 0.0
            return
        raise ChallengeError(f"Challenge backoff active, {remaining:.0f}s left (blocked: {url})")

    def _check_challenge(self, text: str, url: str) -> None:
        """Raise and start backing off when a response is the bot-defence page."""
        if not _is_challenge(text):
            return
        self._challenge_until = time.monotonic() + CHALLENGE_BACKOFF_SECONDS
        logger.error(
            f"Bandcamp served a bot-defence challenge for {url}; "
            f"backing off {CHALLENGE_BACKOFF_SECONDS:.0f}s."
        )
        raise ChallengeError(url)

    def _wait_between_requests(self, crawl: bool = False):
        """Enforce delay between requests."""
        delay = self.crawl_delay if crawl else self.rate_limit_seconds
        if self._last_request_time is not None:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request_time = time.monotonic()

    def close(self):
        """Close the underlying HTTP session."""
        self._session.close()
        logger.debug("HTTP client closed.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
