from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .downloader import DownloadError

if TYPE_CHECKING:
    from .store import LibraryStore


class VirtualPlayer:
    """A persistent folder-backed player; no OS mount or eject operations.

    The control database lives outside the simulated volume. Its transaction
    serializes disconnect with new download/removal reservations in every
    process, while the real library database remains inside the volume.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "device.sqlite3"
        with self._transaction() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS device ("
                "id INTEGER PRIMARY KEY CHECK (id = 1), "
                "connected INTEGER NOT NULL CHECK (connected IN (0, 1)))"
            )
            # First use starts connected; restarting preserves the last state.
            connection.execute("INSERT OR IGNORE INTO device VALUES (1, 1)")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _connected(connection: sqlite3.Connection) -> bool:
        row = connection.execute("SELECT connected FROM device WHERE id = 1").fetchone()
        return bool(row[0])

    @property
    def connected(self) -> bool:
        # Status polling does not need to acquire the write gate.
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            return self._connected(connection)
        finally:
            connection.close()

    def require_connected(self) -> None:
        if not self.connected:
            raise DownloadError("The virtual player is disconnected. Connect it to continue.")

    @contextmanager
    def operation(self) -> Iterator[None]:
        with self._transaction() as connection:
            if not self._connected(connection):
                raise DownloadError("The virtual player is disconnected. Connect it to continue.")
            yield

    def connect(self) -> None:
        with self._transaction() as connection:
            connection.execute("UPDATE device SET connected = 1 WHERE id = 1")

    def disconnect(self, store: LibraryStore) -> None:
        with self._transaction() as connection:
            if any(
                item.get("status") in {"queued", "downloading", "deleting"}
                for item in store.all()
            ):
                raise DownloadError(
                    "Wait for every download or removal operation to finish before "
                    "disconnecting the virtual player."
                )
            connection.execute("UPDATE device SET connected = 0 WHERE id = 1")

    def status(self) -> dict[str, Any]:
        return {
            "storage_mode": "simulator",
            "connected": self.connected,
            "volume": str(self.root / "Music"),
            "library_dir": str(self.root / "Music" / "Music"),
            "state_dir": str(self.root / "Music" / ".nineties-music"),
        }
