"""In-place transforms that add derived display fields to API entities."""

from .format import album_host


def prepare_album(album: dict, *, lyrics_as_text: bool = False) -> None:
    """Precompute app-friendly derived fields on an album entity.

    Adds ``_host_label`` (labelled host/artist line). When any track has
    lyrics, sets either ``_lyrics_tracks`` (list of track dicts, for the
    CLI's custom renderer) or ``_lyrics_text`` (a single joined string,
    for the Discord renderer) depending on ``lyrics_as_text``.
    """
    host = album_host(album)
    artist = album.get("artist_name", "")
    album["_host_label"] = f"Host: {host}" if host else f"Artist: {artist}"

    lyrics = [t for t in album.get("tracks", []) if t.get("lyrics")]
    if not lyrics:
        return
    if lyrics_as_text:
        album["_lyrics_text"] = "\n\n".join(
            f"**{t.get('title', '')}**\n{t['lyrics'].strip()}" for t in lyrics
        )
    else:
        album["_lyrics_tracks"] = lyrics
