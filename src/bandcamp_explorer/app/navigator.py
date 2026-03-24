"""Interactive entity browser with back-navigation and pagination."""

import json

from ..core.api import AlbumAPI, ArtistAPI
from ..core.client import BandcampClient
from .display import (
    DETAILS,
    SEARCH_SUMMARY,
    SUMMARY,
    console,
)

DISCOGRAPHY_PAGE_SIZE = 50


class Navigator:
    """Handles interactive browsing between entities."""

    def __init__(self, client: BandcampClient, args):
        self.client = client
        self.args = args

    def paginate(self, results, has_more, current_page, fetch_page, summary_key, title):
        """Shared pagination loop for search and tag browse."""
        page_size = len(results)
        while True:
            offset = (current_page - 1) * page_size

            console.print(f"\n[bold]{title}[/bold]")
            console.print(f"[dim]Page {current_page} — {len(results)} results[/dim]\n")

            for i, r in enumerate(results, 1):
                console.print(f"  [bold cyan]\\[{offset + i}][/bold cyan] ", end="")
                self._display_result(r, summary_key)

            console.print()
            hints = []
            if current_page > 1:
                hints.append("[bold]p[/bold]rev page")
            if has_more:
                hints.append("[bold]n[/bold]ext page")
            hints.append("enter number to select")
            console.print(f"[dim]{' | '.join(hints)}[/dim]")

            try:
                choice = console.input("[bold]>[/bold] ").strip()
            except (EOFError, KeyboardInterrupt):
                return

            if not choice:
                continue

            if choice.lower() == "n" and has_more:
                current_page += 1
                with console.status("Loading..."):
                    results, has_more = fetch_page(current_page)
                if not results:
                    console.print("[dim]No more results.[/dim]")
                    return
                continue

            if choice.lower() == "p" and current_page > 1:
                current_page -= 1
                with console.status("Loading..."):
                    results, has_more = fetch_page(current_page)
                continue

            try:
                num = int(choice)
                idx = num - 1 - offset
                if 0 <= idx < len(results):
                    self._handle_selection(results[idx])
                else:
                    console.print("[red]Invalid selection.[/red]")
            except ValueError:
                console.print("[red]Invalid input.[/red]")

    def navigate_entity(self, entity, *, skip_shortcut=False, top_level=False):
        """Display an entity and let the user navigate to related pages."""
        entity_type = entity["_type"]

        if entity_type == "artist":
            discography = entity.get("discography", [])
            if not skip_shortcut and len(discography) == 1:
                url = discography[0].get("url")
                if url:
                    with console.status("Fetching album..."):
                        album = AlbumAPI(self.client).get(url)
                    if album:
                        self.navigate_entity(album, top_level=top_level)
                        return
            self._show_detail(entity)
            if discography:
                self._browse_discography(
                    discography,
                    top_level=top_level,
                    label_name=entity.get("label"),
                    label_url=entity.get("label_url"),
                )
            elif not top_level:
                _wait_for_back()
        else:
            self._show_detail(entity)
            self._album_prompt(entity, top_level=top_level)

    def fetch_selection(self, result):
        """Fetch the full entity for a selected search/browse result."""
        result_type = result.get("_type")

        if result_type == "release_summary":
            url = result.get("album_url")
            if url:
                with console.status("Fetching album..."):
                    return AlbumAPI(self.client).get(url)

        elif result_type == "search_result":
            search_type = result.get("result_type")
            url = result.get("url")
            if not url:
                console.print("[red]No URL available.[/red]")
                return None

            if search_type in ("album", "track"):
                with console.status("Fetching album..."):
                    return AlbumAPI(self.client).get(url)
            elif search_type == "band":
                with console.status("Fetching artist..."):
                    return ArtistAPI(self.client).get(url)
            else:
                console.print(f"[dim]{url}[/dim]")
                return None

        console.print("[red]Failed to fetch details.[/red]")
        return None

    def _handle_selection(self, result):
        """Fetch and display details for a selected result."""
        entity = self.fetch_selection(result)
        if not entity:
            return
        self.navigate_entity(entity)

    def _show_detail(self, entity):
        """Show full details for a selected entity, with JSON support."""
        entity_type = entity["_type"]
        if self.args.json:
            clean = {k: v for k, v in entity.items() if not k.startswith("_")}
            print(json.dumps(clean, indent=2, ensure_ascii=False))
        else:
            DETAILS[entity_type](entity)

    def _display_result(self, r, summary_key):
        """Display a single result using the appropriate summary function."""
        if summary_key == "search_result":
            result_type = r.get("result_type", "band")
            SEARCH_SUMMARY.get(result_type, SEARCH_SUMMARY["band"])(r)
        else:
            SUMMARY[summary_key](r)

    def _album_prompt(self, album, *, top_level=False):
        """After showing an album, let the user navigate to host or track pages."""
        host = album.get("artist", {})
        host_url = host.get("url")
        tracks = album.get("tracks", [])

        if not host_url:
            if not top_level:
                _wait_for_back()
            return

        artist_name = album.get("artist_name", "")
        host_name = host.get("name", "")
        if host_name and host_name != artist_name:
            nav_hint = f"[bold]h[/bold]ost page ({host_name})"
            nav_key = "h"
        else:
            nav_hint = f"[bold]a[/bold]rtist page ({artist_name})"
            nav_key = "a"

        console.print()
        hints = [nav_hint]
        if tracks:
            hints.append("track number to open")
        if not top_level:
            hints.append("[bold]0[/bold] to go back")
        console.print(f"[dim]{' | '.join(hints)}[/dim]")

        try:
            choice = console.input("[bold]>[/bold] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return

        if not choice or choice == "0":
            return

        if choice == nav_key:
            with console.status(f"Fetching {host_name or artist_name}..."):
                artist = ArtistAPI(self.client).get(host_url)
            if artist:
                self.navigate_entity(artist, skip_shortcut=True)
            else:
                console.print("[red]Failed to fetch page.[/red]")
            return

        try:
            num = int(choice)
            track = next((t for t in tracks if t.get("position") == num), None)
            if track and track.get("url"):
                with console.status("Fetching track..."):
                    entity = AlbumAPI(self.client).get(track["url"])
                if entity:
                    self._show_detail(entity)
                    self._album_prompt(entity)
                else:
                    console.print("[red]Failed to fetch track.[/red]")
            else:
                console.print("[red]Invalid track number.[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")

    def _browse_discography(
        self, items, *, top_level=False, label_name=None, label_url=None
    ):
        """Interactive discography browser with pagination."""
        current_page = 1
        total_pages = (len(items) + DISCOGRAPHY_PAGE_SIZE - 1) // DISCOGRAPHY_PAGE_SIZE

        while True:
            start = (current_page - 1) * DISCOGRAPHY_PAGE_SIZE
            end = min(start + DISCOGRAPHY_PAGE_SIZE, len(items))
            page_items = items[start:end]

            console.print()
            console.print(
                f"[bold]Discography[/bold]  "
                f"[dim]{len(items)} releases — page {current_page}/{total_pages}[/dim]"
            )
            console.print()
            for i, item in enumerate(page_items, start + 1):
                artist = item.get("artist_name")
                suffix = f"  [dim]{artist}[/dim]" if artist else ""
                console.print(
                    f"  [bold cyan]\\[{i}][/bold cyan] {item.get('title', '')}{suffix}"
                )

            console.print()
            hints = []
            if current_page > 1:
                hints.append("[bold]p[/bold]rev page")
            if current_page < total_pages:
                hints.append("[bold]n[/bold]ext page")
            hints.append("enter number to select")
            if label_url:
                hints.append(f"[bold]l[/bold]abel ({label_name})")
            if not top_level:
                hints.append("[bold]0[/bold] to go back")
            console.print(f"[dim]{' | '.join(hints)}[/dim]")

            try:
                choice = console.input("[bold]>[/bold] ").strip()
            except (EOFError, KeyboardInterrupt):
                return

            if choice == "0" and not top_level:
                return

            if not choice:
                continue

            if choice.lower() == "l" and label_url:
                with console.status(f"Fetching {label_name}..."):
                    label = ArtistAPI(self.client).get(label_url)
                if label:
                    self.navigate_entity(label)
                else:
                    console.print("[red]Failed to fetch label.[/red]")
                continue

            if choice.lower() == "n" and current_page < total_pages:
                current_page += 1
                continue

            if choice.lower() == "p" and current_page > 1:
                current_page -= 1
                continue

            try:
                num = int(choice)
                idx = num - 1
                if 0 <= idx < len(items):
                    url = items[idx].get("url")
                    if url:
                        with console.status("Fetching album..."):
                            album = AlbumAPI(self.client).get(url)
                        if album:
                            self._show_detail(album)
                            self._album_prompt(album)
                        else:
                            console.print("[red]Failed to fetch album.[/red]")
                    else:
                        console.print("[red]No URL available.[/red]")
                else:
                    console.print("[red]Invalid selection.[/red]")
            except ValueError:
                console.print("[red]Invalid input.[/red]")


def _wait_for_back():
    """Wait for user to press 0 to go back."""
    console.print("\n[dim][bold]0[/bold] to go back[/dim]")
    while True:
        try:
            choice = console.input("[bold]>[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0" or not choice:
            return
