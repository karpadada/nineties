from __future__ import annotations

import subprocess
from typing import TypedDict

from .config import AppConfig
from .downloader import DownloadManager


class StorageError(RuntimeError):
    """The removable music storage could not be safely ejected."""


class SafeRemoveResult(TypedDict):
    safely_removed: bool
    volume: str


def safely_remove_player(
    config: AppConfig, downloads: DownloadManager
) -> SafeRemoveResult:
    """Eject the configured player volume after all downloads have finished."""
    volume = config.player_volume
    if volume is None or not volume.is_dir():
        raise StorageError("The Music device is not connected.")
    active_operations = [
        item
        for item in downloads.store.all()
        if item.get("status") in {"queued", "downloading", "deleting"}
    ]
    if active_operations:
        raise StorageError(
            "Wait for every download or removal operation to finish before safely "
            "removing the Music device."
        )

    try:
        result = subprocess.run(
            ["/usr/sbin/diskutil", "eject", str(volume)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise StorageError(
            "Safe removal is only available on macOS with diskutil installed."
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StorageError(
            "The Music device could not be safely removed. Close files using it "
            "and try again."
        ) from exc

    if result.returncode != 0:
        raise StorageError(
            "The Music device could not be safely removed. Close files using it "
            "and try again."
        )
    return {"safely_removed": True, "volume": str(volume)}
