from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import threading
import unicodedata
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

from .discovery import DiscoveryError, MusicDiscovery
from .downloader import (
    MAX_COLLECTION_TRACKS,
    DownloadError,
    YtDlpDownloader,
    sanitize_component,
)
from .spotify import SpotifyClient, SpotifyError, spotify_playlist_id


MATCH_THRESHOLD = 0.72
SPOTIFY_DIRECTORY_NAME_MAX = 48
SPOTIFY_FILENAME_MAX = 64
_QUALIFIERS = {"acoustic", "cover", "karaoke", "live", "remix", "remastered"}
SyncProgress = Callable[[dict[str, Any]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(
        character for character in value if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _normalized(left), _normalized(right)).ratio()


def score_track(source: dict[str, Any], candidate: dict[str, Any]) -> float:
    artists = ", ".join(source.get("artists") or [])
    weighted: list[tuple[float, float]] = [
        (
            0.6,
            _similarity(
                str(source.get("name") or ""), str(candidate.get("title") or "")
            ),
        ),
        (0.3, _similarity(artists, str(candidate.get("creator") or ""))),
    ]
    source_album = str(source.get("album") or "")
    candidate_album = str(candidate.get("album") or "")
    if source_album and candidate_album:
        weighted.append((0.05, _similarity(source_album, candidate_album)))
    source_duration = int(source.get("duration_seconds") or 0)
    candidate_duration = int(candidate.get("duration_seconds") or 0)
    if source_duration and candidate_duration:
        difference = abs(source_duration - candidate_duration)
        weighted.append((0.05, max(0.0, 1.0 - difference / 30)))
    score = sum(weight * value for weight, value in weighted) / sum(
        weight for weight, _value in weighted
    )
    source_words = set(_normalized(str(source.get("name") or "")).split())
    candidate_words = set(_normalized(str(candidate.get("title") or "")).split())
    if (source_words ^ candidate_words) & _QUALIFIERS:
        score -= 0.15
    return round(max(0.0, min(score, 1.0)), 3)


def best_track_match(
    source: dict[str, Any], candidates: list[dict[str, Any]]
) -> dict[str, Any] | None:
    ranked = sorted(
        (
            {**candidate, "score": score_track(source, candidate)}
            for candidate in candidates
        ),
        key=lambda item: item["score"],
        reverse=True,
    )
    if not ranked or ranked[0]["score"] < MATCH_THRESHOLD:
        return None
    return ranked[0]


class SpotifySyncEngine:
    """Mirror one Spotify playlist into a Nineties-managed MP3 directory."""

    def __init__(
        self,
        library_dir: Path,
        state_dir: Path,
        spotify: SpotifyClient,
        discovery: MusicDiscovery,
        downloader: YtDlpDownloader,
        storage_operation: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        self.library_dir = library_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.spotify = spotify
        self.discovery = discovery
        self.downloader = downloader
        self.storage_operation = storage_operation
        self._sync_state_dir = self.state_dir / "spotify-syncs"
        self._activity_lock = threading.Lock()
        self._active_count = 0

    @property
    def active(self) -> bool:
        with self._activity_lock:
            return self._active_count > 0

    def sync(
        self,
        raw_playlist_id: str,
        *,
        overrides: dict[str, str] | None = None,
        progress: SyncProgress | None = None,
    ) -> dict[str, Any]:
        playlist_id = spotify_playlist_id(raw_playlist_id)
        overrides = overrides or {}
        with self._activity_lock:
            self._active_count += 1
        try:
            with self.storage_operation():
                self._sync_state_dir.mkdir(parents=True, exist_ok=True)
                lock_path = self._sync_state_dir / f"{playlist_id}.lock"
                with lock_path.open("a+", encoding="utf-8") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    return self._sync_locked(playlist_id, overrides, progress)
        except DownloadError as exc:
            raise SpotifyError(str(exc)) from exc
        finally:
            with self._activity_lock:
                self._active_count -= 1

    def last_sync(self, raw_playlist_id: str) -> dict[str, Any] | None:
        playlist_id = spotify_playlist_id(raw_playlist_id)
        return self._read_manifest(self._manifest_path(playlist_id))

    def syncs(self) -> list[dict[str, Any]]:
        try:
            paths = list(self._sync_state_dir.glob("*.json"))
        except OSError:
            return []
        values = [self._read_manifest(path) for path in paths]
        return sorted(
            (value for value in values if value),
            key=lambda value: str(value.get("synced_at") or ""),
            reverse=True,
        )

    def reconciled(self, report: dict[str, Any]) -> dict[str, Any]:
        """Return a sync report with the MP3 files that are actually on disk."""
        value = {**report, "files": [], "disk_total": 0, "integrity": "missing"}
        directory = str(report.get("directory") or "")
        if not directory:
            return value
        try:
            target = self._safe_library_path(directory)
            if not target.is_dir() or target.is_symlink():
                return value
            files = sorted(
                path.name
                for path in target.iterdir()
                if path.is_file()
                and not path.is_symlink()
                and path.suffix.casefold() == ".mp3"
            )
        except (OSError, SpotifyError):
            return value
        expected = int(report.get("available_total") or 0)
        value.update(
            {
                "files": files,
                "disk_total": len(files),
                "integrity": "complete" if len(files) == expected else "missing files",
            }
        )
        return value

    def _sync_locked(
        self,
        playlist_id: str,
        overrides: dict[str, str],
        progress: SyncProgress | None,
    ) -> dict[str, Any]:
        playlist = self.spotify.playlist(playlist_id)
        previous = self._read_manifest(self._manifest_path(playlist_id))
        previous_directory = (
            str(previous.get("directory") or "") if previous else ""
        )
        previous_target = (
            self._safe_library_path(previous_directory)
            if previous_directory
            else None
        )
        directory = self._playlist_directory(
            str(playlist.get("name") or "Spotify playlist"),
            playlist_id,
            previous,
        )
        target = self._safe_library_path(directory)
        if target.exists() and not self._owned_by_playlist(target, playlist_id, previous):
            raise SpotifyError(
                "The Spotify playlist destination already exists and is not managed by this sync."
            )

        staging_root = self.library_dir / ".nineties-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = staging_root / f"sync-{secrets.token_hex(6)}"
        staging.mkdir()
        tracks: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        previous_tracks = self._previous_tracks(previous)
        current_files: dict[str, Path] = {}
        source_tracks = list(playlist.get("tracks") or [])
        if len(source_tracks) > MAX_COLLECTION_TRACKS:
            raise SpotifyError(
                f"Spotify playlists are limited to {MAX_COLLECTION_TRACKS} tracks."
            )
        source_track_ids = {
            str(source.get("id") or source.get("uri") or f"position-{index}")
            for index, source in enumerate(source_tracks, start=1)
        }
        unknown_overrides = sorted(set(overrides) - source_track_ids)
        if unknown_overrides:
            raise SpotifyError(
                "A --match Spotify track ID is not present in this playlist: "
                + unknown_overrides[0]
            )
        width = max(2, len(str(max(1, len(source_tracks)))))

        def report_progress(
            phase: str,
            *,
            completed_total: int,
            current_position: int = 0,
            current_title: str = "",
        ) -> None:
            if progress is None:
                return
            progress(
                {
                    "phase": phase,
                    "playlist_id": playlist_id,
                    "playlist_name": str(
                        playlist.get("name") or "Untitled playlist"
                    ),
                    "completed_total": completed_total,
                    "track_total": len(source_tracks),
                    "available_total": completed_total - len(missing),
                    "missing_total": len(missing),
                    "current_position": current_position,
                    "current_title": current_title,
                }
            )

        report_progress("starting", completed_total=0)
        try:
            for index, source in enumerate(source_tracks, start=1):
                current_title = str(source.get("name") or "Unavailable track")

                def report_track_phase(phase: str) -> None:
                    report_progress(
                        phase,
                        completed_total=index - 1,
                        current_position=index,
                        current_title=current_title,
                    )

                result = self._sync_track(
                    source,
                    index,
                    width,
                    staging,
                    previous_tracks,
                    current_files,
                    overrides,
                    report_track_phase,
                )
                tracks.append(result)
                if result["status"] == "missing":
                    missing.append(result)
                report_progress(
                    "processed",
                    completed_total=index,
                    current_position=index,
                    current_title=current_title,
                )

            report_progress("finalizing", completed_total=len(source_tracks))
            report: dict[str, Any] = {
                "playlist_id": playlist_id,
                "playlist_name": str(playlist.get("name") or "Untitled playlist"),
                "playlist_url": str(playlist.get("url") or ""),
                "owner": str(playlist.get("owner") or "Unknown owner"),
                "snapshot_id": str(playlist.get("snapshot_id") or ""),
                "directory": directory,
                "status": "partial" if missing else "complete",
                "track_total": len(source_tracks),
                "available_total": len(source_tracks) - len(missing),
                "missing_total": len(missing),
                "tracks": tracks,
                "missing_tracks": missing,
                "synced_at": _utc_now(),
            }
            self._write_manifest(staging / ".nineties-spotify.json", report)
            self._replace_target(staging, target)
            self._write_manifest(self._manifest_path(playlist_id), report)
            if (
                previous_target
                and previous_target != target
                and previous_target.exists()
                and self._owned_by_playlist(previous_target, playlist_id, previous)
            ):
                shutil.rmtree(previous_target)
            report_progress(report["status"], completed_total=len(source_tracks))
            return report
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _sync_track(
        self,
        source: dict[str, Any],
        index: int,
        width: int,
        staging: Path,
        previous_tracks: dict[str, dict[str, Any]],
        current_files: dict[str, Path],
        overrides: dict[str, str],
        progress: Callable[[str], None],
    ) -> dict[str, Any]:
        track_id = str(source.get("id") or source.get("uri") or f"position-{index}")
        artists = [str(value) for value in source.get("artists") or []]
        title = str(source.get("name") or "Unavailable track")
        suggested_query = " - ".join(filter(None, (", ".join(artists), title)))
        base = {
            "position": index,
            "spotify_track_id": track_id,
            "title": title,
            "artists": artists,
            "album": str(source.get("album") or ""),
            "spotify_url": str(source.get("spotify_url") or ""),
            "suggested_query": suggested_query,
        }
        progress("matching")
        if source.get("is_local"):
            return {
                **base,
                "status": "missing",
                "reason": "Spotify local files cannot be matched.",
            }
        if source.get("type") != "track" or not source.get("available"):
            return {**base, "status": "missing", "reason": "The Spotify track is unavailable."}

        filename = self._filename(index, width, title)
        destination = staging / filename
        existing_staged = current_files.get(track_id)
        if existing_staged and existing_staged.is_file():
            progress("reusing")
            shutil.copy2(existing_staged, destination)
            current_files[track_id] = destination
            return {
                **base,
                "status": "available",
                "matched_by": "reused",
                "youtube_url": "",
                "match_score": 1.0,
                "file": filename,
            }

        previous = previous_tracks.get(track_id)
        if previous:
            progress("reusing")
            if self._copy_previous(previous, destination):
                current_files[track_id] = destination
                return {
                    **base,
                    "status": "available",
                    "matched_by": "reused",
                    "youtube_url": str(previous.get("youtube_url") or ""),
                    "match_score": float(previous.get("match_score") or 1.0),
                    "file": filename,
                }

        youtube_url = overrides.get(track_id)
        matched_by = "override"
        score = 1.0
        candidates: list[dict[str, Any]] = []
        if not youtube_url:
            matched_by = "automatic"
            try:
                candidates = self.discovery.search_tracks(suggested_query, limit=5)
            except DiscoveryError as exc:
                return {**base, "status": "missing", "reason": str(exc)}
            match = best_track_match(source, candidates)
            if match is None:
                return {
                    **base,
                    "status": "missing",
                    "reason": "No confident YouTube Music match was found.",
                    "candidates": [
                        self._candidate_summary(item, source)
                        for item in candidates[:3]
                    ],
                }
            youtube_url = str(match["url"])
            score = float(match["score"])

        try:
            progress("downloading")
            self.downloader.download_track(youtube_url, destination)
        except DownloadError as exc:
            return {
                **base,
                "status": "missing",
                "reason": str(exc),
                "youtube_url": youtube_url,
            }
        current_files[track_id] = destination
        return {
            **base,
            "status": "available",
            "matched_by": matched_by,
            "youtube_url": youtube_url,
            "match_score": score,
            "file": filename,
        }

    def _copy_previous(self, track: dict[str, Any], destination: Path) -> bool:
        relative = str(track.get("file") or "")
        previous_directory = str(track.get("directory") or "")
        if not relative or not previous_directory:
            return False
        source = self._safe_library_path(Path(previous_directory) / relative)
        if not source.is_file() or source.is_symlink():
            return False
        shutil.copy2(source, destination)
        return True

    @staticmethod
    def _candidate_summary(
        candidate: dict[str, Any], source: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "title": str(candidate.get("title") or ""),
            "creator": str(candidate.get("creator") or ""),
            "duration_seconds": int(candidate.get("duration_seconds") or 0),
            "url": str(candidate.get("url") or ""),
            "score": score_track(source, candidate),
        }

    @staticmethod
    def _filename(index: int, width: int, title: str) -> str:
        prefix = f"{index:0{width}d} - "
        suffix = ".mp3"
        available = SPOTIFY_FILENAME_MAX - len(prefix) - len(suffix)
        name = sanitize_component(title, "Unavailable track")[:available].rstrip(" .")
        return f"{prefix}{name}{suffix}"

    def _playlist_directory(
        self,
        playlist_name: str,
        playlist_id: str,
        previous: dict[str, Any] | None,
    ) -> str:
        base = sanitize_component(playlist_name, "Spotify playlist")[
            :SPOTIFY_DIRECTORY_NAME_MAX
        ].rstrip(" .")
        claims = {
            str(value.get("directory") or ""): str(value.get("playlist_id") or "")
            for value in self.syncs()
        }
        for number in range(1, 1000):
            suffix = "" if number == 1 else f" ({number})"
            stem = base[: SPOTIFY_DIRECTORY_NAME_MAX - len(suffix)].rstrip(" .")
            directory = (Path("Playlists") / f"{stem}{suffix}").as_posix()
            target = self._safe_library_path(directory)
            if target.exists():
                if self._owned_by_playlist(target, playlist_id, previous):
                    return directory
                continue
            owner = claims.get(directory)
            if not owner or owner == playlist_id:
                return directory
        raise SpotifyError("Could not choose a short folder name for this playlist.")

    def _previous_tracks(
        self, manifest: dict[str, Any] | None
    ) -> dict[str, dict[str, Any]]:
        if not manifest:
            return {}
        directory = str(manifest.get("directory") or "")
        values: dict[str, dict[str, Any]] = {}
        for track in manifest.get("tracks") or []:
            if not isinstance(track, dict) or track.get("status") != "available":
                continue
            track_id = str(track.get("spotify_track_id") or "")
            if track_id:
                values[track_id] = {**track, "directory": directory}
        return values

    def _owned_by_playlist(
        self,
        target: Path,
        playlist_id: str,
        previous: dict[str, Any] | None,
    ) -> bool:
        if previous and str(previous.get("playlist_id") or "") == playlist_id:
            previous_directory = str(previous.get("directory") or "")
            if previous_directory:
                try:
                    if self._safe_library_path(previous_directory) == target:
                        return True
                except SpotifyError:
                    pass
        marker = self._read_manifest(target / ".nineties-spotify.json")
        return bool(marker and str(marker.get("playlist_id") or "") == playlist_id)

    def _replace_target(self, staging: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_name(f".{target.name}.backup-{secrets.token_hex(8)}")
        moved_old = False
        try:
            if target.exists():
                target.rename(backup)
                moved_old = True
            staging.rename(target)
        except Exception:
            if moved_old and backup.exists() and not target.exists():
                backup.rename(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)

    def _safe_library_path(self, relative: str | Path) -> Path:
        candidate = (self.library_dir / relative).resolve()
        try:
            candidate.relative_to(self.library_dir)
        except ValueError as exc:
            raise SpotifyError("Spotify sync path leaves the library root.") from exc
        return candidate

    def _manifest_path(self, playlist_id: str) -> Path:
        return self._sync_state_dir / f"{playlist_id}.json"

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _write_manifest(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
