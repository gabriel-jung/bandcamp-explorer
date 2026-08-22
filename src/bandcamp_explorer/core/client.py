"""HTTP client for Bandcamp with rate limiting."""

import json
import time
from pathlib import Path

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

# Opt-in: Bandcamp can serve one TLS fingerprint a 404 on a page another one
# fetches fine, and the ladder re-checks a 404 before believing it. Off by
# default because it costs an extra request per fallback on every genuine 404.
FALLBACK_IMPERSONATE: tuple[str, ...] = ()

# A starting point, not a claim that any of these is blocked. Several builds of
# one browser share a failure axis, so a block on a build range can catch them
# all at once; different engines fail independently and come first.
SUGGESTED_FALLBACK_IMPERSONATE = ("firefox144", "safari184", "chrome131")

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

        A 404 is re-checked against the fallback fingerprints before it is
        believed, because Bandcamp soft-blocks some of them with a 404. If one
        of them serves the page, that fingerprint takes over the session for
        good and its body is returned.

        Raises ``NotFoundError`` on a 404 no fingerprint could contradict, and
        ``ChallengeError`` on a bot-defence response. Every other failure is
        logged and returns ``None``.
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
