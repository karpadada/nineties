from __future__ import annotations

from pathlib import Path

from nineties_music.config import AppConfig
from nineties_music.discovery import DiscoveryError
from nineties_music.downloader import DownloadManager, YtDlpDownloader
from nineties_music.store import LibraryStore
from nineties_music.web import create_app


class FakeDiscovery:
    def search(self, query: str) -> list[dict[str, str]]:
        if query == "broken":
            raise DiscoveryError("Search is unavailable")
        return [
            {
                "kind": "album",
                "title": "Fictional Album",
                "creator": "Fictional Artist",
                "details": "1998",
                "thumbnail_url": "https://example.com/fictional-album.jpg",
                "url": "https://music.youtube.com/browse/album-id",
            }
        ]


def test_discovery_result_contract() -> None:
    from nineties_music.discovery import MusicDiscovery

    class Client:
        def search(self, query: str, filter: str, limit: int):
            if filter == "albums":
                return [
                    {
                        "browseId": "MPRE_album",
                        "playlistId": "OLAK_album",
                        "title": "Fictional Album",
                        "artist": "Fictional Artist - Topic",
                        "year": "1998",
                        "thumbnails": [
                            {
                                "url": "https://example.com/small.jpg",
                                "width": 60,
                                "height": 60,
                            },
                            {
                                "url": "https://example.com/medium.jpg",
                                "width": 120,
                                "height": 120,
                            },
                            {
                                "url": "https://example.com/large.jpg",
                                "width": 544,
                                "height": 544,
                            },
                        ],
                    }
                ]
            return [
                {
                    "browseId": "VLPL_mix",
                    "title": "Fictional Playlist",
                    "author": "Fictional Curator",
                    "itemCount": "20",
                    "thumbnails": [
                        {
                            "url": "https://example.com/fictional-playlist.jpg",
                            "width": 226,
                            "height": 226,
                        }
                    ],
                }
            ]

    discovery = MusicDiscovery()
    discovery._client = Client()
    results = discovery.search("music")
    assert results[0]["creator"] == "Fictional Artist"
    assert results[0]["thumbnail_url"] == "https://example.com/medium.jpg"
    assert results[0]["url"].endswith("list=OLAK_album")
    assert results[1]["thumbnail_url"] == "https://example.com/fictional-playlist.jpg"
    assert results[1]["url"].endswith("list=PL_mix")


def test_discovery_enforces_result_limit() -> None:
    from nineties_music.discovery import MusicDiscovery

    class Client:
        def search(self, query: str, filter: str, limit: int):
            if filter == "albums":
                return [
                    {
                        "browseId": f"MPRE_album_{index}",
                        "title": f"Album {index}",
                    }
                    for index in range(20)
                ]
            return [
                {
                    "browseId": f"VLPL_mix_{index}",
                    "title": f"Mix {index}",
                }
                for index in range(20)
            ]

    discovery = MusicDiscovery()
    discovery._client = Client()

    results = discovery.search("music", limit=3)

    assert [result["title"] for result in results] == [
        "Album 0",
        "Album 1",
        "Album 2",
        "Mix 0",
        "Mix 1",
        "Mix 2",
    ]


def test_discovery_updates_and_retries_after_search_failure() -> None:
    from nineties_music.discovery import MusicDiscovery

    class FailingClient:
        def search(self, query: str, filter: str, limit: int):
            raise RuntimeError("outdated client")

    class WorkingClient:
        def search(self, query: str, filter: str, limit: int):
            return []

    clients = iter((FailingClient(), WorkingClient()))
    updates: list[str] = []

    def update() -> bool:
        updates.append("updated")
        return True

    discovery = MusicDiscovery(
        update,
        client_factory=lambda: next(clients),
    )

    assert discovery.search("music") == []
    assert updates == ["updated"]


class FakeDownloader:
    def probe(
        self,
        url: str,
        kind_hint: str | None = None,
        title_hint: str | None = None,
        artist_hint: str | None = None,
    ) -> dict[str, object]:
        return {
            "source_url": url,
            "source_id": "album-id",
            "kind": kind_hint or "album",
            "title": title_hint or "Fictional Album",
            "artist": artist_hint or "Fictional Artist",
            "directory": "Fictional Artist/Fictional Album [album-id]",
            "track_total": 10,
        }


