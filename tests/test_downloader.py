from __future__ import annotations

import json
import multiprocessing
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nineties_music.downloader import (
    DownloadError,
    DownloadManager,
    MAX_COLLECTION_TRACKS,
    YtDlpDownloader,
    _cleanup_download_artifacts,
    _download_is_partial,
    _kind_from_url,
    _parse_progress_line,
    normalize_artist_name,
    remove_collection,
    sanitize_component,
    validate_youtube_url,
)
from nineties_music.store import LibraryStore, ManifestError, utc_now


def _collection_record(
    collection_id: str,
    source_id: str,
    directory: str | None = None,
) -> dict[str, object]:
    now = utc_now()
    return {
        "id": collection_id,
        "source_id": source_id,
        "source_url": f"https://music.youtube.com/playlist?list={source_id}",
        "kind": "playlist",
        "title": f"Fictional Playlist {source_id}",
        "artist": "Fictional Curator",
        "directory": directory or f"Playlists/Fictional Playlist [{source_id}]",
        "track_total": 1,
        "files": [],
        "status": "queued",
        "error": None,
        "progress": {},
        "created_at": now,
        "updated_at": now,
    }


def _add_collection_in_process(
    state_dir: str,
    library_dir: str,
    collection_id: str,
    source_id: str,
    directory: str,
    start,
    results,
) -> None:
    start.wait()
    try:
        store = LibraryStore(
            Path(state_dir), Path(library_dir), recover_interrupted=False
        )
        store.add(_collection_record(collection_id, source_id, directory))
    except Exception as exc:
        results.put(type(exc).__name__)
    else:
        results.put("ok")


class _ConcurrentProcessDownloader:
    def __init__(self, source_id: str, gate) -> None:
        self.source_id = source_id
        self.gate = gate

    def probe(self, url: str, kind_hint: str | None = None, **hints):
        return {
            "source_url": url,
            "source_id": self.source_id,
            "kind": kind_hint or "playlist",
            "title": f"Fictional Playlist {self.source_id}",
            "artist": "Fictional Curator",
            "directory": f"Playlists/Fictional Playlist [{self.source_id}]",
            "track_total": 1,
        }

    def download(self, collection, progress):
        progress(
            {
                "percent": "50%",
                "track_index": "1",
                "track_total": "1",
                "current_title": f"Fictional Track {self.source_id}",
            }
        )
        self.gate.wait(timeout=10)
        return {
            "status": "complete",
            "files": [],
            "error": None,
            "progress": {
                "percent": "100%",
                "track_index": "1",
                "track_total": "1",
                "current_title": "Finished",
            },
        }


def _download_collection_in_process(
    state_dir: str,
    library_dir: str,
    source_id: str,
    gate,
    results,
) -> None:
    try:
        store = LibraryStore(
            Path(state_dir), Path(library_dir), recover_interrupted=False
        )
        manager = DownloadManager(
            store,
            _ConcurrentProcessDownloader(source_id, gate),  # type: ignore[arg-type]
        )
        item = manager.enqueue(
            f"https://music.youtube.com/playlist?list={source_id}"
        )
        results.put(manager.wait(item["id"])["status"])
    except Exception as exc:
        results.put(type(exc).__name__)


def test_url_allowlist() -> None:
    assert validate_youtube_url("https://music.youtube.com/playlist?list=abc")
    assert validate_youtube_url("https://youtu.be/abc")
    for value in (
        "http://youtube.com/playlist?list=abc",
        "https://example.com/watch?v=abc",
        "https://youtube.com:444/playlist?list=abc",
        "file:///tmp/music",
        "https://user:pass@youtube.com/playlist?list=abc",
    ):
        with pytest.raises(DownloadError):
            validate_youtube_url(value)


def test_sanitize_component() -> None:
    assert sanitize_component(' Artist: <Album> / "A" ') == "Artist- -Album- - -A-"
    assert sanitize_component("...", "Fallback") == "Fallback"


def test_normalize_artist_name_removes_youtube_topic_suffix() -> None:
    assert normalize_artist_name("Fictional Artist - Topic") == "Fictional Artist"
    assert normalize_artist_name("Fictional Artist - topic") == "Fictional Artist"
    assert normalize_artist_name("On Topic") == "On Topic"


