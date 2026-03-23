"""HTTP client for Bandcamp with rate limiting."""

import time

import requests
from loguru import logger

REQUEST_TIMEOUT = 15
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0",
}


class BandcampClient:
    """HTTP client for Bandcamp with rate limiting.

    Rate limits:
        - Normal requests: 0.5s between calls (interactive use).
        - Crawl requests: 5.0s between calls (bulk discovery/scraping).
    """

    def __init__(self):
        self.rate_limit_seconds = 0.5
        self.crawl_delay = 5.0
        self._last_request_time = None
        self._session = requests.Session()
        self._session.headers.update(DEFAULT_HEADERS)
        logger.debug("HTTP client initialized.")

    def get(
        self, url: str, params: dict | None = None, crawl: bool = False
    ) -> str | None:
        """GET request, return response text.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters.
            crawl: If True, use longer crawl delay.

        Returns:
            Response body as string, or None on failure.
        """
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.text
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                logger.warning(f"Not found (404): {url}")
                raise
            logger.error(f"GET failed for {url}: {e}")
            return None
        except Exception as e:
            logger.error(f"GET failed for {url}: {e}")
            return None

    def post_json(self, url: str, payload: dict, crawl: bool = False) -> dict | None:
        """POST with JSON body, return parsed JSON response.

        Args:
            url: Full URL to post to.
            payload: JSON-serializable dict for the request body.
            crawl: If True, use longer crawl delay.

        Returns:
            Parsed JSON as a dict, or None on failure.
        """
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"POST failed for {url}: {e}")
            return None

    def get_bytes(self, url: str, crawl: bool = False) -> bytes | None:
        """GET request, return raw bytes (for images).

        Args:
            url: Full URL to fetch.
            crawl: If True, use longer crawl delay.

        Returns:
            Raw bytes, or None on failure.
        """
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"GET bytes failed for {url}: {e}")
            return None

    def _wait_between_requests(self, crawl: bool = False):
        """Enforce delay between requests."""
        delay = self.crawl_delay if crawl else self.rate_limit_seconds
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request_time = time.time()

    def close(self):
        """Close the underlying HTTP session."""
        self._session.close()
        logger.debug("HTTP client closed.")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
