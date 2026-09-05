from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from .config import AppConfig
from .discovery import MusicDiscovery
from .downloader import DownloadManager, YtDlpDownloader
from .simulator import VirtualPlayer
from .store import LibraryStore
from .updates import update_youtube_packages


@dataclass(frozen=True)
class AppServices:
    config: AppConfig
    store: LibraryStore
    discovery: MusicDiscovery
    downloads: DownloadManager
    simulator: VirtualPlayer | None = None


def create_services(
    config: AppConfig | None = None,
    *,
    discovery: MusicDiscovery | None = None,
    manager: DownloadManager | None = None,
    store: LibraryStore | None = None,
    start_worker: bool = True,
    recover_interrupted: bool = True,
) -> AppServices:
    config = config or AppConfig.from_environment()
    simulator = VirtualPlayer(config.simulator_dir) if config.simulator_dir else None
    library_store = store or (
        manager.store
        if manager is not None
        else LibraryStore(
            config.state_dir,
            config.library_dir,
            recover_interrupted=recover_interrupted,
        )
    )
    music_discovery = discovery or MusicDiscovery(update_youtube_packages)
    download_manager = manager or DownloadManager(
        library_store,
        YtDlpDownloader(
            config.library_dir,
            executable=config.yt_dlp_executable,
            compatibility_updater=update_youtube_packages,
        ),
        start_worker=start_worker,
        storage_operation=simulator.operation if simulator else nullcontext,
    )
    if simulator:
        download_manager.storage_operation = simulator.operation
    return AppServices(
        config=config,
        store=library_store,
        discovery=music_discovery,
        downloads=download_manager,
        simulator=simulator,
    )
