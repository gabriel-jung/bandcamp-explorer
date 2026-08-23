"""Measure which curl_cffi fingerprints Bandcamp serves from THIS host.

Which fingerprints are blocked depends on where the requests come from as much
as on the build, and two hosts can see opposite sets. So the fallback ladder has
to be measured from the machine that will run the fetches, not from a developer
machine standing in for it.

Run it there:

    uv run --with curl-cffi python scripts/probe_fingerprints.py [live-album-url]

Status codes alone are not enough: a blocked fingerprint answers HTTP 200 with
the bot-defence interstitial, which looks like success until you read the body.
"""

import sys
import time

from curl_cffi import requests as curl_requests

# A page that must exist, plus a path that must not: together they separate
# "this fingerprint is blocked" from "this album is really gone". Pass another
# album URL as the first argument when this one stops being a safe control: if
# the default release is ever taken down, every candidate 404s and the run
# reports a blocked host, which is the one conclusion the script exists to
# prevent. The 404 classification below catches that inversion.
DEFAULT_LIVE_URL = "https://burial.bandcamp.com/album/untrue"

# The defence being measured reacts to request rate, so candidates fired
# back-to-back get classified by the burst rather than by the fingerprint.
DELAY_BETWEEN_REQUESTS = 3.0

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
    live_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIVE_URL
    dead_url = live_url.rstrip("/") + "-does-not-exist-xyz"
    print(f"live control: {live_url}")
    print(f"404 control : {dead_url}\n")

    usable = []
    verdicts = {}
    first = True
    for name in CANDIDATES:
        if not first:
            time.sleep(DELAY_BETWEEN_REQUESTS)
        first = False
        try:
            session = curl_requests.Session(impersonate=name)
        except Exception as e:  # this curl_cffi build does not know the target
            print(f"{name:12} unavailable: {e}")
            continue
        try:
            live = classify(session.get(live_url, timeout=20))
            time.sleep(DELAY_BETWEEN_REQUESTS)
            dead = classify(session.get(dead_url, timeout=20))
        except Exception as e:
            print(f"{name:12} error: {e}")
            continue
        finally:
            session.close()

        # Usable means both answers are honest: it serves a page that exists and
        # 404s one that does not. Anything else cannot corroborate a 404.
        ok = live == "PAGE" and dead == "404"
        verdicts[name] = live
        print(f"{name:12} live={live:12} missing={dead:12} {'USABLE' if ok else ''}")
        if ok:
            usable.append(name)

    print()
    if usable:
        print("usable fingerprints:", ", ".join(usable))
        print("suggested primary  :", usable[0])
        print("suggested fallbacks:", tuple(usable[1:4]))
    elif verdicts and all(v == "404" for v in verdicts.values()):
        # Every fingerprint agreeing the control page is gone says nothing about
        # this host: it says the control page is gone. Re-run with a live album.
        print(f"Every fingerprint 404'd {live_url}. That is a dead control URL,")
        print("not a blocked host. Re-run with a release you know is up:")
        print(f"    python {sys.argv[0]} https://<artist>.bandcamp.com/album/<slug>")
    else:
        print("No fingerprint served the live page. This host looks blocked outright.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
