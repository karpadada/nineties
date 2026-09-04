from pathlib import Path

from nineties_music.config import AppConfig, find_player_volume


def test_find_player_volume_matches_name_case_insensitively(tmp_path: Path) -> None:
    player = tmp_path / "Echo Nano"
    player.mkdir()

    assert find_player_volume(tmp_path, "ECHO NANO") == player.resolve()
    assert find_player_volume(tmp_path, "../Echo Nano") is None


def test_music_sd_card_is_the_default_portable_storage(tmp_path: Path) -> None:
    volume_root = tmp_path / "Volumes"
    player = volume_root / "Music"
    player.mkdir(parents=True)

    config = AppConfig.from_environment(
        project_root=tmp_path / "project",
        environment={"MUSIC_VOLUME_ROOT": str(volume_root)},
    )

    assert config.player_volume == player.resolve()
    assert config.library_dir == (player / "Music").resolve()
    assert config.state_dir == (player / ".nineties-music").resolve()


def test_local_storage_is_used_when_player_is_absent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = AppConfig.from_environment(
        project_root=project,
        environment={"MUSIC_VOLUME_ROOT": str(tmp_path / "missing")},
    )

    assert config.player_volume is None
    assert config.library_dir == (project / "downloads").resolve()
    assert config.state_dir == (project / ".state").resolve()


def test_installed_app_can_put_local_storage_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "read-only-project"
    app_data = tmp_path / "app-data"
    config = AppConfig.from_environment(
        project_root=project,
        environment={
            "MUSIC_VOLUME_ROOT": str(tmp_path / "missing"),
            "MUSIC_LOCAL_DATA_DIR": str(app_data),
        },
    )

    assert config.library_dir == (app_data / "downloads").resolve()
    assert config.state_dir == (app_data / ".state").resolve()


def test_explicit_paths_override_player_detection(tmp_path: Path) -> None:
    volume_root = tmp_path / "Volumes"
    (volume_root / "ECHO NANO").mkdir(parents=True)
    library = tmp_path / "custom-library"
    state = tmp_path / "custom-state"

    config = AppConfig.from_environment(
        project_root=tmp_path / "project",
        environment={
            "MUSIC_VOLUME_ROOT": str(volume_root),
            "MUSIC_LIBRARY_DIR": str(library),
            "MUSIC_STATE_DIR": str(state),
        },
    )

    assert config.player_volume is None
    assert config.library_dir == library.resolve()
    assert config.state_dir == state.resolve()


def test_explicit_yt_dlp_executable_is_preserved(tmp_path: Path) -> None:
    executable = "/opt/homebrew/bin/yt-dlp"

    config = AppConfig.from_environment(
        project_root=tmp_path,
        environment={
            "MUSIC_VOLUME_ROOT": str(tmp_path / "missing"),
            "MUSIC_YT_DLP": executable,
        },
    )

    assert config.yt_dlp_executable == executable


def test_web_server_is_bound_to_loopback(tmp_path: Path) -> None:
    config = AppConfig.from_environment(
        project_root=tmp_path,
        environment={"MUSIC_VOLUME_ROOT": str(tmp_path / "missing")},
    )

    assert config.host == "127.0.0.1"
