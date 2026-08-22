"""Measure which curl_cffi fingerprints Bandcamp serves from THIS host.

Which fingerprints are blocked depends on where the requests come from as much
as on the build, and two hosts can see opposite sets. So the fallback ladder has
to be measured from the machine that will run the fetches, not from a developer
machine standing in for it.

Run it there:

    uv run --with curl-cffi python scripts/probe_fingerprints.py

Status codes alone are not enough: a blocked fingerprint answers HTTP 200 with
the bot-defence interstitial, which looks like success until you read the body.
"""

import sys

from curl_cffi import requests as curl_requests

# A page that must exist, plus a path that must not: together they separate
# "this fingerprint is blocked" from "this album is really gone".
LIVE_URL = "https://burial.bandcamp.com/album/untrue"
DEAD_URL = "https://burial.bandcamp.com/album/this-album-does-not-exist-xyz"

CANDIDATES = (
    "chrome",
    "chrome146",
    "chrome145",
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome123",
    "chrome120",
    "chrome116",
    "chrome110",
)

CHALLENGE_MARKERS = ("<title>Client Challenge</title>", "/_fs-ch-")


def classify(response) -> str:
    body = response.text[:8192]
    if any(marker in body for marker in CHALLENGE_MARKERS):
        return "CHALLENGED"
    if response.status_code == 404:
        return "404"
    if "data-tralbum" in response.text:
        return "PAGE"
    return f"other({response.status_code})"


def main() -> int:
    usable = []
    for name in CANDIDATES:
        try:
            session = curl_requests.Session(impersonate=name)
        except Exception as e:  # this curl_cffi build does not know the target
            print(f"{name:12} unavailable: {e}")
            continue
        try:
            live = classify(session.get(LIVE_URL, timeout=20))
            dead = classify(session.get(DEAD_URL, timeout=20))
        except Exception as e:
            print(f"{name:12} error: {e}")
            continue
        finally:
            session.close()

        # Usable means both answers are honest: it serves a page that exists and
        # 404s one that does not. Anything else cannot corroborate a 404.
        ok = live == "PAGE" and dead == "404"
        print(f"{name:12} live={live:12} missing={dead:12} {'USABLE' if ok else ''}")
        if ok:
            usable.append(name)

    print()
    if usable:
        print("usable fingerprints:", ", ".join(usable))
        print("suggested primary  :", usable[0])
        print("suggested fallbacks:", tuple(usable[1:4]))
    else:
        print("No fingerprint served the live page. This host looks blocked outright.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
