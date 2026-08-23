"""Tests for ``ArtistPageParser`` across the two artist-page layouts.

Bandcamp serves the standard theme (profile in ``p#band-name-location``,
releases in ``ol#music-grid``) and a custom "index page" theme carrying
neither: identity only in the ``data-band`` JSON blob, releases in ``.ipCell``
anchors. Reading only the standard theme returns a nameless artist with an
empty discography from a page that is alive and lists releases, which is the
same shape a defunct subdomain's root parses to.

The bodies below are distilled from real pages: only the markers the parser
reads are kept, since the served pages run to well over a hundred kilobytes of
markup that none of this code looks at.
"""

from bs4 import BeautifulSoup

from bandcamp_explorer.core.parsers import ArtistPageParser

INDEX_URL = "https://wizardashdod.bandcamp.com"

DATA_BAND = (
    '{"id":226795703,"create_date":"28 Mar 2013 19:06:27 GMT","disabled_date":null,'
    '"name":"wizard ashdod","subdomain":"wizardashdod",'
    '"url":"https://wizardashdod.bandcamp.com"}'
)


def _cell(href: str | None, title: str | None = None, artist: str | None = None) -> str:
    """One index-page cell. ``href=None`` is the empty padding cell the layout
    appends to square off a row."""
    if href is None:
        return "<div class='ipCell'><div class='ipCellSet'></div></div>"
    return (
        "<div class='ipCell'><div class='ipCellSet'>"
        f"<div class='ipCellImage'><a href='{href}'>"
        "<img class='resizableArt' src='https://f4.bcbits.com/img/a1_2.jpg'></a></div>"
        "<div class='ipCellLabel'>"
        f"<div class='ipCellLabel1'><a href='{href}'>{title}</a></div>"
        + (f"<div class='ipCellLabel2'><a href='{href}'>{artist}</a></div>" if artist else "")
        + "</div></div></div>"
    )


# Real page, real order: four releases on the page's own subdomain, five on two
# others, one hub link to another band's /releases page, two padding cells.
FBM = "https://fullbodymassagerecords1.bandcamp.com"
DDMM = "https://ddmmyyyy1.bandcamp.com"
INDEX_BODY = (
    "<html><head><title>wizard ashdod</title></head><body>"
    f"<div data-band='{DATA_BAND}'>"
    "<span class='indexpage_list cols3'><span class='indexpage_list_row'>"
    "<span class='indexpage_list_cell odd'>"
    + _cell(f"{FBM}/album/incense-in-the-hall", "Incense in the hall", "Wizard Ashdod")
    + _cell(f"{FBM}/album/wizard-ashdod-nussbaum", "Wizard Ashdod - Nussbaum", "Wizard Ashdod")
    + _cell(f"{FBM}/album/wizard-ashdod-breccia", "WIZARD ASHDOD - BRECCIA", "wizard ashdod")
    + _cell("/album/see-you-on-the-street", "see you on the street", "wizard ashdod")
    + _cell(f"{DDMM}/releases", "dd/mm/yyyy")
    + _cell("/album/bridge-under-water", "bridge under water", "wizard ashdod")
    + _cell("/album/holiday-in-ashdod", "holiday in ashdod", "wizard ashdod")
    + _cell(f"{DDMM}/album/first", "first", "dd/mm/yyyy")
    + _cell("/album/wizard-ashdod", "wizard ashdod", "wizard ashdod")
    + _cell(
        f"{FBM}/album/bastian-hagedorn-and-wizard-ashdod-live-at-waldfest-emck",
        "BASTIAN HAGEDORN AND WIZARD ASHDOD LIVE AT WALDFEST/EMCK",
        "wizard ashdod and bastian hagedorn",
    )
    + _cell(None)
    + _cell(None)
    + "</span></span></span></div></body></html>"
)

