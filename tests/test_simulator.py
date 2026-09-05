from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from nineties_music import __main__, cli
from nineties_music.agent import MusicAgentAPI
from nineties_music.config import AppConfig
from nineties_music.downloader import DownloadError, remove_collection
from nineties_music.services import create_services
from nineties_music.storage import safely_remove_player
from nineties_music.web import create_app

from test_agent import FakeDiscovery, FakeDownloader


SOURCE = "https://music.youtube.com/playlist?list=OLAK_album"


def simulator_config(tmp_path: Path) -> AppConfig:
    return AppConfig.from_environment(
        project_root=tmp_path, environment={}, storage_mode="simulator"
    )


def make_services(tmp_path: Path):
    services = create_services(
        simulator_config(tmp_path), discovery=FakeDiscovery(), start_worker=False
    )
    services.downloads.downloader = FakeDownloader()
    return services


def post(client, app, path, **data):
    return client.post(path, data={"_csrf_token": app.config["CSRF_TOKEN"], **data})


def test_local_mode_ignores_player_and_disables_installed_requirement(tmp_path):
    volumes = tmp_path / "Volumes"
    (volumes / "Music").mkdir(parents=True)
    config = AppConfig.from_environment(
        project_root=tmp_path,
        environment={
            "MUSIC_VOLUME_ROOT": str(volumes),
            "MUSIC_REQUIRE_PLAYER_VOLUME": "1",
        },
        storage_mode="local",
    )
    assert config.player_volume is None
    assert config.library_dir == tmp_path / "downloads"
    assert config.state_dir == tmp_path / ".state"
    assert not config.require_player_volume
    app = create_app(config, start_worker=False)
    response = app.test_client().get("/")
    assert b"Local storage mode" in response.data
    assert b'action="/storage/safely-remove"' not in response.data
    assert app.test_client().get("/api/storage").json == {"storage_available": True}
    assert list((volumes / "Music").iterdir()) == []


def test_simulator_paths_override_physical_environment(tmp_path):
    root = tmp_path / "test-player"
    config = AppConfig.from_environment(
        environment={
            "MUSIC_LIBRARY_DIR": "/Volumes/Music/Music",
            "MUSIC_STATE_DIR": "/Volumes/Music/.nineties-music",
            "MUSIC_REQUIRE_PLAYER_VOLUME": "1",
            "MUSIC_STORAGE_MODE": "simulator",
            "MUSIC_SIMULATOR_DIR": str(root),
        }
    )
    assert config.simulator_dir == root
    assert config.player_volume == root / "Music"
    assert config.library_dir == root / "Music/Music"
    assert config.state_dir == root / "Music/.nineties-music"
    assert not config.require_player_volume


def test_web_and_agent_share_disconnect_and_reconnect(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "nineties_music.storage._run_diskutil",
        lambda *_: pytest.fail("Simulator must never invoke diskutil"),
    )
    services = make_services(tmp_path)
    app = create_app(
        services.config, manager=services.downloads, discovery=FakeDiscovery(),
        start_worker=False,
    )
    app.testing = True
    client = app.test_client()
    agent = MusicAgentAPI(create_services(services.config, start_worker=False))
    assert b"Virtual player connected" in client.get("/").data
    assert client.post("/storage/simulator/disconnect").status_code == 403

    result = agent.safely_remove()
    assert result["safely_removed"]
    assert client.get("/api/storage").json == {"storage_available": False}
    assert b"Connect virtual player" in client.get("/").data
    assert client.get("/api/jobs").status_code == 503
    assert post(client, app, "/downloads", url=SOURCE).status_code == 503
    assert cli.run_agent_cli(services.config, ["library"]) == 2
    assert not create_services(services.config, start_worker=False).simulator.connected

    assert post(client, app, "/storage/simulator/connect").status_code == 302
    assert agent.library() == {"collections": []}
    assert client.get("/api/storage").json == {"storage_available": True}
    removed = post(client, app, "/storage/safely-remove")
    assert removed.status_code == 200
    assert b"Connect virtual player" in removed.data
    assert b"You can now disconnect the music player" not in removed.data
    assert not services.simulator.connected


@pytest.mark.parametrize("status", ["queued", "downloading", "deleting"])
def test_both_disconnect_controls_refuse_active_operations(tmp_path, status):
    services = make_services(tmp_path)
    collection = services.downloads.enqueue(SOURCE)
    services.store.update(collection["id"], {"status": status})
    app = create_app(services.config, manager=services.downloads, start_worker=False)
    for path in ("/storage/simulator/disconnect", "/storage/safely-remove"):
        assert post(app.test_client(), app, path).status_code == 409
        assert services.simulator.connected


def test_disconnected_simulator_blocks_mutations_without_touching_library(tmp_path):
    services = make_services(tmp_path)
    collection = services.downloads.enqueue(SOURCE)
    services.store.update(collection["id"], {"status": "failed"})
    services.simulator.disconnect(services.store)
    for action in (
        lambda: services.downloads.enqueue(SOURCE),
        lambda: services.downloads.retry(collection["id"]),
        lambda: remove_collection(services.store, services.downloads, collection["id"]),
        lambda: MusicAgentAPI(services).library(),
        lambda: MusicAgentAPI(services).status(),
    ):
        with pytest.raises(DownloadError, match="disconnected"):
            action()
    assert services.store.get(collection["id"])["status"] == "failed"


