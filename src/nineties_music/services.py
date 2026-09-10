from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from .config import AppConfig
from .discovery import MusicDiscovery
from .downloader import DownloadManager, YtDlpDownloader
from .simulator import VirtualPlayer
from .spotify import SpotifyClient
from .spotify_sync import SpotifySyncEngine
from .store import LibraryStore
from .updates import update_youtube_packages


@dataclass(frozen=True)
class AppServices:
    config: AppConfig
    store: LibraryStore
    discovery: MusicDiscovery
    downloads: DownloadManager
    simulator: VirtualPlayer | None = None
    spotify: SpotifyClient | None = None
    spotify_sync: SpotifySyncEngine | None = None


def create_services(
    config: AppConfig | None = None,
    *,
    discovery: MusicDiscovery | None = None,
    manager: DownloadManager | None = None,
    store: LibraryStore | None = None,
    spotify: SpotifyClient | None = None,
    spotify_sync: SpotifySyncEngine | None = None,
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
    spotify_client = spotify or SpotifyClient(
        config.spotify_client_id,
        config.spotify_redirect_uri,
        config.private_state_dir,
    )
    sync_engine = spotify_sync or SpotifySyncEngine(
        config.library_dir,
        config.state_dir,
        spotify_client,
        music_discovery,
        download_manager.downloader,
        storage_operation=simulator.operation if simulator else nullcontext,
    )
    return AppServices(
        config=config,
        store=library_store,
        discovery=music_discovery,
        downloads=download_manager,
        simulator=simulator,
        spotify=spotify_client,
        spotify_sync=sync_engine,
    )
