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


class NotFoundError(Exception):
    """Raised when a resource returns HTTP 404.

    Deliberately *not* a subclass of ``curl_requests.exceptions.RequestException``.
    The request helpers below catch that base class and return ``None``, so
    inheriting from it makes this exception swallow itself three lines after it
    is raised. Callers need a 404 to escape in order to tell a deleted entry
    from a failed fetch: the scraping pipeline flags the former as deleted and
    retries the latter, and collapsing the two means dead pages are re-fetched
    forever. Keep this outside the request-exception hierarchy.
    """


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

    def __init__(self):
        self.rate_limit_seconds = 1.0
        self.crawl_delay = 5.0
        self._last_request_time = None
        self._challenge_until = 0.0
        self._session = curl_requests.Session(impersonate="chrome")
        logger.debug("HTTP client initialized.")

    def get(self, url: str, params: dict | None = None, crawl: bool = False) -> str | None:
        """GET request, return response text or None on failure.

        Raises ``NotFoundError`` on 404 and ``ChallengeError`` on a bot-defence
        response. Every other failure is logged and returns ``None``.
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

        Used by the sibling ``bandcamp-explorer-data`` project for bulk
        image harvesting. The filename comes from a remote URL, so the
        resolved destination is checked to be inside ``output_dir``.
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