INDEX_RELEASE_URLS = [
    f"{FBM}/album/incense-in-the-hall",
    f"{FBM}/album/wizard-ashdod-nussbaum",
    f"{FBM}/album/wizard-ashdod-breccia",
    f"{INDEX_URL}/album/see-you-on-the-street",
    f"{INDEX_URL}/album/bridge-under-water",
    f"{INDEX_URL}/album/holiday-in-ashdod",
    f"{DDMM}/album/first",
    f"{INDEX_URL}/album/wizard-ashdod",
    f"{FBM}/album/bastian-hagedorn-and-wizard-ashdod-live-at-waldfest-emck",
]

# The standard theme, both markers present. data-band is on this layout too, so
# these cases also pin that the DOM wins wherever it has an answer.
STANDARD_URL = "https://craveblood.bandcamp.com"
STANDARD_BODY = (
    "<html><body>"
    '<div data-band=\'{"id":266336502,"name":"CraveBlood"}\' data-band-id="266336502">'
    "<p id='band-name-location'><span class='title'>CraveBlood</span>"
    "<span class='location'>Calabria, Italy</span></p>"
    "<p id='bio-text'>Death metal.<span class='peekaboo-link'>... more</span></p>"
    "<img class='band-photo' src='https://f4.bcbits.com/img/photo.jpg'>"
    "<ol id='music-grid'>"
    "<li data-item-id='album-1'><a href='/album/one'>"
    "<img src='https://f4.bcbits.com/img/one_2.jpg'><p class='title'>One</p></a></li>"
    "<li data-item-id='track-2'><a href='/track/two'><p class='title'>"
    "<span class='artist-override'>Guest</span>Two</p></a></li>"
    "</ol></div></body></html>"
)

# What a defunct subdomain's root serves. Neither body carries data-band, which
# is what keeps the fallback from naming a band that is gone.
SIGNUP_BODY = (
    "<html><head><title>Signup | Bandcamp</title></head><body>"
    "<div id='signup-form' data-for-band-id=''><input name='email'></div></body></html>"
)
CHALLENGE_BODY = "<html><head><title>Client Challenge</title></head></html>"


def parse(body: str, url: str, **kwargs) -> dict | None:
    return ArtistPageParser(BeautifulSoup(body, "html.parser"), url, **kwargs).parse()


def test_the_index_page_layout_yields_identity_from_data_band():
    artist = parse(INDEX_BODY, INDEX_URL)

    assert artist["artist_id"] == "226795703"
    assert artist["name"] == "wizard ashdod"


def test_the_index_page_layout_has_no_source_for_location_bio_or_image():
    """Nothing on this layout carries them, so they stay None rather than
    being invented from elsewhere."""
    artist = parse(INDEX_BODY, INDEX_URL)

    assert artist["location"] is None
    assert artist["bio"] is None
    assert artist["image_url"] is None


def test_the_index_page_layout_yields_every_release():
    artist = parse(INDEX_BODY, INDEX_URL)

    assert [item["url"] for item in artist["discography"]] == INDEX_RELEASE_URLS


def test_off_domain_index_page_releases_stay_absolute():
    """More than half this page's releases live on other subdomains.
    Resolving them against the page URL would point them at the wrong band."""
    urls = [item["url"] for item in parse(INDEX_BODY, INDEX_URL)["discography"]]

    assert f"{FBM}/album/incense-in-the-hall" in urls
    assert f"{DDMM}/album/first" in urls


def test_an_index_page_hub_link_is_not_a_release():
    """Cells also link other bands' /releases discography pages, which are not
    releases and must not reach a caller expecting album URLs."""
    urls = [item["url"] for item in parse(INDEX_BODY, INDEX_URL)["discography"]]

    assert f"{DDMM}/releases" not in urls


def test_index_page_items_carry_the_grid_item_shape():
    items = parse(INDEX_BODY, INDEX_URL)["discography"]

    assert items[0] == {
        "_type": "album",
        "title": "Incense in the hall",
        "artist_name": None,
        "item_type": "album",
        "url": f"{FBM}/album/incense-in-the-hall",
        "art_url": "https://f4.bcbits.com/img/a1_2.jpg",
    }


