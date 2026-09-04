from __future__ import annotations

from pathlib import Path

import pytest

from nineties_music.agent import MusicAgentAPI
from nineties_music.config import AppConfig
from nineties_music.downloader import DownloadManager
from nineties_music.services import AppServices
from nineties_music.store import LibraryStore


class FakeDiscovery:
    def search(self, query: str, limit: int = 8) -> list[dict[str, str]]:
        return [
            {
                "kind": "album",
                "title": "Fictional Album",
                "creator": "Fictional Artist",
                "details": "1998",
                "url": "https://music.youtube.com/playlist?list=OLAK_album",
            }
        ][:limit]


class FakeDownloader:
    def probe(
        self,
        url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, object]:
        return {
            "source_url": url,
            "source_id": "OLAK_album",
            "kind": kind_hint or "album",
            "title": "Fictional Album",
            "artist": "Fictional Artist",
            "directory": "Fictional Artist/Fictional Album",
            "track_total": 10,
        }


def make_api(
    tmp_path: Path, *, player_volume: Path | None = None
) -> tuple[MusicAgentAPI, LibraryStore, DownloadManager]:
    config = AppConfig(
        project_root=tmp_path,
        library_dir=tmp_path / "music",
        state_dir=tmp_path / "state",
        player_volume=player_volume,
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    services = AppServices(
        config=config,
        store=store,
        discovery=FakeDiscovery(),  # type: ignore[arg-type]
        downloads=manager,
    )
    return MusicAgentAPI(services), store, manager


def test_agent_search_and_download(tmp_path: Path) -> None:
    api, store, _ = make_api(tmp_path)

    results = api.search("Fictional Album", limit=100)
    assert results["results"][0]["creator"] == "Fictional Artist"

    queued = api.download(results["results"][0]["url"], "album")
    collection = queued["collection"]
    assert collection["status"] == "queued"
    assert collection["directory"] == "Fictional Artist/Fictional Album"
    assert store.get(collection["id"]) is not None


def test_agent_status_and_library_are_compact(tmp_path: Path) -> None:
    api, store, _ = make_api(tmp_path)
    collection = api.download(
        "https://music.youtube.com/playlist?list=OLAK_album", "album"
    )["collection"]
    store.update(collection["id"], {"status": "failed", "error": "Unavailable"})

    status = api.status(collection["id"])["jobs"][0]
    assert status["error"] == "Unavailable"
    assert "source_url" not in status

    library = api.library("artist")["collections"]
    assert [item["id"] for item in library] == [collection["id"]]


def test_agent_rejects_unknown_job(tmp_path: Path) -> None:
    api, _, _ = make_api(tmp_path)

    with pytest.raises(ValueError, match="job ID"):
        api.status("missing")


def test_agent_safely_removes_player(tmp_path: Path, monkeypatch) -> None:
    player = tmp_path / "Music"
    player.mkdir()
    api, _, _ = make_api(tmp_path, player_volume=player)
    monkeypatch.setattr(
        "nineties_music.agent.safely_remove_player",
        lambda config, downloads: {
            "safely_removed": True,
            "volume": str(config.player_volume),
        },
    )

    assert api.safely_remove() == {
        "safely_removed": True,
        "volume": str(player),
    }