def test_progress_parser_accepts_yt_dlp_prefixes() -> None:
    expected = {
        "percent": "42.5%",
        "track_index": "2",
        "track_total": "9",
        "current_title": "Fictional Track's Title",
    }
    assert (
        _parse_progress_line(
            "[download] __PROGRESS__ 42.5%|2|9|Fictional Track's Title"
        )
        == expected
    )
    assert _parse_progress_line("__PROGRESS__42.5%|2|9|Fictional Track's Title") == expected
    assert _parse_progress_line("[download] 42.5%") is None


def test_download_forces_progress_when_printing_completed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Playlists" / "Fictional Playlist [abc]"
    target.mkdir(parents=True)
    (target / "01 - Fictional Track.mp3").write_bytes(b"synthetic-mp3-data")
    commands: list[list[str]] = []

    class FakeProcess:
        stdout = iter(["__PROGRESS__42.5%|1|1|Fictional Track\n"])

        def wait(self) -> int:
            return 0

    def fake_popen(command, **kwargs):
        commands.append(command)
        return FakeProcess()

    monkeypatch.setattr("nineties_music.downloader.subprocess.Popen", fake_popen)
    reported: list[dict[str, str]] = []

    result = YtDlpDownloader(tmp_path).download(
        {
            "source_url": "https://music.youtube.com/playlist?list=abc",
            "directory": "Playlists/Fictional Playlist [abc]",
            "track_total": 1,
        },
        reported.append,
    )

    assert "--print" in commands[0]
    assert "--progress" in commands[0]
    progress_template = commands[0][commands[0].index("--progress-template") + 1]
    assert progress_template == (
        "download:__PROGRESS__%(progress._percent_str)s|"
        "%(info.playlist_index)s|%(info.playlist_count)s|%(info.title)s"
    )
    assert reported == [
        {
            "percent": "42.5%",
            "track_index": "1",
            "track_total": "1",
            "current_title": "Fictional Track",
        }
    ]
    assert result["status"] == "complete"


def test_download_ignores_appledouble_mp3_sidecars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "Fictional Artist" / "Fictional Album"
    target.mkdir(parents=True)
    music = target / "01 - Fictional Track.mp3"
    sidecar = target / "._01 - Fictional Track.mp3"
    music.write_bytes(b"synthetic-mp3-data")
    sidecar.write_bytes(b"appledouble-metadata")

    class FakeProcess:
        stdout = iter(())

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(
        "nineties_music.downloader.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        "nineties_music.downloader._cleanup_download_artifacts",
        lambda path: None,
    )

    result = YtDlpDownloader(tmp_path).download(
        {
            "source_url": "https://music.youtube.com/playlist?list=album",
            "directory": "Fictional Artist/Fictional Album",
            "track_total": 1,
        },
        lambda progress: None,
    )

    assert result["status"] == "complete"
    assert result["files"] == [
        "Fictional Artist/Fictional Album/01 - Fictional Track.mp3"
    ]
    assert result["progress"]["track_index"] == "1"
    assert sidecar.exists()


def test_complete_track_set_wins_over_ancillary_yt_dlp_error() -> None:
    assert not _download_is_partial(return_code=1, expected=9, file_count=9)
    assert _download_is_partial(return_code=1, expected=9, file_count=8)
    assert _download_is_partial(return_code=1, expected=0, file_count=9)


def test_download_artifact_cleanup_preserves_music_and_unrelated_files(
    tmp_path: Path,
) -> None:
    music = tmp_path / "01 - Fictional Track.mp3"
    note = tmp_path / "notes.txt"
    generated = [
        tmp_path / "01 - Fictional Track.jpg",
        tmp_path / "01 - Fictional Track.webp",
        tmp_path / "01 - Fictional Track.webm.part",
        tmp_path / "01 - Fictional Track.temp.mp3",
        tmp_path / "._01 - Fictional Track.mp3",
    ]
    music.write_bytes(b"synthetic-mp3-data")
    note.write_text("keep", encoding="utf-8")
    for path in generated:
        path.write_bytes(b"generated")

    _cleanup_download_artifacts(tmp_path)

    assert music.exists()
    assert note.exists()
    assert all(not path.exists() for path in generated)


def test_youtube_music_album_playlist_is_classified_as_album() -> None:
    assert (
        _kind_from_url("https://music.youtube.com/playlist?list=OLAK5uy_album")
        == "album"
    )


