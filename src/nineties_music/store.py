from __future__ import annotations

import copy
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


DATABASE_VERSION = 1
MAX_COLLECTIONS = 2000
MAX_COLLECTION_TRACKS = 500
DEFAULT_LEASE_SECONDS = 90
_ALLOWED_SOURCE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
_COLLECTION_FIELDS = {
    "artist",
    "created_at",
    "directory",
    "error",
    "files",
    "id",
    "kind",
    "progress",
    "source_id",
    "source_url",
    "status",
    "title",
    "track_total",
    "updated_at",
}
_PROGRESS_FIELDS = {"current_title", "percent", "track_index", "track_total"}
_STATUSES = {
    "complete",
    "deleting",
    "downloading",
    "failed",
    "interrupted",
    "partial",
    "queued",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_UNSET = object()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL UNIQUE,
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('album', 'playlist')),
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    directory TEXT NOT NULL UNIQUE,
    track_total INTEGER NOT NULL CHECK (track_total BETWEEN 1 AND 500),
    status TEXT NOT NULL CHECK (
        status IN (
            'complete', 'deleting', 'downloading', 'failed',
            'interrupted', 'partial', 'queued'
        )
    ),
    error TEXT,
    progress_percent TEXT,
    progress_track_index TEXT,
    progress_track_total TEXT,
    progress_current_title TEXT,
    worker_token TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_files (
    collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    PRIMARY KEY (collection_id, position)
);

