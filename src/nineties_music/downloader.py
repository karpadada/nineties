from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .store import LibraryStore, ManifestError, utc_now
from .updates import CompatibilityUpdater, update_youtube_packages


ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
ACTIVE_STATUSES = {"queued", "downloading"}
RETRYABLE_STATUSES = {"failed", "interrupted", "partial"}
PROGRESS_PREFIX = "__PROGRESS__"
FILE_PREFIX = "__FILE__"
MAX_COLLECTION_TRACKS = 500
MAX_PENDING_DOWNLOADS = 8
MAX_SOURCE_URL_CHARS = 2048
_TOPIC_SUFFIX = re.compile(r"\s+-\s+Topic$", re.IGNORECASE)


class DownloadError(RuntimeError):
    pass


class YoutubeCompatibilityError(DownloadError):
    pass


def validate_youtube_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url or len(url) > MAX_SOURCE_URL_CHARS:
        raise DownloadError("Enter an HTTPS YouTube or YouTube Music URL.")
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise DownloadError("Enter an HTTPS YouTube or YouTube Music URL.") from exc
    if (
        parsed.scheme != "https"
        or hostname not in ALLOWED_HOSTS
        or port not in {None, 443}
    ):
        raise DownloadError("Enter an HTTPS YouTube or YouTube Music URL.")
    if parsed.username or parsed.password:
        raise DownloadError("Credentials are not allowed in a source URL.")
    return url


def sanitize_component(value: str, fallback: str = "Unknown") -> str:
    cleaned = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "-", value).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned or fallback)[:100]


def normalize_artist_name(value: str) -> str:
    """Remove YouTube's auto-generated Topic channel suffix from an artist."""
    return _TOPIC_SUFFIX.sub("", value.strip()).strip()


