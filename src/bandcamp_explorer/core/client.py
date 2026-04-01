"""HTTP client for Bandcamp with rate limiting."""

import time
from pathlib import Path

from curl_cffi import requests as curl_requests
from loguru import logger

REQUEST_TIMEOUT = 15


class NotFoundError(Exception):
    """Raised when a resource returns HTTP 404."""


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
        self._session = curl_requests.Session(impersonate="chrome")
        logger.debug("HTTP client initialized.")

    def get(self, url: str, params: dict | None = None, crawl: bool = False) -> str | None:
        """GET request, return response text or None on failure."""
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                logger.warning(f"Not found (404): {url}")
                raise NotFoundError(url)
            response.raise_for_status()
            return response.text
        except NotFoundError:
            raise
        except Exception as e:
            logger.error(f"GET failed for {url}: {e}")
            return None

    def post_json(self, url: str, payload: dict, crawl: bool = False) -> dict | None:
        """POST with JSON body, return parsed JSON response or None on failure."""
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"POST failed for {url}: {e}")
            return None

    def get_bytes(self, url: str, crawl: bool = False) -> bytes | None:
        """GET request, return raw bytes or None on failure."""
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"GET bytes failed for {url}: {e}")
            return None

    def download_image(self, url: str, output_dir: str = "./images/") -> str | None:
        """Download an image to a local file, return saved path or None."""
        if not url:
            return None

        try:
            filename = url.rsplit("/", 1)[-1].split("?")[0]
            output_path = Path(output_dir) / filename
            output_path.parent.mkdir(parents=True, exist_ok=True)

            image_data = self.get_bytes(url, crawl=True)
            if not image_data:
                return None

            output_path.write_bytes(image_data)
            logger.debug(f"Downloaded {filename} -> {output_path}")
            return str(output_path)

        except Exception as e:
            logger.debug(f"Failed to download {url}: {e}")
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
