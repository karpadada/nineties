from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from .discovery import DiscoveryError
from .downloader import DownloadError
from .services import AppServices
from .storage import SafeRemoveResult, StorageError, safely_remove_player


class SearchResult(TypedDict):
    kind: str
    title: str
    creator: str
    details: str
    url: str


class SearchResponse(TypedDict):
    results: list[SearchResult]


class CollectionSummary(TypedDict):
    id: str
    kind: str
    title: str
    artist: str
    status: str
    directory: str
    progress: dict[str, Any]
    integrity: NotRequired[str]
    error: NotRequired[str | None]


class DownloadResponse(TypedDict):
    collection: CollectionSummary


class StatusResponse(TypedDict):
    jobs: list[CollectionSummary]


class LibraryResponse(TypedDict):
    collections: list[CollectionSummary]


class MusicAgentAPI:
    """Small, transport-independent API intended for an AI tool caller."""

    def __init__(self, services: AppServices) -> None:
        self.services = services

    def search(self, query: str, limit: int = 8) -> SearchResponse:
        query = query.strip()
        if not query:
            raise ValueError("Enter a search term.")
        limit = max(1, min(limit, 12))
        try:
            results = self.services.discovery.search(query, limit=limit)
        except DiscoveryError as exc:
            raise ValueError(str(exc)) from exc
        return {"results": [SearchResult(**item) for item in results]}

    def download(
        self,
        source_url: str,
        kind: Literal["album", "playlist"] | None = None,
    ) -> DownloadResponse:
        try:
            collection = self.services.downloads.enqueue(source_url, kind)
        except DownloadError as exc:
            raise ValueError(str(exc)) from exc
        return {"collection": _summary(collection)}

    def status(self, job_id: str | None = None) -> StatusResponse:
        if self.services.simulator:
            self.services.simulator.require_connected()
        if job_id:
            collection = self.services.store.get(job_id)
            if collection is None:
                raise ValueError("No managed collection has that job ID.")
            collections = [self.services.store.reconciled(collection)]
        else:
            collections = self.services.downloads.jobs()
        return {"jobs": [_summary(item) for item in collections]}

    def library(self, query: str | None = None, limit: int = 50) -> LibraryResponse:
        if self.services.simulator:
            self.services.simulator.require_connected()
        normalized_query = (query or "").strip().casefold()
        limit = max(1, min(limit, 100))
        collections = reversed(self.services.store.all())
        if normalized_query:
            collections = (
                item
                for item in collections
                if normalized_query
                in " ".join(
                    str(item.get(field, ""))
                    for field in ("title", "artist", "kind", "directory")
                ).casefold()
            )
        selected = list(collections)[:limit]
        return {
            "collections": [
                _summary(self.services.store.reconciled(item)) for item in selected
            ]
        }

    def safely_remove(self) -> SafeRemoveResult:
        try:
            return safely_remove_player(self.services.config, self.services.downloads)
        except StorageError as exc:
            raise ValueError(str(exc)) from exc


def _summary(collection: dict[str, Any]) -> CollectionSummary:
    result = CollectionSummary(
        id=str(collection.get("id", "")),
        kind=str(collection.get("kind", "")),
        title=str(collection.get("title", "")),
        artist=str(collection.get("artist", "")),
        status=str(collection.get("status", "")),
        directory=str(collection.get("directory", "")),
        progress=dict(collection.get("progress") or {}),
    )
    if "integrity" in collection:
        result["integrity"] = str(collection["integrity"])
    if collection.get("error"):
        result["error"] = str(collection["error"])
    return result