def make_app(tmp_path: Path):
    config = AppConfig(
        project_root=tmp_path,
        library_dir=tmp_path / "music",
        state_dir=tmp_path / "state",
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    app = create_app(
        config,
        discovery=FakeDiscovery(),  # type: ignore[arg-type]
        manager=manager,
        store=store,
        start_worker=False,
    )
    app.testing = True
    return app, store


def csrf_form(app, **values: str) -> dict[str, str]:
    return {"_csrf_token": app.config["CSRF_TOKEN"], **values}


def test_default_manager_uses_configured_yt_dlp(tmp_path: Path) -> None:
    config = AppConfig(
        project_root=tmp_path,
        library_dir=tmp_path / "music",
        state_dir=tmp_path / "state",
        yt_dlp_executable="/opt/homebrew/bin/yt-dlp",
    )

    app = create_app(config, start_worker=False)
    manager = app.extensions["download_manager"]

    assert isinstance(manager.downloader, YtDlpDownloader)
    assert manager.downloader.executable == "/opt/homebrew/bin/yt-dlp"


def test_home_and_search(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = app.test_client()
    assert client.get("/").status_code == 200
    response = client.get("/search?q=nineties")
    assert response.status_code == 200
    assert b"Fictional Album" in response.data
    assert b'src="/artwork?url=https://example.com/fictional-album.jpg"' in response.data
    assert b'alt="Cover art for Fictional Album"' in response.data
    assert b"stylesheet" not in response.data


def test_home_has_agent_setup_instructions(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"nineties plugins install codex" in response.data
    assert b"nineties plugins install claude" in response.data
    assert b"nineties plugins install pi" in response.data
    assert b"MCP" not in response.data


def test_artwork_proxy_uses_same_origin_cacheable_response(
    tmp_path: Path, monkeypatch
) -> None:
    app, _ = make_app(tmp_path)
    fetched_urls: list[str] = []

    def fake_fetch(source_url: str) -> tuple[bytes, str]:
        fetched_urls.append(source_url)
        return b"jpeg-data", "image/jpeg"

    monkeypatch.setattr("nineties_music.web._fetch_artwork", fake_fetch)
    response = app.test_client().get(
        "/artwork?url=https://yt3.googleusercontent.com/cover=w120-h120"
    )

    assert response.status_code == 200
    assert response.data == b"jpeg-data"
    assert response.content_type == "image/jpeg"
    assert response.cache_control.max_age == 86400
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert fetched_urls == [
        "https://yt3.googleusercontent.com/cover=w120-h120"
    ]


def test_artwork_proxy_rejects_untrusted_urls(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)

    response = app.test_client().get(
        "/artwork?url=http://127.0.0.1/private-image"
    )

    assert response.status_code == 404


def test_artwork_proxy_rejects_active_image_content(tmp_path: Path, monkeypatch) -> None:
    app, _ = make_app(tmp_path)
    monkeypatch.setattr(
        "nineties_music.web._fetch_artwork",
        lambda _source_url: (b"<svg><script/></svg>", "image/svg+xml"),
    )

    response = app.test_client().get(
        "/artwork?url=https://yt3.googleusercontent.com/cover.svg"
    )

    assert response.status_code == 404


def test_empty_and_failed_search(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = app.test_client()
    assert client.get("/search?q=").status_code == 400
    assert client.get("/search?q=broken").status_code == 502
    assert client.get(f"/search?q={'x' * 201}").status_code == 400


def test_local_web_security_boundary(tmp_path: Path) -> None:
    app, store = make_app(tmp_path)
    client = app.test_client()
    url = "https://music.youtube.com/browse/album-id"

    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.post("/downloads", data={"url": url}).status_code == 403
    assert store.all() == []


def test_valid_csrf_token_is_authoritative_for_browser_posts(tmp_path: Path) -> None:
    app, store = make_app(tmp_path)
    response = app.test_client().post(
        "/downloads",
        base_url="http://127.0.0.1:4310",
        headers={"Origin": "null"},
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )

    assert response.status_code == 302
    assert len(store.all()) == 1


def test_rejected_form_explains_how_to_recover(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)

    response = app.test_client().post(
        "/downloads",
        data={"url": "https://music.youtube.com/browse/album-id"},
    )

    assert response.status_code == 403
    assert b"This form has expired. Reload the page and try again." in response.data


def test_missing_required_player_disables_downloads(tmp_path: Path) -> None:
    config = AppConfig(
        project_root=tmp_path,
        library_dir=tmp_path / "fallback-music",
        state_dir=tmp_path / "fallback-state",
        require_player_volume=True,
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(
        store, FakeDownloader(), start_worker=False  # type: ignore[arg-type]
    )
    app = create_app(
        config,
        discovery=FakeDiscovery(),  # type: ignore[arg-type]
        manager=manager,
        store=store,
        start_worker=False,
    )
    app.testing = True
    client = app.test_client()

    home = client.get("/")
    assert home.status_code == 200
    assert b"Music storage is not connected" in home.data
    assert b"Downloads are disabled" in home.data
    assert b"fallback-music" not in home.data
    assert b"Inspect and download</button>" in home.data
    assert b"disabled aria-disabled" in home.data

    response = client.post(
        "/downloads",
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )
    assert response.status_code == 503
    assert b"Music storage is not connected" in response.data
    assert store.all() == []
    assert client.get("/api/jobs").status_code == 503


def test_required_player_allows_downloads_when_mounted(tmp_path: Path) -> None:
    player = tmp_path / "Music"
    player.mkdir()
    config = AppConfig(
        project_root=tmp_path,
        library_dir=player / "Music",
        state_dir=player / ".nineties-music",
        player_volume=player,
        require_player_volume=True,
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(
        store, FakeDownloader(), start_worker=False  # type: ignore[arg-type]
    )
    app = create_app(
        config,
        discovery=FakeDiscovery(),  # type: ignore[arg-type]
        manager=manager,
        store=store,
        start_worker=False,
    )
    app.testing = True

    response = app.test_client().post(
        "/downloads",
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )

    assert response.status_code == 302
    assert len(store.all()) == 1


def test_connected_player_has_safely_remove_button(tmp_path: Path) -> None:
    player = tmp_path / "Music"
    player.mkdir()
    config = AppConfig(
        project_root=tmp_path,
        library_dir=player / "Music",
        state_dir=player / ".nineties-music",
        player_volume=player,
        require_player_volume=True,
    )
    app = create_app(config, discovery=FakeDiscovery(), start_worker=False)  # type: ignore[arg-type]
    app.testing = True

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b">Safely remove</button>" in response.data
    assert b'action="/storage/safely-remove"' in response.data
    assert b"also ejects its other removable volumes" in response.data


def test_safely_remove_button_is_disabled_during_download(tmp_path: Path) -> None:
    player = tmp_path / "Music"
    player.mkdir()
    config = AppConfig(
        project_root=tmp_path,
        library_dir=player / "Music",
        state_dir=player / ".nineties-music",
        player_volume=player,
        require_player_volume=True,
    )
    store = LibraryStore(config.state_dir, config.library_dir)
    manager = DownloadManager(store, FakeDownloader(), start_worker=False)  # type: ignore[arg-type]
    manager.enqueue("https://music.youtube.com/browse/album-id", "album")
    app = create_app(
        config,
        discovery=FakeDiscovery(),  # type: ignore[arg-type]
        manager=manager,
        store=store,
        start_worker=False,
    )
    app.testing = True

    response = app.test_client().get("/")

    assert b'disabled aria-disabled="true"' in response.data
    assert b"all library operations finish" in response.data


def test_safely_remove_route_ejects_player(tmp_path: Path, monkeypatch) -> None:
    player = tmp_path / "Music"
    player.mkdir()
    config = AppConfig(
        project_root=tmp_path,
        library_dir=player / "Music",
        state_dir=player / ".nineties-music",
        player_volume=player,
        require_player_volume=True,
    )
    app = create_app(config, discovery=FakeDiscovery(), start_worker=False)  # type: ignore[arg-type]
    app.testing = True
    calls = []

    def fake_safely_remove(config, downloads):
        calls.append((config, downloads))
        return {
            "safely_removed": True,
            "volume": str(player),
            "volumes": [str(player)],
        }

    monkeypatch.setattr("nineties_music.web.safely_remove_player", fake_safely_remove)

    response = app.test_client().post(
        "/storage/safely-remove", data=csrf_form(app)
    )

    assert response.status_code == 200
    assert b"Music storage safely removed" in response.data
    assert len(calls) == 1


def test_request_body_limit_and_security_headers(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path)
    client = app.test_client()
    response = client.post(
        "/downloads",
        data=csrf_form(app, url="x" * (64 * 1024)),
    )

    assert response.status_code == 413
    assert response.headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert client.get(f"/search?padding={'x' * 8193}").status_code == 414


def test_download_route_and_job_api(tmp_path: Path) -> None:
    app, store = make_app(tmp_path)
    client = app.test_client()
    response = client.post(
        "/downloads",
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )
    assert response.status_code == 302
    collection = store.all()[0]
    assert response.headers["Location"].endswith(f"/collections/{collection['id']}")
    jobs = client.get("/api/jobs").get_json()
    assert jobs["jobs"][0]["title"] == "Fictional Album"
    assert "source_url" not in jobs["jobs"][0]

    detail = client.get(response.headers["Location"])
    assert b'http-equiv="refresh" content="2"' in detail.data


def test_failed_download_retry_route(tmp_path: Path) -> None:
    app, store = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/downloads",
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )
    collection = store.all()[0]
    store.update(
        collection["id"],
        {"status": "failed", "error": "HTTP Error 403: Forbidden"},
    )

    detail = client.get(f"/collections/{collection['id']}")
    assert b"HTTP Error 403: Forbidden" in detail.data
    assert b"Retry download" in detail.data
    assert b'http-equiv="refresh"' not in detail.data

    retried = client.post(
        f"/collections/{collection['id']}/retry", data=csrf_form(app)
    )
    assert retried.status_code == 302
    assert store.get(collection["id"])["status"] == "queued"


def test_remove_confirmation_and_post(tmp_path: Path) -> None:
    app, store = make_app(tmp_path)
    client = app.test_client()
    client.post(
        "/downloads",
        data=csrf_form(
            app,
            url="https://music.youtube.com/browse/album-id",
            kind="album",
        ),
    )
    collection = store.all()[0]
    store.update(collection["id"], {"status": "complete"})
    confirmation = client.get(f"/collections/{collection['id']}/remove")
    assert confirmation.status_code == 200
    assert b"permanently delete" in confirmation.data
    removed = client.post(
        f"/collections/{collection['id']}/remove", data=csrf_form(app)
    )
    assert removed.status_code == 302
    assert store.get(collection["id"]) is None