class YtDlpDownloader:
    def __init__(
        self,
        library_dir: Path,
        executable: str = "yt-dlp",
        compatibility_updater: CompatibilityUpdater = update_youtube_packages,
    ) -> None:
        self.library_dir = library_dir.resolve()
        self.executable = executable
        self._compatibility_updater = compatibility_updater

    def probe(
        self,
        raw_url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, Any]:
        url = validate_youtube_url(raw_url)
        try:
            return self._probe_once(
                url, kind_hint, title_hint=title_hint, artist_hint=artist_hint
            )
        except YoutubeCompatibilityError as first_error:
            if not self._compatibility_updater():
                raise first_error
            try:
                return self._probe_once(
                    url, kind_hint, title_hint=title_hint, artist_hint=artist_hint
                )
            except YoutubeCompatibilityError as retry_error:
                raise DownloadError(
                    f"{retry_error} (compatibility packages were updated and the "
                    "operation was retried)"
                ) from retry_error

    def _probe_once(
        self,
        url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, Any]:
        command = [
            self.executable,
            "--dump-single-json",
            "--flat-playlist",
            "--playlist-end",
            str(MAX_COLLECTION_TRACKS + 1),
            "--no-warnings",
            "--skip-download",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            url,
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise YoutubeCompatibilityError(
                f"Could not inspect that collection: {exc}"
            ) from exc
        if result.returncode != 0:
            message = _last_error(result.stderr) or "yt-dlp could not read that URL."
            raise YoutubeCompatibilityError(message)
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise YoutubeCompatibilityError(
                "yt-dlp returned invalid collection information."
            ) from exc

        entries = info.get("entries")
        if not isinstance(entries, list) or not entries:
            raise DownloadError("Only albums and playlists can be downloaded.")
        if len(entries) > MAX_COLLECTION_TRACKS:
            raise DownloadError(
                f"Collections are limited to {MAX_COLLECTION_TRACKS} tracks."
            )
        source_id = str(info.get("id") or _source_id_from_url(url) or "")
        if not source_id:
            raise DownloadError("This collection has no stable source identifier.")
        kind = kind_hint if kind_hint in {"album", "playlist"} else _kind_from_url(url)
        title = str(title_hint or info.get("title") or "Untitled collection").strip()
        if kind == "album" and title.lower().startswith("album - "):
            title = title[8:].strip()
        first_entry = next(
            (entry for entry in entries if isinstance(entry, dict)), {}
        )
        artist = (
            normalize_artist_name(
                str(
                    artist_hint
                    or info.get("album_artist")
                    or info.get("artist")
                    or info.get("uploader")
                    or info.get("channel")
                    or first_entry.get("artist")
                    or first_entry.get("uploader")
                    or first_entry.get("channel")
                    or "Unknown artist"
                )
            )
            or "Unknown artist"
        )
        if kind == "album":
            directory = Path(sanitize_component(artist)) / sanitize_component(title)
        else:
            source_key = sanitize_component(source_id, "source")
            directory = Path("Playlists") / f"{sanitize_component(title)} [{source_key}]"
        return {
            "source_url": url,
            "source_id": source_id,
            "kind": kind,
            "title": title,
            "artist": artist,
            "directory": directory.as_posix(),
            "track_total": len(entries),
        }

    def download(
        self,
        collection: dict[str, Any],
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        try:
            result = self._download_once(collection, progress)
        except YoutubeCompatibilityError as first_error:
            if not self._compatibility_updater():
                raise first_error
            try:
                return self._download_once(collection, progress)
            except YoutubeCompatibilityError as retry_error:
                raise DownloadError(
                    f"{retry_error} (compatibility packages were updated and the "
                    "operation was retried)"
                ) from retry_error

        if result["status"] == "partial" and self._compatibility_updater():
            return self._download_once(collection, progress)
        return result

    def _download_once(
        self,
        collection: dict[str, Any],
        progress: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        target = (self.library_dir / collection["directory"]).resolve()
        try:
            target.relative_to(self.library_dir)
        except ValueError as exc:
            raise DownloadError("Download directory leaves the library root.") from exc
        target.mkdir(parents=True, exist_ok=True)
        output_template = str(target / "%(playlist_index)02d - %(title).160B.%(ext)s")
        command = [
            self.executable,
            "--newline",
            "--progress",
            "--no-overwrites",
            "--windows-filenames",
            "--yes-playlist",
            "--ignore-errors",
            "--socket-timeout",
            "30",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--extract-audio",
            "--audio-format",
            "mp3",
            "--audio-quality",
            "0",
            "--embed-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--output",
            output_template,
            "--progress-template",
            f"download:{PROGRESS_PREFIX}%(progress._percent_str)s|%(info.playlist_index)s|%(info.playlist_count)s|%(info.title)s",
            "--print",
            f"after_move:{FILE_PREFIX}%(filepath)s",
            collection["source_url"],
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise YoutubeCompatibilityError(f"Could not start yt-dlp: {exc}") from exc

        recent_output: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip()
            parsed_progress = _parse_progress_line(line)
            if parsed_progress is not None:
                progress(parsed_progress)
            elif line.startswith(FILE_PREFIX):
                continue
            elif line:
                recent_output.append(line)
                recent_output = recent_output[-12:]

        return_code = process.wait()
        _cleanup_download_artifacts(target)
        files = sorted(
            path
            for path in target.rglob("*.mp3")
            if path.is_file() and not path.name.startswith("._")
        )
        relative_files = [path.relative_to(self.library_dir).as_posix() for path in files]
        expected = int(collection.get("track_total") or 0)
        if not files:
            raise YoutubeCompatibilityError(
                _last_error("\n".join(recent_output)) or "No MP3 files were created."
            )
        partial = _download_is_partial(return_code, expected, len(files))
        return {
            "status": "partial" if partial else "complete",
            "files": relative_files,
            "error": (
                "Some tracks could not be downloaded."
                if partial
                else None
            ),
            "progress": {
                "percent": "100%",
                "track_index": str(len(files)),
                "track_total": str(expected or len(files)),
                "current_title": "Finished",
            },
        }


class DownloadManager:
    def __init__(
        self,
        store: LibraryStore,
        downloader: YtDlpDownloader,
        start_worker: bool = True,
        *,
        max_pending: int = MAX_PENDING_DOWNLOADS,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self.store = store
        self.downloader = downloader
        self._queue: queue.Queue[str] = queue.Queue(maxsize=max_pending)
        self._enqueue_lock = threading.Lock()
        self._active_id: str | None = None
        self._worker: threading.Thread | None = None
        if start_worker:
            for collection_id in self.store.queued_ids(max_pending):
                self._queue.put_nowait(collection_id)
            self._worker = threading.Thread(
                target=self._run, name="download-worker", daemon=True
            )
            self._worker.start()

    def enqueue(
        self,
        url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, Any]:
        title_hint = (title_hint or "").strip()[:200] or None
        artist_hint = (artist_hint or "").strip()[:200] or None
        with self._enqueue_lock:
            if self._queue.full():
                raise DownloadError("The download queue is full. Try again later.")
            probed = self.downloader.probe(
                url, kind_hint, title_hint=title_hint, artist_hint=artist_hint
            )
            track_total = probed.get("track_total")
            if (
                type(track_total) is not int
                or track_total < 1
                or track_total > MAX_COLLECTION_TRACKS
            ):
                raise DownloadError(
                    f"Collections must contain between 1 and {MAX_COLLECTION_TRACKS} tracks."
                )
            existing = self.store.find_by_source(probed["source_id"])
            if existing:
                raise DownloadError(
                    f"{existing['title']} is already managed. Remove it before importing it again."
                )
            destination = self.store.safe_collection_path(probed["directory"])
            if destination.exists():
                raise DownloadError(
                    "The destination folder already exists and is not managed by this app."
                )
            now = utc_now()
            collection = {
                "id": uuid.uuid4().hex,
                **probed,
                "status": "queued",
                "files": [],
                "error": None,
                "progress": {
                    "percent": "0%",
                    "track_index": "0",
                    "track_total": str(probed["track_total"]),
                    "current_title": "Waiting",
                },
                "created_at": now,
                "updated_at": now,
            }
            try:
                self.store.add(collection)
            except ManifestError as exc:
                existing = self.store.find_by_source(probed["source_id"])
                if existing:
                    raise DownloadError(
                        f"{existing['title']} is already managed. "
                        "Remove it before importing it again."
                    ) from exc
                if self.store.find_by_directory(probed["directory"]):
                    raise DownloadError(
                        "Another managed collection already owns that destination folder."
                    ) from exc
                raise DownloadError("The collection metadata is invalid.") from exc
            try:
                self._queue.put_nowait(collection["id"])
            except queue.Full as exc:
                self.store.remove_record(collection["id"])
                raise DownloadError(
                    "The download queue is full. Try again later."
                ) from exc
        return collection

    def is_active(self, collection_id: str) -> bool:
        collection = self.store.get(collection_id)
        return bool(collection and collection.get("status") in ACTIVE_STATUSES)

    def retry(self, collection_id: str) -> dict[str, Any]:
        with self._enqueue_lock:
            collection = self.store.get(collection_id)
            if collection is None:
                raise KeyError(collection_id)
            status = collection.get("status")
            if status not in RETRYABLE_STATUSES:
                raise DownloadError(f"A {status or 'unknown'} download cannot be retried.")
            if self._queue.full():
                raise DownloadError("The download queue is full. Try again later.")
            try:
                collection = self.store.retry(
                    collection_id,
                    {
                        "percent": "0%",
                        "track_index": "0",
                        "track_total": str(collection.get("track_total") or ""),
                        "current_title": "Waiting to retry",
                    },
                )
            except ValueError as exc:
                current_status = str(exc)
                raise DownloadError(
                    f"A {current_status or 'unknown'} download cannot be retried."
                ) from exc
            self._queue.put_nowait(collection_id)
            return collection

    def jobs(self) -> list[dict[str, Any]]:
        jobs = [
            item for item in self.store.all() if item.get("status") in ACTIVE_STATUSES
        ]
        return sorted(jobs, key=lambda item: item.get("created_at", ""))

    def wait(self, collection_id: str) -> dict[str, Any]:
        """Wait for queued work and return the requested collection's final state."""
        if self._worker is None:
            raise DownloadError("The download worker is not running.")
        self._queue.join()
        next_recovery = time.monotonic()
        while True:
            if time.monotonic() >= next_recovery:
                self.store.recover_expired_operations()
                next_recovery = time.monotonic() + 5
            collection = self.store.get(collection_id)
            if collection is None:
                raise DownloadError("The downloaded collection is no longer managed.")
            if collection.get("status") not in ACTIVE_STATUSES:
                return collection
            time.sleep(0.2)

    def _run(self) -> None:
        while True:
            collection_id = self._queue.get()
            self._active_id = collection_id
            worker_token = uuid.uuid4().hex
            heartbeat_stop = threading.Event()
            heartbeat: threading.Thread | None = None
            claimed = False
            try:
                collection = self.store.claim_download(collection_id, worker_token)
                if collection is None:
                    continue
                claimed = True
                collection = self.store.update_owned(
                    collection_id,
                    worker_token,
                    {
                        "progress": {
                            "percent": "0%",
                            "track_index": "0",
                            "track_total": str(collection.get("track_total") or ""),
                            "current_title": "Starting yt-dlp",
                        }
                    },
                )
                if collection is None:
                    continue

                def heartbeat_lease() -> None:
                    while not heartbeat_stop.wait(30):
                        try:
                            renewed = self.store.renew_lease(
                                collection_id, worker_token
                            )
                        except ManifestError:
                            continue
                        if not renewed:
                            return

                heartbeat = threading.Thread(
                    target=heartbeat_lease,
                    name=f"download-heartbeat-{collection_id[:8]}",
                    daemon=True,
                )
                heartbeat.start()
                last_progress_at = 0.0
                last_progress_title: str | None = None

                def report(progress: dict[str, Any]) -> None:
                    nonlocal last_progress_at, last_progress_title
                    now = time.monotonic()
                    title = str(progress.get("current_title") or "")
                    if now - last_progress_at < 1 and title == last_progress_title:
                        return
                    updated = self.store.update_owned(
                        collection_id,
                        worker_token,
                        {"progress": progress},
                    )
                    if updated is None:
                        raise DownloadError("The download lease is no longer active.")
                    last_progress_at = now
                    last_progress_title = title

                result = self.downloader.download(collection, report)
                if self.store.update_owned(
                    collection_id, worker_token, result
                ) is None:
                    raise DownloadError("The download lease is no longer active.")
            except Exception as exc:
                try:
                    if claimed:
                        self.store.update_owned(
                            collection_id,
                            worker_token,
                            {"status": "failed", "error": _safe_error(exc)},
                        )
                    else:
                        self.store.fail_queued(collection_id, _safe_error(exc))
                except ManifestError:
                    pass
            finally:
                heartbeat_stop.set()
                if heartbeat is not None:
                    heartbeat.join(timeout=1)
                self._active_id = None
                self._queue.task_done()


def remove_collection(
    store: LibraryStore, manager: DownloadManager, collection_id: str
) -> None:
    removal_token = uuid.uuid4().hex
    collection = store.claim_removal(collection_id, removal_token)
    if collection is None:
        raise DownloadError("A queued or running download cannot be removed.")
    target = store.safe_collection_path(collection["directory"])
    try:
        if target.is_symlink():
            raise ManifestError("Refusing to remove a symlinked collection directory")
        if target.exists():
            if not target.is_dir():
                raise ManifestError("The managed collection path is not a directory")
            shutil.rmtree(target)
        if not store.finish_removal(collection_id, removal_token):
            raise ManifestError("The collection removal claim is no longer active")
    except Exception:
        store.cancel_removal(
            collection_id,
            removal_token,
            str(collection["status"]),
        )
        raise


def _source_id_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("list"):
        return query["list"][0]
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    return parsed.path.rstrip("/").split("/")[-1]


def _kind_from_url(url: str) -> str:
    parsed = urlparse(url)
    playlist_id = (parse_qs(parsed.query).get("list") or [""])[0]
    if parsed.path.startswith("/browse/") or playlist_id.startswith("OLAK"):
        return "album"
    return "playlist"


def _last_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [line for line in lines if "error" in line.lower()]
    value = errors[-1] if errors else (lines[-1] if lines else "")
    return value[:500]


def _parse_progress_line(line: str) -> dict[str, str] | None:
    marker_index = line.find(PROGRESS_PREFIX)
    if marker_index < 0:
        return None
    fields = line[marker_index + len(PROGRESS_PREFIX) :].split("|", 3)
    if len(fields) != 4:
        return None
    return {
        "percent": fields[0].strip(),
        "track_index": fields[1].strip(),
        "track_total": fields[2].strip(),
        "current_title": fields[3].strip(),
    }


def _download_is_partial(return_code: int, expected: int, file_count: int) -> bool:
    if expected > 0:
        return file_count < expected
    return return_code != 0


def _cleanup_download_artifacts(target: Path) -> None:
    generated_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".part", ".ytdl"}
    for path in target.iterdir():
        if not path.is_file():
            continue
        if (
            path.name.startswith("._")
            or path.suffix.lower() in generated_suffixes
            or ".temp." in path.name
        ):
            path.unlink(missing_ok=True)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, (DownloadError, ManifestError)):
        return str(exc)[:500]
    return f"Download failed: {exc}"[:500]