def test_probe_uses_album_metadata_from_first_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    info = {
        "id": "OLAK5uy_album",
        "title": "Album - Fictional Album",
        "entries": [{"id": "track", "channel": "Fictional Artist - Topic"}],
    }

    def fake_run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr="")

    monkeypatch.setattr("nineties_music.downloader.subprocess.run", fake_run)
    downloader = YtDlpDownloader(tmp_path)
    result = downloader.probe(
        "https://music.youtube.com/playlist?list=OLAK5uy_album"
    )
    assert result["kind"] == "album"
    assert result["title"] == "Fictional Album"
    assert result["artist"] == "Fictional Artist"
    assert result["source_id"] == "OLAK5uy_album"
    assert result["directory"] == "Fictional Artist/Fictional Album"


def test_probe_updates_and_retries_after_yt_dlp_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    updates: list[str] = []

    def fake_run(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: outdated")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "PL_retry",
                    "title": "Retry Playlist",
                    "entries": [{"id": "track"}],
                }
            ),
            stderr="",
        )

    def update() -> bool:
        updates.append("updated")
        return True

    monkeypatch.setattr("nineties_music.downloader.subprocess.run", fake_run)
    downloader = YtDlpDownloader(tmp_path, compatibility_updater=update)

    result = downloader.probe(
        "https://music.youtube.com/playlist?list=PL_retry"
    )

    assert result["title"] == "Retry Playlist"
    assert calls == 2
    assert updates == ["updated"]


def test_probe_rejects_an_individual_track(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "track", "title": "Fictional Individual Track"}),
            stderr="",
        )

    monkeypatch.setattr("nineties_music.downloader.subprocess.run", fake_run)
    downloader = YtDlpDownloader(tmp_path)
    with pytest.raises(DownloadError, match="Only albums and playlists"):
        downloader.probe("https://youtu.be/track")
    assert (
        _kind_from_url("https://music.youtube.com/playlist?list=PL_mix")
        == "playlist"
    )


def test_probe_rejects_oversized_collections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "id": "too-large",
                    "title": "Oversized Playlist",
                    "entries": [{"id": str(index)} for index in range(501)],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("nineties_music.downloader.subprocess.run", fake_run)

    with pytest.raises(DownloadError, match="limited to 500 tracks"):
        YtDlpDownloader(tmp_path).probe(
            "https://music.youtube.com/playlist?list=too-large"
        )

    assert commands[0][commands[0].index("--playlist-end") + 1] == str(
        MAX_COLLECTION_TRACKS + 1
    )


