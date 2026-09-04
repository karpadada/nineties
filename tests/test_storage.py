from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nineties_music.config import AppConfig
from nineties_music.downloader import DownloadManager
from nineties_music.storage import StorageError, safely_remove_player
from nineties_music.store import LibraryStore

from test_agent import FakeDownloader


def make_storage(tmp_path: Path):
    player = tmp_path / "Music"
    player.mkdir()
    config = AppConfig(
        project_root=tmp_path,
        library_dir=player / "Music",
        state_dir=player / ".nineties-music",
        player_volume=player,
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    return config, store, manager


def test_safely_remove_uses_diskutil_eject(tmp_path: Path, monkeypatch) -> None:
    config, _, manager = make_storage(tmp_path)
    calls = []

    def fake_run(command, **options):
        calls.append((command, options))
        return subprocess.CompletedProcess(command, 0, "Disk ejected", "")

    monkeypatch.setattr("nineties_music.storage.subprocess.run", fake_run)

    result = safely_remove_player(config, manager)

    assert result == {
        "safely_removed": True,
        "volume": str(config.player_volume),
    }
    assert calls == [
        (
            ["/usr/sbin/diskutil", "eject", str(config.player_volume)],
            {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "check": False,
            },
        )
    ]


def test_safely_remove_refuses_active_downloads(tmp_path: Path, monkeypatch) -> None:
    config, store, manager = make_storage(tmp_path)
    manager.enqueue("https://music.youtube.com/playlist?list=OLAK_album", "album")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("nineties_music.storage.subprocess.run", fake_run)

    with pytest.raises(StorageError, match="operation to finish"):
        safely_remove_player(config, manager)

    assert store.all()[0]["status"] == "queued"
    assert called is False


def test_safely_remove_refuses_collection_removal(tmp_path: Path) -> None:
    config, store, manager = make_storage(tmp_path)
    collection = manager.enqueue(
        "https://music.youtube.com/playlist?list=OLAK_album", "album"
    )
    store.update(collection["id"], {"status": "failed"})
    assert store.claim_removal(collection["id"], "remover") is not None

    with pytest.raises(StorageError, match="operation to finish"):
        safely_remove_player(config, manager)


def test_safely_remove_reports_eject_failure(tmp_path: Path, monkeypatch) -> None:
    config, _, manager = make_storage(tmp_path)
    monkeypatch.setattr(
        "nineties_music.storage.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "busy"),
    )

    with pytest.raises(StorageError, match="could not be safely removed"):
        safely_remove_player(config, manager)