def test_disconnect_during_probe_prevents_late_download_reservation(tmp_path):
    services = make_services(tmp_path)
    peer = create_services(services.config, start_worker=False)
    probe_started = threading.Event()
    finish_probe = threading.Event()
    errors = []

    class SlowProbe(FakeDownloader):
        def probe(self, *args, **kwargs):
            probe_started.set()
            assert finish_probe.wait(5)
            return super().probe(*args, **kwargs)

    services.downloads.downloader = SlowProbe()

    def enqueue():
        try:
            services.downloads.enqueue(SOURCE)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=enqueue)
    thread.start()
    try:
        assert probe_started.wait(5)
        peer.simulator.disconnect(peer.store)
    finally:
        finish_probe.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], DownloadError)
    assert "disconnected" in str(errors[0])
    assert services.store.all() == []


def test_real_library_workflow_preserves_files_across_reconnection(tmp_path, monkeypatch):
    class FixtureDownloader(FakeDownloader):
        def download(self, collection, progress):
            target = config.library_dir / collection["directory"]
            target.mkdir(parents=True)
            track = target / "01 - Test tone.mp3"
            track.write_bytes(b"fixture audio")
            progress({"percent": "100%", "current_title": "Test tone"})
            return {"status": "complete", "files": [track.relative_to(config.library_dir).as_posix()]}

    config = simulator_config(tmp_path)
    monkeypatch.setattr("nineties_music.services.YtDlpDownloader", lambda *a, **k: FixtureDownloader())
    services = create_services(config)
    collection = services.downloads.enqueue(SOURCE)
    completed = services.downloads.wait(collection["id"])
    track = config.library_dir / completed["files"][0]
    unrelated = config.library_dir / "keep.txt"
    unrelated.write_text("unmanaged")
    safely_remove_player(config, services.downloads)
    assert track.read_bytes() == b"fixture audio"
    restarted = create_services(config, start_worker=False)
    assert not restarted.simulator.connected
    restarted.simulator.connect()
    assert restarted.store.reconciled(restarted.store.get(collection["id"]))["integrity"] == "available"
    remove_collection(restarted.store, restarted.downloads, collection["id"])
    assert not track.exists()
    assert unrelated.read_text() == "unmanaged"
    assert restarted.store.all() == []


def test_simulator_commands_persist_state_between_processes(tmp_path):
    root = tmp_path / "player with spaces"
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }

    def run(*args):
        return subprocess.run(
            [sys.executable, "-m", "nineties_music", *args],
            env=environment, capture_output=True, text=True, timeout=10,
        )

    for action, connected in (("status", True), ("disconnect", False), ("status", False), ("connect", True)):
        result = run("simulator", action, "--directory", str(root))
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["connected"] is connected
        assert payload["volume"] == str(root / "Music")
    result = run("agent", "--simulator-dir", str(root), "safely-remove")
    assert result.returncode == 0, result.stderr
    result = run("agent", "--simulator-dir", str(root), "library")
    assert result.returncode == 2
    assert "disconnected" in json.loads(result.stderr)["error"]
    result = run("simulator", "connect", "--directory", str(root))
    assert result.returncode == 0
    result = run("simulator", "agent", "library", "--directory", str(root))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"collections": []}


@pytest.mark.parametrize("arguments,mode", [(["web", "--local"], "local"), (["web", "--simulator"], "simulator")])
def test_web_entrypoint_selects_storage(tmp_path, monkeypatch, arguments, mode):
    monkeypatch.setenv("MUSIC_LOCAL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MUSIC_REQUIRE_PLAYER_VOLUME", "1")
    selected = []

    class App:
        def run(self, **kwargs):
            assert kwargs["host"] == "127.0.0.1"

    def create(config):
        selected.append(config)
        return App()

    monkeypatch.setattr(__main__, "create_app", create)
    __main__.main(arguments)
    assert selected[0].storage_mode == mode
    assert not selected[0].require_player_volume


def test_simulator_controls_are_unavailable_in_local_mode(tmp_path):
    config = AppConfig.from_environment(project_root=tmp_path, environment={}, storage_mode="local")
    app = create_app(config, start_worker=False)
    for action in ("connect", "disconnect"):
        assert post(app.test_client(), app, f"/storage/simulator/{action}").status_code == 404


@pytest.mark.parametrize("arguments", [
    ["web", "--local"],
    ["simulator", "web", "--directory", "player with spaces"],
    ["simulator", "status"],
    ["agent", "--simulator", "library"],
])
def test_installed_launcher_forwards_storage_commands(tmp_path, arguments):
    project = Path(__file__).resolve().parents[1]
    version = tomllib.loads((project / "pyproject.toml").read_text())["project"]["version"]
    app_data = tmp_path / "app-data"
    runtime = app_data / "runtime" / version
    runtime_bin = runtime / ".venv/bin"
    runtime_bin.mkdir(parents=True)
    (runtime / ".ready").touch()
    log = tmp_path / "arguments.log"
    executable = runtime_bin / "python"
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_ARGUMENTS_LOG"\n')
    executable.chmod(0o755)
    (runtime_bin / "yt-dlp").touch(mode=0o755)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, status in (("uv", 0), ("python3.12", 0), ("codex", 1), ("claude", 1), ("pi", 1)):
        command = fake_bin / name
        command.write_text(f"#!/bin/sh\nexit {status}\n")
        command.chmod(0o755)
    result = subprocess.run(
        [project / "scripts/nineties", *arguments],
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "MUSIC_APP_DATA_DIR": str(app_data),
            "NINETIES_UV": str(fake_bin / "uv"),
            "NINETIES_PYTHON": str(fake_bin / "python3.12"),
            "NINETIES_PACKAGE_ROOT": str(project),
            "TEST_ARGUMENTS_LOG": str(log),
        },
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines() == ["-m", "nineties_music", *arguments]