def test_store_reconciliation_and_interrupted_recovery(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    store = LibraryStore(state, library)
    now = utc_now()
    collection = {
        "id": "one",
        "source_id": "source",
        "source_url": "https://music.youtube.com/playlist?list=source",
        "kind": "playlist",
        "title": "Fictional Playlist",
        "artist": "Fictional Curator",
        "directory": "Playlists/Fictional Playlist [source]",
        "track_total": 1,
        "files": ["Playlists/Fictional Playlist [source]/01 - Fictional Track.mp3"],
        "status": "downloading",
        "error": None,
        "progress": {},
        "created_at": now,
        "updated_at": now,
    }
    store.add(collection)

    reopened = LibraryStore(state, library)
    recovered = reopened.get("one")
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert reopened.reconciled(recovered)["integrity"] == "missing"

    folder = library / recovered["directory"]
    folder.mkdir(parents=True)
    file_path = library / recovered["files"][0]
    file_path.write_bytes(b"synthetic-mp3-data")
    assert reopened.reconciled(recovered)["integrity"] == "available"
    file_path.unlink()
    assert reopened.reconciled(recovered)["integrity"] == "incomplete"


def test_store_can_read_active_jobs_without_marking_them_interrupted(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    store = LibraryStore(state, library)
    collection = {
        "id": "active",
        "source_id": "source",
        "source_url": "https://music.youtube.com/playlist?list=source",
        "kind": "playlist",
        "title": "Fictional Playlist",
        "artist": "Fictional Curator",
        "directory": "Playlists/Fictional Playlist [source]",
        "track_total": 1,
        "files": [],
        "status": "downloading",
        "error": None,
        "progress": {},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    store.add(collection)

    observer = LibraryStore(state, library, recover_interrupted=False)

    assert observer.get("active")["status"] == "downloading"


def test_separate_processes_keep_both_collection_writes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    LibraryStore(state, library, recover_interrupted=False)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_add_collection_in_process,
            args=(
                str(state),
                str(library),
                f"collection-{index}",
                f"source-{index}",
                f"Playlists/Fictional Playlist {index}",
                start,
                results,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=1) for _ in processes) == ["ok", "ok"]
    assert {item["source_id"] for item in LibraryStore(state, library).all()} == {
        "source-0",
        "source-1",
    }


def test_separate_processes_cannot_reserve_the_same_source(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    LibraryStore(state, library, recover_interrupted=False)
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_add_collection_in_process,
            args=(
                str(state),
                str(library),
                f"collection-{index}",
                "same-source",
                f"Playlists/Directory {index}",
                start,
                results,
            ),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(results.get(timeout=1) for _ in processes) == [
        "ManifestError",
        "ok",
    ]
    assert len(LibraryStore(state, library).all()) == 1


def test_separate_agent_processes_can_finish_downloads_concurrently(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    LibraryStore(state, library, recover_interrupted=False)
    context = multiprocessing.get_context("spawn")
    gate = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_download_collection_in_process,
            args=(str(state), str(library), f"parallel-{index}", gate, results),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert [results.get(timeout=1) for _ in processes].count("complete") == 2
    assert {
        item["status"] for item in LibraryStore(state, library).all()
    } == {"complete"}


def test_download_claim_is_owned_by_only_one_store(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    first = LibraryStore(state, library, recover_interrupted=False)
    first.add(_collection_record("claim", "claim-source"))
    second = LibraryStore(state, library, recover_interrupted=False)

    claimed = first.claim_download("claim", "worker-one")
    rejected = second.claim_download("claim", "worker-two")

    assert claimed is not None
    assert rejected is None
    assert second.update_owned(
        "claim", "worker-two", {"status": "complete"}
    ) is None
    assert first.update_owned(
        "claim", "worker-one", {"status": "complete"}
    )["status"] == "complete"


def test_startup_recovers_only_expired_download_leases(tmp_path: Path) -> None:
    state = tmp_path / "state"
    library = tmp_path / "music"
    store = LibraryStore(state, library, recover_interrupted=False)
    store.add(_collection_record("live", "live-source"))
    store.add(_collection_record("expired", "expired-source"))
    assert store.claim_download("live", "live-worker", lease_seconds=90)
    assert store.claim_download("expired", "expired-worker", lease_seconds=-1)

    reopened = LibraryStore(state, library)

    assert reopened.get("live")["status"] == "downloading"
    assert reopened.get("expired")["status"] == "interrupted"


def test_store_rejects_untrusted_collection_data(tmp_path: Path) -> None:
    now = utc_now()
    collection = {
        "id": "malicious",
        "source_id": "source",
        "source_url": "javascript:alert(1)",
        "kind": "playlist",
        "title": "Fictional Playlist",
        "artist": "Fictional Curator",
        "directory": "Playlists/Fictional Playlist [source]",
        "track_total": 1,
        "files": [],
        "status": "complete",
        "error": None,
        "progress": {},
        "created_at": now,
        "updated_at": now,
    }
    store = LibraryStore(tmp_path / "state", tmp_path / "music")

    with pytest.raises(ManifestError, match="source URL"):
        store.add(collection)

    collection["source_url"] = "https://music.youtube.com/playlist?list=source"
    collection["directory"] = "../outside"
    with pytest.raises(ManifestError, match="path"):
        store.add(collection)


def test_safe_path_rejects_traversal_and_symlink_escape(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    with pytest.raises(ManifestError):
        store.safe_collection_path("../outside")

    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.library_dir / "link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ManifestError):
        store.safe_collection_path("link/collection")

    inside = store.library_dir / "inside"
    inside.mkdir()
    inside_link = store.library_dir / "inside-link"
    inside_link.symlink_to(inside, target_is_directory=True)
    with pytest.raises(ManifestError, match="symlink"):
        store.safe_collection_path("inside-link/collection")


class FakeDownloader:
    def __init__(self, source_id: str = "abc") -> None:
        self.source_id = source_id

    def probe(
        self,
        url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, object]:
        return {
            "source_url": url,
            "source_id": self.source_id,
            "kind": kind_hint or "playlist",
            "title": title_hint or "Fictional Playlist",
            "artist": artist_hint or "Fictional Curator",
            "directory": f"Playlists/Fictional Playlist [{self.source_id}]",
            "track_total": 2,
        }


def test_download_queue_is_bounded_before_additional_probes(tmp_path: Path) -> None:
    class CountingDownloader(FakeDownloader):
        def __init__(self) -> None:
            super().__init__()
            self.probes = 0

        def probe(self, *args, **kwargs):
            self.probes += 1
            return super().probe(*args, **kwargs)

    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    downloader = CountingDownloader()
    manager = DownloadManager(
        store,
        downloader,  # type: ignore[arg-type]
        start_worker=False,
        max_pending=1,
    )

    manager.enqueue("https://music.youtube.com/playlist?list=abc")
    with pytest.raises(DownloadError, match="queue is full"):
        manager.enqueue("https://music.youtube.com/playlist?list=def")

    assert downloader.probes == 1


class CompletingDownloader(FakeDownloader):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def download(self, collection, progress):
        self.started.set()
        progress(
            {
                "percent": "100%",
                "track_index": "2",
                "track_total": "2",
                "current_title": "Finished",
            }
        )
        return {
            "status": "complete",
            "files": [],
            "error": None,
            "progress": {
                "percent": "100%",
                "track_index": "2",
                "track_total": "2",
                "current_title": "Finished",
            },
        }


def test_worker_loads_collection_before_initial_progress_update(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    downloader = CompletingDownloader()
    manager = DownloadManager(store, downloader)  # type: ignore[arg-type]

    item = manager.enqueue("https://music.youtube.com/playlist?list=abc")

    assert downloader.started.wait(timeout=1)
    manager._queue.join()
    saved = store.get(item["id"])
    assert saved is not None
    assert saved["status"] == "complete"
    assert saved["progress"]["track_total"] == "2"


def test_new_worker_resumes_a_committed_queued_job(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    queued = _collection_record("orphaned-queue", "orphaned-source")
    store.add(queued)

    manager = DownloadManager(
        store, CompletingDownloader()  # type: ignore[arg-type]
    )
    completed = manager.wait("orphaned-queue")

    assert completed["status"] == "complete"


def test_duplicate_import_and_active_removal_are_rejected(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    item = manager.enqueue("https://music.youtube.com/playlist?list=abc")

    with pytest.raises(DownloadError, match="already managed"):
        manager.enqueue("https://music.youtube.com/playlist?list=abc")
    with pytest.raises(DownloadError, match="cannot be removed"):
        remove_collection(store, manager, item["id"])


def test_unmanaged_destination_collision_is_rejected(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    target = store.library_dir / "Playlists/Fictional Playlist [abc]"
    target.mkdir(parents=True)
    (target / "personal-file.mp3").write_bytes(b"keep")
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]

    with pytest.raises(DownloadError, match="not managed"):
        manager.enqueue("https://music.youtube.com/playlist?list=abc")
    assert (target / "personal-file.mp3").exists()
    assert store.all() == []


def test_failed_download_can_be_retried(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    item = manager.enqueue("https://music.youtube.com/playlist?list=abc")
    store.update(
        item["id"],
        {
            "status": "failed",
            "error": "HTTP Error 403: Forbidden",
        },
    )

    retried = manager.retry(item["id"])

    assert retried["status"] == "queued"
    assert retried["error"] is None
    assert retried["progress"] == {
        "percent": "0%",
        "track_index": "0",
        "track_total": "2",
        "current_title": "Waiting to retry",
    }
    assert manager._queue.get_nowait() == item["id"]
    assert manager._queue.get_nowait() == item["id"]


def test_active_or_complete_download_cannot_be_retried(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    item = manager.enqueue("https://music.youtube.com/playlist?list=abc")

    with pytest.raises(DownloadError, match="queued download cannot be retried"):
        manager.retry(item["id"])

    store.update(item["id"], {"status": "complete"})
    with pytest.raises(DownloadError, match="complete download cannot be retried"):
        manager.retry(item["id"])


def test_removal_deletes_only_collection_directory(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "state", tmp_path / "music")
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    item = manager.enqueue("https://music.youtube.com/playlist?list=abc")
    target = store.safe_collection_path(item["directory"])
    target.mkdir(parents=True)
    (target / "01 - Fictional Track.mp3").write_bytes(b"synthetic-mp3-data")
    unrelated = store.library_dir / "do-not-delete.txt"
    unrelated.write_text("safe", encoding="utf-8")
    store.update(item["id"], {"status": "complete"})

    remove_collection(store, manager, item["id"])
    assert not target.exists()
    assert unrelated.exists()
    assert store.get(item["id"]) is None
