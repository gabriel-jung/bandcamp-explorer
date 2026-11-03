"""HTTP client for Bandcamp with rate limiting."""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
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

# curl_cffi's "chrome" alias floats to the newest build it ships. It follows
# curl_cffi releases, not Bandcamp's blocks, so it is no protection against a
# block on its current build: ``challenge_fallback_impersonate`` is for that.
DEFAULT_IMPERSONATE = "chrome"

# Opt-in escape hatch for a host that finds one fingerprint answered a 404 where
# another served the page. That has not been reproduced here, and a probe that
# counts HTTP 200 as "served" will report it wrongly, since the bot-defence
# interstitial is itself a 200. Off by default: it costs an extra request per
# fallback on every genuine 404, and every 404 is genuine until shown otherwise.
FALLBACK_IMPERSONATE: tuple[str, ...] = ()

# Opt-in failover for a fingerprint Bandcamp's bot defence has started to
# challenge. Off by default: with it on, every challenged page costs one extra
# request, whether or not the fingerprint had anything to do with it.
CHALLENGE_FALLBACK_IMPERSONATE: tuple[str, ...] = ()

# A starting point for either list, not a claim that any of these is blocked.
# Several builds of one browser share a failure axis, so a block on a build
# range can catch them all at once; other engines are off that axis and come
# first. The Chrome
# entry is a recent build on purpose: older ones have been measured challenged
# on at least one host, and a challenged fallback corroborates nothing while
# still costing a request. Measure with scripts/probe_fingerprints.py.
SUGGESTED_FALLBACK_IMPERSONATE = ("firefox144", "safari184", "chrome136")

# One 404 on the primary can be transient, which is too little to hand the
# session to another fingerprint for the rest of its life.
PROMOTE_AFTER_RESCUES = 2

