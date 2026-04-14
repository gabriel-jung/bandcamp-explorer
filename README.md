# bandcamp-explorer

A terminal browser and Python library for [Bandcamp](https://bandcamp.com).

Search for artists and albums, discover releases by genre and location,
browse artist/label profiles and discographies — all from the command line.

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

- **Artists** — view bio, browse discography, select an album to see its
  tracklist, select a track to view its page, navigate to the label.
- **Albums** — header with tracklist, description, and lyrics; navigate
  to the artist/host page or select a track.

Press `0` to go back, `Ctrl+C` to quit.

### Output modes

```bash
bandcamp "erang" --artist --json            # output as JSON
bandcamp --tag dungeon-synth --json --limit 100   # cap tag dump
bandcamp https://erang.bandcamp.com/album/tome-iv --json
bandcamp https://erang.bandcamp.com/album/tome-iv --full   # all sections at once
bandcamp -v ...                             # enable debug logging
```

### Terminal images

Album covers and artist images render inline on terminals that support the
iTerm2 or Kitty image protocol (iTerm2, Kitty, WezTerm, Mintty).

## Library

The `core` module has no terminal dependencies — use it in scripts,
pipelines, or other tools. All data is returned as plain dicts with a
`_type` discriminator key.

```python
from bandcamp_explorer.core import (
    BandcampClient, AlbumAPI, ArtistAPI, DiscoverWebAPI, SearchAPI,
    resolve_geoname,
)

with BandcampClient() as client:
    # Search
    results, has_more = SearchAPI(client).search("caladan brood", item_type="album")

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

> The legacy `DiscoverAPI` (the older `dig_deeper` hub endpoint) and
> `resolve_location` (location-tag resolver) are still exported for
> callers that specifically need them, but `DiscoverWebAPI` returns a
> broader feed with richer inline fields (`release_date`, `location`,
> `duration`, `price`, …) and is the recommended entry point.

## License

MIT
