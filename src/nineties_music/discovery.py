from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode

from .downloader import normalize_artist_name
from .updates import CompatibilityUpdater, update_youtube_packages


class DiscoveryError(RuntimeError):
    pass


class MusicDiscovery:
    def __init__(
        self,
        compatibility_updater: CompatibilityUpdater = update_youtube_packages,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._client: Any | None = None
        self._client_factory = client_factory
        self._compatibility_updater = compatibility_updater
        self._client_lock = threading.Lock()

    @property
    def client(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                from ytmusicapi import YTMusic

                self._client = YTMusic()
        return self._client

    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        query = query.strip()
        if not query:
            return []
        with self._client_lock:
            try:
                albums, playlists = self._search(query, limit)
            except Exception as first_error:
                if not self._compatibility_updater():
                    raise DiscoveryError(
                        f"YouTube Music search failed: {first_error}"
                    ) from first_error
                self._discard_client()
                try:
                    albums, playlists = self._search(query, limit)
                except Exception as retry_error:
                    raise DiscoveryError(
                        "YouTube Music search failed after updating compatibility "
                        f"packages: {retry_error}"
                    ) from retry_error

        results: list[dict[str, str]] = []
        for item in albums[:limit]:
            browse_id = str(item.get("browseId", ""))
            playlist_id = str(item.get("playlistId", ""))
            if not browse_id and not playlist_id:
                continue
            artists = item.get("artists") or []
            artist = str(item.get("artist") or "")
            if not artist and artists:
                artist = str(artists[0].get("name") or "")
            artist = normalize_artist_name(artist) or "Unknown artist"
            year = str(item.get("year") or "")
            if playlist_id:
                source_url = "https://music.youtube.com/playlist?" + urlencode(
                    {"list": playlist_id}
                )
            else:
                source_url = (
                    f"https://music.youtube.com/browse/{quote(browse_id, safe='')}"
                )
            results.append(
                {
                    "kind": "album",
                    "title": str(item.get("title") or "Untitled album"),
                    "creator": artist,
                    "details": year,
                    "thumbnail_url": self._thumbnail_url(item),
                    "url": source_url,
                }
            )

        for item in playlists[:limit]:
            browse_id = str(item.get("browseId") or item.get("playlistId") or "")
            playlist_id = browse_id.removeprefix("VL")
            if not playlist_id:
                continue
            author = item.get("author") or item.get("artists") or []
            if isinstance(author, list) and author:
                creator = str(author[0].get("name") or "Unknown curator")
            elif isinstance(author, str):
                creator = author
            else:
                creator = "Unknown curator"
            creator = normalize_artist_name(creator) or "Unknown curator"
            count = item.get("itemCount") or item.get("count") or ""
            details = f"{count} tracks" if count else ""
            results.append(
                {
                    "kind": "playlist",
                    "title": str(item.get("title") or "Untitled playlist"),
                    "creator": creator,
                    "details": details,
                    "thumbnail_url": self._thumbnail_url(item),
                    "url": "https://music.youtube.com/playlist?"
                    + urlencode({"list": playlist_id}),
                }
            )
        return results

    def _search(
        self, query: str, limit: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        albums = self.client.search(query, filter="albums", limit=limit)
        playlists = self.client.search(
            query, filter="community_playlists", limit=limit
        )
        return albums, playlists

    def _discard_client(self) -> None:
        self._client = None
        if self._client_factory is None:
            for module_name in tuple(sys.modules):
                if module_name == "ytmusicapi" or module_name.startswith(
                    "ytmusicapi."
                ):
                    del sys.modules[module_name]

    @staticmethod
    def _thumbnail_url(item: dict[str, Any]) -> str:
        thumbnails = item.get("thumbnails") or []
        if not isinstance(thumbnails, list):
            return ""

        available = [
            thumbnail
            for thumbnail in thumbnails
            if isinstance(thumbnail, dict) and thumbnail.get("url")
        ]
        if not available:
            return ""

        def area(thumbnail: dict[str, Any]) -> int:
            try:
                return int(thumbnail.get("width") or 0) * int(
                    thumbnail.get("height") or 0
                )
            except (TypeError, ValueError):
                return 0

        display_area = 80 * 80
        large_enough = [
            thumbnail for thumbnail in available if area(thumbnail) >= display_area
        ]
        selected = (
            min(large_enough, key=area)
            if large_enough
            else max(available, key=area)
        )
        return str(selected["url"])
