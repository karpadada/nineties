from __future__ import annotations

import json
from dataclasses import replace

from nineties_music import cli
from nineties_music.config import AppConfig

from test_agent import make_api


def test_agent_cli_search_outputs_json(tmp_path, monkeypatch, capsys) -> None:
    api, _, _ = make_api(tmp_path)
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: api.services)

    result = cli.run_agent_cli(
        AppConfig(
            project_root=tmp_path,
            library_dir=tmp_path / "music",
            state_dir=tmp_path / "state",
        ),
        ["search", "Fictional Album", "--limit", "1"],
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["title"] == "Fictional Album"


def test_agent_cli_reports_domain_errors_as_json(tmp_path, monkeypatch, capsys) -> None:
    api, _, _ = make_api(tmp_path)
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: api.services)

    result = cli.run_agent_cli(
        api.services.config,
        ["status", "missing"],
    )

    assert result == 2
    payload = json.loads(capsys.readouterr().err)
    assert "job ID" in payload["error"]


def test_agent_cli_safely_remove_outputs_json(tmp_path, monkeypatch, capsys) -> None:
    api, _, _ = make_api(tmp_path)
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: api.services)
    monkeypatch.setattr(
        "nineties_music.agent.safely_remove_player",
        lambda *_args: {
            "safely_removed": True,
            "volume": "/Volumes/Music",
            "volumes": ["/Volumes/Music", "/Volumes/ECHO NANO"],
        },
    )

    result = cli.run_agent_cli(api.services.config, ["safely-remove"])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        "safely_removed": True,
        "volume": "/Volumes/Music",
        "volumes": ["/Volumes/Music", "/Volumes/ECHO NANO"],
    }


class FakeSpotify:
    configured = True
    connected = True

    def playlists(self):
        return [{"id": "playlist-id", "name": "Agent Playlist"}]

    def profile(self):
        return {"id": "user-id", "display_name": "Agent User"}


class FakeSpotifySync:
    def __init__(self):
        self.request = None

    def sync(self, playlist_id, *, overrides=None):
        self.request = (playlist_id, overrides)
        return {
            "playlist_id": playlist_id,
            "status": "complete",
            "missing_tracks": [],
        }


def test_agent_cli_lists_and_syncs_spotify_playlists(
    tmp_path, monkeypatch, capsys
) -> None:
    api, _, _ = make_api(tmp_path)
    sync = FakeSpotifySync()
    services = replace(
        api.services,
        spotify=FakeSpotify(),  # type: ignore[arg-type]
        spotify_sync=sync,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(cli, "create_services", lambda *_args, **_kwargs: services)

    listed = cli.run_agent_cli(api.services.config, ["spotify", "playlists"])
    assert listed == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["playlists"][0]["name"] == "Agent Playlist"

    synced = cli.run_agent_cli(
        api.services.config,
        [
            "spotify",
            "sync",
            "1234567890ABCDEFGHIJKL",
            "--match",
            "track-id=https://music.youtube.com/watch?v=video-id",
        ],
    )
    assert synced == 0
    assert json.loads(capsys.readouterr().out)["sync"]["status"] == "complete"
    assert sync.request == (
        "1234567890ABCDEFGHIJKL",
        {"track-id": "https://music.youtube.com/watch?v=video-id"},
    )
