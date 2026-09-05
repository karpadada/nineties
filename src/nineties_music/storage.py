from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, TypedDict

from .config import AppConfig
from .downloader import DownloadError, DownloadManager
from .simulator import VirtualPlayer


_DISKUTIL = "/usr/sbin/diskutil"
_WHOLE_DISK = re.compile(r"^disk\d+$")


class StorageError(RuntimeError):
    """The removable music storage could not be safely ejected."""


class SafeRemoveResult(TypedDict):
    safely_removed: bool
    volume: str
    volumes: list[str]


class _PlayerDisk(TypedDict):
    identifier: str
    mount_points: list[str]


def safely_remove_player(
    config: AppConfig, downloads: DownloadManager
) -> SafeRemoveResult:
    """Eject every removable disk exposed by the configured USB player."""
    if config.simulator_dir is not None:
        try:
            VirtualPlayer(config.simulator_dir).disconnect(downloads.store)
        except DownloadError as exc:
            raise StorageError(str(exc)) from exc
        volume = str(config.player_volume)
        return {"safely_removed": True, "volume": volume, "volumes": [volume]}
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

    player_disks = _recognize_player_disks(volume)
    mount_points = [
        mount_point
        for disk in player_disks
        for mount_point in disk["mount_points"]
    ]
    for disk in player_disks:
        if disk["mount_points"] and not any(
            Path(mount_point).is_dir() for mount_point in disk["mount_points"]
        ):
            continue
        _run_diskutil("eject", disk["identifier"])

    return {
        "safely_removed": True,
        "volume": str(volume),
        "volumes": mount_points or [str(volume)],
    }


def _recognize_player_disks(volume: Path) -> list[_PlayerDisk]:
    primary = _disk_info(str(volume))
    device_tree_path = primary.get("DeviceTreePath")
    primary_disk = primary.get("ParentWholeDisk")
    if (
        not isinstance(device_tree_path, str)
        or not device_tree_path
        or not isinstance(primary_disk, str)
        or not _WHOLE_DISK.fullmatch(primary_disk)
        or primary.get("Internal") is not False
        or primary.get("Ejectable") is not True
    ):
        raise StorageError("The configured Music volume is not a removable device.")

    listing = _diskutil_plist("list", "-plist", "external", "physical")
    whole_disks = listing.get("WholeDisks")
    disk_entries = listing.get("AllDisksAndPartitions")
    if not isinstance(whole_disks, list) or not isinstance(disk_entries, list):
        raise StorageError("Could not identify the removable music player.")

    mount_points_by_disk = _mount_points_by_disk(disk_entries)
    recognized: list[_PlayerDisk] = []
    for identifier in whole_disks:
        if not isinstance(identifier, str) or not _WHOLE_DISK.fullmatch(identifier):
            continue
        info = _disk_info(identifier)
        if (
            info.get("DeviceIdentifier") == identifier
            and info.get("DeviceTreePath") == device_tree_path
            and info.get("WholeDisk") is True
            and info.get("Internal") is False
            and info.get("Ejectable") is True
        ):
            recognized.append(
                {
                    "identifier": identifier,
                    "mount_points": mount_points_by_disk.get(identifier, []),
                }
            )

    if not any(disk["identifier"] == primary_disk for disk in recognized):
        raise StorageError("Could not identify the removable music player.")
    return recognized


def _mount_points_by_disk(entries: list[Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("DeviceIdentifier")
        partitions = entry.get("Partitions")
        if not isinstance(identifier, str) or not isinstance(partitions, list):
            continue
        result[identifier] = [
            mount_point
            for partition in partitions
            if isinstance(partition, dict)
            and isinstance((mount_point := partition.get("MountPoint")), str)
            and mount_point
        ]
    return result


def _disk_info(identifier: str) -> dict[str, Any]:
    return _diskutil_plist("info", "-plist", identifier)


def _diskutil_plist(*arguments: str) -> dict[str, Any]:
    result = _run_diskutil(*arguments)
    try:
        payload = plistlib.loads(result.stdout.encode())
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise StorageError("Could not identify the removable music player.") from exc
    if not isinstance(payload, dict):
        raise StorageError("Could not identify the removable music player.")
    return payload


def _run_diskutil(*arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [_DISKUTIL, *arguments],
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
            "The music player could not be safely removed. Close files using it "
            "and try again."
        ) from exc
    if result.returncode != 0:
        raise StorageError(
            "The music player could not be safely removed. Close files using it "
            "and try again."
        )
    return result