CREATE INDEX IF NOT EXISTS collections_status_created
    ON collections(status, created_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _lease_deadline(seconds: int = DEFAULT_LEASE_SECONDS) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


class ManifestError(RuntimeError):
    """The managed library metadata could not be read or safely changed."""


class LibraryStore:
    def __init__(
        self,
        state_dir: Path,
        library_dir: Path,
        *,
        recover_interrupted: bool = True,
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.library_dir = library_dir.resolve()
        self.database_path = self.state_dir / "library.sqlite3"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()
        if recover_interrupted:
            self.recover_expired_operations()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=10,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as exc:
            raise ManifestError(f"Could not open {self.database_path}: {exc}") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provide a transactional connection and always release its file handles."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_VERSION:
                raise ManifestError("Unsupported library database version")
            connection.executescript(_SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version == 0:
                    connection.execute(f"PRAGMA user_version = {DATABASE_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        except sqlite3.Error as exc:
            raise ManifestError(
                f"Could not initialize {self.database_path}: {exc}"
            ) from exc
        finally:
            connection.close()

    def recover_expired_operations(self) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE collections
                SET status = 'interrupted', error = ?, worker_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE status = 'downloading'
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (
                    "The application stopped before this download finished.",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE collections
                SET status = 'failed', error = ?, worker_token = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE status = 'deleting'
                  AND (lease_expires_at IS NULL OR lease_expires_at < ?)
                """,
                (
                    "The application stopped before this removal finished.",
                    now,
                    now,
                ),
            )

    def all(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM collections ORDER BY created_at, rowid"
            ).fetchall()
            return [self._row_to_collection(connection, row) for row in rows]

    def get(self, collection_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            return self._row_to_collection(connection, row) if row else None

    def find_by_source(self, source_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collections WHERE source_id = ?", (source_id,)
            ).fetchone()
            return self._row_to_collection(connection, row) if row else None

    def find_by_directory(self, directory: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM collections WHERE directory = ?", (directory,)
            ).fetchone()
            return self._row_to_collection(connection, row) if row else None

    def queued_ids(self, limit: int) -> list[str]:
        if limit < 1:
            return []
        with self._connection() as connection:
            return [
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT id FROM collections
                    WHERE status = 'queued' ORDER BY created_at, rowid LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            ]

    def add(self, collection: dict[str, Any]) -> dict[str, Any]:
        candidate = copy.deepcopy(collection)
        self._validate_collection(candidate, 0)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            count = int(connection.execute("SELECT COUNT(*) FROM collections").fetchone()[0])
            if count >= MAX_COLLECTIONS:
                raise ManifestError("The library database is full")
            self._insert_collection(connection, candidate)
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ManifestError(
                "Collection ID, source, directory, or file already exists"
            ) from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not add the collection: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return candidate

    def update(self, collection_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        return self._change(
            collection_id, lambda item: item.update(copy.deepcopy(changes))
        )

    def mutate(
        self, collection_id: str, callback: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        return self._change(collection_id, callback)

    def _change(
        self, collection_id: str, callback: Callable[[dict[str, Any]], None]
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                raise KeyError(collection_id)
            collection = self._row_to_collection(connection, row)
            previous_files = collection["files"]
            callback(collection)
            collection["updated_at"] = utc_now()
            self._validate_collection(collection, 0)
            self._update_collection(
                connection,
                collection,
                replace_files=collection["files"] != previous_files,
                worker_token=None if collection["status"] != "downloading" else _UNSET,
                lease_expires_at=(
                    None if collection["status"] != "downloading" else _UNSET
                ),
            )
            connection.commit()
            return collection
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ManifestError(
                "Collection source, directory, or file already exists"
            ) from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not update the collection: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_download(
        self,
        collection_id: str,
        worker_token: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE collections
                SET status = 'downloading', error = NULL,
                    worker_token = ?, lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (worker_token, _lease_deadline(lease_seconds), utc_now(), collection_id),
            )
            if changed.rowcount != 1:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            collection = self._row_to_collection(connection, row) if row else None
            connection.commit()
            return collection
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not claim the download: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fail_queued(self, collection_id: str, error: str) -> bool:
        try:
            with self._connection() as connection:
                changed = connection.execute(
                    """
                    UPDATE collections
                    SET status = 'failed', error = ?, updated_at = ?
                    WHERE id = ? AND status = 'queued'
                    """,
                    (error[:500], utc_now(), collection_id),
                )
                return changed.rowcount == 1
        except sqlite3.Error as exc:
            raise ManifestError(f"Could not fail the queued download: {exc}") from exc

    def renew_lease(
        self,
        collection_id: str,
        worker_token: str,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        try:
            with self._connection() as connection:
                changed = connection.execute(
                    """
                    UPDATE collections SET lease_expires_at = ?
                    WHERE id = ? AND status = 'downloading' AND worker_token = ?
                    """,
                    (_lease_deadline(lease_seconds), collection_id, worker_token),
                )
                return changed.rowcount == 1
        except sqlite3.Error as exc:
            raise ManifestError(f"Could not renew the download lease: {exc}") from exc

    def update_owned(
        self,
        collection_id: str,
        worker_token: str,
        changes: dict[str, Any],
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM collections
                WHERE id = ? AND status = 'downloading' AND worker_token = ?
                """,
                (collection_id, worker_token),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            collection = self._row_to_collection(connection, row)
            previous_files = collection["files"]
            collection.update(copy.deepcopy(changes))
            collection["updated_at"] = utc_now()
            self._validate_collection(collection, 0)
            terminal = collection["status"] != "downloading"
            self._update_collection(
                connection,
                collection,
                replace_files=collection["files"] != previous_files,
                worker_token=None if terminal else worker_token,
                lease_expires_at=None if terminal else _lease_deadline(lease_seconds),
            )
            connection.commit()
            return collection
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not update the download: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def retry(
        self, collection_id: str, progress: dict[str, str]
    ) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                raise KeyError(collection_id)
            collection = self._row_to_collection(connection, row)
            if collection["status"] not in {"failed", "interrupted", "partial"}:
                raise ValueError(collection["status"])
            collection.update({"status": "queued", "error": None, "progress": progress})
            collection["updated_at"] = utc_now()
            self._validate_collection(collection, 0)
            self._update_collection(
                connection,
                collection,
                replace_files=False,
                worker_token=None,
                lease_expires_at=None,
            )
            connection.commit()
            return collection
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not retry the download: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_removal(
        self, collection_id: str, worker_token: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if row is None:
                raise KeyError(collection_id)
            collection = self._row_to_collection(connection, row)
            if collection["status"] in {"queued", "downloading", "deleting"}:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE collections
                SET status = 'deleting', worker_token = ?, lease_expires_at = ?,
                    updated_at = ? WHERE id = ?
                """,
                (worker_token, _lease_deadline(), utc_now(), collection_id),
            )
            connection.commit()
            return collection
        except sqlite3.Error as exc:
            connection.rollback()
            raise ManifestError(f"Could not claim the removal: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_removal(self, collection_id: str, worker_token: str) -> bool:
        with self._connection() as connection:
            changed = connection.execute(
                """
                DELETE FROM collections
                WHERE id = ? AND status = 'deleting' AND worker_token = ?
                """,
                (collection_id, worker_token),
            )
            return changed.rowcount == 1

    def cancel_removal(
        self,
        collection_id: str,
        worker_token: str,
        previous_status: str,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE collections
                SET status = ?, worker_token = NULL, lease_expires_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'deleting' AND worker_token = ?
                """,
                (previous_status, utc_now(), collection_id, worker_token),
            )

    def remove_record(self, collection_id: str) -> None:
        with self._connection() as connection:
            changed = connection.execute(
                "DELETE FROM collections WHERE id = ?", (collection_id,)
            )
            if changed.rowcount != 1:
                raise KeyError(collection_id)

    def safe_collection_path(self, relative: str) -> Path:
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ManifestError("Invalid managed collection path")
        candidate = self.library_dir / relative_path
        resolved_candidate = candidate.resolve()
        try:
            resolved_candidate.relative_to(self.library_dir)
        except ValueError as exc:
            raise ManifestError("Managed collection path leaves the library") from exc
        if resolved_candidate == self.library_dir:
            raise ManifestError("Refusing to operate on the library root")
        current = self.library_dir
        for part in relative_path.parts:
            current /= part
            if current.is_symlink():
                raise ManifestError("Managed collection path contains a symlink")
        return candidate

    def reconciled(self, collection: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(collection)
        expected = collection.get("files", [])
        missing: list[str] = []
        for relative in expected:
            try:
                path = self.safe_collection_path(relative)
            except ManifestError:
                missing.append(relative)
                continue
            if not path.is_file():
                missing.append(relative)
        try:
            directory_exists = self.safe_collection_path(collection["directory"]).is_dir()
        except (KeyError, ManifestError):
            directory_exists = False
        if not directory_exists:
            integrity = "missing"
        elif missing:
            integrity = "incomplete"
        else:
            integrity = "available"
        result["integrity"] = integrity
        result["missing_files"] = missing
        return result

    def _insert_collection(
        self, connection: sqlite3.Connection, collection: dict[str, Any]
    ) -> None:
        progress = collection["progress"]
        connection.execute(
            """
            INSERT INTO collections (
                id, source_id, source_url, kind, title, artist, directory,
                track_total, status, error, progress_percent,
                progress_track_index, progress_track_total,
                progress_current_title, worker_token, lease_expires_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                collection["id"], collection["source_id"], collection["source_url"],
                collection["kind"], collection["title"], collection["artist"],
                collection["directory"], collection["track_total"],
                collection["status"], collection["error"],
                progress.get("percent"), progress.get("track_index"),
                progress.get("track_total"), progress.get("current_title"),
                collection["created_at"], collection["updated_at"],
            ),
        )
        self._replace_files(connection, collection["id"], collection["files"])

    def _update_collection(
        self,
        connection: sqlite3.Connection,
        collection: dict[str, Any],
        *,
        replace_files: bool,
        worker_token: str | None | object = _UNSET,
        lease_expires_at: str | None | object = _UNSET,
    ) -> None:
        progress = collection["progress"]
        assignments = """
            source_id = ?, source_url = ?, kind = ?, title = ?, artist = ?,
            directory = ?, track_total = ?, status = ?, error = ?,
            progress_percent = ?, progress_track_index = ?,
            progress_track_total = ?, progress_current_title = ?,
            created_at = ?, updated_at = ?
        """
        values: list[Any] = [
            collection["source_id"], collection["source_url"], collection["kind"],
            collection["title"], collection["artist"], collection["directory"],
            collection["track_total"], collection["status"], collection["error"],
            progress.get("percent"), progress.get("track_index"),
            progress.get("track_total"), progress.get("current_title"),
            collection["created_at"], collection["updated_at"],
        ]
        if worker_token is not _UNSET:
            assignments += ", worker_token = ?"
            values.append(worker_token)
        if lease_expires_at is not _UNSET:
            assignments += ", lease_expires_at = ?"
            values.append(lease_expires_at)
        values.append(collection["id"])
        connection.execute(f"UPDATE collections SET {assignments} WHERE id = ?", values)
        if replace_files:
            self._replace_files(connection, collection["id"], collection["files"])

    @staticmethod
    def _replace_files(
        connection: sqlite3.Connection, collection_id: str, files: list[str]
    ) -> None:
        connection.execute(
            "DELETE FROM collection_files WHERE collection_id = ?", (collection_id,)
        )
        connection.executemany(
            """
            INSERT INTO collection_files (collection_id, position, relative_path)
            VALUES (?, ?, ?)
            """,
            ((collection_id, index, path) for index, path in enumerate(files)),
        )

    def _row_to_collection(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        progress = {
            key: row[column]
            for key, column in (
                ("percent", "progress_percent"),
                ("track_index", "progress_track_index"),
                ("track_total", "progress_track_total"),
                ("current_title", "progress_current_title"),
            )
            if row[column] is not None
        }
        files = [
            item[0]
            for item in connection.execute(
                """
                SELECT relative_path FROM collection_files
                WHERE collection_id = ? ORDER BY position
                """,
                (row["id"],),
            ).fetchall()
        ]
        return {
            "id": row["id"], "source_id": row["source_id"],
            "source_url": row["source_url"], "kind": row["kind"],
            "title": row["title"], "artist": row["artist"],
            "directory": row["directory"], "track_total": row["track_total"],
            "files": files, "status": row["status"], "error": row["error"],
            "progress": progress, "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _validate_collection(self, collection: Any, index: int) -> None:
        label = f"collection {index + 1}"
        if not isinstance(collection, dict) or set(collection) != _COLLECTION_FIELDS:
            raise ManifestError(f"Invalid fields in {label}")
        collection_id = collection["id"]
        if not isinstance(collection_id, str) or not _SAFE_ID.fullmatch(collection_id):
            raise ManifestError(f"Invalid ID in {label}")
        self._validate_text(collection, "source_id", label, 256)
        self._validate_text(collection, "title", label, 500)
        self._validate_text(collection, "artist", label, 500)
        source_url = collection["source_url"]
        if (
            not isinstance(source_url, str) or not source_url
            or source_url.strip() != source_url or len(source_url) > 2048
            or any(ord(character) < 32 for character in source_url)
        ):
            raise ManifestError(f"Invalid source URL in {label}")
        try:
            parsed_source = urlparse(source_url)
            source_hostname = (parsed_source.hostname or "").lower()
            source_port = parsed_source.port
        except ValueError as exc:
            raise ManifestError(f"Invalid source URL in {label}") from exc
        if (
            parsed_source.scheme != "https" or source_hostname not in _ALLOWED_SOURCE_HOSTS
            or parsed_source.username is not None or parsed_source.password is not None
            or source_port not in {None, 443}
        ):
            raise ManifestError(f"Invalid source URL in {label}")
        if not isinstance(collection["kind"], str) or collection["kind"] not in {
            "album",
            "playlist",
        }:
            raise ManifestError(f"Invalid kind in {label}")
        if (
            not isinstance(collection["status"], str)
            or collection["status"] not in _STATUSES
        ):
            raise ManifestError(f"Invalid status in {label}")
        track_total = collection["track_total"]
        if type(track_total) is not int or not 1 <= track_total <= MAX_COLLECTION_TRACKS:
            raise ManifestError(f"Invalid track count in {label}")
        error = collection["error"]
        if error is not None and (not isinstance(error, str) or len(error) > 500):
            raise ManifestError(f"Invalid error in {label}")
        progress = collection["progress"]
        if not isinstance(progress, dict) or not set(progress).issubset(_PROGRESS_FIELDS):
            raise ManifestError(f"Invalid progress in {label}")
        if any(not isinstance(value, str) or len(value) > 500 for value in progress.values()):
            raise ManifestError(f"Invalid progress value in {label}")
        for field in ("created_at", "updated_at"):
            value = collection[field]
            if not isinstance(value, str) or len(value) > 64:
                raise ManifestError(f"Invalid {field} in {label}")
            try:
                parsed_time = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ManifestError(f"Invalid {field} in {label}") from exc
            if parsed_time.utcoffset() is None:
                raise ManifestError(f"Invalid {field} in {label}")
        directory = collection["directory"]
        if (
            not isinstance(directory, str) or not directory
            or directory.strip() != directory or len(directory) > 512
            or any(ord(character) < 32 for character in directory)
        ):
            raise ManifestError(f"Invalid directory in {label}")
        directory_path = Path(directory)
        if directory_path.as_posix() != directory:
            raise ManifestError(f"Invalid directory in {label}")
        self.safe_collection_path(directory)
        files = collection["files"]
        if not isinstance(files, list) or len(files) > MAX_COLLECTION_TRACKS:
            raise ManifestError(f"Invalid files in {label}")
        if any(
            not isinstance(relative, str) or not relative or relative.strip() != relative
            or len(relative) > 512 or any(ord(character) < 32 for character in relative)
            for relative in files
        ):
            raise ManifestError(f"Invalid file path in {label}")
        if len(set(files)) != len(files):
            raise ManifestError(f"Duplicate files in {label}")
        if len(files) > track_total:
            raise ManifestError(f"Too many files in {label}")
        for relative in files:
            relative_path = Path(relative)
            if (
                relative_path == directory_path or relative_path.as_posix() != relative
                or relative_path.suffix.lower() != ".mp3"
            ):
                raise ManifestError(f"Invalid file path in {label}")
            try:
                relative_path.relative_to(directory_path)
            except ValueError as exc:
                raise ManifestError(f"File outside collection directory in {label}") from exc
            self.safe_collection_path(relative)

    @staticmethod
    def _validate_text(
        collection: dict[str, Any], field: str, label: str, max_chars: int
    ) -> None:
        value = collection[field]
        if not isinstance(value, str) or not value or len(value) > max_chars:
            raise ManifestError(f"Invalid {field} in {label}")
