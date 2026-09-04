from __future__ import annotations

from pathlib import Path

import pytest

from nineties_music.config import AppConfig
from nineties_music.downloader import DownloadManager
from nineties_music.storage import (
    StorageError,
    _recognize_player_disks,
    safely_remove_player,
)
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
    companion = tmp_path / "ECHO NANO"
    companion.mkdir()
    config, _, manager = make_storage(tmp_path)
    calls = []
    monkeypatch.setattr(
        "nineties_music.storage._recognize_player_disks",
        lambda _volume: [
            {"identifier": "disk20", "mount_points": [str(companion)]},
            {
                "identifier": "disk21",
                "mount_points": [str(config.player_volume)],
            },
        ],
    )
    monkeypatch.setattr(
        "nineties_music.storage._run_diskutil",
        lambda *arguments: calls.append(arguments),
    )

    result = safely_remove_player(config, manager)

    assert result == {
        "safely_removed": True,
        "volume": str(config.player_volume),
        "volumes": [str(companion), str(config.player_volume)],
    }
    assert calls == [("eject", "disk20"), ("eject", "disk21")]


def test_recognizes_all_removable_disks_on_the_same_usb_player(
    tmp_path: Path, monkeypatch
) -> None:
    player = tmp_path / "Music"
    device_tree_path = "IODeviceTree:/usb/player"
    disk_info = {
        str(player): {
            "DeviceTreePath": device_tree_path,
            "ParentWholeDisk": "disk21",
            "Internal": False,
            "Ejectable": True,
        },
        "disk19": {
            "DeviceIdentifier": "disk19",
            "DeviceTreePath": "IODeviceTree:/usb/backup",
            "WholeDisk": True,
            "Internal": False,
            "Ejectable": True,
        },
        "disk20": {
            "DeviceIdentifier": "disk20",
            "DeviceTreePath": device_tree_path,
            "WholeDisk": True,
            "Internal": False,
            "Ejectable": True,
        },
        "disk21": {
            "DeviceIdentifier": "disk21",
            "DeviceTreePath": device_tree_path,
            "WholeDisk": True,
            "Internal": False,
            "Ejectable": True,
        },
    }
    listing = {
        "WholeDisks": ["disk19", "disk20", "disk21"],
        "AllDisksAndPartitions": [
            {
                "DeviceIdentifier": "disk19",
                "Partitions": [{"MountPoint": "/Volumes/Backup"}],
            },
            {
                "DeviceIdentifier": "disk20",
                "Partitions": [{"MountPoint": "/Volumes/ECHO NANO"}],
            },
            {
                "DeviceIdentifier": "disk21",
                "Partitions": [{"MountPoint": "/Volumes/Music"}],
            },
        ],
    }
    monkeypatch.setattr(
        "nineties_music.storage._disk_info", lambda identifier: disk_info[identifier]
    )
    monkeypatch.setattr(
        "nineties_music.storage._diskutil_plist", lambda *_arguments: listing
    )

    assert _recognize_player_disks(player) == [
        {"identifier": "disk20", "mount_points": ["/Volumes/ECHO NANO"]},
        {"identifier": "disk21", "mount_points": ["/Volumes/Music"]},
    ]


def test_safely_remove_skips_companion_removed_with_primary(
    tmp_path: Path, monkeypatch
) -> None:
    companion = tmp_path / "ECHO NANO"
    companion.mkdir()
    config, _, manager = make_storage(tmp_path)
    calls = []
    monkeypatch.setattr(
        "nineties_music.storage._recognize_player_disks",
        lambda _volume: [
            {
                "identifier": "disk21",
                "mount_points": [str(config.player_volume)],
            },
            {"identifier": "disk20", "mount_points": [str(companion)]},
        ],
    )

    def fake_diskutil(*arguments):
        calls.append(arguments)
        companion.rmdir()

    monkeypatch.setattr("nineties_music.storage._run_diskutil", fake_diskutil)

    result = safely_remove_player(config, manager)

    assert calls == [("eject", "disk21")]
    assert result["volumes"] == [str(config.player_volume), str(companion)]


def test_safely_remove_refuses_active_downloads(tmp_path: Path, monkeypatch) -> None:
    config, store, manager = make_storage(tmp_path)
    manager.enqueue("https://music.youtube.com/playlist?list=OLAK_album", "album")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr("nineties_music.storage._run_diskutil", fake_run)

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
        "nineties_music.storage._recognize_player_disks",
        lambda _volume: [
            {
                "identifier": "disk21",
                "mount_points": [str(config.player_volume)],
            }
        ],
    )
    monkeypatch.setattr(
        "nineties_music.storage._run_diskutil",
        lambda *_args: (_ for _ in ()).throw(
            StorageError("The music player could not be safely removed.")
        ),
    )

    with pytest.raises(StorageError, match="could not be safely removed"):
        safely_remove_player(config, manager)