# A page can be challenged without the fingerprint being blocked, and a fallback
# that gets through one says little about the primary. Several rescues by the
# same fallback, with no page served by the primary in between, are what a
# blocked fingerprint looks like.
CHALLENGE_PROMOTE_AFTER_RESCUES = 3


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
    is raised. A 404 has to escape so callers can tell a deleted entry from a
    failed fetch, which are worth acting on differently. Keep this outside the
    request-exception hierarchy.

    Since 0.8.0 this can also escape :meth:`BandcampClient.get` where a
    ``ChallengeError`` used to: the root page of a host that no longer exists
    never answers 404, so a bare host root that is challenged is re-checked on
    its ``/music`` subpage before the challenge is believed. See
    :meth:`BandcampClient.check_host_root`.

    ``confirmed_by`` names the fallback fingerprints that independently saw the
    same 404. It means "extra corroboration was obtained", never "this page is
    really gone" nor its negation. It is empty whenever no fallback answered,
    which with the re-check ladder off is always, so gating deletion on a
    non-empty value would stop every deletion from ever being recorded. Only
    callers who enabled the ladder can read anything into it: for them an empty
    tuple means the re-check could not be carried out, which is a reason to
    retry rather than to conclude anything.
    """

    def __init__(self, url: str, confirmed_by: tuple[str, ...] = ()):
        super().__init__(url)
        self.url = url
        self.confirmed_by = confirmed_by


class FetchError(Exception):
    """A search, discover or location request failed: retry later.

    Raised by :meth:`BandcampClient.post_json` for a network error, an HTTP
    error status or a body that is not JSON, so those endpoints never report a
    failure as "no results". Page fetches (:meth:`BandcampClient.get`) keep
    returning None for the same failures. ``url`` names the request.
    Never means the resource is gone; that is :class:`NotFoundError`.
    """

    url: str | None = None

    def __init__(self, *args, url: str | None = None):
        super().__init__(*args)
        if url is not None:
            self.url = url  # kept in __dict__, so it survives pickling


class ChallengeError(FetchError):
    """Raised when Bandcamp answers with a bot-defence interstitial.

    Kept separate from :class:`NotFoundError` on purpose: a challenge means
    "ask again later", never "this resource is gone". Callers that flag
    deleted entries must not treat it as a 404.

    Raised directly only when Bandcamp itself served the interstitial. A request
    refused locally during the backoff that follows raises the subclass
    :class:`ChallengeBackoffError` instead. A subclass of :class:`FetchError`,
    so ``except FetchError`` covers it; catch it first to back off longer.
    """

    def __init__(self, *args, url: str | None = None):
        # Raised as ChallengeError(url); the URL is also the message.
        super().__init__(*args, url=url if url is not None else (args[0] if args else None))


class ChallengeBackoffError(ChallengeError):
    """Raised without touching the network while a challenge backoff is active.

    A subclass so ``except ChallengeError`` still catches it. Catch it first to
    tell "the client declined to ask" from "Bandcamp challenged this request";
    ``retry_after`` is the number of seconds the backoff has left.
    """

    def __init__(self, url: str, retry_after: float):
        # Both arguments go to ``args`` so the exception survives pickling,
        # which rebuilds it as ``cls(*args)``.
        super().__init__(url, retry_after)
        self.url = url
        self.retry_after = retry_after

    def __str__(self) -> str:
        return f"Challenge backoff active, {self.retry_after:.0f}s left (blocked: {self.url})"


@dataclass(frozen=True)
class HostRootCheck:
    """What ``/music`` said about a host root, from :meth:`BandcampClient.check_host_root`.

    ``gone`` is true on a 404 and nothing else. ``music_html`` is the ``/music``
    body when it answered 2xx with a real page, so a caller whose root named
    nobody can read the artist from it without a second request. It is ``None``
    on a 404, a transport error, any other status, or the bot-defence
    interstitial, which must never be parsed as an artist. ``challenged`` says
    the last one happened, which means "ask again later", not "names nobody".
    """

    gone: bool
    music_html: str | None = None
    challenged: bool = False


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
        - Crawl requests: 5.0s between calls (multi-page walks, image bytes).

    Once a bot-defence challenge is seen, further requests fail fast for
    ``CHALLENGE_BACKOFF_SECONDS`` rather than hammering a blocked endpoint.
    Failing fast (instead of sleeping) keeps the backoff usable from a
    Discord command handler, which cannot block for two minutes.

    ``impersonate`` names the fingerprint in use. It changes when a fallback is
    promoted, so read it at the time of logging rather than once at startup.
    """

    def __init__(
        self,
        impersonate: str = DEFAULT_IMPERSONATE,
        fallback_impersonate: tuple[str, ...] = FALLBACK_IMPERSONATE,
        challenge_fallback_impersonate: tuple[str, ...] = CHALLENGE_FALLBACK_IMPERSONATE,
    ):
        """Build a client on a chosen TLS fingerprint.

        Args:
            impersonate: curl_cffi impersonate target for the session. Defaults
                to the floating ``"chrome"`` alias.
            fallback_impersonate: fingerprints to re-check a 404 against, tried
                in order. Pass ``()`` to disable the ladder and let every 404
                raise immediately.
            challenge_fallback_impersonate: fingerprints to re-send a challenged
                request on, one per challenge, moving down the list each time
                the current one is challenged too. One that keeps serving what
                the primary is challenged on takes over the session. Only a
                served page counts: a 404 or an error from a fallback is never
                returned in place of the challenge. See
                ``SUGGESTED_FALLBACK_IMPERSONATE``. Off by default.
        """
        self.rate_limit_seconds = 1.0
        self.crawl_delay = 5.0
        self._last_request_time = None
        self._challenge_until = 0.0
        self.impersonate = impersonate
        self._fallback_impersonate = self._usable_fallbacks(impersonate, fallback_impersonate)
        self._rescues: dict[str, int] = {}
        self._challenge_fallbacks = self._usable_fallbacks(impersonate, challenge_fallback_impersonate)
        self._challenge_cursor = 0
        self._challenge_rescues: dict[str, int] = {}
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
        :meth:`check_host_root`. After that, with challenge fallbacks
        configured, the request is re-sent once on the next of them, and its
        page is used when it served one. ``ChallengeBackoffError``
        means the request never left the client.
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
            # The ladder skips challenged fallbacks, so a rescued body is never
            # the interstitial and needs none of the handling below.
            rescued, confirmed_by = self._retry_on_fallbacks(url, params, crawl)
            if rescued is None:
                raise NotFoundError(url, confirmed_by)
            return rescued
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"GET failed for {url}: {e}")
            return None
        # Outside the except block: a challenge must not be downgraded to None.
        if not _is_challenge(text):
            self._challenge_rescues.clear()
            return text
        # A parameterised request is not a bare host root even when its URL is:
        # the confirmation would fetch /music without those params and answer a
        # question nobody asked.
        confirmable = not params
        if confirmable and self.check_host_root(url, crawl=crawl).gone:
            logger.warning(f"Challenged host root {url} is gone: /music answered 404.")
            raise NotFoundError(url)
        # A host root that survived the check above can still be a dead host
        # whose /music could not be read, and its signup page would then count
        # as a rescue. Serve it, but keep it out of the promotion count.
        rescued = self._challenge_failover(
            url,
            lambda session: session.get(url, params=params, timeout=REQUEST_TIMEOUT),
            crawl,
            counts=not (confirmable and _is_bare_host_root(url)),
        )
        if rescued is None:
            self._raise_challenge(url)
        return rescued

    def post_json(self, url: str, payload: dict, crawl: bool = False) -> dict:
        """POST with JSON body and return the parsed JSON response.

        Raises:
            ChallengeError: On a bot-defence response that the challenge
                fallbacks, when configured, could not get past either.
            FetchError: On a network error, an HTTP error status, or a body
                that is not JSON.
        """
        self._guard_challenge_backoff(url)
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            text = response.text
        except _HTTP_EXCEPTIONS as e:
            raise FetchError(f"POST failed: {e}", url=url) from e
        if not _is_challenge(text):
            self._challenge_rescues.clear()
        else:
            text = self._challenge_failover(
                url, lambda session: session.post(url, json=payload, timeout=REQUEST_TIMEOUT), crawl
            )
            if text is None:
                self._raise_challenge(url)
        try:
            data = json.loads(text)
        except ValueError as e:
            raise FetchError(f"POST returned a non-JSON body: {e}", url=url) from e
        if not isinstance(data, dict):
            raise FetchError(f"POST returned JSON that is not an object ({type(data).__name__})", url=url)
        return data

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
        """True when ``/music`` confirms a host root belongs to a dead host.

        Kept for existing callers: shorthand for ``check_host_root(url,
        crawl).gone``, which also hands back the ``/music`` body this discards.
        See :meth:`check_host_root` for what it costs and what counts as gone.
        """
        return self.check_host_root(url, crawl=crawl).gone

    def check_host_root(self, url: str, crawl: bool = False) -> HostRootCheck:
        """Ask ``url + "/music"`` whether a host root belongs to a dead host.

        The root of a subdomain that no longer exists never answers 404: it
        answers 200 with either the bot-defence interstitial or Bandcamp's
        signup page, depending on the requesting address. ``/music`` on the same
        host does answer a plain 404, and that is the only signal available.
        ``gone`` is true on that 404 and nothing else, since neither a 200 nor a
        challenge nor a transport error is evidence of deletion.

        A live host can also have a root that names nobody, for instance one
        set to its subscription page, while its ``/music`` page is a complete
        artist page. The 2xx body is therefore returned as ``music_html`` rather
        than discarded, unless it is the interstitial, which sets ``challenged``.

        Bypasses the challenge backoff deliberately, and must: the backoff arms
        the moment an interstitial is seen, after which every URL is refused
        locally for two minutes, so a confirmation issued from a caller could
        never reach the network. That is why this lives on the client. For the
        same reason a challenged ``/music`` does not arm the backoff here: the
        caller decides what the challenge means once it knows the host is live.

        Costs one request per dead host, once, and only for a bare root. Not the
        dropped fingerprint ladder, which spent one on *every* genuine 404
        forever. Known gap: :meth:`get` confirms a challenged root it never
        parsed, so there a live host serving no ``/music`` page would read as
        deleted while it was only challenged. Never observed, and dropping this
        leaves dead hosts undetectable from any challenged address, which is the
        form they take there.
        """
        if not _is_bare_host_root(url):
            return HostRootCheck(gone=False)
        music_url = url.rstrip("/") + "/music"
        self._wait_between_requests(crawl=crawl)
        try:
            response = self._session.get(music_url, timeout=REQUEST_TIMEOUT)
        except _HTTP_EXCEPTIONS as e:
            logger.error(f"Host confirmation GET failed for {music_url}: {e}")
            return HostRootCheck(gone=False)
        if response.status_code == 404:
            return HostRootCheck(gone=True)
        logger.debug(f"Host confirmation for {url}: /music answered {response.status_code}, not gone.")
        if not 200 <= response.status_code < 300:
            return HostRootCheck(gone=False)
        if _is_challenge(response.text):
            return HostRootCheck(gone=False, challenged=True)
        return HostRootCheck(gone=False, music_html=response.text)

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
        """Adopt a fallback fingerprint after it rescued repeated 404s.

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
        self._adopt(name, session)

    def _challenge_failover(self, url: str, send, crawl: bool, counts: bool = True) -> str | None:
        """Re-send a challenged request once, on the next challenge fallback.

        ``send`` performs the request on a session it is given. Returns the
        body when the fallback served the page, or ``None`` when there is no
        fallback or it did not serve it, and the caller then raises the
        challenge as it would have. A 404 or an error status from a fallback is
        never returned: turning a challenge into a ``NotFoundError`` on one
        fingerprint's word would make this invent deletions.

        Only one fallback is asked per challenge, so a page that would be
        challenged on any fingerprint costs one extra request, not one per
        fallback. A fallback challenged too moves the cursor to the next one,
        so a block covering the first fallback as well is stepped past on the
        next challenge. Rescues are counted per fallback and survive those
        pages; a page the primary serves clears them. The first fallback to
        reach ``CHALLENGE_PROMOTE_AFTER_RESCUES`` is promoted. ``counts=False``
        serves the page without counting the rescue.
        """
        if not self._challenge_fallbacks:
            return None
        name = self._challenge_fallbacks[self._challenge_cursor]
        self._wait_between_requests(crawl=crawl)
        session = curl_requests.Session(impersonate=name)
        promoted = False
        try:
            try:
                response = send(session)
                response.raise_for_status()
                text = response.text
            except _HTTP_EXCEPTIONS as e:
                logger.error(f"Challenge failover on {name} failed for {url}: {e}")
                return None
            if _is_challenge(text):
                logger.info(f"{url} is challenged on {self.impersonate} and on {name} alike.")
                self._challenge_cursor = (self._challenge_cursor + 1) % len(self._challenge_fallbacks)
                return None
            if not counts:
                return text
            rescues = self._challenge_rescues.get(name, 0) + 1
            self._challenge_rescues[name] = rescues
            if rescues < CHALLENGE_PROMOTE_AFTER_RESCUES:
                logger.info(f"{name} got past a challenge on {self.impersonate} for {url}.")
                return text
            logger.warning(
                f"Fingerprint {self.impersonate} looks blocked: {name} got past its challenge on "
                f"{rescues} pages since the last one it served, most recently {url}. "
                f"Switching this client to {name}."
            )
            self._adopt(name, session)
            promoted = True
            return text
        finally:
            if not promoted:
                session.close()

    def _adopt(self, name: str, session) -> None:
        """Make a fallback fingerprint the session's for the rest of this client's life."""
        self._session.close()
        self._session = session
        self.impersonate = name
        # Otherwise the new primary stays in its own fallback lists: every later
        # 404 pays a second request to the same fingerprint, and the answer is
        # reported in ``confirmed_by`` as if another fingerprint had agreed.
        self._fallback_impersonate = tuple(n for n in self._fallback_impersonate if n != name)
        self._challenge_fallbacks = tuple(n for n in self._challenge_fallbacks if n != name)
        self._challenge_cursor = 0
        self._challenge_rescues.clear()

    def _guard_challenge_backoff(self, url: str) -> None:
        """Fail fast while a previously seen challenge is still cooling down."""
        if not self._challenge_until:
            return
        remaining = self._challenge_until - time.monotonic()
        if remaining <= 0:
            self._challenge_until = 0.0
            return
        raise ChallengeBackoffError(url, remaining)

    def _raise_challenge(self, url: str) -> NoReturn:
        """Start backing off and raise for a request Bandcamp challenged."""
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
