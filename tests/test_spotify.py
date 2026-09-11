from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from nineties_music.spotify import (
    SpotifyClient,
    SpotifyError,
    spotify_client_id,
    spotify_playlist_id,
)
from nineties_music.spotify_sync import SpotifySyncEngine, best_track_match, score_track
from nineties_music.simulator import VirtualPlayer
from nineties_music.store import LibraryStore


PLAYLIST_ID = "1234567890ABCDEFGHIJKL"
TRACK_A = "ABCDEFGHIJKL1234567890"
TRACK_B = "ZYXWVUTSRQP09876543210"


class JsonResponse:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.value).encode("utf-8")


def test_spotify_pkce_connection_stores_no_client_secret(
    tmp_path: Path, monkeypatch
) -> None:
    client = SpotifyClient(
        "public-client-id",
        "http://127.0.0.1:4310/spotify/callback",
        tmp_path / "private",
    )
    authorization_url = client.begin_authorization()
    query = parse_qs(urlparse(authorization_url).query)

    assert query["client_id"] == ["public-client-id"]
    assert query["code_challenge_method"] == ["S256"]
    assert "playlist-read-private" in query["scope"][0]
    assert "user-read-private" not in query["scope"][0]
    assert "client_secret" not in query

    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return JsonResponse(
            {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
        )

    monkeypatch.setattr("nineties_music.spotify.urlopen", fake_urlopen)
    client.finish_authorization("authorization-code", query["state"][0])

    assert client.connected is True
    token = json.loads((tmp_path / "private" / "spotify-token.json").read_text())
    assert token["refresh_token"] == "refresh"
    body = requests[0][0].data.decode("utf-8")
    assert "code_verifier=" in body
    assert "client_secret" not in body

    separate_agent_process = SpotifyClient(
        None,
        "http://127.0.0.1:4310/spotify/callback",
        tmp_path / "private",
    )
    assert separate_agent_process.configured is True
    assert separate_agent_process.connected is True


def test_spotify_rejects_bad_playlist_identifiers() -> None:
    assert (
        spotify_playlist_id(f"https://open.spotify.com/playlist/{PLAYLIST_ID}")
        == PLAYLIST_ID
    )
    assert spotify_playlist_id(f"spotify:playlist:{PLAYLIST_ID}") == PLAYLIST_ID
    with pytest.raises(SpotifyError, match="playlist ID"):
        spotify_playlist_id("https://attacker.example/playlist/123")


def test_spotify_client_id_configuration_is_private_and_replaces_tokens(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "private"
    credentials.mkdir()
    token_path = credentials / "spotify-token.json"
    token_path.write_text('{"access_token": "old"}\n', encoding="utf-8")
    client = SpotifyClient(
        None,
        "http://127.0.0.1:4310/spotify/callback",
        credentials,
    )

    client.configure("A" * 32)

    assert client.configured is True
    assert not token_path.exists()
    app_path = credentials / "spotify-app.json"
    assert json.loads(app_path.read_text())["client_id"] == "A" * 32
    assert app_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SpotifyError, match="valid Spotify client ID"):
        spotify_client_id("not valid")


def test_spotify_playlist_reads_current_items_shape(tmp_path: Path, monkeypatch) -> None:
    client = SpotifyClient(
        "public-client-id",
        "http://127.0.0.1:4310/spotify/callback",
        tmp_path / "private",
    )

    def fake_api_get(path):
        if "?fields=" in path:
            return {
                "id": PLAYLIST_ID,
                "name": "Current Playlist",
                "owner": {"display_name": "Test Listener"},
                "snapshot_id": "snapshot-id",
                "external_urls": {
                    "spotify": f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
                },
            }
        return {
            "items": [
                {
                    "item": {
                        "id": TRACK_A,
                        "uri": f"spotify:track:{TRACK_A}",
                        "type": "track",
                        "name": "First Song",
                        "artists": [{"name": "Test Artist"}],
                        "album": {"name": "Test Album"},
                        "duration_ms": 200500,
                        "external_urls": {
                            "spotify": f"https://open.spotify.com/track/{TRACK_A}"
                        },
                    }
                }
            ],
            "next": None,
        }

    monkeypatch.setattr(client, "_api_get", fake_api_get)

    playlist = client.playlist(PLAYLIST_ID)

    assert playlist["name"] == "Current Playlist"
    assert playlist["tracks"][0]["name"] == "First Song"
    assert playlist["tracks"][0]["duration_seconds"] == 200


class FakeSpotify:
    def __init__(self, playlists):
        self._playlists = iter(playlists)

    def playlist(self, playlist_id):
        assert playlist_id == PLAYLIST_ID
        return next(self._playlists)


class FakeDiscovery:
    def search_tracks(self, query: str, limit: int = 5):
        assert limit == 5
        if "First Song" in query:
            return [
                {
                    "title": "First Song",
                    "creator": "Test Artist",
                    "album": "Test Album",
                    "duration_seconds": 201,
                    "url": "https://music.youtube.com/watch?v=first",
                }
            ]
        return [
            {
                "title": "Completely Different",
                "creator": "Someone Else",
                "album": "Elsewhere",
                "duration_seconds": 400,
                "url": "https://music.youtube.com/watch?v=wrong",
            }
        ]


class FakeTrackDownloader:
    def __init__(self):
        self.calls = []

    def download_track(self, url: str, destination: Path) -> Path:
        self.calls.append((url, destination.name))
        destination.write_bytes(b"mp3")
        return destination


def source_playlist(tracks):
    return {
        "id": PLAYLIST_ID,
        "name": "Test Playlist",
        "owner": "Test Owner",
        "snapshot_id": "snapshot",
        "url": f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        "tracks": tracks,
    }


def source_track(track_id, title):
    return {
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "type": "track",
        "name": title,
        "artists": ["Test Artist"],
        "album": "Test Album",
        "duration_seconds": 200,
        "available": True,
        "is_local": False,
        "spotify_url": f"https://open.spotify.com/track/{track_id}",
    }


def test_spotify_sync_mirrors_source_and_reports_missing_on_simulated_device(
    tmp_path: Path,
) -> None:
    simulated_device = tmp_path / "simulated-device"
    library = simulated_device / "Music"
    state = simulated_device / ".nineties-music"
    spotify = FakeSpotify(
        [
            source_playlist(
                [
                    source_track(TRACK_A, "First Song"),
                    source_track(TRACK_B, "Missing Song"),
                ]
            ),
            source_playlist([source_track(TRACK_B, "Missing Song")]),
        ]
    )
    downloader = FakeTrackDownloader()
    engine = SpotifySyncEngine(
        library,
        state,
        spotify,  # type: ignore[arg-type]
        FakeDiscovery(),  # type: ignore[arg-type]
        downloader,  # type: ignore[arg-type]
    )

    progress = []
    first = engine.sync(PLAYLIST_ID, progress=progress.append)

    assert first["status"] == "partial"
    assert first["available_total"] == 1
    assert first["missing_tracks"][0]["spotify_track_id"] == TRACK_B
    target = library / first["directory"]
    assert len(list(target.glob("*.mp3"))) == 1
    assert progress[0]["phase"] == "starting"
    assert any(update["phase"] == "downloading" for update in progress)
    assert progress[-1] == {
        "phase": "partial",
        "playlist_id": PLAYLIST_ID,
        "playlist_name": "Test Playlist",
        "completed_total": 2,
        "track_total": 2,
        "available_total": 1,
        "missing_total": 1,
        "current_position": 0,
        "current_title": "",
    }

    second = engine.sync(
        PLAYLIST_ID,
        overrides={TRACK_B: "https://music.youtube.com/watch?v=second"},
    )

    assert second["status"] == "complete"
    assert second["track_total"] == 1
    assert second["directory"] == "Playlists/Test Playlist"
    files = list(target.glob("*.mp3"))
    assert len(files) == 1
    assert files[0].name == "01 - Missing Song.mp3"
    assert len(files[0].name) <= 64
    assert TRACK_B not in files[0].name
    assert (target / ".nineties-spotify.json").is_file()
    reconciled = engine.reconciled(second)
    assert reconciled["files"] == ["01 - Missing Song.mp3"]
    assert reconciled["disk_total"] == 1
    assert reconciled["integrity"] == "complete"


def test_spotify_sync_migrates_legacy_id_paths_to_short_names(tmp_path: Path) -> None:
    playlist = source_playlist([source_track(TRACK_A, "First Song")])
    library = tmp_path / "device" / "Music"
    state = tmp_path / "device" / ".nineties-music"
    downloader = FakeTrackDownloader()
    engine = SpotifySyncEngine(
        library,
        state,
        FakeSpotify([playlist, playlist]),  # type: ignore[arg-type]
        FakeDiscovery(),  # type: ignore[arg-type]
        downloader,  # type: ignore[arg-type]
    )
    first = engine.sync(PLAYLIST_ID)
    short_target = library / first["directory"]
    legacy_directory = f"Playlists/Test Playlist [spotify-{PLAYLIST_ID}]"
    legacy_target = library / legacy_directory
    short_target.rename(legacy_target)
    first["directory"] = legacy_directory
    (legacy_target / ".nineties-spotify.json").write_text(
        json.dumps(first), encoding="utf-8"
    )
    (state / "spotify-syncs" / f"{PLAYLIST_ID}.json").write_text(
        json.dumps(first), encoding="utf-8"
    )

    migrated = engine.sync(PLAYLIST_ID)

    assert migrated["directory"] == "Playlists/Test Playlist"
    assert not legacy_target.exists()
    assert (library / migrated["directory"] / "01 - First Song.mp3").is_file()
    assert len(downloader.calls) == 1


def test_spotify_sync_uses_short_numeric_suffix_for_folder_collision(
    tmp_path: Path,
) -> None:
    library = tmp_path / "device" / "Music"
    occupied = library / "Playlists" / "Test Playlist"
    occupied.mkdir(parents=True)
    (occupied / "unrelated.mp3").write_bytes(b"do-not-touch")
    engine = SpotifySyncEngine(
        library,
        tmp_path / "device" / ".nineties-music",
        FakeSpotify(  # type: ignore[arg-type]
            [source_playlist([source_track(TRACK_A, "First Song")])]
        ),
        FakeDiscovery(),  # type: ignore[arg-type]
        FakeTrackDownloader(),  # type: ignore[arg-type]
    )

    report = engine.sync(PLAYLIST_ID)

    assert report["directory"] == "Playlists/Test Playlist (2)"
    assert (occupied / "unrelated.mp3").is_file()


def test_spotify_sync_reuses_unchanged_track(tmp_path: Path) -> None:
    playlist = source_playlist([source_track(TRACK_A, "First Song")])
    downloader = FakeTrackDownloader()
    engine = SpotifySyncEngine(
        tmp_path / "device" / "Music",
        tmp_path / "device" / ".nineties-music",
        FakeSpotify([playlist, playlist]),  # type: ignore[arg-type]
        FakeDiscovery(),  # type: ignore[arg-type]
        downloader,  # type: ignore[arg-type]
    )

    first = engine.sync(PLAYLIST_ID)
    second = engine.sync(PLAYLIST_ID)

    assert first["status"] == second["status"] == "complete"
    assert len(downloader.calls) == 1
    assert second["tracks"][0]["matched_by"] == "reused"


def test_disconnected_simulator_blocks_spotify_sync(tmp_path: Path) -> None:
    player = VirtualPlayer(tmp_path / "simulator")
    library = player.root / "Music" / "Music"
    state = player.root / "Music" / ".nineties-music"
    store = LibraryStore(state, library)
    player.disconnect(store)
    engine = SpotifySyncEngine(
        library,
        state,
        FakeSpotify([source_playlist([])]),  # type: ignore[arg-type]
        FakeDiscovery(),  # type: ignore[arg-type]
        FakeTrackDownloader(),  # type: ignore[arg-type]
        storage_operation=player.operation,
    )

    with pytest.raises(SpotifyError, match="disconnected"):
        engine.sync(PLAYLIST_ID)

    assert not (state / "spotify-syncs").exists()


def test_track_matching_uses_metadata_and_rejects_wrong_variant() -> None:
    source = source_track(TRACK_A, "First Song")
    exact = {
        "title": "First Song",
        "creator": "Test Artist",
        "album": "Test Album",
        "duration_seconds": 201,
        "url": "https://music.youtube.com/watch?v=exact",
    }
    live = {
        **exact,
        "title": "First Song (Live)",
        "url": "https://music.youtube.com/watch?v=live",
    }

    assert score_track(source, exact) > score_track(source, live)
    assert best_track_match(source, [live, exact])["url"] == exact["url"]
