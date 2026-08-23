# bandcamp-explorer

A terminal browser and Python library for [Bandcamp](https://bandcamp.com).

Search for artists and albums, discover releases by genre and location,
browse artist/label profiles and discographies, all from the command line.

## Install

Requires Python 3.12+.

### Terminal CLI

```bash
uv tool install bandcamp-explorer
# or
pip install bandcamp-explorer
```

### Discord bot

```bash
uv tool install bandcamp-explorer[discord]
# or
pip install bandcamp-explorer[discord]
```

Create a bot application at the [Discord Developer Portal](https://discord.com/developers/applications),
enable the `bot` scope with `Send Messages` and `Use Slash Commands` permissions,
then invite it to your server with the generated OAuth2 URL.

Set your bot token and run:

```bash
export DISCORD_TOKEN=your-bot-token
bandcamp-discord
# or with a .env file in the current directory
bandcamp-discord
```

Use `--guild GUILD_ID` to sync slash commands instantly to a specific server
(global sync can take up to an hour).

Slash commands (all under `/bandcamp`):

| Command | Description |
|---------|-------------|
| `/bandcamp search <query>` | Search everything |
| `/bandcamp album <query>` | Search albums |
| `/bandcamp artist <query>` | Search artists/labels |
| `/bandcamp track <query>` | Search tracks |
| `/bandcamp discover <tag>` | Browse releases by tag (with optional slice and location filters) |

### Development

```bash
git clone https://github.com/gabriel-jung/bandcamp-explorer.git
cd bandcamp-explorer
uv sync
```

## CLI

### Search

```bash
bandcamp "caladan brood"                  # search everything
bandcamp "erang" --artist                 # artists/labels only
bandcamp "echoes of battle" --album       # albums only
bandcamp "a forest whisper" --track       # tracks only
```

### Browse by tag

```bash
bandcamp --tag dungeon-synth                        # newest arrivals (default)
bandcamp --tag black-metal --top                    # best-selling
bandcamp --tag dungeon-synth --rand                 # surprise me
bandcamp --tag dungeon-synth --location france
bandcamp --tag dungeon-synth --location paris
bandcamp --tag dungeon-synth black-metal            # multi-tag
```

Slices: `--new` (default), `--top`, `--rand`.

Locations are resolved to geoname IDs via Bandcamp's autocomplete and
cached locally; force a refresh with `--refresh-location`.

### Direct URLs

```bash
bandcamp https://erang.bandcamp.com/album/tome-iv
bandcamp https://erang.bandcamp.com
```

### Interactive navigation

After selecting a result, you enter an interactive browser:

- **Artists**: view bio, browse discography, select an album to see its
  tracklist, select a track to view its page, navigate to the label.
- **Albums**: header with tracklist, description, and lyrics; navigate
  to the artist/host page or select a track.

Press `0` to go back, `Ctrl+C` to quit.

### Output modes

```bash
bandcamp "erang" --artist --json            # output as JSON
bandcamp "erang" --limit 10                 # cap results
bandcamp --tag dungeon-synth --json --limit 100   # cap tag dump
bandcamp https://erang.bandcamp.com/album/tome-iv --json
bandcamp https://erang.bandcamp.com/album/tome-iv --full   # all sections at once
bandcamp -v ...                             # enable debug logging
```

### Terminal images

Album covers and artist images render inline on terminals that support the
iTerm2 or Kitty image protocol (iTerm2, Kitty, WezTerm, Mintty).

## Library

The `core` module has no terminal dependencies; use it in scripts,
pipelines, or other tools. All data is returned as plain dicts with a
`_type` discriminator key.

```python
from bandcamp_explorer.core import (
    BandcampClient, AlbumAPI, ArtistAPI, DiscoverWebAPI, SearchAPI,
    NotFoundError, resolve_geoname,
)

with BandcampClient() as client:
    # Search (one call returns the whole result set)
    results = SearchAPI(client).search("caladan brood", item_type="album")

    # Discover releases by tag (new discover_web endpoint)
    discover = DiscoverWebAPI(client)
    releases, cursor, total = discover.discover(tags=["dungeon-synth"], slice_="new")
    all_releases = discover.discover_all(tags=["dungeon-synth"], max_pages=3)

    # Fetch album details (skip cover-art bytes with fetch_art=False)
    album = AlbumAPI(client).get("https://erang.bandcamp.com/album/tome-iv")
    for track in album["tracks"]:
        print(f"  {track['position']}. {track['title']} ({track['duration']})")

    # Fetch artist/label profile
    artist = ArtistAPI(client).get("https://erang.bandcamp.com")
    for item in artist["discography"]:
        print(f"  {item['title']}")

    # Location filtering (geoname-based)
    geoname_id = resolve_geoname(client, "paris")
    releases, _, _ = discover.discover(tags=["dungeon-synth"], geoname_id=geoname_id)

    # Download images
    client.download_image(album.get("image_url"), output_dir="./images/")
```

### Errors

`AlbumAPI.get` and `ArtistAPI.get` raise `NotFoundError` when a page 404s, so
callers can tell a deleted release from a failed fetch. Every other transport
failure returns `None`. If Bandcamp answers with its bot-defence interstitial
(HTTP 200 with no content in it), the client raises `ChallengeError` and then
fails fast for two minutes rather than hammering a blocked endpoint. Never
treat a `ChallengeError` as a missing resource; it means "ask again later".

```python
from bandcamp_explorer.core import ChallengeError, NotFoundError

try:
    album = AlbumAPI(client).get(url)
except NotFoundError:
    ...  # gone for good, stop retrying
except ChallengeError:
    ...  # blocked for now, retry later
```

One case does not follow that split. The root page of an artist subdomain that
no longer exists never answers 404: it answers HTTP 200 with either the
bot-defence interstitial or Bandcamp's signup page, so nothing read off the
root tells a deleted host from a live one. Since 0.8.0, when a host root
produces no artist, `/music` on the same host is asked before any conclusion is
drawn, and a 404 there raises `NotFoundError` where earlier versions raised
`ChallengeError` or returned an artist with no name. Any other answer keeps the
old behaviour: only a real 404 is read as gone. It costs one extra request per
dead host, once.

### TLS fingerprints

`impersonate` picks the curl_cffi fingerprint the session uses, defaulting to
the floating `"chrome"` alias. Worth changing if Bandcamp starts refusing the
default one.

```python
client = BandcampClient(impersonate="firefox144")
```

`fallback_impersonate` is an escape hatch for a host that finds one
fingerprint answered a 404 where another served the page: it re-checks a 404 on
other fingerprints before raising, and names the ones that agreed in
`NotFoundError.confirmed_by`. That situation has not been reproduced here, and
the ladder is off by default since it costs an extra request per fallback on
every genuine 404. `scripts/probe_fingerprints.py` measures which fingerprints
work from the machine that will run the fetches, reading response bodies rather
than status codes because a blocked fingerprint answers HTTP 200 with a
bot-defence interstitial, which a status-code-only probe scores as success.

```python
from bandcamp_explorer.core import SUGGESTED_FALLBACK_IMPERSONATE

client = BandcampClient(fallback_impersonate=SUGGESTED_FALLBACK_IMPERSONATE)
```

> Bandcamp removed the `dig_deeper` hub endpoint, so `DiscoverAPI` was dropped
> in 0.6.0; use `DiscoverWebAPI`. `resolve_location` went with it: it resolved
> Bandcamp's internal location *tag* ids, which only that endpoint accepted.
> `DiscoverWebAPI` filters by `geoname_id`, so `resolve_geoname` is the one you
> want.

## License

MIT
