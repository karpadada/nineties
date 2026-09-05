from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


DEFAULT_PLAYER_VOLUME = "Music"
PLAYER_LIBRARY_DIRECTORY = "Music"
PLAYER_STATE_DIRECTORY = ".nineties-music"
LOOPBACK_HOST = "127.0.0.1"


def find_player_volume(volume_root: Path, volume_name: str) -> Path | None:
    """Return the mounted player volume with a case-insensitive exact name."""
    if not volume_name or Path(volume_name).name != volume_name:
        return None
    try:
        volumes = list(volume_root.iterdir())
    except OSError:
        return None
    folded_name = volume_name.casefold()
    for candidate in volumes:
        try:
            if candidate.name.casefold() == folded_name and candidate.is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _value(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name, "").strip()
    return value or None


@dataclass(frozen=True)
class AppConfig:
    project_root: Path
    library_dir: Path
    state_dir: Path
    player_volume: Path | None = None
    host: str = field(default=LOOPBACK_HOST, init=False)
    port: int = 4310
    yt_dlp_executable: str = "yt-dlp"
    require_player_volume: bool = False
    storage_mode: str = "auto"
    simulator_dir: Path | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
        storage_mode: str | None = None,
        simulator_dir: Path | None = None,
    ) -> "AppConfig":
        project_root = (project_root or Path(__file__).resolve().parents[2]).resolve()
        if environment is None:
            environment = os.environ
        explicit_library = _value(environment, "MUSIC_LIBRARY_DIR")
        explicit_state = _value(environment, "MUSIC_STATE_DIR")
        local_data_dir = Path(
            _value(environment, "MUSIC_LOCAL_DATA_DIR") or project_root
        ).expanduser()
        storage_mode = (
            storage_mode or _value(environment, "MUSIC_STORAGE_MODE") or "auto"
        )
        if storage_mode not in {"auto", "local", "simulator"}:
            raise ValueError("MUSIC_STORAGE_MODE must be auto, local, or simulator")
        player_volume = None
        if storage_mode == "simulator":
            simulator_dir = Path(
                simulator_dir
                or _value(environment, "MUSIC_SIMULATOR_DIR")
                or local_data_dir / "simulator"
            ).expanduser().resolve()
            player_volume = simulator_dir / DEFAULT_PLAYER_VOLUME
            library_dir = player_volume / PLAYER_LIBRARY_DIRECTORY
        elif explicit_library:
            library_dir = Path(explicit_library).expanduser()
        elif storage_mode == "local":
            library_dir = local_data_dir / "downloads"
        else:
            volume_root = Path(
                _value(environment, "MUSIC_VOLUME_ROOT") or "/Volumes"
            ).expanduser()
            volume_name = (
                _value(environment, "MUSIC_PLAYER_VOLUME") or DEFAULT_PLAYER_VOLUME
            )
            player_volume = find_player_volume(volume_root, volume_name)
            library_dir = (
                player_volume / PLAYER_LIBRARY_DIRECTORY
                if player_volume
                else local_data_dir / "downloads"
            )
        if storage_mode == "simulator":
            state_dir = player_volume / PLAYER_STATE_DIRECTORY
        elif explicit_state:
            state_dir = Path(explicit_state).expanduser()
        elif player_volume:
            state_dir = player_volume / PLAYER_STATE_DIRECTORY
        else:
            state_dir = local_data_dir / ".state"
        yt_dlp_executable = _value(environment, "MUSIC_YT_DLP") or "yt-dlp"
        require_player_volume = (
            storage_mode == "auto"
            and (_value(environment, "MUSIC_REQUIRE_PLAYER_VOLUME") or "").casefold()
            in {"1", "true", "yes", "on"}
        )
        raw_port = _value(environment, "MUSIC_PORT") or "4310"
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError("MUSIC_PORT must be a number") from exc
        if not 1 <= port <= 65535:
            raise ValueError("MUSIC_PORT must be between 1 and 65535")
        return cls(
            project_root=project_root,
            library_dir=library_dir.resolve(),
            state_dir=state_dir.resolve(),
            player_volume=player_volume,
            yt_dlp_executable=yt_dlp_executable,
            port=port,
            require_player_volume=require_player_volume,
            storage_mode=storage_mode,
            simulator_dir=simulator_dir if storage_mode == "simulator" else None,
        )
