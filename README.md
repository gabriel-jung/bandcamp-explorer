# pybandcamp

A terminal browser and Python library for [Bandcamp](https://bandcamp.com).

Search for artists and albums, discover releases by genre and location,
browse artist/label profiles and discographies — all from the command line.

## Install

```bash
pip install pybandcamp
# or
uv tool install pybandcamp
```

For local development:

```bash
git clone https://github.com/gabriel-jung/pybandcamp.git
cd pybandcamp
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
bandcamp --tag dungeon-synth
bandcamp --tag black-metal --sort pop
bandcamp --tag dungeon-synth --location france
bandcamp --tag dungeon-synth --location paris
```

Sort modes: `date` (default), `pop` (popular).

Location tag IDs are discovered from Bandcamp at runtime and cached locally.
Force a refresh with `--refresh-location`.

### Direct URLs

```bash
bandcamp https://erang.bandcamp.com/album/tome-iv
bandcamp https://erang.bandcamp.com
```

### Navigation

- Enter a number to select an item from any list
- `n` / `p` — next / previous page
- `a` — navigate to artist page (from album)
- `h` — navigate to host/label page (when different from artist)
- `0` — go back
- `Ctrl+C` — quit

### JSON output

```bash
bandcamp "erang" --artist --json
bandcamp --tag dungeon-synth --json
bandcamp https://erang.bandcamp.com/album/tome-iv --json
```

## Library

```python
from pybandcamp.core import BandcampClient, AlbumAPI, ArtistAPI, DiscoverAPI, SearchAPI

with BandcampClient() as client:
    # Search
    results, has_more = SearchAPI(client).search("caladan brood", item_type="album")

    # Discover releases by tag
    discover = DiscoverAPI(client)
    releases, has_more = discover.discover(tags=["dungeon-synth"], sort="pop")
    all_releases = discover.discover_all(tags=["dungeon-synth"], max_pages=3)

    # Fetch album details
    album = AlbumAPI(client).get("https://erang.bandcamp.com/album/tome-iv")
    for track in album["tracks"]:
        print(f"  {track['position']}. {track['title']} ({track['duration']})")

    # Fetch artist/label profile
    artist = ArtistAPI(client).get("https://erang.bandcamp.com")
    for item in artist["discography"]:
        print(f"  {item['title']}")

    # Location filtering
    from pybandcamp.core import resolve_location
    tag_id = resolve_location(client, "paris")
    releases, _ = discover.discover(tags=["dungeon-synth", tag_id])
```

All data is returned as plain dicts with a `_type` discriminator key.
The `core` module has no terminal dependencies — use it in scripts,
pipelines, or other tools.

## License

MIT