def test_an_index_page_release_by_another_artist_names_it():
    """Every cell is labelled with its artist, the page's own included.
    Reporting it only when it differs matches the grid, where artist_name is
    None unless an .artist-override says otherwise."""
    by_url = {item["url"]: item for item in parse(INDEX_BODY, INDEX_URL)["discography"]}

    assert by_url[f"{DDMM}/album/first"]["artist_name"] == "dd/mm/yyyy"
    assert by_url[f"{INDEX_URL}/album/wizard-ashdod"]["artist_name"] is None


def test_an_index_page_track_is_typed_from_its_url():
    """There is no data-item-id on this layout, so the type comes from the path."""
    body = INDEX_BODY.replace("/album/holiday-in-ashdod", "/track/holiday-in-ashdod")
    by_url = {item["url"]: item for item in parse(body, INDEX_URL)["discography"]}

    assert by_url[f"{INDEX_URL}/track/holiday-in-ashdod"]["item_type"] == "track"


def test_an_index_page_with_no_releases_still_names_its_artist():
    """A third shape seen live: data-band only, no list of any kind. The
    discography is genuinely empty; the identity is not."""
    body = '<html><body><div data-band=\'{"id":3444826040,"name":"Elegycult"}\'></div></body></html>'

    artist = parse(body, "https://elegycult.bandcamp.com")

    assert artist["artist_id"] == "3444826040"
    assert artist["name"] == "Elegycult"
    assert artist["discography"] == []


def test_the_index_page_layout_parses_in_discography_only_mode():
    """``ArtistAPI`` re-parses /music with the profile skipped, and that page
    uses the same layout as the root."""
    page = parse(INDEX_BODY, f"{INDEX_URL}/music", discography_only=True)

    assert [item["url"] for item in page["discography"]] == INDEX_RELEASE_URLS


def test_the_standard_layout_is_unchanged():
    artist = parse(STANDARD_BODY, STANDARD_URL)

    assert artist["artist_id"] == "266336502"
    assert artist["name"] == "CraveBlood"
    assert artist["location"] == "Calabria, Italy"
    assert artist["bio"] == "Death metal."
    assert artist["image_url"] == "https://f4.bcbits.com/img/photo.jpg"
    assert [(i["title"], i["item_type"], i["artist_name"]) for i in artist["discography"]] == [
        ("One", "album", None),
        ("Two", "track", "Guest"),
    ]
    assert [i["url"] for i in artist["discography"]] == [
        f"{STANDARD_URL}/album/one",
        f"{STANDARD_URL}/track/two",
    ]


def test_the_dom_wins_over_data_band_where_both_are_present():
    """The standard layout carries data-band too, and the header stays the
    source of truth there, so nothing about its parse moves."""
    body = STANDARD_BODY.replace('"name":"CraveBlood"', '"name":"stale blob name"')

    assert parse(body, STANDARD_URL)["name"] == "CraveBlood"


def test_a_signup_page_root_still_names_nobody():
    """``_looks_like_a_dead_host_root`` keys on a root that parsed to no id and
    no name; widening where a name may come from must not blind it. Note
    data-for-band-id, a different attribute that neither selector matches."""
    artist = parse(SIGNUP_BODY, "https://gone.bandcamp.com")

    assert not artist["artist_id"]
    assert not artist["name"]


def test_a_challenge_page_root_still_names_nobody():
    artist = parse(CHALLENGE_BODY, "https://gone.bandcamp.com")

    assert not artist["artist_id"]
    assert not artist["name"]


def test_an_unparseable_data_band_blob_is_not_an_identity():
    """A truncated or re-shaped blob fails closed, leaving the page nameless,
    rather than raising out of profile extraction."""
    artist = parse(
        "<html><body><div data-band='{not json'></div></body></html>",
        "https://x.bandcamp.com",
    )

    assert not artist["artist_id"]
    assert not artist["name"]
